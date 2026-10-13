#!/usr/bin/env python3
"""Frozen test on the untouched holdout window, streaming the (multi-GB) trades file.

Keeps only launches with the frozen identical curve math (the only ones the test uses), then runs
confirm_fresh.run() twice: the frozen rig list unchanged, and the frozen rule re-applied (rigs
re-derived on the window's first half, tested on its second half).
Usage: KINGMAKER_DATA=.local/kingmaker-holdout python holdout_stream.py
"""
import json
from collections import defaultdict

import analyze_pons as ap
import confirm_fresh as cfz

ETH = "0x" + "0" * 40


def stream_trades(path, keep):
    """Yield trade dicts for curves in `keep` from {"meta":..., "trades":[...]} without loading it whole."""
    dec = json.JSONDecoder()
    with open(path) as f:
        buf = f.read(1 << 24)
        start = buf.index('"trades": [') + len('"trades": [')
        buf = buf[start:]
        while True:
            i = 0
            while True:
                while i < len(buf) and buf[i] in ", \n":
                    i += 1
                if i < len(buf) and buf[i] == "]":
                    return
                try:
                    obj, j = dec.raw_decode(buf, i)
                except json.JSONDecodeError:
                    break  # incomplete object at the end of the buffer
                if obj["curve"] in keep:
                    yield obj
                i = j
            more = f.read(1 << 24)
            if not more:
                return
            buf = buf[i:] + more


def load():
    """Return (rows, adds, frozen rigs, rule-derived rigs, second-half rows) for the holdout window."""
    frozen = cfz.FROZEN
    math_key = tuple(frozen["curveMath"])
    data = json.loads((ap.DATA / "pons-launches.json").read_text())
    head_ts = data["meta"]["headTs"]
    launches = {l["curve"]: l for l in data["launches"]
                if l["pairToken"] == ETH and l["ts"] <= head_ts - ap.DAY
                and (l.get("phantomQuote"), l.get("feeBps"), l.get("creatorTaxBps"), l.get("graduationThresholdCall")) == math_key}
    print(f"holdout launches with the frozen identical curve math: {len(launches)}", flush=True)

    events = defaultdict(list)
    dev = defaultdict(float)
    n = 0
    for t in stream_trades(ap.DATA / "pons-trades.json", launches):
        l = launches[t["curve"]]
        if t["ts"] - l["ts"] > ap.DAY:
            continue
        n += 1
        if t["kind"] == "buy" and (t["tx"] == l["tx"] or t["wallet"] == l["creator"]):
            dev[t["curve"]] += t["quote"] / ap.WEI
        events[t["curve"]].append({k: t.get(k) for k in ("kind", "ts", "wallet", "quote", "tokens", "fee", "tax")})
    print(f"kept {n} curve events", flush=True)

    rows = []
    for c, l in launches.items():
        evs = sorted(events.get(c, []), key=lambda e: e["ts"])
        done = [e for e in evs if e["kind"] == "done"]
        rows.append({"token": l["token"], "ts": l["ts"], "creator": l["creator"], "mech": math_key,
                     "devBuyEth": dev[c], "events": evs, "gradTs": done[0]["ts"] if done else None})
    rows.sort(key=lambda r: r["ts"])

    side = json.loads((ap.DATA / "side-lp.json").read_text())
    adds = defaultdict(list)
    for e in side["liquidity"]:
        p = side["pools"].get(e["pool"])
        if p and int(e["delta"]) > 0:
            adds[p["token"]].append((e["ts"], e["from"]))

    rigs = set(frozen["rigs"])
    first, second = rows[:len(rows) // 2], rows[len(rows) // 2:]
    first_tok = {r["token"]: r for r in first}
    by_actor = defaultdict(set)
    for tok, evs in adds.items():
        r = first_tok.get(tok)
        if r:
            for ts, a in evs:
                if ts - r["ts"] <= ap.DAY:
                    by_actor[a].add(tok)
    rule_rigs = {a for a, t in by_actor.items() if len(t) >= frozen["rules"]["rigMinFirstHalfLaunches"]}
    return rows, adds, rigs, rule_rigs, second


def main():
    rows, adds, rigs, rule_rigs, second = load()
    math_key = tuple(cfz.FROZEN["curveMath"])
    cfz.run(rows, rigs, math_key, adds, "frozen rig list, unchanged, on the whole holdout window")
    print(f"\nfrozen rule re-applied: {len(rule_rigs)} rigs from the holdout's first half "
          f"({len(rule_rigs & rigs)} also on the frozen list)")
    cfz.run(second, rule_rigs, math_key, adds, "frozen rule, rigs re-derived, tested on holdout second half")


if __name__ == "__main__":
    main()
