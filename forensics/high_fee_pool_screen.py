#!/usr/bin/env python3
"""Read-only high-fee v4 pool birth, seed LP, and swap activity screen.

This records public on-chain events. No keys, signing, swap, LP mint, or market
orders are used. A positive LP event is not a quote or proof of accessible
fee income for a hypothetical new position.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext, ROUND_DOWN
import json
import os
from pathlib import Path
import sys
import time

import rig_monitor as rig


ROOT = Path(__file__).resolve().parents[1]
SOURCE_MATRIX = ROOT / ".local" / "threshold-matrix-24h.json"
RAW_INIT = ROOT / ".local" / "high-fee-all-init-raw.json"
RAW_EVENTS = ROOT / ".local" / "high-fee-pool-events-24h.json"
OUTPUT = ROOT / ".local" / "high-fee-pool-screen-24h.json"
BLOCK_TIMES = ROOT / ".local" / "high-fee-block-times-24h.json"
DECODED_BIRTHS = ROOT / ".local" / "high-fee-births-decoded-24h.json"
SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
ETH = "0x0000000000000000000000000000000000000000"
QUOTE_DECIMALS = {USDG: 6, ETH: 18}
REMAINING = (Decimal("0.95"), Decimal("0.90"), Decimal("0.85"), Decimal("0.80"),
             Decimal("0.75"), Decimal("0.70"), Decimal("0.60"), Decimal("0.50"))
WINDOWS = (300, 600, 1800, 3600)
Q96 = Decimal(2**96)
MIN_TICK = -887272
MAX_TICK = 887272


def signed_word(hex_word: str) -> int:
    value = int(hex_word, 16)
    return value - 2**256 if value >= 2**255 else value


def words(data: str, minimum: int) -> list[str]:
    content = data[2:] if data.startswith("0x") else data
    if len(content) < minimum * 64:
        raise ValueError("Short ABI data")
    return [content[offset:offset + 64] for offset in range(0, len(content), 64)]


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def decode_birth(log: dict, timestamp: int) -> dict:
    data = words(log["data"], 5)
    topics = log["topics"]
    if len(topics) != 4:
        raise ValueError("Unexpected Initialize topic count")
    currency0 = ("0x" + topics[2][-40:]).lower()
    currency1 = ("0x" + topics[3][-40:]).lower()
    quote = currency0 if currency0 in QUOTE_DECIMALS else currency1 if currency1 in QUOTE_DECIMALS else None
    token = currency1 if quote == currency0 else currency0 if quote == currency1 else None
    return {
        "poolId": topics[1].lower(), "currency0": currency0, "currency1": currency1,
        "quoteAsset": quote, "quoteSide": 0 if quote == currency0 else 1 if quote == currency1 else None,
        "token": token, "feePips": int(data[0], 16),
        "tickSpacing": signed_word(data[1]), "hooks": ("0x" + data[2][-40:]).lower(),
        "initialSqrtPriceX96": str(int(data[3], 16)), "initialTick": signed_word(data[4]),
        "birthBlock": int(log["blockNumber"], 16), "birthTimestamp": timestamp,
        "birthTime": iso(timestamp), "birthTx": log["transactionHash"].lower(),
        "birthTransactionIndex": int(log["transactionIndex"], 16),
        "birthLogIndex": int(log["logIndex"], 16),
    }


def minimal_event(log: dict) -> dict:
    return {
        "poolId": log["topics"][1].lower(), "block": int(log["blockNumber"], 16),
        "transactionIndex": int(log["transactionIndex"], 16),
        "logIndex": int(log["logIndex"], 16), "tx": log["transactionHash"].lower(),
        "data": log["data"],
        "sender": ("0x" + log["topics"][2][-40:]).lower() if len(log["topics"]) > 2 else None,
    }


def event_order(event: dict) -> tuple[int, int, int]:
    return (event["block"], event["transactionIndex"], event["logIndex"])


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def load_block_times_checkpoint(rpc: rig.Rpc, blocks: set[int], path: Path) -> None:
    cached = json.loads(path.read_text()) if path.exists() else {}
    rpc.block_times.update({int(key): int(value) for key, value in cached.items()})
    missing = sorted(blocks - rpc.block_times.keys())
    for offset in range(0, len(missing), 50):
        chunk = set(missing[offset:offset + 50])
        for attempt in range(8):
            try:
                rpc.load_block_times(chunk)
                break
            except RuntimeError as error:
                if "429" not in str(error) or attempt == 7:
                    raise
                pause = min(15 * (attempt + 1), 60)
                print(f"timestamp RPC throttled; retrying {len(chunk)} blocks in {pause}s", file=sys.stderr, flush=True)
                time.sleep(pause)
        cached.update({str(block): rpc.block_times[block] for block in chunk})
        atomic_write(path, cached)
        if offset and offset % 500 == 0:
            print(f"timestamp blocks verified {offset}/{len(missing)}", file=sys.stderr, flush=True)
        time.sleep(0.25)


def fetch_births(rpc: rig.Rpc, source: dict, cache_path: Path, block_time_cache: Path,
                 decoded_cache: Path, min_fee: int, max_fee: int) -> list[dict]:
    if decoded_cache.exists():
        decoded = json.loads(decoded_cache.read_text())
        if (decoded.get("fromBlock") == source["fromBlock"] and decoded.get("toBlock") == source["toBlock"]
                and all(min_fee <= birth["feePips"] < max_fee and int(birth["hooks"], 16) == 0
                        for birth in decoded["births"])):
            return decoded["births"]
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("fromBlock") == source["fromBlock"] and cached.get("toBlock") == source["toBlock"]:
            raw = cached["logs"]
        else:
            raise ValueError("Initialize cache block range differs from source")
    else:
        logs = rpc.logs(rig.INITIALIZE, source["fromBlock"], source["toBlock"])
        raw = [log for log in logs if min_fee <= int(words(log["data"], 1)[0], 16) < max_fee
               and int(words(log["data"], 3)[2], 16) == 0]
        atomic_write(cache_path, {"fromBlock": source["fromBlock"], "toBlock": source["toBlock"], "logs": raw})
    if any(not min_fee <= int(words(log["data"], 1)[0], 16) < max_fee
           or int(words(log["data"], 3)[2], 16) != 0 for log in raw):
        raise ValueError("Initialize cache does not match fee/no-hook cohort")
    load_block_times_checkpoint(rpc, {int(log["blockNumber"], 16) for log in raw}, block_time_cache)
    births = [decode_birth(log, rpc.block_times[int(log["blockNumber"], 16)]) for log in raw]
    ids = [birth["poolId"] for birth in births]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate high-fee pool Initialize")
    births.sort(key=lambda item: (item["birthBlock"], item["birthTransactionIndex"], item["birthLogIndex"]))
    atomic_write(decoded_cache, {"fromBlock": source["fromBlock"], "toBlock": source["toBlock"], "births": births})
    return births


def fetch_events(rpc: rig.Rpc, births: list[dict], start: int, follow_end: int,
                 source_end_time: int, cache_path: Path, block_time_cache: Path) -> dict:
    cohort_ids = [birth["poolId"] for birth in births]
    if cache_path.exists():
        state = json.loads(cache_path.read_text())
        if state.get("fromBlock") != start or state.get("poolIds") != cohort_ids:
            raise ValueError("Event cache does not match this birth cohort")
    else:
        state = {"fromBlock": start, "poolIds": cohort_ids, "completedUntilBlock": start - 1,
                 "lpLogs": [], "swapLogs": []}
    first = max(start, int(state["completedUntilBlock"]) + 1)
    while first <= follow_end:
        last = min(first + 19_999, follow_end)
        first_time = rpc.block_time(first)
        active = [birth["poolId"] for birth in births
                  if birth["birthBlock"] <= last and birth["birthTimestamp"] + 7200 >= first_time]
        print(f"events {first}..{last} active={len(active)}", file=sys.stderr, flush=True)
        for offset in range(0, len(active), 50):
            group = active[offset:offset + 50]
            state["lpLogs"].extend(minimal_event(log)
                                   for log in rpc.logs(rig.MODIFY_LIQUIDITY, first, last, group))
            state["swapLogs"].extend(minimal_event(log)
                                     for log in rpc.logs(SWAP_TOPIC, first, last, group))
        state["completedUntilBlock"] = last
        state["followEndBlock"] = last
        state["sourceEndTime"] = source_end_time
        atomic_write(cache_path, state)
        first = last + 1
    all_blocks = {event["block"] for event in state["lpLogs"] + state["swapLogs"]}
    load_block_times_checkpoint(rpc, all_blocks, block_time_cache)
    state["blockTimes"] = {str(block): rpc.block_times[block] for block in all_blocks}
    atomic_write(cache_path, state)
    return state


def quote_deposit_from_liquidity(birth: dict, seed: dict, sqrt_price_x96: int) -> dict:
    """Theoretical principal from v4 L and price; not a receipt or fill."""
    liquidity = Decimal(seed["delta"])
    if liquidity <= 0:
        return {"status": "nonpositive_liquidity"}
    with localcontext() as context:
        context.prec = 90
        lower = Decimal("1.0001") ** (Decimal(seed["tickLower"]) / 2)
        upper = Decimal("1.0001") ** (Decimal(seed["tickUpper"]) / 2)
        current = Decimal(sqrt_price_x96) / Q96
        if not lower < upper or current <= 0:
            return {"status": "invalid_range_or_price"}
        p = min(max(current, lower), upper)
        amount0 = liquidity * (upper - p) / (upper * p)
        amount1 = liquidity * (p - lower)
        quote_side = birth["quoteSide"]
        if quote_side is None:
            return {"status": "non_quote_pair", "estimatedAmount0Raw": str(int(amount0)),
                    "estimatedAmount1Raw": str(int(amount1))}
        amount = amount0 if quote_side == 0 else amount1
        raw = int(amount.to_integral_value(rounding=ROUND_DOWN))
        decimals = QUOTE_DECIMALS[birth["quoteAsset"]]
        return {"status": "theoretical_from_L_and_price", "estimatedQuoteRaw": str(raw),
                "estimatedQuoteAmount": str(Decimal(raw) / (10**decimals)),
                "quoteAsset": birth["quoteAsset"], "quoteDecimals": decimals,
                "sqrtPriceX96Used": str(sqrt_price_x96),
                "warning": "Theoretical v4 principal; receipt transfers, rounding, native settlement, and shared-tx attribution are not verified."}


def quote_only_ranges(tick: int, spacing: int, quote_side: int | None) -> dict:
    """Legal concentrated ranges immediately outside price on the quote side."""
    if spacing <= 0 or quote_side not in (0, 1):
        return {"status": "unavailable"}
    min_usable = -((-MIN_TICK) // spacing) * spacing
    max_usable = (MAX_TICK // spacing) * spacing
    ranges = {}
    for width in (1, 2, 4):
        if quote_side == 0:
            lower = (tick // spacing + 1) * spacing
            upper = lower + width * spacing
        else:
            upper = (tick // spacing) * spacing
            lower = upper - width * spacing
        ranges[str(width)] = ({"tickLower": lower, "tickUpper": upper}
                              if min_usable <= lower < upper <= max_usable else None)
    return {"status": "legal_grid_ranges", "quoteSide": quote_side,
            "widthInTickIntervals": ranges,
            "note": "Ranges are illustrative and outside the completed entry-block tick; none was minted."}


def block_crossings(changes: list[tuple[int, int]], start_block: int,
                    initial_current: int = 0) -> dict:
    by_block: dict[int, int] = defaultdict(int)
    for block, delta in changes:
        if block >= start_block:
            by_block[block] += delta
    current = initial_current
    peak = max(0, current)
    peak_block = start_block
    first_negative_block = None
    crossing = {}
    for block in sorted(by_block):
        delta = by_block[block]
        current += delta
        if delta < 0 and first_negative_block is None:
            first_negative_block = block
        if current > peak:
            peak, peak_block = current, block
        if peak > 0:
            for fraction in REMAINING:
                key = str(fraction)
                if key not in crossing and Decimal(current) < Decimal(peak) * fraction:
                    crossing[key] = {"block": block, "currentOutstanding": str(current),
                                     "peakOutstanding": str(peak)}
    return {"currentOutstanding": str(current), "peakOutstanding": str(peak),
            "peakBlock": peak_block, "firstNetNegativeBlock": first_negative_block,
            "crossings": crossing}


def analyze(births: list[dict], events: dict, rpc: rig.Rpc, follow_end: int,
            birth_from_time: int, birth_to_time: int, min_fee: int, max_fee: int) -> dict:
    block_times = {int(key): value for key, value in events["blockTimes"].items()}
    by_pool_lp: dict[str, list[dict]] = defaultdict(list)
    by_pool_swap: dict[str, list[dict]] = defaultdict(list)
    for event in events["lpLogs"]:
        by_pool_lp[event["poolId"]].append(event)
    for event in events["swapLogs"]:
        by_pool_swap[event["poolId"]].append(event)
    results = []
    for birth in births:
        pool_id = birth["poolId"]
        birth_end = birth["birthTimestamp"] + 7200
        lp_logs = sorted((event for event in by_pool_lp[pool_id]
                          if birth["birthBlock"] <= event["block"] and
                          block_times[event["block"]] <= birth_end), key=event_order)
        swap_logs = sorted((event for event in by_pool_swap[pool_id]
                            if birth["birthBlock"] <= event["block"] and
                            block_times[event["block"]] <= birth_end), key=event_order)
        lp = []
        for event in lp_logs:
            parsed = words(event["data"], 4)
            lp.append({**{key: event[key] for key in ("block", "transactionIndex", "logIndex", "tx", "sender")},
                       "timestamp": block_times[event["block"]],
                       "tickLower": signed_word(parsed[0]), "tickUpper": signed_word(parsed[1]),
                       "delta": signed_word(parsed[2]), "salt": parsed[3]})
        swaps = []
        for event in swap_logs:
            parsed = words(event["data"], 6)
            swaps.append({**{key: event[key] for key in ("block", "transactionIndex", "logIndex", "tx")},
                          "timestamp": block_times[event["block"]],
                          "amount0": signed_word(parsed[0]), "amount1": signed_word(parsed[1]),
                          "sqrtPriceX96": int(parsed[2], 16), "tick": signed_word(parsed[4])})
        seed = next((event for event in lp if event["delta"] > 0), None)
        first_funded_block = None
        changes_by_block: dict[int, int] = defaultdict(int)
        for event in lp:
            changes_by_block[event["block"]] += event["delta"]
        running = 0
        first_funded_net = 0
        for block in sorted(changes_by_block):
            running += changes_by_block[block]
            if running > 0:
                first_funded_block, first_funded_net = block, running
                break
        own = block_crossings([(event["block"], event["delta"]) for event in lp], birth["birthBlock"])
        for crossing in own["crossings"].values():
            crossing["timestamp"] = block_times[crossing["block"]]
            crossing["time"] = iso(crossing["timestamp"])
        if seed:
            seed_order = (seed["block"], seed["transactionIndex"], seed["logIndex"])
            prior_swaps = [event for event in swaps
                           if (event["block"], event["transactionIndex"], event["logIndex"]) < seed_order]
            sqrt_at_seed = prior_swaps[-1]["sqrtPriceX96"] if prior_swaps else int(birth["initialSqrtPriceX96"])
            tick_at_seed = prior_swaps[-1]["tick"] if prior_swaps else birth["initialTick"]
            seed_record = {
                "block": seed["block"], "timestamp": seed["timestamp"], "time": iso(seed["timestamp"]),
                "tx": seed["tx"], "birthTxSeed": seed["tx"] == birth["birthTx"],
                "sender": seed["sender"], "tickLower": seed["tickLower"],
                "tickUpper": seed["tickUpper"], "liquidityDelta": str(seed["delta"]),
                "tickAtSeed": tick_at_seed,
                "sqrtPriceX96AtSeed": str(sqrt_at_seed),
                "quoteDepositEstimate": quote_deposit_from_liquidity(birth, seed, sqrt_at_seed),
            }
        else:
            seed_record = None
        entry_block = first_funded_block
        if entry_block is not None:
            entry_swaps = [event for event in swaps if event["block"] <= entry_block]
            current_sqrt = entry_swaps[-1]["sqrtPriceX96"] if entry_swaps else int(birth["initialSqrtPriceX96"])
            current_tick = entry_swaps[-1]["tick"] if entry_swaps else birth["initialTick"]
            entry_timestamp = block_times.get(entry_block, birth["birthTimestamp"])
            quote_ranges = quote_only_ranges(current_tick, birth["tickSpacing"], birth["quoteSide"])
        else:
            current_sqrt = current_tick = entry_timestamp = None
            quote_ranges = {"status": "no_positive_completed_block_liquidity"}
        swap_windows = {}
        for seconds in WINDOWS:
            eligible = [event for event in swaps if entry_block is not None and event["block"] > entry_block
                        and 0 <= event["timestamp"] - entry_timestamp <= seconds]
            # Pool.swap returns caller balance deltas. Negative is input owed
            # by the caller to PoolManager; the positive leg is output.
            amount0_in = sum(max(0, -event["amount0"]) for event in eligible)
            amount1_in = sum(max(0, -event["amount1"]) for event in eligible)
            token_side = 1 - birth["quoteSide"] if birth["quoteSide"] is not None else None
            swap_windows[str(seconds)] = {
                "count": len(eligible), "callerOwesCurrency0Raw": str(amount0_in),
                "callerOwesCurrency1Raw": str(amount1_in),
                "tokenInputRaw": str(amount0_in if token_side == 0 else amount1_in) if token_side is not None else None,
                "quoteInputRaw": str(amount0_in if birth["quoteSide"] == 0 else amount1_in) if birth["quoteSide"] is not None else None,
                "firstSwapBlock": eligible[0]["block"] if eligible else None,
            }
        # The event fetch follows birth for 120 minutes. Seed-window coverage
        # may still be short if seed itself first appears close to that edge.
        follow_time = rpc.block_time(follow_end)
        coverage = max(0, min(follow_time, birth_end) - entry_timestamp) if entry_timestamp is not None else 0
        result = {**birth,
                  "positiveLpEventsWithin2h": sum(event["delta"] > 0 for event in lp),
                  "negativeLpEventsWithin2h": sum(event["delta"] < 0 for event in lp),
                  "seed": seed_record,
                  "firstFundedCompletedBlock": first_funded_block,
                  "entryBlock": entry_block,
                  "entryTimestamp": entry_timestamp,
                  "entryTime": iso(entry_timestamp) if entry_timestamp is not None else None,
                  "entryTick": current_tick,
                  "entrySqrtPriceX96": str(current_sqrt) if current_sqrt is not None else None,
                  "quoteOnlyOutsideTickRanges": quote_ranges,
                  "firstFundedCompletedBlockNetLiquidity": str(first_funded_net),
                  "grossLiquidityAddedWithin2h": str(sum(event["delta"] for event in lp if event["delta"] > 0)),
                  "grossLiquidityRemovedWithin2h": str(-sum(event["delta"] for event in lp if event["delta"] < 0)),
                  "firstWithdrawal": next(({"block": event["block"], "timestamp": event["timestamp"],
                                             "time": iso(event["timestamp"]), "delta": str(event["delta"])}
                                            for event in lp if event["delta"] < 0), None),
                  "ownLiquidity": own, "swapsAfterSeed": swap_windows,
                  "swapWindowCoverageSeconds": coverage,
                  "observedSwapEventsWithin2h": len(swaps),
                  "swapEvents": [
                      {"block": event["block"], "timestamp": event["timestamp"],
                       "time": iso(event["timestamp"]), "tx": event["tx"],
                       "amount0CallerDeltaRaw": str(event["amount0"]),
                       "amount1CallerDeltaRaw": str(event["amount1"]),
                       "postTick": event["tick"], "postSqrtPriceX96": str(event["sqrtPriceX96"]),
                       "reportedActiveLiquidityAfter": str(int(words(raw["data"], 6)[3], 16)),
                       "feePips": int(words(raw["data"], 6)[5], 16),
                      }
                      for event, raw in zip(swaps, swap_logs)
                  ],
                  "seedRangeIsFullUsableRange": bool(seed and birth["tickSpacing"] > 0 and
                       seed["tickLower"] <= (MIN_TICK // birth["tickSpacing"] + 1) * birth["tickSpacing"] and
                       seed["tickUpper"] >= (MAX_TICK // birth["tickSpacing"]) * birth["tickSpacing"]),
                  }
        results.append(result)
    add_dynamic_token_exits(results, events, rpc.block_time(follow_end),
                            birth_from_time, birth_to_time)
    return {"schemaVersion": 1, "chainId": 4663, "poolManager": rig.POOL_MANAGER,
            "birthCohort": f"no-hook static fee >={min_fee} and <{max_fee} pips",
            "eventObservation": "from birth through 120 minutes or latest fetched block; swap windows counted only in later blocks after first positive LP event",
            "followEndBlock": follow_end, "followEndTime": iso(rpc.block_time(follow_end)),
            "poolCount": len(results), "pools": results}


def add_dynamic_token_exits(pools: list[dict], events: dict, follow_end_time: int,
                            birth_from_time: int, birth_to_time: int) -> None:
    """Add later high-fee pools only when their LP delta enters a completed block.

    The raw scan observes each pool for two hours from birth. A token with an
    older high-fee pool may therefore lack a complete aggregate baseline; in
    that case we emit an explicit unavailable status rather than a crossing.
    """
    by_token: dict[str, list[dict]] = defaultdict(list)
    by_pool = {pool["poolId"]: pool for pool in pools}
    for pool in pools:
        if pool["token"] is not None:
            by_token[pool["token"]].append(pool)
        else:
            pool["dynamicTokenHighFeeLiquidity"] = {"status": "non_quote_pair"}
    block_times = {int(key): int(value) for key, value in events["blockTimes"].items()}
    token_changes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for event in events["lpLogs"]:
        pool = by_pool[event["poolId"]]
        if pool["token"] is None:
            continue
        timestamp = block_times[event["block"]]
        if timestamp > pool["birthTimestamp"] + 7200:
            continue
        token_changes[pool["token"]].append((event["block"], rig.liquidity_delta(event)))
    for token, cohort in by_token.items():
        cohort.sort(key=lambda pool: (pool["birthBlock"], pool["birthLogIndex"]))
        changes = sorted(token_changes[token])
        for pool in cohort:
            entry_block = pool["entryBlock"]
            entry_timestamp = pool["entryTimestamp"]
            if entry_block is None or entry_timestamp is None:
                pool["dynamicTokenHighFeeLiquidity"] = {"status": "unfunded_at_completed_block"}
                continue
            target_end = min(entry_timestamp + 3600, follow_end_time)
            relevant = [other for other in cohort if other["birthTimestamp"] <= target_end]
            coverage_end = min((other["birthTimestamp"] + 7200 for other in relevant), default=target_end)
            if coverage_end < entry_timestamp:
                pool["dynamicTokenHighFeeLiquidity"] = {
                    "status": "older_pool_history_missing_at_entry", "coverageUntilTime": iso(coverage_end)}
                continue
            # The birth cohort begins/ends at the sample boundary. Older pools
            # may remain active before it and new pools can appear after it;
            # never present this as a complete token-wide liquidity history.
            valid_end = min(target_end, coverage_end, birth_to_time)
            baseline = sum(delta for block, delta in changes if block <= entry_block)
            later = [(block, delta) for block, delta in changes
                     if block > entry_block and block_times[block] <= valid_end]
            crossing = block_crossings(later, entry_block + 1, baseline)
            for item in crossing["crossings"].values():
                item["timestamp"] = block_times[item["block"]]
                item["time"] = iso(item["timestamp"])
            later_pools = [other["poolId"] for other in relevant if other["birthBlock"] > entry_block
                           and other["birthTimestamp"] <= valid_end]
            pool["dynamicTokenHighFeeLiquidity"] = {
                "status": ("birth_window_censored" if birth_to_time < entry_timestamp + 3600
                           else "pool_event_history_censored" if coverage_end < entry_timestamp + 3600
                           else "cohort_bounded_60m"),
                "entryOutstanding": str(baseline),
                "peakOutstanding": crossing["peakOutstanding"],
                "currentOutstandingAtCoverageEnd": crossing["currentOutstanding"],
                "firstCrossingsObservedWithinCohort": crossing["crossings"],
                "coverageUntilTimestamp": valid_end, "coverageUntilTime": iso(valid_end),
                "birthCohortFromTimestamp": birth_from_time,
                "birthCohortToTimestamp": birth_to_time,
                "preCohortPoolHistoryVerified": False,
                "postCohortPoolBirthsIncluded": False,
                "poolCountKnownAtEntry": sum(other["birthBlock"] <= entry_block for other in cohort),
                "laterHighFeePoolsBornWithinCoverage": later_pools,
                "unitsCaveat": "Aggregate v4 liquidity units across tick ranges and pools are not token/quote amounts or USD depth. This cohort-only series may omit older and follow-on pools.",
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-matrix", type=Path, default=SOURCE_MATRIX)
    parser.add_argument("--raw-init-cache", type=Path, default=RAW_INIT)
    parser.add_argument("--raw-event-cache", type=Path, default=RAW_EVENTS)
    parser.add_argument("--block-time-cache", type=Path, default=BLOCK_TIMES)
    parser.add_argument("--decoded-birth-cache", type=Path, default=DECODED_BIRTHS)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--rpc-url", default=rig.RPC_URL)
    parser.add_argument("--min-fee-pips", type=int, default=700_000)
    parser.add_argument("--max-fee-pips", type=int, default=1_000_000)
    parser.add_argument("--extend-to-latest", action="store_true",
                        help="continue an existing event cache to current head")
    args = parser.parse_args()
    if not 0 <= args.min_fee_pips < args.max_fee_pips <= 1_000_000:
        parser.error("fee bounds must be 0 <= min < max <= 1000000")
    source = json.loads(args.source_matrix.read_text())
    rpc = rig.Rpc(args.rpc_url)
    births = fetch_births(rpc, source, args.raw_init_cache,
                          args.block_time_cache, args.decoded_birth_cache,
                          args.min_fee_pips, args.max_fee_pips)
    follow_end = (int(json.loads(args.raw_event_cache.read_text())["completedUntilBlock"])
                  if args.raw_event_cache.exists() and not args.extend_to_latest
                  else rpc.latest_block())
    events = fetch_events(rpc, births, source["fromBlock"], follow_end,
                          int(datetime.fromisoformat(source["toTime"].replace("Z", "+00:00")).timestamp()),
                          args.raw_event_cache,
                          args.block_time_cache)
    birth_from_time = int(datetime.fromisoformat(source["fromTime"].replace("Z", "+00:00")).timestamp())
    birth_to_time = int(datetime.fromisoformat(source["toTime"].replace("Z", "+00:00")).timestamp())
    output = analyze(births, events, rpc, follow_end, birth_from_time, birth_to_time,
                     args.min_fee_pips, args.max_fee_pips)
    output["fromBlock"], output["toBirthBlock"] = source["fromBlock"], source["toBlock"]
    output["fromTime"], output["toBirthTime"] = source["fromTime"], source["toTime"]
    atomic_write(args.output, output)
    pools = output["pools"]
    print(json.dumps({"output": str(args.output), "pools": len(pools),
                      "seededPositiveLp": sum(pool["seed"] is not None for pool in pools),
                      "fundedAtCompletedBlock": sum(pool["firstFundedCompletedBlock"] is not None for pool in pools),
                      "swapped30mAfterSeed": sum(pool["swapsAfterSeed"]["1800"]["count"] > 0 for pool in pools)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
