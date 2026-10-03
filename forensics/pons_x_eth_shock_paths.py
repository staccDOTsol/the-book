#!/usr/bin/env python3
"""Export observed post-signal Pons X/ETH swap shocks for counterfactual LP replay.

One row per *completed swap block* is intentional: a new arbitrage transaction
cannot react to intermediate events in a historical block. The exported quote
volumes are observations, not executable depth in an invented X/NB pool.

Inputs are the archived 24 h signal and v4 event caches. The public RH RPC is
used only once to cache the Pons hook's immutable-at-registration fee terms;
subsequent runs work offline with that cache. No transaction is sent.
"""

from __future__ import annotations

from collections import defaultdict
import csv
import json
from pathlib import Path

from rig_monitor import RPC_URL, Rpc
from v4_quote_range_screen import sqrt_price_at_tick


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "forensics/results/nothingburger-historical-pair-candidates-24h.csv"
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
REPLAY = ROOT / ".local/v4-burst-fee-capital-24h.json"
EVENTS = ROOT / ".local/burst-v4-pool-events-24h.json"
FEE_CACHE = ROOT / ".local/pons-x-eth-hook-fees-24h.json"
OUTPUT = ROOT / "forensics/results/nothingburger-historical-x-eth-shocks-24h.json"
ETH = "0x0000000000000000000000000000000000000000"
PONS_HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
# keccak256("launches(bytes32)")[:4]; public mapping getter on PonsV2MemeHook.
LAUNCHES_SELECTOR = "0xad091230"
Q96 = 2**96
WAD = 10**18


def abi_words(data: str) -> list[int]:
    if not data.startswith("0x") or (len(data) - 2) % 64:
        raise ValueError("Invalid ABI word data")
    return [int(data[i:i + 64], 16) for i in range(2, len(data), 64)]


def signed(value: int) -> int:
    return value - 2**256 if value >= 2**255 else value


def price_eth_per_x(sqrt_price_x96: int, quote_side: int) -> float:
    # Both native ETH and Pons launcher tokens have 18 decimals.
    ratio = sqrt_price_x96 / Q96
    return ratio * ratio if quote_side == 1 else 1 / (ratio * ratio)


def local_buy_quote(sqrt_price_x96: int, liquidity: int, tick: int,
                    spacing: int, total_hook_fee_bps: int,
                    eth_input_raw: int) -> dict | None:
    """Conservative within-current-tick X output for an exact ETH input.

Pons X/ETH pools have ETH as currency0. The hook charges its fee on X output
    for an exact ETH input. This avoids pretending to know liquidity beyond the
    nearest initializable tick, while preserving the actual signal liquidity.
    """
    if liquidity <= 0 or eth_input_raw <= 0:
        return None
    lower_tick = tick // spacing * spacing
    lower_price = sqrt_price_at_tick(lower_tick)
    if not lower_price <= sqrt_price_x96:
        raise ValueError("Signal sqrt price below current tick band")
    max_eth_raw = (liquidity * Q96 * (sqrt_price_x96 - lower_price)
                   // (sqrt_price_x96 * lower_price))
    if eth_input_raw > max_eth_raw:
        return None
    post_price = (liquidity * Q96 * sqrt_price_x96
                  // (liquidity * Q96 + eth_input_raw * sqrt_price_x96))
    gross_x_raw = liquidity * (sqrt_price_x96 - post_price) // Q96
    net_x_raw = gross_x_raw * (10_000 - total_hook_fee_bps) // 10_000
    return {
        "ethInput": eth_input_raw / WAD,
        "grossXOut": gross_x_raw / WAD,
        "netXOutAfterPonsHookFee": net_x_raw / WAD,
        "effectiveEthPerNetX": eth_input_raw / net_x_raw if net_x_raw else None,
        "maxEthInputWithinCurrentTick": max_eth_raw / WAD,
        "boundaryTick": lower_tick,
        "calculation": "single active-liquidity tick, no core fee, hook fee on X output; gas excluded",
    }


def hook_fees(candidates: list[dict]) -> dict[str, dict]:
    if FEE_CACHE.exists():
        cached = json.loads(FEE_CACHE.read_text())
        if set(cached) == {row["ponsPoolId"] for row in candidates}:
            return cached
    rpc = Rpc(RPC_URL)
    requests = [{"jsonrpc": "2.0", "id": index,
                 "method": "eth_call",
                 "params": [{"to": PONS_HOOK,
                             "data": LAUNCHES_SELECTOR + row["ponsPoolId"][2:]}, "latest"]}
                for index, row in enumerate(candidates, 1)]
    response = rpc._request(requests)
    if not isinstance(response, list):
        raise RuntimeError("Pons hook RPC batch response is not a list")
    by_id = {item["id"]: item for item in response}
    fees = {}
    for index, row in enumerate(candidates, 1):
        item = by_id[index]
        if "error" in item:
            raise RuntimeError(f"Hook fee call failed for {row['ponsPoolId']}: {item['error']}")
        words = abi_words(item["result"])
        if len(words) != 13 or words[0] != 1:
            raise ValueError(f"Unexpected Pons LaunchInfo for {row['ponsPoolId']}")
        if "0x" + format(words[2], "040x") != row["token"]:
            raise ValueError("Pons LaunchInfo token mismatch")
        if words[3] != 0:
            raise ValueError("Expected native ETH Pons quote")
        fees[row["ponsPoolId"]] = {
            "externalPonsCreatorTaxBps": words[7],
            "externalPonsBaseFeeBps": words[10],
            "externalPonsTotalFeeBps": words[7] + words[10],
            "hookLaunchInfoReadAt": "latest",
            "hookFeeTermsFrozenAtRegistration": True,
        }
    FEE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FEE_CACHE.write_text(json.dumps(fees, indent=2) + "\n")
    return fees


def main() -> None:
    with CANDIDATES.open(newline="") as handle:
        candidates = list(csv.DictReader(handle))
    if len(candidates) != 19 or len({row["token"] for row in candidates}) != 19:
        raise ValueError("Unexpected candidate cohort")
    births = {row["poolId"]: row for row in json.loads(BIRTHS.read_text())["births"]}
    replay_rows = json.loads(REPLAY.read_text())["hookedCoreOnlyRows"]
    replay_by_key = {(row["token"], row["signalBlock"], row["poolId"]): row
                     for row in replay_rows
                     if row["signalSource"] == "strict_n_funded_births"
                     and row["quoteAsset"] == ETH}
    pool_ids = {row["ponsPoolId"] for row in candidates}
    swaps_by_pool = defaultdict(list)
    for row in json.loads(EVENTS.read_text())["swapLogs"]:
        if row["poolId"] in pool_ids:
            swaps_by_pool[row["poolId"]].append(row)
    fee_terms = hook_fees(candidates)
    paths = []
    for candidate in candidates:
        token = candidate["token"]
        pool_id = candidate["ponsPoolId"]
        signal_block = int(candidate["entryBlock"])
        signal_time = int(candidate["entryTimestamp"])
        pool = births[pool_id]
        signal = replay_by_key[(token, signal_block, pool_id)]
        cap_block = signal["timeCapBlock"]
        quote_side = pool["quoteSide"]
        if pool["quoteAsset"] != ETH or pool["token"] != token:
            raise ValueError("Candidate is not Pons X/ETH")
        if signal["signalTimestamp"] != signal_time:
            raise ValueError("Signal timestamp mismatch")
        if quote_side != 0:
            raise ValueError("Expected native ETH as v4 currency0")
        prior = max((event for event in swaps_by_pool[pool_id]
                     if event["block"] <= signal_block),
                    key=lambda event: (event["block"], event["transactionIndex"],
                                       event["logIndex"]), default=None)
        if prior is None or abi_words(prior["data"])[2] != int(signal["signalSqrtPriceX96"]):
            raise ValueError("Signal price does not equal last completed-block Pons swap")
        post = sorted((event for event in swaps_by_pool[pool_id]
                       if signal_block < event["block"] <= cap_block),
                      key=lambda row: (row["block"], row["transactionIndex"], row["logIndex"]))
        by_block = defaultdict(list)
        for event in post:
            by_block[event["block"]].append(event)
        updates = []
        total_eth_notional = 0.0
        for block, events in sorted(by_block.items()):
            amount_eth_in = amount_eth_out = amount_x_in = amount_x_out = 0
            for event in events:
                words = abi_words(event["data"])
                if len(words) < 6:
                    raise ValueError("Truncated v4 Swap")
                amount_eth = signed(words[quote_side])
                amount_x = signed(words[1 - quote_side])
                amount_eth_in += max(0, -amount_eth)
                amount_eth_out += max(0, amount_eth)
                amount_x_in += max(0, -amount_x)
                amount_x_out += max(0, amount_x)
            last = events[-1]
            words = abi_words(last["data"])
            eth_in = amount_eth_in / WAD
            eth_out = amount_eth_out / WAD
            eth_notional = max(eth_in, eth_out)
            total_eth_notional += eth_notional
            updates.append({
                "block": block,
                "lastTransactionIndex": last["transactionIndex"],
                "lastLogIndex": last["logIndex"],
                "postPriceEthPerX": price_eth_per_x(words[2], quote_side),
                "postSqrtPriceX96": str(words[2]),
                "ethInputVolume": eth_in,
                "ethOutputVolume": eth_out,
                "ethNotional": eth_notional,
                "xInputRaw": str(amount_x_in),
                "xOutputRaw": str(amount_x_out),
                "swapCount": len(events),
                "postLiquidityRaw": str(words[3]),
                "postTick": signed(words[4]),
                "coreFeePipsLastSwap": words[5],
            })
        paths.append({
            "token": token,
            "rule": candidate["rule"],
            "entryBlock": signal_block,
            "entryTimestamp": signal_time,
            "entryTimeUtc": candidate["entryTimeUtc"],
            "timeCapBlock": cap_block,
            "timeCapTimestampUpperBound": signal_time + 1800,
            "ponsPoolId": pool_id,
            "ponsBirthBlock": int(candidate["ponsBirthBlock"]),
            "fundedPoolsAtSignal": int(candidate["fundedPoolsAtSignal"]),
            "initialPriceEthPerX": price_eth_per_x(int(signal["signalSqrtPriceX96"]), quote_side),
            "initialSqrtPriceX96": signal["signalSqrtPriceX96"],
            "initialActiveLiquidityRaw": signal["activeLpAtSignal"],
            "initialTick": signal["signalTick"],
            "tickSpacing": pool["tickSpacing"],
            "signalSmallBuyQuote001Eth": local_buy_quote(
                int(signal["signalSqrtPriceX96"]), int(signal["activeLpAtSignal"]),
                signal["signalTick"], pool["tickSpacing"],
                fee_terms[pool_id]["externalPonsTotalFeeBps"], 10**15),
            **fee_terms[pool_id],
            "postSignalSwapCount": len(post),
            "postSignalSwapBlocks": len(updates),
            "observedEthNotional30m": total_eth_notional,
            "swaps": updates,
        })
        if any(not signal_block < event["block"] <= cap_block for event in updates):
            raise ValueError("Swap update outside 30-minute cap")
        if any(left["block"] >= right["block"] for left, right in zip(updates, updates[1:])):
            raise ValueError("Swap updates not strictly ordered by completed block")
    output = {
        "schemaVersion": 1,
        "scope": "19 strict funded 4/300 or 5/600 first Pons X/ETH pool-burst signals in the archived 24 h window; completed-block X/ETH price shocks for the following 30 minutes",
        "source": {
            "candidateCsv": str(CANDIDATES.relative_to(ROOT)),
            "birthCache": str(BIRTHS.relative_to(ROOT)),
            "eventCache": str(EVENTS.relative_to(ROOT)),
            "signalReplay": str(REPLAY.relative_to(ROOT)),
            "hookAddress": PONS_HOOK,
        },
        "units": {
            "initialPriceEthPerX": "ETH per X, both 18-decimal tokens; end of signal block",
            "postPriceEthPerX": "ETH per X after last observed Pons X/ETH Swap in this block",
            "ethInputVolume": "ETH paid by historical swappers to Pons pool in this block; not available depth",
            "ethOutputVolume": "ETH received by historical swappers from Pons pool in this block; not available depth",
            "ethNotional": "max(ethInputVolume,ethOutputVolume) for this block, ETH",
            "xInputRaw": "raw 18-decimal X paid by swappers",
            "xOutputRaw": "raw 18-decimal X received by swappers",
            "postLiquidityRaw": "v4 active liquidity after last swap, not a quote or across-tick depth",
            "signalSmallBuyQuote001Eth": "within-current-tick calculated 0.001 ETH exact-input X quote, including frozen Pons hook fee; excludes gas and route restrictions",
        },
        "feeEvidence": "Pons hook launches(bytes32) getter at latest; fee terms are snapshotted at PoolRegistered. Historical-state eth_call was unavailable on the public RPC.",
        "caveats": [
            "The proposed NOTHINGBURGER token and X/NOTHINGBURGER pool did not exist in this sample.",
            "Historical X/ETH trades do not imply those traders would have routed through X/NOTHINGBURGER.",
            "Pons hook fee is levied in afterSwap and coreFeePips in the v4 Swap event is zero; use externalPonsTotalFeeBps rather than coreFeePipsLastSwap.",
            "Quote volume is observed flow, not executable depth. A counterfactual arb needs separate route and impact modeling.",
            "The 0.001 ETH signal buy quote is calculated from historical v4 active liquidity within the current tick; public RH RPC did not offer historical-state eth_call. It is not a live or across-tick quoter response.",
        ],
        "paths": paths,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(OUTPUT), "paths": len(paths),
                      "swapBlocks": sum(p["postSignalSwapBlocks"] for p in paths),
                      "swaps": sum(p["postSignalSwapCount"] for p in paths),
                      "observedEthNotional30m": sum(p["observedEthNotional30m"] for p in paths)}, indent=2))


if __name__ == "__main__":
    main()
