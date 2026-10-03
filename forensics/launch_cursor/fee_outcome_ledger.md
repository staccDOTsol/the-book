# Fee-arm outcome ledger

`fee_outcome_ledger.py` reconciles one evidence JSON file into per-position and
per-fee-arm accounting. It reads local files only. It does not send a
transaction, authenticate RPC data, or call `StaticNextPoolFee.recordClosed`.
The current `HooklessLPExecutor.exit` returns `(0, false)`, so Q leaves each
successful exit pending for seven days. Its price configurator may submit
one receipt-backed cash return during that window or censor the outcome
after expiry. A [read-only receipt collector](pons_fee_reporter.md) now builds
partial canonical evidence but cannot prove complete strategy-paid gas, so
it does not submit fee scores. No authenticated reporting writer is running;
the onchain selector has no live performance calibration.

## Evidence to collect

Use confirmed receipts and logs at a stated `as_of_block`, with the canonical
chain checked for reorgs. Record **every selected fee**, including failed or
never opened attempts. For an open position, take `quoteSpent` and fee from
`PositionOpened`. For an exit, take `tokenSettled`, `quoteSettled`, `ethOut`,
and `quoteBurned` from `PositionSettled`; reconcile them against the same
transaction's router `ExitSettled` (`xSold`, `quoteBought`, `wizardWeth`, and
`developerEth`). These events give actual cash and token flows. They do not
give the ETH cost of Q used at open.

Allocate the Q spent at open to actual acquisition lots. `eth_paid_wei` is
the ETH paid for the allocated quantity, excluding gas. A zero-cost minted or
gifted lot still needs its provenance and **does not mean Q had zero market
value**. Put acquisition, enqueue, configuration, failed attempts, opens,
keeper calls, harvests, and exits in `gas_receipts` when attributable to the
position. For a shared transaction set `attributed_fee_wei` to the justified
allocation. The report rejects allocations totaling more than one receipt
fee and requires an `allocation_ref` for partial allocations, but only the
operator can attest that the list is complete. Count gas
paid by third-party Q-transfer callers as a system cost, while preserving
its payer in `gas_by_payer_eth_wei`. List the configured owner/configurator
signers in top-level `strategy_gas_payers`. The candidate cash objective
charges only their attributed gas. External callers' gas stays in the
system-cost reconciliation. The report also gives the sum of full receipt
fees for unique linked transactions, so attribution cannot hide network
cost. Shared receipts appear in more than one observation, so use the
top-level unique total rather than summing per-row full fees.

For an ETH-valued **estimate**, supply size-aware exact-input Q-to-ETH quotes
for the entire Q spent at open and the entire Q burned at exit. Use the
corresponding block and identify the route and query in `source_ref`; a
marginal spot rate multiplied by Q amount is not an executable quote. The
exit quote is hypothetical because the Q has already been burned. Also
measure the custom X/Q pool's full `Swap` log window through exit, reporting
input volume in X and Q separately and a zero-swap observation when known.
Open positions remain right-censored at the measurement horizon. The ledger
does not turn an idle position into a zero-return close.

The minimum schema is illustrated with **placeholder values**, not a trade:

```json
{
  "schema_version": 1,
  "chain_id": 4663,
  "as_of_block": 123456,
  "strategy_gas_payers": ["0x2222222222222222222222222222222222222222"],
  "observations": [
    {
      "token": "0x1111111111111111111111111111111111111111",
      "fee_pips": 50000,
      "selection_tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "stage": "active",
      "open": {
        "tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "block_number": 123450,
        "q_spent_wei": "1000000000000000000"
      },
      "q_cost_lots": [{
        "q_amount_wei": "1000000000000000000",
        "eth_paid_wei": "10000000000000000",
        "acquisition_ref": "confirmed purchase receipt and Q Transfer log"
      }],
      "gas_complete": false,
      "gas_receipts": [],
      "swap_volume": {
        "swap_count": 0,
        "x_in_wei": "0",
        "q_in_wei": "0",
        "through_block": 123456,
        "source_ref": "complete X/Q Swap log query over opening through as_of_block"
      }
    }
  ]
}
```

An exited observation additionally needs `exit` with `tx_hash`,
`block_number`, `x_settled_wei`, `q_settled_wei`, `x_sold_wei`, `eth_out_wei`,
`q_bought_wei`, `q_burned_wei`, `wizard_weth_wei`, and `developer_eth_wei`.
`entry_q_eth_quote` and `exit_burn_q_eth_quote` each need `q_amount_wei`,
`eth_out_wei`, `block_number`, `route: "Q/ETH"`, and `source_ref`. When gas is
complete, set `gas_complete: true` with a `gas_scope_ref`, and include each
attributed receipt's `tx_hash`, `payer`, `role`, `gas_used`,
`effective_gas_price_wei`, `extra_fee_wei`, and optional
`attributed_fee_wei` and `allocation_ref` when less than the full receipt fee.
The selected/open/exit transaction hashes must occur
in that list. Use decimal strings for wei, since ordinary JSON numbers can
lose precision in other tools.

Run:

```sh
python3 forensics/launch_cursor/fee_outcome_ledger.py evidence.json --output fee-report.json
```

For a **local unsigned draft** of `reportExitedOutcome` calldata, add
`--draft-report-calls` and optionally include `cursor_address` in the input.
The report then contains `unsigned_report_calls` only for exits eligible
for the cash-return score. Each draft uses a SHA-256 hash of canonical JSON
containing the observation and report context as its `evidenceHash`; archive
that exact evidence and verify it against confirmed chain receipts and Q
lots before a trusted price-configurator signer submits anything. The tool
does not check onchain `outcomeDeadline`, fee-policy status, canonical
receipts, or acquisition provenance. It does not sign, broadcast, or call
an RPC endpoint. A draft alone is insufficient authorization or proof.

`realized_recipient_cash_eth_wei` is the actual ETH plus WETH paid to the
developer and wizard. `recipient_cash_after_q_cost_and_strategy_gas_wei`
subtracts actual Q acquisition cost and gas paid by the configured strategy
signers. This is the arm objective's numerator, even when recipients and
funders are different wallets; it is not one wallet's profit.
`system_cash_after_q_cost_and_all_gas_wei` additionally charges outside
Q-transfer callers and other attributed external payers.
`q_recovered_and_burned_wei` and `q_bought_and_burned_wei` show how much Q
left supply. `system_cash_plus_burn_valuation_estimate_wei` adds a hypothetical
exit liquidation quote for *all* burned Q, then subtracts the executable
open Q value and all attributed gas. It is an estimate of aggregate value transferred,
not realized ETH or guaranteed benefit to Q holders. It cannot establish
strategy profitability without a real, liquid Q market and reliable quotes.

`cash_return_on_actual_q_cost_bps_clipped` is shown only when acquisition
lots establish a **positive actual ETH cost**, attributed gas is complete,
and complete X/Q swap evidence shows nonzero volume. Its denominator is the
actual ETH acquisition cost of the Q deployed to that LP. Entry/exit Q
market quotes are not part of this cash score. An untraceable or zero-cost
lot leaves the arm outcome censored; it does not become a free-capital win.
The arm table includes every assignment, ineligible exit, active position,
and idle observation. It sums known cash, Q burn, payer-split gas, and only
complete swap-volume windows; `eligible_cash_result_eth_wei` sums only exits
that pass the cash-feedback evidence gate. Its cash-return mean is descriptive: fee assignments are
not matched across launch conditions, exits are selected by path, and
right-censored positions still affect comparison. Do not submit these
values to Q without authenticating receipts, lot tracing, and payer
attribution. `reportExitedOutcome` trusts the price configurator's evidence
hash; it cannot independently prove those inputs.

## Proposed automatic collector and reporter

This is a design for a later implementation, not an enabled service. A
reorg-aware read-only indexer would follow confirmed `FeeSelected`,
`PositionOpened`, `PositionSettled`, `ExitSettled`, cursor step events, ERC-20
Q transfers, and the custom pool's `Swap` logs. It would retrieve the
receipts for all relevant owner/configurator, executor-triggering, and Q
acquisition transactions. A deterministic lot policy would trace Q bought
with actual ETH into the vault and allocate only those lots to
`PositionOpened.quoteSpent`. Unknown, minted, gifted, or zero-cost lots
would leave the cash-return arm result censored. The indexer would publish
a content-hashed evidence record and recompute the cash return after the
confirmation window, with outside Q-transfer callers' gas reported but
excluded from the strategy-paid numerator. A fixed evidence horizon would
finalize cases lacking a traceable basis as censored.

Q now exposes `reportExitedOutcome(token, clippedCashBps, evidenceHash)` only
to the price configurator for a completed, still-selected exit before its
seven-day `outcomeDeadline`. It calls `StaticNextPoolFee.recordClosed` once.
`censorExpiredOutcome(token)` can mark an unvalued exit `UnvaluedExit` at or
after the deadline. The existing price-configurator signer could run the
future reporter serially with the keepers, avoiding a new executor path.
That signer and the offchain cost-lot computation remain trusted: Solidity
cannot verify historical Q acquisition cost or all attributable gas from
the exit call. Already-censored positions cannot be reopened under the
current fee-policy ABI. This local ledger produces analysis, not a signed
or authenticated attestation, and no reporting transaction is sent by it.
