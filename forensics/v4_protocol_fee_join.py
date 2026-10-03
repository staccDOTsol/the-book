#!/usr/bin/env python3
"""Join RH v4 high-fee Swap logs to preceding protocol-fee updates.

Read-only historical chain analysis. No wallet, signing, trading, or posting.
The output preserves block/transaction/log order so a fee update in the same
block is applied only to later swaps.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from rig_monitor import INITIALIZE, POOL_MANAGER, QUOTES, RPC_URL, Rpc, log_order


ROOT = Path(__file__).resolve().parents[1]
WINDOW = ROOT / ".local" / "threshold-matrix-24h.json"
OUTPUT = ROOT / ".local" / "v4-highfee-swap-fees-24h.json"
PROTOCOL_UPDATED = "0xe9c42593e71f84403b84352cd168d693e2c9fcd1fdbcc3feb21d92b43e6696f9"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
DENOMINATOR = 1_000_000


def signed(word: int, bits: int) -> int:
    word &= (1 << bits) - 1
    return word - (1 << bits) if word >= 1 << (bits - 1) else word


def words(data: str, minimum: int) -> list[int]:
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 2 + 64 * minimum:
        raise ValueError("malformed event data")
    return [int(data[index:index + 64], 16) for index in range(2, len(data), 64)]


def meta(log: dict) -> dict:
    return {
        "block": int(log["blockNumber"], 16),
        "transactionIndex": int(log["transactionIndex"], 16),
        "logIndex": int(log["logIndex"], 16),
        "tx": log["transactionHash"].lower(),
    }


def calculate_total_swap_fee(lp_pips: int, protocol_pips: int) -> int:
    """ProtocolFeeLibrary.calculateSwapFee, with its integer rounding."""
    return protocol_pips + lp_pips - protocol_pips * lp_pips // DENOMINATOR


def ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def join(rpc: Rpc, start: int, birth_end: int, follow_end: int) -> dict:
    births = rpc.call("eth_getLogs", [{
        "address": POOL_MANAGER, "topics": [INITIALIZE],
        "fromBlock": hex(start), "toBlock": hex(birth_end),
    }])
    pools = {}
    for log in births:
        fee, spacing, hooks, *_ = words(log["data"], 5)
        if not 700_000 <= fee < 1_000_000 or hooks != 0:
            continue
        pool = log["topics"][1].lower()
        currency0 = "0x" + log["topics"][2][-40:].lower()
        currency1 = "0x" + log["topics"][3][-40:].lower()
        pools[pool] = {
            "poolId": pool, "currency0": currency0, "currency1": currency1,
            "quotePair": currency0 in QUOTES or currency1 in QUOTES,
            "lpFeePips": fee, "tickSpacing": signed(spacing, 24),
            "hooks": "0x" + "0" * 40, "birth": meta(log),
        }
    ids = sorted(pools)
    if not ids:
        raise ValueError("no static high-fee no-hook pools")
    updates = []
    for first in range(start, follow_end + 1, 100_000):
        last = min(follow_end, first + 99_999)
        updates.extend(rpc.call("eth_getLogs", [{
            "address": POOL_MANAGER, "topics": [PROTOCOL_UPDATED, ids],
            "fromBlock": hex(first), "toBlock": hex(last),
        }]))
    swaps = []
    for first in range(start, follow_end + 1, 100_000):
        last = min(follow_end, first + 99_999)
        swaps.extend(rpc.call("eth_getLogs", [{
            "address": POOL_MANAGER, "topics": [SWAP, ids],
            "fromBlock": hex(first), "toBlock": hex(last),
        }]))
    stream = sorted(updates + swaps, key=log_order)
    protocol_by_pool = {pool: 0 for pool in pools}
    records = []
    mismatches = []
    updates_seen = set()
    for log in stream:
        pool = log["topics"][1].lower()
        if log["topics"][0].lower() == PROTOCOL_UPDATED:
            packed = words(log["data"], 1)[0]
            if packed >= 1 << 24:
                raise ValueError("protocol fee exceeds uint24")
            protocol_by_pool[pool] = packed
            updates_seen.add(pool)
            continue
        amount0, amount1, sqrt_price, active_liquidity, tick, event_fee = words(log["data"], 6)[:6]
        amount0, amount1 = signed(amount0, 128), signed(amount1, 128)
        if amount0 < 0 <= amount1:
            direction = "zeroForOne"
            input_currency = pools[pool]["currency0"]
            input_raw = -amount0
            protocol_pips = protocol_by_pool[pool] & 0xfff
        elif amount1 < 0 <= amount0:
            direction = "oneForZero"
            input_currency = pools[pool]["currency1"]
            input_raw = -amount1
            protocol_pips = (protocol_by_pool[pool] >> 12) & 0xfff
        elif amount0 == 0 and amount1 == 0:
            # A nonzero swap request can hit its price limit with no exchange.
            # Direction is absent from the event; both directions can be checked.
            direction = "no_exchange"
            input_currency = None
            input_raw = 0
            protocol_pips = None
        else:
            raise ValueError(f"unexpected v4 Swap sign at {log['transactionHash']}")
        p0 = protocol_by_pool[pool] & 0xfff
        p1 = (protocol_by_pool[pool] >> 12) & 0xfff
        expected_fees = (
            {calculate_total_swap_fee(pools[pool]["lpFeePips"], p0),
             calculate_total_swap_fee(pools[pool]["lpFeePips"], p1)}
            if direction == "no_exchange" else
            {calculate_total_swap_fee(pools[pool]["lpFeePips"], protocol_pips)}
        )
        if event_fee not in expected_fees:
            mismatches.append({"poolId": pool, **meta(log),
                               "eventFeePips": event_fee, "expectedFeePips": sorted(expected_fees)})
        if direction == "no_exchange":
            total_fee_floor = protocol_fee_ceiling = lp_fee_floor = 0
        elif event_fee == protocol_pips:
            lp_fee_floor = 0
            total_fee_floor = ceil_div(input_raw * event_fee, DENOMINATOR)
            protocol_fee_ceiling = total_fee_floor
        else:
            total_fee_floor = ceil_div(input_raw * event_fee, DENOMINATOR)
            protocol_fee_ceiling = input_raw * protocol_pips // DENOMINATOR
            lp_fee_floor = max(0, total_fee_floor - protocol_fee_ceiling)
        records.append({
            "poolId": pool, **meta(log), "direction": direction,
            "inputCurrency": input_currency, "inputRaw": str(input_raw),
            "amount0": str(amount0), "amount1": str(amount1),
            "sqrtPriceX96": str(sqrt_price),
            "activeLiquidityAfter": str(active_liquidity), "tickAfter": signed(tick, 24),
            "lpFeePips": pools[pool]["lpFeePips"],
            "protocolFeePips": protocol_pips,
            "totalSwapFeePips": event_fee,
            "totalFeeLowerBoundRaw": str(total_fee_floor),
            "protocolFeeUpperBoundRaw": str(protocol_fee_ceiling),
            "historicalLpFeeLowerBoundRaw": str(lp_fee_floor),
        })
    fee_counts = Counter(str(record["protocolFeePips"]) for record in records)
    return {
        "schemaVersion": 1, "chainId": 4663, "poolManager": POOL_MANAGER,
        "fromBlock": start, "toBirthBlock": birth_end, "followEndBlock": follow_end,
        "poolCriterion": "Initialize static LP fee 700000..999999 pips, hooks=0",
        "eventOrder": ["block", "transactionIndex", "logIndex"],
        "swapSign": "negative Swap amount is core input owed by caller",
        "feeRule": "Swap.fee is combined LP+protocol pips; protocol applies to input currency first",
        "feeBoundRule": "ceil(inputRaw*totalSwapFeePips/1e6)-floor(inputRaw*protocolFeePips/1e6) when LP fee positive; this bounds historical pool LP fee, not a hypothetical position's PnL",
        "count": {"pools": len(pools), "quotePools": sum(pool["quotePair"] for pool in pools.values()),
                  "poolsWithProtocolUpdate": len(updates_seen), "protocolUpdates": len(updates),
                  "poolsWithSwap": len({record["poolId"] for record in records}),
                  "swaps": len(records), "eventFeeFormulaMismatches": len(mismatches),
                  "swapsByProtocolFeePips": dict(fee_counts)},
        "pools": pools, "swaps": records, "feeFormulaMismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--follow-end-block", type=int,
                        help="continue swaps for the fixed 24h birth cohort through this block")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    window = json.loads(WINDOW.read_text())
    follow_end = args.follow_end_block or window["toBlock"]
    if follow_end < window["toBlock"]:
        raise ValueError("follow end precedes birth cohort end")
    result = join(Rpc(RPC_URL), window["fromBlock"], window["toBlock"], follow_end)
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({"output": str(args.output), "count": result["count"]}))
    return 1 if result["count"]["eventFeeFormulaMismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
