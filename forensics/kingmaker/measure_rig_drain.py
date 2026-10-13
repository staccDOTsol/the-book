#!/usr/bin/env python3
"""Measure the drain through rig side pools on held-out (B) Pons launches, 24 h after launch.

Replays every v4 side pool in chain order:
- each liquidity change is converted to exact currency amounts with the v4 range formula at the
  pool's price at that moment, and attributed to the transaction sender;
- each swap's fee (input x fee pips) is split across the positions in range at the pre-swap tick,
  pro rata by liquidity (intra-swap tick crossings ignored);
- positions still open at launch + 24 h are valued at that price.
ETH-quoted pools are reported in ETH. Writes .local/kingmaker/rig-drain.json.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

import analyze_pons as ap

Q96 = 2 ** 96
ETH = "0x" + "0" * 40
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"  # 6 decimals
USD_PER_ETH = 2600  # fixed conversion for USDG-quoted pools, stated in every report
QUOTE_TO_WEI = {ETH: 1.0, WETH: 1.0, USDG: 1e12 / USD_PER_ETH}
LOG_BASE = math.log(1.0001)


def amounts(L: float, sp: float, tl: int, tu: int) -> tuple[float, float]:
    sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
    if sp <= sa:
        return L * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (sb - sp) / (sp * sb), L * (sp - sa)


def main():
    rows, a_set, b_set, *_ = ap.build(600)
    launch = {r["token"]: r for r in rows}
    b_tokens = {r["token"] for r in b_set}
    side = json.loads((ap.DATA / "side-lp.json").read_text())
    sender_of = {e["tx"]: e["from"] for e in side["liquidity"]}

    # Rigs from the first half (same rule as analyze_rig_lp.py).
    a_tokens = {r["token"] for r in a_set}
    by_actor = defaultdict(set)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if p and int(e["delta"]) > 0 and p["token"] in a_tokens:
            if e["ts"] - launch[p["token"]]["ts"] <= ap.DAY:
                by_actor[e["from"]].add(p["token"])
    rigs = {a for a, t in by_actor.items() if len(t) >= 5}

    # Reference price: the token's bonding-curve execution price (wei per token unit) at or before t.
    curve_px = {}
    for r in rows:
        if r["token"] in b_tokens:
            curve_px[r["token"]] = [(e["ts"], e["quote"] / e["tokens"]) for e in r["events"]
                                    if e["kind"] in ("buy", "sell") and e["tokens"]]

    def ref(token, ts):
        series = curve_px.get(token) or []
        if not series:
            return None
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= ts:
                lo = mid + 1
            else:
                hi = mid
        return series[lo - 1][1] if lo else series[0][1]

    tot = {g: defaultdict(float) for g in ("rig", "other LP")}
    swaps = defaultdict(float)
    skipped = defaultdict(int)
    per_rig = defaultdict(lambda: defaultdict(float))
    with (ap.DATA / "side-events.jsonl").open() as f:
        for line in f:
            pool = json.loads(line)
            tok = pool["token"]
            if tok not in b_tokens:
                continue
            quote = pool["other"]
            if quote not in QUOTE_TO_WEI:
                skipped["other quote assets"] += 1
                continue
            if not curve_px.get(tok):
                skipped["no curve price"] += 1
                continue
            q_wei = QUOTE_TO_WEI[quote]
            quote_is_0 = int(quote, 16) < int(tok, 16)
            t_end = launch[tok]["ts"] + ap.DAY

            def value(a0, a1, ts):
                qa, ta = (a0, a1) if quote_is_0 else (a1, a0)
                return qa * q_wei + ta * ref(tok, ts), qa * q_wei, ta

            sp, tick, last_ts = None, None, launch[tok]["ts"]
            pos = {}
            for ev in pool["events"]:
                if ev["ts"] > t_end:
                    break
                last_ts = ev["ts"]
                if ev["kind"] == "init":
                    sp, tick = int(ev["sqrtP"]) / Q96, ev["tick"]
                    continue
                if sp is None:
                    skipped["events before init"] += 1
                    continue
                if ev["kind"] == "lp":
                    owner = sender_of.get(ev["tx"], ev["sender"])
                    key = (owner, ev["tickLower"], ev["tickUpper"], ev["salt"])
                    d = int(ev["delta"])
                    v, q, t = value(*amounts(abs(d), sp, ev["tickLower"], ev["tickUpper"]), ev["ts"])
                    g = "rig" if owner in rigs else "other LP"
                    p = pos.setdefault(key, {"L": 0, "owner": owner, "tl": ev["tickLower"], "tu": ev["tickUpper"]})
                    side_ = "deposit" if d > 0 else "withdraw"
                    tot[g][side_ + "Value"] += v; tot[g][side_ + "Quote"] += q; tot[g][side_ + "Tok"] += t
                    per_rig[owner][side_] += v
                    p["L"] += d
                    continue
                a0, a1 = int(ev["a0"]), int(ev["a1"])
                fee_frac = ev["fee"] / 1e6
                paid0, paid1 = max(0, -a0), max(0, -a1)
                got0, got1 = max(0, a0), max(0, a1)
                paid_v = value(paid0, paid1, ev["ts"])[0]
                got_v = value(got0, got1, ev["ts"])[0]
                fee_v = paid_v * fee_frac
                swaps["count"] += 1
                swaps["paidValue"] += paid_v
                swaps["receivedValue"] += got_v
                swaps["feesValue"] += fee_v
                in_range = [p for p in pos.values() if p["L"] > 0 and p["tl"] <= tick < p["tu"]]
                active = sum(p["L"] for p in in_range)
                if active > 0:
                    for p in in_range:
                        share = fee_v * p["L"] / active
                        g = "rig" if p["owner"] in rigs else "other LP"
                        tot[g]["feesEarned"] += share
                        per_rig[p["owner"]]["fees"] += share
                else:
                    swaps["feesNoTrackedLp"] += fee_v
                sp, tick = int(ev["sqrtP"]) / Q96, ev["tick"]
            for p in pos.values():
                if p["L"] > 0 and sp is not None:
                    v, q, t = value(*amounts(p["L"], sp, p["tl"], p["tu"]), t_end)
                    g = "rig" if p["owner"] in rigs else "other LP"
                    tot[g]["openValue"] += v; tot[g]["openQuote"] += q; tot[g]["openTok"] += t
                    per_rig[p["owner"]]["open"] += v

    W = 1e18
    report = {"rigs": len(rigs), "usdPerEth": USD_PER_ETH, "skipped": skipped,
              "swaps": {k: v / (W if k != "count" else 1) for k, v in swaps.items()}, "groups": {}}
    print(f"rigs {len(rigs)}; held-out side pools quoted in ETH/WETH/USDG (USDG at ${USD_PER_ETH}/ETH); "
          f"skipped {dict(skipped)}")
    print(f"swaps within 24 h of launch: {int(swaps['count'])} | traders paid in {swaps['paidValue'] / W:.2f} ETH-value, "
          f"received {swaps['receivedValue'] / W:.2f} | swap fees {swaps['feesValue'] / W:.2f} "
          f"(no tracked position in range: {swaps['feesNoTrackedLp'] / W:.2f})")
    for g, t in tot.items():
        dep, wd, op, fe = (t[k] / W for k in ("depositValue", "withdrawValue", "openValue", "feesEarned"))
        pnl = wd + op + fe - dep
        q_in, q_out = t["depositQuote"] / W, (t["withdrawQuote"] + t["openQuote"]) / W
        k_in, k_out = t["depositTok"] / W, (t["withdrawTok"] + t["openTok"]) / W
        report["groups"][g] = {"depositValueEth": dep, "withdrawValueEth": wd, "openValueEth": op, "feesEth": fe,
                               "pnlEth": pnl, "quoteInEth": q_in, "quoteOutPlusOpenEth": q_out,
                               "tokensIn": k_in, "tokensOutPlusOpen": k_out}
        print(f"\n  {g}: deposited {dep:.3f} ETH-value | withdrawn {wd:.3f} | open at 24 h {op:.3f} | fees earned {fe:.3f}"
              f" | P&L {pnl:+.3f} ETH" + (f" ({pnl / dep:+.1%})" if dep else ""))
        print(f"     quote side: in {q_in:.3f} ETH, out+open {q_out:.3f} | token side: in {k_in:,.0f}, out+open {k_out:,.0f}"
              + (f" ({k_out / k_in - 1:+.1%})" if k_in else ""))
    pr = sorted(((o, v["withdraw"] + v["open"] + v["fees"] - v["deposit"], v["deposit"]) for o, v in per_rig.items()
                 if o in rigs and v["deposit"] > 0), key=lambda x: -x[2])[:10]
    report["topRigs"] = [{"rig": o, "pnlEth": p / W, "depositEth": d / W} for o, p, d in pr]
    print("\n  biggest rigs by deposits: P&L ETH / deposited ETH")
    for o, p, d in pr:
        print(f"    {o} {p / W:+9.3f} / {d / W:9.3f}")
    (ap.DATA / "rig-drain.json").write_text(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
