# Kingmaker test — results

Definitions were fixed in [DEFINITIONS.md](DEFINITIONS.md) before outcomes were computed. Reports: `.local/kingmaker/pons-report-early{180,600,1800}.txt`, `rig-lp-report-H{600,3600,21600}.txt`, `pumpfun-report.txt`.

## Primary result: matched on the state before the rig arrived, replicated (`risk_set.py`, Amendment 5)

| Test | Treated | Graduate | Matched controls | Effect, 95% [creator-clustered] | p |
|---|---|---|---|---|---|
| Main window, held-out half, frozen rigs | 249 | 12.0% | 5.6% | +6.4 [1.6, 11.2] | 0.004 |
| Untouched holdout (Sept 13–27), frozen rig list | 1,180 | 13.4% | 10.0% | +3.4 [0.9, 6.0] | 0.003 |
| Holdout second half, rule re-applied (47 rigs) | 788 | 12.7% | 7.6% | +5.1 [2.1, 8.1] | 0.0002 |

- **Superseded checkpoint design**: it matched on progress measured *after* the rig's first add, a post-treatment variable. Its results, including the holdout ones, are in the table below and in paper Appendix B.
- **Repeat adjustment** (`cluster_stats.py`): checkpoint results at 10 min and 6 h survive clustering by creator and by rig, and Holm correction. The 1 h checkpoint fails.
- **Paper**: [`paper/kingmakers/manuscript.pdf`](../../paper/kingmakers/manuscript.pdf).

## Status after adversarial review (superseded in part by the result above)

The results below are strong associations, not proof of the four points.

### Curve-progress control (`adversarial_progress.py`)

Graduation is deterministic: 4.2 ETH of net buying. So progress along the curve at the checkpoint is the "mathematical reason" to hold fixed. Blessed charts are further along at 10 min (median 18% vs 0%). Same curve math, contested launches only, matched on progress, retail buyers and creator buy:

| Checkpoint | Blessed | Matched unblessed | p | Graduations among blessed |
|---|---|---|---|---|
| 10 min | 10.2% | 1.9% | 0.004 | 16 |
| 1 h | 5.4% | 6.3% | 0.76, **fails** | 12 |
| 6 h | 3.4% | 0.0% | 0.005 | 8 |

At 10 min the direction holds in every progress band: 2/95 vs 9/2,702, 8/39 vs 7/77, and 6/22 vs 0/10.

### Who the rigs are

The top 10 rigs share one profile:
- **Wallets, not contracts.** Each is an EOA with 650 to 66,000 transactions.
- **Shared entry point.** All add liquidity through the official Uniswap v4 PositionManager `0x58da…4fa7`.
- **Honeypot fee tiers.** They run 79–99% static fees, with no hook, as described in [40-onchain-xgasdev.md](../record/40-onchain-xgasdev.md).
- **Not Pons infrastructure.** None is a Pons contract or fee recipient.

### What each group can actually get out (`exit_value.py`)

**Method**:
- "Retail" excludes 8,517 high-frequency wallets: anyone trading more than 10 launches in the window. The threshold was fixed before running.
- Tokens still held are sold into each graduated pool's real v4 liquidity at the collection head, net of the launch's fee and creator tax.
- An AMM pays earlier sellers more, so each group is run selling first and selling last.

| Group, 153 graduated charts | Cash in/out | Exit value of tokens held | Net |
|---|---|---|---|
| Retail | −218.29 | 56.93 – 176.73 | **−41.55 to −161.36 ETH** |
| High-frequency wallets | −123.20 | 21.09 – 80.57 | −102.11 to −42.63 |
| Early-buyer kingmakers | −48.03 | 29.31 – 62.39 | −18.72 to +14.36 |
| Creators (before 42.98 ETH tax) | −68.26 | 18.41 – 45.65 | −49.85 to −22.61 |
| Rigs | −0.08 | 0.02 | **−0.06 in every case** |

- **Rigs**: flat in every exit order.
- **Retail**: loses 42–161 ETH across the 153 graduated charts.
- **Who gains**: no blessing group captures retail's loss as profit. It goes to fees, creator tax, and liquidity that stays locked in the pools.

### Fresh-data confirmation

The rig list, the early-buyer list and the rules are frozen in `frozen/2026-10-13.json`, with SHA-256 hashes of the source data. `confirm_fresh.py` reruns the checkpoint test unchanged, only on launches created after the freeze. It is the clean answer to problem 6 below.

### Open problems a reviewer will raise

1. **Small counts and many tests.** There are only 8–16 graduations per test, and many tests were run. Same creators and rigs repeat across launches, so the p-values assume independence that isn't there.
2. **Reverse causality.** Rigs add liquidity to charts that are already moving. Holding progress fixed narrows this; it does not remove it.
3. **"Retail" is residual.** It means everyone not a creator, rig or listed early buyer, which includes bots and arbitrage traders.
4. **Drain magnitude depends on valuation.** Curve-only loss counts graduation's 4.2 ETH transfer as a loss. Lifecycle retail cash is −341 ETH, but retail still holds about 70% of the tokens against 375 ETH in pools. No group has taken out retail's ETH as cash.
5. **Rig P&L is incomplete.** About 71M tokens reached rigs by transfer at unknown cost, and fee income is approximated.
6. **Definitions changed after early results.** Amendments 1–4 were logged before each of their tests, but only a run on fresh launches, with frozen code, settles this.
7. **The top rigs are unidentified.** Their contracts and funding have not been checked.

## The blessing is rig liquidity (Amendment 4, `analyze_rig_lp.py`)

- **Side pools**: 11,366 v4 and 56 v3 side pools on the 58,430 launch tokens. 131,156 liquidity adds, each with the adding transaction's sender.
- **Rigs**: 259 LP actors that added side-pool liquidity on at least 5 first-half launches. The top three did so on 679, 661 and 448 launches. They are absent from the earlier record.
- **Selectivity**: only 1,232 of 22,885 first-half launches (5.4%) got any side-pool liquidity within 24 h.
- **Test design**: held-out half, launches not yet graduated at the landmark. Rig-blessed means a rig added liquidity by the landmark. The outcome is graduation after the landmark, within 24 h.

| Landmark | Contested, identical curve math | Risk ratio, Fisher p | Matched on early retail demand |
|---|---|---|---|
| 10 min | 16/157 (10.2%) vs 16/2,789 (0.57%) | 17.8, 6e-13 | 10.2% vs 3.8%, p 0.03 |
| 1 h | 12/222 (5.4%) vs 7/2,805 (0.25%) | 21.7, 6e-10 | 5.4% vs 6.3%, p 0.72 (not significant) |
| 6 h | 8/238 (3.4%) vs **0/2,808 (0%)** | n/a, 1e-9 | 3.4% vs 0.0%, p 0.006 |

- **All contested launches, 10 min**: 30/518 (5.8%) vs 35/9,141 (0.38%). Demand-matched: 5.8% vs 2.7%, p 0.007.
- **5× after the landmark**: not reliably predicted. Rig-blessed charts graduate; they don't keep multiplying after the landmark.
- **Rig exits**: 78% of rig LP positions are fully withdrawn within the window. The median first withdrawal comes 2.7 h after the add, and 5.7% are pulled within 60 s.
### Drain through rig liquidity: measured (`measure_rig_drain.py`, `rig_hedge.py`)

**Method**: every held-out side pool replayed event by event for 24 h after launch (11,366 pools; 501,367 swaps in total).
- Liquidity changes are converted to exact amounts with the v4 range formula at the pool's price at that moment.
- Tokens are valued at the bonding-curve price at that moment. USDG converts at $2,600/ETH.
- Each swap's fee is split across the positions in range.

**Rig side-pool positions vs other LPs (held-out, 24 h)**

| | Rigs (259) | Other LPs |
|---|---|---|
| Deposited | 145.3 ETH-value | 226.0 |
| Withdrawn + open at 24 h | 135.4 | 234.8 |
| Fees earned | 4.5 | 7.8 |
| P&L | −5.4 ETH (−3.7%) | +16.5 (+7.3%) |
| Quote side (ETH in → out + open) | 141.2 → 127.1 | 198.1 → 203.1 |
| Tokens (in → out + open) | 343M → 551M (+60%) | 2.02B → 2.65B (+31%) |

- **Rig profit**: rigs end close to flat in value. They are not the ones profiting.
- **Rig inventory**: rigs are not flat in tokens on anything visible on-chain. Their side-pool liquidity absorbed sellers' tokens (+60%), and the same addresses bought 293M tokens on curves (1.22 ETH) and sold none within 24 h. Any delta-neutral offset happens off these addresses, off this chain, or after 24 h.
- **Side-pool traders**: paid 254.1 ETH-value and received 246.6, a loss of 7.5 ETH. Swap fees came to 12.3 ETH.

**Curve outcomes, held-out launches, rig liquidity at any time within 24 h (descriptive; the timing-safe test is the checkpoint table above)**

| | Rig-blessed | No rig liquidity |
|---|---|---|
| Launches | 855 | 22,030 |
| Graduated | 151 (17.7%) | 2 (0.01%) |
| Net ETH of non-rig, non-creator traders | −1,050.9 (−17.2% of ETH in) | −431.6 (−6.2%) |
| Per launch | −1.23 ETH | −0.02 ETH |
| Fees + creator tax as share of that loss | 29% | 92% |

On rig-blessed charts, 71% of the traders' loss is not fees. Most of it is ETH carried into the 151 graduated pools.

### Point 3, end to end (`prove_drain.py`)

**Scope**: all 153 held-out launches that graduated, followed from launch to the collection head.
- **Venues**: bonding curve, side pools, and the graduated Pons pool. The graduated pools have 523,043 swaps, each attributed by the trade's `tx.from`.
- **Groups**, first match wins: creator, rig, early-buyer kingmaker, retail.
- **Valuation**: realized ETH is cash actually received minus cash paid. Tokens still held are marked at each pool's last price.

| Group | Realized ETH | Tokens held, at last price | Side LP still open | Total |
|---|---|---|---|---|
| Retail | **−341.48** | 2,015.32 | 25.98 | — |
| Early-buyer kingmakers | −48.03 | 306.02 | 0 | — |
| **Rigs** | −0.08 | −2.50 | +2.55 | **−0.02** |
| Creators (trading) | −68.26 | 551.48 | 0 | — |
| Side-pool traders (wallet not attributed) | +9.54 | −7.87 | 0 | — |

**Where retail's 341 ETH sits:**

| Destination | ETH |
|---|---|
| Still in the 153 graduated pools at head | 374.75 |
| Creator tax | 42.98 |
| Curve fees | 26.43 |
| Side-pool traders | 9.54 |
| **Conservation residual** (side-pool reserves) | −4.15 |

- **Rigs net out near zero across the lifecycle**: −0.02 ETH, with a short token position (−2.50 ETH) offset by still-open side LP (+2.55). This is not a clean P&L: about 71M tokens reached rigs by transfer at unknown cost.
- **Retail is out 341.48 ETH in cash**, but it still holds about 70% of the tokens, and selling them would recover part of the pools' 374.75 ETH. The true loss is unmeasured. The chart marks its tokens at 2,015 ETH, but all 153 graduated pools together hold 374.75 ETH. Every group's marked tokens together come to 2,872 ETH, 7.7× the ETH that backs them.
- **No wallet group has taken retail's ETH out as cash.** Kingmakers (−48) and creators (−68 from trading, −25 after collecting tax) are also negative. The money sits in permanently locked graduated liquidity, plus fees and tax. It goes to whoever sells into those pools first.
- **Not included**: Pons hook fees on post-graduation swaps. They are an extra cost to traders, so retail's loss is understated.

## Early-buyer coalition (Amendments 1–3)

## Robinhood Chain, Pons V2 (on-chain, every wallet)

45,770 ETH launches from 2026-09-28 to 2026-10-12; 2.67M curve trades. Kingmakers were identified on the first 22,885 launches and tested on the next 22,885, which they had never seen.

**KM-pick**: 1,357 wallets that bought early in at least 3 first-half launches, at least 2 of which graduated, at ≥3× the base rate. They are pseudonymous and specific: a fixed list, frozen before the test.

| Held-out half, 10-min early window | Blessed graduated | Unblessed graduated | Risk ratio | Fisher p |
|---|---|---|---|---|
| Contested (≥3 retail early buyers) | 142 / 6,226 (2.28%) | 3 / 1,788 (0.17%) | 13.6 | 1.2e-12 |
| Contested, identical curve math | 62 / 1,850 (3.35%) | 2 / 540 (0.37%) | 9.1 | 1.2e-5 |
| All launches | 143 / 9,969 (1.43%) | 10 / 12,916 (0.08%) | 18.5 | 7.3e-40 |

- Of 145 contested held-out launches that graduated, 142 had a kingmaker buying in the first 10 minutes.
- **Identical math**: in the "identical curve math" row, every launch has `phantomQuote` 1.68 ETH, `feeBps` 100, `creatorTaxBps` 200 and `graduationThreshold` 4.2 ETH (read from each curve).
- **Prediction**: launch mechanics alone (creator buy, tax, hour) give a cross-validated AUC of 0.63. Adding blessing raises it to 0.70–0.72.
- **Early demand (exploratory, not pre-registered)**: blessed launches had about 2× the early retail buyers. Matched one-to-one on early retail buyers, early retail ETH, creator buy and tax, blessed identical-math launches still graduate about twice as often:

| Early window | Blessed | Demand-matched unblessed | p |
|---|---|---|---|
| 3 min | 3.48% | 1.37% | 0.0005 |
| 10 min | 3.35% | 1.68% | 0.0005 |
| 30 min | 3.27% | 2.00% | 0.0025 |

- **Recurring early buyers are not kingmakers**: KM-recur (≥5 early buys, outcome-free) passes the raw test. Matched on demand, though, it does worse than unblessed (10 min, identical math: 3.67% vs 8.41%). High-frequency early buyers follow demand; they don't pick winners.

### Drain: kingmakers stay delta-neutral, retail holds the bag (Amendment 3, `dn_drain.py`)

Blessed contested held-out launches, curve phase, 24 h:

| | Kingmakers | Retail |
|---|---|---|
| Net ETH, % of ETH put in (10 min) | +1.5% | −19.4% |
| Identical math | −0.3% | −29.1% |
| 3 min / 30 min windows | +6.0% / −0.7% | — |
| Share of their bought tokens still held at 24 h | 31% per address; as a group they sold *more* tokens than they bought | 47% (68% on identical math) |
| Positions flat (≤10% of tokens left) | 59% | 46% (33% on identical math) |

- **Where retail's 1,493 ETH went** (10 min, contested):
  - Fees and creator tax: 606 ETH (41%).
  - Left in curves or carried into graduated pools, behind the tokens retail still holds: 808 ETH (54%).
  - Kingmakers' and creators' trading: 80 ETH (5%).
- **Flat as a group, not per address.** 5,490 kingmaker positions sold tokens that the same address never bought on that curve (1,467 ETH), so inventory moves between their addresses.
- **Token hand-offs** (`trace_inventory.py`): 556 of the 1,357 early-buyer wallets hand tokens directly to each other. Of the tokens they received from anywhere other than a curve:
  - 12% came from other wallets on the list.
  - 53% came from unlisted wallets (EOAs).
  - 7% came from token creators.
- **Kingmakers are not the creators.** 29 of 1,357 kingmaker wallets ever launched a token. None of the 142 blessed graduates was created by one. All 337.5 ETH of creator tax went to other addresses.
- **Not yet shown**: whether those creators are linked to the kingmakers by funding source, and what happens in the graduated v4 pools.

### Limits

- Launches share creators and wallets, so the p-values treat dependent rows as independent. Clustering by creator is the next robustness step.
- KM-pick uses first-half outcomes, so it shows the kingmakers' picks run. It does not show whether they cause runs or foresee them.

## pump.fun (one day, 2026-10-12, same bonding curve for every coin)

9,375 coins first seen at ≤5 min old in `~/callouts` snapshots. "Blessed" means pump.fun's own `numKolsTraded ≥ 1` at that first snapshot.

| | Blessed | Unblessed | Matched on curve state at 5 min |
|---|---|---|---|
| Graduated | 88 / 3,837 (2.29%) | 39 / 5,538 (0.70%) | 2.29% vs 1.02%, p 0.0001 |
| ATH ≥ 5× | 862 / 3,837 (22.5%) | 49 / 5,538 (0.9%) | 22.5% vs 3.0%, p 0.0001 |

With ≥3 KOLs at 5 min (n = 46), there is no effect beyond curve state.

**King of the Hill: blocked.** pump.fun's API no longer returns a King of the Hill timestamp, its King of the Hill endpoint returns 404, and the callouts collector never recorded it. It can only be tested going forward, with a collector that records the current King of the Hill every minute.

## Callouts bot (2026-10-12, 515 executed buys, all passing the same math score)

| KOLs at entry | n | Mean marked PnL | Win rate |
|---|---|---|---|
| 0 | 357 | −2.3% | 21% |
| ≥ 1 | 158 | +18.0% | 37% |
| ≥ 3 | 66 | +42.4% | 56% |
| ≥ 5 | 18 | +57.7% | 83% |

- Matched on bot score, SOL-to-2×, 5-min net flow, curve progress and age, KOL ≥ 3 picks beat their math-twins by +43 points (48 of 66 pairs, sign-flip p 0.005).
- Only 24 of the 515 exits executed.
