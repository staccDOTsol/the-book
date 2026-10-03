#!/usr/bin/env python3
"""Count Pons V2 launches and graduations in the RH 24-hour replay window.

Uses public RPC logs and block timestamps only. Writes aggregate counts rather
than raw launch logs, which are much larger than the result.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from rig_monitor import RPC_URL, Rpc


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "forensics/results/pons-v2-launch-cadence-24h.json"
FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
POOL_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"
START_BLOCK = 77_940_490
END_BLOCK = 78_796_050


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def event_logs(rpc: Rpc) -> list[dict]:
    output = []
    for start in range(START_BLOCK, END_BLOCK + 1, 90_000):
        end = min(END_BLOCK, start + 89_999)
        output.extend(rpc.call("eth_getLogs", [{
            "address": FACTORY,
            "topics": [[TOKEN_LAUNCHED, POOL_GRADUATED]],
            "fromBlock": hex(start),
            "toBlock": hex(end),
        }]))
    return output


def build(logs: list[dict], start_time: int, end_time: int) -> dict:
    launches = [row for row in logs if row["topics"][0] == TOKEN_LAUNCHED]
    graduations = [row for row in logs if row["topics"][0] == POOL_GRADUATED]
    launch_tokens = {row["topics"][1].lower() for row in launches}
    graduation_tokens = {row["topics"][1].lower() for row in graduations}
    if len(launch_tokens) != len(launches) or len(graduation_tokens) != len(graduations):
        raise ValueError("Duplicate token launch or graduation event")
    if end_time - start_time != 86_400:
        raise ValueError("Replay window is not exactly 24 hours")

    pairs = Counter()
    configs = Counter()
    for row in launches:
        data = row["data"][2:]
        pairs["0x" + data[24:64].lower()] += 1
        configs[str(int(data[64:128], 16))] += 1

    same_cohort = len(launch_tokens & graduation_tokens)
    return {
        "schemaVersion": 1,
        "source": "Robinhood Chain public JSON-RPC eth_getLogs and eth_getBlockByNumber",
        "factory": FACTORY,
        "topics": {"TokenLaunched": TOKEN_LAUNCHED, "PoolGraduated": POOL_GRADUATED},
        "window": {"fromBlock": START_BLOCK, "toBlock": END_BLOCK,
                   "fromUtc": iso(start_time), "toUtc": iso(end_time),
                   "inclusiveBlockRange": True},
        "launches": len(launches),
        "launchesPerMinute": len(launches) / 1440,
        "pairTokenSummary": {
            "nativeEth": pairs["0x" + "0" * 40],
            "usdg": pairs[USDG],
            "otherErc20": len(launches) - pairs["0x" + "0" * 40] - pairs[USDG],
        },
        "pairTokenCounts": dict(sorted(pairs.items())),
        "launchConfigIdCounts": dict(sorted(configs.items(), key=lambda item: int(item[0]))),
        "graduationsInWindow": len(graduations),
        "graduationsOfSameWindowLaunches": same_cohort,
        "graduationsOfEarlierLaunches": len(graduation_tokens - launch_tokens),
        "sameWindowLaunchGraduationFraction": same_cohort / len(launches),
        "sameWindowFractionCaveat": "Right-censored: launches near window end may graduate later; not an eventual graduation probability.",
    }


def main() -> None:
    rpc = Rpc(RPC_URL)
    logs = event_logs(rpc)
    result = build(logs, rpc.block_time(START_BLOCK), rpc.block_time(END_BLOCK))
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "launches": result["launches"],
                      "graduations": result["graduationsInWindow"]}))


if __name__ == "__main__":
    main()
