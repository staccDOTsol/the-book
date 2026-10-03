# Hookless Pons X/Q prototype

This directory contains an **undeployed** custom `Q` ERC-20 and a launch cursor
for ETH-paired Pons tokens `X`. Q can be launched through Uniswap's
existing-token LiquidityLauncher route into a separate, locked Q/ETH pool.
For each eligible Pons X, the cursor creates **one hookless Uniswap v4 X/Q
pool with three Q-only LP positions**. One static pool fee is selected from
5% through 50% and remains fixed for that X. No Q, executor, inspector, or
router from this plan has been deployed, and no live transaction was sent
while building this prototype.

## Opening a pool

1. [pons_launch_watcher.py](pons_launch_watcher.py) indexes confirmed Pons V2
   `TokenLaunched` logs. Its only contract write is owner-signed
   `Q.enqueue(X)`. Q verifies the factory record and places the launch on its
   LIFO entry stack.
2. [pons_price_keeper.py](pons_price_keeper.py) authenticates the ETH-paired
   factory record and curve, reads Q and X supply at one pinned block, and
   sends a short-lived `Q.configureOpen(X, bytes)` plan. It uses the curve's
   phantom quote reserve and graduation threshold to set three nested Q/X
   ranges. See [the exact formula and tick math](price_keeper.md).
3. [LaunchCursorToken.sol](LaunchCursorToken.sol) selects one of 46 static fee
   arms (5%, 6%, …, 50%) and, in one atomic open attempt, mints three new Q
   tranches. Each mint is **0.1% of Q's then-current total supply**, so the
   amounts compound sequentially. It passes the three amounts to
   [HooklessLPExecutor.sol](HooklessLPExecutor.sol), which initializes one
   zero-hook X/Q pool and mints three Q-only NFTs. It burns unused newly
   minted Q before the call completes. A failed open rolls the issuance back.
   A hard ceiling of ten times the initial Q supply rejects an opening that
   lacks room for all three mints; it remains retryable after later burns.
4. A direct EOA Q transfer with sufficient gas may trigger one cursor step.
   `Q.processNext()` is the permissionless fallback. The caller pays that
   transaction's gas; internal settlement transfers do not recursively run
   the cursor. An unconfigured or failed launch remains retryable.

The price scale is deliberately separate from the position size:
`p0 = (1% of current Q supply) / X totalSupply` in raw token units. Reducing
the actual mint to 0.1% therefore makes each position smaller without moving
the existing bands tenfold. The Pons curve contributes the dimensionless multiplier
`R = ((phantom + graduationThreshold) / phantom)²`. The three bands span
`p0 → R·p0`, `p0 → 2R·p0`, and `p0 → 10R·p0`. The new pool starts beyond
the widest band so all three NFTs initially hold Q only, for either token
address ordering. **p0 is not a conversion of the Pons ETH price into Q,
a fair-market-value estimate, or evidence of profit.** Opening has no
X/ETH or Q/ETH executable-price gate. The executor no longer binds an
`OpenPriceGuard` or `OpenExecutableDepthGuard`; those contracts do not veto
pool creation. The executor still rejects invalid ranges, an initialized
PoolKey, an expired plan, or a tranche spend cap above its actual new mint.
A third party could initialize the chosen PoolKey before the executor.

The ceiling prevents arithmetic exhaustion, but it also limits throughput:
from 1 billion Q, three 0.1% mints leave about 2.85 million new Q outstanding
per open if 95% of each mint enters the LP. With no later burns, 42 opens
would raise supply about 12.7%, and the tenfold ceiling would stop new opens
after roughly 808. These are conditional calculations, not a launch forecast.
The 120-minute winddown burns Q that is still in an untouched position; Q
traded away to an X seller requires value recovered on exit to offset it.

The [fee and band-edge arbitrage scenario](fee_arb_scenario.py) is **under
test, not implemented in the strategy**. It explores conditional continuous
LP math and can reconcile caller-supplied same-block route quotes. It does
not authenticate those quotes, choose live trade sizes, place an arbitrage
trade, or change the keeper's three fixed band formulas. A 5–50% fee alone
does not establish profitable entry or prevent arbitrage against a
mispriced X/Q pool.
The [pinned Robinhood route counterfactual](point_one_percent_route_fork.md)
tests a locally launched Q/ETH pool, a Pons X buy, X/Q and Q/ETH swaps, and
three timed exits. Its one sampled rational route gives developer wallet cash
below strategy-paid gas; an untouched pool has zero payout and still costs
gas. It is one fork state, not an expected-return estimate.

## Observation, exits, and interim fees

[PositionInspector.sol](PositionInspector.sol) reads all three live NFTs.
It records a tranche's entry when the price is observed inside its range or
has crossed completely from the opening Q-only side to the all-X side. Once
entered, the first **observed** one-sided boundary can queue an exit. A
hookless observer can miss a touch and reversal between inspections; it
cannot promise the first intrablock touch. At 120 minutes after opening,
`Q.requestWindDown(X)` can queue a timed exit even for an untouched Q-only
position. The cursor requeues timed exits until all three tranches close.

[pons_exit_keeper.py](pons_exit_keeper.py) tracks open NFTs separately. It
simulates the selected tranche's real PositionManager withdrawal with
`eth_call`, quotes a size-aware X→ETH sale through the active Pons curve or
graduated v4 pool, then quotes the ETH→Q purchase. It signs a bounded
`Q.configureExit(X, bytes)` and drives the inspector and scheduler.
`executor.exit` burns **one** entered tranche and settles its receipts plus
previously harvested balances. A swept Pons phase waits when X must be sold.
The other NFTs remain active and can later exit at their own observed
boundaries or the 120-minute winddown. Only the **third and final** exit marks X fully Exited and opens
fee-outcome feedback. An all-Q exit can burn Q without an X sale or ETH
payout. See [exit keeper details](exit_keeper.md).

[ExitSettlementRouter.sol](ExitSettlementRouter.sol) sells recovered X for
ETH, spends half that ETH buying Q in the Q/ETH pool, and burns bought Q plus
Q recovered from the LP. It wraps half the remaining ETH as WETH for the
Squarefun wizard fanout and sends the other half as native ETH to the
configured developer. Positive output minima and a short deadline bound the
swaps. The router is mandatory for normal exits; the executor has **no
price-guard or depth-guard binding**. A separate `OpenPriceGuard` can still
supply the offchain exit and harvest keepers with Q/ETH quote-pool metadata.
The onchain router checks its real Q/ETH buy path when bound.

Interim fee harvesting is implemented. [pons_harvest_keeper.py](pons_harvest_keeper.py)
simulates fee collection across active NFTs, quotes sale or burn value,
requires a margin over whole-cycle gas, and signs `Q.configureHarvest` plus
an inspector report. `executor.harvest` collects accrued X/Q fees without
reducing LP principal. It sells X fees when bounded, burns Q fees, and keeps
unsellable X reserved for a later harvest or exit. Q-only burns have an
estimated ETH value but produce no ETH reimbursement to the signer. Exits
have scheduler priority over harvests. See [harvest keeper details](harvest_keeper.md).

## Fee feedback and operators

[StaticNextPoolFee.sol](StaticNextPoolFee.sol) freezes the selected fee per X
across open retries. It explores 15% of new selections and otherwise uses
its recorded arm scores. It is **not calibrated to profitability**. The
[receipt collector](pons_fee_reporter.md) and
[feedback keeper](pons_fee_feedback_keeper.py) reconcile one pool's three
minted-Q tranches, three independent exits, interim harvests, burns,
recipient ETH/WETH, and observed X/Q swaps. After the final exit, the keeper
can report one gross ETH-equivalent mark backed by canonical receipts and
historical full-size Q/ETH quotes. That mark excludes complete gas
attribution and includes hypothetical Q sales; it is not realized profit.
An unvalued final exit remains reportable for seven days, then can be
censored. Open, idle, and censored arms retain the policy's provisional
−5,000 bps selection penalty. See the [outcome ledger](fee_outcome_ledger.md).

The owner watcher, price/feedback keeper, and exit/harvest keeper use **three
distinct signing accounts and nonce streams**. Feedback shares the price
signer lane; harvest shares the exit signer lane. Each lane has a private
`.local` journal and lock for exact signed-transaction recovery.
[pons_keeper_supervisor.py](pons_keeper_supervisor.py) runs bounded cycles,
prioritizes exits, and reports all four keepers. It does not remove the need
to inspect onchain deployment, recipient addresses, or gas funding.

## Bootstrap and local status

The [staged Robinhood bootstrap](bootstrap.md) pins the current Uniswap
LiquidityLauncher existing-token route and checks the canonical v4, Pons,
Permit2, and launch-strategy addresses. Its launch preflight returns calldata
for atomic `depositToken(Q)` plus `distributeToken(Q)` in one launcher
multicall. The locked Q/ETH launch holds Q's initial 1-billion-token supply.
A bounded owner ETH→Q buy may be needed to bring that pool into active
liquidity for later cashouts, but **executor Q bought from the market is not
   the source of X/Q opening inventory**. The three new 0.1% mints fund that
inventory. Post-launch deployment binds the settlement router, its recipient
and child adapters, and distinct configurator roles; it does not bind spot
or executable-depth guards to the executor. Nothing in bootstrap broadcasts
a transaction without a separately reviewed external signer action.

The GET-only [local status page](q_status_dashboard.py) serves
`http://127.0.0.1:8767`. It filters watcher, price, exit, and supervisor
journals and never reads keys, starts keepers, calls an RPC, or submits a
transaction. A recent supervisor heartbeat and successful price/exit cycles
can earn a **local** `live_reported` label. It checks Q and chain agreement,
expects the new price journal's guard to be zero, and allows the separate
exit journal's nonzero quote guard. It does not independently verify
contract deployment or onchain liveness.

## Verification and limits

With Solc 0.8.35, via-IR, and optimizer runs 1, the executor runtime is
**24,031 bytes** (545 below EIP-170) and Q's runtime is **23,351 bytes**.
Robinhood fork tests exercise a three-position mint and abort, and a
three-tranche full cycle through real v4 LP mint/burn and Pons/router
settlement with synthetic Q/ETH liquidity. A separate fork test exercises
the 120-minute winddown of three untouched Q-only NFTs.
In the full-cycle fork test, the three `Q.processNext()` exits used
**967,420**, **928,079**, and **964,981** gas respectively. There is no
current isolated measured gas figure for the three-position open; do not use
an older one-position gas result for it. These tests establish transaction
mechanics, not Q demand, fair X/Q pricing, fee income, or a positive
cash return. No live transaction was broadcast.

For historical launch cadence and earlier strategy assumptions, see
[the strategy record](../record/160-pons-every-launch-pool-plan.md); its
one-position and purchased-Q discussion predates this three-mint design.
