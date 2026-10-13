#!/usr/bin/env python3
"""Adversarial check: does rig liquidity still predict graduation once curve progress at the
checkpoint is held fixed? Graduation is a deterministic function of net ETH raised (threshold
4.2 ETH on identical-math curves), so progress at H is the 'mathematical reason' to control for."""
import json, math, random, sys
from collections import defaultdict
import analyze_pons as ap

def main():
    rows, a_set, b_set, _, _, mech_mode, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    pools = side["pools"]; a_tok = {r["token"] for r in a_set}; launch = {r["token"]: r for r in rows}
    adds = defaultdict(list); by_actor = defaultdict(set)
    for e in side["liquidity"]:
        p = pools.get(e["pool"])
        if not p or int(e["delta"]) <= 0 or p["token"] not in launch: continue
        adds[p["token"]].append((e["ts"], e["from"]))
        if p["token"] in a_tok and e["ts"] - launch[p["token"]]["ts"] <= ap.DAY: by_actor[e["from"]].add(p["token"])
    rigs = {a for a, t in by_actor.items() if len(t) >= 5}
    for H in (600, 3600, 21600):
        sample = []
        for r in b_set:
            if r["mech"] != mech_mode: continue
            th = r["ts"] + H
            if r["gradTs"] is not None and r["gradTs"] <= th: continue
            ev = [e for e in r["events"] if e["kind"] in ("buy", "sell") and e["ts"] <= th]
            retail = {e["wallet"] for e in ev if e["kind"] == "buy" and e["wallet"] != r["creator"] and e["wallet"] not in rigs}
            if len(retail) < ap.CONTESTED_MIN: continue
            net = sum((e["quote"] - e["fee"] - e["tax"]) if e["kind"] == "buy" else -(e["quote"] + e["fee"] + e["tax"]) for e in ev) / ap.WEI
            sample.append({"blessed": int(any(ts <= th and a in rigs for ts, a in adds.get(r["token"], []))),
                           "ran": int(r["gradTs"] is not None), "progress": max(0.0, net) / 4.2,
                           "retailN": math.log1p(len(retail)), "logDev": math.log1p(r["devBuyEth"])})
        bl = [s for s in sample if s["blessed"]]; un = [s for s in sample if not s["blessed"]]
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        print(f"\nH={H}s identical math, contested: blessed {len(bl)} (grad {sum(s['ran'] for s in bl)}), unblessed {len(un)} (grad {sum(s['ran'] for s in un)})")
        print(f"  median curve progress at H: blessed {med([s['progress'] for s in bl]):.0%} vs unblessed {med([s['progress'] for s in un]):.0%}")
        feats = ["progress", "retailN", "logDev"]
        mu = {k: sum(s[k] for s in sample) / len(sample) for k in feats}
        sd = {k: (sum((s[k] - mu[k]) ** 2 for s in sample) / len(sample)) ** .5 or 1 for k in feats}
        z = lambda s: [(s[k] - mu[k]) / sd[k] for k in feats]
        uz = [(z(u), u) for u in un]
        diffs = []
        for s in bl:
            zs = z(s); d, u = min(((sum((a - b) ** 2 for a, b in zip(zs, v)), u) for v, u in uz), key=lambda t: t[0])
            diffs.append(s["ran"] - u["ran"])
        obs = sum(diffs) / len(diffs); rng = random.Random(3)
        p = (sum(1 for _ in range(5000) if sum(x * rng.choice((1, -1)) for x in diffs) / len(diffs) >= obs) + 1) / 5001
        rb = sum(s["ran"] for s in bl) / len(bl)
        print(f"  matched on progress + retail buyers + dev buy: blessed {rb:.1%} vs matched unblessed {rb - obs:.1%} (p {p:.3f})")
        # within progress bands
        for lo, hi in ((0, .25), (.25, .5), (.5, .75), (.75, 2)):
            b = [s for s in bl if lo <= s["progress"] < hi]; u = [s for s in un if lo <= s["progress"] < hi]
            if b and u:
                print(f"    progress {lo:.0%}-{hi:.0%}: blessed {sum(s['ran'] for s in b)}/{len(b)} vs unblessed {sum(s['ran'] for s in u)}/{len(u)}")
main()
