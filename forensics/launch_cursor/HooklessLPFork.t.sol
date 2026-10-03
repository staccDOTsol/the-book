// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, IPonsV2LaunchFactoryCursor, ILaunchCursorConfigurator} from "./LaunchCursorToken.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";

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

    constructor(address factory_, address stateView_, address quote_) {
        ponsFactory = factory_;
        stateView = stateView_;
        quoteToken = quote_;
    }

    function validate(address, uint160 proposed) external pure returns (uint160) {
        return proposed;
    }
}

/// @notice Runs against an RPC fork; no transaction is broadcast.
contract HooklessLPForkTest {
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
