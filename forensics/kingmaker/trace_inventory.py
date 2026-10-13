#!/usr/bin/env python3
"""Where did kingmakers' extra tokens come from? Every ERC-20 Transfer of a blessed contested
held-out token into a KM-pick wallet that did not come from that token's curve, classified by sender.

Writes .local/kingmaker/km-inflows.json and prints the coalition table.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
from collections import Counter, defaultdict

from eth_hash.auto import keccak

import analyze_pons as ap
from collect_pons import call, get_logs

TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
ZERO = "0x" + "0" * 40
WEI = 10 ** 18


def pad(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def main():
    rows, _, b_set, km, *_ = ap.build(600)
    blessed = [r for r in b_set if len(r["earlyWallets"] - km) >= ap.CONTESTED_MIN and r["earlyWallets"] & km]
    by_token = {r["token"]: r for r in blessed}
    launches = json.loads((ap.DATA / "pons-launches.json").read_text())["launches"]
    first_block = {l["token"]: l["block"] for l in launches}
    all_creators = {l["creator"] for l in launches}
    curves = {l["curve"] for l in launches}
    head = int(call("eth_blockNumber", []), 16)
    blocks_per_day = 900_000  # generous: ~10 blocks/s on chain 4663
    km_sorted = [pad(w) for w in sorted(km)]
    km_groups = [km_sorted[i:i + 700] for i in range(0, len(km_sorted), 700)]  # RPC caps filter entries

    tokens = sorted(by_token, key=lambda t: first_block[t])
    batches = [tokens[i:i + 300] for i in range(0, len(tokens), 300)]

    def pull(job):
        batch, group = job
        lo = min(first_block[t] for t in batch)
        hi = min(head, max(first_block[t] for t in batch) + blocks_per_day)
        return get_logs(batch, [TRANSFER, None, group], lo, hi, chunk=50_000)

    logs = []
    with cf.ThreadPoolExecutor(4) as ex:
        for part in ex.map(pull, [(b, g) for b in batches for g in km_groups]):
            logs += part
    print(f"Transfer logs into kingmaker wallets for {len(tokens)} blessed tokens: {len(logs)}", flush=True)

    rows_out, senders = [], set()
    for lg in logs:
        token = lg["address"].lower()
        r = by_token.get(token)
        if not r:
            continue
        ts = int(lg["blockTimestamp"], 16)
        if ts - r["ts"] > ap.DAY:
            continue
        frm, to = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
        if frm == r["curve"]:
            continue  # ordinary curve buy
        amt = int(lg["data"], 16) if lg["data"] not in ("0x", "") else 0
        rows_out.append({"token": token, "from": frm, "to": to, "amount": amt, "ts": ts, "tx": lg["transactionHash"]})
        senders.add(frm)

    code = {}
    def is_contract(a):
        return a, call("eth_getCode", [a, "latest"]) not in ("0x", "0x0")
    with cf.ThreadPoolExecutor(8) as ex:
        code.update(dict(ex.map(is_contract, sorted(senders))))

    def kind(row):
        f, r = row["from"], by_token[row["token"]]
        if f == ZERO:
            return "mint"
        if f == r["creator"]:
            return "this token's creator"
        if f in km:
            return "another kingmaker"
        if f in all_creators:
            return "creator of another launch"
        if f == POOL_MANAGER:
            return "v4 PoolManager (post-graduation swap)"
        if f in curves:
            return "another curve"
        return "other contract (router, aggregator)" if code.get(f) else "other wallet (EOA)"

    tally, tok = Counter(), Counter()
    for row in rows_out:
        k = kind(row)
        row["kind"] = k
        tally[k] += 1
        tok[k] += row["amount"]
    total = sum(tok.values()) or 1
    print("\nNon-curve token inflows into kingmaker wallets (blessed contested held-out launches, 24 h):")
    for k, n in tally.most_common():
        print(f"  {k:40s} transfers {n:7d} | tokens {tok[k] / WEI:>20,.0f} ({tok[k] / total:.1%})")
    km_km = defaultdict(set)
    for row in rows_out:
        if row["kind"] == "another kingmaker":
            km_km[row["from"]].add(row["to"])
    linked = set(km_km) | {w for v in km_km.values() for w in v}
    print(f"\nkingmaker wallets linked by direct token hand-offs: {len(linked)} of {len(km)}")
    (ap.DATA / "km-inflows.json").write_text(json.dumps({"rows": rows_out, "tally": tally, "tokens": tok}, default=str))


if __name__ == "__main__":
    main()
