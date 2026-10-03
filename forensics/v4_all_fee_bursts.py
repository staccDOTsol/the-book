#!/usr/bin/env python3
"""Causal v4 quote-pool birth bursts across all static fee tiers.

This is a birth-only screen. It does not prove positive LP at the signal block;
that must be joined from ModifyLiquidity logs before an LP entry is modeled.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".local" / "all-v4-births-24h-with-warmup.json"
OUTPUT = ROOT / ".local" / "v4-all-fee-zero-hook-bursts-24h.json"
CSV = ROOT / "forensics" / "results" / "v4-all-fee-zero-hook-bursts-24h.csv"
TARGETS = ROOT / ".local" / "burst-static-signals-safe-lp-targets-24h.json"
TARGET_CSV = ROOT / "forensics" / "results" / "v4-static-burst-safe-lp-targets-24h.csv"
RULES = ((3, 300), (3, 600), (4, 300), (4, 600), (5, 300), (5, 600))


def eligible(birth: dict, cohort: str) -> bool:
    if birth["quoteAsset"] is None or birth["token"] is None:
        return False
    if cohort == "all_quote":
        return True
    if cohort == "static_quote":
        return birth["feePips"] < 1_000_000
    if cohort == "zero_hook_static_quote":
        return birth["feePips"] < 1_000_000 and int(birth["hooks"], 16) == 0
    raise ValueError(cohort)


def pool_ref(birth: dict) -> dict:
    return {"poolId": birth["poolId"], "feePips": birth["feePips"],
            "quoteAsset": birth["quoteAsset"], "currency0": birth["currency0"],
            "currency1": birth["currency1"], "tickSpacing": birth["tickSpacing"],
            "hooks": birth["hooks"], "birthBlock": birth["birthBlock"],
            "birthTimestamp": birth["birthTimestamp"], "birthTime": birth["birthTime"],
            "birthTx": birth["birthTx"]}


def scan_rule(births: list[dict], n: int, seconds: int, sample_start: int) -> tuple[list[dict], list[str]]:
    queues: dict[str, deque[dict]] = defaultdict(deque)
    first_seen: set[str] = set()
    prestart: set[str] = set()
    triggers = []
    for birth in births:
        token = birth["token"]
        queue = queues[token]
        while queue and birth["birthTimestamp"] - queue[0]["birthTimestamp"] > seconds:
            queue.popleft()
        queue.append(birth)
        if len(queue) < n or token in first_seen:
            continue
        first_seen.add(token)
        if birth["birthBlock"] < sample_start:
            prestart.add(token)
            continue
        triggers.append(birth)
    by_token: dict[str, list[dict]] = defaultdict(list)
    for birth in births:
        by_token[birth["token"]].append(birth)
    signals = []
    for trigger in triggers:
        token = trigger["token"]
        # The signal is usable at the end of its block; later Initialize logs
        # in that same block are then also visible, without future-block data.
        known = [birth for birth in by_token[token]
                 if birth["birthBlock"] <= trigger["birthBlock"]]
        burst = [birth for birth in known
                 if trigger["birthTimestamp"] - birth["birthTimestamp"] <= seconds]
        later = [birth for birth in by_token[token]
                 if birth["birthBlock"] > trigger["birthBlock"]]
        if len({birth["poolId"] for birth in burst}) < n:
            raise AssertionError("Incomplete completed-block signal")
        signals.append({
            "rule": f"{n}/{seconds}", "token": token,
            "entryBlock": trigger["birthBlock"],
            "entryTimestamp": trigger["birthTimestamp"], "entryTime": trigger["birthTime"],
            "signalPoolId": trigger["poolId"],
            "windowSeconds": trigger["birthTimestamp"] - burst[0]["birthTimestamp"],
            "poolIds": [birth["poolId"] for birth in known],
            "burstPoolIds": [birth["poolId"] for birth in burst],
            "lowerFeePoolIdsAtSignal": [birth["poolId"] for birth in known
                                        if birth["feePips"] < 700_000],
            "lowerFeeBurstPoolIds": [birth["poolId"] for birth in burst
                                     if birth["feePips"] < 700_000],
            "knownPoolsAtSignal": [pool_ref(birth) for birth in known],
            "burstPools": [pool_ref(birth) for birth in burst],
            "laterPoolsNotKnownAtSignal": [pool_ref(birth) for birth in later],
            "fundingStatus": "not_checked_birth_only",
        })
    return signals, sorted(prestart)


def build(source: dict) -> dict:
    sample_start = int(source["birthFromBlock"])
    ordered = sorted(source["births"], key=lambda birth: (
        birth["birthBlock"], birth["birthTransactionIndex"], birth["birthLogIndex"]))
    if len({birth["poolId"] for birth in ordered}) != len(ordered):
        raise ValueError("Duplicate Initialize pool IDs")
    cohorts = {}
    for name in ("all_quote", "static_quote", "zero_hook_static_quote"):
        births = [birth for birth in ordered if eligible(birth, name)]
        by_rule = {}
        for n, seconds in RULES:
            rule = f"{n}/{seconds}"
            signals, prestart = scan_rule(births, n, seconds, sample_start)
            by_rule[rule] = {"uniqueFirstSignalTokens": len(signals),
                             "leftTruncatedTokens": prestart, "signals": signals}
        cohorts[name] = {"birthsIncludingWarmup": len(births), "rules": by_rule}
    return {"schemaVersion": 1, "chainId": 4663,
            "rawInitializeLogsIncludingWarmup": len(ordered),
            "fromBlock": source["fromBlock"], "birthFromBlock": sample_start,
            "toBlock": source["toBlock"],
            "signalRule": "First n distinct eligible quote-pair Initialize pools in <=window seconds per token, with all same-block births visible only at completed signal block. Ten-minute warmup excludes pre-sample threshold crossings. Funding is not inferred from Initialize.",
            "cohorts": cohorts}


def write_csv(path: Path, result: dict) -> None:
    rows = []
    for rule, data in result["cohorts"]["zero_hook_static_quote"]["rules"].items():
        for signal in data["signals"]:
            rows.append({"rule": rule, "token": signal["token"],
                         "entryBlock": signal["entryBlock"],
                         "entryTimeUtc": signal["entryTime"],
                         "signalPoolId": signal["signalPoolId"],
                         "windowSeconds": signal["windowSeconds"],
                         "knownPoolCountAtSignal": len(signal["poolIds"]),
                         "burstPoolCountAtSignal": len(signal["burstPoolIds"]),
                         "lowerFeeKnownPoolCount": len(signal["lowerFeePoolIdsAtSignal"]),
                         "lowerFeeBurstPoolCount": len(signal["lowerFeeBurstPoolIds"]),
                         "poolIds": "|".join(signal["poolIds"]),
                         "lowerFeePoolIdsAtSignal": "|".join(signal["lowerFeePoolIdsAtSignal"]),
                         "fundingStatus": signal["fundingStatus"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def safe_lp_targets(result: dict) -> dict:
    """Allow hooked births as signal evidence, but never as LP destinations."""
    rules = result["cohorts"]["static_quote"]["rules"]
    signals = []
    for rule in ("4/300", "5/600"):
        for signal in rules[rule]["signals"]:
            safe = [pool for pool in signal["knownPoolsAtSignal"]
                    if int(pool["hooks"], 16) == 0]
            later_safe = [pool for pool in signal["laterPoolsNotKnownAtSignal"]
                          if int(pool["hooks"], 16) == 0]
            signals.append({
                "rule": rule, "token": signal["token"],
                "entryBlock": signal["entryBlock"],
                "entryTimestamp": signal["entryTimestamp"],
                "entryTime": signal["entryTime"],
                "signalPoolId": signal["signalPoolId"],
                "signalPoolHooked": next(pool for pool in signal["knownPoolsAtSignal"]
                                         if pool["poolId"] == signal["signalPoolId"])["hooks"] != "0x" + "0" * 40,
                "signalPoolIdsIncludingHooked": signal["poolIds"],
                "poolIds": [pool["poolId"] for pool in safe],
                "lowerFeePoolIdsAtSignal": [pool["poolId"] for pool in safe
                                            if pool["feePips"] < 700_000],
                "safePoolsAtSignal": safe,
                "laterSafePoolsNotKnownAtSignal": later_safe,
                "fundingStatus": "not_checked_birth_only",
            })
    return {"schemaVersion": 1, "signalCohort": "all static-fee quote-pair v4 pools; hooked births may form a signal",
            "lpDestinationCohort": "only zero-hook static-fee quote pools known at completed signal block",
            "rules": ["4/300", "5/600"],
            "signalRows": len(signals), "signalTokens": len({row["token"] for row in signals}),
            "safeLpPoolIdsAtSignals": sorted({pool_id for row in signals for pool_id in row["poolIds"]}),
            "signals": signals}


def write_safe_csv(path: Path, targets: dict) -> None:
    rows = []
    for signal in targets["signals"]:
        rows.append({"rule": signal["rule"], "token": signal["token"],
                     "entryBlock": signal["entryBlock"],
                     "entryTimeUtc": signal["entryTime"],
                     "signalPoolId": signal["signalPoolId"],
                     "signalBirthPoolCountIncludingHooked": len(signal["signalPoolIdsIncludingHooked"]),
                     "safeLpPoolCountAtSignal": len(signal["poolIds"]),
                     "lowerFeeSafePoolCountAtSignal": len(signal["lowerFeePoolIdsAtSignal"]),
                     "safeLpPoolIdsAtSignal": "|".join(signal["poolIds"]),
                     "lowerFeeSafePoolIdsAtSignal": "|".join(signal["lowerFeePoolIdsAtSignal"]),
                     "laterSafePoolCount": len(signal["laterSafePoolsNotKnownAtSignal"]),
                     "fundingStatus": signal["fundingStatus"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--csv", type=Path, default=CSV)
    parser.add_argument("--targets-output", type=Path, default=TARGETS)
    parser.add_argument("--targets-csv", type=Path, default=TARGET_CSV)
    args = parser.parse_args()
    result = build(json.loads(args.source.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    write_csv(args.csv, result)
    args.targets_output.parent.mkdir(parents=True, exist_ok=True)
    targets = safe_lp_targets(result)
    args.targets_output.write_text(json.dumps(targets, separators=(",", ":")) + "\n")
    write_safe_csv(args.targets_csv, targets)
    print(json.dumps({"output": str(args.output), "csv": str(args.csv),
                      "counts": {name: {rule: row["uniqueFirstSignalTokens"]
                                        for rule, row in cohort["rules"].items()}
                                 for name, cohort in result["cohorts"].items()}}))


if __name__ == "__main__":
    main()
