#!/usr/bin/env python3
"""Read-only Pons-hook birth funding and burst timing audit for RH v4."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from statistics import median

from rig_monitor import MODIFY_LIQUIDITY, POOL_MANAGER, RPC_URL, Rpc, liquidity_delta
from v4_all_fee_bursts import scan_rule


ROOT = Path(__file__).resolve().parents[1]
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
STRICT = ROOT / ".local/v4-burst-strict-funded-signals-24h.json"
OUTPUT = ROOT / ".local/pons-birth-cadence-24h.json"
PONS = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
ZERO = "0x" + "0" * 40


def batch(rpc: Rpc, method: str, params: list[list]) -> list:
    values = []
    for offset in range(0, len(params), 10):
        chunk = params[offset:offset + 10]
        requests = []
        for item in chunk:
            request_id = rpc.next_id
            rpc.next_id += 1
            requests.append({"jsonrpc": "2.0", "id": request_id,
                             "method": method, "params": item})
        response = rpc._request(requests)
        if not isinstance(response, list):
            raise ValueError(f"Non-batch {method} response")
        by_id = {item["id"]: item for item in response}
        for request in requests:
            item = by_id.get(request["id"])
            if item is None or "error" in item or "result" not in item:
                raise ValueError(f"Missing {method} batch result: {item}")
            values.append(item["result"])
    return values


def main() -> None:
    source = json.loads(BIRTHS.read_text())
    ordered = sorted(source["births"], key=lambda birth: (
        birth["birthBlock"], birth["birthTransactionIndex"], birth["birthLogIndex"]))
    broad_pons = [birth for birth in ordered if birth["hooks"] == PONS
                  and birth["birthBlock"] >= source["birthFromBlock"]]
    pons = [birth for birth in ordered if birth["hooks"] == PONS
            and birth["token"] and birth["quoteAsset"]
            and birth["birthBlock"] >= source["birthFromBlock"]]
    if len({birth["token"] for birth in pons}) != len(pons):
        raise ValueError("Multiple Pons births for a token")
    rpc = Rpc(RPC_URL)
    receipts = batch(rpc, "eth_getTransactionReceipt",
                     [[birth["birthTx"]] for birth in pons])
    block_logs = batch(rpc, "eth_getLogs", [[{
        "address": POOL_MANAGER,
        "topics": [MODIFY_LIQUIDITY, birth["poolId"]],
        "fromBlock": hex(birth["birthBlock"]),
        "toBlock": hex(birth["birthBlock"]),
    }] for birth in pons])
    birth_rows = []
    for birth, receipt, logs in zip(pons, receipts, block_logs):
        if receipt is None or int(receipt["blockNumber"], 16) != birth["birthBlock"]:
            raise ValueError(f"Missing birth receipt {birth['poolId']}")
        own_logs = [log for log in receipt["logs"]
                    if log["address"].lower() == POOL_MANAGER
                    and log["topics"][:2] == [MODIFY_LIQUIDITY, birth["poolId"]]]
        birth_tx_delta = sum(liquidity_delta(log) for log in own_logs)
        completed_block_delta = sum(liquidity_delta(log) for log in logs)
        prior = sum(other["token"] == birth["token"] and other["quoteAsset"]
                    and (other["birthBlock"], other["birthTransactionIndex"],
                         other["birthLogIndex"]) <
                    (birth["birthBlock"], birth["birthTransactionIndex"],
                     birth["birthLogIndex"])
                    for other in ordered)
        birth_rows.append({"token": birth["token"], "poolId": birth["poolId"],
                           "birthBlock": birth["birthBlock"],
                           "birthTimestamp": birth["birthTimestamp"],
                           "birthTime": birth["birthTime"],
                           "quoteAsset": birth["quoteAsset"],
                           "priorQuotePools": prior,
                           "birthTransactionNetLp": str(birth_tx_delta),
                           "birthCompletedBlockNetLp": str(completed_block_delta),
                           "fundedAtCompletedBirthBlock": completed_block_delta > 0,
                           "birthTransactionModifyLogs": len(own_logs),
                           "birthCompletedBlockModifyLogs": len(logs)})
    by_token = {row["token"]: row for row in birth_rows}
    birth_stamps = sorted(row["birthTimestamp"] for row in birth_rows)
    interarrivals = [right - left for left, right in
                     zip(birth_stamps, birth_stamps[1:])]
    def eligible(birth: dict, mode: str) -> bool:
        return (birth["token"] and birth["quoteAsset"]
                and birth["feePips"] < 1_000_000
                and (birth["hooks"] == ZERO if mode == "zero_hook"
                     else birth["hooks"] in (ZERO, PONS)))
    scenarios = {}
    for mode in ("zero_hook", "pons_plus_zero_hook"):
        births = [birth for birth in ordered if eligible(birth, mode)]
        scenarios[mode] = {}
        for n, window in ((4, 300), (5, 600)):
            key = f"{n}/{window}"
            signals, left = scan_rule(births, n, window, source["birthFromBlock"])
            rows = []
            for signal in signals:
                pons_birth = by_token.get(signal["token"])
                rows.append({"token": signal["token"],
                             "entryBlock": signal["entryBlock"],
                             "entryTimestamp": signal["entryTimestamp"],
                             "signalPoolId": signal["signalPoolId"],
                             "ponsFundedBirthBlock": pons_birth["birthBlock"] if pons_birth else None,
                             "ponsToSignalSeconds": signal["entryTimestamp"] - pons_birth["birthTimestamp"]
                             if pons_birth else None})
            lags = [row["ponsToSignalSeconds"] for row in rows
                    if row["ponsToSignalSeconds"] is not None]
            scenarios[mode][key] = {"rawSignals": len(rows),
                                    "leftTruncatedTokens": left,
                                    "ponsTokenSignals": len(lags),
                                    "medianPonsToSignalSeconds": median(lags) if lags else None,
                                    "rows": rows}
    strict = json.loads(STRICT.read_text())["signals"]
    by_pool = {birth["poolId"]: birth for birth in ordered}
    excluded_in_strict_windows = []
    for signal in strict:
        if signal["funded"]["status"] != "funded_signal":
            continue
        excluded = [pool_id for pool_id in signal["funded"]["fundedWindowPoolIds"]
                    if by_pool[pool_id]["hooks"] not in (ZERO, PONS)]
        if excluded:
            excluded_in_strict_windows.append({"rule": signal["rule"],
                                               "token": signal["token"],
                                               "excludedPoolIds": excluded})
    result = {"schemaVersion": 1,
              "source": "RH PoolManager Initialize and ModifyLiquidity logs; each Pons birth receipt and completed birth block queried from public RPC",
              "ponsHook": PONS,
              "sampleBirthFromBlock": source["birthFromBlock"],
              "broadPonsPoolBirthsIncludingNonQuote": len(broad_pons),
              "nonQuotePonsPoolBirths": len(broad_pons) - len(birth_rows),
              "ponsQuotePoolBirths": len(birth_rows),
              "fundedPonsBirths": sum(row["fundedAtCompletedBirthBlock"] for row in birth_rows),
              "medianQuotePonsInterarrivalSeconds": median(interarrivals) if interarrivals else None,
              "ponsFirstQuotePoolTokens": sum(row["priorQuotePools"] == 0 for row in birth_rows),
              "quoteAssetCounts": dict(Counter(row["quoteAsset"] for row in birth_rows)),
              "strictPreviouslyComputedFundedRows": sum(
                  signal["funded"]["status"] == "funded_signal" for signal in strict),
              "excludedOtherHookPoolsInStrictFundedWindows": excluded_in_strict_windows,
              "scenarios": scenarios, "births": birth_rows}
    OUTPUT.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(OUTPUT),
                      "ponsBirths": len(birth_rows),
                      "funded": result["fundedPonsBirths"],
                      "firstQuotePool": result["ponsFirstQuotePoolTokens"],
                      "signals": {mode: {rule: data["rawSignals"]
                                         for rule, data in rules.items()}
                                  for mode, rules in scenarios.items()}}, indent=2))


if __name__ == "__main__":
    main()
