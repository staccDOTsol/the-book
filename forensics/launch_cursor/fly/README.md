# Fly runtime for Q keeper

This image runs the corrected Q supervisor in one private Fly Machine.
`fly.toml` currently selects `trader-paid`, the live writer mode. It has no
HTTP service. The build context is
`forensics/launch_cursor`, whose `.dockerignore` excludes the repository's
private `.local` directory and every file outside the selected keeper code.

At the October 3, 2026 18:59 UTC check, `the-book-q-keeper` had one started
Machine with a volume in `yyz`. Its heartbeat reported `mode: live`,
`writesEnabled: true`, `dequeueMode: trader_transfer`, a running watcher, and
successful exit, harvest, price, and feedback worker cycles without a current
failure. The corrected Q at `0x623B5374c4CB838DA24EE9F48F08664337936a06`
had 10 confirmed `LaunchEnqueued` events. No dequeue had mined:
`successfulExecutorSteps()` and the `StepSucceeded` event count were both zero.
Queued work still depends on an eligible Q/USDG v3 transfer or a separately
authorized manual call. The [deployment record](../live-deployment.md) tracks
Q activation and X/Q launch state.
At 19:01 UTC the owner watcher paused on its 0.01 ETH reserve floor while the
owner held about 0.00658 ETH. The checked-in trader-paid runtime now uses a
0.001 ETH floor; that revision was deployed, and the watcher resumed
enqueuing. Use the live heartbeat and chain reads for current health.
Do not deploy this over `the-book-self-serve` or `delta-lp-keeper`.

## Standby rebuild

For a rebuild after losing the app or volume, first set
`PONS_KEEPER_MODE = "standby"` in `fly/fly.toml` and update
`fly/runtime-standby.json` to the current Q, guard, and inclusive start blocks.
The checked-in standby config and existing standby cursors belong to the old
Q. Then, from `forensics/launch_cursor`:

```sh
flyctl apps create the-book-q-keeper --org personal
flyctl volumes create keeper_data --app the-book-q-keeper --region yyz --size 1 --snapshot-retention 14 --yes
flyctl secrets import --app the-book-q-keeper --stage
flyctl deploy . --config fly/fly.toml --ha=false --strategy rolling
flyctl machines list --app the-book-q-keeper
flyctl ssh console --app the-book-q-keeper --command 'cat /app/.local/pons-keeper-supervisor-status.json'
```

At the `secrets import` step, supply `PONS_HTTP_RPC_URL` and
`PONS_WS_RPC_URL` through operator-controlled stdin. Keep values out of
command arguments, shell history, the repository, and this document. The
image uses the public chain ID, Q address, guard address, block cursors, fee
caps, and signer addresses from its checked-in runtime config. Confirm the
standby heartbeat reports `writesEnabled: false` and that exactly one Machine
is running. No private keys are needed for standby.

## Live writer migration and recovery

Only after the previous writer has been stopped and all child processes have
exited, copy its five current live journals to the Fly volume. Do not
copy raw private key files, the encrypted owner keystore, or the RPC env file.
The journal transfer must preserve mode `0600`; each journal can include a
pending signed transaction and must be treated as sensitive operational state.

From the repository root, use this loop only when the source `.local` contains
the latest journals for the same Q and guard as the Fly runtime:

```sh
for name in pons-launch-watcher.json pons-price-keeper.json pons-exit-keeper.json pons-fee-feedback.json pons-keeper-budget.json; do
  flyctl ssh sftp put --app the-book-q-keeper --mode 0600 ".local/$name" "/app/.local/$name"
done
```

Verify those five remote files, one Machine, signer balances, and the local
writer's stopped state. Then stage the three raw signer secrets through
operator-controlled stdin with `flyctl secrets import --app the-book-q-keeper
--stage`: `PONS_OWNER_PRIVATE_KEY`,
`PONS_PRICE_CONFIGURATOR_PRIVATE_KEY`, and
`PONS_EXIT_CONFIGURATOR_PRIVATE_KEY`. The entrypoint writes them as mode-0600
files under the Machine's memory-backed `/dev/shm` and removes the input secret
variables before starting the supervisor. It refuses live mode when any of
the five handed-off journals is missing or unsafe.

For a future keeper-paid writer migration, select `PONS_KEEPER_MODE = "live"`
in `fly/fly.toml` and review the diff. The current corrected-Q runtime uses
`trader-paid`. From `forensics/launch_cursor`, deploy the reviewed mode with:

```sh
flyctl deploy . --config fly/fly.toml --ha=false --strategy rolling
flyctl machines list --app the-book-q-keeper
flyctl ssh console --app the-book-q-keeper --command 'cat /app/.local/pons-keeper-supervisor-status.json'
```

The live heartbeat must say `mode: live`, `writesEnabled: true`, and show
recent successful child cycles before relying on the keeper. Check the live
onchain bindings and signer balances at cutover. One volume is one local
failure domain; preserve separate backups of the journals, especially the
spend budget, and avoid canary or bluegreen deploys for this writer.

## Trader-paid dequeue mode

The replacement Q design can use a canonical Q/USDG Uniswap v3 pool as its
transfer trigger while keeping Q/ETH and X/Q liquidity work on v4. The Fly
entrypoint accepts `PONS_KEEPER_MODE=trader-paid` and selects
`fly/runtime-trader-paid.json`. This is a live writer mode: the watcher may
enqueue, and the price, exit, and harvest keepers may configure and poke, but
their child commands include `--no-process-next`. The v3 buyer or seller pays
for the Q transfer that attempts the next queued step. Fee feedback remains
live. The supervisor heartbeat keeps `mode: live` and reports
`dequeueMode: trader_transfer`. The supervisor refuses to start a price,
exit, or harvest child if its journal still contains a pending keeper-signed
`processNext` transaction; reconcile that transaction before switching modes.

`runtime-trader-paid.json` pins the replacement Q at
`0x623B5374c4CB838DA24EE9F48F08664337936a06` and starts each fresh
cursor at its deployment block, **79,306,622**. Its Q/ETH launch completed at
block 79,307,686. The feedback journal requires the actual deployment block:
on first use it checks that Q had no code in the preceding block. The config
uses replacement price guard `0x9755b28b7f69b105599691e220da7a7f582faf07`.
Confirm that Q's owner, price configurator, and exit configurator equal the
three configured signer addresses,
and verify Q's funded canonical Q/USDG v3 pool binding, automatic flag, and
working v4 Q/ETH route.

The four old-Q live cursors could not be reused: each was bound to the old Q,
and the price journal contained a signed, unresolved old-Q `processNext()`.
For the completed replacement-Q cutover, four fresh, mode-0600 template
journals were staged locally with `lastBlock: 79306621` and no pending signed
transactions, then transferred under the runtime's normal names:

| Local source | Fly destination |
| --- | --- |
| `.local/pons-launch-watcher.v2.json` | `/app/.local/pons-launch-watcher.json` |
| `.local/pons-price-keeper.v2.json` | `/app/.local/pons-price-keeper.json` |
| `.local/pons-exit-keeper.v2.json` | `/app/.local/pons-exit-keeper.json` |
| `.local/pons-fee-feedback.v2.json` | `/app/.local/pons-fee-feedback.json` |
| `.local/pons-keeper-budget.json` | `/app/.local/pons-keeper-budget.json` |

The existing budget journal was retained so the same signers kept their
cumulative spend reservations. For future recovery, restore the **latest**
live journals and budget from a private backup, verify their Q/guard bindings,
pending transactions, and mode-0600 permissions, and run only one writer.
The initial `.v2.json` templates are not current recovery cursors after live
progress. Copy only JSON files, not local lock files. The old standby cursors
remain bound to the old Q. The generic live-journal copy loop above would
copy the wrong Q cursors if run against the old local `.local` directory.

No scheduled keeper in `trader-paid` mode sends `Q.processNext()`; if v3
trading stops, queued actions wait until another eligible transfer or a
separately authorized manual call.
