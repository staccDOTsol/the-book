#!/usr/bin/env python3
"""Amendment 5: risk-set matching on the state *before* the rig arrived.

For each age window (g, g'] on the grid, a launch is treated if its first rig liquidity add falls in
the window, and a control if no rig has added liquidity by g'. Both must be ungraduated at g' and
have >= 3 retail buyers by g. Treated launches are matched to the nearest control on curve progress,
retail buyer count and creator buy, all measured at g. Outcome: graduation after g', within 24 h.
Usage: risk_set.py            (held-out half of the main window, frozen rigs)
       KINGMAKER_DATA=.local/kingmaker-holdout risk_set.py --holdout
"""
import json
import math
import random
import sys
from collections import defaultdict

import analyze_pons as ap
import confirm_fresh as cfz

GRID = [60, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200]
B = 10_000


def states(r, rigs):
    """Per grid age: (progress, retail buyer count). One pass over the launch's events."""
    out, net, retail, i = {}, 0.0, set(), 0
    evs = [e for e in r["events"] if e["kind"] in ("buy", "sell")]
    for g in GRID:
        while i < len(evs) and evs[i]["ts"] - r["ts"] <= g:
            e = evs[i]
            if e["kind"] == "buy":
                net += (e["quote"] - e["fee"] - e["tax"]) / ap.WEI
                if e["wallet"] != r["creator"] and e["wallet"] not in rigs:
                    retail.add(e["wallet"])
            else:
                net -= (e["quote"] + e["fee"] + e["tax"]) / ap.WEI
            i += 1
        out[g] = (max(0.0, net) / 4.2, len(retail))
    return out


def test(rows, rigs, adds, label):
    math_key = tuple(cfz.FROZEN["curveMath"])
    rows = [r for r in rows if r["mech"] == math_key]
    first_rig = {}
    for r in rows:
        ts = [t - r["ts"] for t, a in adds.get(r["token"], []) if a in rigs and t >= r["ts"]]
        first_rig[r["token"]] = min(ts) if ts else None
    st = {r["token"]: states(r, rigs) for r in rows}
    pairs, per_window = [], []
    for g, g2 in zip(GRID, GRID[1:]):
        elig = []
        for r in rows:
            grad_age = r["gradTs"] - r["ts"] if r["gradTs"] is not None else None
            if grad_age is not None and grad_age <= g2:
                continue
            prog, nret = st[r["token"]][g]
            if nret < ap.CONTESTED_MIN:
                continue
            fr = first_rig[r["token"]]
            if fr is not None and g < fr <= g2:
                kind = "T"
            elif fr is None or fr > g2:
                kind = "C"
            else:
                continue  # already blessed before this window
            elig.append({"kind": kind, "progress": prog, "retailN": math.log1p(nret),
                         "logDev": math.log1p(r["devBuyEth"]), "ran": int(grad_age is not None),
                         "creator": r["creator"]})
        T = [e for e in elig if e["kind"] == "T"]
        C = [e for e in elig if e["kind"] == "C"]
        if not T or not C:
            continue
        feats = ["progress", "retailN", "logDev"]
        mu = {k: sum(e[k] for e in elig) / len(elig) for k in feats}
        sd = {k: (sum((e[k] - mu[k]) ** 2 for e in elig) / len(elig)) ** .5 or 1 for k in feats}
        z = lambda e: [(e[k] - mu[k]) / sd[k] for k in feats]
        cz = [(z(c), c) for c in C]
        w = []
        for t in T:
            zt = z(t)
            c = min(((sum((a - b) ** 2 for a, b in zip(zt, v)), c) for v, c in cz), key=lambda x: x[0])[1]
            w.append((t, t["ran"] - c["ran"]))
        pairs += w
        per_window.append((g, g2, len(T), sum(t["ran"] for t in T), len(C), sum(d for _, d in w) / len(w)))
    print(f"\n### {label}")
    for g, g2, nt, gt, nc, d in per_window:
        print(f"  window {g // 60:>4}-{g2 // 60:<4} min: treated {nt:4d} (graduate {gt:3d}), controls {nc:6d}, matched effect {d:+.3f}")
    if not pairs:
        print("  no treated launches")
        return
    mean = sum(d for _, d in pairs) / len(pairs)
    groups = defaultdict(list)
    for t, d in pairs:
        groups[t["creator"]].append(d)
    ids, rng, means = list(groups), random.Random(1), []
    for _ in range(B):
        draw = [d for i in (rng.choice(ids) for _ in ids) for d in groups[i]]
        means.append(sum(draw) / len(draw))
    means.sort()
    tr = sum(t["ran"] for t, _ in pairs) / len(pairs)
    print(f"  POOLED: {len(pairs)} treated; graduate {tr:.1%} vs matched controls {tr - mean:.1%}; effect {mean:+.3f}, "
          f"95% [{means[int(.025 * B)]:+.3f}, {means[int(.975 * B)]:+.3f}] by creator ({len(ids)} clusters), "
          f"p {sum(1 for m in means if m <= 0) / B:.4f}")


def main():
    if "--holdout" in sys.argv:
        import holdout_stream
        rows, adds, rigs, rule_rigs, second = holdout_stream.load()
        test(rows, rigs, adds, "UNTOUCHED HOLDOUT, frozen rig list")
        test(second, rule_rigs, adds, "UNTOUCHED HOLDOUT second half, rigs re-derived by the frozen rule")
        return
    _, _, b_set, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    adds = defaultdict(list)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if p and int(e["delta"]) > 0:
            adds[p["token"]].append((e["ts"], e["from"]))
    test(b_set, set(cfz.FROZEN["rigs"]), adds, "MAIN WINDOW held-out half, frozen rig list")


if __name__ == "__main__":
    main()
