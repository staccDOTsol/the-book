# Conditional X/Q fee and band illustration

This is a deterministic, continuous-liquidity scenario, **not a backtest, an
executable arbitrage quote, or a forecast of realized profit**. The companion
[CSV](fee_arb_matrix_illustrative.csv) sweeps every static fee arm from 5%
through 50% in 1% steps at four hypothetical Pons X price factors. It uses
[`fee_arb_scenario.py`](fee_arb_scenario.py) with the following illustrative
fork inputs. Reproduce it with
`python3 forensics/launch_cursor/fee_arb_scenario.py matrix > forensics/launch_cursor/fee_arb_matrix_illustrative.csv`:

| Input | Value |
| --- | ---: |
| Q/ETH post-first-buy spot | 0.000000002527405895 ETH/Q |
| Pons starting X spot | 0.00000000168 ETH/X |
| External starting X price, converted at those spots | 0.664713176195 Q/X |
| Self-defined band base `p0` | 0.01 Q/X |
| Pons curve shape `R` | 12.25 |
| Band upper prices `R`, `2R`, `10R` times `p0` | 0.1225, 0.245, 1.225 Q/X |
| Sequential gross 0.1% mints from 1 billion Q | 1,000,000; 1,001,000; 1,002,001 Q |
| Assumed Q actually deposited, 95% of each mint | 950,000; 950,950; 951,900.95 Q |

Actual v4 deposits can be **less** than 95% after tick and liquidity rounding;
unused minted Q is burned. Here `p0` is held fixed at the earlier 0.01 Q/X
band scale while the mint fraction changes to 0.1%. It is **not recomputed**
from the first mint or taken from the Pons-to-Q market conversion. The spot
conversion above assumes Q/ETH stays fixed while the Pons X price is
multiplied by each factor. The three 95%-deposited tranches total
2,852,850.95 Q, a 0.285285095% supply increase for this opening before any
later fee collections, sales, or burns. A positive net mint rate still grows
without bound across indefinitely many openings; 0.1% alone is not a hard
`uint256` supply cap solution.
For a 1-billion-Q initial supply with 18 decimals and no subsequent burns or
skipped launches, the 0.285285095% per-opening growth reaches `uint256`
headroom after roughly 40,464 openings. This is arithmetic under the stated
deposit assumption, not a cadence forecast.
The implemented Q contract instead enforces a tenfold initial-supply
ceiling; without offsetting burns, it stops opening pools far earlier.

## First marginal X-in trade

At the top of the widest band, the pool pays roughly
`(1 − fee) × 1.225` Q per gross X input. In this flat external-price model,
the first marginal arbitrage disappears only when
`fee >= 1 − (external Q/X price / 1.225)`:

| Pons X price factor | External Q/X | Fee floor at top | First whole-percent fee arm meeting floor | At 50%: Q extracted | At 50%: issuer LP mark loss |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1.00 | 0.664713 | 45.7377% | 46% | 0 Q | 0 Q |
| 0.75 | 0.498535 | 59.3033% | none | 102,360 Q | 10,013 Q |
| 0.50 | 0.332357 | 72.8688% | none | 275,604 Q | 72,586 Q |
| 0.25 | 0.166178 | 86.4344% | none | 501,379 Q | 240,223 Q |

The 50% rows show the model's optimal **partial** X-in trade, rather than a
forced sweep to the low bound. At factor 1, a selected fee of 46% or more
closes the *initial marginal gap under these spots*. A fall to 75% of the
stated Pons spot reopens that gap even at 50%. The 0.01 Q/X low boundary
limits the Q principal a tranche can release; it cannot undo Q already
extracted while the pool price moves down from 1.225 Q/X.

The modeled issuer mark loss is `Q out − external Q/X × gross X in`, relative
to holding the deposited Q. Under the sole-LP, flat-price assumptions it
equals the arbitrageur's gross Q gain before gas. It is **not an ETH cash
loss or realized strategy return**. The issuer receives the modeled X input
fees; later selling X into Pons and buying/burning Q has separate executable
prices. Competing LPs or a protocol fee would reduce the issuer's fee share.

## The 120 minute window

The implemented 120 minute onchain Q winddown cannot prevent an arbitrage
trade executed inside that window. For example, at the 0.75 price factor and a 50% fee, the
model shows 102,360 Q released and a 10,013 Q immediate LP mark loss if
the trade occurs before minute 120. Ending the position afterward changes
the issuer's exposure; it does not reverse the prior swap. The Q winddown is
implemented and tested onchain, and the exit keeper handles timed exits. Its
120 minute horizon is separate from the exit keeper's 120 **second** quote
TTL.

## What would turn this into an executable bound

At one pinned block, for every material gross X input size and each band/tick
crossing, collect matching executable quotes for: (1) the ETH required to
buy that exact X amount on the active Pons curve or graduated pool, (2) the
Q output from an exact-input X→Q swap in the proposed X/Q pool at its selected
fee, and (3) the ETH output from selling that exact Q amount through Q/ETH.
The route's ETH margin is `Q-sale ETH − Pons-buy ETH − gas`. Checking only a
spot ratio or the full-band endpoint can miss a profitable partial trade.
The quote bundle mode in `fee_arb_scenario.py` checks matching sizes and
block hashes but does not authenticate RPC data. Other routes or fee capture
by an arbitrageur's own liquidity would also need analysis.

The concentrated-liquidity and input-fee formulas follow the
[Uniswap v3 whitepaper](https://app.uniswap.org/whitepaper-v3.pdf) and
[Uniswap v4 core swap implementation](https://github.com/Uniswap/v4-core/blob/main/src/libraries/Pool.sol).
