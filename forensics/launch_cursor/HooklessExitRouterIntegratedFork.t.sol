// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {LaunchCursorToken} from "./LaunchCursorToken.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {PonsActiveExitAdapter} from "./PonsActiveExitAdapter.sol";
import {ExitSettlementRouter} from "./ExitSettlementRouter.sol";
import {ExitRouterForkPoolSeeder, ExitRouterForkWETH, ExitRouterForkFanout} from "./ExitSettlementRouterFork.t.sol";
import {
    SeedableQuoteExitExecutor,
    QuoteExitMockPositionManager,
    QuoteExitMockStateView,
    QuoteExitMockPermit2,
    QuoteExitMockToken
} from "./HooklessLPQuoteExitUnit.t.sol";

interface IIntegratedExitForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 amount) external;
    function prank(address sender) external;
}

interface IIntegratedActiveCurve {
    function buy(uint256 quoteIn, uint256 minTokensOut, address recipient)
        external payable returns (uint256 tokensOut);
}

/// @notice Read-only fork smoke of executor -> router -> active Pons sale ->
/// real v4 Q buy -> Q burn and payout. The LP removal is a test double so the
/// sale path can be exercised with a real Pons X and synthetic Q/ETH pool.
contract HooklessExitRouterIntegratedForkTest {
    IIntegratedExitForkVm private constant vm =
        IIntegratedExitForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant ACTIVE_X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant ACTIVE_CURVE = 0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD;
    address private constant GRADUATED_X = 0x6C7C3113bFa9EeF3E716A4912D9B0dc47AEFE796;
    address private constant GRADUATED_SOURCE = 0x2E05B44DC8682Aa5497eeB3f1C44dDa3f204F0CC;
    address payable private constant DEV = payable(address(0xD00D));

    event log_named_uint(string key, uint256 val);

    struct Setup {
        SeedableQuoteExitExecutor executor;
        QuoteExitMockPositionManager positionManager;
        QuoteExitMockStateView stateView;
        LaunchCursorToken quote;
        ExitSettlementRouter router;
        ExitRouterForkWETH weth;
        ExitRouterForkFanout fanout;
    }

    struct Before {
        uint256 idleQuote;
        uint256 supply;
        uint256 fanoutWeth;
        uint256 developerEth;
    }

    function _setup() private returns (Setup memory s) {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");
        s.stateView = new QuoteExitMockStateView(POOL_MANAGER);
        s.positionManager = new QuoteExitMockPositionManager(POOL_MANAGER);
        QuoteExitMockPermit2 permit2 = new QuoteExitMockPermit2();
        s.executor = new SeedableQuoteExitExecutor(
            POOL_MANAGER, address(s.positionManager), address(s.stateView), address(permit2)
        );
        PositionInspector inspector = new PositionInspector(address(s.executor));
        address[] memory endpoints = new address[](0);
        s.quote = new LaunchCursorToken(
            LaunchCursorToken.Metadata("Fork Integrated Q", "Q", "Fork integrated quote", "ipfs://fork-integrated-q"),
            1_000_000_000 ether,
            FACTORY, address(s.executor), address(inspector), 30, 3_000_000, 1 gwei, endpoints
        );
        s.executor.bindController(address(s.quote));
        inspector.bindCursor(address(s.quote));

        s.weth = new ExitRouterForkWETH();
        s.fanout = new ExitRouterForkFanout();
        ExitRouterForkPoolSeeder seeder = new ExitRouterForkPoolSeeder(POOL_MANAGER, address(s.quote));
        require(s.quote.transfer(address(seeder), 1_000 ether), "pool Q funding failed");
        vm.deal(address(seeder), 100 ether);
        seeder.seed();
        s.router = new ExitSettlementRouter(
            address(s.executor), address(s.quote), address(s.weth), address(s.fanout), DEV, 2_500, 25
        );
        s.executor.bindSettlementRouter(address(s.router));
        require(s.quote.transfer(address(s.executor), 5 ether), "idle vault Q funding failed");
    }

    function _prepareActiveExit(Setup memory s) private {
        // Test-only PositionManager dispenses real Pons X when the executor
        // burns its seeded position; the router handles every subsequent leg.
        s.positionManager.setAssets(s.quote, QuoteExitMockToken(ACTIVE_X));
        vm.deal(address(this), 1 ether);
        uint256 xAmount = IIntegratedActiveCurve(ACTIVE_CURVE).buy{value: 0.001 ether}(
            0.001 ether, 1, address(s.positionManager)
        );
        require(xAmount != 0, "active X buy failed");
        s.positionManager.setReceipts(0, xAmount);
        s.executor.seedPosition(ACTIVE_X);
        s.stateView.setPrice(address(s.quote) < ACTIVE_X
            ? HooklessTickMath.getSqrtPriceAtTick(100)
            : HooklessTickMath.getSqrtPriceAtTick(-100));
        (, uint256 quotedEth) = PonsActiveExitAdapter(payable(address(s.router.activeSale())))
            .previewNativeActiveSale(ACTIVE_X, xAmount);
        require(quotedEth > 1, "no executable active quote");
        vm.prank(address(s.quote));
        s.executor.configureExit(ACTIVE_X, abi.encode(HooklessLPExecutor.ExitConfig({
            tranche: 0, minTokenOut: 1, minQuoteOut: 0,
            minEthOut: quotedEth * 95 / 100, minQOut: 1,
            deadline: uint64(block.timestamp + 60), timed: false
        })));
    }

    function _prepareGraduatedExit(Setup memory s) private {
        uint256 xAmount = 1_000 ether;
        QuoteExitMockToken x = QuoteExitMockToken(GRADUATED_X);
        require(x.balanceOf(GRADUATED_SOURCE) >= xAmount, "missing graduated X");
        s.positionManager.setAssets(s.quote, x);
        vm.prank(GRADUATED_SOURCE);
        require(x.transfer(address(s.positionManager), xAmount), "graduated X funding failed");
        s.positionManager.setReceipts(0, xAmount);
        s.executor.seedPosition(GRADUATED_X);
        s.stateView.setPrice(address(s.quote) < GRADUATED_X
            ? HooklessTickMath.getSqrtPriceAtTick(100)
            : HooklessTickMath.getSqrtPriceAtTick(-100));
        vm.prank(address(s.quote));
        s.executor.configureExit(GRADUATED_X, abi.encode(HooklessLPExecutor.ExitConfig({
            tranche: 0, minTokenOut: 1, minQuoteOut: 0, minEthOut: 1, minQOut: 1,
            deadline: uint64(block.timestamp + 60), timed: false
        })));
    }

    function _assertExit(Setup memory s, address token, string memory gasLabel) private {
        Before memory before_ = Before({
            idleQuote: s.quote.balanceOf(address(s.executor)),
            supply: s.quote.totalSupply(),
            fanoutWeth: s.weth.balanceOf(address(s.fanout)),
            developerEth: DEV.balance
        });
        uint256 gasBefore = gasleft();
        vm.prank(address(s.quote));
        (int32 result, bool comparable) = s.executor.exit(token);
        uint256 gasUsed = gasBefore - gasleft();
        emit log_named_uint(gasLabel, gasUsed);
        require(gasUsed < 3_000_000, "exceeds cursor executor gas cap");
        require(result == 0 && !comparable, "invented ROI");
        require(s.quote.balanceOf(address(s.executor)) == before_.idleQuote, "idle Q changed");
        require(s.quote.totalSupply() < before_.supply, "Q not burned");
        require(s.weth.balanceOf(address(s.fanout)) > before_.fanoutWeth, "wizard not paid");
        require(DEV.balance > before_.developerEth, "developer not paid");
        require(QuoteExitMockToken(token).balanceOf(address(s.executor)) == 0, "X retained by executor");
    }

    function testExecutorRoutesActiveExitUnderCursorGasCap() external {
        Setup memory s = _setup();
        _prepareActiveExit(s);
        _assertExit(s, ACTIVE_X, "executor active exit gas");
    }

    function testExecutorRoutesGraduatedExitUnderCursorGasCap() external {
        Setup memory s = _setup();
        _prepareGraduatedExit(s);
        _assertExit(s, GRADUATED_X, "executor graduated exit gas");
    }

    receive() external payable {}
}
