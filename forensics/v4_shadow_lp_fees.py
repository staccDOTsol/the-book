#!/usr/bin/env python3
"""Illustrative fixed-path v4 LP fee allocation for the 24h high-fee cohort.

This is a shadow allocation of *historical* swap fees, not a counterfactual
transaction replay. Adding the position changes liquidity, execution, routing,
and later prices. It does not calculate inventory value or liquidation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import heapq
import json
from pathlib import Path

try:
    from .high_fee_pool_screen import signed_word, words
    from .v4_firehose_cash_envelope import QUOTE, quote_only_band
    from .v4_quote_range_screen import Q96, sqrt_price_at_tick, traverses_interior
except ImportError:  # Direct script execution.
    from high_fee_pool_screen import signed_word, words
    from v4_firehose_cash_envelope import QUOTE, quote_only_band
    from v4_quote_range_screen import Q96, sqrt_price_at_tick, traverses_interior


ROOT = Path(__file__).resolve().parents[1]
POOL_SOURCE = ROOT / ".local/high-fee-pool-screen-24h.json"
EVENT_SOURCE = ROOT / ".local/high-fee-pool-events-24h.json"
FEE_SOURCE = ROOT / ".local/v4-highfee-swap-fees-24h-followup.json"
OUTPUT = ROOT / ".local/v4-shadow-lp-fees-30m.json"
ETH = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
Q192 = Q96 * Q96
ONE_MILLION = Decimal(1_000_000)


def stakes_at_entry(pools: list[dict], initial_usd: Decimal,
                    fraction: Decimal, hold_seconds: int) -> dict[str, Decimal]:
    """Same principal-only timing convention as v4_firehose_cash_envelope."""
    cash = initial_usd
    active: list[tuple[int, str, Decimal]] = []
    stakes = {}
    for pool in pools:
        timestamp = pool["entryTimestamp"]
        while active and active[0][0] <= timestamp:
            cash += heapq.heappop(active)[2]
        stake = cash * fraction
        cash -= stake
        stakes[pool["poolId"]] = stake
        heapq.heappush(active, (timestamp + hold_seconds, pool["poolId"], stake))
    return stakes


def hypothetical_liquidity(pool: dict, stake_usd: Decimal,
                           eth_usd: Decimal) -> tuple[int, int]:
    """Round quote input and L down; a one-sided position at the original band."""
    band = quote_only_band(pool)
    lower = sqrt_price_at_tick(band["tickLower"])
    upper = sqrt_price_at_tick(band["tickUpper"])
    raw_quote = int(stake_usd * (Decimal(10**6) if pool["quoteAsset"] == USDG
                                 else Decimal(10**18) / eth_usd))
    if pool["quoteSide"] == 0:
        liquidity = raw_quote * lower * upper // (Q96 * (upper - lower))
    else:
        liquidity = raw_quote * Q96 // (upper - lower)
    return raw_quote, liquidity


def benchmark_usd(raw: Decimal, currency: str, pool: dict,
                  band_mid_sqrt: int, eth_usd: Decimal) -> Decimal:
    """Mid-band spot mark only; it is not a liquidation quote."""
    if currency == pool["quoteAsset"]:
        raw_quote = raw
    elif currency == pool["token"]:
        raw_quote = (raw * Decimal(band_mid_sqrt)**2 / Decimal(Q192)
                     if pool["quoteSide"] == 1 else
                     raw * Decimal(Q192) / Decimal(band_mid_sqrt)**2)
    else:
        raise ValueError("Fee currency is not in pool")
    scale = Decimal(10**6) if pool["quoteAsset"] == USDG else Decimal(10**18)
    return raw_quote / scale * (Decimal(1) if pool["quoteAsset"] == USDG else eth_usd)


def principal_at_price(liquidity: int, sqrt_price: int,
                       lower: int, upper: int) -> tuple[int, int]:
    """Rounded-down raw principal of a hypothetical position at pool price."""
    bounded = min(max(sqrt_price, lower), upper)
    amount0 = liquidity * Q96 * (upper - bounded) // (upper * bounded)
    amount1 = liquidity * (bounded - lower) // Q96
    return amount0, amount1


def position_outcome(pool: dict, swap_records: list[dict], delay: int,
                     liquidity: int, quote_input_raw: int,
                     lower: int, upper: int, eth_usd: Decimal,
                     own_lp_exit: dict | None = None) -> dict:
    """Use the first observed post-activation endpoint, then stop fee accrual."""
    first_mint_block = pool["entryBlock"] + delay
    price_before_mint = int(pool["entrySqrtPriceX96"])
    for record in swap_records:
        if record["block"] >= first_mint_block:
            break
        price_before_mint = record["postPrice"]
    quote_only = (price_before_mint <= lower if pool["quoteSide"] == 0
                  else price_before_mint >= upper)
    if not quote_only:
        return {"status": "not_quote_only_at_first_mint_block",
                "firstMintBlock": first_mint_block,
                "priceBeforeMintSqrtX96": str(price_before_mint)}
    lp_block = None
    within_cap = (own_lp_exit["block"] <= pool["timeCapBlock"]
                  if own_lp_exit is not None and pool.get("timeCapBlock") is not None
                  else own_lp_exit is not None and own_lp_exit.get("timestamp") is not None
                  and own_lp_exit["timestamp"] <= pool["entryTimestamp"] + 1800)
    if within_cap:
        lp_block = int(own_lp_exit["block"])
        if lp_block < first_mint_block:
            return {"status": "liquidity_signal_before_mint",
                    "firstMintBlock": first_mint_block,
                    "liquiditySignalBlock": lp_block,
                    "priceBeforeMintSqrtX96": str(price_before_mint)}
    activated = False
    fee_quote = fee_token = Decimal(0)
    fee_benchmark = Decimal(0)
    fee_swap_count = zero_l_swaps = 0
    exit_reason = "30m_no_activation"
    exit_price = price_before_mint
    exit_block = None
    exit_timestamp = pool["entryTimestamp"] + 1800
    for record in swap_records:
        if record["block"] < first_mint_block:
            continue
        if lp_block is not None and record["block"] > lp_block:
            break
        exit_price = record["postPrice"]
        if record["overlap"]:
            activated = True
            fee_swap_count += 1
            zero_l_swaps += record["historicalBandLiquidity"] == 0
            if record["inputCurrency"] == pool["quoteAsset"]:
                fee_quote += record["ownFeeRaw"]
            else:
                fee_token += record["ownFeeRaw"]
            fee_benchmark += record["ownFeeBenchmarkUsd"]
        if activated:
            token_only = (exit_price >= upper if pool["quoteSide"] == 0
                          else exit_price <= lower)
            returned_quote = (exit_price <= lower if pool["quoteSide"] == 0
                              else exit_price >= upper)
            if token_only or returned_quote:
                exit_reason = "first_token_only" if token_only else "first_return_quote_only"
                exit_block = record["block"]
                exit_timestamp = record["timestamp"]
                break
            exit_reason = "30m_activated_no_endpoint"
    if exit_block is None and lp_block is not None:
        # A historical LP crossing is observable only when that block has
        # completed. Include any swaps in the same block, then withdraw later.
        exit_reason = "own_pool_liquidity"
        exit_block = lp_block
        exit_timestamp = (int(own_lp_exit["timestamp"])
                          if own_lp_exit.get("timestamp") is not None else None)
    amount0, amount1 = principal_at_price(liquidity, exit_price, lower, upper)
    principal_quote = amount0 if pool["quoteSide"] == 0 else amount1
    principal_token = amount1 if pool["quoteSide"] == 0 else amount0
    entry_amount0, entry_amount1 = principal_at_price(liquidity, price_before_mint,
                                                       lower, upper)
    entry_quote = entry_amount0 if pool["quoteSide"] == 0 else entry_amount1
    fee_exit_mark = (benchmark_usd(fee_quote, pool["quoteAsset"], pool,
                                   exit_price, eth_usd)
                     + benchmark_usd(fee_token, pool["token"], pool,
                                     exit_price, eth_usd))
    principal_exit_mark = (benchmark_usd(Decimal(principal_quote), pool["quoteAsset"],
                                         pool, exit_price, eth_usd)
                           + benchmark_usd(Decimal(principal_token), pool["token"],
                                           pool, exit_price, eth_usd))
    return {"status": "shadow_position", "firstMintBlock": first_mint_block,
            "priceBeforeMintSqrtX96": str(price_before_mint),
            "activated": activated, "exitReason": exit_reason,
            "exitObservationBlock": exit_block,
            "withdrawEarliestBlock": exit_block + 1 if exit_block is not None else None,
            "exitTimestamp": exit_timestamp,
            "exitSqrtPriceX96": str(exit_price),
            "entryQuotePrincipalRaw": str(entry_quote),
            "unusedQuoteRaw": str(quote_input_raw - entry_quote),
            "principalQuoteRaw": str(principal_quote),
            "principalTokenRaw": str(principal_token),
            "feeQuoteRawModel": str(fee_quote),
            "feeTokenRawModel": str(fee_token),
            "feeMidBandBenchmarkUsd": str(fee_benchmark),
            "feeExitSpotMarkUsd": str(fee_exit_mark),
            "principalExitSpotMarkUsd": str(principal_exit_mark),
            "feeSwapCount": fee_swap_count,
            "feeSwapsWithZeroHistoricalBandLiquidity": zero_l_swaps}


def shadow(pool: dict, events: list[tuple[str, dict]], fee_rows: dict,
           block_times: dict[int, int], stake_usd: Decimal, eth_usd: Decimal,
           delays: tuple[int, ...] = (1, 2, 5, 10)) -> dict:
    band = quote_only_band(pool)
    lower_tick, upper_tick = band["tickLower"], band["tickUpper"]
    lower = sqrt_price_at_tick(lower_tick)
    upper = sqrt_price_at_tick(upper_tick)
    mid = (lower + upper) // 2
    quote_raw, own_liquidity = hypothetical_liquidity(pool, stake_usd, eth_usd)
    positions: Counter[tuple[int, int]] = Counter()
    price = int(pool["initialSqrtPriceX96"])
    totals = {str(delay): {"eligibleSwaps": 0, "historicalBandLiquidityZeroSwaps": 0,
                           "historicalBandLpFeeInputRaw": Decimal(0),
                           "shadowOwnFeeInputRaw": Decimal(0),
                           "shadowFeeBenchmarkUsd": Decimal(0),
                           "quoteInputFeeBenchmarkUsd": Decimal(0),
                           "tokenInputFeeBenchmarkUsd": Decimal(0)}
              for delay in delays}
    swap_records = []
    for kind, event in sorted(events, key=lambda item: (
            item[1]["block"], item[1]["transactionIndex"], item[1]["logIndex"])):
        data = words(event["data"], 4 if kind == "lp" else 6)
        if kind == "lp":
            key = (signed_word(data[0]), signed_word(data[1]))
            positions[key] += signed_word(data[2])
            if positions[key] < 0:
                raise ValueError(f"Negative historical LP on {pool['poolId']}")
            continue
        post_price = int(data[2], 16)
        if event["block"] <= pool["entryBlock"]:
            price = post_price
            continue
        cap_block = pool.get("timeCapBlock")
        beyond_cap = (event["block"] > cap_block if cap_block is not None
                      else block_times[event["block"]] > pool["entryTimestamp"] + 1800)
        if beyond_cap:
            price = post_price
            continue
        fee = fee_rows[(pool["poolId"], event["block"], event["logIndex"])]
        input_raw = int(fee["inputRaw"])
        overlap = input_raw > 0 and traverses_interior(price, post_price, lower, upper)
        historical_l = 0
        historical_band_fee = own_fee = value_usd = Decimal(0)
        if overlap:
            lo = max(min(price, post_price), lower)
            hi = min(max(price, post_price), upper)
            historical_l = sum(liquidity for (a, b), liquidity in positions.items()
                               if a <= lower_tick and b >= upper_tick)
            if historical_l < 0:
                raise ValueError("Negative active band liquidity")
            total_fee_pips = int(fee["totalSwapFeePips"])
            net_input = Decimal(input_raw) * (ONE_MILLION - total_fee_pips) / ONE_MILLION
            if net_input <= 0:
                raise ValueError("Nonpositive estimated principal input")
            if fee["inputCurrency"] == pool["currency1"]:
                band_input = Decimal(historical_l) * Decimal(hi - lo) / Decimal(Q96)
            else:
                band_input = (Decimal(historical_l) * Decimal(Q96) * Decimal(hi - lo)
                              / (Decimal(hi) * Decimal(lo)))
            path_fraction = band_input / net_input
            if path_fraction > Decimal("1.01"):
                raise ValueError(f"Band principal exceeds observed swap principal: {pool['poolId']}")
            path_fraction = min(Decimal(1), path_fraction)
            historical_band_fee = Decimal(fee["historicalLpFeeLowerBoundRaw"]) * path_fraction
            # Fixed-path, fixed-total-fee allocation assumption; when existing
            # band L is zero, the counterfactual fee remains unidentified.
            own_fee = (historical_band_fee * own_liquidity / (historical_l + own_liquidity)
                       if historical_l > 0 and own_liquidity > 0 else Decimal(0))
            value_usd = benchmark_usd(own_fee, fee["inputCurrency"], pool, mid, eth_usd)
            for delay in delays:
                if event["block"] < pool["entryBlock"] + delay:
                    continue
                result = totals[str(delay)]
                result["eligibleSwaps"] += 1
                result["historicalBandLiquidityZeroSwaps"] += historical_l == 0
                result["historicalBandLpFeeInputRaw"] += historical_band_fee
                result["shadowOwnFeeInputRaw"] += own_fee
                result["shadowFeeBenchmarkUsd"] += value_usd
                asset = "quote" if fee["inputCurrency"] == pool["quoteAsset"] else "token"
                result[f"{asset}InputFeeBenchmarkUsd"] += value_usd
        swap_records.append({"block": event["block"],
                             "timestamp": block_times.get(event["block"]),
                             "postPrice": post_price, "overlap": overlap,
                             "historicalBandLiquidity": historical_l,
                             "inputCurrency": fee["inputCurrency"],
                             "ownFeeRaw": own_fee,
                             "ownFeeBenchmarkUsd": value_usd})
        price = post_price
    if price is None:
        raise AssertionError("Impossible missing pool price")
    return {"poolId": pool["poolId"], "token": pool["token"],
            "quoteAsset": pool["quoteAsset"], "entryBlock": pool["entryBlock"],
            "entryTimestamp": pool["entryTimestamp"], "stakeUsd": str(stake_usd),
            "quoteInputRaw": str(quote_raw), "hypotheticalLiquidity": str(own_liquidity),
            "boundaryExitByMinimumBlockDelay": {
                str(delay): position_outcome(pool, swap_records, delay, own_liquidity,
                                             quote_raw, lower, upper, eth_usd)
                for delay in delays},
            "boundaryOrOwn75ExitByMinimumBlockDelay": {
                str(delay): position_outcome(
                    pool, swap_records, delay, own_liquidity, quote_raw, lower,
                    upper, eth_usd, pool["ownLiquidity"]["crossings"].get("0.75"))
                for delay in delays},
            "ownLiquidityExitSweepAtDelay2": {
                remaining: position_outcome(
                    pool, swap_records, 2, own_liquidity, quote_raw, lower,
                    upper, eth_usd, pool["ownLiquidity"]["crossings"].get(remaining))
                for remaining in ("0.95", "0.90", "0.85", "0.80", "0.75", "0.70", "0.60", "0.50")},
            "byMinimumBlockDelay": {
                delay: {key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in stats.items()}
                for delay, stats in totals.items()}}


def main() -> None:
    with localcontext() as context:
        context.prec = 100
        source = json.loads(POOL_SOURCE.read_text())
        raw = json.loads(EVENT_SOURCE.read_text())
        fees = json.loads(FEE_SOURCE.read_text())
        pools = [pool for pool in source["pools"]
                 if pool["quoteAsset"] in QUOTE and quote_only_band(pool) is not None]
        pools.sort(key=lambda pool: (pool["entryTimestamp"], pool["entryBlock"], pool["poolId"]))
        stakes = stakes_at_entry(pools, Decimal(104), Decimal("0.25"), 1800)
        by_pool: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for kind, key in (("lp", "lpLogs"), ("swap", "swapLogs")):
            for event in raw[key]:
                by_pool[event["poolId"]].append((kind, event))
        fee_rows = {(row["poolId"], row["block"], row["logIndex"]): row
                    for row in fees["swaps"]}
        block_times = {int(block): timestamp for block, timestamp in raw["blockTimes"].items()}
        rows = [shadow(pool, by_pool[pool["poolId"]], fee_rows, block_times,
                       stakes[pool["poolId"]], Decimal(2600)) for pool in pools]
        def outcome_summary(items: list[tuple[dict, dict]]) -> dict:
            live = [(row, outcome) for row, outcome in items
                    if outcome["status"] == "shadow_position"]
            return {
                "statusCounts": dict(Counter(outcome["status"] for _, outcome in items)),
                "exitReasonCounts": dict(Counter(outcome["exitReason"]
                                         for _, outcome in live)),
                "enteredPools": len(live),
                "validStakeUsd": str(sum(Decimal(row["stakeUsd"]) for row, _ in live)),
                "activatedPools": sum(outcome["activated"] for _, outcome in live),
                "accrualSwapCountBeforeExit": sum(outcome["feeSwapCount"]
                                                  for _, outcome in live),
                "feeSwapsWithZeroHistoricalBandLiquidity": sum(
                    outcome["feeSwapsWithZeroHistoricalBandLiquidity"]
                    for _, outcome in live),
                "feeExitSpotMarkUsd": str(sum(Decimal(outcome["feeExitSpotMarkUsd"])
                                              for _, outcome in live)),
                "principalExitSpotMarkUsd": str(sum(
                    Decimal(outcome["principalExitSpotMarkUsd"])
                    for _, outcome in live)),
            }

        summary = {}
        boundary_summary = {}
        own75_summary = {}
        for delay in (1, 2, 5, 10):
            stats = [row["byMinimumBlockDelay"][str(delay)] for row in rows]
            summary[str(delay)] = {
                "poolsWithEligibleSwap": sum(row["eligibleSwaps"] > 0 for row in stats),
                "eligibleSwapCount": sum(row["eligibleSwaps"] for row in stats),
                "swapsWithZeroHistoricalBandLiquidity": sum(
                    row["historicalBandLiquidityZeroSwaps"] for row in stats),
                "poolsWithZeroHistoricalBandLiquiditySwap": sum(
                    row["historicalBandLiquidityZeroSwaps"] > 0 for row in stats),
                "shadowFeeBenchmarkUsd": str(sum(
                    Decimal(row["shadowFeeBenchmarkUsd"]) for row in stats)),
                "quoteInputFeeBenchmarkUsd": str(sum(
                    Decimal(row["quoteInputFeeBenchmarkUsd"]) for row in stats)),
                "tokenInputFeeBenchmarkUsd": str(sum(
                    Decimal(row["tokenInputFeeBenchmarkUsd"]) for row in stats)),
            }
            boundary_summary[str(delay)] = outcome_summary([
                (row, row["boundaryExitByMinimumBlockDelay"][str(delay)]) for row in rows])
            own75_summary[str(delay)] = outcome_summary([
                (row, row["boundaryOrOwn75ExitByMinimumBlockDelay"][str(delay)])
                for row in rows])
        threshold_sweep = {
            remaining: outcome_summary([
                (row, row["ownLiquidityExitSweepAtDelay2"][remaining]) for row in rows])
            for remaining in ("0.95", "0.90", "0.85", "0.80", "0.75", "0.70", "0.60", "0.50")}
        result = {"schemaVersion": 1, "poolSource": str(POOL_SOURCE.relative_to(ROOT)),
                  "eventSource": str(EVENT_SOURCE.relative_to(ROOT)),
                  "feeSource": str(FEE_SOURCE.relative_to(ROOT)),
                  "model": "Frozen observed price/volume path. Allocate historical LP input-asset fee bound to the one-tick band by its reconstructed pre-swap active L and sqrt-price path's net-principal fraction; assign proposed position L/(historical band L + proposed L). Not an executable replay.",
                  "valuation": "Boundary-exit fee and principal marks use historical exit post-swap sqrtPriceX96 (30m last pool price for timeout), not a liquidation quote. Diagnostic all-30m fees use original band-midpoint spot. ETH/USDG conversion assumes $2600/ETH. Gas, slippage and route changes excluded.",
                  "stakeRule": "25% of remaining $104 quote-equivalent cash; static baseline stakes reserve every entry for 30m and return principal unchanged, no gas debit. Exits and invalid entries do not rebudget subsequent stakes; use a separate dynamic wallet replay for that.",
                  "poolCount": len(rows), "minimumBlockDelaySummary": summary,
                  "boundaryExitSummary": boundary_summary,
                  "boundaryOrOwn75ExitSummary": own75_summary,
                  "ownLiquidityThresholdSweepAtDelay2": threshold_sweep,
                  "rows": rows}
        OUTPUT.write_text(json.dumps(result, separators=(",", ":")) + "\n")
        print(json.dumps({"output": str(OUTPUT), "boundaryOrOwn75ExitSummary": own75_summary,
                          "ownLiquidityThresholdSweepAtDelay2": threshold_sweep},
                         indent=2))


if __name__ == "__main__":
    main()
