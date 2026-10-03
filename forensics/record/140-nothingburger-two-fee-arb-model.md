# NOTHINGBURGER as a quote asset: two-fee arbitrage model

The proposed NOTHINGBURGER launch is an **unlaunched** Pons token paired with ETH. The supplied form previews a 0.0005 ETH launch fee, a 0.044 ETH developer buy, a 4.2 ETH graduation threshold, and an optional creator tax up to 10%. Those are draft settings, not a deployed token or a record of trading. Pons says a creator tax on its own bonding curve and graduated hook goes to the creator in the token's paired asset, ETH here, after fees are swept and claimed. Its token has no transfer tax. [Pons fee and payout documentation](https://docs.ponsfamily.com/v2#fees), [Pons hook documentation](https://docs.ponsfamily.com/v2#uniswap-v4-pools)

## Where the two fees arise

Suppose we seed an independent Uniswap v4 `X/NOTHINGBURGER` pool with a quote-only NOTHINGBURGER LP position. An arbitrageur can trade in that pool and then use the Pons `NOTHINGBURGER/ETH` curve or graduated pool to complete a route. On a route that actually traverses both venues, we can receive:

1. Our share of the independent pool's **core LP fee**, in whichever asset the swap pays as input; and
2. The **creator tax on the Pons hop**, eventually claimable in ETH, plus any creator share of Pons' standard fee.

An `X/NOTHINGBURGER` swap alone pays only the first fee. The Pons token is a standard ERC-20; transferring it or swapping it in an unrelated pool does not invoke the Pons hook. We must also buy our NOTHINGBURGER inventory from the curve: the creator has no initial allocation, since the full token supply starts in the curve. [Pons launch and hook mechanics](https://docs.ponsfamily.com/v2)

Both receipts belong in one owner balance sheet. Let `L` be the executable change in our LP position value **including its core fees**, `C` be ETH actually claimed from *third-party* Pons-route trades, and `G` be launch, entry, exit, conversion, sweep, claim, and routing costs. Then incremental strategy value is `L + C − G`, compared with the same token launch without our extra LP markets. Creator tax that we pay on our **own** conversion is a transfer between our wallets, not outside revenue. Organic Pons volume that would have happened without the extra pool is baseline launch income, not an LP strategy gain.

## Arb hurdle

Let `t` be the creator tax, `b` the Pons base trading fee, `f` the independent pool fee, `e` the fee on the other external market, and `R` the pre-fee price advantage as an output/input multiple. Ignoring slippage and gas, a one-Pons-hop route is profitable only if

`R × (1 − b − t) × (1 − f) × (1 − e) > 1`.

Thus the minimum gap above parity is `1 / [(1 − b − t)(1 − f)(1 − e)] − 1`. The table below takes the launch form's **1% Pons base fee**, sets `e = 0` as an optimistic lower bound, and excludes price impact and gas. Real routes require larger gaps.

| Creator tax | 1% X-pool fee | 7% X-pool fee | 20% X-pool fee | 50% X-pool fee | 70% X-pool fee |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0% | 2.03% | 8.61% | 26.26% | 102.02% | 236.70% |
| 2% | 4.13% | 10.85% | 28.87% | 106.19% | 243.64% |
| 4% | 6.33% | 13.19% | 31.58% | 110.53% | 250.88% |
| 6% | 8.61% | 15.62% | 34.41% | 115.05% | 258.42% |
| 8% | 11.00% | 18.16% | 37.36% | 119.78% | 266.30% |
| 10% | 13.49% | 20.82% | 40.45% | 124.72% | 274.53% |

The tax raises ETH earned **per successful external Pons trade** while raising the minimum price gap that makes the trade worth executing. Pairing a 10% creator tax with a 50–70% independent-pool fee requires a roughly **2.25–3.75×** pre-fee price difference before slippage, external fees, and gas. Lower independent-pool fees allow a much broader set of possible arb routes but pay less core fee per swap. The best creator tax cannot be read from fee percentages alone; it depends on how genuine arb volume responds to these hurdles.

The [reproducible 0–10% sweep](../pons_arb_fee_math.py) and [55-row CSV](../results/pons-arb-fee-hurdles.csv) provide every integer creator-tax setting. Under an illustrative 1% Pons base fee, 30% protocol share of that base fee, no buyback, and a $100 taxed Pons trade, a 10% creator tax yields **$10 tax plus $0.70 of base-fee creator share** before sweep/conversion costs. At 0% creator tax the conditional creator base share is $0.70. The volume needed to cover $0.0359 of gas ranges from **$5.13** at 0% tax to **$0.34** at 10%, *if that volume is genuinely additional Pons-route trading*. A separate historical fork's $1.153373 gross loss plus $0.035885 sampled gas would require $11.11 of such volume at 10%; that fork used a USDG pool, so this last figure is a scale illustration, not a NOTHINGBURGER backtest.

## A fully specified toy route

A local mathematical example uses fictional reserves: 10 ETH and 1 million NOTHINGBURGER on its curve, 10,000 X and 1 million NOTHINGBURGER in an independent constant-product X pool, an external X price of 0.0008 ETH, and our ownership of 10% of X-pool liquidity. An arbitrageur buys 100 X externally, sells it into the 1%-fee X pool, then sells the resulting NOTHINGBURGER to the Pons curve at a 10% creator tax. The route leaves the arb **0.006389 ETH profit** after an assumed 0.00001 ETH gas. Our independent LP earns its share of the X-pool fee, and the creator tax accrues **0.009708 ETH**; however, our LP's marginal spot-marked inventory loses **0.020935 ETH**, leaving **−0.011227 ETH** combined before our own gas. At a 50%-fee X pool the same route loses the arb about 0.03595 ETH, so a rational arb would not take it. This example demonstrates double fee collection and inventory loss in the same route; its reserves and external price are invented and it is **not a historical backtest**.

No historical Pons/NOTHINGBURGER price path or third-party arb volume exists yet, so the [24-hour RH burst replay](130-lower-fee-burst-lp-replay.md) cannot supply realized NOTHINGBURGER returns. The next live-independent test is to measure price gaps, executable route sizes, and creator fee accrual on a local fork or after a launch; until then the tax sweep gives feasibility thresholds, not a chosen optimum or expected wallet profit.

## Which X tokens, and when

The [historical candidate generator](../nothingburger_pair_candidates.py) takes the first **strictly funded** `4/300` or `5/600` signal per token after an ETH/USDG-paired Pons graduation. Both Pons and zero-hook pools count as burst evidence; third-party hooks do not. In the 24-hour sample it selected **19 tokens**: 18 first qualified at `4/300` and one at `5/600`. The completed signal block is the earliest point this rule knows the token qualifies. Pons graduation preceded those signals by a median 49 seconds; at most three candidates appeared in any 30-minute window. The exact past token addresses, signal blocks, and UTC times are in the [candidate CSV](../results/nothingburger-historical-pair-candidates-24h.csv).

These 19 are past observations, not pending orders. For a future token, the mechanical candidate rule is clear, but initializing an X/NOTHINGBURGER pool still needs a deployed NOTHINGBURGER address, live X/ETH and NOTHINGBURGER/ETH executable quotes, a chosen v4 fee and tick spacing, and an entry band checked at submission. The historical candidate count estimates **signal cadence only**. It cannot establish that a newly created quote pool will attract profitable arbitrage.

As a limited reality check, a [historical price-gap proxy](../pons_two_fee_gap_proxy.py) compares each existing Pons X/ETH pool with same-token independent ETH/USDG pools from the funded burst sample. It found 30 matched pool paths for `4/300`; four exceeded their actual independent fee plus a 0%-tax Pons fee at the signal, three with 10% tax. Thirteen exceeded either hurdle at some point in the following 30 minutes. For `5/600`, the corresponding counts were 29 paths, four/three at the signal, and ten/nine within 30 minutes. Only 12 of the 30 `4/300` pairs and five of the 29 `5/600` pairs swapped on **both** venues after the signal. Many apparent gaps are stale quotes in tiny, high-fee pools. This proxy measures neither executable route size nor the volume a new X/NOTHINGBURGER pool would attract, and it does not select a creator-tax rate. [Proxy results](../results/pons-two-fee-gap-proxy-24h.json)
