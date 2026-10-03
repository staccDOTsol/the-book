// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ExitSettlementRouter, IExitSettlementToken} from "./ExitSettlementRouter.sol";
import {HooklessQuoteBuyPoolKey, IHooklessQuoteBuyPoolManager} from "./HooklessQuoteBuyAdapter.sol";
import {IPonsActiveExitCurve, IPonsActiveExitFactory} from "./PonsActiveExitAdapter.sol";

interface IExitRouterForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 newBalance) external;
    function startPrank(address sender) external;
    function stopPrank() external;
    function expectRevert(bytes4 selector) external;
    function mockCall(address callee, bytes calldata data, bytes calldata returnData) external;
    function clearMockedCalls() external;
}

interface IExitRouterForkManager is IHooklessQuoteBuyPoolManager {
    struct ModifyLiquidityParams {
        int24 tickLower;
        int24 tickUpper;
        int256 liquidityDelta;
        bytes32 salt;
    }

    function initialize(HooklessQuoteBuyPoolKey memory key, uint160 sqrtPriceX96) external returns (int24 tick);
    function modifyLiquidity(
        HooklessQuoteBuyPoolKey memory key,
        ModifyLiquidityParams memory params,
        bytes calldata hookData
    ) external returns (int256 callerDelta, int256 feesAccrued);
    function sync(address currency) external;
}

interface IExitRouterForkCurve is IPonsActiveExitCurve {
    function buy(uint256 quoteIn, uint256 minTokensOut, address recipient) external payable returns (uint256 tokensOut);
}

contract ExitRouterForkQ {
    string public constant name = "Fork Synthetic Q";
    string public constant symbol = "Q";
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function burn(uint256 amount) external {
        balanceOf[msg.sender] -= amount;
        totalSupply -= amount;
    }
}

contract ExitRouterForkWETH {
    mapping(address => uint256) public balanceOf;

    function deposit() external payable {
        balanceOf[msg.sender] += msg.value;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract ExitRouterForkFanout {}

/// @dev Funds a new zero-hook Q/ETH pool in the real Robinhood PoolManager.
contract ExitRouterForkPoolSeeder {
    IExitRouterForkManager private immutable _manager;
    ExitRouterForkQ private immutable _q;

    constructor(address manager_, address quote_) {
        _manager = IExitRouterForkManager(manager_);
        _q = ExitRouterForkQ(quote_);
    }

    function key() private view returns (HooklessQuoteBuyPoolKey memory) {
        return HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: address(_q), fee: 2_500, tickSpacing: 25, hooks: address(0)
        });
    }

    function seed() external {
        _manager.initialize(key(), uint160(1 << 96));
        _manager.unlock("");
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        require(msg.sender == address(_manager), "bad seeder callback");
        (int256 packedDelta,) = _manager.modifyLiquidity(
            key(),
            IExitRouterForkManager.ModifyLiquidityParams({
                tickLower: -1_000, tickUpper: 1_000, liquidityDelta: int256(1_000 ether), salt: bytes32(0)
            }),
            ""
        );
        int128 ethDelta = int128(packedDelta >> 128);
        int128 qDelta = int128(packedDelta);
        require(ethDelta < 0 && qDelta < 0, "no two-sided liquidity");
        uint256 ethOwed = uint256(-int256(ethDelta));
        uint256 qOwed = uint256(-int256(qDelta));
        require(_manager.settle{value: ethOwed}() == ethOwed, "bad ETH settlement");
        _manager.sync(address(_q));
        require(_q.transfer(address(_manager), qOwed), "bad Q transfer");
        require(_manager.settle() == qOwed, "bad Q settlement");
        return "";
    }

    receive() external payable {}
}

/// @notice No transactions are broadcast. Only the synthetic Q pool, WETH,
/// and fanout are local; Pons sales use actual fork state and PoolManager.
contract ExitSettlementRouterForkTest {
    IExitRouterForkVm private constant vm = IExitRouterForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant PONS_FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant ACTIVE_X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant ACTIVE_CURVE = 0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD;
    address private constant GRADUATED_X = 0x6C7C3113bFa9EeF3E716A4912D9B0dc47AEFE796;
    address private constant GRADUATED_SOURCE = 0x2E05B44DC8682Aa5497eeB3f1C44dDa3f204F0CC;
    address payable private constant DEV = payable(address(0xD00D));

    struct SettlementSnapshot {
        uint256 sourceX;
        uint256 sourceQ;
        uint256 supply;
        uint256 fanoutWeth;
        uint256 developerNative;
        uint256 routerNative;
        uint256 routerQ;
        uint256 routerX;
    }

    struct SettlementResult {
        uint256 xAmount;
        uint256 qAmount;
        uint256 minEthOut;
        uint256 minQOut;
        uint256 ethOut;
        uint256 qBurned;
    }

    function _setup(address source)
        private
        returns (ExitSettlementRouter router, ExitRouterForkQ q, ExitRouterForkWETH weth, ExitRouterForkFanout fanout)
    {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");
        q = new ExitRouterForkQ();
        weth = new ExitRouterForkWETH();
        fanout = new ExitRouterForkFanout();
        ExitRouterForkPoolSeeder seeder = new ExitRouterForkPoolSeeder(POOL_MANAGER, address(q));
        q.mint(address(seeder), 1_000 ether);
        vm.deal(address(seeder), 100 ether);
        seeder.seed();
        router = new ExitSettlementRouter(source, address(q), address(weth), address(fanout), DEV, 2_500, 25);
        require(router.activeSale().source() == address(router), "active source cycle");
        require(router.graduatedSale().source() == address(router), "graduated source cycle");
        require(router.quoteBuy().source() == address(router), "quote source cycle");
    }

    function _assertSettlement(
        ExitSettlementRouter router,
        address x,
        uint256 xAmount,
        uint256 qAmount,
        uint256 minEthOut,
        uint256 minQOut
    ) private {
        SettlementSnapshot memory before_ = _snapshot(router, x);
        uint64 deadline = uint64(block.timestamp + 60);
        (uint256 ethOut, uint256 qBurned) = router.settle(x, xAmount, qAmount, minEthOut, minQOut, deadline);
        _assertAfter(
            router,
            x,
            before_,
            SettlementResult({
                xAmount: xAmount,
                qAmount: qAmount,
                minEthOut: minEthOut,
                minQOut: minQOut,
                ethOut: ethOut,
                qBurned: qBurned
            })
        );
    }

    function _snapshot(ExitSettlementRouter router, address x)
        private
        view
        returns (SettlementSnapshot memory before_)
    {
        ExitRouterForkQ q = ExitRouterForkQ(address(router.quoteToken()));
        ExitRouterForkWETH weth = ExitRouterForkWETH(address(router.weth()));
        before_ = SettlementSnapshot({
            sourceX: IExitSettlementToken(x).balanceOf(router.source()),
            sourceQ: q.balanceOf(router.source()),
            supply: q.totalSupply(),
            fanoutWeth: weth.balanceOf(router.wizardFanout()),
            developerNative: DEV.balance,
            routerNative: address(router).balance,
            routerQ: q.balanceOf(address(router)),
            routerX: IExitSettlementToken(x).balanceOf(address(router))
        });
    }

    function _assertAfter(
        ExitSettlementRouter router,
        address x,
        SettlementSnapshot memory before_,
        SettlementResult memory result
    ) private view {
        ExitRouterForkQ q = ExitRouterForkQ(address(router.quoteToken()));
        ExitRouterForkWETH weth = ExitRouterForkWETH(address(router.weth()));
        uint256 buyEth = result.ethOut / 2;
        uint256 wizardEth = (result.ethOut - buyEth) / 2;
        uint256 devEth = result.ethOut - buyEth - wizardEth;
        require(
            result.ethOut >= result.minEthOut && result.qBurned >= result.qAmount + result.minQOut, "minimums not met"
        );
        require(
            IExitSettlementToken(x).balanceOf(router.source()) == before_.sourceX - result.xAmount, "source X mismatch"
        );
        require(q.balanceOf(router.source()) == before_.sourceQ - result.qAmount, "source Q mismatch");
        require(q.totalSupply() == before_.supply - result.qBurned, "Q supply not burned");
        require(q.balanceOf(address(router)) == before_.routerQ, "router retained Q");
        require(IExitSettlementToken(x).balanceOf(address(router)) == before_.routerX, "router retained X");
        require(address(router).balance == before_.routerNative, "router retained ETH");
        require(weth.balanceOf(address(router)) == 0, "router retained WETH");
        require(weth.balanceOf(router.wizardFanout()) == before_.fanoutWeth + wizardEth, "wizard WETH mismatch");
        require(DEV.balance == before_.developerNative + devEth, "developer ETH mismatch");
    }

    function testActiveSaleQuoteBuyBurnAndPayout() external {
        (ExitSettlementRouter router,,,) = _setup(address(this));
        vm.deal(address(this), 0.01 ether);
        IExitRouterForkCurve curve = IExitRouterForkCurve(ACTIVE_CURVE);
        require(curve.token() == ACTIVE_X && !curve.graduated(), "wrong active curve");
        uint256 xAmount = curve.buy{value: 0.001 ether}(0.001 ether, 1, address(this));
        require(xAmount > 0, "failed to buy X");
        require(IExitSettlementToken(ACTIVE_X).approve(address(router), xAmount), "X approval failed");
        _assertSettlement(router, ACTIVE_X, xAmount, 0, 1, 1);
    }

    function testGraduatedSaleQuoteBuyBurnAndPayout() external {
        (ExitSettlementRouter router, ExitRouterForkQ q,,) = _setup(GRADUATED_SOURCE);
        uint256 xAmount = 1_000 ether;
        require(IExitSettlementToken(GRADUATED_X).balanceOf(GRADUATED_SOURCE) >= xAmount, "missing real X");
        q.mint(GRADUATED_SOURCE, 1 ether);
        vm.startPrank(GRADUATED_SOURCE);
        require(IExitSettlementToken(GRADUATED_X).approve(address(router), xAmount), "X approval failed");
        require(q.approve(address(router), 1 ether), "Q approval failed");
        _assertSettlement(router, GRADUATED_X, xAmount, 1 ether, 1, 1);
        vm.stopPrank();
    }

    function testAllQuoteExitAndSweptPhaseRejection() external {
        (ExitSettlementRouter router, ExitRouterForkQ q, ExitRouterForkWETH weth, ExitRouterForkFanout fanout) =
            _setup(address(this));
        q.mint(address(this), 2 ether);
        require(q.approve(address(router), 2 ether), "Q approval failed");
        uint64 deadline = uint64(block.timestamp + 60);

        vm.startPrank(address(0xBAD));
        vm.expectRevert(ExitSettlementRouter.Unauthorized.selector);
        router.settle(ACTIVE_X, 0, 1 ether, 0, 0, deadline);
        vm.stopPrank();

        vm.expectRevert(ExitSettlementRouter.InvalidAmount.selector);
        router.settle(ACTIVE_X, 0, 1 ether, 1, 0, deadline);
        vm.expectRevert(ExitSettlementRouter.InvalidAmount.selector);
        router.settle(ACTIVE_X, 0, 1 ether, 0, 1, deadline);
        vm.expectRevert(ExitSettlementRouter.Expired.selector);
        router.settle(ACTIVE_X, 0, 1 ether, 0, 0, uint64(block.timestamp - 1));

        IPonsActiveExitFactory.LaunchedToken memory launch =
            IPonsActiveExitFactory(PONS_FACTORY).getLaunchedToken(ACTIVE_X);
        launch.phase = 1;
        vm.mockCall(
            PONS_FACTORY,
            abi.encodeWithSelector(IPonsActiveExitFactory.getLaunchedToken.selector, ACTIVE_X),
            abi.encode(launch)
        );
        vm.expectRevert(ExitSettlementRouter.SweptPhase.selector);
        router.settle(ACTIVE_X, 1, 0, 1, 1, deadline);
        vm.clearMockedCalls();

        uint256 supplyBefore = q.totalSupply();
        uint256 developerBefore = DEV.balance;
        uint256 fanoutBefore = weth.balanceOf(address(fanout));
        (uint256 ethOut, uint256 qBurned) = router.settle(ACTIVE_X, 0, 2 ether, 0, 0, deadline);
        require(ethOut == 0 && qBurned == 2 ether, "wrong all-Q result");
        require(q.totalSupply() == supplyBefore - 2 ether, "all-Q burn mismatch");
        require(q.balanceOf(address(router)) == 0, "router retained Q");
        require(weth.balanceOf(address(fanout)) == fanoutBefore && DEV.balance == developerBefore, "all-Q paid out");
    }

    receive() external payable {}
}
