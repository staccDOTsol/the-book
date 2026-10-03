# Conditional X/Q fee and band illustration

This is a deterministic, continuous-liquidity scenario, **not a backtest, an
executable arbitrage quote, or a forecast of realized profit**. The companion
[CSV](fee_arb_matrix_illustrative.csv) sweeps every static fee arm from 5%
through 50% in 1% steps at four hypothetical Pons X price factors. It uses
[`fee_arb_scenario.py`](fee_arb_scenario.py) with the following illustrative
fork inputs:

| Input | Value |
| --- | ---: |
| Q/ETH post-first-buy spot | 0.000000002527405895 ETH/Q |
| Pons starting X spot | 0.00000000168 ETH/X |
| External starting X price, converted at those spots | 0.664713176195 Q/X |
| Self-defined band base `p0` | 0.01 Q/X |
| Pons curve shape `R` | 12.25 |
| Band upper prices `R`, `2R`, `10R` times `p0` | 0.1225, 0.245, 1.225 Q/X |
| Sequential gross 1% mints from 1 billion Q | 10,000,000; 10,100,000; 10,201,000 Q |
| Assumed Q actually deposited, 95% of each mint | 9,500,000; 9,595,000; 9,690,950 Q |

Actual v4 deposits can be **less** than 95% after tick and liquidity rounding;
unused minted Q is burned. `p0` is a mint/X-supply scale, not a Pons-to-Q
market conversion. The spot conversion above assumes Q/ETH stays fixed while
the Pons X price is multiplied by each factor.

## First marginal X-in trade

At the top of the widest band, the pool pays roughly
`(1 − fee) × 1.225` Q per gross X input. In this flat external-price model,
the first marginal arbitrage disappears only when
`fee >= 1 − (external Q/X price / 1.225)`:

| Pons X price factor | External Q/X | Fee floor at top | First 1% arm meeting floor | At 50%: Q extracted | At 50%: issuer LP mark loss |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1.00 | 0.664713 | 45.7377% | 46% | 0 Q | 0 Q |
| 0.75 | 0.498535 | 59.3033% | none | 1,042,092 Q | 101,934 Q |
| 0.50 | 0.332357 | 72.8688% | none | 2,805,818 Q | 738,970 Q |
| 0.25 | 0.166178 | 86.4344% | none | 5,104,352 Q | 2,445,619 Q |

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
model shows 1,042,092 Q released and a 101,934 Q immediate LP mark loss if
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
