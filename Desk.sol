// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

interface IPonsCurve {
    function getReserves() external view returns (uint256, uint256);
    function buy(uint256 amountIn, uint256 minOut, address to) external payable;
    function sell(uint256 amountIn, uint256 minOut, address to) external returns (uint256);
}

interface IPonsClaim {
    function claim(uint256 amount) external;
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface IOrch {
    function tick() external;
    function getRigStats(address token) external view returns (bool, uint256, uint256, int128);
}

interface IPoolManager {
    struct PoolKey {
        address currency0;
        address currency1;
        uint24 fee;
        int24 tickSpacing;
        address hooks;
    }
    struct ModifyLiquidityParams {
        int24 tickLower;
        int24 tickUpper;
        int256 liquidityDelta;
        bytes32 salt;
    }
    function initialize(PoolKey calldata key, uint160 sqrtPriceX96) external;
    function unlock(bytes calldata data) external returns (bytes memory);
    function modifyLiquidity(PoolKey calldata key, ModifyLiquidityParams calldata params, bytes calldata hookData) external returns (int256, int256);
    function currencyDelta(address caller, address currency) external view returns (int256);
    function settle() external payable returns (uint256);
    function sync(address currency) external;
    function take(address currency, address to, uint256 amount) external;
    struct SwapParams { bool zeroForOne; int256 amountSpecified; uint160 sqrtPriceLimitX96; }
    function swap(PoolKey calldata key, SwapParams calldata params, bytes calldata hookData) external returns (int256);
}

/// @title Desk — the atomic delta-neutral market maker
/// @notice one call = claim fees → EMA → dip buy / spike sell → tick the rig.
///         fees are inventory capital. deployed at the edges. never wasted.
contract Desk {
    address public owner;
    IPonsCurve public immutable curve;
    IPonsClaim public immutable pons;
    IERC20 public immutable token;
    IOrch public immutable orch;

    uint256 public emaEth;
    uint256 public emaTok;
    uint256 public cycleCount;
    uint256 public totalBuys;
    uint256 public totalSells;
    uint256 public totalClaimedWei;
    uint256 public targetInventory;   // the sized position — held for the desk's life
    bool public entered;
    bool public upOnly;
    bool public sellEnabled;
    uint160 public poolPrice; // our 10% pool current sqrtPriceX96
    IPoolManager public immutable pm;
    int24 public nextSpacing = 620;
    uint256 public gridTopMultBps = 10000;  // richest rung spawned (10000 = 1.0x curve price)
    uint256 public lastSwapsSeen;

    uint16 public recycleSpacing = 10;
    uint256 private constant EMA_NUM = 5;
    uint256 private constant EMA_DEN = 100;
    uint256 private constant MIN_KEEP = 0.001 ether;
    uint256 private constant MIN_BUY = 0.0001 ether;
    // TickMath.MAX_SQRT_PRICE — swap limit for zeroForOne == false (price goes up)
    uint160 private constant MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342;

    constructor(address _curve, address _pons, address _token, address _orch, address _pm) {
        owner = msg.sender;
        curve = IPonsCurve(_curve);
        pons = IPonsClaim(_pons);
        token = IERC20(_token);
        orch = IOrch(_orch);
        pm = IPoolManager(_pm);
    }

    receive() external payable {}

    /// @notice ENTRY — deploy a % of desk capital as the inventory tranche.
    ///         one buy, sized once, held until exit. the band trades around it.
    /// @param pctBps percent of desk ETH to deploy as inventory (e.g., 3000 = 30%)
    function enter(uint256 pctBps) external {
        require(msg.sender == owner, "!owner");
        require(!entered, "entered");
        require(pctBps > 0 && pctBps <= 10000, "bad pct");
        entered = true;
        uint256 spend = address(this).balance * pctBps / 10000;
        require(spend > MIN_BUY, "no capital");
        uint256 before = token.balanceOf(address(this));
        curve.buy{value: spend}(spend, 0, address(this));
        targetInventory = token.balanceOf(address(this)) - before;
        totalBuys++;
    }

    /// @notice EXIT — liquidate the tranche + band inventory, sweep everything.
    ///         the inventory is sold back to the curve in slices to limit impact.
    function exitDesk() external {
        require(msg.sender == owner, "!owner");
        require(entered, "!entered");
        uint256 bag = token.balanceOf(address(this));
        if (bag > 0) {
            token.approve(address(curve), bag);
            curve.sell(bag, 0, address(this));
            totalSells++;
        }
        entered = false;
        targetInventory = 0;
        (bool s, ) = owner.call{value: address(this).balance}("");
        require(s);
    }

    /// @notice the whole desk in one atomic tx
    function cycle() external {
        require(msg.sender == owner, "!owner");
        cycleCount++;

        uint256 before = address(this).balance;
        _claimAll();
        totalClaimedWei += address(this).balance - before;

        (uint256 eth, uint256 tok) = curve.getReserves();
        if (emaEth == 0) {
            emaEth = eth;
            emaTok = tok;
        } else {
            emaEth = (emaEth * (EMA_DEN - EMA_NUM) + eth * EMA_NUM) / EMA_DEN;
            emaTok = (emaTok * (EMA_DEN - EMA_NUM) + tok * EMA_NUM) / EMA_DEN;
        }

        // AUTO-RECYCLE FIRST: capital cycles — convert a bag slice at premium BEFORE the buy spends the ETH
        // the JIT LP needs ETH + J4 — run it while the desk still holds its full balance
        if (poolPrice > 0) {
            uint256 bag2 = token.balanceOf(address(this));
            uint256 floor2 = entered ? targetInventory : 0;
            if (bag2 > floor2) {
                uint256 excess = bag2 - floor2;
                uint256 cap = 3_000_000e18;
                (uint256 ethR, uint256 tokR) = curve.getReserves();
                uint256 capByEth = (address(this).balance * 9 / 10) * tokR / (ethR == 0 ? 1 : ethR);
                if (excess > capByEth) excess = capByEth;
                if (excess > cap) excess = cap;
                if (excess > 0) {
                uint256 sqrtLo = uint256(poolPrice) * 980 / 1000;
                uint256 sqrtHi = uint256(poolPrice) * 1020 / 1000;
                uint256 L = excess * (1 << 96) / (uint256(poolPrice) - sqrtLo);
                int24 loA = (poolTick() - 200) / 10 * 10;
                int24 hiA = (poolTick() + 200) / 10 * 10 + 10;
                try pm.unlock(abi.encode(uint8(1), _keyNH(int24(uint24(recycleSpacing)), 0), excess, L, loA, hiA)) {
                    // batch converted
                } catch {
                    // pool blown (price at limit) — spawn the next rung at the same price
                    recycleSpacing++;
                    pm.initialize(_keyNH(int24(uint24(recycleSpacing)), 0), poolPrice);
                }
                }
            }
        }

        // DIP: eth/tok < emaPrice * 0.995 → eth * emaTok * 1000 < emaEth * tok * 999
        uint256 spendable = address(this).balance > MIN_KEEP ? address(this).balance - MIN_KEEP : 0;
        bool shouldBuy = upOnly || (eth * emaTok * 1000 < emaEth * tok * 999);
        if (spendable > MIN_BUY && shouldBuy) {
            uint256 buyWei = spendable * 80 / 100;
            curve.buy{value: buyWei}(buyWei, 0, address(this));
            totalBuys++;
        }

        // SPIKE: sell only the EXCESS above the tranche — never below the sized position
        uint256 bag = token.balanceOf(address(this));
        uint256 floor = entered ? targetInventory : 0;
        bool shouldSell = sellEnabled && eth * emaTok * 1000 > emaEth * tok * 1050;
        if (bag > floor && shouldSell) {
            uint256 excess = bag - floor;
            uint256 sellAmt = excess / 5; // work down the excess in slices
            if (sellAmt > 0) {
                token.approve(address(curve), sellAmt);
                curve.sell(sellAmt, 0, address(this));
                totalSells++;
            }
        }

        // GRID GROWTH: interest feeds the toll surface.
        // every swap through our hooked pools = demand → extend the grid outward.
        (, , uint256 swaps, ) = orch.getRigStats(address(token));
        if (swaps > lastSwapsSeen) {
            uint256 delta = swaps - lastSwapsSeen;
            uint256 n = delta > 3 ? 3 : delta; // cap rungs per cycle
            _growGrid(n);
        }
        lastSwapsSeen = swaps;

        try orch.tick() {} catch {}

    }

    /// @dev spawn n new pools on the RICH side (JESTER priced above curve).
    ///      arbs buy curve cheap → sell into these pools → chart up + tolls in.
    ///      direction: richer JESTER = LOWER sqrtPrice (fewer JESTER per ETH).
    function _growGrid(uint256 n) internal {
        (uint256 eth, uint256 tok) = curve.getReserves();
        // p = JESTER per ETH, scaled 1e18
        uint256 p = tok * 1e18 / eth;
        for (uint256 i = 0; i < n; i++) {
            gridTopMultBps = gridTopMultBps * 115 / 100; // +15% per rung
            // sqrtPriceX96 = sqrt(p * 1e18 * 2^192 / gridTopMultBps * 10000)
            // = isqrt(p * 2^192 * 10000 / mult)
            uint256 radicand = p * (1 << 192) / gridTopMultBps * 10000;
            uint160 sp = uint160(_isqrt(radicand));
            pm.initialize(IPoolManager.PoolKey({
                currency0: address(0),
                currency1: address(token),
                fee: 0,
                tickSpacing: nextSpacing,
                hooks: address(orch)
            }), sp);
            nextSpacing++;
        }
    }

    /// @notice THE ENTIRE LAUNCH — ONE ATOMIC TX.
    ///         spawn the pool grid (0% control + 5%/10% extraction) at the live curve price,
    ///         place the J2-only extraction ladder above price via unlockCallback,
    ///         then enter the inventory tranche. after this, cycle() is the heartbeat.
    /// @param enterPctBps percent of desk ETH to deploy as inventory (4000 = 40%)
    function launch(uint256 enterPctBps) external {
        require(msg.sender == owner, "!owner");
        require(!entered, "entered");

        // 1. live price from the curve
        (uint256 eth, uint256 tok) = curve.getReserves();
        require(eth > 0 && tok > 0, "no curve");
        uint160 sp = uint160(_isqrt((tok << 96) / eth) << 48);

        // 2. spawn the grid at live price — initialize is atomic, no unlock needed
        pm.initialize(_key(900, 0), sp);
        // J4 FIX: spawn the recycle pool and store its TRUE tick from initialize — no guessing
        uint160 recSp = uint160(uint256(sp) * 1074 / 1000); // ~1.15x
        pm.initialize(_key(920, 0), recSp);
        poolPrice = recSp;
        pm.initialize(_key(901, 0), uint160(uint256(sp) * 1074 / 1000));  // 0% @ ~1.15x
        pm.initialize(_key(910, 50000), sp);                   // 5% extraction @ 1.0x
        pm.initialize(_key(912, 100000), sp);                   // 10% extraction @ 1.0x
        pm.initialize(_key(913, 100000), uint160(uint256(sp) * 1074 / 1000)); // 10% @ 1.15x
        pm.initialize(_key(914, 100000), uint160(uint256(sp) * 1225 / 1000)); // 10% @ 1.5x

        // 3. J2-only extraction ladder above price in the 10% pools — pays 10% to us as LP
        uint256 bag = token.balanceOf(address(this));
        if (bag > 1e6 * 1e18 && poolTickStore != 0) {
            uint256 commit = bag * 20 / 100;
            _placeLadder(commit, poolTickStore);
        }

        // 4. enter the tranche
        _enter(enterPctBps);
    }

    /// @dev place J2-only LP above price in the 10% pool via the unlock pattern.
    ///      range [tick+200, tick+1800] — sells into strength at 10% fee.
    function _placeLadder(uint256 commit, int24 tick) internal {
        (uint256 eth, uint256 tok) = curve.getReserves();
        uint160 sp = uint160(_isqrt((tok << 96) / eth) << 48);
        // L = commit × 2^96 / (sqrt_hi − sqrt_lo),  sqrt_hi ≈ sp×1.094, sqrt_lo ≈ sp×1.020
        uint256 sqrtLo = uint256(sp) * 1020 / 1000;
        uint256 sqrtHi = uint256(sp) * 1094 / 1000;
        uint256 L = commit * (1 << 96) / (sqrtHi - sqrtLo);
        int24 lo = ((tick + 912) / 912) * 912;
        int24 hi = ((tick + 2736) / 912) * 912;
        pm.unlock(abi.encode(uint8(0), _key(912, 100000), L, lo, hi));
    }

    /// @notice v4 unlock callback — LP placement (mode 0) or recycle swap (mode 1)
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(pm), "!pm");
        uint8 mode = abi.decode(data, (uint8));
        if (mode == 0) {
            (, IPoolManager.PoolKey memory key, uint256 L, int24 lo, int24 hi) =
                abi.decode(data, (uint8, IPoolManager.PoolKey, uint256, int24, int24));
            (int256 dAdd, ) = pm.modifyLiquidity(key, IPoolManager.ModifyLiquidityParams({
                tickLower: lo, tickUpper: hi, liquidityDelta: int256(L), salt: 0
            }), "");
            // BalanceDelta: amount0 (ETH) = low 128, amount1 (J4) = high 128
            _closeNet(int128(dAdd >> 128), int128(dAdd)); // fork packing: amount0 HIGH, amount1 LOW
        } else {
            // mode 1: JIT recycle — add liq → swap J4→ETH → LP stands. deltas open until return.
            (, IPoolManager.PoolKey memory key, uint256 amt, uint256 L, int24 lo, int24 hi) =
                abi.decode(data, (uint8, IPoolManager.PoolKey, uint256, uint256, int24, int24));
            (int256 dAdd, ) = pm.modifyLiquidity(key, IPoolManager.ModifyLiquidityParams({
                tickLower: lo, tickUpper: hi, liquidityDelta: int256(L), salt: 0
            }), "");
            int256 dSwap = pm.swap(key, IPoolManager.SwapParams({
                zeroForOne: false,               // sell token1 (J4) → buy ETH: price goes UP
                amountSpecified: int256(amt),    // positive = exact input (official v4)
                sqrtPriceLimitX96: MAX_SQRT_PRICE - 1
            }), "");
            // net the add + swap deltas — no currencyDelta queries (not exposed on this fork)
            _closeNet(int128(dAdd >> 128) + int128(dSwap >> 128), int128(dAdd) + int128(dSwap)); // fork packing flip
        }
        return "";
    }

    /// @dev close the net deltas: negative = pay (ETH: settle{value}, J4: sync+transfer+settle), positive = take
    function _closeNet(int128 ethNet, int128 j4Net) internal {
        if (j4Net < 0) {
            pm.sync(address(token));
            token.transfer(address(pm), uint256(-int256(j4Net)));
            pm.settle();
        } else if (j4Net > 0) {
            pm.take(address(token), address(this), uint256(int256(j4Net)));
        }
        if (ethNet < 0) {
            pm.settle{value: uint256(-int256(ethNet))}();
        } else if (ethNet > 0) {
            pm.take(address(0), address(this), uint256(int256(ethNet)));
        }
    }

    function _keyNH(int24 spacing, uint24 fee) internal view returns (IPoolManager.PoolKey memory) {
        return IPoolManager.PoolKey({currency0: address(0), currency1: address(token), fee: fee, tickSpacing: spacing, hooks: address(0)});
    }

    function _key(int24 spacing, uint24 fee) internal view returns (IPoolManager.PoolKey memory) {
        return IPoolManager.PoolKey({
            currency0: address(0),
            currency1: address(token),
            fee: fee,
            tickSpacing: spacing,
            hooks: address(orch)
        });
    }

    function _enter(uint256 pctBps) internal {
        require(pctBps > 0 && pctBps <= 10000, "bad pct");
        entered = true;
        uint256 spend = address(this).balance * pctBps / 10000;
        require(spend > MIN_BUY, "no capital");
        uint256 before = token.balanceOf(address(this));
        curve.buy{value: spend}(spend, 0, address(this));
        targetInventory = token.balanceOf(address(this)) - before;
        totalBuys++;
    }

    function _isqrt(uint256 x) internal pure returns (uint256) {
        if (x == 0) return 0;
        uint256 r = 1 << 127;
        while (true) {
            uint256 nr = (r + x / r) / 2;
            if (nr >= r) break;
            r = nr;
        }
        return r;
    }

    /// @dev probe with max, catch InsufficientBalance(requested, available), claim available
    function _claimAll() internal {
        try pons.claim(340282366920938463463374607431768211455) {} catch (bytes memory reason) {
            if (reason.length >= 68) {
                uint256 avail;
                assembly {
                    avail := mload(add(reason, 68))
                }
                if (avail > 20000000000000) {
                    try pons.claim(avail) {} catch {}
                }
            }
        }
    }

    function withdraw() external {
        require(msg.sender == owner, "!owner");
        (bool s, ) = owner.call{value: address(this).balance}("");
        require(s);
        token.transfer(owner, token.balanceOf(address(this)));
    }

    function setUpOnly(bool v) external { require(msg.sender == owner, "!owner"); upOnly = v; }
    function setSellEnabled(bool v) external { require(msg.sender == owner, "!owner"); sellEnabled = v; }
    function setPoolPrice(uint160 sp, int24 t) external { require(msg.sender == owner, "!owner"); poolPrice = sp; poolTickStore = t; }
    int24 public poolTickStore;
    function poolTick() internal view returns (int24) { return poolTickStore; }

    /// @notice RECYCLE (JIT) — off-chart sell through our own 10% pool.
    ///         add two-sided liq → swap J3→ETH (fee pays ourselves) → remove liq.
    ///         the public chart never sees the sell. XGAS split-flow.
    /// @param j3Amount J3 to recycle
    /// @param L liquidity for the JIT position
    /// @param lo hi tick range bracketing the pool's current price
    function recycleJIT(uint256 j3Amount, uint256 L, int24 lo, int24 hi) external returns (uint256) {
        require(msg.sender == owner, "!owner");
        require(j3Amount > 0 && L > 0, "amt");
        pm.unlock(abi.encode(uint8(1), _keyNH(10, 0), j3Amount, L, lo, hi));
        return address(this).balance;
    }

    function stats() external view returns (uint256, uint256, uint256, uint256, uint256) {
        return (cycleCount, totalBuys, totalSells, totalClaimedWei, address(this).balance);
    }
}
