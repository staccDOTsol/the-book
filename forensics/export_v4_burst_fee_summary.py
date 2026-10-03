#!/usr/bin/env python3
"""Export a compact tracked audit of the lower-fee strict funded LP replay."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".local/v4-burst-fee-capital-24h.json"
OUTPUT = ROOT / "forensics/results/v4-burst-low-fee-strict-summary-24h.json"


def build(source: dict) -> dict:
    rules = ("4/300", "5/600")
    summaries = {rule: source["summaries"][f"strict_n_funded_births:{rule}"]
                 for rule in rules}
    rows = []
    for row in source["rows"]:
        if row["signalSource"] != "strict_n_funded_births" or row["rule"] not in rules:
            continue
        if not 0 <= row["feePips"] < 700_000:
            continue
        out = row["outcomes"]["4"]
        if out["status"] != "shadow_position":
            continue
        principal = Decimal(out["principalExitSpotMarkUsd"])
        fee = Decimal(out["feeExitSpotMarkUsd"])
        rows.append({
            "rule": row["rule"], "token": row["token"], "poolId": row["poolId"],
            "signalBlock": row["signalBlock"], "feePips": row["feePips"],
            "quoteAsset": row["quoteAsset"], "activated": out["activated"],
            "exitReason": out["exitReason"],
            "exitObservationBlock": out["exitObservationBlock"],
            "withdrawEarliestBlock": out["withdrawEarliestBlock"],
            "principalExitSpotMarkUsd": str(principal),
            "feeExitSpotMarkUsd": str(fee),
            "totalExitSpotMarkUsd": str(principal + fee),
            "feeSwapCount": out["feeSwapCount"],
        })
    rows.sort(key=lambda row: (row["rule"], row["signalBlock"], row["poolId"]))
    return {
        "schemaVersion": 1,
        "source": source["source"],
        "capitalModel": source["capitalModel"],
        "signalTiming": source["signalTiming"],
        "ethUsdAssumption": source["ethUsdAssumption"],
        "subset": "strict funded 4/300 and 5/600; zero-hook 0<=fee<70%; $4 shadow positions",
        "strictSummaries": summaries,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = build(json.loads(args.source.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({"rows": len(result["rows"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
