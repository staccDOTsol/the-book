# X/Q fee outcome ledger

`fee_outcome_ledger.py` provides local reconciliation of fee assignments.
Schema version 2 is for the current three-tranche, newly minted Q design.
It accepts an archived evidence record from
[`pons_fee_feedback_keeper.py`](pons_fee_reporter.md) for each scored exit and
keeps unvalued exits, active positions, and idle positions in the arm table.
It recomputes the same gross ETH-equivalent mark score and checks that quotes
cover the whole Q amount at the blocks before opening, each of the three
tranche exits, and every interim harvest.

The version 2 score is:

```
(recipient ETH/WETH + executable Q→ETH quote for all Q burned
 - executable Q→ETH quote for newly minted Q actually deposited)
 / entry Q→ETH quote
```

It excludes gas that cannot be fully attributed from receipts. The score
uses hypothetical full-size Q sales, so it is **an estimate, not realized
profit**. The local ledger does not authenticate RPC data; use the live
feedback keeper for canonical receipt and quote checks. Its unsigned draft
calls are for review, not authorization to broadcast.

Example version 2 input shape:

```json
{
  "schema_version": 2,
  "chain_id": 4663,
  "as_of_block": 123456,
  "cursor_address": "0x5555555555555555555555555555555555555555",
  "observations": [
    {
      "token": "0x1111111111111111111111111111111111111111",
      "fee_pips": 100000,
      "stage": "exited",
  "evidence": {"...": "exact archived feedback evidence JSON"}
    },
    {
      "token": "0x2222222222222222222222222222222222222222",
      "fee_pips": 100000,
      "stage": "active",
      "observed_swap_count": 0
    }
  ]
}
```

The first record's `evidence` must contain the complete JSON archived under
`.local/pons-fee-evidence/`, including the three-tranche mint and settlement
observation, pinned executable quotes for entry and every burn, and
recomputable score. For an
unvalued exit, omit `evidence`; the ledger counts it as censored. For an
active pool, include a complete observed swap count when available so the
ledger can distinguish idle from merely open. Every unscored assignment
contributes the selector's provisional −5,000 bps **selection penalty**, not
an observed loss. The arm table is exploratory and has no causal fee claim.

Run:

```sh
.venv/bin/python forensics/launch_cursor/fee_outcome_ledger.py evidence.json \
  --output fee-report.json --draft-report-calls
```

Schema version 1 remains available only to reproduce the earlier purchased-Q
counterfactual accounting. Its historical Q acquisition-cost return is not
the metric used by the newly minted Q fee feedback keeper or selector.
