// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Selects a static Uniswap v4 fee for each new X/Q pool. No hook is
/// involved: the chosen fee is placed in PoolKey.fee when the pool is created.
/// The trusted controller must select before the first open attempt and pass
/// the returned fee to every open retry for that launch token.
/// @dev Fees are v4 pips (1,000,000 = 100%). The 46 arms are 5%, 6%, ...,
/// 50%. A fixed 15% exploration probability samples a random arm; otherwise
/// the arm with the highest mean realized net return is used. Selection has
/// a fixed upper bound of 46 arm reads and never scans launches or positions.
/// No-trade and stuck observations are censored and excluded from that mean.
/// Launch tokens differ in flow, liquidity, and volatility, so historical arm
/// means are observational feedback, not causal estimates of a fee's effect.
contract StaticNextPoolFee {
    uint24 public constant MIN_FEE_PIPS = 50_000;
    uint24 public constant MAX_FEE_PIPS = 500_000;
    uint24 public constant FEE_STEP_PIPS = 10_000;
    uint8 public constant ARM_COUNT = 46;
    uint16 public constant EXPLORATION_BPS = 1_500;
    int32 public constant MAX_ABS_RETURN_BPS = 10_000;

    enum Status { None, Selected, Closed, Censored }
    enum CensorReason { None, NeverOpened, NoTrade, StuckUnwound }

    struct Assignment {
        uint24 feePips;
        uint8 arm;
        Status status;
    }

    struct ArmStats {
        uint64 closed;
        uint64 censored;
        int128 netReturnSumBps;
    }

    address public immutable controller;
    uint64 public selections;
    uint64 public totalClosed;
    uint64 public totalCensored;

    mapping(address => Assignment) public assignments;
    ArmStats[46] public armStats;

    event FeeSelected(address indexed token, uint24 feePips, uint8 arm, bool exploration);
    event ClosedRecorded(address indexed token, uint24 feePips, int32 netReturnBps);
    event CensoredRecorded(address indexed token, uint24 feePips, CensorReason reason);

    error NotController();
    error InvalidController();
    error InvalidToken();
    error NotSelected();
    error InvalidReturn();
    error InvalidCensorReason();

    modifier onlyController() {
        if (msg.sender != controller) revert NotController();
        _;
    }

    constructor(address controller_) {
        if (controller_ == address(0)) revert InvalidController();
        controller = controller_;
    }

    /// @notice Reserve the fee for a launch token. Calling again, including on
    /// an open retry, returns the same fee and cannot resample or change it.
    /// The controller must use this fee in the PoolKey of the newly created
    /// X/Q pool, then initialize and fund its LP position atomically.
    function selectFee(address token) external onlyController returns (uint24 feePips) {
        if (token == address(0)) revert InvalidToken();
        Assignment storage assignment = assignments[token];
        if (assignment.status != Status.None) return assignment.feePips;

        uint64 nonce = ++selections;
        // EVM entropy is only pseudo-random. A block producer or a controller
        // choosing when to call can bias exploration; use a verifiable random
        // source if adversarially unbiased fee selection is required.
        uint256 draw = uint256(keccak256(abi.encode(
            block.prevrandao, blockhash(block.number - 1), address(this), token, nonce
        )));
        bool explore = totalClosed == 0 || draw % 10_000 < EXPLORATION_BPS;
        uint8 arm = explore ? uint8((draw >> 16) % ARM_COUNT) : _bestArm();
        feePips = MIN_FEE_PIPS + uint24(arm) * FEE_STEP_PIPS;

        assignment.feePips = feePips;
        assignment.arm = arm;
        assignment.status = Status.Selected;
        emit FeeSelected(token, feePips, arm, explore);
    }

    /// @notice Explicit dashboard/executor getter. Zero means not selected.
    function selectedFee(address token) external view returns (uint24) {
        return assignments[token].feePips;
    }

    /// @notice Feed back one completed position after the executor has
    /// removed liquidity, settled balances, and collected fees. The trusted
    /// controller computes net return in basis points of entry value, using
    /// the same valuation rule for every pool and including execution costs.
    /// An outcome above +/-100% must be clipped before reporting. A call made
    /// before a successful exit would corrupt learning and violates the
    /// controller's integration contract.
    function recordClosed(address token, int32 netReturnBps) external onlyController {
        Assignment storage assignment = assignments[token];
        if (assignment.status != Status.Selected) revert NotSelected();
        if (netReturnBps < -MAX_ABS_RETURN_BPS || netReturnBps > MAX_ABS_RETURN_BPS) {
            revert InvalidReturn();
        }

        assignment.status = Status.Closed;
        ArmStats storage stats = armStats[assignment.arm];
        ++stats.closed;
        stats.netReturnSumBps += int128(netReturnBps);
        ++totalClosed;
        emit ClosedRecorded(token, assignment.feePips, netReturnBps);
    }

    /// @notice Terminal observations without a comparable realized outcome
    /// are counted but excluded from the mean return. The controller must
    /// only call this after it confirms that no position remains open (or an
    /// entry was permanently skipped). An active, stuck position stays
    /// Selected; it must not be reported as a zero-return closed position.
    function recordCensored(address token, CensorReason reason) external onlyController {
        Assignment storage assignment = assignments[token];
        if (assignment.status != Status.Selected) revert NotSelected();
        if (reason == CensorReason.None) revert InvalidCensorReason();

        assignment.status = Status.Censored;
        ++armStats[assignment.arm].censored;
        ++totalCensored;
        emit CensoredRecorded(token, assignment.feePips, reason);
    }

    /// @dev Compare cross-products rather than dividing signed returns;
    /// bounded int128 sums and uint64 counts cannot overflow int256 here.
    function _bestArm() private view returns (uint8 best) {
        uint64 bestCount;
        int128 bestSum;
        for (uint8 arm; arm < ARM_COUNT; ++arm) {
            ArmStats storage stats = armStats[arm];
            uint64 count = stats.closed;
            if (count == 0) continue;
            int128 sum = stats.netReturnSumBps;
            if (
                bestCount == 0 ||
                int256(sum) * int256(uint256(bestCount)) >
                    int256(bestSum) * int256(uint256(count))
            ) {
                best = arm;
                bestCount = count;
                bestSum = sum;
            }
        }
    }
}
