# Bounded Q keeper runtime

The active writer serves corrected Q at
`0x623B5374c4CB838DA24EE9F48F08664337936a06` on Fly app
`the-book-q-keeper`. It runs in **trader-paid** mode: the watcher and
configuration keepers may sign bounded writes, while eligible Q/USDG v3
trades pay for dequeue steps. See the [live deployment record](live-deployment.md)
for onchain identifiers and the latest recorded snapshot, and the
[Fly runtime guide](fly/README.md) for the Machine and journal handoff.

At the **2026-10-03 18:59 UTC** check, Fly reported `mode: live`,
`writesEnabled: true`, and `dequeueMode: trader_transfer`; no trader-paid
executor step had mined. These are observations at that check, not a live
health guarantee. Inspect the current Fly heartbeat and Q state before
relying on them.

## Runtime modes

`pons_keeper_supervisor.py --live --trader-paid` keeps the owner watcher and
configuration keepers live but passes `--no-process-next` to the price, exit,
and harvest children. They can still enqueue, configure, poke, and request
timed wind-downs. Fee feedback runs independently. When a ready action
exists, an eligible Q transfer through the bound v3 pool must complete one
`Q.processTransferStep()` or the trade reverts. If v3 trading stops, queued
work waits for another eligible transfer or a separately authorized manual
call. Q/ETH v4 trades do not trigger this path.

`--live` without `--trader-paid` is the keeper-paid mode and can send
`processNext()` from a keeper signer. It is not the active Fly mode. Price
and exit signer lanes remain serial within each signer; independent lanes
can run together. The supervisor refuses trader-paid startup when a child
journal contains an unresolved keeper-signed `processNext()` transaction.

The earlier local `--standby` runtime belonged to the previous Q. It ran the
five child checks without signing, used separate `*.standby.json` cursors,
and reported `mode: standby`, `writesEnabled: false`. Its heartbeat did not
prove that signed cycles were running. The local live writer was stopped
before the corrected-Q Fly cutover; the old standby cursors are not recovery
cursors for corrected Q. See [Fly runtime](fly/README.md#trader-paid-dequeue-mode)
for the current journals and restart procedure.

## Signers and spend limits

Live mode uses three distinct raw hex signer keys with their exact expected
public addresses and explicit daily, total, per-transaction, and
minimum-balance limits. Locally, key files must be mode 0600; on Fly, the
entrypoint writes secrets to mode-0600 files in memory-backed `/dev/shm` and
removes the input secret variables before starting the supervisor. Keys never
appear in process arguments, status, or logs. A key file contains `0x`
followed by 64 hex digits or exactly 64 hex digits. The local RPC env file
remains `.local/pons-rpc.env`.

All signed writes pass through `pons_launch_watcher.HttpRpc`, including price,
exit, fee feedback, and harvest writes. The transport reserves **gas limit ×
maxFeePerGas + value** in `.local/pons-keeper-budget.json` before forwarding a
transaction. It checks chain 4663, exact signer, per-transaction maximum,
daily and lifetime cumulative maximum, and starting- and live-balance
floors. The journal and its lock are mode 0600. A rebroadcast of the same
signed transaction reuses the reservation. When a cap would block a new write,
the runtime reconciles receipts with at least 12 confirmations against the
canonical block and mined transaction, then charges actual gas cost instead
of the maximum reservation. A reverted mined transaction still pays its actual
gas cost; unknown or unconfirmed transactions retain their full reservations.
Review the journal before any manual reset. Lowering caps takes effect at the
next send. This guard covers supervised keeper processes, not other wallet
software using the same keys.

The checked-in [trader-paid configuration](fly/runtime-trader-paid.json) uses
these limits in wei:

| Signer | Daily | Total | Per transaction | Balance floor |
|---|---:|---:|---:|---:|
| Owner | `1000000000000000` | `10000000000000000` | `100000000000000` | `1000000000000000` |
| Price | `800000000000000` | `1500000000000000` | `500000000000000` | `500000000000000` |
| Exit | `800000000000000` | `1500000000000000` | `500000000000000` | `500000000000000` |

Its fee caps are `--max-fee-gwei 0.1` and `--max-priority-gwei 0.01`.
These are limits, not spend forecasts. A process that needs more than a cap
stops instead of sending a transaction. The price and exit transaction caps
also bound keeper-paid `processNext()` if an operator deliberately switches
to that mode; the current trader-paid schedule does not send it.

The owner floor was lowered from 0.01 ETH to 0.001 ETH after the 0.01 ETH
setting stopped enqueues despite a roughly 0.00658 ETH live balance. The
daily, lifetime, per-transaction, and max-fee caps remain unchanged.

## Historical local launchd path

The local launchd starter remains available for a separately reviewed local
runtime. It is not the active corrected-Q writer. It reads a mode-0600
`.local/pons-keeper-runtime.json` with `schemaVersion: 1`, an `arguments`
array containing complete supervisor flags without raw keys, and validated
public `qAddress` and `priceGuardAddress` fields. The supervisor loads RPC
settings from `.local/pons-rpc.env`. Preparing the plist writes
`.local/pons-keeper.launchd.plist` without installing or starting it:

```sh
.venv/bin/python forensics/launch_cursor/pons_keeper_launchd.py prepare
```

The plist runs `pons_keeper_launchd.py run`, which validates the private
config and execs the supervisor. Any future local use needs current
corrected-Q addresses, cursors, journals, one-writer coordination with Fly,
and an operator review before installing or loading the plist. The plist
restarts after a nonzero exit; the supervisor restarts failed child cycles
with bounded backoff.
