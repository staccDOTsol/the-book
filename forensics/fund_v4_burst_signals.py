#!/usr/bin/env python3
"""Join v4 burst births to completed-block positive-net-LP evidence.

This strict alternate trigger requires n distinct pools whose Initialize times
fall within the rule window and whose own net liquidity is positive at the
completed signal block. Hooked pools may count toward the signal; destination
eligibility is a separate field. The historical event fetch is bounded to the
first 600 seconds after each token's first raw 4/300 or 5/600 signal.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from high_fee_pool_screen import signed_word, words


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / ".local/burst-static-signals-safe-lp-targets-24h.json"
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
LP_FILES = (ROOT / ".local/burst-v4-pool-events-24h.json",
            ROOT / ".local/burst-v4-supplemental-events-24h.json",
            ROOT / ".local/burst-hooked-signal-evidence-lp.json",
            ROOT / ".local/burst-additional-lp-23.json")
BLOCK_TIMES = ROOT / ".local/high-fee-block-times-24h.json"
OUTPUT = ROOT / ".local/v4-burst-strict-funded-signals-24h.json"


def lp_delta(log: dict) -> int:
    return signed_word(words(log["data"], 4)[2])


def build(targets: dict, births: dict, logs: list[dict], block_times: dict[int, int]) -> dict:
    raw_signals = targets["signals"]
    first_raw_time = {}
    for signal in raw_signals:
        token = signal["token"]
        first_raw_time[token] = min(first_raw_time.get(token, 10**20), signal["entryTimestamp"])
    by_token_births: dict[str, list[dict]] = defaultdict(list)
    by_pool_birth = {}
    for birth in births["births"]:
        token = birth["token"]
        if (token not in first_raw_time or birth["quoteAsset"] is None or
                birth["feePips"] >= 1_000_000 or
                birth["birthTimestamp"] > first_raw_time[token] + 600):
            continue
        by_token_births[token].append(birth)
        by_pool_birth[birth["poolId"]] = birth
    by_pool_block_delta: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    seen_logs = set()
    for log in logs:
        pool_id = log["poolId"]
        if pool_id not in by_pool_birth:
            continue
        key = (pool_id, log["block"], log["transactionIndex"], log["logIndex"])
        if key in seen_logs:
            continue
        seen_logs.add(key)
        by_pool_block_delta[pool_id][log["block"]] += lp_delta(log)
    pool_ids_with_evidence = {pool_id for pool_id in by_pool_birth
                              if pool_id in by_pool_block_delta}
    missing_pool_ids = sorted(set(by_pool_birth) - pool_ids_with_evidence)
    # A pool can have zero LP logs; every relevant pool must still have been in
    # a queried input cohort. Verify that separately via LP_FILES poolIds.
    queried_ids = set()
    for path in LP_FILES:
        data = json.loads(path.read_text())
        queried_ids.update(data.get("poolIds", [data["poolId"]] if "poolId" in data else []))
    unqueried = sorted(set(by_pool_birth) - queried_ids)
    if unqueried:
        raise ValueError(f"Missing LP scan for {len(unqueried)} relevant pools")
    signals = []
    for raw in raw_signals:
        token, rule = raw["token"], raw["rule"]
        n, window = map(int, rule.split("/"))
        cutoff = first_raw_time[token] + 600
        token_births = by_token_births[token]
        candidate_blocks = {birth["birthBlock"] for birth in token_births}
        for birth in token_births:
            candidate_blocks.update(by_pool_block_delta[birth["poolId"]])
        net_by_pool = defaultdict(int)
        first = None
        for block in sorted(candidate_blocks):
            timestamp = block_times[block]
            if timestamp > cutoff:
                break
            for birth in token_births:
                net_by_pool[birth["poolId"]] += by_pool_block_delta[birth["poolId"]].get(block, 0)
            recent_funded = [birth for birth in token_births
                             if birth["birthBlock"] <= block and
                             0 <= timestamp - birth["birthTimestamp"] <= window and
                             net_by_pool[birth["poolId"]] > 0]
            if len(recent_funded) < n:
                continue
            if block < births["birthFromBlock"]:
                first = {"status": "left_truncated_funded_signal"}
                break
            safe_known = [birth for birth in token_births
                          if birth["birthBlock"] <= block and int(birth["hooks"], 16) == 0]
            safe_funded = [birth for birth in safe_known if net_by_pool[birth["poolId"]] > 0]
            first = {"status": "funded_signal", "entryBlock": block,
                     "entryTimestamp": timestamp,
                     "fundedWindowPoolIds": [birth["poolId"] for birth in recent_funded],
                     "fundedWindowPoolCount": len(recent_funded),
                     "hookedFundedWindowPoolIds": [birth["poolId"] for birth in recent_funded
                                                    if int(birth["hooks"], 16) != 0],
                     "safePoolIdsKnownAtSignal": [birth["poolId"] for birth in safe_known],
                     "safeFundedPoolIdsAtSignal": [birth["poolId"] for birth in safe_funded],
                     "safeLowerFeeFundedPoolIdsAtSignal": [birth["poolId"] for birth in safe_funded
                                                            if birth["feePips"] < 700_000]}
            break
        signals.append({"rule": rule, "token": token,
                        "rawBirthSignalBlock": raw["entryBlock"],
                        "rawBirthSignalTimestamp": raw["entryTimestamp"],
                        "funded": first or {"status": "no_strict_funded_signal_within_first_600s"},
                        "observationUntilTimestamp": cutoff})
    return {"schemaVersion": 1, "rules": ["4/300", "5/600"],
            "definition": "Earliest completed block within 600s after first raw signal with >=n positive-net-LP pools whose Initialize timestamps fall within the last rule-window seconds; hooked pools may count, but safe destinations are zero-hook.",
            "rawSignalRows": len(raw_signals),
            "relevantPools": len(by_pool_birth), "lpLogsUsed": len(seen_logs),
            "poolsQueriedWithNoLpEvent": len(missing_pool_ids),
            "fundedRows": sum(row["funded"]["status"] == "funded_signal" for row in signals),
            "fundedTokens": len({row["token"] for row in signals
                                if row["funded"]["status"] == "funded_signal"}),
            "statusCounts": dict(Counter(row["funded"]["status"] for row in signals)),
            "signals": signals}


def main() -> None:
    targets = json.loads(TARGETS.read_text())
    births = json.loads(BIRTHS.read_text())
    raw = [log for path in LP_FILES for log in json.loads(path.read_text())["lpLogs"]]
    block_times = {int(block): int(timestamp) for block, timestamp in
                   json.loads(BLOCK_TIMES.read_text()).items()}
    result = build(targets, births, raw, block_times)
    OUTPUT.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(OUTPUT), "rawRows": result["rawSignalRows"],
                      "fundedRows": result["fundedRows"],
                      "fundedTokens": result["fundedTokens"],
                      "statusCounts": result["statusCounts"]}))


if __name__ == "__main__":
    main()
