#!/usr/bin/env python3
"""Signal-time fee/capital shadow screen for RH v4 burst-token quote LPs.

Only zero-hook static-fee quote pools with positive historical net LP at the
completed 4/300 or 5/600 signal block are candidate destinations. The signal
may have been caused by a hooked pool; that pool is never a destination here.
Every hypothetical band is selected from the *signal-block* price. All fee
marks remain fixed-path scenarios, not executable fills or profit estimates.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import json
from pathlib import Path
from statistics import median

from high_fee_pool_screen import (atomic_write, block_crossings,
                                  quote_only_ranges, signed_word, words)
from rig_monitor import RPC_URL, Rpc
from v4_protocol_fee_join import calculate_total_swap_fee, ceil_div
from v4_shadow_lp_fees import shadow


ROOT = Path(__file__).resolve().parents[1]
BIRTHS = ROOT / ".local/all-v4-births-24h-with-warmup.json"
BASE_EVENTS = ROOT / ".local/burst-v4-pool-events-24h.json"
SUP_EVENTS = ROOT / ".local/burst-v4-supplemental-events-24h.json"
MATRIX = ROOT / ".local/threshold-matrix-24h.json"
RAW_SIGNALS = ROOT / ".local/burst-static-signals-safe-lp-targets-24h.json"
STRICT_SIGNALS = ROOT / ".local/v4-burst-strict-funded-signals-24h.json"
HOOK_EVENTS = ROOT / ".local/burst-hooked-signal-evidence-events.json"
BLOCK_TIME_CACHE = ROOT / ".local/high-fee-block-times-24h.json"
CAP_CACHE = ROOT / ".local/burst-v4-cap-block-times.json"
OUTPUT = ROOT / ".local/v4-burst-fee-capital-24h.json"
STAKES = (Decimal("2.60"), Decimal("4"), Decimal("26"))
ETH_USD = Decimal(2600)
MINT_DELAY_BLOCKS = 2


def order(event: dict) -> tuple[int, int, int]:
    return event["block"], event["transactionIndex"], event["logIndex"]


def signals() -> tuple[list[dict], list[dict], list[dict]]:
    matrix = json.loads(MATRIX.read_text())
    primary = [{"rule": rule, "token": candidate["token"],
                "entryBlock": candidate["entryBlock"],
                "entryTimestamp": candidate["entryTimestamp"],
                "source": "old_matrix_funded"}
               for rule in ("4/300", "5/600")
               for candidate in matrix["cells"][rule]["candidates"]]
    raw = [{"rule": row["rule"], "token": row["token"],
            "entryBlock": row["entryBlock"],
            "entryTimestamp": row["entryTimestamp"],
            "source": "raw_all_static"}
           for row in json.loads(RAW_SIGNALS.read_text())["signals"]]
    raw_by_key = {(row["rule"], row["token"]): row for row in raw}
    if any((row["rule"], row["token"]) not in raw_by_key
           or row["entryBlock"] != raw_by_key[row["rule"], row["token"]]["entryBlock"]
           for row in primary):
        raise ValueError("Funded and raw first-signal timing differs")
    strict = [{"rule": row["rule"], "token": row["token"],
               "entryBlock": row["funded"]["entryBlock"],
               "entryTimestamp": row["funded"]["entryTimestamp"],
               "source": "strict_n_funded_births"}
              for row in json.loads(STRICT_SIGNALS.read_text())["signals"]
              if row["funded"]["status"] == "funded_signal"]
    return primary, raw, strict


def resolve_cap_blocks(signals_to_run: list[dict], rpc: Rpc) -> dict[int, int]:
    cached = {int(block): int(stamp) for block, stamp in
              json.loads(BLOCK_TIME_CACHE.read_text()).items()}
    if CAP_CACHE.exists():
        cached.update({int(block): int(stamp) for block, stamp in
                       json.loads(CAP_CACHE.read_text()).items()})
    rpc.block_times.update(cached)
    ordered = sorted(cached)
    times = [cached[block] for block in ordered]
    if any(a > b for a, b in zip(times, times[1:])):
        raise ValueError("Cached RH block timestamps are not monotone")
    targets = {signal["entryTimestamp"] + 1800 for signal in signals_to_run}
    bounds = {}
    for target in targets:
        index = bisect_left(times, target)
        if index == 0 or index == len(ordered):
            raise ValueError("Cap outside timestamp cache")
        bounds[target] = [ordered[index - 1], ordered[index]]
    while any(high - low > 1 for low, high in bounds.values()):
        mids = {(low + high) // 2 for low, high in bounds.values() if high - low > 1}
        rpc.load_block_times(mids)
        cached.update(rpc.block_times)
        for target, pair in bounds.items():
            low, high = pair
            if high - low <= 1:
                continue
            middle = (low + high) // 2
            pair[1 if cached[middle] >= target else 0] = middle
    for target, (low, high) in bounds.items():
        if high != low + 1 or not cached[low] < target <= cached[high]:
            raise AssertionError("Cap block not bounded by adjacent headers")
    atomic_write(CAP_CACHE, {str(block): stamp for block, stamp in cached.items()})
    return {target: high for target, (_, high) in bounds.items()}


def load_events() -> dict[str, list[tuple[str, dict]]]:
    by_pool: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    seen_ids = set()
    for path in (BASE_EVENTS, SUP_EVENTS, HOOK_EVENTS):
        cache = json.loads(path.read_text())
        if cache["completedUntilBlock"] < 78_740_000:
            raise ValueError(f"Incomplete cache {path}")
        overlap = seen_ids.intersection(cache["poolIds"])
        if overlap:
            raise ValueError(f"Duplicate pool event cache: {len(overlap)}")
        seen_ids.update(cache["poolIds"])
        for key, kind in (("lpLogs", "lp"), ("swapLogs", "swap"),
                          ("protocolFeeLogs", "protocol")):
            for event in cache[key]:
                by_pool[event["poolId"]].append((kind, event))
    for rows in by_pool.values():
        rows.sort(key=lambda item: order(item[1]))
    return by_pool


def fee_rows(births: dict[str, dict], events: dict[str, list[tuple[str, dict]]],
             safe_pool_ids: set[str]) -> tuple[dict, int]:
    records = {}
    mismatches = 0
    for pool_id in safe_pool_ids:
        birth = births[pool_id]
        packed = 0
        for kind, event in events[pool_id]:
            if kind == "protocol":
                packed = int(words(event["data"], 1)[0], 16)
                if packed >= 1 << 24:
                    raise ValueError("Protocol fee exceeds uint24")
                continue
            if kind != "swap":
                continue
            data = words(event["data"], 6)
            amount0, amount1 = signed_word(data[0]), signed_word(data[1])
            if amount0 < 0 <= amount1:
                input_currency = birth["currency0"]
                input_raw = -amount0
                protocol_pips = packed & 0xfff
            elif amount1 < 0 <= amount0:
                input_currency = birth["currency1"]
                input_raw = -amount1
                protocol_pips = packed >> 12 & 0xfff
            elif amount0 == amount1 == 0:
                input_currency = None
                input_raw = protocol_pips = 0
            else:
                raise ValueError(f"Unexpected Swap signs: {pool_id}")
            event_fee = int(data[5], 16)
            if input_raw and event_fee != calculate_total_swap_fee(
                    birth["feePips"], protocol_pips):
                mismatches += 1
            if input_raw and event_fee != protocol_pips:
                lp_fee = max(0, ceil_div(input_raw * event_fee, 1_000_000)
                             - input_raw * protocol_pips // 1_000_000)
            else:
                lp_fee = 0
            records[(pool_id, event["block"], event["logIndex"])] = {
                "poolId": pool_id, "block": event["block"],
                "logIndex": event["logIndex"], "inputRaw": str(input_raw),
                "inputCurrency": input_currency,
                "totalSwapFeePips": event_fee,
                "historicalLpFeeLowerBoundRaw": str(lp_fee),
            }
    return records, mismatches


def signal_pool(birth: dict, events: list[tuple[str, dict]], signal: dict,
                cap_block: int) -> tuple[dict, dict] | None:
    price = int(birth["initialSqrtPriceX96"])
    tick = int(birth["initialTick"])
    net_lp = 0
    ranges: Counter[tuple[int, int]] = Counter()
    pre_signal_swap_count = 0
    for kind, event in events:
        if event["block"] > signal["entryBlock"]:
            break
        if kind == "lp":
            data = words(event["data"], 4)
            delta = signed_word(data[2])
            net_lp += delta
            ranges[(signed_word(data[0]), signed_word(data[1]))] += delta
        elif kind == "swap":
            data = words(event["data"], 6)
            price = int(data[2], 16)
            tick = signed_word(data[4])
            pre_signal_swap_count += 1
    if net_lp <= 0:
        return None
    band = quote_only_ranges(tick, birth["tickSpacing"], birth["quoteSide"])
    one = band["widthInTickIntervals"].get("1")
    if one is None:
        return None
    from v4_quote_range_screen import sqrt_price_at_tick
    lower, upper = sqrt_price_at_tick(one["tickLower"]), sqrt_price_at_tick(one["tickUpper"])
    outside = price <= lower if birth["quoteSide"] == 0 else price >= upper
    if not outside:
        return None
    active_lp = sum(liquidity for (a, b), liquidity in ranges.items() if a <= tick < b)
    later_changes = []
    for kind, event in events:
        if kind != "lp" or not signal["entryBlock"] < event["block"] <= cap_block:
            continue
        later_changes.append((event["block"], signed_word(words(event["data"], 4)[2])))
    own = block_crossings(later_changes, signal["entryBlock"] + 1,
                          initial_current=net_lp)
    synthetic = {**birth, "entryBlock": signal["entryBlock"],
                 "entryTimestamp": signal["entryTimestamp"],
                 "entrySqrtPriceX96": str(price), "entryTick": tick,
                 "quoteOnlyOutsideTickRanges": band,
                 "timeCapBlock": cap_block, "ownLiquidity": own}
    metadata = {"poolId": birth["poolId"], "feePips": birth["feePips"],
                "tickSpacing": birth["tickSpacing"], "quoteAsset": birth["quoteAsset"],
                "poolBirthBlock": birth["birthBlock"],
                "preSignalSwapCount": pre_signal_swap_count,
                "netLpAtSignal": str(net_lp), "activeLpAtSignal": str(active_lp),
                "ownLpCrossing75Block": own["crossings"].get("0.75", {}).get("block"),
                "signalSqrtPriceX96": str(price), "signalTick": tick,
                "tickLower": one["tickLower"], "tickUpper": one["tickUpper"]}
    return synthetic, metadata


def fee_band(pips: int) -> str:
    cuts = ((10_000, "<1%"), (50_000, "1–<5%"), (100_000, "5–<10%"),
            (300_000, "10–<30%"), (500_000, "30–<50%"),
            (700_000, "50–<70%"), (900_000, "70–<90%"),
            (990_000, "90–<99%"), (1_000_000, "99–<100%"))
    return next(name for cut, name in cuts if pips < cut)


def summarize(rows: list[dict], stake: str) -> dict:
    eligible = [row for row in rows if row["outcomes"][stake]["status"] == "shadow_position"]
    activated = [row for row in eligible if row["outcomes"][stake]["activated"]]
    fee_marks = [Decimal(row["outcomes"][stake]["feeExitSpotMarkUsd"])
                 for row in eligible]
    yields = [mark / Decimal(stake) for mark in fee_marks]
    return {"eligibleDestinations": len(eligible),
            "uniqueTokens": len({row["token"] for row in eligible}),
            "activatedDestinations": len(activated),
            "feeMarkUsdTotal": str(sum(fee_marks)),
            "feeYieldMedian": str(median(yields)) if yields else None,
            "feeYieldMean": str(sum(yields) / len(yields)) if yields else None,
            "feeMarkUsdExcludingLargestToken": str(sum(
                Decimal(row["outcomes"][stake]["feeExitSpotMarkUsd"])
                for row in eligible if row["token"] != "0xafdc93ae51e4d9640534f4e44789561c182cdab9"))}


def main() -> None:
    with localcontext() as context:
        context.prec = 100
        primary, raw, strict = signals()
        all_signals = primary + raw + strict
        rpc = Rpc(RPC_URL)
        caps = resolve_cap_blocks(all_signals, rpc)
        birth_list = json.loads(BIRTHS.read_text())["births"]
        births = {birth["poolId"]: birth for birth in birth_list}
        by_token = defaultdict(list)
        for birth in birth_list:
            if (birth["token"] and birth["quoteAsset"]
                    and int(birth["hooks"], 16) == 0
                    and 0 <= birth["feePips"] < 1_000_000):
                by_token[birth["token"]].append(birth)
        hooked_by_token = defaultdict(list)
        for birth in birth_list:
            if (birth["token"] and birth["quoteAsset"]
                    and birth["hooks"] == "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
                    and birth["feePips"] == 0):
                hooked_by_token[birth["token"]].append(birth)
        events = load_events()
        safe_ids = {birth["poolId"] for signal in all_signals
                    for birth in by_token[signal["token"]]
                    if birth["birthBlock"] <= signal["entryBlock"]}
        missing = safe_ids - events.keys()
        if missing:
            raise ValueError(f"Missing LP/Swap history for {len(missing)} safe pools")
        fee_map, mismatches = fee_rows(births, events, safe_ids)
        if mismatches:
            raise ValueError(f"{mismatches} observed Swap fee formula mismatches")
        hooked_ids = {birth["poolId"] for signal in all_signals
                      for birth in hooked_by_token[signal["token"]]
                      if birth["birthBlock"] <= signal["entryBlock"]}
        hook_missing = hooked_ids - events.keys()
        if hook_missing:
            raise ValueError(f"Missing event history for {len(hook_missing)} hooked pools")
        hooked_fee_map, hook_core_mismatches = fee_rows(births, events, hooked_ids)
        output_rows = []
        hooked_rows = []
        for signal in all_signals:
            cap = caps[signal["entryTimestamp"] + 1800]
            for birth in by_token[signal["token"]]:
                if birth["birthBlock"] > signal["entryBlock"]:
                    continue
                pool = signal_pool(birth, events[birth["poolId"]], signal, cap)
                if pool is None:
                    continue
                synthetic, metadata = pool
                outcomes = {}
                sweep_at_4 = {}
                for stake in STAKES:
                    detail = shadow(synthetic, [(kind, event) for kind, event in events[birth["poolId"]]
                                                if kind in ("lp", "swap")], fee_map, {},
                                    stake, ETH_USD, delays=(MINT_DELAY_BLOCKS,))
                    outcomes[str(stake)] = detail[
                        "boundaryOrOwn75ExitByMinimumBlockDelay"][str(MINT_DELAY_BLOCKS)]
                    if stake == Decimal(4):
                        sweep_at_4 = detail["ownLiquidityExitSweepAtDelay2"]
                output_rows.append({"signalSource": signal["source"],
                                    "rule": signal["rule"], "token": signal["token"],
                                    "signalBlock": signal["entryBlock"],
                                    "signalTimestamp": signal["entryTimestamp"],
                                    "timeCapBlock": cap, "feeBand": fee_band(birth["feePips"]),
                                    **metadata, "outcomes": outcomes,
                                    "ownLpThresholdSweepAt4Usd": sweep_at_4})
            for birth in hooked_by_token[signal["token"]]:
                if birth["birthBlock"] > signal["entryBlock"]:
                    continue
                pool = signal_pool(birth, events[birth["poolId"]], signal, cap)
                if pool is None:
                    continue
                synthetic, metadata = pool
                detail = shadow(synthetic, [(kind, event) for kind, event in events[birth["poolId"]]
                                            if kind in ("lp", "swap")], hooked_fee_map, {},
                                Decimal(4), ETH_USD, delays=(MINT_DELAY_BLOCKS,))
                outcome = detail["boundaryOrOwn75ExitByMinimumBlockDelay"][str(MINT_DELAY_BLOCKS)]
                post_signal_swaps = sum(kind == "swap" and signal["entryBlock"] + 2 <= event["block"] <= cap
                                        for kind, event in events[birth["poolId"]])
                hooked_rows.append({"signalSource": signal["source"],
                                    "rule": signal["rule"], "token": signal["token"],
                                    "signalBlock": signal["entryBlock"],
                                    "signalTimestamp": signal["entryTimestamp"],
                                    "timeCapBlock": cap, "hook": birth["hooks"],
                                    **metadata, "postSignalSwaps30m": post_signal_swaps,
                                    "coreFeeStatus": "Pons core fee is zero; its hook fee accrues to creator/protocol/buyback, not outside LP fee growth",
                                    "coreOnly4UsdOutcome": outcome})
        summaries = {}
        for source in ("old_matrix_funded", "raw_all_static", "strict_n_funded_births"):
            for rule in ("4/300", "5/600"):
                subset = [row for row in output_rows if row["rule"] == rule
                          and row["signalSource"] == source]
                summaries[f"{source}:{rule}"] = {
                    stake: summarize(subset, stake) for stake in map(str, STAKES)}
                summaries[f"{source}:{rule}"]["byFeeBandAt4Usd"] = {
                    band: summarize([row for row in subset if row["feeBand"] == band], "4")
                    for band in sorted({row["feeBand"] for row in subset})}
                hook_subset = [row for row in hooked_rows if row["rule"] == rule
                               and row["signalSource"] == source]
                summaries[f"{source}:{rule}"]["hookedCoreOnly"] = {
                    "destinationCases": len(hook_subset),
                    "uniqueTokens": len({row["token"] for row in hook_subset}),
                    "postSignalSwaps30m": sum(row["postSignalSwaps30m"]
                                              for row in hook_subset),
                    "oneTickBandActivations": sum(
                        row["coreOnly4UsdOutcome"].get("activated", False)
                        for row in hook_subset),
                    "positiveModeledCoreFeeCases": sum(
                        Decimal(row["coreOnly4UsdOutcome"].get("feeExitSpotMarkUsd", "0")) > 0
                        for row in hook_subset),
                }
        result = {"schemaVersion": 1,
                  "source": "4/300 and 5/600 completed-block signals; zero-hook static quote destinations with positive historical net LP at signal",
                  "signalTiming": "Band selected from last observed pool price at completed signal block; fees before that block excluded. Mint assumed in signalBlock+2 before that block's swaps; actual tx order unverified.",
                  "capitalModel": "Independent fixed $2.60/$4/$26 position per eligible pool, not a cash-constrained portfolio. First observed own-pool net LP below 75% of post-signal peak, full-token/return-full-quote boundary, or 30m ends accrual. Principal/fee spot marks use observed pool sqrt at exit. No executable liquidation or counterfactual swap simulation.",
                  "ethUsdAssumption": str(ETH_USD),
                  "protocolFeeMismatches": mismatches,
                  "hookedCoreFeeFormulaMismatches": hook_core_mismatches,
                  "hookedAccountingScope": "0xe5e702... is the Pons V2 Meme Hook. Its core LP fee is zero; the hook's separate tax routes to protocol/creator/buyback, not outside LPs. Hooked rows are a core-fee comparator, not LP targets.",
                  "primarySignalRows": len(primary), "rawSensitivitySignalRows": len(raw),
                  "strictFundedSignalRows": len(strict),
                  "strictOnlyRuleTokens": sorted([list(key) for key in
                      ({(row["rule"], row["token"]) for row in strict} -
                       {(row["rule"], row["token"]) for row in primary})]),
                  "oldMatrixOnlyRuleTokens": sorted([list(key) for key in
                      ({(row["rule"], row["token"]) for row in primary} -
                       {(row["rule"], row["token"]) for row in strict})]),
                  "safePoolIds": len(safe_ids),
                  "hookedPoolIds": len(hooked_ids),
                  "summaries": summaries, "rows": output_rows,
                  "hookedCoreOnlyRows": hooked_rows}
        atomic_write(OUTPUT, result)
        print(json.dumps({"output": str(OUTPUT), "rows": len(output_rows),
                          "primary4At4": summaries["old_matrix_funded:4/300"]["4"],
                          "primary5At4": summaries["old_matrix_funded:5/600"]["4"]},
                         indent=2))


if __name__ == "__main__":
    main()
