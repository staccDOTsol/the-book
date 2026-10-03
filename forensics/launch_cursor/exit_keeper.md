# Automated Q position exit keeper

`pons_exit_keeper.py` reads confirmed Q `StepSucceeded(Open)`,
`AllPositionsExited`, and `LaunchAborted` logs into its durable active-pool
cursor. A `StepSucceeded(Exit)` closes one of three LP NFTs; only
`AllPositionsExited` removes X from the active cursor. It polls
`executor.inspectTranche(X, index)` for each live tranche and calls
`PositionInspector.poke(X)` when a tranche enters its band or reaches a
one-sided exit boundary. A reported exit remains pending across restarts,
retry delays, and phase-1 Pons graduation waits. At
`Q.launches(X).activeAt + 120 minutes`, the keeper calls permissionless
`Q.requestWindDown(X)` if no exit is queued, even if all positions remain
Q-only and untouched. Before signing, it checks that an `eth_call` of the
request returns `queued=true`; after confirmation, it reads Q's `exitReady`
state instead of assuming the receipt queued an exit. Q automatically
requeues after each successful partial
timed exit until all three NFTs are closed. Interim fee claims use the same
signer lane; see [harvest_keeper.md](harvest_keeper.md).

If an owner abort leaves a stale Q exit heap root, the keeper may submit a
bounded `Q.processNext()` solely to clear it. It first verifies that
`Q.nextAction()` selects that Exit and that the token is Exited or Aborted at
the confirmed cursor block. An unconfirmed latest-only close or an active
exit without a configuration does not trigger this cleanup. Pending signed
cleanup transactions retain that purpose across restart and are rebroadcast
only while the same confirmed stale condition holds.

Before the 120-minute deadline, a live one-sided boundary is required. The
planner selects the first eligible active tranche and calls
`executor.withdrawTranche(X, index, 1-or-0, 1-or-0, deadline)` with `from: Q`
via `eth_call`. At or after the deadline, it selects the first active tranche,
including an untouched Q-only or interior NFT, and calls
`executor.simulateTimedWithdraw(X, index, 0, 0, deadline)` from Q. It sets
`ExitConfig.timed=true` even if that NFT is also at a boundary. Both routes
simulate that NFT's actual PositionManager burn without changing chain state.
The keeper waits if the timed simulator is unavailable; it never substitutes
estimated receipts. It adds
`executor.harvestedAmounts(X)` to the new X/Q receipts; previously harvested
amounts settle with the next exited tranche.
For any X to settle, it obtains a size-aware X→ETH quote from the settlement
router's active sale adapter in phase 0 or the official Robinhood v4 Quoter in
phase 2. It quotes ETH→Q on **half the conservative minimum ETH proceeds**,
matching the router's half-ETH buy, and applies a second output haircut.
It simulates the proposed LP minima again with the same withdrawal route
before configuration. A timed interior burn can require positive minima for
both X and Q; a Q-only burn needs no X sale or ETH→Q swap. All planner reads
use one recent block tag, with the block hash checked before and after.

The live signer is restricted to `PositionInspector.poke(address)`,
`Q.requestWindDown(address)`,
`Q.configureExit(address,bytes)` containing the canonical
`(uint8 tranche,uint128 minTokenOut,uint128 minQuoteOut,uint256 minEthOut,uint256 minQOut,uint64 deadline,bool timed)` tuple, and
permissionless `Q.processNext()`. It checks Q/executor/inspector/router,
the distinct `exitConfigurator`, guard, Quoter, and factory bindings at startup. It stores
the signed raw transaction and hash in `.local/pons-exit-keeper.json` before
broadcast, and validates them before recovery or rebroadcast. Gas and fee
caps apply to each selector. A stale unknown transaction or ambiguous consumed
nonce stops for review. No live transaction was sent while building it.

## Runtime

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r forensics/requirements-cranker.txt
```

Set `PONS_HTTP_RPC_URL`, `PONS_CHAIN_ID`, `PONS_Q_ADDRESS`, and
`PONS_PRICE_GUARD_ADDRESS` in the process environment. Supply
`PONS_EXIT_CONFIGURATOR_PRIVATE_KEY` only for `--live`; read-only mode does
not read it. First run with inclusive `--start-block` at or before Q's first
open event, preferably Q deployment:

```sh
.venv/bin/python forensics/launch_cursor/pons_exit_keeper.py --start-block BLOCK --once
```

Later read-only cycles omit `--start-block`. After configuring the signer and
RPC runtime, `--live --once` performs one bounded cycle. Live mode requires
`--once`; the supervisor below provides continuous independent operation.
`--no-process-next` leaves Q scheduler execution to another worker; timed
wind-down requests and exit configuration remain enabled. Inspect `--help`
for confirmation depth, poll interval, TTL, LP/swap haircuts, selector gas
caps (including `--max-wind-down-gas`), and fee caps. The default v4 Quoter is
`0x8dc178efb8111bb0973dd9d722ebeff267c98f94`.

The owner watcher, price keeper, and exit/harvest keeper have **three distinct**
signing accounts and nonce streams. Fund the exit account independently for
gas. `Q.exitConfigurator` configures both exits and interim harvests; those
keepers share the same `.local/pons-exit-keeper.json` journal and configurator
lock, separate from the price keeper's. Each recovers the other's pending
signed transaction before it signs another. The first invocation's
`--start-block` must cover all opens; a later start cannot reconstruct an
earlier pool.

The opt-in supervisor starts the owner watcher as an independent websocket
process, runs price and feedback separately, and serializes exit then
harvest in the shared exit signer lane. Supply the same environment
variables above plus `PONS_WS_RPC_URL`, `PONS_OWNER_PRIVATE_KEY`, and
`PONS_PRICE_CONFIGURATOR_PRIVATE_KEY`. The supervisor rejects duplicate
signer addresses before launching any child.
On first use, supply inclusive block cursors at or before the first relevant
event for each component (usually launch factory activity and Q deployment):

```sh
.venv/bin/python forensics/launch_cursor/pons_keeper_supervisor.py --live \
  --watcher-start-block FACTORY_BLOCK \
  --price-start-block FACTORY_BLOCK \
  --exit-start-block Q_DEPLOY_BLOCK
```

On restarts, saved `.local` cursors are reused; omit the block flags. The
exit and harvest children cannot sign concurrently or pass a pending nonce
to one another. A replaced or ambiguous signer nonce requires external
reconciliation. The owner watcher has a separate signer and can run concurrently.

## Limits

Polling a hookless pool observes the current state. A transient range touch
and return between polls can be missed; this keeper cannot guarantee an exit
at the first touch. The 120-minute route avoids relying on a boundary touch,
but execution still depends on real LP receipts and executable settlement.
Q and the executor recheck timing, active position, minima, and deadline at
execution. Phase 1 with X receipts, unavailable quotes, failed timed
simulation, expired minima, reorgs, and changed receipts remain pending or
stop as indicated in status. A swap can still move between quote and
execution, so the router enforces positive `minEthOut` and `minQOut` onchain.

Offline tests:

```sh
.venv/bin/python -m unittest -v forensics/launch_cursor/test_pons_exit_keeper.py
```
