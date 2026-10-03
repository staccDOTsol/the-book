#!/usr/bin/env python3
"""List past Pons-backed funded burst tokens for a hypothetical X/NB pool.

This is candidate timing only. It does not infer profitable pool creation or
authorize an on-chain transaction. A live token address and executable route
are required before an actual X/NOTHINGBURGER market can be priced.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "forensics/results/v4-pons-signal-screen-24h.json"
OUTPUT = ROOT / "forensics/results/nothingburger-historical-pair-candidates-24h.csv"


def select(source: dict) -> list[dict]:
    pons_by_token = {pool["token"]: pool for pool in source["ponsPools"]
                     if pool["quoteEligible"]}
    earliest: dict[str, dict] = {}
    for signal in source["signals"]:
        if signal["rule"] not in ("4/300", "5/600"):
            continue
        funded = signal["funded"]
        if funded.get("status") != "funded_signal":
            continue
        token = signal["token"]
        pons = pons_by_token.get(token)
        if pons is None or pons["birthBlock"] > funded["entryBlock"]:
            continue
        if token not in earliest or funded["entryTimestamp"] < earliest[token]["entryTimestamp"]:
            earliest[token] = {
                "token": token,
                "rule": signal["rule"],
                "entryBlock": funded["entryBlock"],
                "entryTimestamp": funded["entryTimestamp"],
                "entryTimeUtc": datetime.fromtimestamp(funded["entryTimestamp"], timezone.utc).isoformat(),
                "ponsPoolId": pons["poolId"],
                "ponsBirthBlock": pons["birthBlock"],
                "secondsAfterPonsBirth": funded["entryTimestamp"] - pons["birthTimestamp"],
                "fundedPoolsAtSignal": funded["fundedWindowPoolCount"],
            }
    return sorted(earliest.values(), key=lambda row: (row["entryTimestamp"], row["token"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    rows = select(json.loads(args.source.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    max30 = max((sum(0 <= other["entryTimestamp"] - row["entryTimestamp"] < 1800
                     for other in rows) for row in rows), default=0)
    print(json.dumps({"candidates": len(rows), "firstRuleCounts": {
        rule: sum(row["rule"] == rule for row in rows) for rule in ("4/300", "5/600")},
        "maxSignalsIn30m": max30, "output": str(args.output)}))


if __name__ == "__main__":
    main()
