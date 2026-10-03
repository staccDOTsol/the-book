// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken, CursorERC20} from "./LaunchCursorToken.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {ForkMockPriceGuard, ForkMockSettlementRouter} from "./HooklessLPFork.t.sol";

interface IQuoteExitUnitVm {
    function prank(address sender) external;
    function expectRevert(bytes4 selector) external;
}

contract QuoteExitMockToken is CursorERC20 {
    constructor() CursorERC20("X", "X") {}
    function mint(address to, uint256 amount) external { _update(address(0), to, amount); }
}

contract QuoteExitMockManager {}
contract QuoteExitMockPermit2 {}
contract QuoteExitMockFactory {}

contract QuoteExitMockStateView {
    address public immutable poolManager;
    uint160 public price;

    constructor(address manager) { poolManager = manager; }
    function setPrice(uint160 price_) external { price = price_; }
    function getSlot0(bytes32) external view returns (uint160, int24, uint24, uint24) {
        return (price, 0, 0, 50_000);
    }
}

contract QuoteExitMockPositionManager {
    address public immutable poolManager;
    LaunchCursorToken public quote;
    QuoteExitMockToken public token;
    uint256 public quoteOut;
    uint256 public tokenOut;

    constructor(address manager) { poolManager = manager; }
    function setAssets(LaunchCursorToken quote_, QuoteExitMockToken token_) external {
        quote = quote_;
        token = token_;
    }
    function setReceipts(uint256 quoteOut_, uint256 tokenOut_) external {
        quoteOut = quoteOut_;
        tokenOut = tokenOut_;
    }
    function modifyLiquidities(bytes calldata, uint256) external {
        if (quoteOut != 0) require(quote.transfer(msg.sender, quoteOut), "Q receipt failed");
        if (tokenOut != 0) require(token.transfer(msg.sender, tokenOut), "X receipt failed");
    }
}

contract SeedableQuoteExitExecutor is HooklessLPExecutor {
    constructor(address manager, address positionManager, address view_, address permit2_)
        HooklessLPExecutor(manager, positionManager, view_, permit2_)
    {}

    function seedPosition(address token) external {
        Position storage position = positions[token];
        position.tokenId = 1;
        position.poolId = keccak256(abi.encode(token, quoteToken));
        position.feePips = 50_000;
        position.tickSpacing = 10;
        position.tickLower = -100;
        position.tickUpper = 100;
        position.active = true;
        position.enteredBand = true;
        position.quoteSpent = 1 ether;
    }
}

contract HooklessLPQuoteExitUnitTest {
    IQuoteExitUnitVm private constant vm =
        IQuoteExitUnitVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function _setup()
        private
        returns (
            SeedableQuoteExitExecutor executor,
            QuoteExitMockPositionManager positionManager,
            QuoteExitMockToken x,
            LaunchCursorToken q
        )
    {
        QuoteExitMockManager manager = new QuoteExitMockManager();
        QuoteExitMockStateView stateView = new QuoteExitMockStateView(address(manager));
        positionManager = new QuoteExitMockPositionManager(address(manager));
        QuoteExitMockPermit2 permit2 = new QuoteExitMockPermit2();
        QuoteExitMockFactory factory = new QuoteExitMockFactory();
        executor = new SeedableQuoteExitExecutor(
            address(manager), address(positionManager), address(stateView), address(permit2)
        );
        PositionInspector inspector = new PositionInspector(address(executor));
        address[] memory endpoints = new address[](0);
        q = new LaunchCursorToken(
            "Q", "Q", 1_000_000_000 ether, address(factory), address(executor),
            address(inspector), 30, 2_000_000, 1 gwei, endpoints
        );
        executor.bindController(address(q));
        inspector.bindCursor(address(q));
        ForkMockPriceGuard guard = new ForkMockPriceGuard(address(factory), address(stateView), address(q));
        executor.bindPriceGuard(address(guard));
        ForkMockSettlementRouter router = new ForkMockSettlementRouter(
            address(executor), address(q), address(factory), address(manager), guard.quoteEthPoolId()
        );
        executor.bindSettlementRouter(address(router));
        x = new QuoteExitMockToken();
        positionManager.setAssets(q, x);
        stateView.setPrice(address(q) < address(x)
            ? HooklessTickMath.getSqrtPriceAtTick(-100)
            : HooklessTickMath.getSqrtPriceAtTick(100));
        executor.seedPosition(address(x));
        require(q.transfer(address(executor), 5 ether), "idle Q funding failed");
        require(q.transfer(address(positionManager), 1 ether), "position Q funding failed");
        vm.prank(address(q));
        executor.configureExit(address(x), HooklessLPExecutor.ExitConfig({
            minTokenOut: 0, minQuoteOut: 1, minEthOut: 0, minQOut: 0,
            deadline: uint64(block.timestamp + 60)
        }));
    }

    function testQuoteExitBurnsReceiptAndPreservesIdleQ() external {
        (SeedableQuoteExitExecutor executor, QuoteExitMockPositionManager pm, QuoteExitMockToken x, LaunchCursorToken q) =
            _setup();
        pm.setReceipts(1 ether, 0);
        uint256 idleBefore = q.balanceOf(address(executor));
        uint256 supplyBefore = q.totalSupply();
        vm.prank(address(q));
        (int32 netReturnBps, bool comparable) = executor.exit(address(x));
        require(netReturnBps == 0 && !comparable, "fabricated return");
        require(q.balanceOf(address(executor)) == idleBefore, "idle Q changed");
        require(q.totalSupply() == supplyBefore - 1 ether, "wrong burn amount");
        require(pm.quoteOut() == 1 ether, "mock changed unexpectedly");
    }

    function testQuoteExitRejectsXWithoutSwapMinimaAndRollsBackBurn() external {
        (SeedableQuoteExitExecutor executor, QuoteExitMockPositionManager pm, QuoteExitMockToken x, LaunchCursorToken q) =
            _setup();
        pm.setReceipts(1 ether, 1);
        x.mint(address(pm), 1);
        uint256 supplyBefore = q.totalSupply();
        vm.prank(address(q));
        vm.expectRevert(HooklessLPExecutor.InvalidConfiguration.selector);
        executor.exit(address(x));
        require(q.totalSupply() == supplyBefore, "Q burn did not roll back");
        require(q.balanceOf(address(executor)) == 5 ether, "idle Q changed");
        require(q.balanceOf(address(pm)) == 1 ether && x.balanceOf(address(pm)) == 1,
            "position receipts did not roll back");
        (, , bool atQuoteBoundary, bool atTokenBoundary, bool entered) = executor.inspect(address(x));
        require(atQuoteBoundary && !atTokenBoundary && entered, "position state changed");
    }

    function testQuoteExitIncludesPriorHarvestAndRoutesXFees() external {
        (SeedableQuoteExitExecutor executor, QuoteExitMockPositionManager pm, QuoteExitMockToken x, LaunchCursorToken q) =
            _setup();
        ForkMockSettlementRouter router = ForkMockSettlementRouter(executor.settlementRouter());
        require(q.transfer(address(pm), 3 ether), "harvest Q funding failed");
        require(q.transfer(address(router), 1 ether), "mock quote buy funding failed");
        x.mint(address(pm), 3);
        pm.setReceipts(3 ether, 2);
        vm.prank(address(q));
        executor.harvest(address(x));
        (uint256 harvestedX, uint256 harvestedQ) = executor.harvestedAmounts(address(x));
        require(harvestedX == 2 && harvestedQ == 3 ether, "fees not attributed");
        require(executor.reservedHarvestedQuote() == 3 ether, "Q reservation missing");

        pm.setReceipts(1 ether, 1);
        vm.prank(address(q));
        executor.configureExit(address(x), HooklessLPExecutor.ExitConfig({
            minTokenOut: 0, minQuoteOut: 1, minEthOut: 1, minQOut: 1 ether,
            deadline: uint64(block.timestamp + 60)
        }));
        uint256 supplyBefore = q.totalSupply();
        vm.prank(address(q));
        (int32 netReturnBps, bool comparable) = executor.exit(address(x));
        require(netReturnBps == 0 && !comparable, "invented ROI");
        require(router.lastXAmount() == 3 && router.lastQAmount() == 4 ether, "wrong routed receipts");
        require(q.totalSupply() == supplyBefore - 5 ether, "wrong total burn");
        require(q.balanceOf(address(executor)) == 5 ether, "idle Q changed");
        require(executor.reservedHarvestedQuote() == 0, "harvest reservation not cleared");
        (harvestedX, harvestedQ) = executor.harvestedAmounts(address(x));
        require(harvestedX == 0 && harvestedQ == 0, "harvest attribution not cleared");
        require(q.allowance(address(executor), address(router)) == 0, "Q allowance left open");
        require(x.allowance(address(executor), address(router)) == 0, "X allowance left open");
    }

    function testHarvestedQCannotBeRescuedAsIdleFunding() external {
        (SeedableQuoteExitExecutor executor, QuoteExitMockPositionManager pm, QuoteExitMockToken x, LaunchCursorToken q) =
            _setup();
        require(q.transfer(address(pm), 3 ether), "fee funding failed");
        pm.setReceipts(3 ether, 0);
        vm.prank(address(q));
        executor.harvest(address(x));
        require(q.balanceOf(address(executor)) == 8 ether, "wrong vault balance");
        vm.prank(address(q));
        vm.expectRevert(HooklessLPExecutor.UnsettledHarvest.selector);
        executor.rescueHeldERC20(address(q), 6 ether, address(this));
        require(q.balanceOf(address(executor)) == 8 ether, "reserved Q was rescued");
    }
}
