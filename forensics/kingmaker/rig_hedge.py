#!/usr/bin/env python3
"""Do rigs offset their side-pool inventory on the bonding curve, and what does retail lose on the
curve in rig-blessed launches vs the rest? Held-out (B) launches, 24 h. Exploratory follow-up to
measure_rig_drain.py."""
import json
from collections import defaultdict

import analyze_pons as ap

W = 1e18


def main():
    rows, a_set, b_set, *_ = ap.build(600)
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    a_tokens = {r["token"] for r in a_set}
    launch = {r["token"]: r for r in rows}
    by_actor = defaultdict(set)
    rig_lp_ts = defaultdict(list)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if not p or int(e["delta"]) <= 0 or p["token"] not in launch:
            continue
        if p["token"] in a_tokens and e["ts"] - launch[p["token"]]["ts"] <= ap.DAY:
            by_actor[e["from"]].add(p["token"])
        rig_lp_ts[p["token"]].append((e["ts"], e["from"]))
    rigs = {a for a, t in by_actor.items() if len(t) >= 5}

    curve = defaultdict(float)
    groups = {"rig-blessed (rig LP within 24 h)": defaultdict(float), "no rig LP": defaultdict(float)}
    for r in b_set:
        rigged = any(a in rigs and ts - r["ts"] <= ap.DAY for ts, a in rig_lp_ts.get(r["token"], []))
        g = groups["rig-blessed (rig LP within 24 h)" if rigged else "no rig LP"]
        g["launches"] += 1
        g["graduated"] += r["ran"]
        for e in r["events"]:
            if e["kind"] == "done":
                continue
            who = "creator" if e["wallet"] == r["creator"] else ("rig" if e["wallet"] in rigs else "retail")
            sign = 1 if e["kind"] == "buy" else -1
            if who == "rig":
                curve["tokBought" if sign > 0 else "tokSold"] += e["tokens"]
                curve["ethIn" if sign > 0 else "ethOut"] += e["quote"]
            if who == "retail":
                g["retailEthIn" if sign > 0 else "retailEthOut"] += e["quote"]
                g["retailTokIn" if sign > 0 else "retailTokOut"] += e["tokens"]
            if e["kind"] in ("buy", "sell"):
                g["fees"] += e["fee"]; g["tax"] += e["tax"]
    print(f"rig wallets trading on the curve in held-out launches: bought {curve['tokBought'] / W:,.0f} tokens for "
          f"{curve['ethIn'] / W:.2f} ETH; sold {curve['tokSold'] / W:,.0f} tokens for {curve['ethOut'] / W:.2f} ETH")
    for name, g in groups.items():
        n = g["launches"]
        net = (g["retailEthOut"] - g["retailEthIn"]) / W
        bag = 1 - min(g["retailTokOut"], g["retailTokIn"]) / g["retailTokIn"] if g["retailTokIn"] else 0
        print(f"\n{name}: {int(n)} launches, graduated {int(g['graduated'])} ({g['graduated'] / n:.2%})")
        print(f"  retail on the curve: in {g['retailEthIn'] / W:.2f} ETH, out {g['retailEthOut'] / W:.2f} -> net {net:+.2f} ETH "
              f"({net / (g['retailEthIn'] / W):+.1%}); still holding {bag:.0%} of tokens bought" if g["retailEthIn"] else "  no retail")
        print(f"  per launch: retail net {net / n:+.4f} ETH; fees {g['fees'] / W / n:.4f}; creator tax {g['tax'] / W / n:.4f}")
        if g["retailEthIn"]:
            print(f"  fees+tax as share of retail loss: {(g['fees'] + g['tax']) / W / -net:.0%}" if net < 0 else "")


if __name__ == "__main__":
    main()
