#!/usr/bin/env python3
"""Screen whether observed RH v4 swaps traverse a quote-only LP range.

This is a passive historical price-path screen, not an executable LP backtest.
It reads high_fee_pool_screen.py's output and writes a compact local result.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".local" / "high-fee-pool-screen-24h.json"
OUTPUT = ROOT / ".local" / "v4-quote-range-screen-30m.json"
Q96 = 1 << 96
MAX_TICK = 887_272
# Q128.128 multipliers from Uniswap v4-core TickMath.getSqrtPriceAtTick.
TICK_FACTORS = (
    0xFFFcb933BD6FAD37AA2D162D1A594001,
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
)


def sqrt_price_at_tick(tick: int) -> int:
    """Exact integer port of Uniswap v4 TickMath.getSqrtPriceAtTick."""
    if not -MAX_TICK <= tick <= MAX_TICK:
        raise ValueError("tick outside v4 TickMath bounds")
    magnitude = abs(tick)
    ratio = 1 << 128
    for index, factor in enumerate(TICK_FACTORS):
        if magnitude & (1 << index):
            ratio = ratio * factor >> 128
    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio
    return (ratio + (1 << 32) - 1) >> 32


def traverses_interior(pre_sqrt: int, post_sqrt: int, lower_sqrt: int, upper_sqrt: int) -> bool:
    """Strict positive-length price overlap; boundary-only touch is excluded."""
    if not lower_sqrt < upper_sqrt:
        raise ValueError("invalid LP sqrt range")
    return max(min(pre_sqrt, post_sqrt), lower_sqrt) < min(max(pre_sqrt, post_sqrt), upper_sqrt)


def pool_screen(pool: dict, width: str = "1", seconds: int = 1800) -> dict:
    record = {"poolId": pool["poolId"], "token": pool["token"],
              "entryBlock": pool["entryBlock"], "entryTimestamp": pool["entryTimestamp"],
              "quoteAsset": pool["quoteAsset"], "widthInTickIntervals": int(width)}
    ranges = pool["quoteOnlyOutsideTickRanges"].get("widthInTickIntervals", {})
    ticks = ranges.get(width)
    if ticks is None:
        return {**record, "status": "no_legal_quote_only_range", "swapCount30m": 0,
                "interiorTraversalCount30m": 0, "possibleFeeSwapCount30m": 0}
    lower_tick, upper_tick = ticks["tickLower"], ticks["tickUpper"]
    lower_sqrt, upper_sqrt = sqrt_price_at_tick(lower_tick), sqrt_price_at_tick(upper_tick)
    pre_sqrt = int(pool["entrySqrtPriceX96"])
    pre_tick = int(pool["entryTick"])
    swaps = traversals = possible = stationary_inside = 0
    first_possible = None
    for event in pool["swapEvents"]:
        if event["block"] <= pool["entryBlock"]:
            continue
        elapsed = event["timestamp"] - pool["entryTimestamp"]
        if elapsed > seconds:
            break
        if elapsed < 0:
            raise ValueError("later block predates entry timestamp")
        post_sqrt = int(event["postSqrtPriceX96"])
        post_tick = int(event["postTick"])
        input_raw = max(0, -int(event["amount0CallerDeltaRaw"])) + max(0, -int(event["amount1CallerDeltaRaw"]))
        if input_raw > 0:
            swaps += 1
            interior = traverses_interior(pre_sqrt, post_sqrt, lower_sqrt, upper_sqrt)
            stationary = (pre_sqrt == post_sqrt and lower_tick <= pre_tick < upper_tick)
            if interior:
                traversals += 1
            if stationary:
                stationary_inside += 1
            if interior or stationary:
                possible += 1
                if first_possible is None:
                    first_possible = {"block": event["block"], "tx": event["tx"],
                                      "timestamp": event["timestamp"],
                                      "interiorTraversal": interior,
                                      "stationaryInside": stationary}
        pre_sqrt, pre_tick = post_sqrt, post_tick
    return {**record, "status": "screened", "tickLower": lower_tick, "tickUpper": upper_tick,
            "sqrtLowerX96": str(lower_sqrt), "sqrtUpperX96": str(upper_sqrt),
            "swapCount30m": swaps, "interiorTraversalCount30m": traversals,
            "stationaryInsideCount30m": stationary_inside,
            "possibleFeeSwapCount30m": possible, "firstPossibleFeeSwap": first_possible}


def main() -> int:
    assert sqrt_price_at_tick(0) == Q96
    assert sqrt_price_at_tick(-MAX_TICK) == 4_295_128_739
    assert sqrt_price_at_tick(MAX_TICK) == 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342
    source = json.loads(SOURCE.read_text())
    quote_pools = [pool for pool in source["pools"] if pool["quoteAsset"] is not None]
    rows = [pool_screen(pool) for pool in quote_pools]
    result = {
        "schemaVersion": 1, "source": str(SOURCE.relative_to(ROOT)),
        "chainId": 4663, "windowSeconds": 1800,
        "priceBoundaryRule": "strict positive-length overlap of observed pre/post sqrtPriceX96 path with exact TickMath [lower,upper] range; stationary in-range input is possible, boundary-only touch excluded",
        "scope": "passive historical price path only; not hypothetical LP fee or executable PnL",
        "count": {"quotePools": len(rows),
                  "legalRanges": sum(row["status"] == "screened" for row in rows),
                  "noLegalRange": sum(row["status"] != "screened" for row in rows),
                  "poolsWithInputSwap30m": sum(row["swapCount30m"] > 0 for row in rows),
                  "poolsWithInteriorTraversal30m": sum(row["interiorTraversalCount30m"] > 0 for row in rows),
                  "poolsWithPossibleFeeSwap30m": sum(row["possibleFeeSwapCount30m"] > 0 for row in rows)},
        "rows": rows,
    }
    OUTPUT.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(OUTPUT), "count": result["count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
