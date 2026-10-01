# The Book — forensic record of launchpad bot structure

An evidentiary record of how newly launched tokens are worked by an industrialized bot operation, built from public on-chain data. Every claim below links to a file with addresses, transactions or data that a third party can re-derive from public RPC.

**Start here:** [`forensics/record/00-index.md`](forensics/record/00-index.md). Each file in the record is written to be read cold.

## Findings

**XGAS.DEV launch, Robinhood Chain (4663), 2026-09-26** — [`40-onchain-xgasdev.md`](forensics/record/40-onchain-xgasdev.md)

- First bot pool 32 minutes after launch; 59–65 permissionless Uniswap v4 pools on a token minutes old.
- Pools initialized at 70–98% fee tiers. A buyer routed through one loses most of the input to fees.
- A price ladder: successive `Initialize` calls with no tokens deposited walked the quoted price of a sibling pool about 17x with no trade touching liquidity.
- Liquidity added and removed by the same wallet within one or a few blocks (JIT +dL/−dL).
- A constant 19.92 USDG side payment on every fill.
- The bots call one helper contract deployed at the same address on 8 chains.

**The cluster** — [`50-cluster.md`](forensics/record/50-cluster.md)

- Three resident wallets: one created pools on 532 distinct tokens in under a month. Net USDG per token is near zero: they are a presence layer on every launch, not a per-token heist.
- Funding topology, the shared helper, and what each link does and does not prove.

**Detection, not instruction.** The six fingerprints in the record are written as detection rules. Pool-count and fee-tier scores for 201 pump.fun tokens are in [`forensics/pumpfun_shape_scores.json`](forensics/pumpfun_shape_scores.json).

## The record

| File | Contents |
|---|---|
| [`00-index.md`](forensics/record/00-index.md) | Overview |
| [`20-litigation.md`](forensics/record/20-litigation.md) | Aguilar v. Baton Corporation Ltd (S.D.N.Y. 1:25-cv-00880): public docket |
| [`40-onchain-xgasdev.md`](forensics/record/40-onchain-xgasdev.md) | Launch forensics and the six fingerprints |
| [`50-cluster.md`](forensics/record/50-cluster.md) | Wallet map and funding |

Raw data: [`forensics/*.json`](forensics/). Chart: [`exhibits/xgasdev_price_bots.png`](exhibits/xgasdev_price_bots.png).

## Other material in this repository

[`DEMO.md`](DEMO.md) describes a separate reproduction of the same market structure on Solana, with its scripts in `forensics/book/` and `forensics/therig-src/`. It is the author's own activity, not third-party evidence, and is kept apart from the record above.
