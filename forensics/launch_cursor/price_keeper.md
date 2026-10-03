# Deterministic Pons X/Q opening keeper

`pons_price_keeper.py` scans confirmed Pons `TokenLaunched` logs and configures a
new hookless Uniswap v4 X/Q pool for each queued ETH-paired Pons launch. It
proposes **three Q-only LP positions in the same pool**. The pool's static fee
is selected separately by Q's `StaticNextPoolFee` policy from 5% through 50%
in one-percentage-point steps. The keeper does not use X/ETH, Q/ETH, a price
oracle, or an executable sale quote to place the bands. The price after pool
creation is set by trades, not by this calculation.

## Deterministic price and funding

All amounts in the formula are ERC-20 atomic units. At one recent pinned
block, read `S = Q.totalSupply()` and `T = X.totalSupply()`. Q's open wrapper
mints three new 0.1%-of-then-current-supply tranches before calling the executor:

```text
m0 = floor(S / 1,000)
m1 = floor((S + m0) / 1,000)
m2 = floor((S + m0 + m1) / 1,000)
p0 = floor(S / 100) / T                 Q per X, at the start-equivalent bound
phantom = curve.getReserves().quoteReserve - curve.realQuoteReserve()
R = ((phantom + graduationThreshold) / phantom)^2
band 0: p0 → p0 × R
band 1: p0 → p0 × 2R
band 2: p0 → p0 × 10R
```

The Pons factory launch record supplies `graduationThreshold`; the keeper
checks it against the curve getter and checks that the curve reports the
factory, token, and native ETH pair from the same record. Pons's official
[curve documentation](https://docs.ponsfamily.com/v2#curve-reserves) and
[contract source](https://github.com/ponsdotdev/pons-labs/blob/main/contractsV2/src/v2/PonsV2BondingCurve.sol)
show that `getReserves().quoteReserve` includes the phantom amount while
`realQuoteReserve()` excludes it. The multiplier is the constant-product
curve's graduation/start marginal-price ratio. It is a *dimensionless shape*
for X/Q; `p0` supplies a deliberately self-defined Q scale. The 1% reference
in `p0` is **only a price-policy numerator**, not Q minted for a position.
Reducing issuance to 0.1% therefore makes positions thinner without moving
all three bands tenfold lower. `p0` does not convert Pons's starting ETH/X
price into Q, measure fair market value, or establish expected profit. The
calculator works in Pons phase 0 and phase 2;
phase 1 waits until the graduated pool is created.

For each band, `maxQuoteIn[i] = mi`, and integer v4 liquidity math chooses
positive liquidity consuming at most 95% of that tranche. The unused newly
minted Q is burned by the executor. The keeper derives ticks with exact
integer ratio comparisons and rounds each range outward to the configured
spacing. If Q is currency0, X/Q is the reciprocal of Q/X and the pool starts
one tick-spacing **below** the widest band's lower tick. If Q is currency1,
the pool starts one spacing **above** its upper tick. All three positions are
therefore Q-only at creation. A zero supply, zero phantom reserve, invalid
factory/curve binding, non-distinct rounded bands, out-of-range tick, or
tranche too small for positive liquidity produces no configuration. Before
configuring, the keeper also checks that **all three gross mints** fit beneath
Q's immutable total-supply ceiling. Unused Q is burned only after all three
mints, so planning against net post-burn issuance would create a reverting open.

Q computes `mi` again when it opens the pool. Supply can change between the
keeper's pinned read and execution. If a saved `maxQuoteIn[i]` exceeds the
actual new mint, or the deadline expires, the executor rejects the opening
and Q rolls that mint attempt back. The keeper can then issue a fresh plan.

## Runtime

From the repository root, use the project-local Python environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r forensics/requirements-cranker.txt
```

| Environment variable | Purpose |
| --- | --- |
| `PONS_HTTP_RPC_URL` | Robinhood RPC with logs and archive `eth_call` |
| `PONS_CHAIN_ID` | Must be `4663` |
| `PONS_Q_ADDRESS` | Deployed hookless Q |
| `PONS_PRICE_CONFIGURATOR_PRIVATE_KEY` | Separate Q price-configurator signer; read only with `--live` |

A first read-only discovery and plan pass uses an inclusive starting block:

```sh
.venv/bin/python forensics/launch_cursor/pons_price_keeper.py --start-block BLOCK --once
```

Later runs use the saved `.local/pons-price-keeper.json` cursor and omit
`--start-block`. The command prints the three planned ticks, liquidity amounts,
mint caps, supply, phantom reserve, threshold, and initial sqrt price. It can
update its local cursor, but does not read a signer key or send a transaction.
The journal is now bound to Q with a zero guard address; an older journal from
the guard-priced strategy will fail its binding check and needs a separate
state path or an operator-reviewed cursor migration.
After reviewing the deployed contracts, signer isolation, and a read-only plan,
a single signed cycle is:

```sh
.venv/bin/python forensics/launch_cursor/pons_price_keeper.py --live --once
```

Use `--no-process-next` if another process drives Q's scheduler. For repeated
serial cycles use `pons_keeper_supervisor.py`. `--utilization-bps`,
`--tick-spacing`, `--ttl-seconds`, snapshot age, work budgets, confirmation
depth, and gas/fee caps are available in `--help`. The default config deadline
is 600 seconds and the executor allows at most 15 minutes. The default
`processNext` gas cap is 10 million because one open now creates three LP
positions; the keeper still estimates gas and refuses an estimate over the
configured cap.

Startup requires the Robinhood chain ID, the Q→executor→settlement-router
bindings, the canonical v4 PoolManager/PositionManager/StateView, Q's 0.1%
mint rule and total-supply ceiling, and a Q-controlled static fee policy with
the expected 5%–50% range.
The executor creates its X/Q PoolKey with `hooks = address(0)` and the fee
selected by that policy. The opening planner does not require the old
`OpenPriceGuard`, `OpenExecutableDepthGuard`, a funded Q/ETH pool, or a Q
balance already sitting in the executor. The separate exit and harvest keepers
still use their guard/Quoter settings to bound cash settlement.

## Journal and scheduling

Confirmed factory logs enter a durable pending-token queue only when the
`TokenLaunched` event's `pairToken` is native ETH (`address(0)`). Non-ETH
launches are skipped before they can consume token checks or planning slots;
malformed event data stops discovery without advancing the cursor. Each cycle checks
at most 12 tokens and makes at most two full plans by default. Fresh launches
get a bounded priority lane, while a rotating background lane prevents older
ones from starving. A block-reorg or overfull single log block halts cursor
advancement. All planning reads are pinned to one block, its hash is checked
again afterward, and a snapshot older than 20 seconds is rejected.

The ignored `.local` state file is mode `0600`, atomically replaced with fsync,
and guarded by a local lock. It records the log cursor, scheduling state, the
last canonically confirmed full open plan for each pending token, and any
outstanding signed raw transaction. The signer may submit only
`Q.configureOpen(address,bytes)` or `Q.processNext()`. It checks calldata,
chain, nonce, target, value, gas, and fees before sending or recovering a raw
transaction. A pending configuration is decoded as the exact seven-field
three-array plan and compared with a fresh pinned calculation before an
unknown transaction is rebroadcast. A consumed nonce without a matching
receipt stops for review.

Solidity's autogenerated `openConfigs(address)` getter omits the three fixed
arrays. The keeper reuses a confirmed plan only while its non-deadline fields
match a fresh pinned calculation, more than 30 seconds remain, the getter's
visible fields match the journal, Q still reports the launch queued and
configured, and Q's latest `OpenPriceConfigured` event for the token carries
the journaled full-plan hash. A changed plan, overwritten event, expired plan,
reorg, or missing journal forces a new configuration. Confirmed pending
transactions are journaled during crash recovery as well. This avoids signing
the same bands every polling cycle without treating the incomplete getter as
proof of their contents.

## Scope and verification

The deterministic X/Q curve is a launch policy, not a fair-market-value or
exit-liquidity guarantee. There is no entry gate tied to the X/ETH or Q/ETH
market. A later X exit or Q cashout can have poor executable depth; those
risks are handled, to the extent possible, by the separate exit and harvest
routes. No live transaction was sent while building or testing this keeper.

```sh
.venv/bin/python -m unittest -v forensics/launch_cursor/test_pons_price_keeper.py
```
