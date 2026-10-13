# Kingmaker test — definitions fixed before outcomes were computed

Written 2026-10-13, while `collect_pons.py` was still running and before any outcome was looked at.

## Claim under test

Nonidentifiable but specific actors bless charts. Blessed charts run, and the run drains retail. Charts that are not blessed do not run, and there is no mathematical reason for the difference: the selection is kingmaking.

## Why Pons on Robinhood Chain

Every Pons V2 launch trades on the same constant-product bonding curve contract. The curve's math is fixed by four immutables read from each curve (`phantomQuote`, `feeBps`, `creatorTaxBps`, `graduationThreshold`). When those four are equal, two charts have identical math by construction. Every trade emits `CurveBuy`/`CurveSell` with the receiving wallet, so who bought, when, and for how much is fully on-chain.

## Universe

- All `TokenLaunched` events from factory `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e`, chain 4663.
- Quote asset native ETH (`pairToken == 0x0`).
- Launched at least 24 h before the collection head, so every outcome window is complete.
- Primary analysis: the modal set of the four curve immutables (identical math). All-launch results reported alongside.

### Amendment 1 (added before any outcome was computed)

The factory has emitted 692,851 launches since deployment, most of them spam that nobody buys. Comparing blessed launches with launches nobody touched would be trivially true. So every test is also reported on the **contested universe**: launches with at least 3 distinct non-creator, non-kingmaker wallets buying in the first 10 minutes. These are charts retail actually showed up for. The contested universe is the headline. The full universe is reported beside it.

Window: launches from 15 days to 1 day before the collection head; trades through the head.

## Variables

- **Launch time**: timestamp of the `TokenLaunched` block.
- **Wallet**: the `recipient` of `CurveBuy` / `CurveSell`.
- **Creator**: the launch's `creator` topic. Creator trades are never counted as blessing or as retail.
- **Early buy**: a non-creator `CurveBuy` within 10 minutes of launch. Sensitivity: 3 and 30 minutes.
- **Run (primary)**: the curve emits `CurveCompleted` (graduates) within 24 h of launch.
- **Run (secondary)**: the highest buy execution price (ETH in / tokens out) within 24 h is at least 5× the first buy's.

## Kingmakers: chosen on the first half, tested on the second

Launches are sorted by time. Set **A** is the first half, set **B** the second half. Kingmaker sets are built from A only and then frozen:

- **KM-recur**: non-creator wallets with early buys in at least 5 distinct A launches. Outcome-free.
- **KM-pick**: wallets with early buys in at least 3 distinct A launches, at least 2 of which graduated, with a graduation rate among their early-bought A launches at least 3× the A base rate.

A launch in B is **blessed** under a set if any wallet in that set early-buys it.

## Tests, run on B only

1. **Blessed charts run.** Graduation rate blessed vs not blessed: risk ratio and Fisher exact p-value. Same for the secondary run.
2. **No mathematical reason.** Primary universe has identical curve math. Remaining launch-time variables (creator's own buy in the launch transaction, creator tax, hour of day) are compared between blessed and unblessed. Logistic model fit on A, scored on B: mechanics only vs mechanics + blessed. Report held-out AUC for both.
3. **The run drains retail.** In blessed B launches, net curve ETH (sells received minus buys paid) by group: kingmakers, creator, retail (everyone else). Also the share of kingmaker sell volume that lands before the 24 h price peak.
4. **Kingmakers come first.** Retail buy volume before vs after the first kingmaker buy.

## What would falsify it

- Test 1 risk ratio at or below 1, or a Fisher p-value above 0.05.
- Mechanics-only AUC as high as mechanics + blessed.
- Kingmaker net curve ETH at or below zero while retail is not negative.

## Known limits, stated up front

- Curve phase only. Trades after graduation happen in the Pons v4 pool, where the PoolManager `Swap` event does not name the end wallet. Tokens kingmakers carry through graduation are reported as held, not valued as profit.
- Wallet clustering by funding source is not applied in this first pass. Each address stands alone, which can only undercount a coalition.

## Amendment 2: pump.fun King of the Hill test (added before King of the Hill data was fetched)

Source: `~/callouts/data/snaps/2026-10-12-v2.jsonl`, minute snapshots of pump.fun curve-phase coins (6.8 h, 15,468 coins), including pump.fun's own `numKolsTraded` and `sniperCount`. Every pump.fun coin trades on the same bonding curve, so the curve math is identical.

- **Universe**: coins whose first snapshot is at age ≤ 5 minutes.
- **Blessed (early)**: pump.fun reports `numKolsTraded ≥ 1` at that first snapshot. Sensitivity: ≥ 3.
- **Mechanics at first snapshot**: progress, virtual SOL, buy count, sell count, sniper count, age.
- **Outcome 1, who reaches the hill**: pump.fun's `king_of_the_hill_timestamp` is set.
- **Outcome 2, who wins after the hill**: among coins that reached King of the Hill, `complete` (graduated) is true. Blessing for this outcome is `numKolsTraded` at the last snapshot before the King of the Hill timestamp.
- Outcomes come from pump.fun's public coin endpoint, fetched once after this amendment was written.

## Amendment 3: kingmakers are delta-neutral (correction from the author, before this test was run)

The earlier drain test expected kingmakers to profit directionally. That was a misreading. In the author's model, kingmakers are never the kings: they stay delta-neutral, and retail ends up holding the inventory. Revised test, on blessed contested B launches, curve phase, 24 h:

- **Position**: one (wallet, launch) pair. Tokens bought, tokens sold, ETH paid, ETH received.
- **Flat**: tokens left ≤ 10% of tokens bought (or the wallet sold more than it bought on the curve). "Always DN" is the share of kingmaker positions that are flat.
- **Bag**: the share of each group's bought tokens still held at 24 h. The claim predicts kingmakers ≈ 0 and retail large.
- **Holding time**: token-weighted FIFO time from buy to sell, for tokens that were sold.
- **Where retail's ETH went**: retail net ETH decomposed into protocol/buyback fees, creator tax, kingmaker net, creator net, and ETH left in the curve or carried into the graduated pool.

## Amendment 4: the blessing is rig liquidity provision (correction from the author, before this test was run)

Amendments 1–3 measured blessing as early curve buying. The author's claim is that **the blessing is liquidity provisioning by rigs**. The early-buyer results stay in the record as a separate finding. The test of the claim is redefined:

- **Side pool**: a Uniswap v4 pool initialized on PoolManager `0x8366…0951` with the launched token as one currency and a hook other than the Pons hook (`0xe5e7…be044`), or a Uniswap v3 pool from factory `0x1f7d…2EfA` containing the token.
- **Liquidity add**: v4 `ModifyLiquidity` with `liquidityDelta > 0`, or v3 `Mint`, on a side pool.
- **LP actor**: the add transaction's `from`. Shared position managers and routers are never actors.
- **Rig**: an LP actor that added side-pool liquidity on at least 5 distinct first-half (A) launches. Outcome-free. Rig-pick variant: at least 3 A launches, at least 2 of them graduated, at ≥3× the A base rate.
- **Landmark design**, so blessing always comes before the outcome. At landmark H after launch (10 min; sensitivity 1 h and 6 h), take held-out (B) launches not yet graduated by H.
  - **Blessed**: a rig added side-pool liquidity by H.
  - **Outcome**: graduation in (H, 24 h]. Secondary: highest buy price after H at least 5× the last buy price before H.
- **Controls**: identical curve math (as before) and early retail demand by H (retail buyer count and ETH), matched one-to-one.
- **Drain (next stage)**: rig LP add and remove amounts from receipts. The claim predicts rigs stay flat while retail pays.

## Amendment 5: match on the state before the rig arrived (added before this test was run)

The checkpoint tests matched on curve progress measured at the checkpoint, which falls after the rig's first liquidity add. If rig liquidity draws buyers, that conditions on a consequence of the treatment and biases the effect toward zero. Corrected design, on launches with identical curve math:

- **Age grid** G (s after launch): 60, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200.
- **Window**: for each consecutive pair (g, g′) in G.
  - **Treated**: the first rig liquidity add falls in (g, g′].
  - **Controls**: no rig add by g′.
  - **Both groups**: not graduated by g′, and at least 3 retail buyers by g.
- **Covariates, all measured at g (before treatment)**: curve progress (net ETH ÷ 4.2), retail buyer count, and creator buy.
- **Matching**: each treated launch to its nearest control in the same window.
- **Outcome**: graduation after g′, within 24 h of launch.
- **Estimate**: the treated-minus-control difference pooled over windows, resampled by creator 10,000 times.

Run unchanged on:
1. The held-out half, with the frozen rig list.
2. The untouched holdout window, with the frozen rig list.
3. The holdout's second half, with rigs re-derived by the frozen rule.

The checkpoint and holdout results already computed are reported as they are, alongside this.
