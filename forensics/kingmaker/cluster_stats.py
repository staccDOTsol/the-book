#!/usr/bin/env python3
"""Repeat-adjusted inference and the 1-hour diagnosis for the checkpoint test.

Sample: held-out launches, identical curve math, contested (>= 3 retail buyers by the checkpoint),
not graduated by the checkpoint. Each rig-blessed launch is matched to the unblessed launch with the
nearest curve progress, retail buyer count and creator buy (as in adversarial_progress.py).

Repeat adjustment: the matched differences are resampled by cluster (the blessed launch's creator,
then the first rig that blessed it), 10,000 times. Reported: mean difference, 95% interval, one-sided
p = share of resamples <= 0, and Holm-adjusted p across the three checkpoints.

1-hour diagnosis: at the 1 h checkpoint, blessed launches are split into those a rig had already
blessed by 10 min and those first blessed between 10 min and 1 h.
"""
import json
import math
import random
from collections import defaultdict

import analyze_pons as ap

B = 10_000


def load():
    rows, a_set, b_set, _, _, mech_mode, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    a_tok = {r["token"] for r in a_set}
    launch = {r["token"]: r for r in rows}
    adds, by_actor = defaultdict(list), defaultdict(set)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if not p or int(e["delta"]) <= 0 or p["token"] not in launch:
            continue
        adds[p["token"]].append((e["ts"], e["from"]))
        if p["token"] in a_tok and e["ts"] - launch[p["token"]]["ts"] <= ap.DAY:
            by_actor[e["from"]].add(p["token"])
    rigs = {a for a, t in by_actor.items() if len(t) >= 5}
    return b_set, mech_mode, adds, rigs


def sample_at(H, b_set, mech_mode, adds, rigs):
    out = []
    for r in b_set:
        if r["mech"] != mech_mode:
            continue
        th = r["ts"] + H
        if r["gradTs"] is not None and r["gradTs"] <= th:
            continue
        ev = [e for e in r["events"] if e["kind"] in ("buy", "sell") and e["ts"] <= th]
        retail = {e["wallet"] for e in ev if e["kind"] == "buy" and e["wallet"] != r["creator"] and e["wallet"] not in rigs}
        if len(retail) < ap.CONTESTED_MIN:
            continue
        net = sum((e["quote"] - e["fee"] - e["tax"]) if e["kind"] == "buy" else -(e["quote"] + e["fee"] + e["tax"])
                  for e in ev) / ap.WEI
        rig_adds = sorted((ts, a) for ts, a in adds.get(r["token"], []) if ts <= th and a in rigs)
        out.append({"blessed": int(bool(rig_adds)), "ran": int(r["gradTs"] is not None),
                    "progress": max(0.0, net) / 4.2, "retailN": math.log1p(len(retail)),
                    "logDev": math.log1p(r["devBuyEth"]), "creator": r["creator"],
                    "rig": rig_adds[0][1] if rig_adds else None,
                    "firstRigAfter": rig_adds[0][0] - r["ts"] if rig_adds else None})
    return out


def matched_diffs(sample, treated):
    feats = ["progress", "retailN", "logDev"]
    un = [s for s in sample if not s["blessed"]]
    mu = {k: sum(s[k] for s in sample) / len(sample) for k in feats}
    sd = {k: (sum((s[k] - mu[k]) ** 2 for s in sample) / len(sample)) ** .5 or 1 for k in feats}
    z = lambda s: [(s[k] - mu[k]) / sd[k] for k in feats]
    uz = [(z(u), u) for u in un]
    out = []
    for s in treated:
        zs = z(s)
        u = min(((sum((a - b) ** 2 for a, b in zip(zs, v)), u) for v, u in uz), key=lambda t: t[0])[1]
        out.append((s, s["ran"] - u["ran"]))
    return out


def cluster_boot(pairs, key, seed=1):
    groups = defaultdict(list)
    for s, d in pairs:
        groups[s[key]].append(d)
    ids = list(groups)
    rng = random.Random(seed)
    means = []
    for _ in range(B):
        draw = [d for i in (rng.choice(ids) for _ in ids) for d in groups[i]]
        means.append(sum(draw) / len(draw))
    means.sort()
    return {"clusters": len(ids), "lo": means[int(.025 * B)], "hi": means[int(.975 * B)],
            "p": sum(1 for m in means if m <= 0) / B}


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    return adj


def main():
    b_set, mech_mode, adds, rigs = load()
    results = {}
    for H in (600, 3600, 21600):
        sample = sample_at(H, b_set, mech_mode, adds, rigs)
        pairs = matched_diffs(sample, [s for s in sample if s["blessed"]])
        mean = sum(d for _, d in pairs) / len(pairs)
        by_creator = cluster_boot(pairs, "creator")
        by_rig = cluster_boot(pairs, "rig")
        results[H] = (len(pairs), mean, by_creator, by_rig)
    adj_c = holm([results[H][2]["p"] for H in results])
    adj_r = holm([results[H][3]["p"] for H in results])
    print("Matched effect of rig liquidity on graduation (blessed minus matched unblessed), repeat-adjusted:")
    for i, (H, (n, mean, c, r)) in enumerate(results.items()):
        print(f"  {H // 60:>4} min: n {n:3d}, effect {mean:+.3f} | by creator ({c['clusters']} clusters): "
              f"95% [{c['lo']:+.3f}, {c['hi']:+.3f}], p {c['p']:.4f}, Holm {adj_c[i]:.4f} | by rig ({r['clusters']}): "
              f"95% [{r['lo']:+.3f}, {r['hi']:+.3f}], p {r['p']:.4f}, Holm {adj_r[i]:.4f}")

    print("\n1-hour diagnosis: when did the first rig arrive?")
    sample = sample_at(3600, b_set, mech_mode, adds, rigs)
    for name, cond in (("already blessed by 10 min", lambda s: s["firstRigAfter"] <= 600),
                       ("first blessed 10-60 min", lambda s: s["firstRigAfter"] > 600)):
        treated = [s for s in sample if s["blessed"] and cond(s)]
        pairs = matched_diffs(sample, treated)
        if not pairs:
            continue
        mean = sum(d for _, d in pairs) / len(pairs)
        grads = sum(s["ran"] for s in treated)
        c = cluster_boot(pairs, "creator")
        print(f"  {name:28s}: {grads}/{len(treated)} graduate ({grads / len(treated):.1%}); matched effect {mean:+.3f}, "
              f"95% [{c['lo']:+.3f}, {c['hi']:+.3f}], p {c['p']:.4f}; median progress at 1 h "
              f"{sorted(s['progress'] for s in treated)[len(treated) // 2]:.0%}")


if __name__ == "__main__":
    main()
