#!/usr/bin/env python3
"""Confirmation on launches that did not exist when the test was frozen.

Uses the frozen rig list and rules in frozen/2026-10-13.json, unchanged. Collect a fresh window
into a separate directory, then run:

  KINGMAKER_DATA=.local/kingmaker-fresh python collect_pons.py --days 7
  KINGMAKER_DATA=.local/kingmaker-fresh python collect_side_lp.py
  KINGMAKER_DATA=.local/kingmaker-fresh python confirm_fresh.py

Only launches after the freeze timestamp and at least 24 h before the new head are tested.
"""
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import analyze_pons as ap

FROZEN = json.loads((Path(__file__).parent / "frozen/2026-10-13.json").read_text())


def main():
    rows, *_ = ap.build(600)
    head_ts = json.loads((ap.DATA / "pons-launches.json").read_text())["meta"]["headTs"]
    rigs = set(FROZEN["rigs"])
    math_key = tuple(FROZEN["curveMath"])
    test = [r for r in rows if FROZEN["testLaunchesAfterTs"] < r["ts"] <= head_ts - ap.DAY]
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    adds = defaultdict(list)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if p and int(e["delta"]) > 0:
            adds[p["token"]].append((e["ts"], e["from"]))
    print(f"fresh launches tested: {len(test)} (after freeze ts {FROZEN['testLaunchesAfterTs']}); frozen rigs {len(rigs)}")
    for H in FROZEN["rules"]["checkpointsSec"]:
        sample = []
        for r in test:
            if r["mech"] != math_key:
                continue
            th = r["ts"] + H
            if r["gradTs"] is not None and r["gradTs"] <= th:
                continue
            ev = [e for e in r["events"] if e["kind"] in ("buy", "sell") and e["ts"] <= th]
            retail = {e["wallet"] for e in ev if e["kind"] == "buy" and e["wallet"] != r["creator"] and e["wallet"] not in rigs}
            if len(retail) < FROZEN["rules"]["contestedMinRetailBuyers"]:
                continue
            net = sum((e["quote"] - e["fee"] - e["tax"]) if e["kind"] == "buy" else -(e["quote"] + e["fee"] + e["tax"])
                      for e in ev) / ap.WEI
            sample.append({"blessed": int(any(ts <= th and a in rigs for ts, a in adds.get(r["token"], []))),
                           "ran": int(r["gradTs"] is not None), "progress": max(0.0, net) / 4.2,
                           "retailN": math.log1p(len(retail)), "logDev": math.log1p(r["devBuyEth"])})
        bl = [s for s in sample if s["blessed"]]
        un = [s for s in sample if not s["blessed"]]
        if not bl or not un:
            print(f"\nH={H}s: blessed {len(bl)}, unblessed {len(un)} — not enough to test")
            continue
        a, c = sum(s["ran"] for s in bl), sum(s["ran"] for s in un)
        p_raw = ap.fisher_greater(a, len(bl) - a, c, len(un) - c)
        feats = ["progress", "retailN", "logDev"]
        mu = {k: sum(s[k] for s in sample) / len(sample) for k in feats}
        sd = {k: (sum((s[k] - mu[k]) ** 2 for s in sample) / len(sample)) ** .5 or 1 for k in feats}
        z = lambda s: [(s[k] - mu[k]) / sd[k] for k in feats]
        uz = [(z(u), u) for u in un]
        diffs = [s["ran"] - min(((sum((x - y) ** 2 for x, y in zip(z(s), v)), u) for v, u in uz), key=lambda t: t[0])[1]["ran"]
                 for s in bl]
        obs = sum(diffs) / len(diffs)
        rng = random.Random(3)
        p_m = (sum(1 for _ in range(5000) if sum(x * rng.choice((1, -1)) for x in diffs) / len(diffs) >= obs) + 1) / 5001
        print(f"\nH={H}s identical math, contested: rig-blessed {a}/{len(bl)} ({a / len(bl):.1%}) vs {c}/{len(un)} "
              f"({c / len(un):.2%}), Fisher p {p_raw:.2e}")
        print(f"  matched on curve progress + retail buyers + creator buy: {a / len(bl):.1%} vs {a / len(bl) - obs:.1%} (p {p_m:.3f})")


if __name__ == "__main__":
    main()
