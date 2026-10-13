#!/usr/bin/env python3
"""Read graduationThreshold() from every traded curve in KINGMAKER_DATA/pons-launches.json.
(For collections made before collect_pons.py captured it.)"""
import concurrent.futures as cf
import json
import time
import urllib.request

from eth_hash.auto import keccak

import analyze_pons as ap
from collect_pons import RPC

SEL = "0x" + keccak(b"graduationThreshold()").hex()[:8]


def batch(chunk):
    reqs = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": l["curve"], "data": SEL}, "latest"]}
            for i, l in enumerate(chunk)]
    for k in range(6):
        try:
            req = urllib.request.Request(RPC, data=json.dumps(reqs).encode(), headers={
                "content-type": "application/json", "user-agent": "kingmaker-test/1"})
            res = {x["id"]: x for x in json.load(urllib.request.urlopen(req, timeout=60))}
            return [(l, int(res[i]["result"], 16)) for i, l in enumerate(chunk)]
        except Exception:
            time.sleep(1 + k)
    raise RuntimeError("batch failed")


def main():
    path = ap.DATA / "pons-launches.json"
    data = json.loads(path.read_text())
    todo = [l for l in data["launches"] if "feeBps" in l and "graduationThresholdCall" not in l]
    with cf.ThreadPoolExecutor(8) as ex:
        for part in ex.map(batch, [todo[i:i + 50] for i in range(0, len(todo), 50)]):
            for l, v in part:
                l["graduationThresholdCall"] = v
    path.write_text(json.dumps(data))
    print(f"filled {len(todo)} curves")


if __name__ == "__main__":
    main()
