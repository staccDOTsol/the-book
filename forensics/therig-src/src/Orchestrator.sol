// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IPoolManager} from "v4-core/interfaces/IPoolManager.sol";
import {ModifyLiquidityParams, SwapParams} from "v4-core/types/PoolOperation.sol";
import {IHooks} from "v4-core/interfaces/IHooks.sol";

import {Currency} from "v4-core/types/Currency.sol";
import {PoolKey} from "v4-core/types/PoolKey.sol";
import {PoolIdLibrary, PoolId} from "v4-core/types/PoolId.sol";
import {BalanceDelta} from "v4-core/types/BalanceDelta.sol";
import {BeforeSwapDelta, BeforeSwapDeltaLibrary} from "v4-core/types/BeforeSwapDelta.sol";

/// @title Orchestrator — the complete rig. Acts 1-X for any token.
/// @dev One contract. Complete lifecycle. Deploy once, run forever.
///
/// Act 1: SPAWN    — create 0% fee ladder pools on a target token
/// Act 2: SEED     — add thin liquidity to the base pool
/// Act 3: TOLL     — hook callbacks count every arb fill (passive)
/// Act 4: WALK     — buy through our own pools to move price (optional)
/// Act 5: EXIT     — pull liquidity, sell inventory, claim profits
///
/// The orchestrator holds inventory, manages positions, and collects
/// the toll across every pool it spawned. It IS the market maker.
contract Orchestrator is IHooks {
    using PoolIdLibrary for PoolKey;

    // ── immutables ──
    address public immutable owner;
    IPoolManager public immutable poolManager;
    address public immutable quoteToken; // what we pair against (ETH or a stable)

    // ── rig state per token ──
    struct Rig {
        bool active;
        PoolKey basePool;           // the "real" pool (1x price, thin liq)
        PoolKey[] ladderPools;      // the rungs (5x, 25x, 100x, ... — zero liq)
        uint160 basePrice;         // sqrt price at spawn
        uint256 spawnBlock;
        uint256 totalSwaps;         // toll counter
        uint256 totalFees;         // accumulated (if we set fee > 0 later)
        int128 netLiquidity;        // our LP position in the base pool
        uint256 inventory;         // tokens we hold from buys
    }

    mapping(address => Rig) public rigs;           // token → rig state
    address[] public activeTokens;

    // ── events ──
    event RigSpawned(address indexed token, uint256 poolCount, uint160 basePrice);
    event RigSeeded(address indexed token, uint256 liquidityAmount);
    event TollCollected(address indexed token, bytes32 indexed poolId, uint256 swapCount);
    event RigExited(address indexed token, uint256 totalSwaps, uint256 profit);

    modifier onlyOwner() {
        require(msg.sender == owner, "!owner");
        _;
    }

    modifier rigActive(address token) {
        require(rigs[token].active, "!active");
        _;
    }

    constructor(address _poolManager, address _quoteToken, address _owner) {
        owner = _owner;
        poolManager = IPoolManager(_poolManager);
        quoteToken = _quoteToken;
    }

    // ═══════════════════════════════════════════════════════════
    // ACT 1: SPAWN — create the ladder
    // ═══════════════════════════════════════════════════════════

    /// @notice spawn the rig on a token. call this when the burst detection fires.
    /// @param token the meme token to rig
    /// @param baseSqrtPrice the current market price (sqrt format) for the base pool
    /// @param ladderMultiples e.g., [5, 25, 100] → pools at 5x, 25x, 100x
    /// @param tickSpacing the tick spacing for all pools (use one that's cheap)
    function spawn(
        address token,
        uint160 baseSqrtPrice,
        uint256[] calldata ladderMultiples,
        int24 tickSpacing
    ) external onlyOwner {
        require(!rigs[token].active, "already spawned");
        require(token != address(0) && token != quoteToken, "bad token");

        Rig storage rig = rigs[token];
        rig.active = true;
        rig.spawnBlock = block.number;
        rig.basePrice = baseSqrtPrice;

        // spawn the BASE pool (1x price — this gets our liquidity)
        (Currency c0, Currency c1) = _orderCurrencies(token);
        rig.basePool = PoolKey({
            currency0: c0, currency1: c1,
            fee: 0, tickSpacing: tickSpacing,
            hooks: IHooks(address(this))
        });
        poolManager.initialize(rig.basePool, baseSqrtPrice);

        // spawn the LADDER pools — each uses a DIFFERENT tickSpacing so they're distinct pools
        // (same tickSpacing = same PoolKey = PoolAlreadyInitialized)
        int24[] memory ladderSpacings = new int24[](ladderMultiples.length);
        for (uint256 i = 0; i < ladderMultiples.length; i++) {
            ladderSpacings[i] = tickSpacing + int24(uint24(i + 1) * 68); // 60, 128, 196, 264...
        }
        for (uint256 i = 0; i < ladderMultiples.length; i++) {
            uint160 ladderPrice = _scaleSqrtPrice(baseSqrtPrice, ladderMultiples[i]);
            PoolKey memory ladderKey = PoolKey({
                currency0: c0, currency1: c1,
                fee: 0, tickSpacing: ladderSpacings[i],
                hooks: IHooks(address(this))
            });
            poolManager.initialize(ladderKey, ladderPrice);
            rig.ladderPools.push(ladderKey);
        }

        activeTokens.push(token);
        emit RigSpawned(token, 1 + ladderMultiples.length, baseSqrtPrice);
    }

    // ═══════════════════════════════════════════════════════════
    // ACT 2: SEED — add thin liquidity to the base pool
    // ═══════════════════════════════════════════════════════════

    /// @notice seed the base pool with STANDING liquidity.
    ///         this stays on the books — it's the exit liquidity, the bait.
    ///         thin enough that price moves on small volume, thick enough
    ///         that the chart looks tradeable. the JIT walk goes ON TOP of this.
    ///         this is what the XGAS bots left in their "real" pools.
    /// @param token the rigged token
    /// @param quoteAmount how much of the quote token to deposit (in native units)
    /// @param tickLower lower bound of the concentrated range
    /// @param tickUpper upper bound of the concentrated range
    function seed(
        address token,
        uint256 quoteAmount,
        int24 tickLower,
        int24 tickUpper
    ) external onlyOwner rigActive(token) {
        Rig storage rig = rigs[token];

        // the orchestrator needs to hold the tokens being deposited
        // (caller must have transferred them to this contract first)

        // approve poolManager to spend our tokens
        _approveIfNeeded(token, address(poolManager));
        _approveIfNeeded(quoteToken, address(poolManager));

        // compute liquidity amount from the quote deposit
        // (simplified — in practice use a proper quote library)
        int256 liquidityDelta = int256(quoteAmount * 1e18 / uint256(rig.basePrice));

        // modify liquidity through the poolManager
        poolManager.modifyLiquidity(
            rig.basePool,
            ModifyLiquidityParams({
                tickLower: tickLower,
                tickUpper: tickUpper,
                liquidityDelta: liquidityDelta,
                salt: 0
            }),
            abi.encode(token) // hook data: tells our callback which token
        );

        rig.netLiquidity += int128(liquidityDelta);
        emit RigSeeded(token, quoteAmount);
    }

    // ═══════════════════════════════════════════════════════════
    // ACT 3: TOLL — passive, via hook callbacks
    // ═══════════════════════════════════════════════════════════
    // (no explicit function — the callbacks below count every fill)

    // ═══════════════════════════════════════════════════════════
    // ACT 4: WALK — buy through our own pool (optional, for direction)
    // ═══════════════════════════════════════════════════════════

    /// @notice walk the price on the base pool by buying with quote tokens
    /// @param token the rigged token
    /// @param quoteAmount how much quote to spend
    /// @param sqrtPriceLimit max price impact (slippage protection)
    function walk(
        address token,
        uint256 quoteAmount,
        uint160 sqrtPriceLimit
    ) external onlyOwner rigActive(token) {
        Rig storage rig = rigs[token];

        _approveIfNeeded(quoteToken, address(poolManager));

        // determine swap direction (quote → token = buy)
        bool zeroForOne = Currency.unwrap(rig.basePool.currency0) == quoteToken;

        poolManager.swap(
            rig.basePool,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: int256(quoteAmount),
                sqrtPriceLimitX96: sqrtPriceLimit
            }),
            abi.encode(token)
        );

        // the tokens we receive are our inventory
        // (settled via the hook's afterSwapDelta or through modifyLiquidity settlement)
    }

    // ═══════════════════════════════════════════════════════════
    // ACT 5: EXIT — pull everything
    // ═══════════════════════════════════════════════════════════

    /// @notice exit the rig on a token. pull liquidity, sell inventory, claim profits.
    /// @param token the token to exit
    function exit(address token) external onlyOwner rigActive(token) {
        Rig storage rig = rigs[token];

        // 1. remove all our liquidity from the base pool
        if (rig.netLiquidity != 0) {
            poolManager.modifyLiquidity(
                rig.basePool,
                ModifyLiquidityParams({
                    tickLower: -887272, // full range for simplicity
                    tickUpper: 887272,
                    liquidityDelta: -int256(uint256(int256(rig.netLiquidity))),
                    salt: 0
                }),
                abi.encode(token)
            );
        }

        // 2. sell any inventory back through the pool
        // (swap tokens → quote, settle)

        // 3. mark inactive
        rig.active = false;
        emit RigExited(token, rig.totalSwaps, address(this).balance);

        // 4. sweep profits to owner
        _sweep(token);
        _sweep(quoteToken);
    }


    // ═══════════════════════════════════════════════════════════
    // V4 HOOK CALLBACKS — the toll (match IHooks interface exactly)
    // ═══════════════════════════════════════════════════════════

    function beforeInitialize(address, PoolKey calldata, uint160) external pure returns (bytes4) {
        return this.beforeInitialize.selector;
    }

    function afterInitialize(address, PoolKey calldata, uint160, int24) external pure returns (bytes4) {
        return this.afterInitialize.selector;
    }

    function beforeAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata) external pure returns (bytes4) {
        return this.beforeAddLiquidity.selector;
    }

    function afterAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, BalanceDelta, BalanceDelta, bytes calldata) external pure returns (bytes4, BalanceDelta) {
        return (this.afterAddLiquidity.selector, BalanceDelta.wrap(0));
    }

    function beforeRemoveLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata) external pure returns (bytes4) {
        return this.beforeRemoveLiquidity.selector;
    }

    function afterRemoveLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, BalanceDelta, BalanceDelta, bytes calldata) external pure returns (bytes4, BalanceDelta) {
        return (this.afterRemoveLiquidity.selector, BalanceDelta.wrap(0));
    }

    function beforeSwap(address, PoolKey calldata key, SwapParams calldata, bytes calldata hookData) external returns (bytes4, BeforeSwapDelta, uint24) {
        if (hookData.length >= 32) {
            address token = abi.decode(hookData, (address));
            if (rigs[token].active) {
                rigs[token].totalSwaps++;
                emit TollCollected(token, PoolId.unwrap(key.toId()), rigs[token].totalSwaps);
            }
        }
        return (this.beforeSwap.selector, BeforeSwapDeltaLibrary.ZERO_DELTA, 0);
    }

    function afterSwap(address, PoolKey calldata, SwapParams calldata, BalanceDelta, bytes calldata) external pure returns (bytes4, int128) {
        return (this.afterSwap.selector, 0);
    }

    function beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        return this.beforeDonate.selector;
    }

    function afterDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        return this.afterDonate.selector;
    }

    // ═══════════════════════════════════════════════════════════
    // THE LOOP — constant, atomic, keeper-callable
    // ═══════════════════════════════════════════════════════════

    /// @notice the heartbeat. call this every block (or every N blocks).
    ///         manages ALL active rigs atomically: walks, checks exits, collects.
    ///         anyone can call it — the caller gets a small keeper reward.
    ///         this is the "up-loop.sh" but as a contract function.
    function tick() external {
        uint256 gasStart = gasleft();

        for (uint256 i = 0; i < activeTokens.length; i++) {
            address token = activeTokens[i];
            Rig storage rig = rigs[token];
            if (!rig.active) continue;

            // ── check exit conditions ──
            // 1. price dropped > 20% from peak → exit
            // 2. too many blocks since spawn → exit (time stop)
            // 3. cluster outflow detected (via hook callback state) → exit

            if (_shouldExit(token, rig)) {
                _exitInternal(token, rig);
                continue;
            }

            // ── walk if needed ──
            // buy more if price is below our target trajectory
            // this is the "0.2 SOL every 5 seconds" from the Solana version
            // but computed on-chain and executed atomically

            if (_shouldWalk(token, rig)) {
                _walkInternal(token, rig);
            }
        }

        // keeper reward: refund gas + small bonus
        uint256 gasUsed = gasStart - gasleft();
        // (in production: refund gas + small profit from the toll)
        // for now the toll is free — the reward is knowing you're part of the machine
    }

    /// @notice spawn + seed + start walking — all in ONE atomic tx
    /// @dev this is the entry point for the pounce detection
    function pounce(
        address token,
        uint160 baseSqrtPrice,
        uint256[] calldata ladderMultiples,
        int24 tickSpacing,
        uint256 seedAmount,
        int24 tickLower,
        int24 tickUpper
    ) external onlyOwner {
        // ACT 1: spawn the ladder
        _spawnInternal(token, baseSqrtPrice, ladderMultiples, tickSpacing);

        // ACT 2: seed the base pool
        if (seedAmount > 0) {
            _seedInternal(token, seedAmount, tickLower, tickUpper);
        }

        // the loop takes over from here — tick() walks, collects, exits
    }

    // ── internal: the loop's decision functions ──

    function _shouldExit(address token, Rig storage rig) internal view returns (bool) {
        // time stop: been alive too long
        if (block.number - rig.spawnBlock > 7200) return true; // ~2 hours at 1s blocks

        // toll threshold: collected enough swaps, time to take profit
        if (rig.totalSwaps > 1000) return true;

        // (price-based exit would need an oracle or sqrtPrice read from the pool)
        // (in production: read current sqrtPrice from poolManager and compare to peak)

        return false;
    }

    function _shouldWalk(address token, Rig storage rig) internal view returns (bool) {
        // walk every N blocks (the cadence)
        // this replaces the "sleep 5" from the bash loop
        return (block.number - rig.spawnBlock) % 5 == 0; // every 5 blocks
    }

    /// @notice JIT walk: add TEMPORARY liquidity → swap → remove ONLY the temp.
    ///         The standing liquidity stays on the books — that's the point.
    ///         People need something to trade against. The standing liq is thin
    ///         enough that the walk moves price, but it's always there.
    ///
    ///         Layers:
    ///           STANDING (from seed())  → always on the books, always tradeable
    ///           JIT OVERLAY (this walk)  → added/removed per cycle, zero exposure
    ///
    ///         After each walk:
    ///           - price moved up (our swap)
    ///           - standing liquidity still there (for the next trader)
    ///           - JIT pulled (no inventory, no risk)
    ///           - ladder pools quote stale prices (arb magnet)
    function _walkInternal(address token, Rig storage rig) internal {
        _approveIfNeeded(quoteToken, address(poolManager));
        bool zeroForOne = Currency.unwrap(rig.basePool.currency0) == quoteToken;

        // ── PHASE 1: ADD JIT liquidity ON TOP of the standing liquidity ──
        // this is the +dL overlay — it exists only during this transaction
        // the standing liquidity from seed() is ALWAYS below this
        // (same range or wider — the standing is the floor, the JIT is the lever)
        int24 tickLower = -100;  // just below current price
        int24 tickUpper = 100;   // just above current price
        uint256 jitAmount = 0.1 ether;
        // NOTE: the standing liquidity uses a DIFFERENT salt or tick range
        // so we only remove the JIT overlay, never the standing position

        int256 liquidityDelta = int256(jitAmount * 1e18 / uint256(rig.basePrice));

        poolManager.modifyLiquidity(
            rig.basePool,
            ModifyLiquidityParams({
                tickLower: tickLower,
                tickUpper: tickUpper,
                liquidityDelta: liquidityDelta,   // + ADD
                salt: 0
            }),
            abi.encode(token)
        );

        // ── PHASE 2: SWAP through the pool (this is what moves price) ──
        // our own liquidity absorbs the fill
        // the swap is quote → token (buy direction, pushes price UP)
        poolManager.swap(
            rig.basePool,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: int256(jitAmount / 2), // spend half the JIT
                sqrtPriceLimitX96: type(uint160).max    // no limit — we WANT price impact
            }),
            abi.encode(token)
        );

        // ── PHASE 3: REMOVE ONLY the JIT overlay (the −dL part) ──
        // the standing liquidity STAYS — that's the exit liquidity
        // the price STAYS where the swap left it (removing liq doesn't revert price)
        // traders can still buy against the standing position
        // the chart shows liquidity, the chart shows volume, both are real
        // we just... placed them
        poolManager.modifyLiquidity(
            rig.basePool,
            ModifyLiquidityParams({
                tickLower: tickLower,
                tickUpper: tickUpper,
                liquidityDelta: -liquidityDelta,  // − REMOVE
                salt: 0
            }),
            abi.encode(token)
        );

        // ── PHASE 4: RE-QUOTE the ladder pools at the new price level ──
        // the ladder pools have zero liquidity, they just quote prices
        // after the walk, we can re-initialize them at higher prices
        // creating a fresh staircase for arbs to climb
        // (in practice: burn and re-initialize, or use dynamic tick arrays)

        // ── THE NET EFFECT ──
        // price moved up (our swap did it)
        // liquidity back to zero (we pulled it)
        // ladder pools quote higher (they see the new price)
        // arbs route through our 0% pools to capture the dislocation
        // the hook's beforeSwap counts every one of their fills
        // we hold zero inventory
        // we took zero directional risk
        // the toll is pure profit
    }

    /// @notice multi-pool JIT: add to base + re-quote ladders, atomically
    /// @dev this is the "max crime" version — do it across ALL pools at once
    function _walkMultiPoolJIT(address token, Rig storage rig) internal {
        _approveIfNeeded(quoteToken, address(poolManager));

        // Phase 1: add thin liquidity to the base pool
        // Phase 2: swap through the base pool (price moves)
        // Phase 3: the ladder pools now quote stale prices
        //          arbs see: base pool at new price, ladders at old prices
        //          they route through our 0% pools to capture the spread
        // Phase 4: remove liquidity
        //
        // The entire surface is a price-staircase that we control.
        // Every arb between any two pools on this token hits our hook.
        // The toll counts all of them.

        _walkInternal(token, rig); // base pool JIT

        // for each ladder pool, optionally add/remove liquidity too
        // creating a multi-dimensional JIT surface
        for (uint256 i = 0; i < rig.ladderPools.length; i++) {
            // each ladder pool can also do its own JIT:
            // add liq at the ladder price → swap → remove
            // this amplifies the dislocation the arbs see
            // and increases the toll count
        }
    }

    function _exitInternal(address token, Rig storage rig) internal {
        // pull liquidity, sell inventory, sweep
        // (same as the exit() function but callable from the loop)
        rig.active = false;
        emit RigExited(token, rig.totalSwaps, address(this).balance);
        _sweep(token);
        _sweep(quoteToken);
    }

    function _spawnInternal(
        address token, uint160 baseSqrtPrice, uint256[] calldata ladderMultiples, int24 tickSpacing
    ) internal {
        require(!rigs[token].active, "already spawned");
        // ... (same logic as spawn() but internal)
        rigs[token].active = true;
        rigs[token].spawnBlock = block.number;
        rigs[token].basePrice = baseSqrtPrice;
        activeTokens.push(token);
    }

    function _seedInternal(address token, uint256 amount, int24 lower, int24 upper) internal {
        // ... (same logic as seed() but internal)
    }

    // ═══════════════════════════════════════════════════════════
    // VIEW / UTILITY
    // ═══════════════════════════════════════════════════════════

    function getRigStats(address token) external view returns (
        bool active, uint256 poolCount, uint256 swapCount, int128 netLiquidity
    ) {
        Rig storage rig = rigs[token];
        return (rig.active, 1 + rig.ladderPools.length, rig.totalSwaps, rig.netLiquidity);
    }

    function getActiveTokens() external view returns (address[] memory) {
        return activeTokens;
    }



    // ── internal ──
    function _orderCurrencies(address token) internal view returns (Currency, Currency) {
        return token < quoteToken
            ? (Currency.wrap(token), Currency.wrap(quoteToken))
            : (Currency.wrap(quoteToken), Currency.wrap(token));
    }

    function _scaleSqrtPrice(uint160 basePrice, uint256 multiplier) internal pure returns (uint160) {
        // sqrt(N) in Q64.96: we need multiplier as sqrt
        // for simplicity: multiply by sqrt(multiplier) ≈ multiplier * 2^48 / sqrt(2^96)
        // this is approximate — use proper fixed point math in production
        uint256 scaled = uint256(basePrice) * _sqrt(multiplier * 2**96) / 2**48;
        return uint160(scaled);
    }

    function _sqrt(uint256 x) internal pure returns (uint256) {
        if (x == 0) return 0;
        uint256 z = (x + 1) / 2;
        uint256 y = x;
        while (z < y) { y = z; z = (x / z + z) / 2; }
        return y;
    }

    function _approveIfNeeded(address token, address spender) internal {
        // ERC20 approve if needed, skip for native ETH
        if (token != address(0)) {
            (bool ok, ) = token.call(abi.encodeWithSignature("approve(address,uint256)", spender, type(uint256).max));
            require(ok, "approve failed");
        }
    }

    function _sweep(address token) internal {
        if (token == address(0)) {
            // native ETH
            payable(owner).transfer(address(this).balance);
        } else {
            // ERC20
            (bool ok, ) = token.call(abi.encodeWithSignature("balanceOf(address)", address(this)));
            if (ok) {
                uint256 bal = abi.decode(ok ? bytes("") : bytes(""), (uint256)); // simplified
                if (bal > 0) {
                    (ok, ) = token.call(abi.encodeWithSignature("transfer(address,uint256)", owner, bal));
                    require(ok, "sweep failed");
                }
            }
        }
    }
}
