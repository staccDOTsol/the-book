// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, IPonsV2LaunchFactoryCursor, ILaunchCursorConfigurator} from "./LaunchCursorToken.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";

interface IHooklessLPForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
}

contract ForkMockPonsFactory {
    function getLaunchedToken(address token)
        external pure returns (IPonsV2LaunchFactoryCursor.LaunchedToken memory launch)
    {
        launch.token = token;
        launch.curve = address(0xC0FFEE);
        launch.phase = 0;
        launch.exists = true;
    }
}

contract ForkMockToken {
    string public constant name = "Fork Test X";
    string public constant symbol = "X";
    uint8 public constant decimals = 18;
    mapping(address => uint256) public balanceOf;
}

/// @notice The fork checks real v4 position encoding; the custom Q/ETH launch
/// pool does not exist, so this stub only supplies the executor's guard ABI.
contract ForkMockPriceGuard {
    address public immutable ponsFactory;
    address public immutable stateView;
    address public immutable quoteToken;
    bytes32 public immutable quoteEthPoolId;

    constructor(address factory_, address stateView_, address quote_) {
        ponsFactory = factory_;
        stateView = stateView_;
        quoteToken = quote_;
        quoteEthPoolId = keccak256(abi.encode(address(0), quote_, uint24(2_500), int24(25), address(0)));
    }

    function validate(address, uint160 proposed) external pure returns (uint160) {
        return proposed;
    }
}

contract ForkMockDepthGuard {
    address public immutable source;
    address public immutable priceGuard;
    address public immutable settlementRouter;
    address public immutable quoteToken;
    address public immutable ponsFactory;
    bool public depthAvailable = true;

    constructor(address source_, address priceGuard_, address router_, address quote_, address factory_) {
        source = source_;
        priceGuard = priceGuard_;
        settlementRouter = router_;
        quoteToken = quote_;
        ponsFactory = factory_;
    }

    function setDepthAvailable(bool available) external { depthAvailable = available; }

    function matches(address source_, address priceGuard_, address router_) external view returns (bool) {
        return source_ == source && priceGuard_ == priceGuard && router_ == settlementRouter;
    }

    function validate(address, uint128, int24, int24)
        external view returns (uint256, uint256, uint256, uint256)
    {
        require(msg.sender == source && depthAvailable, "executable depth unavailable");
        return (1, 1, 1, 1);
    }
}

contract ForkMockExitSale {
    address public immutable source;
    address public immutable factory;
    constructor(address source_, address factory_) { source = source_; factory = factory_; }
}

contract ForkMockExitBuyer {
    address public immutable source;
    address public immutable poolManager;
    address public immutable quoteToken;
    bytes32 public immutable poolId;
    constructor(address source_, address manager_, address quote_, bytes32 poolId_) {
        source = source_;
        poolManager = manager_;
        quoteToken = quote_;
        poolId = poolId_;
    }
}

/// @dev A test-only router that checks executor allowances and receipt
/// ownership. Standalone ExitSettlementRouter fork tests cover real routing.
contract ForkMockSettlementRouter {
    address public immutable source;
    address public immutable quoteToken;
    address public immutable factory;
    address public immutable weth;
    address public immutable wizardFanout;
    address public immutable developer;
    address public immutable activeSale;
    address public immutable graduatedSale;
    address public immutable quoteBuy;

    uint256 public lastXAmount;
    uint256 public lastQAmount;
    uint256 public lastMinEthOut;
    uint256 public lastMinQOut;

    constructor(address source_, address quote_, address factory_, address manager_, bytes32 poolId_) {
        source = source_;
        quoteToken = quote_;
        factory = factory_;
        weth = address(this);
        wizardFanout = address(this);
        developer = address(this);
        activeSale = address(new ForkMockExitSale(address(this), factory_));
        graduatedSale = address(new ForkMockExitSale(address(this), factory_));
        quoteBuy = address(new ForkMockExitBuyer(address(this), manager_, quote_, poolId_));
    }

    function settle(
        address token, uint256 xAmount, uint256 qAmount,
        uint256 minEthOut, uint256 minQOut, uint64 deadline
    ) external returns (uint256 ethOut, uint256 qBurned) {
        require(msg.sender == source && deadline >= block.timestamp, "bad mock settlement caller");
        require(LaunchCursorToken(quoteToken).allowance(source, address(this)) == qAmount, "wrong Q approval");
        if (xAmount != 0) {
            require(minEthOut != 0 && minQOut != 0, "missing swap minima");
            require(ForkMockTransferToken(token).allowance(source, address(this)) == xAmount, "wrong X approval");
            require(ForkMockTransferToken(token).transferFrom(source, address(this), xAmount), "X pull failed");
            ethOut = minEthOut;
        } else {
            require(minEthOut == 0 && minQOut == 0 && qAmount != 0, "bad Q-only settlement");
        }
        if (qAmount != 0) {
            require(LaunchCursorToken(quoteToken).transferFrom(source, address(this), qAmount), "Q pull failed");
        }
        qBurned = qAmount + minQOut;
        LaunchCursorToken(quoteToken).burn(qBurned);
        lastXAmount = xAmount;
        lastQAmount = qAmount;
        lastMinEthOut = minEthOut;
        lastMinQOut = minQOut;
    }
}

interface ForkMockTransferToken {
    function allowance(address owner, address spender) external view returns (uint256);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @notice Runs against an RPC fork; no transaction is broadcast.
contract HooklessLPForkTest {
    IHooklessLPForkVm private constant vm =
        IHooklessLPForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;

    function _assertMintedQuoteOnly(
        HooklessLPExecutor executor, ForkMockToken x, LaunchCursorToken q,
        uint160 startingPrice, uint256 quoteBefore
    ) private view {
        (uint160 livePrice, bool inBand, bool quoteBoundary, bool tokenBoundary, bool entered) =
            executor.inspect(address(x));
        require(livePrice == startingPrice, "wrong initialized price");
        require(!inBand && quoteBoundary && !tokenBoundary && !entered, "not quote-only");
        require(q.balanceOf(address(executor)) < quoteBefore, "no quote was spent");
    }

    function _openAndAssert(
        ForkMockToken x, HooklessLPExecutor executor, LaunchCursorToken q
    ) private {
        uint256 quoteBefore = q.balanceOf(address(executor));
        q.enqueue(address(x));
        int24 tickLower = -100;
        int24 tickUpper = 100;
        uint160 startingPrice = address(q) < address(x)
            ? HooklessTickMath.getSqrtPriceAtTick(tickLower)
            : HooklessTickMath.getSqrtPriceAtTick(tickUpper);
        ILaunchCursorConfigurator.OpenConfig memory config = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: startingPrice,
            liquidity: 1 ether,
            maxQuoteIn: 10 ether,
            tickSpacing: 10,
            tickLower: tickLower,
            tickUpper: tickUpper,
            deadline: uint64(block.timestamp + 1_000)
        });
        q.configureOpen(address(x), config);
        (bool attempted, bool succeeded) = q.processNext();
        require(attempted && succeeded, "real v4 pool/position mint failed");
        _assertMintedQuoteOnly(executor, x, q, startingPrice, quoteBefore);
    }

    function _abortAndAssert(ForkMockToken x, HooklessLPExecutor executor, LaunchCursorToken q) private {
        uint256 ownerQuoteBefore = q.balanceOf(address(this));
        q.emergencyAbort(address(x), 0, 1, uint64(block.timestamp + 1_000));
        require(q.balanceOf(address(this)) > ownerQuoteBefore, "Q was not recovered");
        try executor.inspect(address(x)) returns (uint160, bool, bool, bool, bool) {
            revert("NFT still active after abort");
        } catch {}
    }

    function testInitializeAndMintOnRobinhoodFork() external {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "run with Robinhood fork");
        ForkMockPonsFactory factory = new ForkMockPonsFactory();
        ForkMockToken x = new ForkMockToken();
        HooklessLPExecutor executor = new HooklessLPExecutor(
            POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, PERMIT2
        );
        PositionInspector inspector = new PositionInspector(address(executor));
        address[] memory endpoints = new address[](0);
        LaunchCursorToken q = new LaunchCursorToken(
            "Fork Test Q", "Q", 1_000_000_000 ether,
            address(factory), address(executor), address(inspector),
            30, 2_000_000, 1 gwei, endpoints
        );
        executor.bindController(address(q));
        inspector.bindCursor(address(q));
        ForkMockPriceGuard guard = new ForkMockPriceGuard(address(factory), STATE_VIEW, address(q));
        executor.bindPriceGuard(address(guard));
        ForkMockSettlementRouter router = new ForkMockSettlementRouter(
            address(executor), address(q), address(factory), POOL_MANAGER, guard.quoteEthPoolId()
        );
        executor.bindSettlementRouter(address(router));
        ForkMockDepthGuard depth = new ForkMockDepthGuard(
            address(executor), address(guard), address(router), address(q), address(factory)
        );
        executor.bindDepthGuard(address(depth));
        require(q.transfer(address(executor), 100 ether));
        _openAndAssert(x, executor, q);

        // Exercise the opposite address ordering too: Q can be either
        // currency0 or currency1 depending on the newly launched X address.
        ForkMockToken opposite;
        for (uint256 i; i < 32; ++i) {
            ForkMockToken candidate = new ForkMockToken();
            if ((address(q) < address(candidate)) != (address(q) < address(x))) {
                opposite = candidate;
                break;
            }
        }
        require(address(opposite) != address(0), "could not test opposite orientation");
        _openAndAssert(opposite, executor, q);
        _abortAndAssert(x, executor, q);
        _abortAndAssert(opposite, executor, q);
        uint256 idleQuote = q.balanceOf(address(executor));
        require(idleQuote != 0, "no idle Q to rescue");
        uint256 ownerBefore = q.balanceOf(address(this));
        q.rescueHeldERC20(address(q), idleQuote);
        require(q.balanceOf(address(executor)) == 0, "idle Q remains in vault");
        require(q.balanceOf(address(this)) - ownerBefore == idleQuote, "idle Q not returned");
    }
}
