# Pons graduation as a pool-birth marker

The Robinhood Chain hook `0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044` is the shared **Pons V2 Meme Hook**, not a rig-operator address. [Pons lists this address as its Meme Hook](https://docs.ponsfamily.com/v2#contracts). Pons creates a Uniswap v4 pool when a launch graduates from its bonding curve. The hook accepts pools registered by the Pons factory and charges a post-swap fee for protocol, creator, and optional buyback payouts. Its pool's Uniswap core fee is zero. [Pons' pool and hook description](https://docs.ponsfamily.com/v2#uniswap-v4-pools)

The Pons graduation is useful as an observable **launchpad event**. It is not evidence that a common operator controls later independent pools, or that a Pons pool itself pays an outside LP a share of the hook fee. Pons says its graduation position is locked permanently and the hook, rather than the core pool, collects trading fees. [Pons' liquidity and fee mechanics](https://docs.ponsfamily.com/v2#uniswap-v4-pools)

## Effect on the burst screen

The archived Robinhood Chain `Initialize` sample covers roughly 2026-10-02 04:18 through 2026-10-03 04:17 UTC, with a ten-minute warmup. Pools are counted at the end of their birth block. The signal screen includes quote-pair, static-fee pools; `4/300` means four distinct pools for one token within 300 seconds, and `5/600` means five within 600 seconds. These are birth-only counts, before checking positive deposited liquidity.

| Pools allowed to count toward a signal | Quote-pair births including warmup | `4/300` tokens | `5/600` tokens |
| --- | ---: | ---: | ---: |
| No hook | 3,155 | 16 | 14 |
| No hook or Pons hook | 3,202 | 23 | 23 |
| Any hook | 3,568 | 23 | 23 |

The **no-hook-or-Pons** rule recovers the larger 4–5-pool signal universe while excluding unrelated hooks. This changes which births are evidence for the signal; eligibility of an actual LP destination is a separate decision. The Pons pool has zero core fee, so its large swap count cannot be credited to an outsider's LP fee growth.

Pons had 52 distinct registered pools/tokens in the 24-hour birth file. Its `PoolRegistered` events identify all 52; the generic ETH/USDG quote decoder identifies 47 because five use other quote assets. Receipt checks found positive net `ModifyLiquidity` in the birth transaction of all 47 quote-eligible Pons pools, so these particular births were funded. Among the 23 distinct tokens with a strictly funded `4/300` or `5/600` burst, 20 had a Pons registration, including one custom-pair launch. The Pons birth therefore often precedes the completed burst, but it also marks launches that never meet this burst rule. Among the 47 ETH/USDG Pons tokens, an independent positive-fee, no-hook quote pool existed before the Pons birth in 20 cases, in the same second in one, after it in 25, and never in one. This prevents treating graduation as the universally earliest entry signal.

The [birth-block funding audit](../results/pons-birth-cadence-24h.json) retains the 47 receipt checks and rule counts. The [Pons signal screen](../results/v4-pons-signal-screen-24h.json) joins launch registrations, burst timing, and strict funded signals.

## LP economics still to prove

The relevant LP destinations are separate fee-charging pools. Their historical core fee share must be compared with position conversion, an executable route back to quote asset, and mint/burn/liquidation gas. Pons hook fees belong to the launchpad distribution logic, not the independent LP. The [lower-fee replay](../v4_burst_fee_capital.py) and local historical-block route forks are used for that comparison; no positive executable strategy result is established by the Pons label alone.

A local historical-block fork confirmed the mechanical distinction in one Pons pool. An outside wallet successfully minted and burned a quote-only position around a signed historical swap. The pool's LP fee growth stayed zero while `pendingFees(ETH)` on the hook rose by 817,016,064,910,826 wei. The immediate burn plus same-pool token sale returned 0.000998510463526037 ETH against 0.001 ETH deposited, before gas. This is a one-swap counterfactual, not a complete exit-policy replay. It shows that outsider access to a Pons pool does not imply a share of the hook's 1% fee.
