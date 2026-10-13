#!/usr/bin/env python3
"""Amendment 3: are kingmakers delta-neutral while retail holds the bag? (blessed contested B launches)"""
from __future__ import annotations

import sys
from collections import defaultdict

import analyze_pons as ap

WEI = 10 ** 18


def positions(sample, km):
    pos = defaultdict(lambda: {"tin": 0, "tout": 0, "ein": 0, "eout": 0, "lots": [], "held_s": 0.0, "held_tok": 0})
    fees = tax = 0
    for r in sample:
        if not r["blessed"]:
            continue
        for e in r["events"]:
            if e["kind"] == "done":
                continue
            fees += e["fee"]
            tax += e["tax"]
            g = "creator" if e["wallet"] == r["creator"] else ("km" if e["wallet"] in km else "retail")
            p = pos[(g, e["wallet"], r["token"])]
            if e["kind"] == "buy":
                p["tin"] += e["tokens"]; p["ein"] += e["quote"]; p["lots"].append([e["ts"], e["tokens"]])
            else:
                p["tout"] += e["tokens"]; p["eout"] += e["quote"]
                left = e["tokens"]
                while left and p["lots"]:
                    ts, amt = p["lots"][0]
                    take = min(left, amt)
                    p["held_s"] += take * (e["ts"] - ts); p["held_tok"] += take
                    left -= take
                    if take == amt:
                        p["lots"].pop(0)
                    else:
                        p["lots"][0][1] -= take
    return pos, fees, tax


def summarize(pos):
    out = {}
    for g in ("km", "retail", "creator"):
        allp = [p for (gg, _, _), p in pos.items() if gg == g]
        ps = [p for p in allp if p["tin"] > 0]
        sell_only = [p for p in allp if p["tin"] == 0 and p["tout"] > 0]
        if not ps:
            continue
        flat = sum(1 for p in ps if p["tout"] >= 0.9 * p["tin"])
        oversold = sum(1 for p in ps if p["tout"] > p["tin"])
        tin = sum(p["tin"] for p in ps); tout = sum(min(p["tout"], p["tin"]) for p in ps)
        ein = sum(p["ein"] for p in allp); eout = sum(p["eout"] for p in allp)
        hs = sum(p["held_s"] for p in ps); ht = sum(p["held_tok"] for p in ps)
        absnet = sorted(abs(p["eout"] - p["ein"]) / p["ein"] for p in ps if p["ein"])
        out[g] = {"positions": len(ps), "flatShare": flat / len(ps), "oversoldShare": oversold / len(ps),
                  "bagShare": 1 - tout / tin if tin else None, "ethIn": ein / WEI, "ethOut": eout / WEI,
                  "netEth": (eout - ein) / WEI, "netEthPctOfIn": (eout - ein) / ein if ein else None,
                  "medianAbsNetPct": absnet[len(absnet) // 2] if absnet else None,
                  "holdMinutes": hs / ht / 60 if ht else None,
                  "sellOnlyPositions": len(sell_only), "sellOnlyEth": sum(p["eout"] for p in sell_only) / WEI,
                  "sellOnlyTokens": sum(p["tout"] for p in sell_only) / WEI}
    return out


def main():
    early = int(sys.argv[sys.argv.index("--early") + 1]) if "--early" in sys.argv else 600
    ap.EARLY = early
    _, _, b_set, km_pick, _, mech_mode, *_ = ap.build(early)
    for uni in ("contested", "contested+same-math"):
        sample = []
        for r in b_set:
            if len(r["earlyWallets"] - km_pick) < ap.CONTESTED_MIN:
                continue
            if uni == "contested+same-math" and r["mech"] != mech_mode:
                continue
            sample.append(dict(r, blessed=int(bool(r["earlyWallets"] & km_pick))))
        pos, fees, tax = positions(sample, km_pick)
        s = summarize(pos)
        print(f"\n== KM-pick / {uni} / early {early}s: blessed launches {sum(r['blessed'] for r in sample)}")
        for g, v in s.items():
            print(f"  {g:8s} positions {v['positions']:6d} | flat {v['flatShare']:.1%} (sold more than bought {v['oversoldShare']:.1%}) "
                  f"| bag kept {v['bagShare']:.1%} | net {v['netEth']:+.2f} ETH ({v['netEthPctOfIn']:+.1%} of in; median |net| {v['medianAbsNetPct']:.1%}) "
                  f"| hold {v['holdMinutes'] and round(v['holdMinutes'], 1)} min | sold without buying here: "
                  f"{v['sellOnlyPositions']} positions, {v['sellOnlyEth']:.1f} ETH")
        retail = s["retail"]["netEth"]
        km, cr = s["km"]["netEth"], s.get("creator", {}).get("netEth", 0.0)
        f, t = fees / WEI, tax / WEI
        left = -(retail + km + cr) - f - t
        print(f"  where retail's {retail:+.1f} ETH went: fees {f:.1f} | creator tax {t:.1f} | kingmakers {km:+.1f} | "
              f"creators' trading {cr:+.1f} | left in curves / carried to graduated pools {left:.1f}")


if __name__ == "__main__":
    main()
