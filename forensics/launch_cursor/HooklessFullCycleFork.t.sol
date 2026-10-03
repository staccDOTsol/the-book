// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {LaunchCursorToken, ILaunchCursorConfigurator, ILaunchCursorExitConfigurator} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {OpenPriceGuard, OpenPriceFullMath} from "./OpenPriceGuard.sol";
import {OpenExecutableDepthGuard} from "./OpenExecutableDepthGuard.sol";
import {ExitSettlementRouter} from "./ExitSettlementRouter.sol";
import {ExitRouterForkWETH, ExitRouterForkFanout} from "./ExitSettlementRouterFork.t.sol";
import {DepthForkPoolSeeder} from "./OpenExecutableDepthGuardFork.t.sol";
import {
    HooklessQuoteBuyPoolKey,
    HooklessQuoteBuySwapParams,
    IHooklessQuoteBuyPoolManager
} from "./HooklessQuoteBuyAdapter.sol";
import {IPonsActiveExitCurve} from "./PonsActiveExitAdapter.sol";
import {StaticNextPoolFee} from "./StaticNextPoolFee.sol";

interface IFullCycleVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 amount) external;
}

interface IFullCycleManager is IHooklessQuoteBuyPoolManager {
    function sync(address currency) external;
}

interface IFullCycleX {
    function balanceOf(address owner) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IFullCyclePonsBuy is IPonsActiveExitCurve {
    function buy(uint256 quoteIn, uint256 minTokensOut, address recipient) external payable returns (uint256 tokensOut);
}

/// @dev Sends actual Pons X through the hookless X/Q pool to move the real
/// PositionManager NFT across the band before the executor burns it.
contract FullCycleXSwap {
    IFullCycleManager public immutable manager;
    address public immutable owner;

    struct SwapCall {
        address x;
        address q;
        uint24 feePips;
        int24 spacing;
        uint256 xIn;
    }

    constructor(address manager_) {
        manager = IFullCycleManager(manager_);
        owner = msg.sender;
    }

    function sellX(address x, address q, uint24 feePips, int24 spacing, uint256 xIn)
        external
        returns (uint256 xSpent, uint256 qOut)
    {
        require(msg.sender == owner && xIn > 0 && xIn <= uint256(uint128(type(int128).max)), "bad X input");
        return abi.decode(manager.unlock(abi.encode(x, q, feePips, spacing, xIn)), (uint256, uint256));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(manager), "wrong manager");
        SwapCall memory c = abi.decode(data, (SwapCall));
        (uint256 xSpent, uint256 qOut) = _swap(c);
        _settle(c.x, c.q, xSpent, qOut);
        return abi.encode(xSpent, qOut);
    }

    function _swap(SwapCall memory c) private returns (uint256 xSpent, uint256 qOut) {
        bool xIs0 = c.x < c.q;
        HooklessQuoteBuyPoolKey memory key = HooklessQuoteBuyPoolKey({
            currency0: xIs0 ? c.x : c.q,
            currency1: xIs0 ? c.q : c.x,
            fee: c.feePips,
            tickSpacing: c.spacing,
            hooks: address(0)
        });
        int256 packed = manager.swap(
            key,
            HooklessQuoteBuySwapParams({
                zeroForOne: xIs0,
                amountSpecified: -int256(c.xIn),
                sqrtPriceLimitX96: xIs0
                    ? uint160(4_295_128_740)
                    : uint160(1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_341)
            }),
            ""
        );
        int128 delta0 = int128(packed >> 128);
        int128 delta1 = int128(packed);
        int128 xDelta = xIs0 ? delta0 : delta1;
        int128 qDelta = xIs0 ? delta1 : delta0;
        require(xDelta < 0 && qDelta > 0, "no X to Q swap");
        xSpent = uint256(uint128(-xDelta));
        qOut = uint256(uint128(qDelta));
    }

    function _settle(address x, address q, uint256 xSpent, uint256 qOut) private {
        uint256 beforeX = IFullCycleX(x).balanceOf(address(manager));
        manager.sync(x);
        require(IFullCycleX(x).transfer(address(manager), xSpent), "X transfer");
        require(IFullCycleX(x).balanceOf(address(manager)) - beforeX == xSpent, "X mismatch");
        require(manager.settle() == xSpent, "X settle");
        manager.take(q, address(this), qOut);
    }
}

/// @notice One read-only fork transaction sequence with real v4 mint and
/// burn, real Pons X crossing the LP band, and real integrated settlement.
contract HooklessFullCycleForkTest {
    IFullCycleVm private constant vm = IFullCycleVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant QUOTER = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    address private constant ACTIVE_X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant ACTIVE_CURVE = 0x9d4bcCd80332ba9CcCC75657B560Bb89265462aD;
    address payable private constant DEV = payable(address(0xD00D));

    event log_named_uint(string key, uint256 val);

    struct Setup {
        FullCycleXSwap swapper;
        HooklessLPExecutor executor;
        PositionInspector inspector;
        LaunchCursorToken q;
        OpenPriceGuard spot;
        ExitRouterForkWETH weth;
        ExitRouterForkFanout fanout;
    }

    function _floorTick(uint160 sqrtPrice) private pure returns (int24 tick) {
        int256 lo = -887272;
        int256 hi = 887272;
        while (lo < hi) {
            int256 mid = (lo + hi + 1) / 2;
            if (HooklessTickMath.getSqrtPriceAtTick(int24(mid)) <= sqrtPrice) lo = mid;
            else hi = mid - 1;
        }
        tick = int24(lo);
    }

    function _down(int24 value, int24 spacing) private pure returns (int24) {
        int24 rem = value % spacing;
        return rem < 0 ? value - rem - spacing : value - rem;
    }

    function _openConfig(OpenPriceGuard spot, address q)
        private
        view
        returns (ILaunchCursorConfigurator.OpenConfig memory c)
    {
        bool qIs0 = q < ACTIVE_X;
        int24 refTick = _floorTick(spot.referenceSqrtPriceX96(ACTIVE_X));
        int24 lower;
        int24 upper;
        if (qIs0) {
            int24 floor = _down(refTick + 5_000, 60);
            lower = floor == refTick + 5_000 ? floor : floor + 60;
            upper = lower + 1_200;
        } else {
            upper = _down(refTick - 5_000, 60);
            lower = upper - 1_200;
        }
        uint160 a = HooklessTickMath.getSqrtPriceAtTick(lower);
        uint160 b = HooklessTickMath.getSqrtPriceAtTick(upper);
        uint256 targetQ = 0.01 ether;
        uint256 liquidity = qIs0
            ? OpenPriceFullMath.mulDiv(OpenPriceFullMath.mulDiv(targetQ, b, 1 << 96), a, b - a)
            : OpenPriceFullMath.mulDiv(targetQ, 1 << 96, b - a);
        liquidity = liquidity * 9 / 10;
        require(liquidity > 0 && liquidity <= type(uint128).max, "bad liquidity");
        c = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: spot.referenceSqrtPriceX96(ACTIVE_X),
            liquidity: uint128(liquidity),
            maxQuoteIn: uint128(targetQ),
            tickSpacing: 60,
            tickLower: lower,
            tickUpper: upper,
            deadline: uint64(block.timestamp + 120)
        });
    }

    function _setup() private returns (Setup memory s) {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");
        s.swapper = new FullCycleXSwap(POOL_MANAGER);
        s.executor = new HooklessLPExecutor(POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, PERMIT2);
        s.inspector = new PositionInspector(address(s.executor));
        address[] memory endpoints = new address[](2);
        endpoints[0] = POOL_MANAGER;
        endpoints[1] = address(s.swapper);
        s.q = new LaunchCursorToken(
            "Full Cycle Q",
            "Q",
            1_000_000_000 ether,
            FACTORY,
            address(s.executor),
            address(s.inspector),
            30,
            3_000_000,
            1 gwei,
            endpoints
        );
        s.executor.bindController(address(s.q));
        s.inspector.bindCursor(address(s.q));
        DepthForkPoolSeeder seeder = new DepthForkPoolSeeder(POOL_MANAGER, address(s.q));
        require(s.q.transfer(address(seeder), 1_000 ether), "Q seed funding");
        vm.deal(address(seeder), 100 ether);
        seeder.seed();
        s.spot = new OpenPriceGuard(FACTORY, STATE_VIEW, address(s.q), 2_500, 25, 1_000);
        s.executor.bindPriceGuard(address(s.spot));
        s.weth = new ExitRouterForkWETH();
        s.fanout = new ExitRouterForkFanout();
        ExitSettlementRouter router = new ExitSettlementRouter(
            address(s.executor), address(s.q), address(s.weth), address(s.fanout), DEV, 2_500, 25
        );
        s.executor.bindSettlementRouter(address(router));
        OpenExecutableDepthGuard depth =
            new OpenExecutableDepthGuard(address(s.executor), address(s.spot), address(router), QUOTER, 1_500);
        s.executor.bindDepthGuard(address(depth));
        require(s.q.transfer(address(s.executor), 10 ether), "vault funding");
    }

    function _openAndCross(Setup memory s) private {
        s.q.enqueue(ACTIVE_X);
        s.q.configureOpen(ACTIVE_X, _openConfig(s.spot, address(s.q)));
        (bool attemptedOpen, bool opened) = s.q.processNext();
        require(attemptedOpen && opened, "real v4 mint failed");
        (,, bool qBoundary,,) = s.executor.inspect(ACTIVE_X);
        require(qBoundary, "position not Q-only");

        vm.deal(address(this), 1 ether);
        uint256 xBought = IFullCyclePonsBuy(ACTIVE_CURVE).buy{value: 0.1 ether}(0.1 ether, 1, address(s.swapper));
        require(xBought > 0, "Pons X buy failed");
        (uint24 feePips,,) = s.q.feePolicy().assignments(ACTIVE_X);
        (uint256 xSpent, uint256 qOut) = s.swapper.sellX(ACTIVE_X, address(s.q), feePips, 60, xBought);
        require(xSpent > 0 && qOut > 0, "X/Q swap failed");
        s.executor.markEntered(ACTIVE_X);
        (,, bool stillQBoundary, bool xBoundary, bool entered) = s.executor.inspect(ACTIVE_X);
        require(!stillQBoundary && xBoundary && entered, "position did not reach all-X boundary");
        (bool reportedEntry, bool exitReady) = s.inspector.poke(ACTIVE_X);
        require(reportedEntry && exitReady, "exit not queued");
    }

    function _exitAndAssert(Setup memory s) private {
        s.q
            .configureExit(
                ACTIVE_X,
                ILaunchCursorExitConfigurator.ExitConfig({
                    minTokenOut: 1, minQuoteOut: 0, minEthOut: 1, minQOut: 1, deadline: uint64(block.timestamp + 60)
                })
            );
        uint256 supplyBefore = s.q.totalSupply();
        uint256 fanoutBefore = s.weth.balanceOf(address(s.fanout));
        uint256 devBefore = DEV.balance;
        uint256 gasBefore = gasleft();
        (bool attemptedExit, bool exited) = s.q.processNext();
        uint256 exitGas = gasBefore - gasleft();
        emit log_named_uint("full real-v4/router exit processNext gas", exitGas);
        require(attemptedExit && exited, "integrated real LP exit failed");
        require(exitGas < 3_000_000, "cursor exit gas cap exceeded");
        require(s.q.totalSupply() < supplyBefore, "Q not burned");
        require(s.weth.balanceOf(address(s.fanout)) > fanoutBefore, "wizard fanout unpaid");
        require(DEV.balance > devBefore, "developer unpaid");
        (,, StaticNextPoolFee.Status status) = s.q.feePolicy().assignments(ACTIVE_X);
        require(status == StaticNextPoolFee.Status.Selected && s.q.outcomeDeadline(ACTIVE_X) != 0,
            "unvalued exit not pending");
        bool active;
        try s.executor.inspect(ACTIVE_X) returns (uint160, bool, bool, bool, bool) {
            active = true;
        } catch {}
        require(!active, "NFT still active");
    }

    function testRealV4MintBurnAndRouterActiveSaleUnderCursorGasCap() external {
        Setup memory s = _setup();
        _openAndCross(s);
        _exitAndAssert(s);
    }

    receive() external payable {}
}
