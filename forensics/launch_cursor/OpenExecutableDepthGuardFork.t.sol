// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {OpenExecutableDepthGuard} from "./OpenExecutableDepthGuard.sol";
import {OpenPriceGuard, OpenPriceFullMath} from "./OpenPriceGuard.sol";
import {HooklessLPExecutor, HooklessTickMath} from "./HooklessLPExecutor.sol";
import {LaunchCursorToken, ILaunchCursorConfigurator} from "./LaunchCursorToken.sol";
import {PositionInspector} from "./PositionInspector.sol";
import {ExitSettlementRouter} from "./ExitSettlementRouter.sol";
import {ExitRouterForkWETH, ExitRouterForkFanout, IExitRouterForkManager} from "./ExitSettlementRouterFork.t.sol";
import {HooklessQuoteBuyPoolKey} from "./HooklessQuoteBuyAdapter.sol";

interface IDepthForkVm {
    function createSelectFork(string calldata rpcUrl) external returns (uint256);
    function deal(address account, uint256 amount) external;
    function prank(address sender) external;
}

/// @dev Owns synthetic Q/ETH liquidity so the test can remove it without
/// changing slot0. The Quoter then observes genuinely thinner live depth.
contract DepthForkPoolSeeder {
    IExitRouterForkManager public immutable manager;
    LaunchCursorToken public immutable q;
    uint128 private constant START_LIQUIDITY = 1_000 ether;
    int256 private liquidityDelta;

    constructor(address manager_, address q_) {
        manager = IExitRouterForkManager(manager_);
        q = LaunchCursorToken(q_);
    }

    function key() public view returns (HooklessQuoteBuyPoolKey memory) {
        return HooklessQuoteBuyPoolKey({
            currency0: address(0), currency1: address(q),
            fee: 2_500, tickSpacing: 25, hooks: address(0)
        });
    }

    function seed() external {
        manager.initialize(key(), uint160(1 << 96));
        liquidityDelta = int256(uint256(START_LIQUIDITY));
        manager.unlock("");
    }

    function shrink() external {
        liquidityDelta = -int256(uint256(START_LIQUIDITY - uint128(1e12)));
        manager.unlock("");
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        require(msg.sender == address(manager), "wrong manager");
        int256 delta = liquidityDelta;
        (int256 packed,) = manager.modifyLiquidity(
            key(),
            IExitRouterForkManager.ModifyLiquidityParams({
                tickLower: -1_000, tickUpper: 1_000,
                liquidityDelta: delta, salt: bytes32(0)
            }), ""
        );
        int128 ethDelta = int128(packed >> 128);
        int128 qDelta = int128(packed);
        if (delta > 0) {
            uint256 ethOwed = uint256(-int256(ethDelta));
            uint256 qOwed = uint256(-int256(qDelta));
            require(manager.settle{value: ethOwed}() == ethOwed, "ETH settle");
            manager.sync(address(q));
            require(q.transfer(address(manager), qOwed), "Q transfer");
            require(manager.settle() == qOwed, "Q settle");
        } else {
            require(ethDelta > 0 && qDelta > 0, "no removal receipts");
            manager.take(address(0), address(this), uint256(uint128(ethDelta)));
            manager.take(address(q), address(this), uint256(uint128(qDelta)));
        }
        return "";
    }

    receive() external payable {}
}

contract OpenExecutableDepthGuardForkTest {
    IDepthForkVm private constant vm =
        IDepthForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    address private constant FACTORY = 0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e;
    address private constant POOL_MANAGER = 0x8366a39CC670B4001A1121B8F6A443A643e40951;
    address private constant POSITION_MANAGER = 0x58daec3116aae6D93017bAAea7749052E8a04fA7;
    address private constant STATE_VIEW = 0xF3334192D15450CdD385c8B70e03f9A6bD9E673b;
    address private constant PERMIT2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
    address private constant QUOTER = 0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94;
    address private constant ACTIVE_X = 0xeB765696eE5905ce1D06D72280dEFB2cE426115d;
    address private constant GRADUATED_X = 0x6C7C3113bFa9EeF3E716A4912D9B0dc47AEFE796;

    event log_named_uint(string key, uint256 val);

    struct Setup {
        HooklessLPExecutor executor;
        LaunchCursorToken q;
        OpenPriceGuard spot;
        OpenExecutableDepthGuard depth;
        DepthForkPoolSeeder seeder;
    }

    function _setup() private returns (Setup memory s) {
        vm.createSelectFork("https://rpc.mainnet.chain.robinhood.com");
        require(block.chainid == 4663, "not Robinhood");
        s.executor = new HooklessLPExecutor(POOL_MANAGER, POSITION_MANAGER, STATE_VIEW, PERMIT2);
        PositionInspector inspector = new PositionInspector(address(s.executor));
        address[] memory endpoints = new address[](0);
        s.q = new LaunchCursorToken(
            "Depth Test Q", "Q", 1_000_000_000 ether,
            FACTORY, address(s.executor), address(inspector),
            30, 3_000_000, 1 gwei, endpoints
        );
        s.executor.bindController(address(s.q));
        inspector.bindCursor(address(s.q));
        s.seeder = new DepthForkPoolSeeder(POOL_MANAGER, address(s.q));
        require(s.q.transfer(address(s.seeder), 1_000 ether), "seed Q funding");
        vm.deal(address(s.seeder), 100 ether);
        s.seeder.seed();
        s.spot = new OpenPriceGuard(FACTORY, STATE_VIEW, address(s.q), 2_500, 25, 1_000);
        s.executor.bindPriceGuard(address(s.spot));
        ExitSettlementRouter router = new ExitSettlementRouter(
            address(s.executor), address(s.q),
            address(new ExitRouterForkWETH()), address(new ExitRouterForkFanout()),
            payable(address(0xD00D)), 2_500, 25
        );
        s.executor.bindSettlementRouter(address(router));
        s.depth = new OpenExecutableDepthGuard(
            address(s.executor), address(s.spot), address(router), QUOTER, 1_500
        );
        s.executor.bindDepthGuard(address(s.depth));
        require(s.q.transfer(address(s.executor), 10 ether), "vault Q funding");
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

    function _up(int24 value, int24 spacing) private pure returns (int24) {
        int24 floor = _down(value, spacing);
        return floor == value ? floor : floor + spacing;
    }

    function _config(Setup memory s, address token) private view returns (ILaunchCursorConfigurator.OpenConfig memory c) {
        bool qIs0 = address(s.q) < token;
        int24 refTick = _floorTick(s.spot.referenceSqrtPriceX96(token));
        int24 lower;
        int24 upper;
        if (qIs0) {
            lower = _up(refTick + 5_000, 60);
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
        require(liquidity > 0 && liquidity <= type(uint128).max, "invalid test liquidity");
        c = ILaunchCursorConfigurator.OpenConfig({
            startingSqrtPriceX96: s.spot.referenceSqrtPriceX96(token),
            liquidity: uint128(liquidity), maxQuoteIn: uint128(targetQ),
            tickSpacing: 60, tickLower: lower, tickUpper: upper,
            deadline: uint64(block.timestamp + 120)
        });
    }

    function testRealQuoterAllowsOpenUnderCursorGasCap() external {
        Setup memory s = _setup();
        ILaunchCursorConfigurator.OpenConfig memory c = _config(s, ACTIVE_X);
        vm.prank(address(s.executor));
        (uint256 maxX, uint256 qOut, uint256 safeQ, uint256 requiredQ) =
            s.depth.validate(ACTIVE_X, c.liquidity, c.tickLower, c.tickUpper);
        require(maxX != 0 && qOut != 0 && safeQ >= requiredQ, "full-X quote failed");
        s.q.enqueue(ACTIVE_X);
        s.q.configureOpen(ACTIVE_X, c);
        uint256 gasBefore = gasleft();
        (bool attempted, bool succeeded) = s.q.processNext();
        uint256 gasUsed = gasBefore - gasleft();
        emit log_named_uint("full real-Quoter processNext gas", gasUsed);
        require(attempted && succeeded, "real Quoter open failed");
        require(gasUsed < 3_000_000, "cursor gas cap exceeded");
        (,, bool atQuoteBoundary,,) = s.executor.inspect(ACTIVE_X);
        require(atQuoteBoundary, "not Q-only");
    }

    function testRemovingQuoteDepthWithoutSpotMoveBlocksOpen() external {
        Setup memory s = _setup();
        ILaunchCursorConfigurator.OpenConfig memory c = _config(s, ACTIVE_X);
        vm.prank(address(s.executor));
        s.depth.validate(ACTIVE_X, c.liquidity, c.tickLower, c.tickUpper);
        uint160 spotBefore = s.spot.referenceSqrtPriceX96(ACTIVE_X);
        s.seeder.shrink();
        require(s.spot.referenceSqrtPriceX96(ACTIVE_X) == spotBefore, "spot changed");
        s.spot.validate(ACTIVE_X, c.startingSqrtPriceX96);
        s.q.enqueue(ACTIVE_X);
        s.q.configureOpen(ACTIVE_X, c);
        (bool attempted, bool succeeded) = s.q.processNext();
        require(attempted && !succeeded, "thin depth opened");
        bool opened;
        try s.executor.inspect(ACTIVE_X) returns (uint160, bool, bool, bool, bool) {
            opened = true;
        } catch {}
        require(!opened, "position was minted");
    }

    function testGraduatedPonsQuoterAllowsOpenUnderCursorGasCap() external {
        Setup memory s = _setup();
        ILaunchCursorConfigurator.OpenConfig memory c = _config(s, GRADUATED_X);
        vm.prank(address(s.executor));
        (uint256 maxX, uint256 qOut, uint256 safeQ, uint256 requiredQ) =
            s.depth.validate(GRADUATED_X, c.liquidity, c.tickLower, c.tickUpper);
        require(maxX != 0 && qOut != 0 && safeQ >= requiredQ, "graduated quote failed");
        s.q.enqueue(GRADUATED_X);
        s.q.configureOpen(GRADUATED_X, c);
        uint256 gasBefore = gasleft();
        (bool attempted, bool succeeded) = s.q.processNext();
        uint256 gasUsed = gasBefore - gasleft();
        emit log_named_uint("graduated real-Quoter processNext gas", gasUsed);
        require(attempted && succeeded, "graduated open failed");
        require(gasUsed < 3_000_000, "cursor gas cap exceeded");
    }
}
