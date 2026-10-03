# On-chain forensics — XGAS.DEV launch, 2026-09-26

Evidentiary record of what happened to the XGAS.DEV launch on the Robinhood chain (chainid 4663), and the fingerprints that let a third party recognize and prove the same pattern elsewhere. Descriptive, not a build guide.

## Fixed reference data

- **Token (real CA):** 0x006D2D9e65f847e8B5f5053C9eb3a7824ec7dFa3
- **Launch hook:** 0xe5e70264…
- **Uniswap v4 on chain 4663:** PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`; PositionManager `0x58daec3116aae6d93017baaea7749052e8a04fa7`
- **Event topics:** Initialize `0xdd466e67…`; ModifyLiquidity `0xf208f491…`; Swap `0x40e9cecb…`
- **RPC:** https://rpc.mainnet.chain.robinhood.com (needs a browser User-Agent; 429-rate-limited)

## What the price did (ETH pool, 1-min, UTC)

- Launch ~04:59. 17,566 swaps 04:59–17:35.
- **Pre-bot (04:59→05:31):** $5.5e-5 → $1.2e-4, +123%, on organic flow (3,447 swaps).
- **First bot pool Initialize: 05:31**, by 3e6b, 9.8% fee tier — 32 minutes after launch.
- Run-up to peak **$9.7e-4 at 06:35**, dense with 7a62/62a4 Initialize clusters.
- **7a62 dumps 521k XGAS at 07:34**, price −45% off peak.
- Close ~$1.69e-4.
- Chart: `xgasdev_price_bots.png` (in this folder).

## The fingerprints (how to recognize this rig)

These are the observable signatures. Each is a detection rule, not a recipe.

1. **AMM-count anomaly at t≈0.** Dozens of permissionless v4 pools on a token minutes old. On XGAS: ~59–65 pools on the real CA. A brand-new token with more pools than trades is the tell.
2. **Honeypot fee tiers.** Pools initialized at fee tiers of 70–98% (up to the v4 max of 1e6 = 100%). A buyer routing through one loses most of their input to fees. Legitimate pools sit at 0.05–1%.
3. **Initialize-without-tokens price ladder.** `Initialize` writes a `sqrtPriceX96` — a quoted price — with **zero tokens deposited**. Successive Initializes at rising prices move the *quoted* price with no trade ever hitting liquidity. On a sibling USDG pool the quoted price walked ~17x (05:26→06:45) this way. This is the "move price without buys or sells" mechanism.
4. **JIT +dL/−dL pairing.** ModifyLiquidity add and remove by the same wallet inside one block or a few blocks — liquidity that exists only to be quoted, then withdrawn.
5. **Fixed side-payment per fill.** The resident bots emit a constant **19.92 USDG** per fill — a machine constant, not a market outcome.

**Correction:** An earlier sixth item treated `0x6e2a35a7…` as a private cross-chain helper and inferred common control from its use. [OKX lists the full address as its public Robinhood DEX Router](https://web3.okx.pro/es-es/onchainos/dev-docs/trade/dex-smart-contract). Shared use of that router is a venue observation, not a detection rule for common ownership. See [the cluster correction](50-cluster.md#public-router-correction-2026-10-03).

## Net effect

Per-token USDG net for the bots is near zero (see [50-cluster.md](50-cluster.md)) — they seeded and pulled liquidity to within a few dollars. **The launch was catalogued, not looted.** For a court that is the better claim: the rig runs on ~every launch on the chain (one bot touched 532 tokens in under a month), so this is systemic market structure, not a grudge.

## Data files (scratchpad)

`xgasdev_real_pools_decoded.json` (fee/tick/hooks/price per pool), `xgasdev_swaps.json` (17,566 ETH-pool swaps), `xgasdev_overlay.json` (per-minute OHLC + bot markers), `xgasdev_bot_xfers.json`.
