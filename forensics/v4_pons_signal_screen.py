#!/usr/bin/env python3
"""Reproduce Pons-graduation timing versus independent v4 pool bursts.

Only public Initialize and Pons PoolRegistered logs are used. Initialize alone
does not prove a positive LP balance; the optional strict funding join is kept
separate from the birth-only signal. No wallet, quote, or transaction is used.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from statistics import median

from v4_all_fee_bursts import scan_rule


ROOT = Path(__file__).resolve().parents[1]
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
REGISTERED = ROOT / ".local/pons-hook-pool-registered-24h.json"
LAUNCHED = ROOT / ".local/pons-token-launched-24h.json"
INIT_TXS = ROOT / ".local/pons-independent-pool-init-tx-24h.json"
FUNDED = ROOT / ".local/v4-burst-strict-funded-signals-24h.json"
OUTPUT = ROOT / "forensics/results/v4-pons-signal-screen-24h.json"
CSV = ROOT / "forensics/results/v4-pons-signal-screen-24h.csv"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
ZERO_HOOK = "0x" + "0" * 40
RULES = ((3, 300), (4, 300), (5, 600))


def register_word(data: str, index: int) -> str:
    return "0x" + data[2 + index * 64 + 24:2 + (index + 1) * 64].lower()


def registration_map(logs: list[dict]) -> dict[str, dict]:
    registered = {}
    for log in logs:
        pool_id = log["topics"][1].lower()
        if pool_id in registered:
            raise ValueError(f"Duplicate Pons PoolRegistered: {pool_id}")
        registered[pool_id] = {
            "memecoin": register_word(log["data"], 0),
            "quoteToken": register_word(log["data"], 1),
            "creatorFeeRecipient": register_word(log["data"], 2),
            "registeredBlock": int(log["blockNumber"], 16),
            "registeredTx": log["transactionHash"].lower(),
        }
    return registered


def order(birth: dict) -> tuple[int, int, int]:
    return (birth["birthBlock"], birth["birthTransactionIndex"], birth["birthLogIndex"])


def first(items: list[dict]) -> dict | None:
    return min(items, key=order) if items else None


def causal_funding(signal: dict, funded_index: dict[tuple[str, str], dict]) -> dict:
    row = funded_index.get((signal["rule"], signal["token"]))
    if row is None:
        return {"status": "not_scanned_for_strict_funding"}
    return row["funded"]


def build(birth_source: dict, registration_logs: list[dict], funded_rows: list[dict],
          launched_logs: list[dict] | None = None,
          initializer_txs: dict[str, dict] | None = None) -> dict:
    sample_start = birth_source["birthFromBlock"]
    births = sorted(birth_source["births"], key=order)
    if len({birth["poolId"] for birth in births}) != len(births):
        raise ValueError("Duplicate Initialize pool ID")
    registered = registration_map(registration_logs)
    hook_births = [birth for birth in births if birth["hooks"] == PONS_HOOK
                   and birth["birthBlock"] >= sample_start]
    if {birth["poolId"] for birth in hook_births} != registered.keys():
        raise ValueError("Pons Initialize and PoolRegistered sets differ")
    for birth in hook_births:
        record = registered[birth["poolId"]]
        if (record["registeredBlock"] != birth["birthBlock"] or
                record["registeredTx"] != birth["birthTx"] or
                record["memecoin"] not in (birth["currency0"], birth["currency1"]) or
                record["quoteToken"] not in (birth["currency0"], birth["currency1"])):
            raise ValueError(f"Pons registration mismatch: {birth['poolId']}")
    pons_by_token = {registered[birth["poolId"]]["memecoin"]: birth for birth in hook_births}
    if len(pons_by_token) != len(hook_births):
        raise ValueError("More than one Pons pool for a token")
    launch_by_token = {}
    if launched_logs is not None:
        for log in launched_logs:
            token = "0x" + log["topics"][1][-40:].lower()
            if token in launch_by_token:
                raise ValueError(f"Duplicate Pons TokenLaunched: {token}")
            launch_by_token[token] = {"deployer": "0x" + log["topics"][3][-40:].lower(),
                                      "launchBlock": int(log["blockNumber"], 16),
                                      "launchTx": log["transactionHash"].lower()}
        if set(launch_by_token) != set(pons_by_token):
            raise ValueError("Pons TokenLaunched and PoolRegistered token sets differ")
    allowed = [birth for birth in births
               if birth["quoteAsset"] is not None and birth["token"] is not None
               and birth["feePips"] < 1_000_000
               and birth["hooks"] in (ZERO_HOOK, PONS_HOOK)]
    by_token = defaultdict(list)
    for birth in births:
        if birth["token"] is not None:
            by_token[birth["token"]].append(birth)
    funded_index = {(row["rule"], row["token"]): row for row in funded_rows}
    if len(funded_index) != len(funded_rows):
        raise ValueError("Duplicate strict funded signal")

    signals = []
    for n, window in RULES:
        raw, _ = scan_rule(allowed, n, window, sample_start)
        for signal in raw:
            token = signal["token"]
            hook_birth = pons_by_token.get(token)
            independent = first([birth for birth in by_token[token]
                                 if birth["hooks"] == ZERO_HOOK
                                 and 0 < birth["feePips"] < 1_000_000
                                 and birth["quoteAsset"] is not None])
            funding = causal_funding(signal, funded_index)
            signals.append({
                "rule": signal["rule"], "token": token,
                "rawEntryBlock": signal["entryBlock"],
                "rawEntryTimestamp": signal["entryTimestamp"],
                "rawEntryTime": signal["entryTime"],
                "rawSignalPoolIds": signal["burstPoolIds"],
                "funded": funding,
                "ponsPoolId": hook_birth["poolId"] if hook_birth else None,
                "ponsBirthBlock": hook_birth["birthBlock"] if hook_birth else None,
                "ponsBirthTimestamp": hook_birth["birthTimestamp"] if hook_birth else None,
                "ponsBirthTime": hook_birth["birthTime"] if hook_birth else None,
                "ponsQuoteEligible": bool(hook_birth and hook_birth["quoteAsset"] is not None),
                "ponsLaunchDeployer": launch_by_token[token]["deployer"]
                    if hook_birth and token in launch_by_token else None,
                "secondsPonsBeforeRawSignal":
                    signal["entryTimestamp"] - hook_birth["birthTimestamp"] if hook_birth else None,
                "secondsPonsBeforeFundedSignal":
                    funding["entryTimestamp"] - hook_birth["birthTimestamp"]
                    if hook_birth and funding["status"] == "funded_signal" else None,
                "independentPositiveFeeZeroHookPoolId": independent["poolId"] if independent else None,
                "independentPositiveFeeZeroHookBirthTimestamp":
                    independent["birthTimestamp"] if independent else None,
                "independentPositiveFeeZeroHookFeePips": independent["feePips"] if independent else None,
                "secondsFirstIndependentPoolAfterPons":
                    independent["birthTimestamp"] - hook_birth["birthTimestamp"]
                    if independent and hook_birth else None,
            })

    hook_tokens = set(pons_by_token)
    quote_hook_tokens = {registered[birth["poolId"]]["memecoin"] for birth in hook_births
                         if birth["quoteAsset"] is not None}
    funded_union = {row["token"] for row in funded_rows
                    if row["funded"]["status"] == "funded_signal"}
    per_hook = []
    for token, hook_birth in sorted(pons_by_token.items(), key=lambda pair: order(pair[1])):
        independent = first([birth for birth in by_token[token]
                             if birth["hooks"] == ZERO_HOOK
                             and 0 < birth["feePips"] < 1_000_000
                             and birth["quoteAsset"] is not None])
        per_hook.append({"token": token, "poolId": hook_birth["poolId"],
                         "birthBlock": hook_birth["birthBlock"],
                         "birthTimestamp": hook_birth["birthTimestamp"],
                         "birthTime": hook_birth["birthTime"],
                         "quoteEligible": hook_birth["quoteAsset"] is not None,
                         "quoteToken": registered[hook_birth["poolId"]]["quoteToken"],
                         "creatorFeeRecipient": registered[hook_birth["poolId"]]["creatorFeeRecipient"],
                         "launchDeployer": launch_by_token[token]["deployer"]
                             if token in launch_by_token else None,
                         "launchBlock": launch_by_token[token]["launchBlock"]
                             if token in launch_by_token else None,
                         "matchesStrictFunded4Or5": token in funded_union,
                         "firstIndependentPositiveFeeZeroHookPoolId":
                             independent["poolId"] if independent else None,
                         "firstIndependentInitializer":
                             initializer_txs[independent["birthTx"]]["from"]
                             if independent and initializer_txs is not None else None,
                         "secondsIndependentPoolAfterPons":
                             independent["birthTimestamp"] - hook_birth["birthTimestamp"]
                             if independent else None})
    def summary(tokens: set[str]) -> dict:
        sample = [row for row in per_hook if row["token"] in tokens]
        delay = [row["secondsIndependentPoolAfterPons"] for row in sample]
        return {"hookTokens": len(tokens),
                "matchesStrictFunded4Or5": len(tokens & funded_union),
                "doesNotMatchStrictFunded4Or5": len(tokens - funded_union),
                "firstIndependentPoolBeforeHook": sum(d is not None and d < 0 for d in delay),
                "firstIndependentPoolSameSecond": sum(d == 0 for d in delay),
                "firstIndependentPoolAfterHook": sum(d is not None and d > 0 for d in delay),
                "noIndependentPoolInDecodedWindow": sum(d is None for d in delay)}
    by_rule = {}
    for n, window in RULES:
        rule = f"{n}/{window}"
        rows = [row for row in signals if row["rule"] == rule]
        delays = [row["secondsPonsBeforeFundedSignal"] for row in rows
                  if row["secondsPonsBeforeFundedSignal"] is not None]
        by_rule[rule] = {"rawSignalTokens": len(rows),
                         "strictFundedTokens": sum(row["funded"]["status"] == "funded_signal"
                                                   for row in rows),
                         "strictFundingUnscanned": sum(row["funded"]["status"] ==
                                                      "not_scanned_for_strict_funding" for row in rows),
                         "fundedWithPriorPonsHook": sum(d >= 0 for d in delays),
                         "medianSecondsPonsBeforeFunded": median(delays) if delays else None}
    return {"schemaVersion": 1, "chainId": 4663,
            "fromBlock": sample_start, "toBlock": birth_source["toBlock"],
            "source": "PoolManager Initialize; Pons PoolRegistered; separate strict positive-net-LP join",
            "signalCohort": "static ETH/USDG quote pools with hooks either zero or the official Pons V2 meme hook",
            "hookScope": "Pons PoolRegistered identifies memecoin and fee recipient, including five custom-quote pools absent from ETH/USDG decoder",
            "sameBlockRule": "birth signals become visible after completed trigger block",
            "hookAddress": PONS_HOOK,
            "hookBirths": len(hook_births),
            "hookAllQuotes": summary(hook_tokens),
            "hookEthUsdgQuotes": summary(quote_hook_tokens),
            "hookQuoteOtherThanEthUsdg": len(hook_tokens - quote_hook_tokens),
            "strictFunded4Or5Tokens": len(funded_union),
            "strictFunded4Or5WithoutPons": sorted(funded_union - hook_tokens),
            "creatorFeeRecipientCount": len({row["creatorFeeRecipient"] for row in per_hook}),
            "launchDeployerCount": len({row["launchDeployer"] for row in per_hook
                                        if row["launchDeployer"]}),
            "firstIndependentInitializerCount": len({row["firstIndependentInitializer"]
                                                       for row in per_hook
                                                       if row["firstIndependentInitializer"]}),
            "firstIndependentInitializerMatchesLaunchDeployer":
                sum(row["firstIndependentInitializer"] == row["launchDeployer"]
                    for row in per_hook if row["firstIndependentInitializer"] and row["launchDeployer"]),
            "firstIndependentInitializerMatchesCreatorFeeRecipient":
                sum(row["firstIndependentInitializer"] == row["creatorFeeRecipient"]
                    for row in per_hook if row["firstIndependentInitializer"]),
            "rules": by_rule, "ponsPools": per_hook, "signals": signals}


def write_csv(path: Path, result: dict) -> None:
    rows = []
    for row in result["signals"]:
        funded = row["funded"]
        rows.append({"rule": row["rule"], "token": row["token"],
                     "rawEntryBlock": row["rawEntryBlock"],
                     "rawEntryTimeUtc": row["rawEntryTime"],
                     "strictFundingStatus": funded["status"],
                     "fundedEntryBlock": funded.get("entryBlock"),
                     "fundedEntryTimestamp": funded.get("entryTimestamp"),
                     "ponsPoolId": row["ponsPoolId"],
                     "ponsBirthBlock": row["ponsBirthBlock"],
                     "ponsBirthTimeUtc": row["ponsBirthTime"],
                     "ponsQuoteEligible": row["ponsQuoteEligible"],
                     "ponsLaunchDeployer": row["ponsLaunchDeployer"],
                     "secondsPonsBeforeRawSignal": row["secondsPonsBeforeRawSignal"],
                     "secondsPonsBeforeFundedSignal": row["secondsPonsBeforeFundedSignal"],
                     "firstIndependentPositiveFeeZeroHookPoolId": row["independentPositiveFeeZeroHookPoolId"],
                     "firstIndependentPositiveFeeZeroHookFeePips": row["independentPositiveFeeZeroHookFeePips"],
                     "secondsFirstIndependentPoolAfterPons": row["secondsFirstIndependentPoolAfterPons"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--births", type=Path, default=BIRTHS)
    parser.add_argument("--registered", type=Path, default=REGISTERED)
    parser.add_argument("--funded", type=Path, default=FUNDED)
    parser.add_argument("--launched", type=Path, default=LAUNCHED)
    parser.add_argument("--initializer-txs", type=Path, default=INIT_TXS)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--csv", type=Path, default=CSV)
    args = parser.parse_args()
    result = build(json.loads(args.births.read_text()),
                   json.loads(args.registered.read_text())["logs"],
                   json.loads(args.funded.read_text())["signals"],
                   json.loads(args.launched.read_text())["logs"],
                   json.loads(args.initializer_txs.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    write_csv(args.csv, result)
    print(json.dumps({"output": str(args.output), "csv": str(args.csv),
                      "hookAllQuotes": result["hookAllQuotes"],
                      "hookEthUsdgQuotes": result["hookEthUsdgQuotes"],
                      "rules": result["rules"]}))


if __name__ == "__main__":
    main()
