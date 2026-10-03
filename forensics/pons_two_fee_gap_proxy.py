#!/usr/bin/env python3
"""Descriptive RH 24h two-venue spot-gap proxy for Pons-plus-v4 arbitrage.

For strict funded 4/300 and 5/600 signals, compare the completed-block spot
price of each Pons-hooked X/quote pool with each separate zero-hook X/quote
pool that passed the existing quote-only LP-destination screen. Pool prices
are carried forward across archived Swap logs for 30 minutes.
This is *not* a quote, executable arbitrage, volume, or profit backtest. It
deliberately does not invent a historical X/NOTHINGBURGER venue, which cannot
exist until the user's proposed token is launched and funded.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
REPLAY = ROOT / ".local/v4-burst-fee-capital-24h.json"
CACHES = (
    ROOT / ".local/burst-v4-pool-events-24h.json",
    ROOT / ".local/burst-v4-supplemental-events-24h.json",
    ROOT / ".local/burst-hooked-signal-evidence-events.json",
)
OUT = ROOT / "forensics/results/pons-two-fee-gap-proxy-24h.json"
Q96 = 2**96
TAX_BPS = tuple(range(0, 1001, 100))
POOL_FEE_BPS = (100, 700, 2000, 5000, 7000)
PONS_BASE_BPS = 100
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"


def log_quote_per_token(sqrt_price: int, quote_side: int) -> float:
    if sqrt_price <= 0:
        raise ValueError("Uninitialized price")
    raw = 2 * (math.log(sqrt_price) - math.log(Q96))
    return -raw if quote_side == 0 else raw


def log_fee_hurdle(pons_bps: int, pool_bps: int) -> float:
    if not 0 <= pons_bps < 10000 or not 0 <= pool_bps < 10000:
        raise ValueError("Fee outside [0,100%)")
    return -math.log1p(-pons_bps / 10000) - math.log1p(-pool_bps / 10000)


def load_swap_prices() -> dict[str, list[tuple[int, int]]]:
    by_pool: dict[str, dict[int, int]] = defaultdict(dict)
    seen = set()
    for path in CACHES:
        source = json.loads(path.read_text())
        overlap = seen.intersection(source["poolIds"])
        if overlap:
            raise ValueError(f"Overlapping cache pool IDs: {path}")
        seen.update(source["poolIds"])
        for row in sorted(source["swapLogs"], key=lambda x: (x["block"], x["transactionIndex"], x["logIndex"])):
            word = row["data"][2 + 2 * 64:2 + 3 * 64]
            by_pool[row["poolId"]][row["block"]] = int(word, 16)
    return {pool_id: sorted(blocks.items()) for pool_id, blocks in by_pool.items()}


def path_summary(hook: dict, independent: dict, hook_side: int, independent_side: int,
                 prices: dict[str, list[tuple[int, int]]]) -> dict:
    if hook["signalBlock"] != independent["signalBlock"] or hook["timeCapBlock"] != independent["timeCapBlock"]:
        raise ValueError("Unaligned signal and horizon")
    start, end = hook["signalBlock"], hook["timeCapBlock"]
    h_price = log_quote_per_token(int(hook["signalSqrtPriceX96"]), hook_side)
    i_price = log_quote_per_token(int(independent["signalSqrtPriceX96"]), independent_side)
    h_post = [(block, sqrt) for block, sqrt in prices.get(hook["poolId"], []) if start < block <= end]
    i_post = [(block, sqrt) for block, sqrt in prices.get(independent["poolId"], []) if start < block <= end]
    observation_blocks = sorted({start, *(x[0] for x in h_post), *(x[0] for x in i_post)})
    h_updates, i_updates = dict(h_post), dict(i_post)
    start_gap = abs(h_price - i_price)
    max_gap = start_gap
    for block in observation_blocks[1:]:
        if block in h_updates:
            h_price = log_quote_per_token(h_updates[block], hook_side)
        if block in i_updates:
            i_price = log_quote_per_token(i_updates[block], independent_side)
        max_gap = max(max_gap, abs(h_price - i_price))
    return {
        "rule": hook["rule"], "token": hook["token"],
        "signalBlock": start, "timeCapBlock": end,
        "ponsPoolId": hook["poolId"], "independentPoolId": independent["poolId"],
        "independentActualFeeBps": independent["feePips"] / 100,
        "ponsPostSignalSwapBlocks": len(h_post),
        "independentPostSignalSwapBlocks": len(i_post),
        "jointObservationBlocksIncludingSignal": len(observation_blocks),
        "signalAbsoluteLogPriceGap": start_gap,
        "maxAbsoluteLogPriceGap30m": max_gap,
        "signalGapPctApprox": math.expm1(start_gap) * 100,
        "maxGapPctApprox30m": math.expm1(max_gap) * 100,
        "bothPoolsSwappedAfterSignal": bool(h_post and i_post),
        "taxActualFee": {
            str(tax): {
                "signalClearsFeeOnly": start_gap >= log_fee_hurdle(PONS_BASE_BPS + tax, independent["feePips"] / 100),
                "within30mClearsFeeOnly": max_gap >= log_fee_hurdle(PONS_BASE_BPS + tax, independent["feePips"] / 100),
            } for tax in TAX_BPS
        },
        "taxHypotheticalFeeGrid": {
            str(tax): {
                str(fee): {
                    "signalClearsFeeOnly": start_gap >= log_fee_hurdle(PONS_BASE_BPS + tax, fee),
                    "within30mClearsFeeOnly": max_gap >= log_fee_hurdle(PONS_BASE_BPS + tax, fee),
                } for fee in POOL_FEE_BPS
            } for tax in TAX_BPS
        },
    }


def main() -> None:
    birth = {row["poolId"]: row for row in json.loads(BIRTHS.read_text())["births"]}
    replay = json.loads(REPLAY.read_text())
    hooks = [row for row in replay["hookedCoreOnlyRows"]
             if row["signalSource"] == "strict_n_funded_births" and row["hook"] == PONS_HOOK]
    independent = [row for row in replay["rows"]
                   if row["signalSource"] == "strict_n_funded_births"]
    prices = load_swap_prices()
    matched = []
    for h in hooks:
        for p in independent:
            if (p["rule"], p["token"], p["quoteAsset"], p["signalBlock"]) != (
                h["rule"], h["token"], h["quoteAsset"], h["signalBlock"]
            ):
                continue
            matched.append(path_summary(h, p, birth[h["poolId"]]["quoteSide"],
                                        birth[p["poolId"]]["quoteSide"], prices))
    if len({(row["rule"], row["token"], row["independentPoolId"]) for row in matched}) != len(matched):
        raise ValueError("Duplicated pair")
    counts = {}
    for rule in ("4/300", "5/600"):
        subset = [row for row in matched if row["rule"] == rule]
        for active in (False, True):
            filtered = [row for row in subset if not active or row["bothPoolsSwappedAfterSignal"]]
            label = f"{rule}:{'both_swapped' if active else 'all_funded_pairs'}"
            counts[label] = {
                "pairs": len(filtered), "tokens": len({row["token"] for row in filtered}),
                "signalActualFeeCrossingPairsByTaxBps": {
                    str(tax): sum(row["taxActualFee"][str(tax)]["signalClearsFeeOnly"] for row in filtered)
                    for tax in TAX_BPS},
                "within30mActualFeeCrossingPairsByTaxBps": {
                    str(tax): sum(row["taxActualFee"][str(tax)]["within30mClearsFeeOnly"] for row in filtered)
                    for tax in TAX_BPS},
                "hypotheticalTierCrossingPairsByTaxAndFeeBps": {
                    str(tax): {
                        str(fee): sum(row["taxHypotheticalFeeGrid"][str(tax)][str(fee)]["within30mClearsFeeOnly"]
                                      for row in filtered)
                        for fee in POOL_FEE_BPS}
                    for tax in TAX_BPS},
            }
    out = {
        "schemaVersion": 1,
        "scope": "Historical Pons X/ETH or X/USDG vs independent X/same-quote pool spot paths for strict funded 4/300 and 5/600 signals; independent pools are the subset passing the existing quote-only LP destination screen; no hypothetical X/NOTHINGBURGER pool",
        "priceConvention": "Raw quote-per-token ratios; identical token and quote decimals cancel in each pair comparison. End-of-block swaps carried forward; sample at signal and every subsequent swap block within 30 minutes.",
        "feeOnlyHurdle": "abs(log(P_pons/P_independent)) >= -log(1-(basePonsBps+creatorTaxBps)/10000)-log(1-independentFeeBps/10000); either direction, zero size/gas/impact",
        "limitations": ["Spot gap is not executable depth, arb volume, or owner revenue.",
                        "Most pools have large or stale price disparities and may have tiny usable liquidity. Even both-swapped pairs do not prove simultaneous executable depth.",
                        "The proposed NOTHINGBURGER token and X/NOTHINGBURGER venue did not exist in this historical sample.",
                        "Hypothetical fee tiers reprice historical pool fees without rerunning swaps; actual-fee counts preserve each pool's fee.",
                        "The 4/300 and 5/600 rule rows overlap and should not be summed as distinct opportunities."],
        "scenario": {"ponsBaseFeeBps": PONS_BASE_BPS,
                     "creatorTaxBps": list(TAX_BPS), "hypotheticalIndependentFeeBps": list(POOL_FEE_BPS)},
        "counts": counts, "pairPaths": matched,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(OUT), "matchedPairs": len(matched), "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
