#!/usr/bin/env python3
"""Amendment 4: is the blessing rig liquidity? Landmark test on held-out Pons launches.

Blessed at landmark H = a rig (LP actor with side-pool adds on >= 5 first-half launches) added
side-pool liquidity on the token by launch + H. Outcome = graduation in (H, 24 h].
Usage: analyze_rig_lp.py [--landmark 600]
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict

import analyze_pons as ap

H = int(sys.argv[sys.argv.index("--landmark") + 1]) if "--landmark" in sys.argv else 600
RIG_MIN = 5


def main():
    rows, a_set, b_set, _, _, mech_mode, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    pools = side["pools"]
    adds = defaultdict(list)  # token -> [(ts, actor, pool)]
    for e in side["liquidity"]:
        if int(e["delta"]) > 0 and e["pool"] in pools:
            adds[pools[e["pool"]]["token"]].append((e["ts"], e["from"], e["pool"]))

    # Rigs, from the first half only.
    tokens_by_actor = defaultdict(set)
    for r in a_set:
        for ts, actor, _ in adds.get(r["token"], []):
            if ts - r["ts"] <= ap.DAY:
                tokens_by_actor[actor].add(r["token"])
    ran_a = {r["token"] for r in a_set if r["ran"]}
    base_a = len(ran_a) / len(a_set)
    rigs = {a for a, t in tokens_by_actor.items() if len(t) >= RIG_MIN}
    rig_pick = {a for a, t in tokens_by_actor.items()
                if len(t) >= 3 and len(t & ran_a) >= 2 and len(t & ran_a) / len(t) >= 3 * base_a}
    a_side = sum(1 for r in a_set if adds.get(r["token"]))
    print(f"side pools {len(pools)}; liquidity adds {sum(len(v) for v in adds.values())}; "
          f"A launches with any side-pool add in 24 h: {a_side}/{len(a_set)}")
    print(f"LP actors in A: {len(tokens_by_actor)}; rigs (>= {RIG_MIN} A launches): {len(rigs)}; rig-pick: {len(rig_pick)}")
    top = sorted(rigs, key=lambda a: -len(tokens_by_actor[a]))[:10]
    print("top rigs by A launches LP'd:", [(a, len(tokens_by_actor[a]), len(tokens_by_actor[a] & ran_a)) for a in top])

    report = {"landmark": H, "rigs": len(rigs), "rigPick": len(rig_pick), "tests": {}}
    for rig_name, rig in (("rigs", rigs), ("rig-pick", rig_pick)):
        for uni in ("contested", "contested+same-math", "all-not-yet-graduated"):
            sample = []
            for r in b_set:
                t_h = r["ts"] + H
                if r["gradTs"] is not None and r["gradTs"] <= t_h:
                    continue
                buys_h = [e for e in r["events"] if e["kind"] == "buy" and e["ts"] <= t_h and e["wallet"] != r["creator"]
                          and e["wallet"] not in rig]
                retail_wallets = {e["wallet"] for e in buys_h}
                if uni.startswith("contested") and len(retail_wallets) < ap.CONTESTED_MIN:
                    continue
                if uni == "contested+same-math" and r["mech"] != mech_mode:
                    continue
                blessed = int(any(ts <= t_h and actor in rig for ts, actor, _ in adds.get(r["token"], [])))
                before = [e for e in r["events"] if e["kind"] == "buy" and e["ts"] <= t_h and e["tokens"]]
                after = [e for e in r["events"] if e["kind"] == "buy" and e["ts"] > t_h and e["tokens"]]
                p_h = before[-1]["quote"] / before[-1]["tokens"] if before else (
                    after[0]["quote"] / after[0]["tokens"] if after else 0)
                p_max = max((e["quote"] / e["tokens"] for e in after), default=0)
                sample.append({
                    "blessed": blessed, "ran": int(r["gradTs"] is not None), "ran5x": int(p_h > 0 and p_max >= 5 * p_h),
                    "retailEarlyN": math.log1p(len(retail_wallets)),
                    "retailEarlyEth": math.log1p(sum(e["quote"] for e in buys_h) / ap.WEI),
                    "logDev": math.log1p(r["devBuyEth"]), "creatorTaxBps": r["creatorTaxBps"],
                    "hs": math.sin(2 * math.pi * r["hour"] / 24), "hc": math.cos(2 * math.pi * r["hour"] / 24)})
            res = block(sample)
            key = f"{rig_name}/{uni}"
            report["tests"][key] = res
            show(key, res)
    (ap.DATA / f"rig-lp-results-H{H}.json").write_text(json.dumps(report, indent=1))


def block(sample):
    out = {"n": len(sample)}
    if not sample:
        return out
    for label in ("ran", "ran5x"):
        a = sum(s["blessed"] and s[label] for s in sample); nb = sum(s["blessed"] for s in sample)
        c = sum((not s["blessed"]) and s[label] for s in sample); nu = len(sample) - nb
        out[label] = {"blessed": [a, nb], "unblessed": [c, nu],
                      "riskRatio": (a / nb) / (c / nu) if nb and nu and c else None,
                      "fisherP": ap.fisher_greater(a, nb - a, c, nu - c)}
    mech = ["logDev", "creatorTaxBps", "hs", "hc", "retailEarlyN", "retailEarlyEth"]
    if 10 <= sum(s["ran"] for s in sample) and 0 < sum(s["blessed"] for s in sample) < len(sample):
        pos = [s for s in sample if s["ran"]]
        neg = [s for s in sample if not s["ran"]]
        if len(neg) > 6000:
            neg = random.Random(11).sample(neg, 6000)
        out["cvAucMechanicsAndDemand"] = ap.cv_auc(pos + neg, mech, "ran")
        out["cvAucPlusRigLp"] = ap.cv_auc(pos + neg, mech + ["blessed"], "ran")
    dm = ap.demand_matched(sample) if 0 < sum(s["blessed"] for s in sample) < len(sample) else {}
    out["demandMatched"] = {k: v for k, v in dm.items() if k in ("ran", "ran5x", "medians")}
    return out


def show(key, res):
    print(f"\n== {key} @ landmark {H}s: n={res['n']}")
    if not res["n"]:
        return
    for label in ("ran", "ran5x"):
        t = res[label]
        rb = t["blessed"][0] / t["blessed"][1] if t["blessed"][1] else float("nan")
        ru = t["unblessed"][0] / t["unblessed"][1] if t["unblessed"][1] else float("nan")
        print(f"  {label:6s} rig-blessed {t['blessed'][0]}/{t['blessed'][1]} ({rb:.2%}) vs not {t['unblessed'][0]}/"
              f"{t['unblessed'][1]} ({ru:.2%}); RR {t['riskRatio'] and round(t['riskRatio'], 2)}; Fisher p {t['fisherP']:.2e}")
    if "cvAucMechanicsAndDemand" in res:
        print(f"  CV AUC mechanics+demand {res['cvAucMechanicsAndDemand']:.3f} -> +rig LP {res['cvAucPlusRigLp']:.3f}")
    dm = res.get("demandMatched") or {}
    for label in ("ran", "ran5x"):
        if label in dm:
            m = dm[label]
            print(f"  demand-matched {label}: rig-blessed {m['blessedRate']:.2%} vs matched {m['matchedUnblessedRate']:.2%} "
                  f"(n {m['n']}, p {m['signFlipP']:.4f})")


if __name__ == "__main__":
    main()
