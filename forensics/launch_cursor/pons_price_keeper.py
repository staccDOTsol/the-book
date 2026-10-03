#!/usr/bin/env python3
"""Automated, size-aware Q-only price planner for Pons launches.

The keeper uses Pons's phase-0 sell formula or the Robinhood v4 Quoter for a
phase-2 X→ETH sale, then the v4 Quoter for ETH→Q. It never treats the guard's
marginal spot as an executable price. Live Q.configureOpen/processNext writes
are opt-in and are restricted to these two selectors.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass, asdict
import fcntl
import json
from math import isqrt
import os
from pathlib import Path
import sys
import time
from typing import Any

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account import Account
from eth_utils import keccak
import rlp

import pons_launch_watcher as watch


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
DEFAULT_STATE = LOCAL / "pons-price-keeper.json"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
ZERO = "0x" + "00" * 20
Q96 = 1 << 96
Q192 = 1 << 192
Q128 = 1 << 128
MAX_UINT128 = (1 << 128) - 1
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT = 4295128739
MAX_SQRT = 1461446703485210103287273052203988822378723970342

CONFIGURE = "0xb109c8d4"  # configureOpen(address,(uint160,uint128,uint128,int24,int24,int24,uint64))
PROCESS_NEXT = "0x4ba3eeaf"  # processNext()
PRICE_CONFIGURATOR = "0x315d563b"
EXECUTOR = "0xc34c08e5"
PRICE_GUARD = "0x36d8b0cc"
QUOTE_TOKEN = "0x217a4b70"
QUOTE_ETH_POOL_ID = "0x55d66a6c"
QUOTE_ETH_FEE = "0x33fe9dcd"
QUOTE_ETH_SPACING = "0x2dceba42"
STATE_VIEW = "0x4c4a3c25"
MEME_HOOK = "0x6651812c"
GET_LAUNCHED = "0x3cf28b5a"
GET_LIQUIDITY = "0xfa6793d5"
REFERENCE = "0x6e731efb"
VALIDATE = "0xc394bbe0"  # validate(address,uint160)
BALANCE_OF = "0x70a08231"
OPEN_CONFIGS = "0xb1a38d6c"
RESERVED_HARVESTED_QUOTE = "0x6a8604a8"
GET_RESERVES = "0x0902f1ac"
FEE_BPS = "0x24a9d853"
CREATOR_TAX_BPS = "0xc1bb8901"
QUOTE_EXACT_INPUT_SINGLE = "0xaa9d21cb"
DEPTH_GUARD = "0x" + keccak(text="depthGuard()")[:4].hex()
SETTLEMENT_ROUTER = "0x" + keccak(text="settlementRouter()")[:4].hex()
DEPTH_SOURCE = "0x" + keccak(text="source()")[:4].hex()
DEPTH_QUOTER = "0x" + keccak(text="quoter()")[:4].hex()
DEPTH_SAFETY_BPS = "0x" + keccak(text="safetyBps()")[:4].hex()

LAUNCH_TYPES = ["address", "address", "address", "address", "address", "uint256", "uint24",
                "int24", "uint16", "bool", "uint8", "uint256", "uint256", "uint256", "bool"]
CONFIG_TYPES = ["uint160", "uint128", "uint128", "int24", "int24", "int24", "uint64"]
TICK_RATIOS = (
    0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a,
    0xfff2e50f5f656932ef12357cf3c7fdcc, 0xffe5caca7e10e4e61c3624eaa0941cd0,
    0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
    0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053,
    0xfcbe86c7900a88aedcffc83b479aa3a4, 0xf987a7253ac413176f2b074cf7815e54,
    0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
    0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5,
    0x70d869a156d2a1b890bb3df62baf32f7, 0x31be135f97d08fd981231505542fcfa6,
    0x9aa508b5b7a84e1c677de54f3e99bc9, 0x5d6af8dedb81196699c329225ee604,
    0x2216e584f5fa1ea926041bedfe98, 0x48a170391f7dc42444e8fa2,
)


class KeeperError(Exception):
    pass


class WaitForPrice(KeeperError):
    pass


class UnsupportedLaunch(WaitForPrice):
    pass


class PinnedRpc:
    """Keep all planning eth_call and block timestamp reads at one block."""

    def __init__(self, upstream: watch.Rpc, block: int):
        self.upstream, self.block = upstream, block

    def call(self, method: str, params: list[Any]) -> Any:
        if method == "eth_call" and len(params) == 2 and params[1] == "latest":
            params = [params[0], hex(self.block)]
        elif method == "eth_getBlockByNumber" and params and params[0] == "latest":
            params = [hex(self.block), *params[1:]]
        return self.upstream.call(method, params)


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def sqrt_at_tick(tick: int) -> int:
    if not MIN_TICK <= tick <= MAX_TICK:
        raise KeeperError("tick outside v4 TickMath range")
    magnitude = abs(tick)
    ratio = 1 << 128
    for i, multiplier in enumerate(TICK_RATIOS):
        if magnitude & (1 << i):
            ratio = ratio * multiplier >> 128
    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio
    return (ratio >> 32) + (1 if ratio & ((1 << 32) - 1) else 0)


def floor_tick(sqrt_price: int) -> int:
    if not MIN_SQRT <= sqrt_price < MAX_SQRT:
        raise WaitForPrice("price outside v4 TickMath range")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sqrt_at_tick(mid) <= sqrt_price:
            lo = mid
        else:
            hi = mid - 1
    return lo


def align_down(value: int, spacing: int) -> int:
    return value // spacing * spacing


def align_up(value: int, spacing: int) -> int:
    return -((-value) // spacing) * spacing


def ceil_sqrt_ratio(numerator: int, denominator: int) -> int:
    value = isqrt(ceil_div(numerator, denominator))
    return value if value * value * denominator >= numerator else value + 1


def amount0_ceil(liquidity: int, lower_sqrt: int, upper_sqrt: int) -> int:
    return ceil_div(ceil_div((liquidity << 96) * (upper_sqrt - lower_sqrt), upper_sqrt), lower_sqrt)


def amount1_ceil(liquidity: int, lower_sqrt: int, upper_sqrt: int) -> int:
    return ceil_div(liquidity * (upper_sqrt - lower_sqrt), Q96)


def required_q_at_first_buy(max_x: int, boundary_sqrt: int, quote_is_0: bool) -> int:
    """Match OpenExecutableDepthGuard's conservative Q128 price rounding."""
    ratio_x128 = boundary_sqrt * boundary_sqrt // (1 << 64)
    if ratio_x128 == 0:
        raise WaitForPrice("boundary price is too small")
    return (ceil_div(max_x * Q128, ratio_x128) if quote_is_0 else
            ceil_div(max_x * (ratio_x128 + 1), Q128))


def liquidity_for_budget(budget: int, lower_sqrt: int, upper_sqrt: int, quote_is_0: bool) -> int:
    amount = amount0_ceil if quote_is_0 else amount1_ceil
    lo, hi = 0, MAX_UINT128
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if amount(mid, lower_sqrt, upper_sqrt) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


@dataclass(frozen=True)
class PlanSettings:
    budget_bps: int = 2500
    utilization_bps: int = 9500
    safety_bps: int = 1500
    quote_size_multiple: int = 2
    tick_spacing: int = 60
    band_width_ticks: int = 1200
    gap_ticks: int = 60
    ttl_seconds: int = 120
    max_snapshot_age_seconds: int = 20

    def validate(self) -> None:
        if (not 1 <= self.budget_bps <= 10000 or not 1 <= self.utilization_bps <= 10000 or
                not 1 <= self.safety_bps < 10000 or not 1 <= self.quote_size_multiple <= 10 or
                not 1 <= self.tick_spacing <= 32767 or not 1 <= self.band_width_ticks <= 100000 or
                not 1 <= self.gap_ticks <= 10000 or not 30 <= self.ttl_seconds <= 3600 or
                not 1 <= self.max_snapshot_age_seconds <= 120):
            raise KeeperError("invalid planner setting")


@dataclass(frozen=True)
class OpenPlan:
    token: str
    starting_sqrt_price_x96: int
    liquidity: int
    max_quote_in: int
    tick_spacing: int
    tick_lower: int
    tick_upper: int
    deadline: int
    sample_x_in: int
    executable_q_out: int
    safe_q_out: int
    quote_is_0: bool

    def abi_config(self) -> tuple[int, int, int, int, int, int, int]:
        return (self.starting_sqrt_price_x96, self.liquidity, self.max_quote_in,
                self.tick_spacing, self.tick_lower, self.tick_upper, self.deadline)


def plan_position(token: str, q: str, reference_sqrt: int, idle_q: int, x_to_q_quote: Any,
                  block_time: int, settings: PlanSettings) -> OpenPlan:
    """Build a Q-only band against the executable sale of its full X exposure."""
    settings.validate()
    token, q = watch.address(token), watch.address(q)
    if token == q or not MIN_SQRT <= reference_sqrt < MAX_SQRT:
        raise WaitForPrice("invalid token or guard reference")
    budget = min(idle_q * settings.budget_bps // 10000, MAX_UINT128)
    if budget == 0:
        raise WaitForPrice("vault has no budgeted idle Q")
    quote_is_0 = int(q, 16) < int(token, 16)
    spot_x = (ceil_div(budget * reference_sqrt * reference_sqrt, Q192) if quote_is_0
              else ceil_div(budget * Q192, reference_sqrt * reference_sqrt))
    sample_x = spot_x * settings.quote_size_multiple
    reference_tick = floor_tick(reference_sqrt)
    width = align_up(settings.band_width_ticks, settings.tick_spacing)
    target = budget * settings.utilization_bps // 10000
    # Moving the band farther from spot can increase the X bought by the LP.
    # Requote that *whole* inventory and widen again until its own sale quote
    # supports the band. A depth curve with no fixed point must be skipped.
    for _ in range(12):
        if not 0 < sample_x <= MAX_UINT128:
            raise WaitForPrice("full X exposure is outside uint128 quote range")
        executable_q = int(x_to_q_quote(sample_x))
        safe_q = executable_q * (10000 - settings.safety_bps) // 10000
        if safe_q == 0:
            raise WaitForPrice("executable sale returns too little Q")
        if quote_is_0:
            # Q is currency0 and the sole LP asset below tickLower.
            boundary_sqrt = ceil_sqrt_ratio(sample_x * Q192, safe_q)
            if boundary_sqrt >= MAX_SQRT:
                raise WaitForPrice("sale floor exceeds tick range")
            boundary_tick = floor_tick(max(MIN_SQRT, boundary_sqrt))
            if sqrt_at_tick(boundary_tick) < boundary_sqrt:
                boundary_tick += 1
            lower = align_up(max(reference_tick, boundary_tick) + settings.gap_ticks,
                             settings.tick_spacing)
            upper = lower + width
        else:
            # Q is currency1 and the sole LP asset above tickUpper.
            boundary_sqrt = isqrt(safe_q * Q192 // sample_x)
            if boundary_sqrt < MIN_SQRT:
                raise WaitForPrice("sale floor is below tick range")
            boundary_tick = floor_tick(min(boundary_sqrt, MAX_SQRT - 1))
            upper = align_down(min(reference_tick, boundary_tick) - settings.gap_ticks,
                               settings.tick_spacing)
            lower = upper - width
        if lower < MIN_TICK or upper > MAX_TICK or lower >= upper:
            raise WaitForPrice("Q-only band is outside tick range")
        lower_sqrt, upper_sqrt = sqrt_at_tick(lower), sqrt_at_tick(upper)
        if not (reference_sqrt <= lower_sqrt if quote_is_0 else reference_sqrt >= upper_sqrt):
            raise KeeperError("planner violated the executor's Q-only condition")
        liquidity = liquidity_for_budget(target, lower_sqrt, upper_sqrt, quote_is_0)
        if liquidity == 0:
            raise WaitForPrice("Q budget cannot mint positive liquidity")
        spent_q = (amount0_ceil(liquidity, lower_sqrt, upper_sqrt) if quote_is_0
                   else amount1_ceil(liquidity, lower_sqrt, upper_sqrt))
        if spent_q > target or target > budget:
            raise KeeperError("liquidity calculation exceeded Q budget")
        max_x = (amount1_ceil(liquidity, lower_sqrt, upper_sqrt) if quote_is_0
                 else amount0_ceil(liquidity, lower_sqrt, upper_sqrt))
        if not 0 < max_x <= MAX_UINT128:
            raise WaitForPrice("full X exposure is outside uint128 quote range")
        full_quote = int(x_to_q_quote(max_x))
        full_safe_q = full_quote * (10000 - settings.safety_bps) // 10000
        if full_safe_q == 0:
            raise WaitForPrice("full X exposure sale returns too little Q")
        safe_floor = full_safe_q >= required_q_at_first_buy(
            max_x, lower_sqrt if quote_is_0 else upper_sqrt, quote_is_0)
        if safe_floor:
            return OpenPlan(token, reference_sqrt, liquidity, budget, settings.tick_spacing,
                            lower, upper, block_time + settings.ttl_seconds, max_x,
                            full_quote, full_safe_q, quote_is_0)
        if max_x == sample_x:
            raise WaitForPrice("full X exposure sale cannot support Q-only band")
        sample_x = max_x
    raise WaitForPrice("full X exposure sale did not converge")


def call_abi(rpc: watch.Rpc, to: str, selector_hex: str, inputs: list[str] | None = None,
             values: list[Any] | None = None, outputs: list[str] | None = None,
             block: str = "latest") -> tuple[Any, ...]:
    payload = selector_hex + (encode(inputs, values).hex() if inputs else "")
    raw = rpc.call("eth_call", [{"to": to, "data": payload}, block])
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise KeeperError("eth_call returned malformed ABI data")
    try:
        return decode(outputs or [], bytes.fromhex(raw[2:]))
    except (ValueError, TypeError, DecodingError) as exc:
        raise KeeperError("eth_call returned undecodable ABI data") from exc


def read_address(rpc: watch.Rpc, contract: str, method: str) -> str:
    return watch.address(call_abi(rpc, contract, method, outputs=["address"])[0])


def read_uint(rpc: watch.Rpc, contract: str, method: str, output_type: str = "uint256") -> int:
    return int(call_abi(rpc, contract, method, outputs=[output_type])[0])


@dataclass(frozen=True)
class Bindings:
    q: str
    executor: str
    guard: str
    state_view: str
    quote_pool_id: str
    quote_fee: int
    quote_spacing: int
    pons_hook: str
    quoter: str
    depth_guard: str = ZERO


def verify_bindings(rpc: watch.Rpc, chain_id: int, q: str, guard: str, quoter: str,
                    configurator: str | None, safety_bps: int | None = 1500) -> Bindings:
    q, guard, quoter = watch.address(q), watch.address(guard), watch.address(quoter)
    if watch.quantity(rpc.call("eth_chainId", []), "chain ID") != chain_id:
        raise KeeperError("RPC chain ID differs from configured chain ID")
    executor = read_address(rpc, q, EXECUTOR)
    for name, contract in (("Q", q), ("executor", executor), ("guard", guard), ("v4 Quoter", quoter)):
        code = rpc.call("eth_getCode", [contract, "latest"])
        if not isinstance(code, str) or code == "0x":
            raise KeeperError(f"{name} has no contract code")
    if read_address(rpc, q, watch.PONS_FACTORY_GETTER) != watch.PONS_FACTORY:
        raise KeeperError("Q has a different Pons factory")
    if read_address(rpc, executor, PRICE_GUARD) != guard:
        raise KeeperError("executor has a different price guard")
    if read_address(rpc, guard, QUOTE_TOKEN) != q or read_address(rpc, guard, watch.PONS_FACTORY_GETTER) != watch.PONS_FACTORY:
        raise KeeperError("guard Q/factory binding mismatch")
    if configurator is not None and read_address(rpc, q, PRICE_CONFIGURATOR) != configurator:
        raise KeeperError("signing account is not Q.priceConfigurator")
    state_view = read_address(rpc, guard, STATE_VIEW)
    state_view_code = rpc.call("eth_getCode", [state_view, "latest"])
    if not isinstance(state_view_code, str) or state_view_code == "0x":
        raise KeeperError("guard StateView has no contract code")
    raw_id = rpc.call("eth_call", [{"to": guard, "data": QUOTE_ETH_POOL_ID}, "latest"])
    if not isinstance(raw_id, str) or not watch.HASH.fullmatch(raw_id):
        raise KeeperError("guard returned invalid Q/ETH pool ID")
    fee = read_uint(rpc, guard, QUOTE_ETH_FEE, "uint24")
    spacing = read_uint(rpc, guard, QUOTE_ETH_SPACING, "int24")
    if fee <= 0 or spacing <= 0:
        raise KeeperError("guard Q/ETH pool settings are invalid")
    pons_hook = read_address(rpc, watch.PONS_FACTORY, MEME_HOOK)
    depth_guard = read_address(rpc, executor, DEPTH_GUARD)
    router = read_address(rpc, executor, SETTLEMENT_ROUTER)
    if depth_guard == ZERO or router == ZERO:
        raise KeeperError("executor has no bound depth guard or settlement router")
    for name, contract in (("depth guard", depth_guard), ("settlement router", router)):
        code = rpc.call("eth_getCode", [contract, "latest"])
        if not isinstance(code, str) or code == "0x":
            raise KeeperError(f"{name} has no contract code")
    if (read_address(rpc, depth_guard, DEPTH_SOURCE) != executor or
            read_address(rpc, depth_guard, PRICE_GUARD) != guard or
            read_address(rpc, depth_guard, SETTLEMENT_ROUTER) != router or
            read_address(rpc, depth_guard, QUOTE_TOKEN) != q or
            read_address(rpc, depth_guard, watch.PONS_FACTORY_GETTER) != watch.PONS_FACTORY or
            read_address(rpc, depth_guard, DEPTH_QUOTER) != quoter):
        raise KeeperError("bound depth guard configuration mismatch")
    if safety_bps is not None and read_uint(rpc, depth_guard, DEPTH_SAFETY_BPS, "uint16") != safety_bps:
        raise KeeperError("depth guard haircut differs from planner safety bps")
    return Bindings(q, executor, guard, state_view, raw_id.lower(), fee, spacing,
                    pons_hook, quoter, depth_guard)


def read_launch(rpc: watch.Rpc, token: str) -> tuple[Any, ...]:
    launch = call_abi(rpc, watch.PONS_FACTORY, GET_LAUNCHED, ["address"], [token], LAUNCH_TYPES)
    if not launch[14] or watch.address(launch[0]) != token or watch.address(launch[1]) == ZERO:
        raise WaitForPrice("factory launch record is unavailable")
    return launch


def q_launch_state(rpc: watch.Rpc, q: str, token: str, block: int | str = "latest") -> tuple[int, bool]:
    raw = rpc.call("eth_call", [{"to": q, "data": watch.LAUNCHES + token[2:].rjust(64, "0")},
                                hex(block) if isinstance(block, int) else block])
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 130:
        raise KeeperError("Q.launches returned malformed data")
    try:
        stage = int(raw[2:66], 16)
        configured = int(raw[66:130], 16)
    except ValueError as exc:
        raise KeeperError("Q.launches returned malformed data") from exc
    if stage > 5 or configured > 1:
        raise KeeperError("Q.launches returned invalid stage/configured flag")
    return stage, bool(configured)


def q_next_action_at(rpc: watch.Rpc, q: str, token: str) -> int:
    raw = rpc.call("eth_call", [{"to": q, "data": watch.LAUNCHES + token[2:].rjust(64, "0")}, "latest"])
    if not isinstance(raw, str) or len(raw) < 2 + 64 * 6:
        raise KeeperError("Q.launches has no nextActionAt field")
    try:
        return int(raw[2 + 64 * 5:2 + 64 * 6], 16)
    except ValueError as exc:
        raise KeeperError("Q.launches nextActionAt is malformed") from exc


def q_vault_balance(rpc: watch.Rpc, bindings: Bindings) -> int:
    balance = int(call_abi(rpc, bindings.q, BALANCE_OF,
                           ["address"], [bindings.executor], ["uint256"])[0])
    reserved = read_uint(rpc, bindings.executor, RESERVED_HARVESTED_QUOTE)
    if reserved > balance:
        raise KeeperError("executor reserved harvested Q exceeds its Q balance")
    return balance - reserved


def latest_block_time(rpc: watch.Rpc) -> int:
    block = rpc.call("eth_getBlockByNumber", ["latest", False])
    if not isinstance(block, dict):
        raise KeeperError("latest block is missing")
    return watch.quantity(block.get("timestamp"), "block timestamp")


def quote_eth_liquidity(rpc: watch.Rpc, bindings: Bindings) -> int:
    return int(call_abi(rpc, bindings.state_view, GET_LIQUIDITY,
                        ["bytes32"], [bytes.fromhex(bindings.quote_pool_id[2:])], ["uint128"])[0])


class ExecutableQuoteProvider:
    """Size-aware X→ETH→Q route. Quoter failures keep a token waiting."""

    def __init__(self, rpc: watch.Rpc, bindings: Bindings):
        self.rpc, self.bindings = rpc, bindings

    def at_block(self, block: int) -> "ExecutableQuoteProvider":
        return ExecutableQuoteProvider(PinnedRpc(self.rpc, block), self.bindings)

    def quote_single(self, key: tuple[str, str, int, int, str], zero_for_one: bool,
                     amount_in: int) -> int:
        if not 0 < amount_in <= MAX_UINT128:
            raise WaitForPrice("v4 quote amount outside uint128")
        try:
            result = call_abi(self.rpc, self.bindings.quoter, QUOTE_EXACT_INPUT_SINGLE,
                              ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
                              [(key, zero_for_one, amount_in, b"")], ["uint256", "uint256"])
        except (watch.WatcherError, KeeperError) as exc:
            raise WaitForPrice("v4 executable quote unavailable") from exc
        amount_out = int(result[0])
        if amount_out <= 0:
            raise WaitForPrice("v4 executable quote returned zero")
        return amount_out

    def quote_x_to_q(self, token: str, launch: tuple[Any, ...], x_in: int) -> int:
        phase = int(launch[10])
        if phase == 0:
            curve = watch.address(launch[1])
            try:
                quote_reserve, token_reserve = call_abi(self.rpc, curve, GET_RESERVES,
                                                       outputs=["uint256", "uint256"])
                fee = read_uint(self.rpc, curve, FEE_BPS, "uint256")
                tax = read_uint(self.rpc, curve, CREATOR_TAX_BPS, "uint256")
            except (watch.WatcherError, KeeperError) as exc:
                raise WaitForPrice("active Pons curve quote unavailable") from exc
            if quote_reserve <= 0 or token_reserve <= 0 or fee + tax >= 10000:
                raise WaitForPrice("active Pons curve has invalid reserves or fees")
            scaled = x_in * 10000
            gross = scaled * quote_reserve // (token_reserve * 10000 + scaled)
            eth_out = gross - gross * fee // 10000 - gross * tax // 10000
        elif phase == 2:
            key = (ZERO, token, int(launch[6]), int(launch[7]), self.bindings.pons_hook)
            eth_out = self.quote_single(key, False, x_in)
        else:
            raise WaitForPrice("Pons launch is not in tradable phase 0 or 2")
        if eth_out <= 0 or eth_out > MAX_UINT128:
            raise WaitForPrice("Pons X→ETH quote is zero or outside uint128")
        quote_key = (ZERO, self.bindings.q, self.bindings.quote_fee,
                     self.bindings.quote_spacing, ZERO)
        return self.quote_single(quote_key, True, eth_out)


def guard_reference(rpc: watch.Rpc, bindings: Bindings, token: str) -> int:
    try:
        reference = int(call_abi(rpc, bindings.guard, REFERENCE,
                                 ["address"], [token], ["uint160"])[0])
        # Guard's own spot bound remains an independent condition on the
        # proposed pool initialization price. The executor repeats this at mint.
        call_abi(rpc, bindings.guard, VALIDATE,
                 ["address", "uint160"], [token, reference], ["uint160"])
        return reference
    except (watch.WatcherError, KeeperError) as exc:
        raise WaitForPrice("guard reference or spot validation unavailable") from exc


@dataclass
class KeeperState:
    chain_id: int
    q: str
    guard: str
    last_block: int
    last_hash: str
    tokens: list[str]
    pending_tx: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return {"version": 1, "chainId": self.chain_id, "factory": watch.PONS_FACTORY,
                "q": self.q, "guard": self.guard, "lastBlock": self.last_block,
                "lastHash": self.last_hash, "tokens": self.tokens, "pendingTx": self.pending_tx}


class KeeperStore:
    def __init__(self, path: Path):
        self.path, self.lock = path, None
        if path.resolve().parent != LOCAL.resolve():
            raise KeeperError("keeper state must be directly inside repository .local")

    def __enter__(self) -> "KeeperStore":
        LOCAL.mkdir(mode=0o700, exist_ok=True)
        if LOCAL.is_symlink():
            raise KeeperError(".local must not be a symlink")
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock = open(lock_path, "a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise KeeperError("another price keeper holds the state lock") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.lock is not None:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()

    def load(self, rpc: watch.Rpc, chain_id: int, q: str, guard: str,
             start_block: int | None) -> KeeperState:
        if not self.path.exists():
            if start_block is None or start_block < 1:
                raise KeeperError("first run requires --start-block >= 1")
            state = KeeperState(chain_id, q, guard, start_block - 1,
                                watch.block_hash(rpc, start_block - 1), [])
            self.save(state)
            return state
        if self.path.is_symlink():
            raise KeeperError("keeper state must not be a symlink")
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise KeeperError("keeper state is unreadable or corrupt") from exc
        if (not isinstance(data, dict) or data.get("version") != 1 or
                data.get("chainId") != chain_id or data.get("factory") != watch.PONS_FACTORY or
                data.get("q") != q or data.get("guard") != guard):
            raise KeeperError("keeper state is bound to another chain, Q, or guard")
        block, block_hash, tokens, pending = (data.get("lastBlock"), data.get("lastHash"),
                                              data.get("tokens"), data.get("pendingTx"))
        if (not isinstance(block, int) or block < 0 or not isinstance(block_hash, str) or
                not watch.HASH.fullmatch(block_hash) or not isinstance(tokens, list) or
                any(not isinstance(t, str) or not watch.ADDRESS.fullmatch(t) for t in tokens) or
                len(tokens) != len(set(tokens)) or
                (pending is not None and not isinstance(pending, dict))):
            raise KeeperError("keeper state fields are malformed")
        if start_block is not None and start_block != block + 1:
            raise KeeperError("--start-block conflicts with saved keeper cursor")
        return KeeperState(chain_id, q, guard, block, block_hash.lower(), tokens, pending)

    def save(self, state: KeeperState) -> None:
        staging = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.new")
        try:
            with open(staging, "x", encoding="utf-8") as file:
                os.chmod(staging, 0o600)
                json.dump(state.json(), file, separators=(",", ":"))
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(staging, self.path)
            directory = os.open(LOCAL, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staging.unlink(missing_ok=True)


class ConfiguratorLock:
    """Serialize one Q configurator key and preserve an unsent-tx owner."""

    def __init__(self, q: str, state_path: Path):
        self.path = LOCAL / f"pons-configurator-{watch.address(q)[2:]}.lock"
        if state_path.resolve().parent != LOCAL.resolve():
            raise KeeperError("configurator state must be directly inside .local")
        self.state_path = state_path.resolve()
        self.file = None

    @staticmethod
    def _has_pending(state_path: Path) -> bool:
        if not state_path.exists():
            return False
        if state_path.is_symlink():
            raise KeeperError("configurator state must not be a symlink")
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError) as exc:
            raise KeeperError("previous configurator state is unreadable") from exc
        if not isinstance(state, dict) or "pendingTx" not in state:
            raise KeeperError("previous configurator state is malformed")
        return state["pendingTx"] is not None

    def _write_owner(self, owner: str) -> None:
        assert self.file is not None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(owner)
        self.file.flush()
        os.fsync(self.file.fileno())

    def __enter__(self) -> "ConfiguratorLock":
        LOCAL.mkdir(mode=0o700, exist_ok=True)
        if LOCAL.is_symlink() or self.path.is_symlink():
            raise KeeperError("configurator lock path must not be a symlink")
        self.file = open(self.path, "a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            raise KeeperError("another live Q configurator keeper is running") from exc
        try:
            self.file.seek(0)
            previous = self.file.read().strip()
            if previous:
                previous_path = Path(previous)
                if previous_path.parent != LOCAL.resolve():
                    raise KeeperError("configurator lock contains an invalid state path")
                if previous_path != self.state_path and self._has_pending(previous_path):
                    raise KeeperError("another keeper has an unresolved signed configurator transaction")
            self._write_owner(str(self.state_path))
        except BaseException:
            self.file.close()
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        if self.file is not None:
            try:
                if not self._has_pending(self.state_path):
                    self._write_owner("")
            finally:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                self.file.close()


def discover_launches(rpc: watch.Rpc, state: KeeperState, store: KeeperStore,
                      confirmations: int, block_span: int) -> int:
    if watch.block_hash(rpc, state.last_block) != state.last_hash:
        raise KeeperError("keeper cursor block hash changed; reconcile the reorg")
    safe_head = watch.chain_head(rpc) - confirmations
    count = 0
    known = set(state.tokens)
    while state.last_block < safe_head:
        first, last = state.last_block + 1, min(safe_head, state.last_block + block_span)
        expected_hash = watch.block_hash(rpc, last)
        logs = rpc.call("eth_getLogs", [{"address": watch.PONS_FACTORY,
                                         "topics": [watch.TOKEN_LAUNCHED],
                                         "fromBlock": hex(first), "toBlock": hex(last)}])
        if not isinstance(logs, list) or watch.block_hash(rpc, last) != expected_hash:
            raise KeeperError("confirmed launch range changed during backfill")
        for log in sorted(logs, key=watch.log_order):
            number = watch.quantity(log.get("blockNumber"), "launch block")
            if not first <= number <= last or str(log.get("blockHash", "")).lower() != watch.block_hash(rpc, number):
                raise KeeperError("noncanonical or out-of-range launch log")
            token = watch.token_from_log(log, watch.PONS_FACTORY)
            if token not in known:
                state.tokens.append(token)
                known.add(token)
                count += 1
        if watch.block_hash(rpc, last) != expected_hash:
            raise KeeperError("confirmed launch range changed during backfill")
        state.last_block, state.last_hash = last, expected_hash
        store.save(state)
    return count


def existing_config(rpc: watch.Rpc, executor: str, token: str) -> tuple[int, ...]:
    return tuple(int(value) for value in call_abi(rpc, executor, OPEN_CONFIGS,
                                                  ["address"], [token], CONFIG_TYPES))


def existing_config_safe(rpc: watch.Rpc, bindings: Bindings, plan: OpenPlan,
                         existing: tuple[int, ...], now: int) -> bool:
    sqrt_price, liquidity, max_q, spacing, lower, upper, deadline = existing
    if (liquidity != plan.liquidity or max_q <= 0 or max_q > plan.max_quote_in or
            spacing != plan.tick_spacing or lower != plan.tick_lower or
            upper != plan.tick_upper or lower >= upper or
            lower < MIN_TICK or upper > MAX_TICK or lower % spacing or upper % spacing or
            deadline < now + 30):
        return False
    try:
        lower_sqrt, upper_sqrt = sqrt_at_tick(lower), sqrt_at_tick(upper)
        call_abi(rpc, bindings.guard, VALIDATE, ["address", "uint160"],
                 [plan.token, sqrt_price], ["uint160"])
    except (KeeperError, watch.WatcherError):
        return False
    if plan.quote_is_0:
        if (plan.starting_sqrt_price_x96 > lower_sqrt or
                plan.safe_q_out < required_q_at_first_buy(plan.sample_x_in,
                                                            lower_sqrt, True)):
            return False
        amount = amount0_ceil(liquidity, lower_sqrt, upper_sqrt)
    else:
        if (plan.starting_sqrt_price_x96 < upper_sqrt or
                plan.safe_q_out < required_q_at_first_buy(plan.sample_x_in,
                                                            upper_sqrt, False)):
            return False
        amount = amount1_ceil(liquidity, lower_sqrt, upper_sqrt)
    return amount <= max_q <= plan.max_quote_in


def make_plan(rpc: watch.Rpc, bindings: Bindings, token: str,
              settings: PlanSettings, quotes: ExecutableQuoteProvider) -> OpenPlan:
    anchor_head = watch.chain_head(rpc)
    anchor_hash = watch.block_hash(rpc, anchor_head)
    snapshot = PinnedRpc(rpc, anchor_head)
    snapshot_quotes = quotes.at_block(anchor_head) if hasattr(quotes, "at_block") else quotes
    launch = read_launch(snapshot, token)
    if watch.address(launch[4]) != ZERO:
        raise UnsupportedLaunch("Pons launch is not paired with native ETH")
    if int(launch[10]) == 1:
        raise WaitForPrice("Pons launch is in swept phase 1")
    if int(launch[10]) not in (0, 2):
        raise UnsupportedLaunch("Pons launch is no longer in phase 0 or 2")
    if quote_eth_liquidity(snapshot, bindings) <= 0:
        raise WaitForPrice("Q/ETH pool has no liquidity")
    reference = guard_reference(snapshot, bindings, token)
    balance = q_vault_balance(snapshot, bindings)
    snapshot_time = latest_block_time(snapshot)
    plan = plan_position(token, bindings.q, reference, balance,
                         lambda x: snapshot_quotes.quote_x_to_q(token, launch, x),
                         snapshot_time, settings)
    if watch.block_hash(rpc, anchor_head) != anchor_hash:
        raise WaitForPrice("pinned price block changed during planning")
    if latest_block_time(rpc) - snapshot_time > settings.max_snapshot_age_seconds:
        raise WaitForPrice("pinned price snapshot became stale during planning")
    return plan


class TransactionReverted(KeeperError):
    pass


def confirmed_receipt(rpc: watch.Rpc, tx_hash: str, confirmations: int) -> bool:
    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return False
    if not isinstance(receipt, dict):
        raise KeeperError("keeper transaction receipt is malformed")
    block = watch.quantity(receipt.get("blockNumber"), "receipt block")
    if str(receipt.get("blockHash", "")).lower() != watch.block_hash(rpc, block):
        raise KeeperError("keeper transaction receipt is not canonical")
    if watch.chain_head(rpc) < block + confirmations:
        return False
    status = watch.quantity(receipt.get("status"), "receipt status")
    if status == 0:
        raise TransactionReverted("keeper transaction reverted")
    if status != 1:
        raise KeeperError("keeper transaction receipt has unknown status")
    return True


class KeeperSigner:
    def __init__(self, rpc: watch.Rpc, bindings: Bindings, chain_id: int, private_key: str,
                 confirmations: int, max_config_gas: int, max_process_gas: int,
                 max_fee_wei: int, max_priority_wei: int, receipt_timeout: int,
                 poll_seconds: float):
        self.account = Account.from_key(private_key)
        self.rpc, self.bindings, self.chain_id = rpc, bindings, chain_id
        self.confirmations = confirmations
        self.max_config_gas, self.max_process_gas = max_config_gas, max_process_gas
        self.max_fee_wei, self.max_priority_wei = max_fee_wei, max_priority_wei
        self.receipt_timeout, self.poll_seconds = receipt_timeout, poll_seconds

    @property
    def signer(self) -> str:
        return self.account.address.lower()

    def _gas_cap(self, kind: str) -> int:
        return self.max_config_gas if kind == "configure" else self.max_process_gas

    def _wait_receipt(self, tx_hash: str) -> None:
        deadline = time.monotonic() + self.receipt_timeout
        while time.monotonic() < deadline:
            if confirmed_receipt(self.rpc, tx_hash, self.confirmations):
                return
            time.sleep(self.poll_seconds)
        raise KeeperError("keeper transaction remains unconfirmed in .local state")

    def submit(self, kind: str, token: str, data: str,
               state: KeeperState, store: KeeperStore) -> None:
        if kind not in ("configure", "process") or not data.startswith(CONFIGURE if kind == "configure" else PROCESS_NEXT):
            raise KeeperError("attempted non-keeper transaction")
        if kind == "process" and data != PROCESS_NEXT:
            raise KeeperError("processNext payload must have no arguments")
        if kind == "configure" and len(data) != 2 + 8 + 64 * 8:
            raise KeeperError("configureOpen payload length is invalid")
        gas_estimate = watch.quantity(self.rpc.call("eth_estimateGas", [{"from": self.signer,
                                               "to": self.bindings.q, "data": data}]), "gas estimate")
        gas = ceil_div(gas_estimate * 120, 100)
        if gas > self._gas_cap(kind):
            raise KeeperError(f"{kind} gas estimate exceeds configured cap")
        nonce = watch.quantity(self.rpc.call("eth_getTransactionCount", [self.signer, "pending"]), "nonce")
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or "baseFeePerGas" not in latest:
            raise KeeperError("RPC did not return an EIP-1559 base fee")
        base_fee = watch.quantity(latest["baseFeePerGas"], "base fee")
        priority = min(watch.quantity(self.rpc.call("eth_maxPriorityFeePerGas", []), "priority fee"),
                       self.max_priority_wei)
        if 2 * base_fee + priority > self.max_fee_wei:
            raise KeeperError("keeper fee exceeds configured cap")
        transaction = {"chainId": self.chain_id, "nonce": nonce, "to": self.bindings.q,
                       "value": 0, "data": data, "gas": gas, "type": 2,
                       "maxFeePerGas": 2 * base_fee + priority, "maxPriorityFeePerGas": priority}
        signed = self.account.sign_transaction(transaction)
        tx_hash = "0x" + signed.hash.hex().lower().removeprefix("0x")
        raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
        state.pending_tx = {"kind": kind, "token": token, "data": data, "nonce": nonce,
                            "txHash": tx_hash, "rawTx": raw}
        store.save(state)
        sent = self.rpc.call("eth_sendRawTransaction", [raw])
        if not isinstance(sent, str) or sent.lower() != tx_hash:
            raise KeeperError("RPC returned an unexpected keeper transaction hash")
        try:
            self._wait_receipt(tx_hash)
        except TransactionReverted:
            state.pending_tx = None
            store.save(state)
            raise WaitForPrice(f"{kind} reverted; token remains pending")
        state.pending_tx = None
        store.save(state)

    def _validated_pending(self, pending: dict[str, Any], state: KeeperState) -> tuple[str, str, str, int]:
        kind, token, data, nonce, tx_hash, raw_hex = (
            pending.get("kind"), pending.get("token"), pending.get("data"),
            pending.get("nonce"), pending.get("txHash"), pending.get("rawTx"))
        if (kind not in ("configure", "process") or not isinstance(token, str) or
                not watch.ADDRESS.fullmatch(token) or not isinstance(data, str) or
                not data.startswith(CONFIGURE if kind == "configure" else PROCESS_NEXT) or
                not isinstance(nonce, int) or nonce < 0 or not isinstance(tx_hash, str) or
                not watch.HASH.fullmatch(tx_hash) or not isinstance(raw_hex, str) or
                not raw_hex.startswith("0x") or len(raw_hex) > 4098 or len(raw_hex) % 2):
            raise KeeperError("pending keeper transaction is malformed")
        if (kind == "process" and data != PROCESS_NEXT) or (kind == "configure" and len(data) != 2 + 8 + 64 * 8):
            raise KeeperError("pending keeper transaction has invalid method payload")
        try:
            raw = bytes.fromhex(raw_hex[2:])
            fields = rlp.decode(raw[1:])
            sender = Account.recover_transaction(raw_hex).lower()
        except Exception as exc:
            raise KeeperError("pending keeper signed transaction cannot be decoded") from exc
        if raw[:1] != b"\x02" or not isinstance(fields, list) or len(fields) != 12:
            raise KeeperError("pending keeper transaction is not signed EIP-1559")
        number = lambda item: int.from_bytes(item, "big")
        if ("0x" + keccak(raw).hex()) != tx_hash.lower():
            raise KeeperError("pending keeper transaction hash mismatch")
        if (number(fields[0]) != state.chain_id or number(fields[1]) != nonce or
                number(fields[2]) > self.max_priority_wei or number(fields[3]) > self.max_fee_wei or
                not 21000 <= number(fields[4]) <= self._gas_cap(kind) or
                fields[5] != bytes.fromhex(state.q[2:]) or number(fields[6]) != 0 or
                fields[7] != bytes.fromhex(data[2:]) or fields[8] != [] or sender != self.signer):
            raise KeeperError("pending signed transaction is outside keeper permissions or caps")
        if kind == "configure":
            try:
                encoded_token, config = decode(["address", "(uint160,uint128,uint128,int24,int24,int24,uint64)"],
                                               bytes.fromhex(data[10:]))
            except (ValueError, DecodingError) as exc:
                raise KeeperError("pending configureOpen payload cannot be decoded") from exc
            if watch.address(encoded_token) != token or config[1] == 0 or config[2] == 0:
                raise KeeperError("pending configureOpen token or budget is invalid")
        return kind, token, tx_hash, nonce

    def recover(self, state: KeeperState, store: KeeperStore,
                settings: PlanSettings, quotes: ExecutableQuoteProvider) -> None:
        if state.pending_tx is None:
            return
        pending = state.pending_tx
        kind, token, tx_hash, nonce = self._validated_pending(pending, state)
        try:
            confirmed = confirmed_receipt(self.rpc, tx_hash, self.confirmations)
        except TransactionReverted:
            state.pending_tx = None
            store.save(state)
            return
        if not confirmed:
            receipt = self.rpc.call("eth_getTransactionReceipt", [tx_hash])
            known = self.rpc.call("eth_getTransactionByHash", [tx_hash]) if receipt is None else receipt
            if known is None:
                confirmed_nonce = watch.quantity(self.rpc.call("eth_getTransactionCount", [self.signer, "latest"]),
                                                 "confirmed nonce")
                if confirmed_nonce > nonce:
                    raise KeeperError("keeper nonce was consumed without matching receipt")
                if kind == "configure":
                    if q_launch_state(self.rpc, state.q, token)[0] != 1:
                        raise KeeperError("unknown pending configureOpen no longer targets a queued launch")
                    fresh = make_plan(self.rpc, self.bindings, token, settings, quotes)
                    config = tuple(int(x) for x in decode(
                        ["address", "(uint160,uint128,uint128,int24,int24,int24,uint64)"],
                        bytes.fromhex(pending["data"][10:]))[1])
                    if not existing_config_safe(self.rpc, self.bindings, fresh, config, latest_block_time(self.rpc)):
                        raise KeeperError("unknown pending configureOpen is stale; inspect nonce before replacing")
                try:
                    sent = self.rpc.call("eth_sendRawTransaction", [pending["rawTx"]])
                except watch.WatcherError:
                    if (self.rpc.call("eth_getTransactionByHash", [tx_hash]) is None and
                            self.rpc.call("eth_getTransactionReceipt", [tx_hash]) is None):
                        raise
                    sent = tx_hash
                if not isinstance(sent, str) or sent.lower() != tx_hash:
                    raise KeeperError("pending rebroadcast returned another transaction hash")
            try:
                self._wait_receipt(tx_hash)
            except TransactionReverted:
                state.pending_tx = None
                store.save(state)
                return
        state.pending_tx = None
        store.save(state)


def configure_data(plan: OpenPlan) -> str:
    return CONFIGURE + encode(["address", "(uint160,uint128,uint128,int24,int24,int24,uint64)"],
                              [plan.token, plan.abi_config()]).hex()


def run_cycle(rpc: watch.Rpc, bindings: Bindings, state: KeeperState, store: KeeperStore,
              settings: PlanSettings, quotes: ExecutableQuoteProvider,
              signer: KeeperSigner | None, confirmations: int, block_span: int,
              process_next: bool) -> dict[str, Any]:
    found = discover_launches(rpc, state, store, confirmations, block_span)
    safe_head = watch.chain_head(rpc) - confirmations
    waiting: dict[str, int] = {}
    # Newest launch first, matching Q's LIFO entry schedule. A token with no
    # Q stage remains durable until the owner watcher enqueues it later.
    for token in reversed(state.tokens[:]):
        stage, configured = q_launch_state(rpc, bindings.q, token, max(0, safe_head))
        if stage not in (0, 1):
            state.tokens.remove(token)
            store.save(state)
            continue
        if stage == 0:
            waiting["not_enqueued_yet"] = waiting.get("not_enqueued_yet", 0) + 1
            continue
        try:
            plan = make_plan(rpc, bindings, token, settings, quotes)
        except UnsupportedLaunch as error:
            state.tokens.remove(token)
            store.save(state)
            waiting[str(error)] = waiting.get(str(error), 0) + 1
            continue
        except WaitForPrice as error:
            reason = str(error)
            waiting[reason] = waiting.get(reason, 0) + 1
            continue
        if signer is None:
            return {"status": "planned_read_only", "found": found, "pending": len(state.tokens),
                    "plan": asdict(plan), "waiting": waiting}
        if q_launch_state(rpc, bindings.q, token)[0] != 1:
            waiting["launch_stage_changed_during_plan"] = waiting.get("launch_stage_changed_during_plan", 0) + 1
            continue
        should_configure = True
        if configured:
            try:
                existing = existing_config(rpc, bindings.executor, token)
                should_configure = not existing_config_safe(rpc, bindings, plan, existing,
                                                             latest_block_time(rpc))
            except (watch.WatcherError, KeeperError):
                should_configure = True
        if should_configure:
            try:
                signer.submit("configure", token, configure_data(plan), state, store)
            except WaitForPrice as error:
                waiting[str(error)] = waiting.get(str(error), 0) + 1
                continue
            current_stage, current_configured = q_launch_state(rpc, bindings.q, token)
            if current_stage == 1 and not current_configured:
                raise KeeperError("confirmed configureOpen did not arm Q launch")
        else:
            current_stage = q_launch_state(rpc, bindings.q, token)[0]
        processed = False
        if process_next and current_stage == 1 and q_next_action_at(rpc, bindings.q, token) <= latest_block_time(rpc):
            try:
                signer.submit("process", token, PROCESS_NEXT, state, store)
                processed = True
            except WaitForPrice as error:
                waiting[str(error)] = waiting.get(str(error), 0) + 1
        return {"status": "configured_or_armed", "token": token, "configured": should_configure,
                "processNextSent": processed, "found": found, "pending": len(state.tokens),
                "waiting": waiting}
    return {"status": "waiting", "found": found, "pending": len(state.tokens), "waiting": waiting}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--chain-id", type=int, default=os.environ.get("PONS_CHAIN_ID"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--guard", default=os.environ.get("PONS_PRICE_GUARD_ADDRESS"))
    parser.add_argument("--quoter", default=QUOTER)
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--block-span", type=int, default=2000)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--budget-bps", type=int, default=2500)
    parser.add_argument("--utilization-bps", type=int, default=9500)
    parser.add_argument("--safety-bps", type=int, default=1500)
    parser.add_argument("--quote-size-multiple", type=int, default=2)
    parser.add_argument("--tick-spacing", type=int, default=60)
    parser.add_argument("--band-width-ticks", type=int, default=1200)
    parser.add_argument("--gap-ticks", type=int, default=60)
    parser.add_argument("--ttl-seconds", type=int, default=120)
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=20)
    parser.add_argument("--max-config-gas", type=int, default=500000)
    parser.add_argument("--max-process-gas", type=int, default=3500000)
    parser.add_argument("--max-fee-gwei", default="5")
    parser.add_argument("--max-priority-gwei", default="1")
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--no-process-next", action="store_true")
    parser.add_argument("--live", action="store_true", help="enable priceConfigurator-signed Q writes")
    parser.add_argument("--once", action="store_true", help="run one discovery/planning cycle")
    args = parser.parse_args(argv)
    if args.live and not args.once:
        parser.error("--live requires --once; run live price/exit cycles serially")
    if not args.http_url or not args.chain_id or not args.q or not args.guard:
        parser.error("HTTP URL, chain ID, Q, and guard are required via flags or environment")
    if (args.confirmations < 1 or args.block_span < 1 or args.poll_seconds <= 0 or
            args.max_config_gas < 21000 or args.max_process_gas < 21000 or args.receipt_timeout < 1):
        parser.error("invalid confirmation, span, poll, gas, or receipt setting")
    settings = PlanSettings(args.budget_bps, args.utilization_bps, args.safety_bps,
                            args.quote_size_multiple, args.tick_spacing,
                            args.band_width_ticks, args.gap_ticks, args.ttl_seconds,
                            args.max_snapshot_age_seconds)
    settings.validate()
    q, guard = watch.address(args.q), watch.address(args.guard)
    rpc = watch.HttpRpc(args.http_url)
    private_key = os.environ.get("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY") if args.live else None
    if args.live and not private_key:
        raise KeeperError("--live requires PONS_PRICE_CONFIGURATOR_PRIVATE_KEY in process environment")
    try:
        signer_address = Account.from_key(private_key).address.lower() if private_key else None
    except Exception as exc:
        raise KeeperError("priceConfigurator private key is invalid") from exc
    bindings = verify_bindings(rpc, args.chain_id, q, guard, args.quoter,
                               signer_address, settings.safety_bps)
    quotes = ExecutableQuoteProvider(rpc, bindings)
    fee_cap, priority_cap = watch.gwei(args.max_fee_gwei), watch.gwei(args.max_priority_gwei)
    if priority_cap > fee_cap:
        raise KeeperError("priority fee cap exceeds max fee cap")
    signer = (KeeperSigner(rpc, bindings, args.chain_id, private_key, args.confirmations,
                           args.max_config_gas, args.max_process_gas, fee_cap,
                           priority_cap, args.receipt_timeout, args.poll_seconds)
              if private_key else None)
    with (ConfiguratorLock(q, args.state) if signer else nullcontext()), KeeperStore(args.state) as store:
        state = store.load(rpc, args.chain_id, q, guard, args.start_block)
        if state.pending_tx is not None:
            if signer is None:
                raise KeeperError("read-only mode cannot recover an outstanding signed transaction")
            signer.recover(state, store, settings, quotes)
        while True:
            try:
                result = run_cycle(rpc, bindings, state, store, settings, quotes, signer,
                                   args.confirmations, args.block_span, not args.no_process_next)
            except watch.WatcherError as error:
                if args.once:
                    raise
                print(json.dumps({"status": "rpc_retry", "reason": str(error)}), flush=True)
                time.sleep(args.poll_seconds)
                continue
            print(json.dumps(result, separators=(",", ":")), flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeeperError, watch.WatcherError) as error:
        print(f"price keeper stopped: {error}", file=sys.stderr)
        sys.exit(1)
