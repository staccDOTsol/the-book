# Record — index

On-chain forensics of launchpad bot structure, built from public data, and a summary of the public docket in the related litigation. Each file can be read on its own.

## Files

| File | What's in it |
|---|---|
| [20-litigation.md](20-litigation.md) | Aguilar v. Baton (S.D.N.Y.): posture and public docket entries |
| [40-onchain-xgasdev.md](40-onchain-xgasdev.md) | The XGAS.DEV launch forensics and the six detection fingerprints. Chart: `exhibits/xgasdev_price_bots.png` |
| [50-cluster.md](50-cluster.md) | The bot cluster: wallets, funding topology, the shared cross-chain helper |

## In one paragraph

A new token on Robinhood Chain (XGAS.DEV, 2026-09-26) was worked within 32 minutes by a multi-chain bot operation: 59–65 permissionless v4 pools on a token minutes old, honeypot fee tiers of 70–98%, a price ladder built from `Initialize` calls with no liquidity, same-wallet liquidity added and pulled within blocks, a fixed 19.92 USDG payment per fill, and one helper contract deployed at the same address on 8 chains. The same wallets touched hundreds of other launches on the chain. Every claim is checkable from public RPC.
