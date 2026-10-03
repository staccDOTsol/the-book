// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, IPonsV2LaunchFactoryCursor} from "./LaunchCursorToken.sol";
import {LaunchCursorTokenV2} from "./LaunchCursorTokenV2.sol";

interface VmV2 {
    struct Log { bytes32[] topics; bytes data; address emitter; }
    function prank(address sender) external;
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
}

contract V2MockPonsFactory {
    function getLaunchedToken(address token)
        external pure returns (IPonsV2LaunchFactoryCursor.LaunchedToken memory launch)
    {
        launch.token = token;
        launch.curve = address(0xC0FFEE);
        launch.exists = true;
    }
}

contract V2MockUsdG {}
contract V2MockNotifier {
    function reportBand(LaunchCursorTokenV2 q, address token) external {
        require(q.notifyBandEntered(token));
    }
}

contract V2MockV3Factory {
    address public q;
    address public usdg;
    address public pool;
    uint24 public fee;

    function register(address q_, address usdg_, address pool_, uint24 fee_) external {
        require(pool == address(0));
        q = q_;
        usdg = usdg_;
        pool = pool_;
        fee = fee_;
    }

    function getPool(address tokenA, address tokenB, uint24 fee_) external view returns (address) {
        if (fee_ != fee) return address(0);
        if ((tokenA == q && tokenB == usdg) || (tokenA == usdg && tokenB == q)) return pool;
        return address(0);
    }
}

contract V2MockV3Pool {
    address public immutable factory;
    address public immutable token0;
    address public immutable token1;
    uint24 public immutable fee;
    uint128 public liquidity;
    LaunchCursorTokenV2 public immutable q;

    constructor(address factory_, LaunchCursorTokenV2 q_, address usdg_, uint24 fee_) {
        factory = factory_;
        q = q_;
        (token0, token1) = address(q_) < usdg_ ? (address(q_), usdg_) : (usdg_, address(q_));
        fee = fee_;
    }

    function setLiquidity(uint128 liquidity_) external { liquidity = liquidity_; }

    function buyQ(address buyer, uint256 amount) external {
        require(q.transfer(buyer, amount));
    }
}

contract V2MockRouter {
    function sellQ(LaunchCursorTokenV2 q, address seller, address pool, uint256 amount) external {
        require(q.transferFrom(seller, pool, amount));
    }
}

contract V2MockExecutor {
    LaunchCursorTokenV2 public q;
    address public pool;
    V2MockNotifier public notifier;
    bool public failOpen;
    bool public reportBandDuringOpen;
    uint256 public openCalls;
    address public lastOpenedToken;
    mapping(address => bool) public configured;

    function bind(LaunchCursorTokenV2 q_, address pool_, V2MockNotifier notifier_) external {
        require(address(q) == address(0));
        q = q_;
        pool = pool_;
        notifier = notifier_;
    }

    function setFailOpen(bool fail_) external { failOpen = fail_; }
    function setReportBandDuringOpen(bool report_) external { reportBandDuringOpen = report_; }

    function configureOpen(address token, bytes calldata) external {
        require(msg.sender == address(q));
        configured[token] = true;
    }

    function open(address token, uint24, uint256[3] calldata mintedQuote)
        external returns (uint256[3] memory quoteSpent)
    {
        require(msg.sender == address(q) && configured[token] && !failOpen);
        ++openCalls;
        lastOpenedToken = token;
        if (reportBandDuringOpen) notifier.reportBand(q, token);
        for (uint8 i; i < 3; ++i) {
            quoteSpent[i] = 1 ether;
            // These inner transfers touch the bound v3 pool while the outer
            // queue action is running. They must not process another action.
            require(q.transfer(pool, quoteSpent[i]));
            q.burn(mintedQuote[i] - quoteSpent[i]);
        }
    }

    function activePositionCount(address) external pure returns (uint256) { return 1; }
    function exit(address) external pure returns (int32, bool) { revert("unused exit"); }
    function previewHarvest(address) external pure returns (uint256, uint256) { revert("unused harvest"); }
    function harvest(address) external pure { revert("unused harvest"); }
    function emergencyUnwind(address, uint128, uint128, uint64, address)
        external pure returns (uint256, uint256) { revert("unused unwind"); }
    function rescueHeldERC20(address, uint256, address) external pure { revert("unused rescue"); }
}

contract LaunchCursorTokenV2Test {
    VmV2 private constant vm = VmV2(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant X = address(0xA001);
    address private constant Y = address(0xA002);
    address private constant BUYER = address(0xB001);
    address private constant EOA = address(0xE0A1);
    uint24 private constant V3_FEE = 3_000;

    bytes32 private constant STEP_ATTEMPTED = keccak256("StepAttempted(address,uint8)");
    bytes32 private constant STEP_SUCCEEDED = keccak256("StepSucceeded(address,uint8)");

    V2MockPonsFactory private factory;
    V2MockExecutor private executor;
    V2MockV3Factory private v3Factory;
    V2MockUsdG private usdg;
    V2MockV3Pool private pool;
    V2MockRouter private router;
    LaunchCursorTokenV2 private q;

    function setUp() external {
        factory = new V2MockPonsFactory();
        executor = new V2MockExecutor();
        v3Factory = new V2MockV3Factory();
        usdg = new V2MockUsdG();
        router = new V2MockRouter();
        V2MockNotifier notifier = new V2MockNotifier();
        address[] memory endpoints = new address[](0);
        q = new LaunchCursorTokenV2(
            LaunchCursorToken.Metadata(
                unicode"🧙‍♂️", unicode"🧙‍♂️", unicode"🧙‍♂️",
                "https://raw.githubusercontent.com/staccDOTsol/the-book/450d558b169408de1483c0540faa1aae72889a57/assets/wizard-token.png"
            ),
            1_000_000_000 ether,
            address(factory), address(executor), address(notifier),
            30, 2_000_000, 1 gwei, endpoints,
            LaunchCursorTokenV2.V3PoolConfig({factory: address(v3Factory), usdg: address(usdg), fee: V3_FEE})
        );
        pool = new V2MockV3Pool(address(v3Factory), q, address(usdg), V3_FEE);
        v3Factory.register(address(q), address(usdg), address(pool), V3_FEE);
        executor.bind(q, address(pool), notifier);
        require(q.transfer(address(pool), 100 ether)); // LP inventory before binding.
        pool.setLiquidity(1);
    }

    function _queue(address token) private {
        q.enqueue(token);
        q.configureOpen(token, "plan");
    }

    function _bind() private {
        q.bindCanonicalV3Pool(address(pool));
    }

    function _count(VmV2.Log[] memory logs, bytes32 topic) private view returns (uint256 n) {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(q) && logs[i].topics.length != 0 && logs[i].topics[0] == topic) ++n;
        }
    }

    function testV2KeepsWizardMetadata() external view {
        string memory expected = string.concat(
            "data:application/json;base64,",
            "eyJuYW1lIjoi8J+nmeKAjeKZgu+4jyIsInN5bWJvbCI6IvCfp5nigI3imYLvuI8iLCJkZXNjcmlwdGlvbiI6IvCfp5nigI3imYLvuI8iLCJpbWFnZSI6Imh0dHBzOi8vcmF3LmdpdGh1YnVzZXJjb250ZW50LmNvbS9zdGFjY0RPVHNvbC90aGUtYm9vay80NTBkNTU4YjE2OTQwOGRlMTQ4M2MwNTQwZmFhMWFhZTcyODg5YTU3L2Fzc2V0cy93aXphcmQtdG9rZW4ucG5nIn0="
        );
        require(keccak256(bytes(q.tokenURI())) == keccak256(bytes(expected)), "metadata changed");
        require(q.totalSupply() == q.INITIAL_SUPPLY(), "initial supply changed");
    }

    function testPoolSetupDoesNotProcessUntilCanonicalPoolBound() external {
        _queue(X);
        require(q.transfer(address(pool), 1 ether));
        require(executor.openCalls() == 0 && q.pendingEntryCount() == 1, "setup dequeued");
        _bind();
        require(q.canonicalV3Pool() == address(pool), "pool not bound");
        (bool rebound,) = address(q).call(abi.encodeCall(q.bindCanonicalV3Pool, (address(pool))));
        require(!rebound, "pool binding changed");
    }

    function testBindingRejectsImpostorAndEmptyPool() external {
        V2MockV3Pool impostor = new V2MockV3Pool(address(v3Factory), q, address(usdg), V3_FEE);
        impostor.setLiquidity(1);
        (bool impostorBound,) = address(q).call(abi.encodeCall(q.bindCanonicalV3Pool, (address(impostor))));
        require(!impostorBound, "unregistered pool bound");
        pool.setLiquidity(0);
        (bool emptyBound,) = address(q).call(abi.encodeCall(q.bindCanonicalV3Pool, (address(pool))));
        require(!emptyBound, "empty pool bound");
        pool.setLiquidity(1);
        _bind();
    }

    function testPoolToBuyerRunsExactlyOneActionDespiteInnerPoolTransfers() external {
        _bind();
        _queue(X);
        _queue(Y);
        vm.recordLogs();
        pool.buyQ(BUYER, 1 ether);
        VmV2.Log[] memory logs = vm.getRecordedLogs();
        require(q.balanceOf(BUYER) == 1 ether, "buyer did not receive Q");
        require(executor.openCalls() == 1 && executor.lastOpenedToken() == Y, "wrong open count");
        require(q.successfulExecutorSteps() == 1 && q.pendingEntryCount() == 1, "double dequeue");
        (address next,,) = q.nextAction();
        require(next == X, "remaining action missing");
        require(_count(logs, STEP_ATTEMPTED) == 1, "nested transfer attempted a step");
        require(_count(logs, STEP_SUCCEEDED) == 1, "wrong successful action count");
    }

    function testRouterToPoolSellerRunsOneAction() external {
        _bind();
        _queue(X);
        require(q.approve(address(router), 1 ether));
        vm.recordLogs();
        router.sellQ(q, address(this), address(pool), 1 ether);
        VmV2.Log[] memory logs = vm.getRecordedLogs();
        require(q.successfulExecutorSteps() == 1 && executor.openCalls() == 1, "sell did not dequeue");
        require(_count(logs, STEP_ATTEMPTED) == 1 && _count(logs, STEP_SUCCEEDED) == 1,
            "seller route double-stepped or skipped");
    }

    function testReadyActionRequiresGasAndSuccessUnlessEmergencyPaused() external {
        _bind();
        _queue(X);
        uint256 poolBefore = q.balanceOf(address(pool));
        (bool lowGas,) = address(pool).call{gas: 500_000}(
            abi.encodeCall(pool.buyQ, (BUYER, 1 ether))
        );
        require(!lowGas && q.balanceOf(address(pool)) == poolBefore, "low-gas bypass settled");

        executor.setFailOpen(true);
        (bool failed,) = address(pool).call(abi.encodeCall(pool.buyQ, (BUYER, 1 ether)));
        require(!failed && q.balanceOf(address(pool)) == poolBefore, "failed open settled");
        require(q.successfulExecutorSteps() == 0 && q.pendingEntryCount() == 1, "failed open changed queue");

        q.setAutomatic(false);
        pool.buyQ(BUYER, 1 ether);
        require(q.balanceOf(BUYER) == 1 ether && q.successfulExecutorSteps() == 0,
            "emergency pause did not permit settlement");
    }

    function testHousekeepingSuccessDoesNotCountAsDequeue() external {
        _bind();
        _queue(X);
        q.skipStale(X); // Removal is lazy; scheduler returns (true, true).
        uint256 poolBefore = q.balanceOf(address(pool));
        (bool bypassed,) = address(pool).call(abi.encodeCall(pool.buyQ, (BUYER, 1 ether)));
        require(!bypassed && q.balanceOf(address(pool)) == poolBefore,
            "stale removal passed as executor action");
        require(q.successfulExecutorSteps() == 0 && q.pendingEntryCount() == 1,
            "reverted cleanup changed the queue");
        (bool attempted, bool succeeded) = q.processNext();
        require(attempted && succeeded && q.pendingEntryCount() == 0,
            "permissionless stale cleanup failed");
        pool.buyQ(BUYER, 1 ether);
        require(q.balanceOf(BUYER) == 1 ether, "empty queue blocked trade");
    }

    function testPendingNotifierReportCannotBypassStrictPoolStep() external {
        _bind();
        _queue(X);
        executor.setReportBandDuringOpen(true);
        pool.buyQ(BUYER, 1 ether);
        require(q.successfulExecutorSteps() == 1 && q.pendingReportCount() == 1,
            "open did not leave a pending report");

        uint256 poolBefore = q.balanceOf(address(pool));
        (bool bypassed,) = address(pool).call(abi.encodeCall(pool.buyQ, (BUYER, 1 ether)));
        require(!bypassed && q.balanceOf(address(pool)) == poolBefore,
            "report-only housekeeping passed as dequeue");
        (bool attempted, bool succeeded) = q.processNext();
        require(attempted && succeeded && q.pendingReportCount() == 0,
            "permissionless report cleanup failed");
        pool.buyQ(BUYER, 1 ether);
        require(q.balanceOf(BUYER) == 2 ether, "empty queue blocked trade");
    }

    function testOriginalDirectEOATriggerStillWorks() external {
        require(q.transfer(EOA, 2 ether));
        _queue(X);
        vm.prank(EOA);
        require(q.transfer(BUYER, 1 ether));
        require(q.successfulExecutorSteps() == 1 && executor.openCalls() == 1,
            "direct transfer policy changed");
    }
}
