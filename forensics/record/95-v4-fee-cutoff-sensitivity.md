# Robinhood v4 LP firehose: fee-cutoff sensitivity

The [70% pool screen](90-high-fee-lp-screen.md) uses an observed 24-hour birth cohort, Robinhood blocks **77,940,490–78,796,050** (2026-10-02 04:17:55 to 2026-10-03 04:17:55 UTC). Extending the same zero-hook, static-fee, positive-seed-LP screen to **50–<70%** adds **107** pools: 85 paired a token with USDG and 22 with native ETH. All 107 had positive net liquidity in their completed seed block and allowed a legal one-tick quote-only band immediately outside that block's price. Pools with a later-block Swap numbered **16 / 25 / 33 / 36** at 5 / 10 / 30 / 60 minutes. In the first 30 minutes, **74** had no Swap, **20** swapped without reaching the proposed band's boundary, and **13** crossed its interior. Of those 13, seven ended token-only and six mixed. [Pool-level 50–<70% data](../results/high-fee-pool-screen-50to70-24h.csv) and the [reproducible threshold comparison](../summarize_v4_fee_thresholds.py) support the [four-cutoff CSV](../results/v4-fee-threshold-screen-24h.csv).

| Minimum static fee | New zero-hook pools | Legal quote-only entries | No Swap in 30m | Swap, no interior entry | Interior crossed | Average arrivals/hour | Peak open at 30m |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 50% | 991 | 987 | 740 | 169 | 78 | 41.1 | 60 |
| 70% | 884 | 880 | 666 | 149 | 65 | 36.7 | 59 |
| 90% | 335 | 332 | 259 | 61 | 12 | 13.8 | 29 |
| 99% | 43 | 41 | 39 | 0 | 2 | 1.7 | 7 |

“Swap, no interior entry” means the observed path never reached the band's exact `sqrtPriceX96` boundary. Every pool's 30-minute window has complete later-block observation. The crossing test uses the exact Uniswap v4 TickMath boundary and strict positive-length overlap of consecutive Swap prices with the band. A reported boundary tick can still have a price outside the band. Interior traversal is a necessary path condition for an adjacent single-sided quote position to become active, **not** a measured fee or a feasible exit value. New liquidity could change the Swap route and price path, especially in these thin pools.

For scale only, a $104 cash envelope staking 25% of then-available cash in each legal pool, returning unchanged principal at 30 minutes, and charging an optimistic $0.03 gas per add/remove round trip would require aggregate gross returns on the *crossing stakes* of about **8.8%, 8.3%, 9.7%, and 2.7%** at the four cutoffs to cover gas on **all** entries. These figures omit token inventory loss, actual LP fees, gas variance, FX, price impact, and failed mints. The 99% figure rests on only two observed crossings and is particularly unstable. The sample cannot identify an optimal fee cutoff or wallet fraction.
