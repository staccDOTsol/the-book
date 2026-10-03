# Hookless Pons X/Q pool prototype

This directory contains a **compileable, un-deployed prototype**. `Q` is a
custom, fixed-supply ERC-20 intended for the underlying Pools.xyz
LiquidityLauncher path. A separate Uniswap v4 pool pairs each eligible Pons
launch token `X` with `Q`. These X/Q pools have **no hook**. Their static fee
is chosen for each *new pool*, from 5% to 50%, using prior completed outcomes.
The existing Pools.xyz Q/ETH launch pool keeps its own fixed fee.
Build with the Solidity optimizer: Solc 0.8.26 at 200 runs produces an
18,640-byte Q runtime; the unoptimized runtime exceeds the EVM
24,576-byte contract-size limit. Remeasure optimized size after edits.

## Contracts and actual flow

1. [LaunchCursorToken.sol](LaunchCursorToken.sol) supplies Q and the LIFO
   scheduler. An owner-controlled log watcher calls `enqueue(X)` after a
   Pons `TokenLaunched` event; Q verifies the factory record in phase 0–2,
   including a fast graduation before the watcher submits it. Enqueueing does
   **not** authorize an LP mint. The launch waits until a separate automated
   price keeper calls `configureOpen(X, config)` through Q. This configures
   bounded Q spend, short deadline, starting v4 price, liquidity, and ticks,
   then arms the entry. An owner can assign a separate `priceConfigurator`;
   the launch watcher itself can remain enqueue-only.
2. [StaticNextPoolFee.sol](StaticNextPoolFee.sol) samples one fee per X. It
   explores 46 integer-percent arms (5–50%) on 15% of assignments; otherwise
   it selects the highest mean completed net return. A failed open retries
   with the **same** fee. There is no dynamic fee and no v4 hook. Censored
   launches are counted separately; the current selector does not correct
   survivorship or time-of-day bias.
3. [HooklessLPExecutor.sol](HooklessLPExecutor.sol) can atomically initialize
   a new static-fee, zero-hook X/Q pool and mint one **Q-only** v4 position.
   It checks the minted NFT, LP liquidity, token balance delta, pool price,
   fee, and spend cap. It owns that NFT. It can inspect current price,
   mark range entry, collect LP fees, and mechanically withdraw at a verified
   one-sided boundary. It requires valuable Q already deposited in the vault
   and a trusted, executable X/Q configuration. Opening is disabled until a
   matching price guard is bound.
4. [PositionInspector.sol](PositionInspector.sol) is permissionless. `poke(X)`
   reads the executor's current tick, records range entry, and reports the
   first **observed** all-Q or all-X boundary to Q. A complete jump from the
   initial Q-only side to the X-only side can record entry and exit readiness
   in the same observation. A boundary touched and reversed before any poke
   can be missed. A keeper must still call `poke` for active positions; Q
   transfers cannot identify all v4 pools or read their history.
   `pokeHarvest(X)` can report a fee claim only after the executor supplies a
   nonzero executable fee-value preview; the current executor deliberately
   reverts from that preview, so harvests stay disabled.
5. [OpenPriceGuard.sol](OpenPriceGuard.sol) checks the proposed starting
   X/Q price against the **current marginal spot** from Pons X/ETH and the
   actual Q/ETH launch pool, in the same transaction as mint. It accepts
   active curve phase 0 and graduated v4 phase 2; swept phase 1 waits. Its
   bound is capped at 10%, requires in-range Q/ETH liquidity, and cannot measure route depth, taxes, or price
   impact. It is a sanity check, not an executable-price oracle.

The scheduler prioritizes ready exits, then configured LIFO entries, then
LP-fee harvests. An eligible direct EOA Q transfer with enough gas attempts
one action; `processNext()` is permissionless when transfers are quiet.
Contract/v4 settlement transfers are excluded to avoid nested PoolManager
operations. Each caller pays the gas for its attempt. A retry backoff avoids
one failed open/exit blocking later launches. The owner can skip a stale
queued launch, and the automatic transfer step can be disabled. An explicit
owner `emergencyAbort(X)` burns an active LP at any price and sends the
recovered X and Q to the owner. `rescueHeldERC20` returns unused vault Q and
previously collected assets. These are recovery paths outside the normal
burn/fanout payout policy.

## Bootstrap and trust boundary

Deploy the executor first with the Robinhood PoolManager, PositionManager,
StateView and Permit2 addresses. Deploy the inspector pointing at it. Deploy
Q pointing at that executor and inspector, with exactly 1 billion units at 18
decimals if the intended Pools.xyz Instant Launch route is used. Then call
`executor.bindController(Q)` from its deployer and `inspector.bindCursor(Q)`
from any account. This reciprocal binding resolves the constructor cycle.
After Q/ETH is launched, deploy `OpenPriceGuard` with Q's **actual** Q/ETH
fee and tick spacing, then have the executor deployer call
`executor.bindPriceGuard(guard)`. This opens the mint path only after the
guard's Q, StateView, and Pons factory links match the executor. These steps
do **not** launch Q. The underlying LiquidityLauncher supports an
existing custom token via atomic `depositToken` + `distributeToken`, whereas
the ordinary Pools.xyz UI does not expose this route. The launcher locks the
whole initial Q supply in the Q/ETH position; the executor needs to acquire
Q separately for X/Q pools.
The Q constructor enforces exactly 1 billion 18-decimal units for this route.

The price keeper is trusted. It must filter unsupported Pons pair assets,
price X and Q through **executable** ETH routes, validate current reserves and
fees, select the intended quote-only tick band, set a short deadline and
bounded Q amount, and revalidate before submitting. An untrusted spot or
arbitrary initial X/Q price can donate the vault's Q to arbitrageurs. The
same-transaction guard limits deviation from **spot**, but still does not
enforce executable prices, size-aware curve quotes, or a TWAP. Another account
can initialize a selected PoolKey first, causing this executor's open to
fail; the owner can then skip that launch.

## Incomplete execution paths

`executor.previewHarvest` and `executor.exit` deliberately revert
`OutcomeUnavailable`. They require executable ETH valuation of fees, gas
estimation, X liquidation through the active Pons curve or a graduated pool,
Q burning, WETH wizard-fanout transfer, developer ETH payout, and net-return
accounting. `withdrawPosition` only demonstrates mechanical LP removal; Q
does not expose that *normal-exit* path. `emergencyAbort` is reachable and
was tested on a Robinhood fork, but it requires the owner to intervene and
does not execute the requested liquidation, burn, or payouts. **Do not fund
this prototype with live assets.** There is no live position, trade, launch,
or realized profit from this code. The active-curve and graduated-v4 sale
adapters are standalone components; normal exit, Q conversion and burn,
recipient payouts, and outcome accounting must be integrated and tested
before deployment.

An exit is triggered by a one-sided boundary **after** range entry. There is
no timer exit. Fee collection before exit is optional and only makes sense
when early conversion/distribution value exceeds its *incremental* gas. The
full LP burn at exit would collect accrued fees anyway. An eventual preview
must value claimable X and Q at executable ETH prices and estimate all claim
and conversion gas; the cursor currently requires gross fee value above
twice its configured gas-price ceiling times estimated units.

## Verification

Solc 0.8.26 with optimization and 200 runs compiled the contracts. Five
local integration tests passed. A read-only Robinhood RPC fork test also
initialized real Uniswap v4 zero-hook pools, minted Q-only positions, then
burned the LPs and recovered Q through the owner emergency path using the
deployed PositionManager in **both** token-address orderings.
The fork used a mock Pons factory and mock price guard because Q has not been
launched; it proves the v4 mint path, not the strategy's market prices, fees,
exit, or profitability. No fork test broadcasts a transaction.

Separate read-only Robinhood fork tests passed for a real graduated Pons
X/ETH v4 sale, a real active Pons X/ETH curve sale, and the graduated
cross-price guard with a synthetic Q/ETH price. Both sale adapters are
standalone: the executor does not yet call them. While no closed outcomes
exist, `StaticNextPoolFee` explores every fee assignment; it cannot learn a
best fee until complete exits report comparable net returns.
