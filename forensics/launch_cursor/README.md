# Hookless Pons X/Q prototype

This directory contains an **undeployed** fixed-supply `Q` ERC-20, a LIFO
launch cursor, and a vault for independent Uniswap v4 `X/Q` pools. `X` is a
Pons launch token. Each new pool has no hook and receives one fixed fee chosen
from 5–50%; the existing Pools.xyz `Q/ETH` launch pool has separate terms.
The current code opens **one Q-only LP position** per eligible launch, then
withdraws at the first *observed* one-sided boundary after range entry.

## Components and flow

1. [pons_launch_watcher.py](pons_launch_watcher.py) consumes confirmed factory
   `TokenLaunched` logs. WebSocket notices wake it, while HTTP log backfill
   and a durable cursor cover disconnections. Its only contract write is the
   owner-signed `LaunchCursorToken.enqueue(X)`, which verifies the factory
   record and puts `X` on a LIFO stack.
2. [pons_price_keeper.py](pons_price_keeper.py) checks a recent pinned-block
   executable X-to-ETH route, the Q/ETH route, Q budget, tick ordering, and
   the bound onchain guards before signing `Q.configureOpen(X, config)`. Its
   default budget is 25% of **idle vault Q**, after reserved harvested Q.
   An unconfigured launch remains queued.
3. [LaunchCursorToken.sol](LaunchCursorToken.sol) schedules configured opens,
   ready exits, and optional fee harvests. An eligible direct EOA `Q`
   transfer can attempt one stage when it supplies enough gas; anyone can
   call `processNext()` if transfers are quiet. The transaction caller pays
   gas. `Q` excludes internal settlement transfers from recursive cursor
   work. [StaticNextPoolFee.sol](StaticNextPoolFee.sol) fixes one fee per `X`
   across retries. Its 46 fee arms are **not calibrated**: until comparable
   net outcomes exist, assignments remain exploratory.
4. [HooklessLPExecutor.sol](HooklessLPExecutor.sol) atomically initializes an
   empty zero-hook pool and mints one Q-only position. It binds both
   [OpenPriceGuard.sol](OpenPriceGuard.sol), a same-transaction marginal spot
   sanity check, and [OpenExecutableDepthGuard.sol](OpenExecutableDepthGuard.sol),
   a same-transaction check of the full LP band’s maximum possible X
   inventory against executable Pons X/ETH and Q/ETH routes. The selected
   pool fee never changes after creation. Both guards must be bound before
   opening. A stale quote, insufficient depth, wrong phase, bad tick band,
   or an already initialized PoolKey rejects the open.
5. [PositionInspector.sol](PositionInspector.sol) reports range entry and
   one-sided boundary observations. [pons_exit_keeper.py](pons_exit_keeper.py)
   tracks open positions, checks `inspect` offchain before paying for `poke`,
   simulates the LP withdrawal, obtains size-aware sale and Q-buy bounds, and
   submits `Q.configureExit` followed by `Q.processNext`. A swept Pons
   phase waits and retries if recovered `X` needs to be sold; an all-Q exit
   can settle without that sale. Polling can miss a transient touch and reversal:
   the enforceable trigger is the **first observed** boundary, with no timer.
6. `executor.exit` burns the active LP, includes prior collected fees, and
   calls [ExitSettlementRouter.sol](ExitSettlementRouter.sol). The router
   sells recovered `X` through the active Pons curve or graduated v4 pool,
   spends half of that ETH buying `Q` through Q/ETH, burns bought and
   recovered `Q`, wraps half the remaining ETH for the Squarefun wizard
   fanout, and sends the other half as native ETH to the developer. Positive
   minimum outputs and a short deadline bound the swaps. An all-Q exit burns
   Q without a sale or payout.

The watcher, price keeper, and exit keeper have separate durable signed
transaction journals. The two keepers use the same price-configurator signer;
[pons_keeper_supervisor.py](pons_keeper_supervisor.py) runs their live `--once`
cycles serially so their nonces cannot race, while the owner-signed watcher
runs independently. [exit_keeper.md](exit_keeper.md) documents startup and
recovery. No live daemon, deployment, Q launch, LP, or trade is running from
this prototype.

## Bootstrap and trust boundary

Deploy the executor with the Robinhood PoolManager, PositionManager,
StateView, and Permit2. Deploy the inspector, then deploy exactly 1 billion
18-decimal `Q` units with that executor and inspector. Bind Q as executor
controller and as inspector cursor. After the actual `Q/ETH` launch pool
exists, deploy the spot guard and settlement router with its real fee and
tick spacing, then deploy the executable depth guard using the official
Robinhood v4 Quoter. Bind the spot guard, settlement router, and depth guard
to the executor. The contracts check reciprocal identities; deployment
scripts still need to verify all addresses and the intended developer and
Squarefun fanout recipients before funding. These steps do **not** launch Q.

The underlying Pools.xyz LiquidityLauncher supports an existing custom token
through atomic `depositToken` plus `distributeToken`; the ordinary Pools.xyz
UI does not expose this route. Its launch locks the initial Q supply in the
Q/ETH position. The executor must acquire valuable Q separately for X/Q
pools. A transfer-triggered cursor does not make LP inventory or gas free.

The price configurator is trusted to select admissible ETH-paired Pons
launches and a fair Q-only band. The onchain depth guard checks liquidity
**at open**, but no spot/depth check guarantees future volume, fee income,
or recoverable ETH at exit. A third party can initialize a selected pool
first. Non-ETH Pons pairs are unsupported by these routes. Interim LP fee
claims are disabled because `previewHarvest` does not provide a reliable
executable ETH valuation; the final burn collects accrued fees.

Successful exits are currently reported to the fee selector as **censored**:
the contracts settle assets but do not know comparable ETH-valued net ROI,
including Q cost basis, burned Q value, and gas. The adaptive selector
therefore has no valid profitability feedback yet. Owner emergency unwind
and asset rescue remain recovery paths outside normal burn and payout policy.
Do not fund the prototype with live assets until deployment, exact recipient
binding, end-to-end execution, and outcome accounting are verified.

## Verification

Solc 0.8.26 with optimization and 200 runs produced a 24,048-byte executor
runtime (528 bytes below EIP-170), a 5,080-byte depth guard, and a
19,481-byte Q runtime. Recheck size after any change to the executor. Local
tests cover LIFO scheduling, guarded entry, boundary
readiness, settlement accounting, and failure paths. Read-only Robinhood
fork tests cover real v4 mint and burn in both token-address orderings,
active-curve and graduated Pons sales, Q/ETH buys, the integrated exit route,
and the same-transaction depth guard. [HooklessFullCycleFork.t.sol](HooklessFullCycleFork.t.sol)
combines a real X/Q LP mint, a swap through its range, a real NFT burn, and
the Pons sale/Q buy/burn/payout route. Its complete exit used 1,002,400 gas,
below the 3 million cursor cap. Synthetic Q/ETH liquidity is used because Q
has not been launched. Tests do not broadcast a transaction or establish
profitability.

For discovery cadence and the economic limits, see
[the strategy record](../record/160-pons-every-launch-pool-plan.md).
