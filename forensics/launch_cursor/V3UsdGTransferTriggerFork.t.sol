// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {CursorERC20} from "./LaunchCursorToken.sol";

/// @notice Read-only Robinhood fork proof for a replacement Q token. It creates a
/// fresh Q/USDG pool through the deployed Uniswap v3 factory, funds it from
/// forked state, and swaps in both directions. No transaction is broadcast.
interface IV3UsdGForkVm {
    function prank(address sender) external;
    function skip(bool skipTest) external;
}

interface IV3UsdGToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
}

interface IV3UsdGFactory {
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address);
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address);
}

interface IV3UsdGPool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function initialize(uint160 sqrtPriceX96) external;
    function mint(address recipient, int24 tickLower, int24 tickUpper, uint128 amount, bytes calldata data)
        external returns (uint256 amount0, uint256 amount1);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96,
        bytes calldata data) external returns (int256 amount0, int256 amount1);
    function slot0() external view returns (
        uint160 sqrtPriceX96, int24 tick, uint16 observationIndex,
        uint16 observationCardinality, uint16 observationCardinalityNext,
        uint8 feeProtocol, bool unlocked
    );
}

interface IV4UsdGManager {
    function unlock(bytes calldata data) external returns (bytes memory);
}

interface ITransferStep {
    function processNext(address from, address to) external;
}

interface IV3SwapCallbackPhase {
    function inSwapCallback() external view returns (bool);
}

/// @notice The essential replacement-Q change: authorize pool transfers to
/// attempt a processing step. The production version needs a gas cap, catch,
/// recursion guard and endpoint policy; this probe only tests call ordering.
contract V3TriggeredQ is CursorERC20 {
    address public pool;
    ITransferStep public processor;
    bool public armed;
    bool private inStep;

    constructor() CursorERC20("Fork proof Q", "Q") {
        _update(address(0), msg.sender, 1_000_000_000 ether);
    }

    function arm(address pool_, ITransferStep processor_) external {
        require(pool == address(0), "already armed");
        pool = pool_;
        processor = processor_;
        armed = true;
    }

    function _update(address from, address to, uint256 amount) internal override {
        super._update(from, to, amount);
        if (armed && !inStep && amount != 0 && from != address(0) && to != address(0) &&
            (from == pool || to == pool)) {
            inStep = true;
            processor.processNext(from, to);
            inStep = false;
        }
    }
}

/// @notice Probes both lock domains: same-pool v3 reentry must fail with LOK;
/// the separate deployed v4 PoolManager must call unlockCallback successfully.
contract V3ToV4StepProbe {
    V3TriggeredQ public immutable q;
    IV3UsdGPool public immutable v3Pool;
    IV4UsdGManager public immutable v4Manager;
    IV3SwapCallbackPhase public immutable swapCaller;
    uint256 public processed;
    uint256 public v3LockRejects;
    uint256 public v4UnlockCallbacks;
    address public lastFrom;
    address public lastTo;
    bool public lastDuringSwapCallback;
    bool private processing;

    constructor(V3TriggeredQ q_, IV3UsdGPool v3Pool_, IV4UsdGManager v4Manager_,
        IV3SwapCallbackPhase swapCaller_) {
        q = q_;
        v3Pool = v3Pool_;
        v4Manager = v4Manager_;
        swapCaller = swapCaller_;
    }

    function processNext(address from, address to) external {
        require(msg.sender == address(q) && !processing, "bad Q step");
        processing = true;
        (,,,,,, bool v3Unlocked) = v3Pool.slot0();
        require(!v3Unlocked, "v3 was not locked");

        // An exit or buyback that tries to use this same Q/USDG v3 pool
        // synchronously is impossible inside either transfer callback.
        try v3Pool.swap(address(this), true, 1, 4_295_128_740, "") returns (int256, int256) {
            revert("same-pool reentry succeeded");
        } catch Error(string memory reason) {
            require(keccak256(bytes(reason)) == keccak256("LOK"), "wrong v3 rejection");
            ++v3LockRejects;
        }

        v4Manager.unlock("");
        ++processed;
        lastFrom = from;
        lastTo = to;
        lastDuringSwapCallback = swapCaller.inSwapCallback();
        processing = false;
    }

    function unlockCallback(bytes calldata) external returns (bytes memory) {
        require(msg.sender == address(v4Manager) && processing, "bad v4 callback");
        (,,,,,, bool v3Unlocked) = v3Pool.slot0();
        require(!v3Unlocked, "v3 unlocked during v4 callback");
        ++v4UnlockCallbacks;
        return "";
    }
}

contract V3UsdGTransferTriggerForkTest {
    IV3UsdGForkVm private constant vm =
        IV3UsdGForkVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    IV3UsdGFactory private constant FACTORY =
        IV3UsdGFactory(0x1f7d7550B1b028f7571E69A784071F0205FD2EfA);
    IV3UsdGToken private constant USDG =
        IV3UsdGToken(0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168);
    address private constant WETH = 0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73;
    IV4UsdGManager private constant V4_MANAGER =
        IV4UsdGManager(0x8366a39CC670B4001A1121B8F6A443A643e40951);

    V3TriggeredQ private q;
    IV3UsdGPool private pool;
    V3ToV4StepProbe private step;
    bool public inSwapCallback;

    function setUp() external {
        if (block.chainid != 4663) return;
        // Give the test actor pre-existing USDG in fork state. The source
        // pool is unrelated to the new Q/USDG pool used in each test.
        address sourcePool = FACTORY.getPool(address(USDG), WETH, 3000);
        require(sourcePool != address(0) && USDG.balanceOf(sourcePool) >= 1_000_000_000,
            "USDG source unavailable");
        vm.prank(sourcePool);
        require(USDG.transfer(address(this), 1_000_000_000), "fork USDG funding failed");
    }

    function _setupPool() private {
        require(address(FACTORY).code.length != 0 && address(V4_MANAGER).code.length != 0,
            "canonical contracts absent");

        q = new V3TriggeredQ();
        pool = IV3UsdGPool(FACTORY.createPool(address(q), address(USDG), 3000));
        pool.initialize(uint160(1 << 96));

        pool.mint(address(this), -600, 600, 10_000_000_000, "");
        step = new V3ToV4StepProbe(q, pool, V4_MANAGER, IV3SwapCallbackPhase(address(this)));
        q.arm(address(pool), ITransferStep(address(step)));
    }

    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata) external {
        require(msg.sender == address(pool), "bad mint caller");
        _pay(pool.token0(), amount0Owed);
        _pay(pool.token1(), amount1Owed);
    }

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external {
        require(msg.sender == address(pool), "bad swap caller");
        inSwapCallback = true;
        if (amount0Delta > 0) _pay(pool.token0(), uint256(amount0Delta));
        if (amount1Delta > 0) _pay(pool.token1(), uint256(amount1Delta));
        inSwapCallback = false;
    }

    function _pay(address token, uint256 amount) private {
        require(IV3UsdGToken(token).transfer(address(pool), amount), "pool payment failed");
    }

    function _swapExactInput(address input, uint256 amount) private {
        bool zeroForOne = pool.token0() == input;
        uint160 limit = zeroForOne ? uint160(4_295_128_740) :
            uint160(1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_341);
        pool.swap(address(this), zeroForOne, int256(amount), limit, "");
    }

    function testBuyQTransferTriggersSeparateV4UnlockWhileV3Locked() external {
        vm.skip(block.chainid != 4663);
        _setupPool(); // Deployment, pool initialization, first buy and step are one call.
        _swapExactInput(address(USDG), 1_000_000);
        require(step.processed() == 1 && step.v4UnlockCallbacks() == 1, "buy did not process");
        require(step.lastFrom() == address(pool) && step.lastTo() == address(this), "wrong buy leg");
        require(!step.lastDuringSwapCallback(), "buy hook ran after callback began");
        require(step.v3LockRejects() == 1, "v3 reentry did not fail");
    }

    function testSellQTransferTriggersSeparateV4UnlockWhileV3Locked() external {
        vm.skip(block.chainid != 4663);
        _setupPool();
        _swapExactInput(address(q), 1_000_000);
        require(step.processed() == 1 && step.v4UnlockCallbacks() == 1, "sell did not process");
        require(step.lastFrom() == address(this) && step.lastTo() == address(pool), "wrong sell leg");
        require(step.lastDuringSwapCallback(), "sell hook ran outside callback");
        require(step.v3LockRejects() == 1, "v3 reentry did not fail");
    }
}
