#!/usr/bin/env python3
"""Cache read-only RH v4 events for 4/300 and 5/600 birth-signal pools."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

from rig_monitor import MODIFY_LIQUIDITY, POOL_MANAGER, RPC_URL, Rpc
from high_fee_pool_screen import SWAP_TOPIC, atomic_write, minimal_event
from v4_protocol_fee_join import PROTOCOL_UPDATED


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / ".local/threshold-matrix-24h.json"
INITIALIZE = ROOT / ".local/all-v4-init-24h-with-warmup.json"
OUTPUT = ROOT / ".local/burst-v4-pool-events-24h.json"
SUPPLEMENTAL = ROOT / ".local/burst-v4-supplemental-events-24h.json"
DECODED = ROOT / ".local/all-v4-births-24h-with-warmup.json"
STRICT = ROOT / ".local/burst-zero-hook-4-300-5-600-targets.json"
FOLLOW_END_BLOCK = 78_740_000  # Beyond latest selected signal +30m.
STEP = 100_000


def fetch_split(rpc: Rpc, topic: str, ids: list[str], first: int, last: int) -> list[dict]:
    try:
        return rpc.call("eth_getLogs", [{"address": POOL_MANAGER,
                                         "topics": [topic, ids],
                                         "fromBlock": hex(first),
                                         "toBlock": hex(last)}])
    except RuntimeError:
        if first == last:
            raise
        middle = (first + last) // 2
        return (fetch_split(rpc, topic, ids, first, middle)
                + fetch_split(rpc, topic, ids, middle + 1, last))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supplemental", action="store_true",
                        help="fetch zero-hook pool IDs missing from the base 114-ID cache")
    args = parser.parse_args()
    matrix = json.loads(MATRIX.read_text())
    if args.supplemental:
        decoded = json.loads(DECODED.read_text())["births"]
        by_token = defaultdict(list)
        for birth in decoded:
            if (birth["token"] and birth["quoteAsset"]
                    and int(birth["hooks"], 16) == 0
                    and birth["feePips"] != 8_388_608):
                by_token[birth["token"]].append(birth)
        primary = {birth["poolId"] for rule in ("4/300", "5/600")
                   for signal in matrix["cells"][rule]["candidates"]
                   for birth in by_token[signal["token"]]
                   if birth["birthBlock"] <= signal["entryBlock"]}
        strict = set(json.loads(STRICT.read_text())["poolIdsAtSignals"])
        existing = set(json.loads(OUTPUT.read_text())["poolIds"])
        ids = sorted((primary | strict) - existing)
        output = SUPPLEMENTAL
    else:
        ids = sorted({pool_id for rule in ("4/300", "5/600")
                      for candidate in matrix["cells"][rule]["candidates"]
                      for pool_id in candidate["poolIds"]})
        output = OUTPUT
    birth_logs = json.loads(INITIALIZE.read_text())["logs"]
    births = {log["topics"][1].lower(): log for log in birth_logs
              if log["topics"][1].lower() in ids}
    if len(births) != len(ids):
        raise ValueError("Missing selected pool Initialize logs")
    start = min(int(log["blockNumber"], 16) for log in births.values())
    if output.exists():
        state = json.loads(output.read_text())
        if state["poolIds"] != ids or state["fromBlock"] != start:
            raise ValueError("Existing event cache has a different cohort")
    else:
        state = {"poolIds": ids, "fromBlock": start,
                 "followEndBlock": FOLLOW_END_BLOCK,
                 "completedUntilBlock": start - 1,
                 "lpLogs": [], "swapLogs": [], "protocolFeeLogs": []}
    rpc = Rpc(RPC_URL)
    first = state["completedUntilBlock"] + 1
    while first <= FOLLOW_END_BLOCK:
        last = min(first + STEP - 1, FOLLOW_END_BLOCK)
        state["lpLogs"].extend(minimal_event(log) for log in
                               fetch_split(rpc, MODIFY_LIQUIDITY, ids, first, last))
        state["swapLogs"].extend(minimal_event(log) for log in
                                 fetch_split(rpc, SWAP_TOPIC, ids, first, last))
        state["protocolFeeLogs"].extend(minimal_event(log) for log in
                                        fetch_split(rpc, PROTOCOL_UPDATED, ids, first, last))
        state["completedUntilBlock"] = last
        atomic_write(output, state)
        print(json.dumps({"completedUntilBlock": last,
                          "lp": len(state["lpLogs"]),
                          "swap": len(state["swapLogs"]),
                          "protocol": len(state["protocolFeeLogs"])}), flush=True)
        first = last + 1


if __name__ == "__main__":
    main()
