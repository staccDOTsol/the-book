#!/usr/bin/env python3
"""Historical RH v4 quote-only firehose activity and cash envelope.

This is deliberately a *partial* LP backtest. It uses completed entry-block
ticks and later observed swap ticks to identify positions that could have
earned core swap fees. It does not assign hypothetical fees or token exit value
to bands that historical LPs never minted. Adding those bands would change
swap execution and aggregator routing, so those outcomes require a fork replay.
"""

from __future__ import annotations

import argparse
from collections import Counter
import heapq
import json
from pathlib import Path
from statistics import median

try:
    from .v4_quote_range_screen import sqrt_price_at_tick
except ImportError:  # Direct script execution from the forensics directory.
    from v4_quote_range_screen import sqrt_price_at_tick


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".local" / "high-fee-pool-screen-24h.json"
OUTPUT = ROOT / ".local" / "v4-firehose-cash-envelope-24h.json"
QUOTE = {
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0x0000000000000000000000000000000000000000",  # native ETH
}
HOLDS_MINUTES = (5, 10, 15, 20, 30, 45, 60)
FRACTIONS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25)
GAS_USD_SENSITIVITY = (0.03, 0.05)


def quote_only_band(pool: dict, width: int = 1) -> dict | None:
    ranges = pool["quoteOnlyOutsideTickRanges"]
    if ranges.get("status") != "legal_grid_ranges":
        return None
    return ranges["widthInTickIntervals"][str(width)]


def observed_range_state(pool: dict, minutes: int, width: int = 1) -> str:
    """Classify observed post-entry path; boundary-only is not interior LP flow."""
    band = quote_only_band(pool, width)
    if band is None:
        return "illegal_band"
    deadline = pool["entryTimestamp"] + minutes * 60
    swaps = [event for event in pool["swapEvents"]
             if event["block"] > pool["entryBlock"] and event["timestamp"] <= deadline]
    if not swaps:
        return "no_swap"
    prices = [int(pool["entrySqrtPriceX96"]),
              *(int(event["postSqrtPriceX96"]) for event in swaps)]
    lower = sqrt_price_at_tick(band["tickLower"])
    upper = sqrt_price_at_tick(band["tickUpper"])
    if pool["quoteSide"] == 0:
        interior = max(prices) > lower
        boundary = max(prices) == lower
        end = prices[-1]
        inventory = "token" if end >= upper else "mixed" if end > lower else "quote"
    else:
        interior = min(prices) < upper
        boundary = min(prices) == upper
        end = prices[-1]
        inventory = "token" if end <= lower else "mixed" if end < upper else "quote"
    if interior:
        return "crossed_end_" + inventory
    return "boundary_only" if boundary else "swap_no_entry"


def cash_envelope(entries: list[dict], states: dict[str, str], minutes: int,
                  fraction: float, initial_cash: float) -> dict:
    """Return every principal dollar at timeout; crossed PnL remains unknown."""
    cash = initial_cash
    active: list[tuple[int, str, float]] = []
    stakes: list[float] = []
    crossing_notional = 0.0
    max_open = 0
    for pool in entries:
        timestamp = pool["entryTimestamp"]
        while active and active[0][0] <= timestamp:
            _, _, principal = heapq.heappop(active)
            cash += principal
        stake = cash * fraction
        cash -= stake
        stakes.append(stake)
        heapq.heappush(active, (timestamp + minutes * 60, pool["poolId"], stake))
        max_open = max(max_open, len(active))
        if states[pool["poolId"]].startswith("crossed_"):
            crossing_notional += stake
    while active:
        cash += heapq.heappop(active)[2]
    if abs(cash - initial_cash) > 1e-7:
        raise AssertionError("Cash conservation failed")
    return {
        "fractionOfAvailableCash": fraction,
        "entryCount": len(stakes),
        "peakOpenPositions": max_open,
        "stakeUsdMedian": median(stakes),
        "stakeUsdMinimum": min(stakes),
        "entriesBelowOneCent": sum(stake < .01 for stake in stakes),
        "entriesBelowTenCents": sum(stake < .1 for stake in stakes),
        "entriesBelowOneDollar": sum(stake < 1 for stake in stakes),
        "crossingStakeUsd": crossing_notional,
        "gasOnlyBreakEvenGrossReturnOnCrossingStake": {
            str(gas): (len(stakes) * gas / crossing_notional if crossing_notional else None)
            for gas in GAS_USD_SENSITIVITY
        },
    }


def run(source: dict, initial_cash: float) -> dict:
    quote_pools = [pool for pool in source["pools"] if pool["quoteAsset"] in QUOTE]
    legal = [pool for pool in quote_pools if quote_only_band(pool) is not None]
    legal.sort(key=lambda pool: (pool["entryTimestamp"], pool["entryBlock"], pool["poolId"]))
    holds = {}
    for minutes in HOLDS_MINUTES:
        states = {pool["poolId"]: observed_range_state(pool, minutes) for pool in legal}
        if any(state == "illegal_band" for state in states.values()):
            raise AssertionError("Illegal entry survived legal-range filter")
        holds[str(minutes)] = {
            "observedPathStates": dict(Counter(states.values())),
            "cashEnvelope": [cash_envelope(legal, states, minutes, fraction, initial_cash)
                             for fraction in FRACTIONS],
        }
    return {
        "schemaVersion": 1,
        "source": str(SOURCE.relative_to(ROOT)),
        "entryRule": "Every birth-cohort no-hook >=70% static-fee USDG/ETH v4 pool with completed-block positive LP and a legal adjacent one-tick quote-only range",
        "entryWindow": [source["fromTime"], source["toBirthTime"]],
        "quotePairBirths": len(quote_pools),
        "legalQuoteOnlyEntries": len(legal),
        "initialCashUsd": initial_cash,
        "cashModel": "Stake fraction of remaining quote-equivalent cash; principal returns unchanged at fixed timeout; no token-conversion PnL, fees, gas, FX, bridge, or dynamic-liquidity exit is applied to cash. ETH/USDG funding is assumed frictionless.",
        "rangeModel": "Observed swap post-tick path. Interior crossing is necessary but insufficient for core fee accrual. Added LP changes pool state and route flow, so crossing outcomes are unidentified.",
        "gasHurdleModel": "Illustrative fixed gas USD per entry+exit, $0.03/$0.05, charged externally; gross crossing-position return needed to offset all firehose gas before any inventory loss. Not measured realized PnL.",
        "holdsMinutes": holds,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--initial-cash", type=float, default=104.0)
    args = parser.parse_args()
    source = json.loads(args.source.read_text())
    result = run(source, args.initial_cash)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output),
                      "legalEntries": result["legalQuoteOnlyEntries"],
                      "thirtyMinuteStates": result["holdsMinutes"]["30"]["observedPathStates"]}))


if __name__ == "__main__":
    main()
