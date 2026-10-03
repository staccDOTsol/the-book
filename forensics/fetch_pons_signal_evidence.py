#!/usr/bin/env python3
"""Fetch public Pons registration, launch, and outside-pool initializer evidence.

This reads RH JSON-RPC only. It uses the decoded 24h v4 Initialize cache as
input, then writes the three small evidence caches consumed by
v4_pons_signal_screen.py. It does not use credentials or send transactions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from high_fee_pool_screen import atomic_write
from rig_monitor import RPC_URL, Rpc
from v4_pons_signal_screen import (BIRTHS, INIT_TXS, LAUNCHED, PONS_HOOK,
                                   REGISTERED, ZERO_HOOK, order, register_word)


FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
POOL_REGISTERED_TOPIC = "0x01bf263a1db1652580721573296e1a1fa70b3d4c87f61d02a69c4e1109d2d573"
TOKEN_LAUNCHED_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
MAX_MULTI_TOPIC_BLOCK_SPAN = 99_999
MAX_SINGLE_TOPIC_BLOCK_SPAN = 9_999_999


def fetch_registered(rpc: Rpc, births: list[dict]) -> list[dict]:
    ids = sorted({birth["poolId"] for birth in births})
    first, last = min(birth["birthBlock"] for birth in births), max(birth["birthBlock"] for birth in births)
    logs = []
    for start in range(first, last + 1, MAX_MULTI_TOPIC_BLOCK_SPAN):
        end = min(last, start + MAX_MULTI_TOPIC_BLOCK_SPAN - 1)
        logs.extend(rpc.call("eth_getLogs", [{"address": PONS_HOOK,
                                              "topics": [POOL_REGISTERED_TOPIC, ids],
                                              "fromBlock": hex(start), "toBlock": hex(end)}]))
    if {log["topics"][1].lower() for log in logs} != set(ids) or len(logs) != len(ids):
        raise ValueError("Pons PoolRegistered evidence does not match Initialize pool IDs")
    return logs


def fetch_launched(rpc: Rpc, births: list[dict], registrations: list[dict]) -> list[dict]:
    by_id = {birth["poolId"]: birth for birth in births}
    logs = []
    for registration in registrations:
        pool_id = registration["topics"][1].lower()
        token = register_word(registration["data"], 0)
        birth_block = by_id[pool_id]["birthBlock"]
        result = rpc.call("eth_getLogs", [{"address": FACTORY,
                                           "topics": [TOKEN_LAUNCHED_TOPIC,
                                                      "0x" + token[2:].rjust(64, "0")],
                                           "fromBlock": hex(max(0, birth_block - MAX_SINGLE_TOPIC_BLOCK_SPAN + 1)),
                                           "toBlock": hex(birth_block)}])
        if len(result) != 1:
            raise ValueError(f"Expected one factory TokenLaunched for {token}; got {len(result)}")
        logs.extend(result)
    return logs


def fetch_first_independent_initializers(rpc: Rpc, all_births: list[dict],
                                         registrations: list[dict]) -> dict[str, dict]:
    by_token = {}
    for registration in registrations:
        token = register_word(registration["data"], 0)
        by_token[token] = None
    for birth in sorted(all_births, key=order):
        token = birth["token"]
        if (token in by_token and by_token[token] is None and birth["hooks"] == ZERO_HOOK
                and 0 < birth["feePips"] < 1_000_000 and birth["quoteAsset"] is not None):
            by_token[token] = birth
    hashes = sorted({birth["birthTx"] for birth in by_token.values() if birth is not None})
    output = {}
    for offset in range(0, len(hashes), 50):
        part = hashes[offset:offset + 50]
        requests = [{"jsonrpc": "2.0", "id": i + 1, "method": "eth_getTransactionByHash",
                     "params": [tx]} for i, tx in enumerate(part)]
        results = rpc._request(requests)
        if not isinstance(results, list):
            raise RuntimeError("Transaction batch response is not a list")
        by_id = {item["id"]: item for item in results}
        for i, tx in enumerate(part, 1):
            item = by_id[i]
            if "error" in item or not item.get("result"):
                raise RuntimeError(f"Missing public Initialize transaction {tx}")
            row = item["result"]
            output[tx] = {"from": row["from"].lower(),
                          "to": row["to"].lower() if row["to"] else None,
                          "block": int(row["blockNumber"], 16)}
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--births", type=Path, default=BIRTHS)
    parser.add_argument("--registered-output", type=Path, default=REGISTERED)
    parser.add_argument("--launched-output", type=Path, default=LAUNCHED)
    parser.add_argument("--initializer-txs-output", type=Path, default=INIT_TXS)
    args = parser.parse_args()
    source = json.loads(args.births.read_text())
    hook_births = [birth for birth in source["births"]
                   if birth["hooks"] == PONS_HOOK
                   and birth["birthBlock"] >= source["birthFromBlock"]]
    if not hook_births:
        raise ValueError("No Pons hook births in supplied interval")
    rpc = Rpc(RPC_URL)
    registrations = fetch_registered(rpc, hook_births)
    launches = fetch_launched(rpc, hook_births, registrations)
    initializers = fetch_first_independent_initializers(rpc, source["births"], registrations)
    atomic_write(args.registered_output, {"logs": registrations})
    atomic_write(args.launched_output, {"logs": launches})
    atomic_write(args.initializer_txs_output, initializers)
    print(json.dumps({"registered": len(registrations), "launched": len(launches),
                      "firstIndependentInitializerTransactions": len(initializers),
                      "registeredOutput": str(args.registered_output),
                      "launchedOutput": str(args.launched_output),
                      "initializerTxsOutput": str(args.initializer_txs_output)}))


if __name__ == "__main__":
    main()
