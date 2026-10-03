# Automated Pons Q-only price keeper

`pons_price_keeper.py` is the offchain companion to the owner launch watcher.
It scans confirmed Pons `TokenLaunched` logs into its own durable `.local`
pending-token queue, waits for `Q.launches(token).stage == Queued`, and polls
the deployed `OpenPriceGuard` and executable swap quotes. Newer launches are
considered first, matching Q's LIFO entry order. Tokens that have not yet
been enqueued remain in the queue across restarts. Phase 1 and empty Q/ETH
liquidity remain pending; non-ETH Pons pairs are removed as unsupported.

All factory, guard, StateView, curve, vault, and Quoter planning reads are
pinned to one recent block. The block hash is checked before and after
planning, and a snapshot more than 20 seconds old is discarded. The
initialization price is the guard's **marginal spot**
`referenceSqrtPriceX96(token)`, checked with `guard.validate`. A separate
size-aware X→ETH→Q sale quote determines the maximum Q/X price the LP band
may pay when it begins buying X. Phase 0 uses Pons's integer-exact reserve,
base fee, and creator tax sale formula. Phase 2 uses Robinhood's official v4
Quoter for X→ETH. Both use the Quoter for ETH→Q. The first quote size defaults
to twice the X amount implied by the budget at spot. The keeper then derives
the **maximum X inventory of the entire planned band** from its liquidity and
ticks, requotes that full amount, and widens or skips until the discounted
full-inventory sale rate supports the band. The executable Q proceeds receive
a 15% safety haircut. For either token address ordering, the keeper checks
that the starting price is Q-only and that the band's first purchase price
cannot exceed that discounted full-inventory rate. A thin route may have no
acceptable band; it is skipped.

The default position budget is at most **25%** of idle Q in the executor,
where idle Q is `Q.balanceOf(executor) -
executor.reservedHarvestedQuote()`. If the reserve exceeds the balance, the
keeper stops. Liquidity is chosen with v4's rounded-up amount formulas to
spend at most 95% of that budget; `maxQuoteIn` is the full 25% cap. The
position uses a configurable tick spacing, 1,200-tick width, and 60-tick
gap by default. The initial configuration expires after 120 seconds.

## Runtime

From the repository root, use the project-local Python environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r forensics/requirements-cranker.txt
```

The pinned manifest includes `eth_abi==6.0.0`, `eth-account`, `eth-utils`,
`rlp`, and `websockets`. Supply these process environment variables:

| Variable | Purpose |
| --- | --- |
| `PONS_HTTP_RPC_URL` | Robinhood HTTP RPC with archive logs, `eth_call`, and transaction support |
| `PONS_CHAIN_ID` | Expected chain ID; mismatch stops startup |
| `PONS_Q_ADDRESS` | Deployed hookless Q address |
| `PONS_PRICE_GUARD_ADDRESS` | Guard bound to Q's executor |
| `PONS_PRICE_CONFIGURATOR_PRIVATE_KEY` | Separate `Q.priceConfigurator` signing key; read only with `--live` from the process environment |

The first invocation needs inclusive `--start-block BLOCK` at or before the
launches to cover. A read-only one-cycle preview is:

```sh
.venv/bin/python forensics/launch_cursor/pons_price_keeper.py --start-block BLOCK --once
```

It may update only its local discovery queue. It does not read the signer key
or send a transaction. After the owner has deliberately configured the
runtime, continuous automated configuration and the permissionless Q step
are enabled with:

```sh
.venv/bin/python forensics/launch_cursor/pons_price_keeper.py --live --once
```

On later starts omit `--start-block`. Use `--no-process-next` to have this
keeper configure positions while another keeper triggers Q's scheduler.
Live mode requires `--once`; use `pons_keeper_supervisor.py` for continuous
serial price and exit cycles with the independent owner watcher. Gas and fee caps, confirmation
depth, poll interval, Q budget fraction, haircut, quote sample multiplier,
tick spacing, band width, gap, snapshot age, and deadline are CLI options; inspect `--help`
before live use. The Robinhood v4 Quoter defaults to
`0x8dc178efb8111bb0973dd9d722ebeff267c98f94` and its code is checked
at startup. The Q/executor/guard/factory, `priceConfigurator`, and bound
executable depth guard bindings are checked before any live transaction. The
depth guard's source, spot guard, settlement router, Q, Pons factory, v4
Quoter, and haircut must match the planner. The executor requotes full-band
exposure in the open transaction.

The state file and lock are inside ignored `.local`, never `/tmp`. The file
is mode `0600` and atomically replaced with fsync. It contains the
confirmed-block cursor, discovered token queue, and any pending signed raw
transaction. The only transactions the keeper can sign are
`Q.configureOpen(token, config)` and permissionless `Q.processNext()`.
On restart, it validates the saved signature, chain, nonce, target, selector,
payload, gas and fee caps before rebroadcasting an unknown transaction. A
stale unknown configuration or an ambiguous consumed nonce stops for
operator review. A confirmed reverted configuration leaves the token pending
for a fresh plan. A committed-block reorg stops the keeper for reconciliation.
The price and exit keepers share one configurator signer and a per-Q `.local`
lock. Run their `--once --live` cycles serially; simultaneous continuous live
processes fail closed. The lock records which state file owns a pending signed
transaction after a crash, so another keeper cannot take its nonce.

## Limits

This is an automated **planner** with a same-transaction executable depth
guard. The quoted depth is current state, not a guarantee about future exits.
The v4 Quoter and Pons reserve quote represent current state and size; both
can change before the separate `configureOpen` and `processNext` transactions.
The executor repeats the spot bound and full-band X→ETH→Q quote at mint. The
default haircut and Q-only gap remain risk controls, not a proof of profit or
fair value after later market movement.
Quoter failure, phase 1, absent Q/ETH liquidity, an impossible band, or
insufficient idle Q produce no configuration.

The separate exit keeper inspects active LPs and configures bounded exits;
this component does not. The repository's hookless strategy remains a
prototype; no live transaction was sent while building or testing this keeper.

Offline tests:

```sh
.venv/bin/python -m unittest -v forensics/launch_cursor/test_pons_price_keeper.py
```
