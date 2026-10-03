# Canonical fee-outcome collector

`pons_fee_reporter.py` follows confirmed Q `FeeOutcomePending` events from
the Q deployment block with a durable `.local` cursor. It authenticates each
log against its successful transaction receipt and the canonical block hash.
The scan is bounded by block and log limits and stops on a reorg, malformed
receipt, or log overflow. Each still-pending exit gets an observation; the
onchain outcome remains pending after a temporary RPC failure. No private key is read and
no transaction is sent.

Run one bounded cycle after deploying and funding Q:

```sh
.venv/bin/python forensics/launch_cursor/pons_fee_reporter.py \
  --http-url "$PONS_HTTP_RPC_URL" \
  --q "$PONS_Q_ADDRESS" \
  --buyer "$PONS_VAULT_BUY_ADAPTER_ADDRESS" \
  --buyer-code-hash "$PONS_VAULT_BUY_CODE_HASH" \
  --start-block "$PONS_Q_DEPLOYMENT_BLOCK" --once
```

The initial block must be the Q deployment block: the collector checks that
Q had no code in the preceding block so earlier Q inflows cannot be omitted.
Subsequent invocations reuse the saved cursor and omit `--start-block`. The
buyer code hash is the keccak256 of its **deployed runtime bytecode** from a
trusted deployment receipt or verified build. This pins the separate
owner-source buyer deployed by `PonsBootstrap.deployVaultBuyer()`, not the
router's buyer that burns Q during exits. The collector also verifies the
buyer's source is Q's owner, its quote token is Q, its pool manager is the
canonical Robinhood v4 manager, and its declared pool ID is the zero-hook
Q/ETH key. One owner transaction must call `buyQ`, pay exactly the event's ETH,
emit one `QuoteBought` to the executor, and transfer exactly its Q output to
the vault. Any mint, gift, owner transfer, or otherwise unexplained Q inflow
is an unpriced FIFO lot. A position that consumes one cannot receive a cost
score.

For an exit, the collector checks the unique `FeeSelected` and
`PositionOpened`, traces Q transfers into and out of the vault up to opening,
and reconciles same-receipt `PositionSettled`, `ExitSettled`, Q burn, and
`FeeOutcomePending`. It reports actual recipient ETH/WETH, actual ETH paid for
traceable entry Q, known transaction gas receipts, and the complete X/Q pool
`Swap` count and gross X/Q inputs and outputs through exit. Uniswap v4's
`Swap.amount0/amount1` are **pool balance deltas**, so positive amounts are
pool inputs. The pool has no hook and the swap fee must match its selected
static fee. [PoolManager source](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol),
[IPoolManager event ABI](https://github.com/Uniswap/v4-core/blob/main/src/interfaces/IPoolManager.sol).

Evidence collection looks back to Q's deployment block to account for all
vault Q inflows. It is capped at 100,000 blocks by default, with a configurable
maximum of 500,000. Once an exit exceeds that span, the collector returns an
evidence-unavailable reason rather than an incomplete cost basis. A confirmed
seven-day expiry still appears as `censor_due` even if evidence collection is
unavailable; this is an observation only and sends no censor transaction.
Long-lived Q accounting will require a durable, authenticated rolling cost-lot
index before fee outcomes can be reported automatically.

## Why it does not yet report a fee score

The `knownReceipts` list is a lower bound. The contracts do not emit enough
information to prove that it includes every transaction whose gas should be
charged to this position. Reverted `processNext` or `poke` calls have no
position event, and a shared Q transfer can process work for one launch while
serving another purpose. The owner purchase gas also needs deterministic
allocation when one Q lot funds several positions. Until a complete signer
transaction audit and allocation policy are implemented and tested, every
row has `gasComplete: false`, `reportable: false`, and no `netReturnBps` or
`evidenceHash`. The collector does **not** call `reportExitedOutcome` or
`censorExpiredOutcome`. It marks a seven-day expiry as `censor_due` for a
separate serial configurator keeper to act on. The price configurator can
defer fee feedback without blocking an exit; no report is required to finish
settlement.

The remaining work for automatic onchain feedback is a complete confirmed
transaction scan for the owner/configurator and all permitted cranks through
each exit, exact gas attribution for reverted/shared calls, deterministic
purchase-gas allocation across FIFO cost lots, a content-addressed evidence
archive, and a narrowly scoped reporter signer with the existing configurator
lock and durable pending-transaction recovery. The signer must recheck
`outcomeDeadline`, exited stage, and selected fee status immediately before
signing, then confirm the report or censor receipt. A launched Q and actual
vault buys are needed to test the route with real receipts. Synthetic unit
tests cover authentic buys, unknown inflows, settlement mismatch, reorgs,
query overflow, and the non-reporting gate.
