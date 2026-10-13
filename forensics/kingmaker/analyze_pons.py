#!/usr/bin/env python3
"""Kingmaker test on Pons V2 launches. Implements DEFINITIONS.md exactly.

Reads .local/kingmaker/pons-launches.json and pons-trades.json.
Writes .local/kingmaker/pons-results.json and prints the report.
Usage: analyze_pons.py [--early 600]
"""
from __future__ import annotations

import json
import os
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("KINGMAKER_DATA", ROOT / ".local/kingmaker"))
ETH = "0x" + "0" * 40
DAY = 86400
WEI = 10 ** 18

EARLY = int(sys.argv[sys.argv.index("--early") + 1]) if "--early" in sys.argv else 600
RECUR_MIN = 5
PICK_MIN, PICK_GRADS, PICK_LIFT = 3, 2, 3.0
CONTESTED_MIN = 3


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p for table [[a, b], [c, d]] (a = blessed & ran)."""
    n1, n2, k = a + b, c + d, a + c
    total = n1 + n2

    def logp(x: int) -> float:
        return (math.lgamma(n1 + 1) - math.lgamma(x + 1) - math.lgamma(n1 - x + 1)
                + math.lgamma(n2 + 1) - math.lgamma(k - x + 1) - math.lgamma(n2 - k + x + 1)
                - math.lgamma(total + 1) + math.lgamma(k + 1) + math.lgamma(total - k + 1))

    hi = min(n1, k)
    return min(1.0, sum(math.exp(logp(x)) for x in range(a, hi + 1)))


def auc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels))
    ranks, i = [0.0] * len(pairs), 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    pos = sum(lab for _, lab in pairs)
    neg = len(pairs) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum = sum(r for r, (_, lab) in zip(ranks, pairs) if lab)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def logistic_fit(x: list[list[float]], y: list[int], ridge: float = 1e-3, iters: int = 50):
    p = len(x[0])
    w = [0.0] * p
    for _ in range(iters):
        grad = [0.0] * p
        hess = [[0.0] * p for _ in range(p)]
        for row, lab in zip(x, y):
            z = sum(a * b for a, b in zip(w, row))
            mu = 1 / (1 + math.exp(-max(-30, min(30, z))))
            for i in range(p):
                grad[i] += (lab - mu) * row[i]
                for j in range(p):
                    hess[i][j] += mu * (1 - mu) * row[i] * row[j]
        for i in range(p):
            grad[i] -= ridge * w[i]
            hess[i][i] += ridge
        step = solve(hess, grad)
        w = [a + b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def cv_auc(rows: list[dict], feats: list[str], label: str, folds: int = 5, seed: int = 7) -> float:
    rng = random.Random(seed)
    idx = list(range(len(rows)))
    rng.shuffle(idx)
    scores, labels = [0.0] * len(rows), [rows[i][label] for i in range(len(rows))]
    for f in range(folds):
        test = set(idx[f::folds])
        train = [i for i in idx if i not in test]
        mu = {k: sum(rows[i][k] for i in train) / len(train) for k in feats}
        sd = {k: (sum((rows[i][k] - mu[k]) ** 2 for i in train) / len(train)) ** 0.5 or 1.0 for k in feats}
        design = lambda r: [1.0] + [(r[k] - mu[k]) / sd[k] for k in feats]
        w = logistic_fit([design(rows[i]) for i in train], [rows[i][label] for i in train])
        for i in test:
            scores[i] = sum(a * b for a, b in zip(w, design(rows[i])))
    return auc(scores, labels)


def build(early: int = EARLY):
    """Load data and return (rows, A, B, km_pick, km_recur, mech_mode, mech_n, base_a, early_in, ran_a, meta)."""
    global EARLY
    EARLY = early
    launches = json.loads((DATA / "pons-launches.json").read_text())
    trades = json.loads((DATA / "pons-trades.json").read_text())
    meta = launches["meta"]
    by_curve = defaultdict(list)
    for t in trades["trades"]:
        by_curve[t["curve"]].append(t)

    rows = []
    for l in launches["launches"]:
        if l["pairToken"] != ETH:
            continue
        evs = sorted(by_curve.get(l["curve"], []), key=lambda t: (t["block"], t["txIndex"], t["logIndex"]))
        t0, creator = l["ts"], l["creator"]
        horizon = [e for e in evs if e["ts"] - t0 <= DAY]
        buys = [e for e in horizon if e["kind"] == "buy" and e["tokens"] > 0]
        done = [e for e in horizon if e["kind"] == "done"]
        dev_buy = sum(e["quote"] for e in buys if e["tx"] == l["tx"] or e["wallet"] == creator) / WEI
        early = [e for e in buys if e["wallet"] != creator and e["ts"] - t0 <= EARLY]
        prices = [(e["quote"] / e["tokens"], e["ts"]) for e in buys]
        peak = max(prices, default=(0, t0))
        rows.append({
            "token": l["token"], "curve": l["curve"], "creator": creator, "ts": t0,
            "mech": (l.get("phantomQuote"), l.get("feeBps"), l.get("creatorTaxBps"), l.get("graduationThresholdCall")),
            "creatorTaxBps": l.get("creatorTaxBps") or 0, "feeBps": l.get("feeBps") or 0,
            "devBuyEth": dev_buy, "hour": (t0 % DAY) / 3600,
            "early": early, "earlyWallets": {e["wallet"] for e in early},
            "ran": int(bool(done)), "gradTs": done[0]["ts"] if done else None,
            "ran5x": int(bool(prices) and peak[0] >= 5 * prices[0][0]),
            "peakTs": peak[1], "events": horizon,
        })
    rows.sort(key=lambda r: r["ts"])
    half = len(rows) // 2
    a_set, b_set = rows[:half], rows[half:]

    traded = [r for r in rows if r["events"]]
    mech_mode, mech_n = Counter(r["mech"] for r in traded).most_common(1)[0]

    # Kingmaker sets, built on A only.
    early_in = defaultdict(set)
    for r in a_set:
        for w in r["earlyWallets"]:
            early_in[w].add(r["token"])
    ran_a = {r["token"] for r in a_set if r["ran"]}
    base_a = len(ran_a) / len(a_set)
    km_recur = {w for w, toks in early_in.items() if len(toks) >= RECUR_MIN}
    km_pick = set()
    for w, toks in early_in.items():
        grads = len(toks & ran_a)
        if len(toks) >= PICK_MIN and grads >= PICK_GRADS and grads / len(toks) >= PICK_LIFT * base_a:
            km_pick.add(w)

    return rows, a_set, b_set, km_pick, km_recur, mech_mode, mech_n, base_a, early_in, ran_a, meta


def main() -> None:
    rows, a_set, b_set, km_pick, km_recur, mech_mode, mech_n, base_a, early_in, ran_a, meta = build(EARLY)
    traded = [r for r in rows if r["events"]]
    report = {"meta": meta, "earlyWindowSec": EARLY, "ethLaunches": len(rows), "A": len(a_set), "B": len(b_set),
              "tradedLaunches": len(traded), "modalMechanics": list(mech_mode), "modalMechanicsCount": mech_n,
              "baseGradRateA": base_a, "kmRecurSize": len(km_recur), "kmPickSize": len(km_pick), "tests": {}}
    print(f"ETH launches {len(rows)} (A {len(a_set)}, B {len(b_set)}); traded {len(traded)}")
    print(f"modal curve math (phantomQuote, feeBps, creatorTaxBps, gradThreshold) = {mech_mode} on {mech_n} traded launches")
    print(f"A graduation base rate {base_a:.4%}; KM-recur {len(km_recur)} wallets; KM-pick {len(km_pick)} wallets")

    for km_name, km in (("KM-pick", km_pick), ("KM-recur", km_recur)):
        for uni_name in ("contested", "contested+same-math", "full"):
            sample = []
            for r in b_set:
                retail_early = r["earlyWallets"] - km
                if uni_name.startswith("contested") and len(retail_early) < CONTESTED_MIN:
                    continue
                if uni_name == "contested+same-math" and r["mech"] != mech_mode:
                    continue
                r2 = dict(r)
                r2["blessed"] = int(bool(r["earlyWallets"] & km))
                retail_buys = [e for e in r["early"] if e["wallet"] not in km]
                r2["retailEarlyN"] = math.log1p(len(retail_early))
                r2["retailEarlyEth"] = math.log1p(sum(e["quote"] for e in retail_buys) / WEI)
                r2["logDev"] = math.log1p(r["devBuyEth"])
                r2["hs"], r2["hc"] = math.sin(2 * math.pi * r["hour"] / 24), math.cos(2 * math.pi * r["hour"] / 24)
                sample.append(r2)
            key = f"{km_name}/{uni_name}"
            res = test_block(sample, km)
            report["tests"][key] = res
            print_block(key, res)

    top = sorted(km_pick, key=lambda w: (-len(early_in[w] & ran_a), -len(early_in[w])))[:15]
    report["kmPickTop"] = [{"wallet": w, "earlyA": len(early_in[w]), "gradA": len(early_in[w] & ran_a)} for w in top]
    (DATA / f"pons-results-early{EARLY}.json").write_text(json.dumps(report, indent=1, default=str))


def test_block(sample: list[dict], km: set) -> dict:
    if not sample:
        return {"n": 0}
    out = {"n": len(sample)}
    for label in ("ran", "ran5x"):
        a = sum(1 for r in sample if r["blessed"] and r[label])
        b = sum(1 for r in sample if r["blessed"] and not r[label])
        c = sum(1 for r in sample if not r["blessed"] and r[label])
        d = sum(1 for r in sample if not r["blessed"] and not r[label])
        rb = a / (a + b) if a + b else float("nan")
        ru = c / (c + d) if c + d else float("nan")
        out[label] = {"blessed": [a, a + b], "unblessed": [c, c + d], "rateBlessed": rb, "rateUnblessed": ru,
                      "riskRatio": rb / ru if ru else float("inf"), "fisherP": fisher_greater(a, b, c, d)}
    mech = ["logDev", "creatorTaxBps", "hs", "hc"]
    for k in ("devBuyEth", "creatorTaxBps", "hour"):
        bl = sorted(r[k] for r in sample if r["blessed"])
        un = sorted(r[k] for r in sample if not r["blessed"])
        out.setdefault("mechanicsMedian", {})[k] = [bl[len(bl) // 2] if bl else None, un[len(un) // 2] if un else None]
    if 10 <= sum(r["ran"] for r in sample) < len(sample) - 10:
        # Keep every launch that ran; cap the rest at 6,000 so pure-Python CV stays tractable.
        pos = [r for r in sample if r["ran"]]
        neg = [r for r in sample if not r["ran"]]
        if len(neg) > 6000:
            neg = random.Random(11).sample(neg, 6000)
            out["cvSubsampledNegatives"] = 6000
        cv = pos + neg
        out["cvAucMechanics"] = cv_auc(cv, mech, "ran")
        out["cvAucMechanicsPlusBlessed"] = cv_auc(cv, mech + ["blessed"], "ran")
    out["demandMatched"] = demand_matched(sample)
    flows = {"km": [0, 0, 0], "creator": [0, 0, 0], "retail": [0, 0, 0]}  # eth in, eth out, tokens net
    km_sell_before_peak = km_sell_total = 0
    retail_before = retail_after = 0
    for r in sample:
        if not r["blessed"]:
            continue
        first_km = min((e["ts"] for e in r["early"] if e["wallet"] in km), default=None)
        for e in r["events"]:
            if e["kind"] == "done":
                continue
            g = "creator" if e["wallet"] == r["creator"] else ("km" if e["wallet"] in km else "retail")
            if e["kind"] == "buy":
                flows[g][0] += e["quote"]
                flows[g][2] += e["tokens"]
                if g == "retail" and first_km is not None:
                    if e["ts"] < first_km:
                        retail_before += e["quote"]
                    else:
                        retail_after += e["quote"]
            else:
                flows[g][1] += e["quote"]
                flows[g][2] -= e["tokens"]
                if g == "km":
                    km_sell_total += e["quote"]
                    if e["ts"] <= r["peakTs"]:
                        km_sell_before_peak += e["quote"]
    out["drain"] = {g: {"ethIn": v[0] / WEI, "ethOut": v[1] / WEI, "netEth": (v[1] - v[0]) / WEI,
                        "netTokens": v[2] / WEI} for g, v in flows.items()}
    out["kmSellShareBeforePeak"] = km_sell_before_peak / km_sell_total if km_sell_total else None
    out["retailBuyEthBeforeFirstKm"] = retail_before / WEI
    out["retailBuyEthAfterFirstKm"] = retail_after / WEI
    return out


def demand_matched(sample: list[dict]) -> dict:
    """Exploratory, not pre-registered: match each blessed launch to the unblessed launch with the
    nearest early retail demand (retail wallet count, retail ETH) and launch mechanics."""
    feats = ["retailEarlyN", "retailEarlyEth", "logDev", "creatorTaxBps"]
    bl = [r for r in sample if r["blessed"]]
    un = [r for r in sample if not r["blessed"]]
    if not bl or not un:
        return {}
    mu = {k: sum(r[k] for r in sample) / len(sample) for k in feats}
    sd = {k: (sum((r[k] - mu[k]) ** 2 for r in sample) / len(sample)) ** 0.5 or 1.0 for k in feats}
    z = lambda r: tuple((r[k] - mu[k]) / sd[k] for k in feats)
    uz = [(z(u), u) for u in un]
    out = {"medians": {}}
    for k in ("retailEarlyN", "retailEarlyEth"):
        a = sorted(math.expm1(r[k]) for r in bl)
        b = sorted(math.expm1(r[k]) for r in un)
        out["medians"][k] = [a[len(a) // 2], b[len(b) // 2]]
    for label in ("ran", "ran5x"):
        diffs, dist = [], []
        for r in bl:
            zr = z(r)
            d, u = min(((sum((x - y) ** 2 for x, y in zip(zr, v)), u) for v, u in uz), key=lambda t: t[0])
            diffs.append(r[label] - u[label])
            dist.append(d ** 0.5)
        obs = sum(diffs) / len(diffs)
        rng = random.Random(13)
        hits = sum(1 for _ in range(2000)
                   if sum(x * rng.choice((1, -1)) for x in diffs) / len(diffs) >= obs)
        out[label] = {"n": len(diffs), "blessedRate": sum(r[label] for r in bl) / len(bl),
                      "matchedUnblessedRate": sum(r[label] for r in bl) / len(bl) - obs,
                      "diff": obs, "signFlipP": (hits + 1) / 2001,
                      "medianMatchDist": sorted(dist)[len(dist) // 2]}
    return out


def print_block(key: str, res: dict) -> None:
    print(f"\n== {key}: n={res['n']}")
    if not res["n"]:
        return
    for label in ("ran", "ran5x"):
        t = res[label]
        print(f"  {label:6s} blessed {t['blessed'][0]}/{t['blessed'][1]} ({t['rateBlessed']:.2%}) vs "
              f"unblessed {t['unblessed'][0]}/{t['unblessed'][1]} ({t['rateUnblessed']:.2%}); "
              f"RR {t['riskRatio']:.2f}; Fisher p {t['fisherP']:.2e}")
    print(f"  mechanics medians [blessed, unblessed]: {res['mechanicsMedian']}")
    if "cvAucMechanics" in res:
        print(f"  CV AUC mechanics {res['cvAucMechanics']:.3f} -> +blessed {res['cvAucMechanicsPlusBlessed']:.3f}")
    for g, v in res["drain"].items():
        print(f"  {g:8s} in {v['ethIn']:10.4f} ETH  out {v['ethOut']:10.4f} ETH  net {v['netEth']:+10.4f} ETH  "
              f"tokens held {v['netTokens']:,.0f}")
    dm = res.get("demandMatched") or {}
    if dm:
        print(f"  early retail demand medians [blessed, unblessed]: wallets {dm['medians']['retailEarlyN']}, "
              f"ETH {[round(x, 4) for x in dm['medians']['retailEarlyEth']]}")
        for label in ("ran", "ran5x"):
            m = dm[label]
            print(f"  demand-matched {label}: blessed {m['blessedRate']:.2%} vs matched unblessed "
                  f"{m['matchedUnblessedRate']:.2%} (n {m['n']}, p {m['signFlipP']:.4f}, dist {m['medianMatchDist']:.2f})")
    print(f"  KM sell ETH before 24h peak: {res['kmSellShareBeforePeak']}; retail buy ETH before/after first KM buy: "
          f"{res['retailBuyEthBeforeFirstKm']:.4f} / {res['retailBuyEthAfterFirstKm']:.4f}")


if __name__ == "__main__":
    main()
