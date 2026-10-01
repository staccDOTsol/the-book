// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

interface IPM {
    struct PoolKey { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }
    struct ModifyLiquidityParams { int24 tickLower; int24 tickUpper; int256 liquidityDelta; bytes32 salt; }
    struct SwapParams { bool zeroForOne; int256 amountSpecified; uint160 sqrtPriceLimitX96; }
    function unlock(bytes calldata data) external returns (bytes memory);
    function initialize(PoolKey calldata key, uint160 sqrtPriceX96) external;
    function modifyLiquidity(PoolKey calldata key, ModifyLiquidityParams calldata params, bytes calldata hookData) external;
    function swap(PoolKey calldata key, SwapParams calldata params, bytes calldata hookData) external;
    function currencyDelta(address caller, address currency) external view returns (int256);
    function settle() external payable returns (uint256);
    function sync(address currency) external;
    function take(address currency, address to, uint256 amount) external;
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

/// @title TestBench — bisect the unlock path. one deploy, three isolated callbacks.
///        t_add: modifyLiquidity(+L) then close — must leave a live position.
///        t_swap: swap only, exact-in, correct limit, against existing pool liq.
///        t_jit: add → swap → remove → close — the whole cycle in one unlock.
contract TestBench {
    IPM public immutable pm;
    IERC20 public immutable tok;
    address public immutable owner;

    // TickMath.MAX_SQRT_PRICE — swap limit for zeroForOne == false (price goes up)
    uint160 private constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    constructor(address _pm, address _tok) { pm = IPM(_pm); tok = IERC20(_tok); owner = msg.sender; }

    function key() internal view returns (IPM.PoolKey memory) {
        return IPM.PoolKey({currency0: address(0), currency1: address(tok), fee: 0, tickSpacing: 10, hooks: address(0)});
    }

    /// t_add: unlock + modifyLiquidity ADD only
    function t_add(uint256 L, int24 lo, int24 hi) external {
        pm.unlock(abi.encode(uint8(2), L, lo, hi));
    }

    /// t_swap: unlock + swap ONLY (exact-in, correct limit) against existing pool liq
    function t_swap(uint256 amt) external {
        pm.unlock(abi.encode(uint8(3), amt));
    }

    /// t_jit: unlock + add → swap → remove → net close
    function t_jit(uint256 L, int24 lo, int24 hi, uint256 amt) external {
        pm.unlock(abi.encode(uint8(4), L, lo, hi, amt));
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) { return _cb(data); }
    fallback(bytes calldata) external payable returns (bytes memory) { return ""; }

    function _cb(bytes calldata data) internal returns (bytes memory) {
        require(msg.sender == address(pm), "!pm");
        uint8 mode = abi.decode(data, (uint8));
        IPM.PoolKey memory k = key();
        if (mode == 2) {
            (, uint256 L, int24 lo, int24 hi) = abi.decode(data, (uint8, uint256, int24, int24));
            pm.modifyLiquidity(k, IPM.ModifyLiquidityParams({tickLower: lo, tickUpper: hi, liquidityDelta: int256(L), salt: 0}), "");
        } else if (mode == 3) {
            (, uint256 amt) = abi.decode(data, (uint8, uint256));
            pm.swap(k, IPM.SwapParams({
                zeroForOne: false,               // sell token1 → buy ETH: price goes UP
                amountSpecified: -int256(amt),  // exact IN
                sqrtPriceLimitX96: MAX_SQRT_PRICE - 1
            }), "");
        } else if (mode == 4) {
            (, uint256 L, int24 lo, int24 hi, uint256 amt) = abi.decode(data, (uint8, uint256, int24, int24, uint256));
            IPM.ModifyLiquidityParams memory lp = IPM.ModifyLiquidityParams({
                tickLower: lo, tickUpper: hi, liquidityDelta: int256(L), salt: 0
            });
            pm.modifyLiquidity(k, lp, "");                       // add
            pm.swap(k, IPM.SwapParams({                          // swap through the range
                zeroForOne: false,
                amountSpecified: -int256(amt),
                sqrtPriceLimitX96: MAX_SQRT_PRICE - 1
            }), "");
            lp.liquidityDelta = -lp.liquidityDelta;
            pm.modifyLiquidity(k, lp, "");                       // remove — same ticks + salt
        }
        _close(address(0));
        _close(address(tok));
        return "";
    }

    /// @dev close one currency's open delta against the PM.
    ///      owe ERC20: sync → transfer → settle. owe ETH: settle{value}. owed: take.
    function _close(address c) internal {
        int256 d = pm.currencyDelta(address(this), c);
        if (d < 0) {
            uint256 owed = uint256(-d);
            if (c == address(0)) {
                pm.settle{value: owed}();
            } else {
                pm.sync(c);
                tok.transfer(address(pm), owed);
                pm.settle();
            }
        } else if (d > 0) {
            pm.take(c, address(this), uint256(d));
        }
    }

    receive() external payable {}
}
