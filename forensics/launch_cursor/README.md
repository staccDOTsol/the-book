# Hookless Pons X/Q pool prototype

This directory contains a **compileable, un-deployed prototype**. `Q` is a
custom, fixed-supply ERC-20 intended for the underlying Pools.xyz
LiquidityLauncher path. A separate Uniswap v4 pool pairs each eligible Pons
launch token `X` with `Q`. These X/Q pools have **no hook**. Their static fee
is chosen for each *new pool*, from 5% to 50%, using prior completed outcomes.
The existing Pools.xyz Q/ETH launch pool keeps its own fixed fee.
Build with the Solidity optimizer: with Solc 0.8.26 and 200 optimizer runs,
Q's runtime is 17,293 bytes; the unoptimized runtime exceeds the EVM
24,576-byte contract-size limit.

## Contracts and actual flow

1. [LaunchCursorToken.sol](LaunchCursorToken.sol) supplies Q and the LIFO
   scheduler. An owner-controlled log watcher calls `enqueue(X)` after a
   Pons `TokenLaunched` event; Q verifies the factory record. Enqueueing does
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
   and a trusted, executable X/Q configuration. It has **no price oracle**.
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

The scheduler prioritizes ready exits, then configured LIFO entries, then
LP-fee harvests. An eligible direct EOA Q transfer with enough gas attempts
one action; `processNext()` is permissionless when transfers are quiet.
Contract/v4 settlement transfers are excluded to avoid nested PoolManager
operations. Each caller pays the gas for its attempt. A retry backoff avoids
one failed open/exit blocking later launches. The owner can skip a stale
queued launch, and the automatic transfer step can be disabled.

## Bootstrap and trust boundary

Deploy the executor first with the Robinhood PoolManager, PositionManager,
StateView and Permit2 addresses. Deploy the inspector pointing at it. Deploy
Q pointing at that executor and inspector, with exactly 1 billion units at 18
decimals if the intended Pools.xyz Instant Launch route is used. Then call
`executor.bindController(Q)` from its deployer and `inspector.bindCursor(Q)`
from any account. This reciprocal binding resolves the constructor cycle.
It does **not** launch Q. The underlying LiquidityLauncher supports an
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
prototype does not enforce a price-oracle deviation bound. Another account
can initialize a selected PoolKey first, causing this executor's open to
fail; the owner can then skip that launch.

## Incomplete execution paths

`executor.previewHarvest` and `executor.exit` deliberately revert
`OutcomeUnavailable`. They require executable ETH valuation of fees, gas
estimation, X liquidation through the active Pons curve or a graduated pool,
Q burning, WETH wizard-fanout transfer, developer ETH payout, and net-return
accounting. `withdrawPosition` only demonstrates mechanical LP removal; Q
does not expose a call path for it, so **do not fund this prototype with live
assets**. There is no live position, trade, launch, or realized profit from
this code. It has not been run on a Robinhood fork or audited. A complete
exit adapter and escape path must be built and tested before deployment.

An exit is triggered by a one-sided boundary **after** range entry. There is
no timer exit. Fee collection before exit is optional and only makes sense
when early conversion/distribution value exceeds its *incremental* gas. The
full LP burn at exit would collect accrued fees anyway. An eventual preview
must value claimable X and Q at executable ETH prices and estimate all claim
and conversion gas; the cursor currently requires gross fee value above
twice its configured gas-price ceiling times estimated units.
