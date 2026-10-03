# Robinhood pool-burst cadence: 24-hour threshold matrix

**Scope correction:** This record freezes the pool set at each signal and models token buy/sell timing. The current proposed strategy instead adds new pools to the tracked liquidity aggregate as they appear and tests concentrated LP entry/withdrawal. The counts below remain a historical comparison, not an LP profitability backtest or the current exit rule.

The [160-cell CSV](../results/threshold-matrix-24h-no-floor-30m.csv) and [JSON](../results/threshold-matrix-24h-no-floor-30m.json) replay 20 entry rules (three through six pools in 60, 120, 180, 300, or 600 seconds) against eight secondary-pool liquidity exit thresholds (95% through 50% remaining). The source is 4,064 eligible Uniswap v4 `Initialize` events across 3,098 tokens, including a 10-minute warmup, on Robinhood Chain from **2026-10-02 04:17:55 to 2026-10-03 04:17:55 UTC**. Each cell is a different view of overlapping tokens; the counts must not be summed across rules.

An entry requires distinct pools, at least one static-fee pool between 70% and under 100%, positive net high-fee liquidity, and positive net secondary-pool liquidity at the completed entry block. **There is no dollar floor on the high-fee pool's birth deposit in the primary replay.** Positive Uniswap liquidity units are a signal condition, not proof of economically meaningful depth or common pool ownership. The [birth-deposit sensitivity CSV](../results/threshold-matrix-24h-birth-deposit-sensitivity.csv) reports how many *baseline selected entries* have a high-fee birth deposit above $0.01, $0.10, $0.50, or $1. It does not reselect a later entry if a proposed floor would reject the first one.

At entry, the replay freezes the known secondary pool IDs. For each threshold it chooses the first block where their net liquidity drops below that fraction of the observed post-entry peak. If the drop has not happened within 30 minutes, the rule exits at a 30-minute maximum hold. An entry too close to the dataset's end without either event is right censored. All entries in the 75% table below have an observable exit by that rule.

| Pools | Window | Entries / 24h | LP exits within 30m at 75% | 30m timeouts |
|---:|---:|---:|---:|---:|
| 3 | 60s | 25 | 3 | 22 |
| 3 | 120s | 33 | 7 | 26 |
| 3 | 180s | 39 | 8 | 31 |
| 3 | 300s | 49 | 10 | 39 |
| 3 | 600s | 60 | 11 | 49 |
| 4 | 60s | 8 | 3 | 5 |
| 4 | 120s | 10 | 4 | 6 |
| 4 | 180s | 15 | 5 | 10 |
| 4 | 300s | 21 | 4 | 17 |
| 4 | 600s | 29 | 7 | 22 |
| 5 | 60s | 0 | 0 | 0 |
| 5 | 120s | 2 | 1 | 1 |
| 5 | 180s | 4 | 3 | 1 |
| 5 | 300s | 10 | 3 | 7 |
| 5 | 600s | 20 | 4 | 16 |
| 6 | 60s | 0 | 0 | 0 |
| 6 | 120s | 0 | 0 | 0 |
| 6 | 180s | 0 | 0 | 0 |
| 6 | 300s | 2 | 2 | 0 |
| 6 | 600s | 9 | 3 | 6 |

For the 3/300 rule, the 49 entries were separated by a **21.1-minute median** between observed signal timestamps, with gaps from zero to about 2.6 hours. At 75% remaining, 10 exits were triggered by LP decline within 30 minutes and 39 by the time cap. Moving the remaining threshold from 95% to 50% changed the LP-trigger count from **13 to 8** within 30 minutes; the complete 160-cell grid records every threshold. These are potential signal and scheduled exit counts, not validated executable orders.

The 3/300 candidates' maximum high-fee birth deposit has a median of **0.991422 USDG**: 44 of 49 have at least $0.01, 42 at least $0.10, 39 at least $0.50, and 10 at least $1. These are receipt transfers to PoolManager in a transaction with one `Initialize` and positive same-transaction LP, not active tick depth. All 264 distinct candidate pool births and all 170 high-fee birth receipts were found; 107 high-fee pools were USDG-quoted and 63 ETH-quoted. The [3/300 candidate event file](../results/threshold-3-300-no-floor-events.json) preserves each entry, 75% exit choice, and per-pool deposit for checking this distribution.

This chain-only matrix **does not measure profitability**. It has no same-size executable quotes or fills at each entry and exit, and no verified gas, route, or slippage cost per candidate. The single completed $4 Fomo LAUNCH round trip elsewhere in this record spent 4.000000 USDC and returned 3.500279 USDC; its 0.87506975 cash-retention factor is one observation, not a general fee estimate. A portfolio replay must also account for overlapping positions, available cash, missing prices, and execution failures before claiming expected PnL.
