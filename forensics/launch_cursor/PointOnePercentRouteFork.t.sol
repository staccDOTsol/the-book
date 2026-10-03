// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessLPExecutor, HooklessTickMath, IHooklessPositionManager} from "./HooklessLPExecutor.sol";
import {LaunchCursorToken, ILaunchCursorConfigurator, ILaunchCursorExitConfigurator} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {OpenPriceFullMath} from "./OpenPriceGuard.sol";
import {ExitSettlementRouter} from "./ExitSettlementRouter.sol";
import {ExitRouterForkWETH, ExitRouterForkFanout} from "./ExitSettlementRouterFork.t.sol";
import {HooklessQuoteBuyAdapter, HooklessQuoteBuyPoolKey, HooklessQuoteBuySwapParams, IHooklessQuoteBuyPoolManager}
    from "./HooklessQuoteBuyAdapter.sol";
import {PonsBootstrap, IBootstrapInstantStrategy, IBootstrapStateView} from "./PonsBootstrap.s.sol";
import {IPonsActiveExitCurve} from "./PonsActiveExitAdapter.sol";

interface IRouteForkVm {
    function createSelectFork(string calldata rpcUrl, uint256 blockNumber) external returns (uint256);
    function envOr(string calldata key, string calldata defaultValue) external returns (string memory);
    function setEnv(string calldata name, string calldata value) external;
    function toString(address value) external returns (string memory);
    function toString(uint256 value) external returns (string memory);
    function deal(address account, uint256 amount) external;
    function roll(uint256 blockNumber) external;
    function warp(uint256 timestamp) external;
    function prevrandao(bytes32 value) external;
    function snapshotState() external returns (uint256);
    function revertToState(uint256 snapshotId) external returns (bool);
    function prank(address sender) external;
}

interface IRouteForkManager is IHooklessQuoteBuyPoolManager {
    function sync(address currency) external;
}

interface IRouteForkToken {
    function totalSupply() external view returns (uint256);
    function balanceOf(address owner) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IRouteForkCurve is IPonsActiveExitCurve {
    function buy(uint256 quoteIn, uint256 minTokensOut, address recipient) external payable returns (uint256 tokensOut);
}

/// @dev This is the counterfactual trader, not strategy code. Both swaps use
/// the real forked Robinhood PoolManager and require complete exact-input fills.
contract RouteForkTrader {
    IRouteForkManager public immutable manager;
    address public immutable owner;

    constructor(address manager_) {
        manager = IRouteForkManager(manager_);
        owner = msg.sender;
    }

    function sellX(address x, address q, uint24 fee, int24 spacing, uint256 amount)
        external returns (uint256 qOut)
    {
        require(msg.sender == owner && amount > 0 && amount <= uint256(uint128(type(int128).max)), "X input");
        qOut = abi.decode(manager.unlock(abi.encode(uint8(0), x, q, fee, spacing, amount)), (uint256));
    }

    function sellQ(address q, uint256 amount) external returns (uint256 ethOut) {
        require(msg.sender == owner && amount > 0 && amount <= uint256(uint128(type(int128).max)), "Q input");
        ethOut = abi.decode(manager.unlock(abi.encode(uint8(1), address(0), q, uint24(2_500), int24(25), amount)),
            (uint256));
    }

    function unlockCallback(bytes calldata payload) external returns (bytes memory) {
        require(msg.sender == address(manager), "manager");
        (uint8 side, address x, address q, uint24 fee, int24 spacing, uint256 amount) =
            abi.decode(payload, (uint8, address, address, uint24, int24, uint256));
        if (side == 0) return abi.encode(_sellX(x, q, fee, spacing, amount));
        require(side == 1 && x == address(0), "side");
        return abi.encode(_sellQ(q, amount));
    }

    function _sellX(address x, address q, uint24 fee, int24 spacing, uint256 amount)
        private returns (uint256 qOut)
    {
        bool xIs0 = x < q;
        HooklessQuoteBuyPoolKey memory key = HooklessQuoteBuyPoolKey({
            currency0: xIs0 ? x : q, currency1: xIs0 ? q : x,
            fee: fee, tickSpacing: spacing, hooks: address(0)
        });
        int256 packed = manager.swap(key, HooklessQuoteBuySwapParams({
            zeroForOne: xIs0, amountSpecified: -int256(amount),
            sqrtPriceLimitX96: xIs0 ? uint160(4_295_128_740)
                : uint160(1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_341)
        }), "");
        int128 d0 = int128(packed >> 128);
        int128 d1 = int128(packed);
        int128 xDelta = xIs0 ? d0 : d1;
        int128 qDelta = xIs0 ? d1 : d0;
        require(xDelta == -int256(amount) && qDelta > 0, "X partial fill");
        qOut = uint256(uint128(qDelta));
        manager.sync(x);
        require(IRouteForkToken(x).transfer(address(manager), amount), "X transfer");
        require(manager.settle() == amount, "X settle");
        manager.take(q, address(this), qOut);
    }

    function _sellQ(address q, uint256 amount) private returns (uint256 ethOut) {
        HooklessQuoteBuyPoolKey memory key = HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: q, fee: 2_500, tickSpacing: 25, hooks: address(0)
        });
        int256 packed = manager.swap(key, HooklessQuoteBuySwapParams({
            zeroForOne: false, amountSpecified: -int256(amount),
            sqrtPriceLimitX96: uint160(1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_341)
        }), "");
        int128 ethDelta = int128(packed >> 128);
        int128 qDelta = int128(packed);
        require(qDelta == -int256(amount) && ethDelta > 0, "Q partial fill");
        ethOut = uint256(uint128(ethDelta));
        manager.sync(q);
        require(IRouteForkToken(q).transfer(address(manager), amount), "Q transfer");
        require(manager.settle() == amount, "Q settle");
        manager.take(address(0), address(this), ethOut);
    }

    receive() external payable {}
}

/// @notice Read-only counterfactual. It creates Q and both pools in a local
/// fork, uses a real Pons X buy and real v4 swaps, then compares developer +
/// fanout cash with minted-Q dilution and a no-trade winddown. It does not
/// replay organic flow or claim a strategy return.
contract PointOnePercentRouteForkTest {
    IRouteForkVm private constant vm = IRouteForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSM = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant LAUNCHER = 0x0000FffFBE8efE702c8703aE3477FF5dE3d319C0;
    address private constant FEES_ON = 0x7c48DDe3B447381F4d986334679b3Afc7F2D35C2;
    address private constant X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant CURVE = 0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD;
    uint256 private constant FORK_BLOCK = 79_257_966;
    address private constant FIRST_Q_BUYER = address(0xBEEF);
    address payable private constant DEV = payable(address(0xD00D));

    event log_named_uint(string key, uint256 val);
    event log_named_int(string key, int256 val);

    struct Setup {
        HooklessLPExecutor executor;
        PositionInspector inspector;
        LaunchCursorToken q;
        RouteForkTrader trader;
        ExitRouterForkWETH weth;
        ExitRouterForkFanout fanout;
        uint24 fee;
        uint256 mintedQ;
        uint256 openGas;
    }

    function _fork() private {
        vm.createSelectFork(vm.envOr("PONS_HTTP_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"), FORK_BLOCK);
        // Foundry's in-test fork selection can retain the runner's block
        // header while pinning storage. Align header fields explicitly.
        vm.roll(FORK_BLOCK);
        vm.warp(1_791_048_165);
        require(block.chainid == 4663, "not Robinhood");
        emit log_named_uint("fork_block", block.number);
        emit log_named_uint("fork_basefee_wei_per_gas", block.basefee);
        require(block.number == FORK_BLOCK, "wrong fork block");
        require(IRouteForkToken(X).totalSupply() == 1_000_000_000 ether, "X supply changed");
    }

    function _launchQ() private returns (Setup memory s) {
        _fork();
        s.executor = new HooklessLPExecutor(MANAGER, POSM, VIEW, PERMIT2);
        s.inspector = new PositionInspector(address(s.executor));
        s.trader = new RouteForkTrader(MANAGER);
        s.weth = new ExitRouterForkWETH();
        s.fanout = new ExitRouterForkFanout();
        address[] memory endpoints = new address[](4);
        endpoints[0] = PERMIT2;
        endpoints[1] = LAUNCHER;
        endpoints[2] = FEES_ON;
        endpoints[3] = POSM;
        s.q = new LaunchCursorToken(
            LaunchCursorToken.Metadata(unicode"🧙‍♂️", unicode"🧙‍♂️", "Fork counterfactual", "ipfs://test"),
            1_000_000_000 ether, FACTORY, address(s.executor), address(s.inspector),
            30, 9_000_000, 1 gwei, endpoints
        );
        s.executor.bindController(address(s.q));
        s.inspector.bindCursor(address(s.q));
        s.q.setAutomatic(false);
        PonsBootstrap bootstrap = new PonsBootstrap();
        vm.setEnv("PONS_DEPLOYER", vm.toString(address(this)));
        vm.setEnv("PONS_INSTANT_STRATEGY", vm.toString(FEES_ON));
        vm.setEnv("PONS_EXECUTOR_ADDRESS", vm.toString(address(s.executor)));
        vm.setEnv("PONS_INSPECTOR_ADDRESS", vm.toString(address(s.inspector)));
        vm.setEnv("PONS_Q_ADDRESS", vm.toString(address(s.q)));
        vm.setEnv("PONS_FEE_BENEFICIARY", vm.toString(address(this)));
        vm.setEnv("PONS_PERMIT2_EXPIRATION", vm.toString(block.timestamp + 1 hours));
        (,, bytes memory qApprove, bytes memory permitApprove, bytes memory launchCall) = bootstrap.preflightLaunch();
        (bool ok,) = address(s.q).call(qApprove);
        require(ok, "Q approve");
        (ok,) = PERMIT2.call(permitApprove);
        require(ok, "Permit2 approve");
        uint256 lockedNft = IHooklessPositionManager(POSM).nextTokenId();
        (ok,) = LAUNCHER.call(launchCall);
        require(ok, "launcher");
        require(IHooklessPositionManager(POSM).ownerOf(lockedNft) ==
            IBootstrapInstantStrategy(FEES_ON).feeSplitter(), "Q/ETH NFT owner");
        bytes32 qPoolId = keccak256(abi.encode(address(0), address(s.q), uint24(2_500), int24(25), address(0)));
        (uint160 sqrtPrice,,,) = IBootstrapStateView(VIEW).getSlot0(qPoolId);
        require(sqrtPrice != 0, "Q/ETH uninitialized");
        HooklessQuoteBuyAdapter buyer = new HooklessQuoteBuyAdapter(MANAGER, address(s.q), 2_500, 25, address(this));
        vm.deal(address(this), 1 ether);
        uint256 firstQ = buyer.buyQ{value: 0.01 ether}(1, FIRST_Q_BUYER, uint64(block.timestamp + 60));
        require(firstQ > 3_000_000 ether, "first Q buy too small for valuation");
        require(IBootstrapStateView(VIEW).getLiquidity(qPoolId) > 0, "Q/ETH inactive");
        ExitSettlementRouter router = new ExitSettlementRouter(
            address(s.executor), address(s.q), address(s.weth), address(s.fanout), DEV, 2_500, 25
        );
        s.executor.bindSettlementRouter(address(router));
    }

    function _band(bool qIs0, uint8 i) private pure returns (int24 lower, int24 upper) {
        // Exact planner ticks for 1B X, 1B Q, p0=0.01 Q/X and R=12.25.
        // Python pons_price_keeper.py rounds each edge to spacing 60.
        if (qIs0) {
            lower = i == 0 ? int24(20_940) : i == 1 ? int24(14_040) : int24(-2_040);
            upper = 46_080;
        } else {
            lower = -46_080;
            upper = i == 0 ? int24(-20_940) : i == 1 ? int24(-14_040) : int24(2_040);
        }
    }

    function _open(Setup memory s, uint8 targetArm) private returns (Setup memory) {
        // The fee arm is normally pseudorandom for an untrained policy. Pin
        // prevrandao in the local fork so 5% and 50% can be compared fairly.
        bool found;
        for (uint256 seed; seed < 3_000; ++seed) {
            uint256 draw = uint256(keccak256(abi.encode(
                bytes32(seed), blockhash(block.number - 1), address(s.q.feePolicy()), X, uint64(1)
            )));
            if (uint8((draw >> 16) % 46) == targetArm) {
                vm.prevrandao(bytes32(seed));
                found = true;
                break;
            }
        }
        require(found, "fee seed");
        bool qIs0 = address(s.q) < X;
        int24[3] memory lowers;
        int24[3] memory uppers;
        uint128[3] memory liquidities;
        uint128[3] memory caps;
        uint256 simulatedSupply = s.q.totalSupply();
        for (uint8 i; i < 3; ++i) {
            uint256 mint = simulatedSupply / 1_000;
            simulatedSupply += mint;
            (lowers[i], uppers[i]) = _band(qIs0, i);
            uint160 a = HooklessTickMath.getSqrtPriceAtTick(lowers[i]);
            uint160 b = HooklessTickMath.getSqrtPriceAtTick(uppers[i]);
            uint256 budget = mint * 95 / 100;
            uint256 liquidity = qIs0
                ? OpenPriceFullMath.mulDiv(OpenPriceFullMath.mulDiv(budget, b, 1 << 96), a, b - a)
                : OpenPriceFullMath.mulDiv(budget, 1 << 96, b - a);
            liquidities[i] = uint128(liquidity);
            caps[i] = uint128(mint);
        }
        int24 initTick = qIs0 ? lowers[2] - 60 : uppers[2] + 60;
        ILaunchCursorConfigurator.OpenConfig memory plan = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: HooklessTickMath.getSqrtPriceAtTick(initTick),
            liquidity: liquidities, maxQuoteIn: caps, tickSpacing: 60,
            tickLower: lowers, tickUpper: uppers, deadline: uint64(block.timestamp + 120)
        });
        uint256 supplyBefore = s.q.totalSupply();
        uint256 gasBefore = gasleft();
        s.q.enqueue(X);
        s.q.configureOpen(X, abi.encode(plan));
        (bool attempted, bool opened) = s.q.processNext();
        s.openGas = gasBefore - gasleft();
        emit log_named_uint("open_calls_gas_units_ex_base", s.openGas);
        require(attempted && opened, "open failed");
        s.mintedQ = s.q.totalSupply() - supplyBefore;
        (s.fee,,) = s.q.feePolicy().assignments(X);
        require(s.fee == uint24(50_000 + uint24(targetArm) * 10_000), "wrong fee arm");
        emit log_named_uint("xq_fee_pips", s.fee);
        emit log_named_uint("net_new_Q_at_open", s.mintedQ);
        return s;
    }

    function _valueQ(Setup memory s, uint256 amount) private returns (uint256 ethOut) {
        uint256 snap = vm.snapshotState();
        vm.prank(FIRST_Q_BUYER);
        require(s.q.transfer(address(s.trader), amount), "value transfer");
        ethOut = s.trader.sellQ(address(s.q), amount);
        require(vm.revertToState(snap), "value rollback");
    }

    function _windDown(Setup memory s) private returns (uint256 exitGas, uint256 burnedQ) {
        vm.warp(block.timestamp + 120 minutes);
        uint256 windDownGasBefore = gasleft();
        require(s.q.requestWindDown(X), "winddown not queued");
        exitGas += windDownGasBefore - gasleft();
        uint256 supplyBefore = s.q.totalSupply();
        for (uint8 i; i < 3; ++i) {
            uint64 deadline = uint64(block.timestamp + 60);
            uint256 snapshot = vm.snapshotState();
            vm.prank(address(s.q));
            (uint256 xOut, uint256 qOut) = s.executor.simulateTimedWithdraw(X, i, 0, 0, deadline);
            require(vm.revertToState(snapshot), "preview rollback");
            // Preview selects the same X/Q principal branch as the exit keeper.
            // Minima of 1 wei deliberately isolate route liveness from slippage.
            uint256 gasBefore = gasleft();
            s.q.configureExit(X, abi.encode(ILaunchCursorExitConfigurator.ExitConfig({
                tranche: i, minTokenOut: xOut > 0 ? 1 : 0, minQuoteOut: qOut > 0 ? 1 : 0,
                minEthOut: xOut > 0 ? 1 : 0, minQOut: xOut > 0 ? 1 : 0,
                deadline: deadline, timed: true
            })));
            (bool attempted, bool exited) = s.q.processNext();
            exitGas += gasBefore - gasleft();
            require(attempted && exited, "timed exit failed");
        }
        burnedQ = supplyBefore - s.q.totalSupply();
        require(s.executor.activePositionCount(X) == 0, "NFT still active");
    }

    function testNoTradeBaseline50Percent() external {
        Setup memory s = _open(_launchQ(), 45);
        uint256 entryEth = _valueQ(s, s.mintedQ);
        uint256 gasUsed;
        uint256 burnedQ;
        (gasUsed, burnedQ) = _windDown(s);
        emit log_named_uint("no_trade_burned_Q_wei", burnedQ);
        emit log_named_int("no_trade_Q_dust_wei", int256(s.mintedQ) - int256(burnedQ));
        uint256 burnEth = _valueQ(s, burnedQ);
        emit log_named_uint("no_trade_minted_Q_entry_liquidation_ETH_wei", entryEth);
        emit log_named_uint("no_trade_burned_Q_exit_liquidation_ETH_wei", burnEth);
        emit log_named_uint("no_trade_exit_gas_units", gasUsed);
        emit log_named_int("no_trade_issuer_mark_ETH_wei_before_gas", int256(burnEth) - int256(entryEth));
        emit log_named_uint("no_trade_strategy_gas_cost_at_fork_basefee_wei",
            (s.openGas + gasUsed + 210_000) * block.basefee);
    }

    function testCounterfactualPonsBuyXqSellQethSellAndTimedExit() external {
        Setup memory s = _open(_launchQ(), 0);
        uint256 entryEth = _valueQ(s, s.mintedQ);
        uint256 ponsEth = 0.0003 ether;
        uint256 gasBefore = gasleft();
        uint256 xBought = IRouteForkCurve(CURVE).buy{value: ponsEth}(ponsEth, 1, address(s.trader));
        uint256 buyGas = gasBefore - gasleft();
        gasBefore = gasleft();
        uint256 qOut = s.trader.sellX(X, address(s.q), s.fee, 60, xBought);
        uint256 xqGas = gasBefore - gasleft();
        gasBefore = gasleft();
        uint256 qSaleEth = s.trader.sellQ(address(s.q), qOut);
        uint256 qethGas = gasBefore - gasleft();
        emit log_named_uint("pons_buy_ETH_wei", ponsEth);
        emit log_named_uint("pons_buy_X_wei", xBought);
        emit log_named_uint("xq_output_Q_wei", qOut);
        emit log_named_uint("qeth_sale_ETH_wei", qSaleEth);
        emit log_named_int("arb_route_ETH_wei_before_gas", int256(qSaleEth) - int256(ponsEth));
        emit log_named_uint("arb_trade_gas_units", buyGas + xqGas + qethGas);
        uint256 devBefore = DEV.balance;
        uint256 fanoutBefore = s.weth.balanceOf(address(s.fanout));
        (uint256 exitGas, uint256 burnedQ) = _windDown(s);
        uint256 devCash = DEV.balance - devBefore;
        uint256 fanoutCash = s.weth.balanceOf(address(s.fanout)) - fanoutBefore;
        emit log_named_uint("issuer_developer_cash_ETH_wei", devCash);
        emit log_named_uint("issuer_wizard_fanout_WETH_wei", fanoutCash);
        emit log_named_uint("issuer_exit_gas_units", exitGas);
        emit log_named_uint("issuer_burned_Q_wei", burnedQ);
        uint256 burnEth = _valueQ(s, burnedQ);
        emit log_named_uint("issuer_new_Q_entry_liquidation_ETH_wei", entryEth);
        emit log_named_uint("issuer_burned_Q_exit_liquidation_ETH_wei", burnEth);
        emit log_named_int("issuer_partial_mark_ETH_wei_before_gas",
            int256(devCash + fanoutCash + burnEth) - int256(entryEth));
        uint256 strategyGas = s.openGas + exitGas + 210_000;
        emit log_named_uint("issuer_strategy_gas_units_with_base", strategyGas);
        emit log_named_int("issuer_total_recipient_cash_after_fork_basefee_wei",
            int256(devCash + fanoutCash) - int256(strategyGas * block.basefee));
        emit log_named_int("issuer_developer_cash_after_full_strategy_gas_at_fork_basefee_wei",
            int256(devCash) - int256(strategyGas * block.basefee));
    }

    function simulateArbRoute(address quote, RouteForkTrader trader, uint24 fee, uint256 ethIn)
        external returns (uint256 xBought, uint256 qOut, uint256 ethOut, uint256 gasUnits)
    {
        require(msg.sender == address(this), "self only");
        uint256 beforeGas = gasleft();
        xBought = IRouteForkCurve(CURVE).buy{value: ethIn}(ethIn, 1, address(trader));
        qOut = trader.sellX(X, quote, fee, 60, xBought);
        ethOut = trader.sellQ(quote, qOut);
        gasUnits = beforeGas - gasleft();
    }

    function _sweep(uint8 feeArm) private {
        Setup memory s = _open(_launchQ(), feeArm);
        uint256[5] memory ethSizes = [
            uint256(0.00001 ether), uint256(0.00003 ether), uint256(0.0001 ether),
            uint256(0.0003 ether), uint256(0.001 ether)
        ];
        for (uint256 i; i < ethSizes.length; ++i) {
            uint256 snapshot = vm.snapshotState();
            emit log_named_uint("route_input_ETH_wei", ethSizes[i]);
            try this.simulateArbRoute(address(s.q), s.trader, s.fee, ethSizes[i])
                returns (uint256 xBought, uint256 qOut, uint256 ethOut, uint256 gasUnits)
            {
                emit log_named_uint("route_bought_X_wei", xBought);
                emit log_named_uint("route_extracted_Q_wei", qOut);
                emit log_named_uint("route_sale_ETH_wei", ethOut);
                emit log_named_int("route_net_ETH_wei_before_gas", int256(ethOut) - int256(ethSizes[i]));
                emit log_named_uint("route_gas_units", gasUnits);
            } catch {
                emit log_named_uint("route_exact_fill_failed", 1);
            }
            require(vm.revertToState(snapshot), "sweep rollback");
        }
    }

    function testFivePercentSameBlockSizeSweep() external {
        _sweep(0);
    }

    function testFiftyPercentSameBlockSizeSweep() external {
        _sweep(45);
    }

    receive() external payable {}
}
