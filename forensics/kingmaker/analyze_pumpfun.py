#!/usr/bin/env python3
"""pump.fun blessing test (DEFINITIONS.md, Amendment 2; the King of the Hill outcome is blocked).

Blessed = pump.fun's own numKolsTraded >= 1 at the coin's first snapshot (age <= 5 min).
Outcomes from coins-v2: complete (graduated); ATH market cap >= 5x the first-snapshot market cap.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics as st
from pathlib import Path

from analyze_pons import cv_auc, fisher_greater

ROOT = Path(__file__).resolve().parents[2]
SNAPS = Path(os.path.expanduser("~/callouts/data/snaps/2026-10-12-v2.jsonl"))
OUTCOMES = ROOT / ".local/kingmaker/pumpfun-outcomes.jsonl"
MECH = ["progress", "vsol", "logBuys", "logSells", "snipers", "age_s"]


def load():
    first = {}
    with SNAPS.open() as f:
        for line in f:
            r = json.loads(line)
            if r["mint"] in first:
                continue
            m = r.get("mem") or {}
            if r["age_s"] > 300 or m.get("numKolsTraded") is None or m.get("progress") is None:
                first[r["mint"]] = None
                continue
            first[r["mint"]] = {
                "mint": r["mint"], "age_s": r["age_s"], "mcap0": r.get("mcap_usd") or m.get("marketCapUsd"),
                "kols": m["numKolsTraded"], "progress": m["progress"], "vsol": r["vsol"],
                "logBuys": math.log1p(m.get("buyCount") or 0), "logSells": math.log1p(m.get("sellCount") or 0),
                "snipers": m.get("sniperCount") or 0}
    outs = {}
    for line in OUTCOMES.open():
        o = json.loads(line)
        if "error" not in o and o.get("complete") is not None:
            outs[o["mint"]] = o
    rows = []
    for mint, r in first.items():
        if not r or mint not in outs or not r["mcap0"]:
            continue
        o = outs[mint]
        r["ran"] = int(bool(o["complete"]))
        r["ran5x"] = int((o.get("ath_market_cap") or 0) >= 5 * r["mcap0"])
        rows.append(r)
    return rows


def matched(rows, threshold, label, rng):
    treated = [r for r in rows if r["kols"] >= threshold]
    control = [r for r in rows if r["kols"] == 0]
    mu = {k: st.mean(r[k] for r in rows) for k in MECH}
    sd = {k: st.pstdev([r[k] for r in rows]) or 1.0 for k in MECH}
    z = lambda r: [(r[k] - mu[k]) / sd[k] for k in MECH]
    zc = [(z(c), c) for c in control]
    diffs, dists = [], []
    for t in treated:
        zt = z(t)
        d, c = min(((sum((a - b) ** 2 for a, b in zip(zt, v)), c) for v, c in zc), key=lambda x: x[0])
        diffs.append(t[label] - c[label])
        dists.append(d ** 0.5)
    obs = st.mean(diffs)
    hits = sum(1 for _ in range(10000) if st.mean([x * rng.choice((1, -1)) for x in diffs]) >= obs)
    return {"n": len(diffs), "treatedRate": st.mean(t[label] for t in treated),
            "matchedControlRate": st.mean(t[label] for t in treated) - obs, "diff": obs,
            "signFlipP": (hits + 1) / 10001, "medianMatchDist": st.median(dists)}


def main():
    rows = load()
    print(f"coins first seen at age <= 5 min with outcomes: {len(rows)}")
    report = {"n": len(rows), "tests": {}}
    rng = random.Random(5)
    for threshold in (1, 3):
        for r in rows:
            r["blessed"] = int(r["kols"] >= threshold)
        for label in ("ran", "ran5x"):
            a = sum(r["blessed"] and r[label] for r in rows)
            nb = sum(r["blessed"] for r in rows)
            c = sum((not r["blessed"]) and r[label] for r in rows)
            nu = len(rows) - nb
            res = {"blessed": [a, nb], "unblessed": [c, nu], "rateBlessed": a / nb if nb else None,
                   "rateUnblessed": c / nu if nu else None,
                   "riskRatio": (a / nb) / (c / nu) if nb and c else None,
                   "fisherP": fisher_greater(a, nb - a, c, nu - c)}
            if sum(r[label] for r in rows) >= 10:
                res["cvAucMechanics"] = cv_auc(rows, MECH, label)
                res["cvAucMechanicsPlusBlessed"] = cv_auc(rows, MECH + ["blessed"], label)
            res["matched"] = matched(rows, threshold, label, rng)
            key = f"kols>={threshold}/{label}"
            report["tests"][key] = res
            m = res["matched"]
            print(f"\n== {key}")
            print(f"  blessed {a}/{nb} ({res['rateBlessed']:.2%}) vs unblessed {c}/{nu} ({res['rateUnblessed']:.2%}); "
                  f"RR {res['riskRatio']}; Fisher p {res['fisherP']:.2e}")
            if "cvAucMechanics" in res:
                print(f"  CV AUC mechanics {res['cvAucMechanics']:.3f} -> +blessed {res['cvAucMechanicsPlusBlessed']:.3f}")
            print(f"  matched on curve state: blessed {m['treatedRate']:.2%} vs math-twin {m['matchedControlRate']:.2%} "
                  f"(n {m['n']}, diff {m['diff']:+.3f}, p {m['signFlipP']:.4f}, median match dist {m['medianMatchDist']:.2f})")
    (ROOT / ".local/kingmaker/pumpfun-results.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
