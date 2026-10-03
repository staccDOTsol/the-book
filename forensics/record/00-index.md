# Record — index

On-chain forensics of launchpad bot structure, built from public data, and a summary of the public docket in the related litigation. Each file can be read on its own.

## Files

| File | What's in it |
|---|---|
| [20-litigation.md](20-litigation.md) | Aguilar v. Baton (S.D.N.Y.): posture and public docket entries |
| [40-onchain-xgasdev.md](40-onchain-xgasdev.md) | The XGAS.DEV launch forensics and five market-structure indicators. Chart: `exhibits/xgasdev_price_bots.png` |
| [50-cluster.md](50-cluster.md) | The wallet activity, funding topology, and correction identifying the shared contract as OKX's public DEX Router |

## In one paragraph

A new token on Robinhood Chain (XGAS.DEV, 2026-09-26) saw 59–65 permissionless v4 pools within minutes, fee tiers of 70–98%, a price ladder built from `Initialize` calls with no liquidity, same-wallet liquidity added and pulled within blocks, and a recurring 19.92 USDG payment per fill. The same wallets touched hundreds of other launches on the chain. Several wallets also used [OKX's public DEX Router](https://web3.okx.pro/es-es/onchainos/dev-docs/trade/dex-smart-contract); that shared route is not evidence that one operator controls them. The on-chain events are checkable from public RPC.
