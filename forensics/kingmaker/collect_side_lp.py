#!/usr/bin/env python3
"""Every v4/v3 side pool on a Pons launch token in the collection window, and every liquidity add
with the adding transaction's sender. Writes .local/kingmaker/side-lp.json (Amendment 4)."""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
import urllib.request
from pathlib import Path

from eth_hash.auto import keccak

from collect_pons import RPC, call, get_logs

DATA = Path(os.environ.get("KINGMAKER_DATA", Path(__file__).resolve().parents[2] / ".local/kingmaker"))
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
MODIFY = "0x" + keccak(b"ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)").hex()
V3_MINT = "0x" + keccak(b"Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
V3_POOL_CREATED = "0x" + keccak(b"PoolCreated(address,address,uint24,int24,address)").hex()


def a40(t: str) -> str:
    return "0x" + t[-40:].lower()


def signed(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def tx_senders(hashes: list[str]) -> dict:
    out = {}

    def batch(chunk):
        reqs = [{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionByHash", "params": [h]}
                for i, h in enumerate(chunk)]
        for k in range(6):
            try:
                req = urllib.request.Request(RPC, data=json.dumps(reqs).encode(), headers={
                    "content-type": "application/json", "user-agent": "kingmaker-test/1"})
                res = {r["id"]: r for r in json.load(urllib.request.urlopen(req, timeout=60))}
                return {h: res[i]["result"]["from"].lower() for i, h in enumerate(chunk)}
            except Exception:
                time.sleep(1 + k)
        raise RuntimeError("tx batch failed")

    with cf.ThreadPoolExecutor(8) as ex:
        for part in ex.map(batch, [hashes[i:i + 50] for i in range(0, len(hashes), 50)]):
            out.update(part)
    return out


def main():
    launches = json.loads((DATA / "pons-launches.json").read_text())
    meta, L = launches["meta"], launches["launches"]
    tokens = {l["token"] for l in L}
    lo, hi = meta["fromBlock"], meta["toBlock"]

    pools = {}
    for lg in get_logs(POOL_MANAGER, [INITIALIZE], lo, hi, chunk=20_000):
        c0, c1 = a40(lg["topics"][2]), a40(lg["topics"][3])
        tok = c0 if c0 in tokens else (c1 if c1 in tokens else None)
        hooks = "0x" + lg["data"][2 + 64 * 2 + 24: 2 + 64 * 3]
        if tok and hooks.lower() != PONS_HOOK:
            pools[lg["topics"][1].lower()] = {"v": 4, "token": tok, "other": c1 if tok == c0 else c0,
                                             "fee": int(lg["data"][2:66], 16), "hooks": hooks.lower(),
                                             "block": int(lg["blockNumber"], 16), "ts": int(lg["blockTimestamp"], 16)}
    print(f"v4 side pools on window tokens: {len(pools)}", flush=True)
    for lg in get_logs(V3_FACTORY, [V3_POOL_CREATED], lo, hi, chunk=50_000):
        t0, t1 = a40(lg["topics"][1]), a40(lg["topics"][2])
        tok = t0 if t0 in tokens else (t1 if t1 in tokens else None)
        if tok:
            addr = "0x" + lg["data"][2 + 64 + 24: 2 + 128]
            pools[addr.lower()] = {"v": 3, "token": tok, "other": t1 if tok == t0 else t0,
                                   "fee": int(lg["topics"][3], 16), "block": int(lg["blockNumber"], 16),
                                   "ts": int(lg["blockTimestamp"], 16)}
    print(f"+ v3 side pools: {sum(1 for p in pools.values() if p['v'] == 3)}", flush=True)

    adds = []
    v4_ids = [k for k, p in pools.items() if p["v"] == 4]
    for i in range(0, len(v4_ids), 600):
        group = v4_ids[i:i + 600]
        start = min(pools[k]["block"] for k in group)
        for lg in get_logs(POOL_MANAGER, [MODIFY, group], start, hi, chunk=50_000):
            d = lg["data"]
            delta = signed(d[2 + 128: 2 + 192])
            adds.append({"pool": lg["topics"][1].lower(), "sender": a40(lg["topics"][2]), "delta": str(delta),
                         "tx": lg["transactionHash"], "block": int(lg["blockNumber"], 16),
                         "ts": int(lg["blockTimestamp"], 16)})
    v3_addrs = [k for k, p in pools.items() if p["v"] == 3]
    for i in range(0, len(v3_addrs), 300):
        group = v3_addrs[i:i + 300]
        start = min(pools[k]["block"] for k in group)
        for lg in get_logs(group, [V3_MINT], start, hi, chunk=50_000):
            adds.append({"pool": lg["address"].lower(), "sender": a40(lg["topics"][1]),
                         "delta": str(int(lg["data"][2 + 64: 2 + 128], 16)), "tx": lg["transactionHash"],
                         "block": int(lg["blockNumber"], 16), "ts": int(lg["blockTimestamp"], 16)})
    print(f"liquidity events on side pools: {len(adds)} (positive adds {sum(int(a['delta']) > 0 for a in adds)})", flush=True)

    senders = tx_senders(sorted({a["tx"] for a in adds}))
    for a in adds:
        a["from"] = senders[a["tx"]]
    (DATA / "side-lp.json").write_text(json.dumps({"meta": meta, "pools": pools, "liquidity": adds}))
    print("wrote side-lp.json", flush=True)


if __name__ == "__main__":
    main()
