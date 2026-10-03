# Robinhood v4 high-fee LP firehose: 24-hour activity screen

This screen covers Robinhood Chain blocks **77,940,490–78,796,050** (2026-10-02 04:17:55 to 2026-10-03 04:17:55 UTC). It found **884** new Uniswap v4 pools with a static fee of at least 70% and below 100%; all 884 had a zero hook address, a positive `ModifyLiquidity` seed, and positive net liquidity at the completed seed block. **612** paired a token with USDG, **269** with native ETH, and **3** used neither quote. The quote-only LP proposal has a legal adjacent range at the completed seed-block tick for **880** of the 881 quote pairs. The [pool-by-pool CSV](../results/high-fee-pool-screen-24h.csv) records currency order, fee, tick spacing, seed ticks and liquidity, estimated quote deposit, legal adjacent one-interval range, Swap counts, the 30-minute band-path class, and observed LP withdrawals. The [read-only collector](../high_fee_pool_screen.py) decodes public PoolManager `Initialize`, `ModifyLiquidity`, and `Swap` logs.

The zero-hook filter removes hook-controlled swap and LP callbacks. It does **not** establish that the paired token can be transferred or sold after the position converts from quote asset to token; that requires a separate token-level exit check.

The proposed position is **single-sided USDG or ETH**, placed immediately outside the completed entry-block price on the quote-asset side. It starts inactive and earns swap fees only if trading reaches its tick interval. The sampled seed positions are evidence about existing liquidity; many are effectively full range and are **not** the proposed concentrated range. For USDG pairs, the median theoretical quote amount in the first positive seed event was **about 0.2354 USDG**; 486 of 612 were below 1 USDG and 611 of 612 below 26 USDG. These amounts come from seed liquidity, ticks, and price, and are not independently attributed receipt deposits for every pool. A new $26 position would materially change many of these thin pools, so historical Swap paths are counterfactual after entry.

## Thirty-minute fee-opportunity screen

Of the **880** legal adjacent quote-only bands, **666** pools had no Swap in a later block during the first 30 minutes after entry. **149** had at least one Swap but never reached the exact `sqrtPriceX96` boundary of the proposed one-interval band. One of those Swaps reported the boundary *tick* while its `sqrtPriceX96` remained outside the band; tick equality alone is insufficient. **65** crossed into the interior; at the 30-minute mark, 48 were token-only, 13 mixed, and 4 back on the quote-only side. For the 884-pool birth cohort, the exact count with any later-block Swap is **129 / 161 / 214 / 246** by 5 / 10 / 30 / 60 minutes, respectively; all windows have full observation coverage. The 30-minute cohort has 925 Swap events in the 214 active pools. Input volumes in the CSV use the negative v4 *caller* balance delta, consistent with [Uniswap v4's PoolManager implementation](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol) and a checked Robinhood receipt. A no-Swap pool offers zero observed swap-fee opportunity during that window; a band that was never entered also has no observed in-range swap-fee opportunity. A crossing alone does **not** establish an LP fee amount, an exit fill, or profit.

The 65 is an optimistic *next-block* ceiling. The [exact tick-price check](../v4_quote_range_screen.py) finds only **59** pools with a possible in-range swap if the mint becomes effective no earlier than entry block +2, and **56** if delayed to +5 or +10 blocks. Six of the original 65 have their only possible in-range swap in the immediately following block. Transaction ordering in that block is unknown, and a changed price can make the originally planned band no longer quote-only at mint time.

## Arrival rate and overlap

The **880** legal quote-only candidates arrived throughout the 24-hour sample, averaging **36.7 per hour**. In sample-relative one-hour bins, arrivals ranged from **12 to 98** (median **31.5**). Placing one position in every candidate and holding for a fixed time produces the following mechanical concurrency, before wallet-cash limits or exits from liquidity changes:

| Hold | Peak simultaneous positions | Time-weighted average |
| ---: | ---: | ---: |
| 5 min | 22 | 3.05 |
| 10 min | 30 | 6.10 |
| 15 min | 39 | 9.15 |
| 20 min | 45 | 12.18 |
| 30 min | 59 | 18.24 |
| 45 min | 81 | 27.32 |
| 60 min | 101 | 36.37 |
| 90 min | 140 | 54.36 |

## $104 firehose cash and gas hurdle

The [cash-envelope replay](../results/v4-firehose-cash-envelope-24h.json) enters **all 880** legal quote-only pools, stakes the chosen fraction of *remaining* cash at each entry, and releases the principal at the fixed deadline. It assumes zero gain or loss on every position only to isolate cash overlap; it does **not** fill in the 65 crossed positions. At a 30-minute deadline:

| Fraction of available cash | Median entry | Smallest entry | Sum placed in 65 crossing pools | Return on those stakes needed to cover $0.03 gas per round trip |
| ---: | ---: | ---: | ---: | ---: |
| 1% | $0.85 | $0.61 | $55.94 | 47.2% |
| 2.5% | $1.68 | $0.78 | $112.48 | 23.5% |
| 5% | $2.45 | $0.54 | $172.19 | 15.3% |
| 10% | $3.20 | $0.22 | $241.24 | 10.9% |
| 25% | $3.54 | $0.0022 | $318.26 | 8.3% |

The gas hurdle assumes the 815 positions whose observed paths never entered the band return unchanged quote principal and treats ETH and USDG as frictionlessly interchangeable for cash allocation. It is an illustrative gross return hurdle for the crossing positions: token conversion losses, price impact, failed mints, bridge/FX costs, and losses from actual fees or routing can raise it. The 25% rule makes six entries smaller than one cent during the observed 30-minute burst. Smaller wallet percentages still place stakes much larger than the typical USDG seed. These rows do not identify a profitable percentage.

The [20 sampled RH add/remove receipts](../results/v4-lp-gas-receipts-24h.json) were selected across the observation window from later LP additions (excluding pool creation transactions) and LP removals. Their separate median fees were **0.000009176 ETH** for an add and **0.000004626 ETH** for a remove. Summing those medians gives **0.000013802 ETH**, roughly **$0.0345–$0.0373** at a $2,500–$2,700 ETH valuation. The add and remove samples were unrelated transactions, so this is an illustrative round-trip gas amount, not an observed paired trade. The table uses $0.03 and the replay JSON also includes $0.05 as sensitivity values. These are other LPs' receipts, not gas estimates for our exact proposed PositionManager calls.

The [fee-threshold sweep](../results/v4-fee-threshold-screen-24h.csv) also scanned the 107 extra no-hook pools in the 50–<70% band, rather than treating 70% as a fixed rule:

| Minimum static fee | No-hook births | Legal quote-only entries | 30-minute interior crossings | Peak 30-minute overlap |
| ---: | ---: | ---: | ---: | ---: |
| 50% | 991 | 987 | 78 | 60 |
| 70% | 884 | 880 | 65 | 59 |
| 90% | 335 | 332 | 12 | 29 |
| 99% | 43 | 41 | 2 | 7 |

Increasing the fee cutoff sharply reduces the observed number of positions that enter their range. The 99% row rests on only two crossings and gives no reliable profit forecast. The [threshold summarizer](../summarize_v4_fee_thresholds.py) uses the same $104 cash assumption and reports a gas-only hurdle for each cutoff; it does not attribute any historical LP fees to the proposed new position.

The collector also records each pool's own net-liquidity peak and first crossings of 95–50% remaining. A token-wide high-fee aggregate is included only as a **cohort-bounded raw-liquidity proxy**: older pools before the birth window, two known follow-on pool births after it, and per-pool event-history limits prevent treating those aggregate crossings as a complete capital exit signal. Liquidity units across different tick ranges do not represent comparable USD depth. A portfolio fee and impermanent-loss replay still needs exact hypothetical range state, fee growth, token balances, gas, and the effect of adding our liquidity. This activity screen does not identify an ideal wallet percentage or establish expected profit.
