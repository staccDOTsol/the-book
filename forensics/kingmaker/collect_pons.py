#!/usr/bin/env python3
"""Collect every Pons V2 launch on Robinhood Chain (4663) and every curve trade by wallet.

Read-only. Writes .local/kingmaker/pons-launches.json and pons-trades.json.
Sources: factory TokenLaunched logs, per-curve immutables via eth_call,
CurveBuy/CurveSell/CurveCompleted logs, block timestamps for every trade block.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from eth_hash.auto import keccak

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("KINGMAKER_DATA", ROOT / ".local/kingmaker"))
ENV = dict(line.strip().split("=", 1) for line in open(ROOT / ".local/pons-rpc.env") if "=" in line)
RPC = ENV["PONS_HTTP_RPC_URL"]
FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"


def topic(sig: str) -> str:
    return "0x" + keccak(sig.encode()).hex()


TOKEN_LAUNCHED = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
CURVE_BUY = topic("CurveBuy(address,address,uint256,uint256,uint256,uint256)")
CURVE_SELL = topic("CurveSell(address,address,uint256,uint256,uint256,uint256)")
CURVE_DONE = topic("CurveCompleted(address,uint256,uint256)")
GETTERS = ["pairToken()", "deployer()", "phantomQuote()", "feeBps()", "creatorTaxBps()",
           "graduationThreshold()", "graduated()"]


def call(method: str, params: list, tries: int = 6):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(RPC, data=body, headers={
                "content-type": "application/json", "user-agent": "kingmaker-test/1"})
            res = json.load(urllib.request.urlopen(req, timeout=60))
            if "result" in res:
                return res["result"]
            err = res.get("error")
        except Exception as exc:  # network
            err = str(exc)
        time.sleep(0.5 + i)
    raise RuntimeError(f"{method} failed: {err}")


def get_logs(address, topics, start: int, end: int, chunk: int = 200_000) -> list:
    spans = [(a, min(a + chunk - 1, end)) for a in range(start, end + 1, chunk)]

    def one(span):
        a, z = span
        flt = {"topics": topics, "fromBlock": hex(a), "toBlock": hex(z)}
        if address:
            flt["address"] = address
        try:
            return call("eth_getLogs", [flt], tries=2)
        except RuntimeError:
            if z - a < 1000:
                raise
            mid = (a + z) // 2
            return one((a, mid)) + one((mid + 1, z))

    with cf.ThreadPoolExecutor(8) as ex:
        parts = list(ex.map(one, spans))
    return [log for part in parts for log in part]


def word(data: str, i: int) -> int:
    return int(data[2 + 64 * i: 2 + 64 * (i + 1)], 16)


def addr(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:].lower()


def deploy_block(head: int) -> int:
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if call("eth_getCode", [FACTORY, hex(mid)]) not in ("0x", "0x0"):
            hi = mid
        else:
            lo = mid + 1
    return lo


def block_ts(number: int) -> int:
    return int(call("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)


def block_at(ts: int, lo: int, hi: int) -> int:
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    days = float(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 14.0
    # --end-days-ago N: end the window N days before the chain head (an untouched holdout period).
    end_ago = float(sys.argv[sys.argv.index("--end-days-ago") + 1]) if "--end-days-ago" in sys.argv else 0.0
    chain_head = int(call("eth_blockNumber", []), 16)
    deployed = deploy_block(chain_head)
    head_ts = block_ts(chain_head) - int(end_ago * 86400)
    head = block_at(head_ts, deployed, chain_head) if end_ago else chain_head
    start = block_at(head_ts - int((days + 1) * 86400), deployed, head)
    launch_end = block_at(head_ts - 86400, start, head)
    print(f"factory deployed {deployed}; launches {start}..{launch_end}; trades to head {head}", flush=True)

    launches = []
    for log in get_logs(FACTORY, [TOKEN_LAUNCHED], start, launch_end, chunk=20_000):
        launches.append({
            "token": addr(log["topics"][1]), "curve": addr(log["topics"][2]),
            "creator": addr(log["topics"][3]), "block": int(log["blockNumber"], 16),
            "tx": log["transactionHash"], "txIndex": int(log["transactionIndex"], 16),
            "logIndex": int(log["logIndex"], 16), "ts": int(log["blockTimestamp"], 16),
            "pairToken": "0x" + log["data"][26:66], "graduationThreshold": word(log["data"], 1)})
    print(f"TokenLaunched: {len(launches)}", flush=True)
    curves = {row["curve"]: row for row in launches}

    trades = []
    for log in get_logs(None, [[CURVE_BUY, CURVE_SELL, CURVE_DONE]], start, head, chunk=20_000):
        curve = log["address"].lower()
        if curve not in curves:
            continue
        kind = {CURVE_BUY: "buy", CURVE_SELL: "sell", CURVE_DONE: "done"}[log["topics"][0]]
        row = {"curve": curve, "kind": kind, "block": int(log["blockNumber"], 16),
               "txIndex": int(log["transactionIndex"], 16), "logIndex": int(log["logIndex"], 16),
               "tx": log["transactionHash"], "ts": int(log["blockTimestamp"], 16)}
        d = log["data"]
        if kind == "buy":
            row.update(caller=addr(log["topics"][1]), wallet=addr(log["topics"][2]),
                       quote=word(d, 0), tokens=word(d, 1), fee=word(d, 2), tax=word(d, 3))
        elif kind == "sell":
            row.update(caller=addr(log["topics"][1]), wallet=addr(log["topics"][2]),
                       tokens=word(d, 0), quote=word(d, 1), fee=word(d, 2), tax=word(d, 3))
        else:
            row.update(wallet="0x" + d[26:66], quote=word(d, 1), tokens=word(d, 2))
        trades.append(row)
    print(f"curve events: {len(trades)}", flush=True)

    traded = sorted({t["curve"] for t in trades})
    sels = {sig[:-2]: "0x" + keccak(sig.encode()).hex()[:8] for sig in GETTERS if sig != "pairToken()"}
    jobs = [(c, k, v) for c in traded for k, v in sels.items()]

    def batch(chunk):
        reqs = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                 "params": [{"to": c, "data": v}, "latest"]} for i, (c, k, v) in enumerate(chunk)]
        for i in range(6):
            try:
                req = urllib.request.Request(RPC, data=json.dumps(reqs).encode(), headers={
                    "content-type": "application/json", "user-agent": "kingmaker-test/1"})
                res = {r["id"]: r for r in json.load(urllib.request.urlopen(req, timeout=60))}
                return [(c, k, res[i]["result"]) for i, (c, k, v) in enumerate(chunk)]
            except Exception:
                time.sleep(1 + i)
        raise RuntimeError("eth_call batch failed")

    with cf.ThreadPoolExecutor(8) as ex:
        for part in ex.map(batch, [jobs[i:i + 50] for i in range(0, len(jobs), 50)]):
            for c, k, v in part:
                row = curves[c]
                if k == "graduationThreshold":
                    k = "graduationThresholdCall"  # TokenLaunched data word 1 is not the threshold
                row[k] = ("0x" + v[-40:]) if k == "deployer" else (
                    bool(int(v, 16)) if k == "graduated" else int(v, 16))
    print(f"immutables for {len(traded)} traded curves", flush=True)

    meta = {"chainId": 4663, "factory": FACTORY, "fromBlock": start, "launchToBlock": launch_end,
            "toBlock": head, "headTs": head_ts, "days": days,
            "collectedAt": int(time.time())}
    (OUT / "pons-launches.json").write_text(json.dumps({"meta": meta, "launches": launches}))
    (OUT / "pons-trades.json").write_text(json.dumps({"meta": meta, "trades": trades}))
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    sys.exit(main())
