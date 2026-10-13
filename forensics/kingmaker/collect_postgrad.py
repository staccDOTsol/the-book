#!/usr/bin/env python3
"""Post-graduation trading in the Pons v4 pool of every held-out (B) launch that graduated.

Finds each graduate's Pons-hook pool (v4 Initialize with the Pons hook and the token as a currency),
pulls every Swap through the collection head, and attaches the trade's tx.from.
Writes .local/kingmaker/postgrad.json.
"""
from __future__ import annotations

import json

import analyze_pons as ap
from collect_pons import call, get_logs
from collect_side_events import SWAP, words
from collect_side_lp import INITIALIZE, PONS_HOOK, POOL_MANAGER, a40, signed, tx_senders


def main():
    rows, _, b_set, *_ = ap.build(600)
    grads = {r["token"]: r for r in b_set if r["ran"]}
    head = int(call("eth_blockNumber", []), 16)
    meta = json.loads((ap.DATA / "pons-launches.json").read_text())["meta"]
    lo = min(e["block"] for r in grads.values() for e in r["events"] if e["kind"] == "done")

    pools = {}
    for lg in get_logs(POOL_MANAGER, [INITIALIZE], lo - 10, head, chunk=20_000):
        c0, c1 = a40(lg["topics"][2]), a40(lg["topics"][3])
        tok = c0 if c0 in grads else (c1 if c1 in grads else None)
        hooks = "0x" + lg["data"][2 + 64 * 2 + 24: 2 + 64 * 3]
        if tok and hooks.lower() == PONS_HOOK:
            pools[lg["topics"][1].lower()] = {"token": tok, "other": c1 if tok == c0 else c0,
                                             "block": int(lg["blockNumber"], 16), "ts": int(lg["blockTimestamp"], 16),
                                             "sqrtP": str(int(words(lg["data"])[3], 16))}
    print(f"graduates {len(grads)}; Pons pools found {len(pools)}", flush=True)

    swaps = []
    ids = list(pools)
    for i in range(0, len(ids), 300):
        group = ids[i:i + 300]
        for lg in get_logs(POOL_MANAGER, [SWAP, group], min(pools[k]["block"] for k in group), head, chunk=50_000):
            w = words(lg["data"])
            swaps.append({"pool": lg["topics"][1].lower(), "sender": a40(lg["topics"][2]),
                          "a0": str(signed(w[0])), "a1": str(signed(w[1])), "sqrtP": str(int(w[2], 16)),
                          "tx": lg["transactionHash"], "block": int(lg["blockNumber"], 16),
                          "txIndex": int(lg["transactionIndex"], 16), "logIndex": int(lg["logIndex"], 16),
                          "ts": int(lg["blockTimestamp"], 16)})
    print(f"post-graduation swaps: {len(swaps)}", flush=True)
    senders = tx_senders(sorted({s["tx"] for s in swaps}))
    for s in swaps:
        s["from"] = senders[s["tx"]]
    (ap.DATA / "postgrad.json").write_text(json.dumps({"head": head, "pools": pools, "swaps": swaps}))
    print("wrote postgrad.json", flush=True)


if __name__ == "__main__":
    main()
