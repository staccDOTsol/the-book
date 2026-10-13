#!/usr/bin/env python3
"""What can each group actually get out? Simulates selling every token a group still holds into its
graduated Pons pool at the collection head, using the pool's real v4 liquidity ranges, with the
launch's fee + creator tax taken from the ETH leg (Pons charges both phases the same way).

Retail here excludes high-frequency wallets: any wallet that traded more than 10 distinct launches
in the 14-day window (curve recipient or graduated-pool tx.from). Threshold fixed before running.
Order matters on an AMM, so retail is simulated selling first (best case) and last (worst case).
"""
import json
import math
from collections import defaultdict

import analyze_pons as ap
import prove_drain as pd

Q96 = 2 ** 96
W = 1e18
HF_LAUNCHES = 10


def high_frequency():
    rows, *_ = ap.build(600)
    seen = defaultdict(set)
    for r in rows:
        for e in r["events"]:
            if e["kind"] in ("buy", "sell"):
                seen[e["wallet"]].add(r["token"])
    post = json.loads((ap.DATA / "postgrad.json").read_text())
    for s in post["swaps"]:
        seen[s["from"]].add(post["pools"][s["pool"]]["token"])
    return {w for w, t in seen.items() if len(t) > HF_LAUNCHES}


def sell(tokens, s, ranges):
    """Sell `tokens` of currency1 (the token) for currency0 (ETH); price sqrt rises. Returns (eth_out, new_s)."""
    out = 0.0
    while tokens > 1e-6:
        active = sum(L for tl, tu, L in ranges if tl <= s < tu)
        nxt = min((tu for tl, tu, L in ranges if tl <= s < tu), default=None)
        if not active or nxt is None:
            break
        s_max = min(nxt, 1.0001 ** (887200 / 2))
        need = active * (s_max - s)
        if tokens <= need:
            s2 = s + tokens / active
            out += active * (1 / s - 1 / s2)
            return out, s2
        out += active * (1 / s - 1 / s_max)
        tokens -= need
        s = s_max
        if s >= 1.0001 ** (887200 / 2) * 0.999999:
            break
    return out, s


def main():
    hf = high_frequency()
    c = pd.compute(hf)
    grads, post, eth, tok = c["grads"], c["post"], c["eth"], c["tok"]
    lp = json.loads((ap.DATA / "postgrad-lp.json").read_text())
    ranges = defaultdict(lambda: defaultdict(float))
    for e in lp:
        ranges[e["pool"]][(e["tl"], e["tu"])] += int(e["d"])
    last = {}
    for s in post["swaps"]:
        k = (s["block"], s["txIndex"], s["logIndex"])
        if s["pool"] not in last or k > last[s["pool"]][0]:
            last[s["pool"]] = (k, int(s["sqrtP"]) / Q96)
    pool_of = {p["token"]: pid for pid, p in post["pools"].items()}

    groups = ["retail", "high-frequency wallets", "kingmaker", "creator", "rig"]
    print(f"high-frequency wallets (> {HF_LAUNCHES} launches in window): {len(hf)}")
    for order_name, order in (("retail sells FIRST", groups), ("retail sells LAST", groups[1:] + groups[:1])):
        recovered = defaultdict(float)
        for t, pid in pool_of.items():
            rg = [(1.0001 ** (tl / 2), 1.0001 ** (tu / 2), L) for (tl, tu), L in ranges[pid].items() if L > 0]
            s = last[pid][1] if pid in last else int(post["pools"][pid]["sqrtP"]) / Q96
            r = grads[t]
            take = (r["feeBps"] + r["creatorTaxBps"]) / 1e4
            for g in order:
                held = tok[g].get(t, 0.0)
                if held <= 0:
                    continue
                out, s = sell(held, s, rg)
                recovered[g] += out * (1 - take)
        print(f"\n{order_name}:")
        for g in groups:
            realized = eth[g] / W
            rec = recovered[g] / W
            print(f"  {g:24s} realized cash {realized:+9.2f} ETH | exit value of tokens still held {rec:8.2f} | "
                  f"net if they exit {realized + rec:+9.2f} ETH")


if __name__ == "__main__":
    main()
