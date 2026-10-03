# Automated Q position exit keeper

`pons_exit_keeper.py` reads confirmed Q `StepSucceeded(Open/Exit)` and
`LaunchAborted` logs into its own durable active-position cursor. It polls
`executor.inspect(X)` for each active position. It submits a permissionless
`PositionInspector.poke(X)` only when observed entry or a one-sided boundary
needs reporting to Q; Q's `enteredBandAt` and `exitReady` suppress repeat
pokes. A reported exit remains pending across restarts and phase-1 Pons
graduation waits.

If an owner abort leaves a stale Q exit heap root, the keeper may submit a
bounded `Q.processNext()` solely to clear it. It first verifies that
`Q.nextAction()` selects that Exit and that the token is Exited or Aborted at
the confirmed cursor block. An unconfirmed latest-only close or an active
exit without a configuration does not trigger this cleanup. Pending signed
cleanup transactions retain that purpose across restart and are rebroadcast
only while the same confirmed stale condition holds.

At a live one-sided boundary, the planner calls
`executor.withdrawPosition(X, 1-or-0, 1-or-0, deadline)` with `from: Q` via
`eth_call`. This simulates the actual PositionManager burn, without changing
chain state. It adds `executor.harvestedAmounts(X)` to those new X/Q receipts.
For any X to settle, it obtains a size-aware X→ETH quote from the settlement
router's active sale adapter in phase 0 or the official Robinhood v4 Quoter in
phase 2. It quotes ETH→Q on **half the conservative minimum ETH proceeds**,
matching the router's half-ETH buy, and applies a second output haircut.
It simulates the proposed LP minima again before configuration. All these
reads use one recent block tag, with the block hash checked before and after.

The live signer is restricted to `PositionInspector.poke(address)`,
`Q.configureExit(address,(uint128,uint128,uint256,uint256,uint64))`, and
permissionless `Q.processNext()`. It checks Q/executor/inspector/router,
priceConfigurator, guard, Quoter, and factory bindings at startup. It stores
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
`PONS_PRICE_CONFIGURATOR_PRIVATE_KEY` only for `--live`; read-only mode does
not read it. First run with inclusive `--start-block` at or before Q's first
open event, preferably Q deployment:

```sh
.venv/bin/python forensics/launch_cursor/pons_exit_keeper.py --start-block BLOCK --once
```

Later read-only cycles omit `--start-block`. After configuring the signer and
RPC runtime, `--live --once` performs one bounded cycle. Live mode requires
`--once`; the supervisor below provides continuous serial operation.
`--no-process-next` leaves Q scheduler calls to another
worker. Inspect `--help` for confirmation depth, poll interval, TTL, LP/swap
haircuts, gas caps, and fee caps. The default v4 Quoter is
`0x8dc178efb8111bb0973dd9d722ebeff267c98f94`.

The price and exit keepers use Q's **same** priceConfigurator nonce. Their
live process lock is shared per Q in `.local`, so run their `--once --live`
cycles serially. A pending signed transaction in one
keeper's state prevents the other keeper from signing after a crash until
the first recovers it. The first invocation's `--start-block` must cover all
opens; a later start cannot reconstruct a position opened before that cursor.

The opt-in supervisor starts the owner watcher as an independent websocket
process and runs one exit cycle then one price cycle, serially. It gives a
pending signed configurator transaction priority on recovery. Supply the same
environment variables above plus `PONS_WS_RPC_URL` and `PONS_OWNER_PRIVATE_KEY`.
On first use, supply inclusive block cursors at or before the first relevant
event for each component (usually launch factory activity and Q deployment):

```sh
.venv/bin/python forensics/launch_cursor/pons_keeper_supervisor.py --live \
  --watcher-start-block FACTORY_BLOCK \
  --price-start-block FACTORY_BLOCK \
  --exit-start-block Q_DEPLOY_BLOCK
```

On restarts, saved `.local` cursors are reused; omit the block flags. Child
cycles have a 600-second timeout and bounded retry/backoff. Repeated failures
stop the supervisor; a replaced or ambiguous signer nonce requires external
reconciliation because automatically guessing a replacement transaction is
unsafe. The owner watcher has a separate signer and can run concurrently.

## Limits

Polling a hookless pool observes the current state. A transient range touch
and return between polls can be missed; this keeper cannot guarantee an exit
at the first touch. At the time it observes an eligible boundary, Q and the
executor recheck state during the eventual exit. Phase 1 with X receipts,
unavailable depth,
expired minima, reorgs, and changed receipts are retried or stopped as
indicated in status. A swap can still move between quote and execution, so
the router enforces positive `minEthOut` and `minQOut` onchain.

Offline tests:

```sh
.venv/bin/python -m unittest -v forensics/launch_cursor/test_pons_exit_keeper.py
```
