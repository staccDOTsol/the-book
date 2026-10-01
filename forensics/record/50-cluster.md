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
- **The strong links are:** (a) the funding hub `0xcc4bc788` bridging in via Relay, and (b) the **shared helper contract** the bots both call.

## The shared helper contract (the real signature)

- **`0x6e2a35a7…`** — exists at the **same address** on eth, base, arb, op, bsc, unichain, ink, soneium. Deployed via canonical CREATE2 deployer `0x4e59b448…`. Deployer EOA `0xeb33b04b…` (13 contract creations).
- **`0xccc88a9d…`** (hub's callee) — same address on 12 chains. Deployer `0x818aa60a…`, itself funded 0.05 ETH from `0xf718d37d…` and dust from the Relay solver.
- Same-address-across-chains is the fingerprint of a professional multi-chain shop. This is what ties the actors together, far more than the shared gas venue.

## The second hook

- **Hook `0xe337e5c4…`** — creator `0xe1a72d04f750be30492089823187e74cc045c5bc` (a throwaway). A second hook operating on the same token; its operator is unidentified.

## Subpoena targets (for the civil case)

1. **Robinhood chain / the gas venue behind `0x53091256…`** — if that's Robinhood's own hot wallet, the operators have KYC'd accounts behind it.
2. **The Relay bridge** (`0xf70da978…` solver) — off-ramp/on-ramp identity for the funding hub.
3. **Deployer EOAs** `0xeb33b04b…`, `0x818aa60a…` — the humans behind the multi-chain contracts.

## Data files (scratchpad)

`cluster_pools.json` (per-bot pools/inits/usdg_net_by_token), `refiller_txs.json` + `refiller_summary.json` (full 146k-tx walk), `cluster_etherscan.json`, `bytecode/raw.json` (runtime + creation inputs — for *characterizing* the contracts).
