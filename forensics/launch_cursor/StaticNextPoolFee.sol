// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Selects a static Uniswap v4 fee for each new X/Q pool. No hook is
/// involved: the chosen fee is placed in PoolKey.fee when the pool is created.
/// The trusted controller must select before the first open attempt and pass
/// the returned fee to every open retry for that launch token.
/// @dev Fees are v4 pips (1,000,000 = 100%). The 46 arms are 5%, 6%, ...,
/// 50%. A fixed 15% exploration probability samples a random arm; otherwise
/// the arm with the highest conservative mean selection score is used. Selection has
/// a fixed upper bound of 46 arm reads and never scans launches or positions.
/// Every assigned token participates in the conservative selection score:
/// a still-open or censored token carries a fixed negative provisional score
/// until a comparable exit replaces it. Launch tokens differ in flow,
/// liquidity, and volatility, so this is exploratory observational feedback,
/// not a causal or realized-profit estimate.
contract StaticNextPoolFee {
    uint24 public constant MIN_FEE_PIPS = 50_000;
    uint24 public constant MAX_FEE_PIPS = 500_000;
    uint24 public constant FEE_STEP_PIPS = 10_000;
    uint8 public constant ARM_COUNT = 46;
    uint16 public constant EXPLORATION_BPS = 1_500;
    int32 public constant MAX_ABS_RETURN_BPS = 10_000;
    int32 public constant UNRESOLVED_PENALTY_BPS = -5_000;

    enum Status { None, Selected, Closed, Censored }
    enum CensorReason { None, NeverOpened, NoTrade, StuckUnwound, UnvaluedExit }

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
    uint64[46] public armAssignments;

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
        ++armAssignments[arm];
        emit FeeSelected(token, feePips, arm, explore);
    }

    /// @notice Explicit dashboard/executor getter. Zero means not selected.
    function selectedFee(address token) external view returns (uint24) {
        return assignments[token].feePips;
    }

    /// @notice Exploratory score used for arm selection. The penalty is a
    /// provisional heuristic for both still-open and censored assignments.
    function selectionScore(uint8 arm)
        external
        view
        returns (uint64 assigned, int256 scoreSumBps, int256 meanBps)
    {
        assigned = armAssignments[arm];
        scoreSumBps = _selectionSum(arm);
        meanBps = assigned == 0 ? int256(0) : scoreSumBps / int256(uint256(assigned));
    }

    /// @notice Feed back one completed three-tranche pool after the executor has
    /// removed all liquidity, settled balances, and collected fees. The trusted
    /// controller/reporter supplies a bounded gross ETH-equivalent mark score
    /// from actual recipient ETH/WETH and full-size executable Q/ETH quotes
    /// for Q deposited at open and Q burned during the position's lifetime.
    /// This estimate excludes gas until complete per-position attribution is
    /// available and is not realized profit. An outcome above +/-100% must be
    /// clipped before reporting. A call before a successful exit corrupts
    /// learning.
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

    /// @notice Terminal observations without a comparable gross mark estimate
    /// retain the provisional selection penalty. This includes a fully
    /// completed exit whose Q burn cannot yet be valued in ETH. The controller
    /// must only call this after it confirms that no position remains open (or
    /// an entry was permanently skipped). An active, stuck position stays
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

    /// @dev Compare cross-products rather than dividing signed returns.
    /// Every selected token remains in the denominator. An unresolved or
    /// censored token contributes a -50% provisional score, preventing a
    /// high-fee arm with only a few lucky closed exits from hiding its idle
    /// and stuck assignments. This penalty is a selection heuristic, not an
    /// observed loss, and is replaced if a selected token closes.
    function _bestArm() private view returns (uint8 best) {
        uint64 bestCount;
        int256 bestSum;
        for (uint8 arm; arm < ARM_COUNT; ++arm) {
            uint64 count = armAssignments[arm];
            if (count == 0) continue;
            int256 sum = _selectionSum(arm);
            if (
                bestCount == 0 ||
                sum * int256(uint256(bestCount)) >
                    bestSum * int256(uint256(count))
            ) {
                best = arm;
                bestCount = count;
                bestSum = sum;
            }
        }
    }

    function _selectionSum(uint8 arm) private view returns (int256) {
        ArmStats storage stats = armStats[arm];
        return int256(stats.netReturnSumBps) +
            int256(UNRESOLVED_PENALTY_BPS) *
            int256(uint256(armAssignments[arm] - stats.closed));
    }
}
