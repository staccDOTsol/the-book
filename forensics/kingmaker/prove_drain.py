#!/usr/bin/env python3
"""Point 3, end to end: on held-out launches that graduated, where does retail's ETH end up, and are
rigs flat? Follows every group through the whole life of each chart up to the collection head:

  bonding curve (CurveBuy/CurveSell, recipient wallet)
  -> side pools (rig and other LP deposits/withdrawals by tx.from; swap fees split in range)
  -> graduated Pons pool (every Swap by tx.from).

Groups (first match wins): creator, rig (side-pool LP actor on >= 5 first-half launches),
kingmaker (KM-pick early buyer from analyze_pons), retail (everyone else). Side-pool swappers are
not attributed to a wallet and are reported as their own row.

ETH is realized cash flow. Tokens still held are marked at each token's last graduated-pool price.
A conservation line checks that every ETH that left a wallet is accounted for. Pons hook fees on
post-graduation swaps are an extra trader cost not included here.
"""
from __future__ import annotations

import json
from collections import defaultdict

import analyze_pons as ap
from measure_rig_drain import Q96, QUOTE_TO_WEI, amounts

W = 1e18


def compute(hf: set | None = None):
    """Return the lifecycle ledger. `hf` = high-frequency wallets to split out of retail."""
    hf = hf or set()
    rows, a_set, b_set, km_pick, *_ = ap.build(600)
    grads = {r["token"]: r for r in b_set if r["ran"]}
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    post = json.loads((ap.DATA / "postgrad.json").read_text())
    sender_of = {e["tx"]: e["from"] for e in side["liquidity"]}
    a_tokens = {r["token"] for r in a_set}
    launch = {r["token"]: r for r in rows}

    by_actor = defaultdict(set)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if p and int(e["delta"]) > 0 and p["token"] in a_tokens and e["ts"] - launch[p["token"]]["ts"] <= ap.DAY:
            by_actor[e["from"]].add(p["token"])
    rigs = {a for a, t in by_actor.items() if len(t) >= 5}

    def group(addr, r):
        if addr == r["creator"]:
            return "creator"
        if addr in rigs:
            return "rig"
        if addr in km_pick:
            return "kingmaker"
        if addr in hf:
            return "high-frequency wallets"
        return "retail"

    eth = defaultdict(float)          # realized ETH, + = received
    tok = defaultdict(lambda: defaultdict(float))  # group -> token -> net tokens held
    sinks = defaultdict(float)
    pons_eth = defaultdict(float)     # ETH in each graduated pool
    final_px = {}                     # wei per token unit at head

    # 1. Bonding curve, whole life (graduates finish inside the 24 h horizon).
    for t, r in grads.items():
        for e in r["events"]:
            if e["kind"] == "done":
                pons_eth[t] += e["quote"]
                continue
            g = group(e["wallet"], r)
            sinks["curve fees"] += e["fee"]
            sinks["creator tax"] += e["tax"]
            if e["kind"] == "buy":
                eth[g] -= e["quote"]; tok[g][t] += e["tokens"]
            else:
                eth[g] += e["quote"]; tok[g][t] -= e["tokens"]

    # 3. Graduated Pons pool (currency0 = native ETH; negative delta = trader pays).
    for s in post["swaps"]:
        p = post["pools"][s["pool"]]
        if p["other"] != "0x" + "0" * 40:
            sinks["skipped non-ETH graduated-pool swaps"] += 1
            continue
        r = grads[p["token"]]
        g = group(s["from"], r)
        a0, a1 = int(s["a0"]), int(s["a1"])
        eth[g] += a0
        tok[g][p["token"]] += a1
        pons_eth[p["token"]] -= a0
    last = {}
    for s in post["swaps"]:
        p = post["pools"][s["pool"]]
        if p["token"] not in last or (s["block"], s["txIndex"], s["logIndex"]) > last[p["token"]][0]:
            last[p["token"]] = ((s["block"], s["txIndex"], s["logIndex"]), int(s["sqrtP"]))
    for pid, p in post["pools"].items():
        sp = (last[p["token"]][1] if p["token"] in last else int(p["sqrtP"])) / Q96
        final_px[p["token"]] = 1 / (sp * sp)  # ETH per token (both 18 decimals)

    # 2. Side pools on these tokens, whole history to head.
    side_lp_open = defaultdict(float)
    with (ap.DATA / "side-events.jsonl").open() as f:
        for line in f:
            pool = json.loads(line)
            t = pool["token"]
            if t not in grads or pool["other"] not in QUOTE_TO_WEI:
                continue
            r = grads[t]
            qw = QUOTE_TO_WEI[pool["other"]]
            quote_is_0 = int(pool["other"], 16) < int(t, 16)
            split = lambda a0, a1: (a0, a1) if quote_is_0 else (a1, a0)
            sp = tick = None
            pos = {}
            for ev in pool["events"]:
                if ev["kind"] == "init":
                    sp, tick = int(ev["sqrtP"]) / Q96, ev["tick"]
                    continue
                if sp is None:
                    continue
                if ev["kind"] == "lp":
                    owner = sender_of.get(ev["tx"], ev["sender"])
                    g = group(owner, r)
                    d = int(ev["delta"])
                    q, k = split(*amounts(abs(d), sp, ev["tickLower"], ev["tickUpper"]))
                    sign = -1 if d > 0 else 1
                    eth[g] += sign * q * qw
                    tok[g][t] += sign * k
                    p = pos.setdefault((owner, ev["tickLower"], ev["tickUpper"], ev["salt"]),
                                       {"L": 0, "g": g, "tl": ev["tickLower"], "tu": ev["tickUpper"]})
                    p["L"] += d
                    continue
                a0, a1 = int(ev["a0"]), int(ev["a1"])
                q, k = split(a0, a1)
                eth["side-pool traders (unattributed)"] += q * qw
                tok["side-pool traders (unattributed)"][t] += k
                paid_q, paid_k = split(max(0, -a0), max(0, -a1))
                fee = (paid_q * qw + paid_k * final_px[t]) * ev["fee"] / 1e6
                in_range = [p for p in pos.values() if p["L"] > 0 and p["tl"] <= tick < p["tu"]]
                active = sum(p["L"] for p in in_range)
                for p in in_range:
                    eth[p["g"]] += fee * p["L"] / active  # fee credited as ETH-value to the LP group
                if not active:
                    sinks["side-pool fees with no tracked LP"] += fee
                sp, tick = int(ev["sqrtP"]) / Q96, ev["tick"]
            for p in pos.values():
                if p["L"] > 0:
                    q, k = split(*amounts(p["L"], sp, p["tl"], p["tu"]))
                    side_lp_open[p["g"]] += q * qw + k * final_px[t]

    return {"grads": grads, "post": post, "eth": eth, "tok": tok, "sinks": sinks, "pons_eth": pons_eth,
            "final_px": final_px, "side_lp_open": side_lp_open, "rigs": rigs, "km_pick": km_pick}


def main():
    c = compute()
    grads, post, eth, tok, sinks = c["grads"], c["post"], c["eth"], c["tok"], c["sinks"]
    pons_eth, final_px, side_lp_open, rigs, km_pick = (c["pons_eth"], c["final_px"], c["side_lp_open"],
                                                       c["rigs"], c["km_pick"])
    print(f"Held-out graduates: {len(grads)}; graduated-pool swaps {len(post['swaps'])}; rigs {len(rigs)}; "
          f"kingmakers {len(km_pick)}")
    print(f"\n{'group':36s} {'realized ETH':>14s} {'tokens held, ETH at last price':>32s} {'side LP still open':>20s} {'total':>12s}")
    report = {}
    for g in ("retail", "kingmaker", "rig", "creator", "side-pool traders (unattributed)"):
        held = sum(n * final_px[t] for t, n in tok[g].items())
        total = eth[g] + held + side_lp_open[g]
        report[g] = {"realizedEth": eth[g] / W, "heldTokensEth": held / W, "sideLpOpenEth": side_lp_open[g] / W,
                     "totalEth": total / W}
        print(f"{g:36s} {eth[g] / W:14.2f} {held / W:32.2f} {side_lp_open[g] / W:20.2f} {total / W:12.2f}")
    pool_left = sum(pons_eth.values())
    print(f"\nETH still in the 153 graduated pools at head: {pool_left / W:.2f} | curve fees {sinks['curve fees'] / W:.2f} | "
          f"creator tax {sinks['creator tax'] / W:.2f}")
    flows = sum(eth[g] for g in eth) + sinks["curve fees"] + sinks["creator tax"] + pool_left
    print(f"conservation check (should be ~0 apart from side-pool reserves and fees): {flows / W:+.2f} ETH")
    rig_tok = {t: n for t, n in tok["rig"].items()}
    rig_long = sum(n for n in rig_tok.values() if n > 0) / W
    rig_short = -sum(n for n in rig_tok.values() if n < 0) / W
    print(f"rig token position at head across all venues: long {rig_long:,.0f} / short {rig_short:,.0f} tokens; "
          f"value {sum(n * final_px[t] for t, n in rig_tok.items()) / W:+.3f} ETH")
    report["sinks"] = {k: v / W for k, v in sinks.items()}
    report["graduatedPoolEthAtHead"] = pool_left / W
    (ap.DATA / "prove-drain.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
