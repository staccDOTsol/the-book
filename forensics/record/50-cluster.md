# The bot cluster — map and money

Who the wallets are, how they connect, and what they actually took. Robinhood chain (4663). All addresses are on-chain public.

## The three resident bots (coverage grid)

| Wallet | Pools | Initialize calls | Distinct tokens touched | Net USDG (all tokens) |
|---|---|---|---|---|
| `0x3e6bc0ec…` | 894 | 869 | 532 | +2,713 |
| `0x7a62a31a…` | 357 | 344 | 201 | +56 |
| `0x62a4ec1f…` | 29 | 24 | 22 | +241 |

Plus `0x05783022…` (0578) and `0x7af2fda1…` (7af2) as JIT counterparties on XGAS.

**Reading:** these are not profit engines per token — net USDG per token rounds to zero, they seed and pull back. They are a **presence layer** that indexes essentially every launch on the chain. 532 tokens for one wallet in under a month. XGAS.DEV sat alongside GME, GOOGL, CRCL, DJT, MEME, DOGGO, hundreds more. The extraction is diffuse and volume-based, not a per-victim heist.

## Funding topology (the hard links)

```
0x80bd8d9f6e ──2.53 ETH──▶ 0x1786dc58…9617b6 ──▶ 0x53091256… (refiller)
                                                        │
                                    gas top-ups (0.002–0.13 ETH each)
                                                        ▼
                                          7a62 , 3e6b  (and ~20 others)

0xcc4bc788 (hub) ──bridged 22.5 ETH via Relay Solver 0xf70da978──▶ pool funding
```

- **`0x53091256…` (the "refiller") is a venue hot wallet, NOT a shared operator.** 9,410 ETH, 146,116 txs; in its last 5,000 transfers it paid **2,578 distinct recipients**, median 0.017 ETH. Both bots drawing gas from it is a *weak* link — like two people using the same bank. Corrected from the earlier read.
- **`0xcc4bc788` has a recorded Relay funding path.** That path needs its own transaction-level comparison before it can link distinct pool creators. Use a direct, time-ordered transfer between specific addresses for a funding claim; shared use of a public contract is insufficient.

## Public router correction (2026-10-03)

- **`0x6e2a35a7ad683cf634d91492d73bb7ff774c6919` is OKX's public Robinhood DEX Router**, listed in [OKX's contract directory](https://web3.okx.pro/es-es/onchainos/dev-docs/trade/dex-smart-contract) and identified as `DexRouter` by [Robinhood Blockscout](https://robinhoodchain.blockscout.com/address/0x6e2a35a7ad683cf634d91492d73bb7ff774c6919). Its use by multiple wallets indicates that they traded through the same venue. Internal transfers from this router can be swap outputs or refunds; they are not, by themselves, direct funding from a common controller.
- The earlier inference that this same-address, cross-chain deployment tied the wallets to one private operator was **wrong**. Public infrastructure can be deployed deterministically on many chains. Neither use of this router nor its deployer identifies the users of the router.
- **`0xCcC88a9d1B4ED6b0EABA998850414b24f1c315bE` is a Relay approval proxy**, with [verified `RelayApprovalProxyV3` source on Polygon](https://polygonscan.com/address/0xccc88a9d1b4ed6b0eaba998850414b24f1c315be) and the [same public contract labeled on other chains](https://sonicscan.org/address/0xccc88a9d1b4ed6b0eaba998850414b24f1c315be). Its appearance in the hub's call path is use of shared infrastructure, not an ownership link to pool initiators.

## The second hook

- **Hook `0xe337e5c4…`** — creator `0xe1a72d04f750be30492089823187e74cc045c5bc` (a throwaway). A second hook operating on the same token; its operator is unidentified.

## Subpoena targets (for the civil case)

1. **Robinhood chain / the gas venue behind `0x53091256…`** — if that's Robinhood's own hot wallet, the operators have KYC'd accounts behind it.
2. **The Relay bridge** (`0xf70da978…` solver) — off-ramp/on-ramp identity for the funding hub.
3. **Specific pool initiator and funder EOAs** — trace their direct transfers and timing independently. The OKX router deployer `0xeb33b04b…` should not be treated as the pool initiators' owner on this evidence.

## Data files (scratchpad)

`cluster_pools.json` (per-bot pools/inits/usdg_net_by_token), `refiller_txs.json` + `refiller_summary.json` (full 146k-tx walk), `cluster_etherscan.json`, `bytecode/raw.json` (runtime + creation inputs — for *characterizing* the contracts).
