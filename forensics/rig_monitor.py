#!/usr/bin/env python3
"""Read-only Robinhood Chain Uniswap v4 pool-burst and liquidity-exit monitor.

Uses only public JSON-RPC methods. It never reads keys, signs, trades, or posts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
from fractions import Fraction
from itertools import groupby
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
MODIFY_LIQUIDITY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
QUOTES = {
    "0x0000000000000000000000000000000000000000",  # native ETH
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
}
WINDOW_SECONDS = 600
ENTRY_POOLS = 5
DEFAULT_EXIT_REMAINING = Fraction(3, 4)
COMPARISON_REMAINING = (
    Fraction(19, 20), Fraction(9, 10), Fraction(17, 20), Fraction(4, 5),
    Fraction(3, 4), Fraction(7, 10), Fraction(3, 5), Fraction(1, 2), Fraction(3, 10),
)
EXPLORER = "https://robinhoodchain.blockscout.com"


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.next_id = 1
        self.block_times: dict[int, int] = {}

    def _request(self, payload: object) -> object:
        request = Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "rig-monitor/1.0"},
        )
        for attempt in range(5):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise RuntimeError(f"RPC request failed: HTTP {exc.code}") from exc
                time.sleep(min(2**attempt, 8))
            except (URLError, TimeoutError) as exc:
                if attempt == 4:
                    raise RuntimeError(f"RPC request failed: {exc}") from exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError("RPC request failed after retries")

    def call(self, method: str, params: list) -> object:
        request_id = self.next_id
        self.next_id += 1
        result = self._request({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(f"RPC {method}: {result}")
        return result["result"]

    def latest_block(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_time(self, block: int) -> int:
        if block not in self.block_times:
            result = self.call("eth_getBlockByNumber", [hex(block), False])
            if result is None:
                raise RuntimeError(f"RPC returned no block {block}")
            self.block_times[block] = int(result["timestamp"], 16)
        return self.block_times[block]

    def load_block_times(self, blocks: set[int]) -> None:
        missing = sorted(blocks - self.block_times.keys())
        for offset in range(0, len(missing), 50):
            chunk = missing[offset : offset + 50]
            requests = []
            for block in chunk:
                request_id = self.next_id
                self.next_id += 1
                requests.append(
                    {"jsonrpc": "2.0", "id": request_id, "method": "eth_getBlockByNumber", "params": [hex(block), False]}
                )
            response = self._request(requests)
            if not isinstance(response, list):
                raise RuntimeError(f"RPC block batch: {response}")
            by_id = {item["id"]: item for item in response}
            for block, request in zip(chunk, requests):
                item = by_id.get(request["id"])
                if item is None or "error" in item or item.get("result") is None:
                    raise RuntimeError(f"RPC returned no block {block}: {item}")
                self.block_times[block] = int(item["result"]["timestamp"], 16)

    def logs(self, topic: str, start: int, end: int, pool_ids: list[str] | None = None) -> list[dict]:
        if start > end:
            return []
        output = []
        for first in range(start, end + 1, 20_000):
            last = min(first + 19_999, end)
            topics: list[object] = [topic]
            if pool_ids is not None:
                topics.append(pool_ids)
            output.extend(
                self.call(
                    "eth_getLogs",
                    [{"address": POOL_MANAGER, "topics": topics, "fromBlock": hex(first), "toBlock": hex(last)}],
                )
            )
        return output


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def emit(record: dict) -> None:
    print(json.dumps(record, separators=(",", ":")), flush=True)


def log_order(log: dict) -> tuple[int, int, int]:
    return tuple(int(log[field], 16) for field in ("blockNumber", "transactionIndex", "logIndex"))


def init_event(log: dict, timestamp: int) -> dict | None:
    topics = log["topics"]
    if len(topics) < 4 or len(log["data"]) < 66:
        raise ValueError("Malformed Initialize log")
    currency0 = ("0x" + topics[2][-40:]).lower()
    currency1 = ("0x" + topics[3][-40:]).lower()
    token = currency1 if currency0 in QUOTES else currency0 if currency1 in QUOTES else None
    if token is None or token in QUOTES:
        return None
    return {
        "token": token,
        "poolId": topics[1].lower(),
        "fee": int(log["data"][2:66], 16),
        "timestamp": timestamp,
        "block": int(log["blockNumber"], 16),
        "tx": log["transactionHash"],
    }


def liquidity_delta(log: dict) -> int:
    # ModifyLiquidity data = tickLower, tickUpper, liquidityDelta, salt.
    # The third ABI word is a signed int256. The local pounce.mjs reads the
    # first word and therefore cannot calculate liquidity exits correctly.
    data = log["data"][2:]
    if len(data) < 64 * 4:
        raise ValueError("Malformed ModifyLiquidity log")
    unsigned = int(data[128:192], 16)
    return unsigned - (1 << 256) if unsigned >= (1 << 255) else unsigned


def find_start_block(rpc: Rpc, latest: int, lookback_seconds: int) -> int:
    target = rpc.block_time(latest) - lookback_seconds
    low, high = 0, latest
    while low < high:
        mid = (low + high) // 2
        if rpc.block_time(mid) < target:
            low = mid + 1
        else:
            high = mid
    return low


class Monitor:
    def __init__(
        self, rpc: Rpc, token_filter: str | None = None,
        exit_remaining: Fraction = DEFAULT_EXIT_REMAINING, emit_statuses: bool = False,
        entry_pools: int = ENTRY_POOLS, entry_window_seconds: int = WINDOW_SECONDS,
        freeze_entry_pools: bool = False,
    ):
        self.rpc = rpc
        self.token_filter = token_filter.lower() if token_filter else None
        self.exit_remaining = exit_remaining
        self.emit_statuses = emit_statuses
        self.entry_pools = entry_pools
        self.entry_window_seconds = entry_window_seconds
        self.freeze_entry_pools = freeze_entry_pools
        self.windows: dict[str, deque[dict]] = defaultdict(deque)
        self.candidates: dict[str, dict] = {}

    def scan_initializations(self, start: int, end: int) -> None:
        logs = sorted(self.rpc.logs(INITIALIZE, start, end), key=log_order)
        self.rpc.load_block_times({int(log["blockNumber"], 16) for log in logs})
        for log in logs:
            event = init_event(log, self.rpc.block_times[int(log["blockNumber"], 16)])
            if event is None or (self.token_filter and event["token"] != self.token_filter):
                continue
            token = event["token"]
            window = self.windows[token]
            while window and event["timestamp"] - window[0]["timestamp"] > self.entry_window_seconds:
                window.popleft()
            if event["poolId"] not in {pool["poolId"] for pool in window}:
                window.append(event)
            candidate = self.candidates.get(token)
            if candidate is None and len(window) >= self.entry_pools and any(700_000 <= pool["fee"] < 1_000_000 for pool in window):
                candidate = {
                    "token": token,
                    "entry": event,
                    "firstTimestamp": window[0]["timestamp"],
                    "pools": {pool["poolId"]: pool for pool in window},
                    "poolCursor": {pool["poolId"]: pool["block"] - 1 for pool in window},
                    "poolTotals": {pool["poolId"]: {"added": 0, "removed": 0} for pool in window},
                    "liquidityEvents": 0,
                    "peakSideOutstanding": 0,
                    "peakSideBlock": None,
                    "currentSideOutstanding": 0,
                    "firstCrossings": {},
                    "exitAlerted": False,
                }
                self.candidates[token] = candidate
                emit(
                    {
                        "event": "ENTRY",
                        "token": token,
                        "time": iso(event["timestamp"]),
                        "block": event["block"],
                        "distinctPools10m": len(window),
                        "distinctPoolsWindow": len(window),
                        "entryWindowLimitSeconds": self.entry_window_seconds,
                        "windowSeconds": event["timestamp"] - window[0]["timestamp"],
                        "tx": EXPLORER + "/tx/" + event["tx"],
                    }
                )
            elif candidate is not None and not self.freeze_entry_pools and event["timestamp"] - candidate["firstTimestamp"] <= self.entry_window_seconds:
                if event["poolId"] not in candidate["pools"]:
                    candidate["pools"][event["poolId"]] = event
                    candidate["poolCursor"][event["poolId"]] = event["block"] - 1
                    candidate["poolTotals"][event["poolId"]] = {"added": 0, "removed": 0}

    @staticmethod
    def pool_groups(candidate: dict) -> dict[str, list[str]]:
        pools = sorted(candidate["pools"].values(), key=lambda pool: (pool["timestamp"], pool["block"]))
        # A first pool with a conventional <=1% fee is treated as the base
        # venue. This is a heuristic, not proof that the other pools share an
        # operator or that their liquidity belongs to a particular cluster.
        # If the first pool has a higher fee, include all pools.
        baseline = pools[0]["poolId"] if pools and pools[0]["fee"] <= 10_000 else None
        return {
            "allBurstPools": [pool["poolId"] for pool in pools],
            "sidePools": [pool["poolId"] for pool in pools if pool["poolId"] != baseline],
            "highFeePools": [pool["poolId"] for pool in pools if 700_000 <= pool["fee"] < 1_000_000],
        }

    @staticmethod
    def liquidity_totals(candidate: dict, pool_ids: list[str]) -> dict:
        added = sum(candidate["poolTotals"][pool_id]["added"] for pool_id in pool_ids)
        removed = sum(candidate["poolTotals"][pool_id]["removed"] for pool_id in pool_ids)
        return {
            "poolCount": len(pool_ids),
            "addedLiquidity": str(added),
            "removedLiquidity": str(removed),
            "removedOverAdded": round(removed / added, 6) if added else None,
            "outstandingLiquidity": str(added - removed),
        }

    def assess_exit(self, candidate: dict, block: int) -> None:
        side_ids = self.pool_groups(candidate)["sidePools"]
        totals = self.liquidity_totals(candidate, side_ids)
        current = int(totals["outstandingLiquidity"])
        candidate["currentSideOutstanding"] = current
        candidate["lastLiquidityEventBlock"] = block
        if self.emit_statuses:
            emit(
                {
                    "event": "LIQUIDITY_STATUS",
                    "token": candidate["token"],
                    "block": block,
                    "time": iso(self.rpc.block_time(block)),
                    "currentOutstanding": str(current),
                    "frozenSidePoolIds": side_ids,
                    "poolSetFrozenAtEntry": self.freeze_entry_pools,
                    "poolDefinition": "secondary-pool liquidity proxy; ownership unverified",
                }
            )
        if current > candidate["peakSideOutstanding"]:
            candidate["peakSideOutstanding"] = current
            candidate["peakSideBlock"] = block
        peak = candidate["peakSideOutstanding"]
        if peak <= 0 or block < candidate["entry"]["block"]:
            return
        for remaining in set(COMPARISON_REMAINING) | {self.exit_remaining}:
            key = str(round(float(remaining), 4))
            if key not in candidate["firstCrossings"] and current * remaining.denominator < peak * remaining.numerator:
                candidate["firstCrossings"][key] = {
                    "block": block,
                    "remainingFraction": round(current / peak, 6),
                    "peakOutstanding": str(peak),
                    "currentOutstanding": str(current),
                }
        if candidate["exitAlerted"] or current * self.exit_remaining.denominator >= peak * self.exit_remaining.numerator:
            return
        candidate["exitAlerted"] = True
        emit(
            {
                "event": "EXIT_LIQUIDITY_DRAWDOWN",
                "token": candidate["token"],
                "block": block,
                "time": iso(self.rpc.block_time(block)),
                "poolDefinition": "secondary-pool liquidity proxy; first burst pool excluded only if fee <=1%",
                "ownership": "unverified",
                "remainingThreshold": float(self.exit_remaining),
                "peakOutstanding": str(peak),
                "currentOutstanding": str(current),
                "remainingFraction": round(current / peak, 6),
            }
        )

    def scan_liquidity(self, end: int) -> None:
        for candidate in self.candidates.values():
            cursors = candidate["poolCursor"]
            if not cursors:
                continue
            first = min(cursors.values()) + 1
            if first > end:
                continue
            logs = sorted(self.rpc.logs(MODIFY_LIQUIDITY, first, end, list(cursors)), key=log_order)
            for block, block_logs in groupby(logs, key=lambda log: int(log["blockNumber"], 16)):
                changed = False
                for log in block_logs:
                    pool_id = log["topics"][1].lower()
                    if block <= cursors[pool_id]:
                        continue
                    delta = liquidity_delta(log)
                    if delta > 0:
                        candidate["poolTotals"][pool_id]["added"] += delta
                    elif delta < 0:
                        candidate["poolTotals"][pool_id]["removed"] -= delta
                    if delta:
                        changed = True
                        candidate["liquidityEvents"] += 1
                if changed:
                    self.assess_exit(candidate, block)
            for pool_id in cursors:
                cursors[pool_id] = end

    def summaries(self, end: int) -> None:
        if not self.candidates:
            emit({"event": "NO_MATCH", "asOfBlock": end, "rule": f"{self.entry_pools} distinct ETH/USDG v4 pools in {self.entry_window_seconds} seconds"})
            return
        for candidate in self.candidates.values():
            pools = sorted(candidate["pools"].values(), key=lambda x: (x["timestamp"], x["block"]))
            groups = self.pool_groups(candidate)
            crossings = candidate["firstCrossings"]
            self.rpc.load_block_times({item["block"] for item in crossings.values()})
            emit(
                {
                    "event": "SUMMARY",
                    "token": candidate["token"],
                    "asOfBlock": end,
                    "firstPoolTime": iso(candidate["firstTimestamp"]),
                    "entryTime": iso(candidate["entry"]["timestamp"]),
                    "ownership": "unverified",
                    "liquidityByPoolGroup": {
                        name: self.liquidity_totals(candidate, pool_ids) for name, pool_ids in groups.items()
                    },
                    "peakSideOutstanding": str(candidate["peakSideOutstanding"]),
                    "peakSideBlock": candidate["peakSideBlock"],
                    "currentSideOutstanding": str(candidate["currentSideOutstanding"]),
                    "firstCrossingsByRemainingFraction": {
                        key: {**item, "time": iso(self.rpc.block_times[item["block"]])}
                        for key, item in sorted(crossings.items(), reverse=True)
                    },
                    "liquidityEvents": candidate["liquidityEvents"],
                    "exitDrawdownAlertedOnSidePools": candidate["exitAlerted"],
                    "pools": [
                        {
                            "poolId": pool["poolId"],
                            "feePips": pool["fee"],
                            "addedLiquidity": str(candidate["poolTotals"][pool["poolId"]]["added"]),
                            "removedLiquidity": str(candidate["poolTotals"][pool["poolId"]]["removed"]),
                            "tx": EXPLORER + "/tx/" + pool["tx"],
                        }
                        for pool in pools
                    ],
                }
            )

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=RPC_URL, help="Robinhood Chain JSON-RPC URL")
    parser.add_argument("--from-block", type=int, help="historical replay start block, inclusive")
    parser.add_argument("--to-block", type=int, help="historical replay end block, inclusive")
    parser.add_argument("--watch-from-block", type=int, help="watch mode initial backfill block for an older token")
    parser.add_argument("--lookback-minutes", type=int, default=15, help="live startup lookback (default: 15)")
    parser.add_argument("--token", help="only report this token address")
    parser.add_argument("--watch", action="store_true", help="poll new blocks continuously")
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="watch polling interval (default: 3)")
    parser.add_argument("--exit-remaining", default="0.75", help="alert when side-pool outstanding liquidity falls below this fraction of its observed peak (default: 0.75)")
    parser.add_argument("--entry-pools", type=int, default=ENTRY_POOLS, help="distinct pools needed for ENTRY (default: 5)")
    parser.add_argument("--entry-window-seconds", type=int, default=WINDOW_SECONDS, help="rolling pool birth window (default: 600)")
    parser.add_argument("--freeze-entry-pools", action="store_true", help="track exit LP only in pools known at ENTRY")
    args = parser.parse_args()
    if (args.from_block is None) != (args.to_block is None):
        parser.error("--from-block and --to-block must be given together")
    if args.from_block is not None and (args.from_block < 0 or args.to_block < args.from_block):
        parser.error("invalid block range")
    if args.lookback_minutes < 10 or args.poll_seconds <= 0:
        parser.error("lookback must be at least 10 minutes and poll interval must be positive")
    if args.watch and args.to_block is not None:
        parser.error("--watch cannot be combined with a historical block range")
    if args.watch_from_block is not None and (not args.watch or args.watch_from_block < 0):
        parser.error("--watch-from-block requires --watch and a nonnegative block")
    if args.token and (not args.token.startswith("0x") or len(args.token) != 42):
        parser.error("--token must be a 20-byte 0x address")
    try:
        args.exit_remaining = Fraction(args.exit_remaining)
    except (ValueError, ZeroDivisionError):
        parser.error("--exit-remaining must be a number between 0 and 1")
    if not 0 < args.exit_remaining < 1:
        parser.error("--exit-remaining must be a number between 0 and 1")
    if not 1 <= args.entry_pools <= 20 or not 1 <= args.entry_window_seconds <= 3600:
        parser.error("entry pools must be 1..20 and entry window seconds 1..3600")
    return args


def main() -> int:
    args = parse_args()
    rpc = Rpc(args.rpc)
    monitor = Monitor(
        rpc, args.token, args.exit_remaining, emit_statuses=args.watch,
        entry_pools=args.entry_pools, entry_window_seconds=args.entry_window_seconds,
        freeze_entry_pools=args.freeze_entry_pools,
    )
    end = args.to_block if args.to_block is not None else rpc.latest_block()
    start = (
        args.watch_from_block if args.watch_from_block is not None
        else args.from_block if args.from_block is not None
        else find_start_block(rpc, end, args.lookback_minutes * 60)
    )
    monitor.scan_initializations(start, end)
    monitor.scan_liquidity(end)
    monitor.summaries(end)
    if not args.watch:
        return 0
    cursor = end
    while True:
        time.sleep(args.poll_seconds)
        try:
            latest = rpc.latest_block()
            if latest <= cursor:
                continue
            monitor.scan_initializations(cursor + 1, latest)
            monitor.scan_liquidity(latest)
            cursor = latest
            emit({"event": "HEARTBEAT", "time": iso(int(time.time())), "asOfBlock": cursor})
        except RuntimeError as error:
            print(f"rig-monitor: {error}; retrying from block {cursor + 1}", file=sys.stderr, flush=True)
            time.sleep(max(args.poll_seconds, 5.0))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (RuntimeError, ValueError) as error:
        print(f"rig-monitor: {error}", file=sys.stderr)
        sys.exit(1)
