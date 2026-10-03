# Record — index

On-chain forensics of launchpad bot structure, built from public data, and a summary of the public docket in the related litigation. Each file can be read on its own.

## Files

| File | What's in it |
|---|---|
| [20-litigation.md](20-litigation.md) | Aguilar v. Baton (S.D.N.Y.): posture and public docket entries |
| [40-onchain-xgasdev.md](40-onchain-xgasdev.md) | The XGAS.DEV launch forensics and five market-structure indicators. Chart: `exhibits/xgasdev_price_bots.png` |
| [50-cluster.md](50-cluster.md) | The wallet activity, funding topology, and correction identifying the shared contract as OKX's public DEX Router |
| [60-launch-replay.md](60-launch-replay.md) | LAUNCH pool signals, liquidity exits, and price-print limitations |
| [70-cadence-matrix.md](70-cadence-matrix.md) | Historical token-swap signal comparison with a frozen poolset; superseded for LP decisions |
| [90-high-fee-lp-screen.md](90-high-fee-lp-screen.md) | 24-hour v4 no-hook high-fee LP firehose, one-sided range activity, cash overlap, and gas hurdles |
| [95-v4-fee-cutoff-sensitivity.md](95-v4-fee-cutoff-sensitivity.md) | 50/70/90/99% static-fee cutoffs and 30-minute range-entry frequency |
| [120-pons-hook-and-burst-universe.md](120-pons-hook-and-burst-universe.md) | Pons graduation hook, its fee routing, and its effect on the RH burst universe |
| [130-lower-fee-burst-lp-replay.md](130-lower-fee-burst-lp-replay.md) | Lower-fee LP fee marks, gas, and causal route-fork checks after funded 4–5-pool bursts |
| [140-nothingburger-two-fee-arb-model.md](140-nothingburger-two-fee-arb-model.md) | Creator-tax and LP-fee route math for a proposed NOTHINGBURGER quote token |
| [150-nothingburger-counterfactual-replay.md](150-nothingburger-counterfactual-replay.md) | One-wallet historical-shock replay for synthetic X/NOTHINGBURGER pools |
| [160-pons-every-launch-pool-plan.md](160-pons-every-launch-pool-plan.md) | Factory launch discovery, three-range feasibility, one-position and three-position gas at every-launch cadence |

## In one paragraph

A new token on Robinhood Chain (XGAS.DEV, 2026-09-26) saw 59–65 permissionless v4 pools within minutes, fee tiers of 70–98%, a price ladder built from `Initialize` calls with no liquidity, same-wallet liquidity added and pulled within blocks, and a recurring 19.92 USDG payment per fill. The same wallets touched hundreds of other launches on the chain. Several wallets also used [OKX's public DEX Router](https://web3.okx.pro/es-es/onchainos/dev-docs/trade/dex-smart-contract); that shared route is not evidence that one operator controls them. The on-chain events are checkable from public RPC.
