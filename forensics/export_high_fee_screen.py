#!/usr/bin/env python3
"""Export the read-only v4 high-fee pool screen as a compact reviewable CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from v4_quote_range_screen import sqrt_price_at_tick, traverses_interior


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / ".local" / "high-fee-pool-screen-24h.json"
DEFAULT_OUTPUT = ROOT / "forensics" / "results" / "high-fee-pool-screen-24h.csv"


def classify_quote_band_30m(pool: dict, band: dict) -> tuple[str, int | None, int | None]:
    if not pool["quoteAsset"]:
        return "ineligible_non_quote_pair", None, None
    if not band:
        return "ineligible_no_legal_band", None, None
    swaps = [event for event in pool["swapEvents"]
             if event["block"] > pool["entryBlock"] and
             0 <= event["timestamp"] - pool["entryTimestamp"] <= 1800]
    if not swaps:
        return "no_swap", None, None
    lower = sqrt_price_at_tick(band["tickLower"])
    upper = sqrt_price_at_tick(band["tickUpper"])
    side = pool["quoteSide"]
    first_interior = None
    entry_edge = lower if side == 0 else upper
    pre_sqrt = int(pool["entrySqrtPriceX96"])
    boundary_touch = False
    for event in swaps:
        post_sqrt = int(event["postSqrtPriceX96"])
        # postTick can equal a boundary tick while sqrtPriceX96 is still
        # outside the band; use exact TickMath prices and the Swap path.
        if first_interior is None and traverses_interior(pre_sqrt, post_sqrt, lower, upper):
            first_interior = event
        if post_sqrt == entry_edge or pre_sqrt == entry_edge:
            boundary_touch = True
        pre_sqrt = post_sqrt
    if first_interior is None:
        return ("boundary_only" if boundary_touch else "swap_no_boundary"), None, swaps[-1]["postTick"]
    final_sqrt = int(swaps[-1]["postSqrtPriceX96"])
    if side == 0:
        end_state = "token_only" if final_sqrt >= upper else "mixed" if final_sqrt > lower else "quote_only"
    else:
        end_state = "token_only" if final_sqrt <= lower else "mixed" if final_sqrt < upper else "quote_only"
    return "interior_crossed_end_" + end_state, first_interior["block"], swaps[-1]["postTick"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    rows = []
    for pool in data["pools"]:
        seed = pool["seed"] or {}
        quote = seed.get("quoteDepositEstimate", {})
        ranges = pool["quoteOnlyOutsideTickRanges"].get("widthInTickIntervals", {})
        one_tick = ranges.get("1") or {}
        band_class, first_cross_block, end_tick = classify_quote_band_30m(pool, one_tick)
        own75 = pool["ownLiquidity"]["crossings"].get("0.75", {})
        dynamic = pool["dynamicTokenHighFeeLiquidity"]
        dynamic75 = dynamic.get("firstCrossingsObservedWithinCohort", {}).get("0.75", {})
        row = {
            "poolId": pool["poolId"], "token": pool["token"],
            "currency0": pool["currency0"], "currency1": pool["currency1"],
            "quoteAsset": pool["quoteAsset"], "quoteSide": pool["quoteSide"],
            "feePips": pool["feePips"], "tickSpacing": pool["tickSpacing"],
            "hooks": pool["hooks"], "birthBlock": pool["birthBlock"],
            "birthTimeUtc": pool["birthTime"], "birthTx": pool["birthTx"],
            "entryBlock": pool["entryBlock"], "entryTimeUtc": pool["entryTime"],
            "entryTick": pool["entryTick"],
            "seedTickLower": seed.get("tickLower"), "seedTickUpper": seed.get("tickUpper"),
            "seedLiquidityDelta": seed.get("liquidityDelta"),
            "firstCompletedBlockNetLiquidity": pool["firstFundedCompletedBlockNetLiquidity"],
            "estimatedSeedQuoteAmount": quote.get("estimatedQuoteAmount"),
            "estimatedSeedQuoteRaw": quote.get("estimatedQuoteRaw"),
            "estimatedSeedQuoteStatus": quote.get("status"),
            "legalAdjacentQuoteBand": bool(one_tick),
            "adjacentQuoteBandTickLower": one_tick.get("tickLower"),
            "adjacentQuoteBandTickUpper": one_tick.get("tickUpper"),
            "adjacentBandPath30m": band_class,
            "firstInteriorCrossBlock30m": first_cross_block,
            "lastObservedTick30m": end_tick,
            "seedRangeFullUsable": pool["seedRangeIsFullUsableRange"],
            "swaps5m": pool["swapsAfterSeed"]["300"]["count"],
            "swaps10m": pool["swapsAfterSeed"]["600"]["count"],
            "swaps30m": pool["swapsAfterSeed"]["1800"]["count"],
            "swaps60m": pool["swapsAfterSeed"]["3600"]["count"],
            "quoteInput30mRaw": pool["swapsAfterSeed"]["1800"]["quoteInputRaw"],
            "tokenInput30mRaw": pool["swapsAfterSeed"]["1800"]["tokenInputRaw"],
            "firstWithdrawalTimeUtc": (pool["firstWithdrawal"] or {}).get("time"),
            "ownPool75PctRemainingCrossTimeUtc": own75.get("time"),
            "dynamicAggregateStatus": dynamic.get("status"),
            "cohortOnlyDynamic75PctCrossTimeUtc": dynamic75.get("time"),
        }
        rows.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"output": str(args.output), "rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
