#!/usr/bin/env python3
"""Full event history of every v4 side pool on a window launch token: Initialize (opening price),
ModifyLiquidity (ticks, delta, salt), Swap (amounts, post price, active liquidity, fee).
Writes .local/kingmaker/side-events.jsonl (one pool per line, events in chain order)."""
from __future__ import annotations

import concurrent.futures as cf
import json

from eth_hash.auto import keccak

from collect_pons import call, get_logs
from collect_side_lp import DATA, INITIALIZE, MODIFY, POOL_MANAGER, signed

SWAP = "0x" + keccak(b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()


def words(data: str) -> list[str]:
    return [data[2 + 64 * i: 2 + 64 * (i + 1)] for i in range((len(data) - 2) // 64)]


def main():
    side = json.loads((DATA / "side-lp.json").read_text())
    pools = {k: v for k, v in side["pools"].items() if v["v"] == 4}
    head = int(call("eth_blockNumber", []), 16)
    ids = sorted(pools, key=lambda k: pools[k]["block"])
    groups = [ids[i:i + 600] for i in range(0, len(ids), 600)]

    def pull(group):
        start = min(pools[k]["block"] for k in group)
        return get_logs(POOL_MANAGER, [[INITIALIZE, MODIFY, SWAP], group], start, head, chunk=50_000)

    events = {k: [] for k in ids}
    with cf.ThreadPoolExecutor(3) as ex:
        for n, logs in enumerate(ex.map(pull, groups), 1):
            for lg in logs:
                pid, w = lg["topics"][1].lower(), words(lg["data"])
                ev = {"block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"],
                      "txIndex": int(lg["transactionIndex"], 16), "logIndex": int(lg["logIndex"], 16),
                      "ts": int(lg["blockTimestamp"], 16)}
                t0 = lg["topics"][0]
                if t0 == INITIALIZE:
                    ev.update(kind="init", sqrtP=str(int(w[3], 16)), tick=signed(w[4]))
                elif t0 == MODIFY:
                    ev.update(kind="lp", sender="0x" + lg["topics"][2][-40:], tickLower=signed(w[0]),
                              tickUpper=signed(w[1]), delta=str(signed(w[2])), salt="0x" + w[3])
                else:
                    ev.update(kind="swap", sender="0x" + lg["topics"][2][-40:], a0=str(signed(w[0])),
                              a1=str(signed(w[1])), sqrtP=str(int(w[2], 16)), L=str(int(w[3], 16)),
                              tick=signed(w[4]), fee=int(w[5], 16))
                events[pid].append(ev)
            print(f"group {n}/{len(groups)} done", flush=True)
    with (DATA / "side-events.jsonl").open("w") as f:
        for pid in ids:
            evs = sorted(events[pid], key=lambda e: (e["block"], e["txIndex"], e["logIndex"]))
            f.write(json.dumps({"pool": pid, **pools[pid], "events": evs}) + "\n")
    print(f"wrote side-events.jsonl: {len(ids)} pools, "
          f"{sum(len(v) for v in events.values())} events, swaps {sum(e['kind'] == 'swap' for v in events.values() for e in v)}",
          flush=True)


if __name__ == "__main__":
    main()
