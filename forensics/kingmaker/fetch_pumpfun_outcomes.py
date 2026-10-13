#!/usr/bin/env python3
"""Fetch pump.fun coins-v2 outcome fields (complete, ATH) for coins first seen at age <= 5 min.

Input: ~/callouts/data/snaps/2026-10-12-v2.jsonl. Output: .local/kingmaker/pumpfun-outcomes.jsonl.
Read-only public GETs, the same endpoint family the callouts collector already used.
"""
import concurrent.futures as cf
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPS = Path(os.path.expanduser("~/callouts/data/snaps/2026-10-12-v2.jsonl"))
OUT = ROOT / ".local/kingmaker/pumpfun-outcomes.jsonl"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
HDR = {"origin": "https://pump.fun", "user-agent": UA, "accept": "application/json"}
KEEP = ("mint", "complete", "ath_market_cap", "ath_market_cap_timestamp", "created_timestamp",
        "usd_market_cap", "last_trade_timestamp", "creator", "pool_address", "program")


def fetch(mint):
    for i in range(4):
        try:
            req = urllib.request.Request(f"https://frontend-api-v3.pump.fun/coins-v2/{mint}", headers=HDR)
            d = json.load(urllib.request.urlopen(req, timeout=20))
            return {k: d.get(k) for k in KEEP} | {"fetchedAt": int(time.time())}
        except Exception as exc:
            err = str(exc)
            time.sleep(1.5 * (i + 1))
    return {"mint": mint, "error": err, "fetchedAt": int(time.time())}


def main():
    seen = {}
    with SNAPS.open() as f:
        for line in f:
            r = json.loads(line)
            seen.setdefault(r["mint"], r["age_s"])
    young = [m for m, age in seen.items() if age <= 300]
    done = set()
    if OUT.exists():
        done = {json.loads(l)["mint"] for l in OUT.open() if '"error"' not in l}
    todo = [m for m in young if m not in done]
    print(f"young {len(young)}; to fetch {len(todo)}", flush=True)
    with OUT.open("a") as out, cf.ThreadPoolExecutor(10) as ex:
        for i, row in enumerate(ex.map(fetch, todo), 1):
            out.write(json.dumps(row) + "\n")
            if i % 500 == 0:
                print(i, flush=True)


if __name__ == "__main__":
    main()
