# Minted-Q fee feedback

`pons_fee_reporter.py` is a read-only, bounded collector. It authenticates
confirmed Q, executor, router, fee-policy, and custom-pool logs against their
canonical successful receipts. For each completed X/Q pool it reconciles:

- one selected static fee and three `PositionOpened` NFTs in one opening
  transaction and one zero-hook pool;
- Q's aggregate `OpenMinted`, three `OpenTrancheMinted` events, mint Transfers,
  and burns of unused newly minted Q;
- each of three independent `PositionSettled` plus router `ExitSettled`
  receipts, any interim `FeesSettled` receipts, and the Q
  `AllPositionsExited` marker on the third exit, including every Q burn and
  wizard/developer payout;
- the complete observed X/Q `Swap` log window from open through exit.

The collector reports the Q actually deposited, all Q burned during the
position's lifetime, all actual recipient ETH/WETH, observed custom-pool swap
counts and volume, and linked receipt gas. It never treats minted Q as a
zero-cost cash profit. It cannot prove full gas attribution for reverted or
shared cranks.

## Automatic gross mark feedback

`pons_fee_feedback_keeper.py` adds exact-input, full-size Q→ETH v4 Quoter
calls using archival `eth_call`: one at the completed block before the open
for all newly minted Q deposited, and one before every tranche exit or
interim harvest for the Q burned in that settlement. It pins and rechecks
each block hash. For position `i`, its **gross estimated** surplus is:

```
actual recipient ETH/WETH across all tranche exits and interim harvests
+ hypothetical executable ETH from selling all Q burned
- hypothetical executable ETH from selling newly minted Q deposited
```

The score is this surplus divided by the entry Q quote, in basis points,
clipped to ±10,000 for `reportExitedOutcome`. The unclipped amounts, quote
blocks and hashes, confirmed receipt references, linked gas receipts, and
limits are retained in canonical JSON under `.local/pons-fee-evidence/`.
Its SHA-256 digest is the onchain `evidenceHash`. The metric is explicitly
`gross_mark_to_market_eth_equivalent_excluding_gas`. **It is not realized
profit, net return, or proof that the fee caused the outcome.** Q/ETH quotes
are hypothetical sales and omit within-block price changes; Q minting and
X/Q mispricing can move both markets and cause arbitrage leakage.

The live keeper verifies the Q/executor/router and Q/ETH pool bindings,
checks the completed exit and selected arm again before signing, archives
the evidence first, and serializes with the price keeper under the existing
price-configurator lock. It writes an EIP-1559 signed transaction to its
mode-0600 state journal before broadcast; restart validates and recovers the
exact signed nonce and receipt. If a historical quote or receipt is missing,
the exit remains pending. At Q's seven-day deadline, the keeper submits
`censorExpiredOutcome` instead of making up a score. Still-open, idle, and
censored assignments remain visible in the selector's conservative
exploratory score with a provisional −5,000 bps selection penalty. That
penalty is a policy heuristic, not an observed loss.

Run one read-only cycle after Q deployment:

```sh
.venv/bin/python forensics/launch_cursor/pons_fee_feedback_keeper.py \
  --http-url "$PONS_HTTP_RPC_URL" --q "$PONS_Q_ADDRESS" \
  --price-guard "$PONS_PRICE_GUARD_ADDRESS" \
  --start-block "$PONS_Q_DEPLOYMENT_BLOCK" --once
```

For live reporting, set the existing
`PONS_PRICE_CONFIGURATOR_PRIVATE_KEY` and add `--live`. Later cycles reuse
`.local/pons-fee-feedback.json` and omit `--start-block`. The supervisor runs
bounded feedback cycles after price cycles with the same signer lock. The
configured RPC must support historical state for both previous-block quotes;
an ordinary nonarchive endpoint may collect receipts but cannot score exits.
The original read-only collector can still be run separately for receipt
inspection, but it does not sign or broadcast.
