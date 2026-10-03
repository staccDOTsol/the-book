// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, ILaunchCursorConfigurator, ILaunchCursorExitConfigurator} from "./LaunchCursorToken.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";
import {
    ForkMockPonsFactory,
    ForkMockToken,
    ForkMockPriceGuard,
    ForkMockDepthGuard,
    ForkMockSettlementRouter
} from "./HooklessLPFork.t.sol";

interface IQuoteExitForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
}

contract ForkSeededQuoteExitExecutor is HooklessLPExecutor {
    constructor(address manager, address positionManager, address view_, address permit2_)
        HooklessLPExecutor(manager, positionManager, view_, permit2_)
    {}

    function markEnteredForTest(address token) external {
        require(positions[token].active, "position inactive");
        positions[token].enteredBand = true;
    }
}

/// @notice Real v4 NFT settlement on a read-only Robinhood fork. The stored
/// range-entry flag is set through a test-only subclass so this
/// isolates the quote-only exit accounting from swap and fee behavior.
contract HooklessLPQuoteExitForkTest {
    IQuoteExitForkVm private constant vm =
        IQuoteExitForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;

    function _open(
        ForkMockToken x, ForkSeededQuoteExitExecutor executor, LaunchCursorToken q
    ) private {
        q.enqueue(address(x));
        int24 tickLower = -100;
        int24 tickUpper = 100;
        uint160 start = address(q) < address(x)
            ? HooklessTickMath.getSqrtPriceAtTick(tickLower)
            : HooklessTickMath.getSqrtPriceAtTick(tickUpper);
        q.configureOpen(address(x), ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: start,
            liquidity: 1 ether,
            maxQuoteIn: 10 ether,
            tickSpacing: 10,
            tickLower: tickLower,
            tickUpper: tickUpper,
            deadline: uint64(block.timestamp + 1_000)
        }));
        (bool attempted, bool succeeded) = q.processNext();
        require(attempted && succeeded, "open failed");
        (, , bool atQuoteBoundary, bool atTokenBoundary, bool entered) = executor.inspect(address(x));
        require(atQuoteBoundary && !atTokenBoundary && !entered, "wrong opening side");
    }

    function _markEnteredForSettlementTest(ForkSeededQuoteExitExecutor executor, PositionInspector inspector, address x)
        private
    {
        // Bypass price traversal solely to isolate the actual PositionManager
        // burn and Q balance accounting in the production executor code.
        executor.markEnteredForTest(x);
        (, , bool atQuoteBoundary, , bool entered) = executor.inspect(x);
        require(atQuoteBoundary && entered, "test entry flag failed");
        (bool reportedEntered, bool exitReady) = inspector.poke(x);
        require(reportedEntered && exitReady, "exit not queued");
    }

    function _exitAndAssert(
        ForkMockToken x, ForkSeededQuoteExitExecutor executor, PositionInspector inspector, LaunchCursorToken q
    ) private {
        _markEnteredForSettlementTest(executor, inspector, address(x));
        q.configureExit(address(x), ILaunchCursorExitConfigurator.ExitConfig({
            minTokenOut: 0, minQuoteOut: 1, minEthOut: 0, minQOut: 0,
            deadline: uint64(block.timestamp + 60)
        }));
        uint256 idleQuoteBefore = q.balanceOf(address(executor));
        uint256 supplyBefore = q.totalSupply();
        (bool attempted, bool succeeded) = q.processNext();
        require(attempted && succeeded, "quote-only exit failed");
        require(q.balanceOf(address(executor)) == idleQuoteBefore, "idle vault Q burned");
        require(q.totalSupply() < supplyBefore, "no recovered Q burned");
        require(x.balanceOf(address(executor)) == 0, "X left in vault");
        (,, StaticNextPoolFee.Status status) = q.feePolicy().assignments(address(x));
        require(status == StaticNextPoolFee.Status.Selected && q.outcomeDeadline(address(x)) != 0,
            "unvalued exit not pending");
        bool stillActive;
        try executor.inspect(address(x)) returns (uint160, bool, bool, bool, bool) {
            stillActive = true;
        } catch {}
        require(!stillActive, "NFT still active");
    }

    function testQuoteOnlyExitBurnsOnlyPositionQOnRobinhoodFork() external {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");
        ForkMockPonsFactory factory = new ForkMockPonsFactory();
        ForkSeededQuoteExitExecutor executor = new ForkSeededQuoteExitExecutor(
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
        require(q.transfer(address(executor), 100 ether), "Q funding failed");

        ForkMockToken x = new ForkMockToken();
        _open(x, executor, q);
        _exitAndAssert(x, executor, inspector, q);

        // Exercise the opposite token-address ordering with the same shared
        // vault Q balance; this catches a currency0/currency1 receipt mixup.
        ForkMockToken opposite;
        for (uint256 i; i < 32; ++i) {
            ForkMockToken candidate = new ForkMockToken();
            if ((address(q) < address(candidate)) != (address(q) < address(x))) {
                opposite = candidate;
                break;
            }
        }
        require(address(opposite) != address(0), "opposite ordering unavailable");
        _open(opposite, executor, q);
        _exitAndAssert(opposite, executor, inspector, q);
        require(q.feePolicy().totalClosed() == 0 && q.feePolicy().totalCensored() == 0,
            "wrong feedback totals");
    }
}
