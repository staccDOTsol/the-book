#!/usr/bin/env python3
"""Robustness: rerun the Amendment 5 risk-set test with the rig cutoff at 3 and 10 first-half
launches instead of 5 (main window, held-out half). Reported whatever the outcome."""
import json
from collections import defaultdict

import analyze_pons as ap
import risk_set as rs


def main():
    _, a_set, b_set, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    a_tok = {r["token"]: r for r in a_set}
    adds, by_actor = defaultdict(list), defaultdict(set)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if not p or int(e["delta"]) <= 0:
            continue
        adds[p["token"]].append((e["ts"], e["from"]))
        r = a_tok.get(p["token"])
        if r and e["ts"] - r["ts"] <= ap.DAY:
            by_actor[e["from"]].add(p["token"])
    for k in (3, 10):
        rigs = {a for a, t in by_actor.items() if len(t) >= k}
        rs.test(b_set, rigs, adds, f"rig cutoff >= {k} first-half launches ({len(rigs)} rigs)")


if __name__ == "__main__":
    main()
