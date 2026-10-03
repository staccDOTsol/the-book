#!/usr/bin/env python3
"""Compare observed quote-only v4 pool activity by static-fee cutoff.

The two read-only screens cover disjoint 50–<70% and 70–<100% pools in the
same birth window. The cash envelope is a gas hurdle, not an LP profit model.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
from statistics import median

from export_high_fee_screen import classify_quote_band_30m
from v4_firehose_cash_envelope import cash_envelope, observed_range_state, quote_only_band


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = (
    ROOT / ".local" / "high-fee-50to70-screen.json",
    ROOT / ".local" / "high-fee-pool-screen-24h.json",
)
DEFAULT_OUTPUT = ROOT / "forensics" / "results" / "v4-fee-threshold-screen-24h.csv"
THRESHOLDS = (500_000, 700_000, 900_000, 990_000)


def summarize(sources: list[dict]) -> list[dict]:
    if not sources:
        raise ValueError("At least one screen is required")
    sample = sources[0]
    keys = ("chainId", "poolManager", "fromBlock", "toBirthBlock", "fromTime", "toBirthTime")
    if any(tuple(source[key] for key in keys) != tuple(sample[key] for key in keys)
           for source in sources[1:]):
        raise ValueError("Screens must cover the same chain and birth window")
    pools = [pool for source in sources for pool in source["pools"]]
    if len({pool["poolId"] for pool in pools}) != len(pools):
        raise ValueError("Screens contain duplicate pool IDs")
    if any(int(pool["feePips"]) >= 1_000_000 or int(pool["feePips"]) < 500_000 or
           int(pool["hooks"], 16) != 0 for pool in pools):
        raise ValueError("Expected zero-hook static-fee pools in [50%,100%)")
    birth_start = min(pool["birthBlock"] for pool in pools)
    birth_end = max(pool["birthBlock"] for pool in pools)
    if birth_start < sample["fromBlock"] or birth_end > sample["toBirthBlock"]:
        raise ValueError("Pool outside stated birth window")
    from datetime import datetime
    start = int(datetime.fromisoformat(sample["fromTime"].replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(sample["toBirthTime"].replace("Z", "+00:00")).timestamp())
    if end - start != 86_400:
        raise ValueError("Expected exact 24-hour sample")
    rows: list[dict] = []
    for threshold in THRESHOLDS:
        cohort = [pool for pool in pools if int(pool["feePips"]) >= threshold]
        quote = [pool for pool in cohort if pool["quoteAsset"]]
        legal = [pool for pool in quote if quote_only_band(pool) is not None]
        legal.sort(key=lambda pool: (pool["entryTimestamp"], pool["entryBlock"], pool["poolId"]))
        states = {pool["poolId"]: observed_range_state(pool, 30) for pool in legal}
        path_classes = Counter(classify_quote_band_30m(pool, quote_only_band(pool))[0]
                               for pool in legal)
        crossed = sum(value.startswith("interior_crossed_") for value in path_classes.elements())
        if crossed != sum(state.startswith("crossed_") for state in states.values()):
            raise AssertionError("Independent 30-minute crossing classifications disagree")
        hourly = [0] * 24
        for pool in legal:
            if not start <= pool["entryTimestamp"] <= end:
                raise ValueError("Entry outside stated sample")
            hourly[min(23, (pool["entryTimestamp"] - start) // 3600)] += 1
        envelope = cash_envelope(legal, states, 30, .25, 104.0) if legal else None
        rows.append({
            "minStaticFeePips": threshold,
            "minStaticFeePercent": threshold / 10_000,
            "zeroHookBirths": len(cohort),
            "quotePairBirths": len(quote),
            "legalAdjacentQuoteOnlyEntries": len(legal),
            "noSwap30m": path_classes["no_swap"],
            "swapNoBoundary30m": path_classes["swap_no_boundary"],
            "boundaryOnly30m": path_classes["boundary_only"],
            "interiorCrossed30m": crossed,
            "crossedEndTokenOnly30m": path_classes["interior_crossed_end_token_only"],
            "crossedEndMixed30m": path_classes["interior_crossed_end_mixed"],
            "crossedEndQuoteOnly30m": path_classes["interior_crossed_end_quote_only"],
            "arrivalsPerHourAverage": len(legal) / 24,
            "arrivalsPerHourMinimum": min(hourly),
            "arrivalsPerHourMedian": median(hourly),
            "arrivalsPerHourMaximum": max(hourly),
            "peakOpenAt30m": envelope["peakOpenPositions"] if envelope else 0,
            "medianStakeUsdAt25PctCash30m": envelope["stakeUsdMedian"] if envelope else None,
            "crossingStakeUsdAt25PctCash30m": envelope["crossingStakeUsd"] if envelope else None,
            "gasOnlyBreakEvenGrossReturnAt25PctCashAnd3cGas":
                envelope["gasOnlyBreakEvenGrossReturnOnCrossingStake"]["0.03"] if envelope else None,
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", dest="inputs")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    sources = [json.loads(path.read_text()) for path in (args.inputs or DEFAULT_INPUTS)]
    rows = summarize(sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"output": str(args.output), "rows": len(rows),
                      "birthCounts": [row["zeroHookBirths"] for row in rows]}))


if __name__ == "__main__":
    main()
