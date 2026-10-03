#!/usr/bin/env python3
"""Deterministic three-band X/Q planner for authenticated Pons launches.

Each band is funded by a separate new 0.1%-of-supply Q mint. The price scale
uses a separate 1%-of-supply reference amount and X's total supply; the range
multiplier comes from the Pons curve's phantom reserve and graduation
threshold. No external X/ETH or Q/ETH price is used. Live
configureOpen/processNext writes are opt-in.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass, asdict, field
import fcntl
import json
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
MAX_PRIORITY_TOKENS = 64
PRIORITY_ROUNDS = 9
MAX_DISCOVERY_LOGS = 128
MAX_DISCOVERY_SPLITS = 16
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"  # exit/harvest keeper compatibility
POOL_MANAGER_ADDRESS = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSITION_MANAGER_ADDRESS = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
STATE_VIEW_ADDRESS = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
ZERO = "0x" + "00" * 20
Q96 = 1 << 96
Q192 = 1 << 192
MAX_UINT128 = (1 << 128) - 1
MINT_BPS = 10
PRICE_REFERENCE_BPS = 100
BPS_DENOMINATOR = 10_000
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT = 4295128739
MAX_SQRT = 1461446703485210103287273052203988822378723970342

CONFIGURE = "0x" + keccak(text="configureOpen(address,bytes)")[:4].hex()
PROCESS_NEXT = "0x4ba3eeaf"  # processNext()
NEXT_ACTION = "0x" + keccak(text="nextAction()")[:4].hex()
TRANSFER_STEP_GAS_LIMIT = "0x" + keccak(text="transferStepGasLimit()")[:4].hex()
# Q reserves 150k after the executor call and up to 140k for a harvest
# preview. Leave room for processNext dispatch and EIP-150 call forwarding.
PROCESS_GAS_OVERHEAD = 1_000_000
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
OPEN_CONFIGS = "0xb1a38d6c"
OPEN_PRICE_CONFIGURED_TOPIC = "0x" + keccak(text="OpenPriceConfigured(address,bytes32)").hex()
GET_RESERVES = "0x0902f1ac"
FEE_BPS = "0x24a9d853"
CREATOR_TAX_BPS = "0xc1bb8901"
QUOTE_EXACT_INPUT_SINGLE = "0xaa9d21cb"
SETTLEMENT_ROUTER = "0x" + keccak(text="settlementRouter()")[:4].hex()
CONTROLLER = "0x" + keccak(text="controller()")[:4].hex()
ROUTER_SOURCE = "0x" + keccak(text="source()")[:4].hex()
ROUTER_FACTORY = "0x" + keccak(text="factory()")[:4].hex()
CURVE_TOKEN = "0x" + keccak(text="token()")[:4].hex()
CURVE_PAIR_TOKEN = "0x" + keccak(text="pairToken()")[:4].hex()
CURVE_GRADUATION_THRESHOLD = "0x" + keccak(text="graduationThreshold()")[:4].hex()
CURVE_REAL_QUOTE = "0x" + keccak(text="realQuoteReserve()")[:4].hex()
TOTAL_SUPPLY = "0x18160ddd"
OPEN_MINT_BPS = "0x" + keccak(text="OPEN_MINT_BPS()")[:4].hex()
TOTAL_SUPPLY_CEILING = "0x" + keccak(text="TOTAL_SUPPLY_CEILING()")[:4].hex()
ACTIVE_POSITION_COUNT = "0x" + keccak(text="activePositionCount(address)")[:4].hex()
FEE_POLICY = "0x" + keccak(text="feePolicy()")[:4].hex()
MIN_FEE_PIPS = "0x" + keccak(text="MIN_FEE_PIPS()")[:4].hex()
MAX_FEE_PIPS = "0x" + keccak(text="MAX_FEE_PIPS()")[:4].hex()
FEE_STEP_PIPS = "0x" + keccak(text="FEE_STEP_PIPS()")[:4].hex()
POOL_MANAGER = "0x" + keccak(text="poolManager()")[:4].hex()
POSITION_MANAGER = "0x" + keccak(text="positionManager()")[:4].hex()

LAUNCH_TYPES = ["address", "address", "address", "address", "address", "uint256", "uint24",
                "int24", "uint16", "bool", "uint8", "uint256", "uint256", "uint256", "bool"]
CONFIG_TYPES = ["uint160", "uint128[3]", "uint128[3]", "int24", "int24[3]", "int24[3]", "uint64"]
CONFIGURE_DATA_HEX_LENGTH = 2 + 8 + 64 * 18  # address, offset, bytes length, 15 plan words
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


def amount0_ceil(liquidity: int, lower_sqrt: int, upper_sqrt: int) -> int:
    return ceil_div(ceil_div((liquidity << 96) * (upper_sqrt - lower_sqrt), upper_sqrt), lower_sqrt)


def amount1_ceil(liquidity: int, lower_sqrt: int, upper_sqrt: int) -> int:
    return ceil_div(liquidity * (upper_sqrt - lower_sqrt), Q96)


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
    utilization_bps: int = 9500
    tick_spacing: int = 60
    ttl_seconds: int = 600
    max_snapshot_age_seconds: int = 20

    def validate(self) -> None:
        if (not 1 <= self.utilization_bps <= 10000 or
                not 1 <= self.tick_spacing <= 32767 or
                not 30 <= self.ttl_seconds <= 900 or
                not 1 <= self.max_snapshot_age_seconds <= 120):
            raise KeeperError("invalid planner setting")


@dataclass(frozen=True)
class OpenPlan:
    token: str
    starting_sqrt_price_x96: int
    liquidity: tuple[int, int, int]
    max_quote_in: tuple[int, int, int]
    tick_spacing: int
    tick_lower: tuple[int, int, int]
    tick_upper: tuple[int, int, int]
    deadline: int
    minted_quote: tuple[int, int, int]
    x_total_supply: int
    q_total_supply: int
    phantom_quote: int
    graduation_threshold: int
    quote_is_0: bool

    def abi_config(self) -> tuple[Any, ...]:
        return (self.starting_sqrt_price_x96, self.liquidity, self.max_quote_in,
                self.tick_spacing, self.tick_lower, self.tick_upper, self.deadline)


def floor_tick_ratio(numerator: int, denominator: int) -> int:
    """Largest v4 tick no higher than an exact token1/token0 raw price."""
    if numerator <= 0 or denominator <= 0:
        raise WaitForPrice("price ratio must be positive")
    scaled = numerator * Q192
    if (MIN_SQRT * MIN_SQRT * denominator > scaled or
            MAX_SQRT * MAX_SQRT * denominator <= scaled):
        raise WaitForPrice("deterministic price exceeds v4 tick range")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sqrt_at_tick(mid) ** 2 * denominator <= scaled:
            lo = mid
        else:
            hi = mid - 1
    return lo


def ceil_tick_ratio(numerator: int, denominator: int) -> int:
    tick = floor_tick_ratio(numerator, denominator)
    if sqrt_at_tick(tick) ** 2 * denominator < numerator * Q192:
        tick += 1
    return tick


def sequential_mints(q_supply: int) -> tuple[int, int, int]:
    if q_supply <= 0:
        raise WaitForPrice("Q total supply is zero")
    supply = q_supply
    mints: list[int] = []
    for _ in range(3):
        minted = supply * MINT_BPS // BPS_DENOMINATOR
        if not 0 < minted <= MAX_UINT128:
            raise WaitForPrice("0.1% Q mint is outside uint128 range")
        mints.append(minted)
        supply += minted
    return tuple(mints)  # type: ignore[return-value]


def plan_position(token: str, q: str, x_supply: int, q_supply: int,
                  phantom_quote: int, graduation_threshold: int,
                  block_time: int, settings: PlanSettings,
                  q_supply_ceiling: int | None = None) -> OpenPlan:
    """Build nested Q-only bands at p0→R*p0, 2R*p0, and 10R*p0."""
    settings.validate()
    token, q = watch.address(token), watch.address(q)
    if token == q or x_supply <= 0 or phantom_quote <= 0 or graduation_threshold <= 0:
        raise WaitForPrice("invalid Pons launch economics")
    mints = sequential_mints(q_supply)
    # Q checks its ceiling on every mint before the executor can burn unused
    # inventory. A net-of-burn cap check would therefore admit a reverting open.
    if q_supply_ceiling is not None and (
            q_supply_ceiling <= 0 or q_supply + sum(mints) > q_supply_ceiling):
        raise WaitForPrice("three new Q mints exceed total-supply ceiling")
    # Keep the prior price policy independently of tranche funding: p0 is a
    # 1%-of-S reference amount divided by X supply, while each actual new
    # tranche is only 0.1% of then-current S. Ratios use v4 atomic units.
    p_num, p_den = q_supply * PRICE_REFERENCE_BPS // BPS_DENOMINATOR, x_supply
    if p_num == 0:
        raise WaitForPrice("Q price reference amount is zero")
    r_num = (phantom_quote + graduation_threshold) ** 2
    r_den = phantom_quote ** 2
    quote_is_0 = int(q, 16) < int(token, 16)
    lowers: list[int] = []
    uppers: list[int] = []
    liquidities: list[int] = []
    limits: list[int] = []
    for multiple, minted in zip((1, 2, 10), mints):
        high_num, high_den = p_num * r_num * multiple, p_den * r_den
        if quote_is_0:
            # token1/token0 is X/Q, the reciprocal of Q/X.
            lower = align_down(floor_tick_ratio(high_den, high_num), settings.tick_spacing)
            upper = align_up(ceil_tick_ratio(p_den, p_num), settings.tick_spacing)
        else:
            lower = align_down(floor_tick_ratio(p_num, p_den), settings.tick_spacing)
            upper = align_up(ceil_tick_ratio(high_num, high_den), settings.tick_spacing)
        if lower < MIN_TICK or upper > MAX_TICK or lower >= upper:
            raise WaitForPrice("deterministic band is outside v4 tick range")
        lower_sqrt, upper_sqrt = sqrt_at_tick(lower), sqrt_at_tick(upper)
        target = minted * settings.utilization_bps // 10000
        liquidity = liquidity_for_budget(target, lower_sqrt, upper_sqrt, quote_is_0)
        spent = (amount0_ceil(liquidity, lower_sqrt, upper_sqrt) if quote_is_0
                 else amount1_ceil(liquidity, lower_sqrt, upper_sqrt))
        if liquidity == 0 or spent == 0 or spent > target or target > minted:
            raise WaitForPrice("0.1% Q mint cannot fund a positive band")
        lowers.append(lower)
        uppers.append(upper)
        liquidities.append(liquidity)
        limits.append(minted)
    if quote_is_0:
        if not (lowers[0] > lowers[1] > lowers[2]):
            raise WaitForPrice("three rounded Pons bands are not distinct")
        initial_tick = lowers[2] - settings.tick_spacing
    else:
        if not (uppers[0] < uppers[1] < uppers[2]):
            raise WaitForPrice("three rounded Pons bands are not distinct")
        initial_tick = uppers[2] + settings.tick_spacing
    if not MIN_TICK <= initial_tick <= MAX_TICK:
        raise WaitForPrice("Q-only initialization exceeds v4 tick range")
    initial_sqrt = sqrt_at_tick(initial_tick)
    if not (initial_sqrt < sqrt_at_tick(lowers[2]) if quote_is_0
            else initial_sqrt > sqrt_at_tick(uppers[2])):
        raise KeeperError("initialization is not beyond all Q-only bands")
    return OpenPlan(token, initial_sqrt, tuple(liquidities), tuple(limits),
                    settings.tick_spacing, tuple(lowers), tuple(uppers),
                    block_time + settings.ttl_seconds, mints, x_supply, q_supply,
                    phantom_quote, graduation_threshold, quote_is_0)


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


def process_gas_floor(rpc: watch.Rpc, q: str) -> int:
    step_gas = read_uint(rpc, q, TRANSFER_STEP_GAS_LIMIT, "uint32")
    if not 100_000 <= step_gas <= 10_000_000:
        raise KeeperError("Q transferStepGasLimit is outside contract bounds")
    return step_gas + PROCESS_GAS_OVERHEAD


def require_code(rpc: watch.Rpc, contract: str, name: str) -> None:
    code = rpc.call("eth_getCode", [contract, "latest"])
    if (not isinstance(code, str) or len(code) <= 2 or len(code) % 2 != 0 or
            not code.startswith("0x")):
        raise KeeperError(f"{name} has no contract code")
    try:
        bytes.fromhex(code[2:])
    except ValueError as exc:
        raise KeeperError(f"{name} returned malformed contract code") from exc


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
    total_supply_ceiling: int = 0


def verify_bindings(rpc: watch.Rpc, chain_id: int, q: str, guard: str, quoter: str,
                    configurator: str | None, safety_bps: int | None = 1500) -> Bindings:
    q, guard, quoter = watch.address(q), watch.address(guard), watch.address(quoter)
    if chain_id != 4663:
        raise KeeperError("Pons price keeper requires Robinhood chain 4663")
    if watch.quantity(rpc.call("eth_chainId", []), "chain ID") != chain_id:
        raise KeeperError("RPC chain ID differs from configured chain ID")
    executor = read_address(rpc, q, EXECUTOR)
    for name, contract in (("Q", q), ("executor", executor),
                           ("Pons factory", watch.PONS_FACTORY),
                           ("v4 PoolManager", POOL_MANAGER_ADDRESS),
                           ("v4 PositionManager", POSITION_MANAGER_ADDRESS),
                           ("v4 StateView", STATE_VIEW_ADDRESS)):
        require_code(rpc, contract, name)
    if read_address(rpc, q, watch.PONS_FACTORY_GETTER) != watch.PONS_FACTORY:
        raise KeeperError("Q has a different Pons factory")
    if read_uint(rpc, q, OPEN_MINT_BPS, "uint16") != MINT_BPS:
        raise KeeperError("Q 0.1% mint rule differs from planner")
    ceiling = read_uint(rpc, q, TOTAL_SUPPLY_CEILING)
    if ceiling <= 0:
        raise KeeperError("Q total-supply ceiling is unavailable")
    if (read_address(rpc, executor, CONTROLLER) != q or
            read_address(rpc, executor, QUOTE_TOKEN) != q):
        raise KeeperError("executor controller/Q binding mismatch")
    if (read_address(rpc, executor, POOL_MANAGER) != POOL_MANAGER_ADDRESS or
            read_address(rpc, executor, POSITION_MANAGER) != POSITION_MANAGER_ADDRESS or
            read_address(rpc, executor, STATE_VIEW) != STATE_VIEW_ADDRESS):
        raise KeeperError("executor v4 infrastructure differs from Robinhood deployment")
    policy = read_address(rpc, q, FEE_POLICY)
    if policy == ZERO:
        raise KeeperError("Q has no static fee policy")
    require_code(rpc, policy, "static fee policy")
    if (read_address(rpc, policy, CONTROLLER) != q or
            read_uint(rpc, policy, MIN_FEE_PIPS, "uint24") != 50_000 or
            read_uint(rpc, policy, MAX_FEE_PIPS, "uint24") != 500_000 or
            read_uint(rpc, policy, FEE_STEP_PIPS, "uint24") != 10_000):
        raise KeeperError("Q static fee policy is outside 5-50% range")
    if configurator is not None and read_address(rpc, q, PRICE_CONFIGURATOR) != configurator:
        raise KeeperError("signing account is not Q.priceConfigurator")
    router = read_address(rpc, executor, SETTLEMENT_ROUTER)
    if router == ZERO:
        raise KeeperError("executor has no bound settlement router")
    require_code(rpc, router, "settlement router")
    if (read_address(rpc, router, ROUTER_SOURCE) != executor or
            read_address(rpc, router, QUOTE_TOKEN) != q or
            read_address(rpc, router, ROUTER_FACTORY) != watch.PONS_FACTORY):
        raise KeeperError("settlement router binding mismatch")
    # The opening planner has no Q/ETH dependency. The optional guard route
    # remains available to the separate exit and harvest keepers that import
    # this module for executable settlement quotes.
    state_view, raw_id, fee, spacing = ZERO, "0x" + "00" * 32, 0, 0
    pons_hook = ZERO
    if guard != ZERO:
        for name, contract in (("guard", guard), ("v4 Quoter", quoter)):
            require_code(rpc, contract, name)
        if (read_address(rpc, guard, QUOTE_TOKEN) != q or
                read_address(rpc, guard, watch.PONS_FACTORY_GETTER) != watch.PONS_FACTORY):
            raise KeeperError("guard Q/factory binding mismatch")
        state_view = read_address(rpc, guard, STATE_VIEW)
        require_code(rpc, state_view, "guard StateView")
        raw_id = rpc.call("eth_call", [{"to": guard, "data": QUOTE_ETH_POOL_ID}, "latest"])
        if not isinstance(raw_id, str) or not watch.HASH.fullmatch(raw_id):
            raise KeeperError("guard returned invalid Q/ETH pool ID")
        fee = read_uint(rpc, guard, QUOTE_ETH_FEE, "uint24")
        spacing = read_uint(rpc, guard, QUOTE_ETH_SPACING, "int24")
        if fee <= 0 or spacing <= 0:
            raise KeeperError("guard Q/ETH pool settings are invalid")
        pons_hook = read_address(rpc, watch.PONS_FACTORY, MEME_HOOK)
    return Bindings(q, executor, guard, state_view, raw_id.lower(), fee, spacing,
                    pons_hook, quoter, ZERO, ceiling)


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


def latest_block_time(rpc: watch.Rpc) -> int:
    block = rpc.call("eth_getBlockByNumber", ["latest", False])
    if not isinstance(block, dict):
        raise KeeperError("latest block is missing")
    return watch.quantity(block.get("timestamp"), "block timestamp")


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


@dataclass
class KeeperState:
    chain_id: int
    q: str
    guard: str
    last_block: int
    last_hash: str
    tokens: list[str]
    pending_tx: dict[str, Any] | None = None
    priority_tokens: list[str] = field(default_factory=list)
    scan_cursor: int = 0
    priority_cursor: int = 0
    schedule_round: int = 0
    priority_expires: dict[str, int] = field(default_factory=dict)
    configured_plans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return {"version": 1, "chainId": self.chain_id, "factory": watch.PONS_FACTORY,
                "q": self.q, "guard": self.guard, "lastBlock": self.last_block,
                "lastHash": self.last_hash, "tokens": self.tokens, "pendingTx": self.pending_tx,
                "priorityTokens": self.priority_tokens, "scanCursor": self.scan_cursor,
                "priorityCursor": self.priority_cursor, "scheduleRound": self.schedule_round,
                "priorityExpires": self.priority_expires,
                "configuredPlans": self.configured_plans}


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
        priority = data.get("priorityTokens", [])
        scan_cursor = data.get("scanCursor", 0)
        priority_cursor = data.get("priorityCursor", 0)
        schedule_round = data.get("scheduleRound", 0)
        priority_expires = data.get("priorityExpires")
        configured_plans = data.get("configuredPlans", {})
        if priority_expires is None and isinstance(priority, list) and type(schedule_round) is int:
            priority_expires = {token: schedule_round + PRIORITY_ROUNDS for token in priority
                                if isinstance(token, str)}
        if (not isinstance(block, int) or block < 0 or not isinstance(block_hash, str) or
                not watch.HASH.fullmatch(block_hash) or not isinstance(tokens, list) or
                any(not isinstance(t, str) or not watch.ADDRESS.fullmatch(t) for t in tokens) or
                len(tokens) != len(set(tokens)) or
                not isinstance(priority, list) or
                any(not isinstance(t, str) or not watch.ADDRESS.fullmatch(t) for t in priority) or
                len(priority) != len(set(priority)) or not set(priority).issubset(tokens) or
                len(priority) > MAX_PRIORITY_TOKENS or
                type(scan_cursor) is not int or scan_cursor < 0 or
                scan_cursor >= max(1, len(tokens)) or
                type(priority_cursor) is not int or priority_cursor < 0 or
                priority_cursor >= max(1, len(priority)) or
                type(schedule_round) is not int or schedule_round < 0 or
                not isinstance(priority_expires, dict) or
                set(priority_expires) != set(priority) or
                any(type(expiry) is not int or expiry < 0 for expiry in priority_expires.values()) or
                not isinstance(configured_plans, dict) or
                any(not isinstance(token, str) or not watch.ADDRESS.fullmatch(token) or
                    token not in tokens or not isinstance(record, dict) or
                    set(record) != {"staticHash", "planHash", "deadline", "sqrtPrice", "spacing",
                                    "txHash", "block", "blockHash"} or
                    any(not isinstance(record[key], str) or not watch.HASH.fullmatch(record[key])
                        for key in ("staticHash", "planHash", "txHash", "blockHash")) or
                    type(record["deadline"]) is not int or not 0 < record["deadline"] <= (1 << 64) - 1 or
                    type(record["sqrtPrice"]) is not int or not 0 < record["sqrtPrice"] < (1 << 160) or
                    type(record["spacing"]) is not int or not 0 < record["spacing"] < (1 << 23) or
                    type(record["block"]) is not int or record["block"] < 0
                    for token, record in configured_plans.items()) or
                (pending is not None and not isinstance(pending, dict))):
            raise KeeperError("keeper state fields are malformed")
        if start_block is not None and start_block != block + 1:
            raise KeeperError("--start-block conflicts with saved keeper cursor")
        return KeeperState(chain_id, q, guard, block, block_hash.lower(), tokens, pending,
                           priority, scan_cursor, priority_cursor, schedule_round,
                           priority_expires, configured_plans)

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
                      confirmations: int, block_span: int, max_ranges: int = 2) -> int:
    if max_ranges < 1:
        raise KeeperError("discovery range budget must be positive")
    if watch.block_hash(rpc, state.last_block) != state.last_hash:
        raise KeeperError("keeper cursor block hash changed; reconcile the reorg")
    safe_head = watch.chain_head(rpc) - confirmations
    count = 0
    ranges = 0
    known = set(state.tokens)
    while state.last_block < safe_head and ranges < max_ranges:
        first, last = state.last_block + 1, min(safe_head, state.last_block + block_span)
        # Shrink dense ranges before per-log canonical hash checks. Even a
        # firehose range can consume at most MAX_DISCOVERY_LOGS such reads;
        # no cursor is committed until the accepted subrange is validated.
        attempts = 0
        while True:
            if attempts >= MAX_DISCOVERY_SPLITS:
                raise KeeperError("launch range exceeds bounded split budget")
            attempts += 1
            expected_hash = watch.block_hash(rpc, last)
            logs = rpc.call("eth_getLogs", [{"address": watch.PONS_FACTORY,
                                             "topics": [watch.TOKEN_LAUNCHED],
                                             "fromBlock": hex(first), "toBlock": hex(last)}])
            if not isinstance(logs, list):
                raise KeeperError("confirmed launch range response is malformed")
            if len(logs) <= MAX_DISCOVERY_LOGS:
                break
            if last == first:
                raise KeeperError("single launch block exceeds bounded discovery log cap")
            last = first + (last - first) // 2
        if watch.block_hash(rpc, last) != expected_hash:
            raise KeeperError("confirmed launch range changed during backfill")
        block_hashes: dict[int, str] = {}
        new_tokens: list[str] = []
        for log in sorted(logs, key=watch.log_order):
            number = watch.quantity(log.get("blockNumber"), "launch block")
            if number not in block_hashes:
                block_hashes[number] = watch.block_hash(rpc, number)
            if not first <= number <= last or str(log.get("blockHash", "")).lower() != block_hashes[number]:
                raise KeeperError("noncanonical or out-of-range launch log")
            token = watch.token_from_log(log, watch.PONS_FACTORY)
            if watch.pair_token_from_log(log) != ZERO:
                continue
            if token not in known:
                new_tokens.append(token)
                known.add(token)
        if watch.block_hash(rpc, last) != expected_hash:
            raise KeeperError("confirmed launch range changed during backfill")
        for token in new_tokens:
            state.tokens.append(token)
            _prioritize(state, token)
        count += len(new_tokens)
        state.last_block, state.last_hash = last, expected_hash
        store.save(state)
        ranges += 1
    return count


def existing_config(rpc: watch.Rpc, executor: str, token: str) -> tuple[Any, ...]:
    # Solidity omits all fixed-array struct fields from this autogenerated
    # getter. The confirmed-tx journal and Q's plan-hash event below verify
    # those fields; this getter independently checks the visible scalars.
    return call_abi(rpc, executor, OPEN_CONFIGS, ["address"], [token],
                    ["uint160", "int24", "uint64"])


def urgent_open_token(rpc: watch.Rpc, bindings: Bindings, state: KeeperState,
                      settings: PlanSettings, now: int) -> str | None:
    """Select a ready Q head whose short-lived open plan needs refreshing."""
    if not state.tokens:
        return None
    token, step, eligible_at = call_abi(rpc, bindings.q, NEXT_ACTION,
                                         outputs=["address", "uint8", "uint64"])
    token = watch.address(token)
    if (int(step) != 0 or token == ZERO or token not in state.tokens or
            int(eligible_at) > now):
        return None
    stage, configured = q_launch_state(rpc, bindings.q, token)
    if stage != 1:
        return None
    if not configured:
        return token
    record = state.configured_plans.get(token)
    if record is None:
        return token
    deadline = int(existing_config(rpc, bindings.executor, token)[2])
    refresh_seconds = min(180, max(30, settings.ttl_seconds // 2))
    return token if (deadline < now + refresh_seconds or
                     record["deadline"] != deadline) else None


def existing_config_safe(rpc: watch.Rpc, bindings: Bindings, plan: OpenPlan,
                         existing: tuple[Any, ...], now: int) -> bool:
    try:
        sqrt_price, liquidity, max_q, spacing, lower, upper, deadline = existing
        liquidity = tuple(int(x) for x in liquidity)
        max_q = tuple(int(x) for x in max_q)
        lower = tuple(int(x) for x in lower)
        upper = tuple(int(x) for x in upper)
        if (int(sqrt_price) != plan.starting_sqrt_price_x96 or
                liquidity != plan.liquidity or int(spacing) != plan.tick_spacing or
                lower != plan.tick_lower or upper != plan.tick_upper or
                int(deadline) < now + 30 or len(max_q) != 3):
            return False
        for i in range(3):
            lower_sqrt, upper_sqrt = sqrt_at_tick(lower[i]), sqrt_at_tick(upper[i])
            spent = (amount0_ceil(liquidity[i], lower_sqrt, upper_sqrt) if plan.quote_is_0
                     else amount1_ceil(liquidity[i], lower_sqrt, upper_sqrt))
            if not 0 < spent <= max_q[i] <= plan.max_quote_in[i]:
                return False
        return True
    except (ValueError, TypeError, IndexError, KeeperError):
        return False


def config_static_hash(config: tuple[Any, ...]) -> str:
    """Hash every plan field except the rolling deadline."""
    return "0x" + keccak(encode(CONFIG_TYPES[:-1], config[:-1])).hex()


def _configured_plan_record(rpc: watch.Rpc, data: str, tx_hash: str) -> dict[str, Any]:
    """Journal only a canonically confirmed configure transaction."""
    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if not isinstance(receipt, dict):
        raise KeeperError("confirmed configuration receipt disappeared")
    block = watch.quantity(receipt.get("blockNumber"), "configuration block")
    block_hash = str(receipt.get("blockHash", "")).lower()
    if not watch.HASH.fullmatch(block_hash) or watch.block_hash(rpc, block) != block_hash:
        raise KeeperError("configuration receipt is not canonical")
    try:
        _, encoded = decode(["address", "bytes"], bytes.fromhex(data[10:]))
        config = decode(CONFIG_TYPES, encoded)
    except (ValueError, DecodingError) as exc:
        raise KeeperError("confirmed configuration calldata is malformed") from exc
    return {"staticHash": config_static_hash(config),
            "planHash": "0x" + keccak(encoded).hex(),
            "deadline": int(config[6]), "sqrtPrice": int(config[0]),
            "spacing": int(config[3]), "txHash": tx_hash.lower(),
            "block": block, "blockHash": block_hash}


def configured_plan_reusable(rpc: watch.Rpc, bindings: Bindings, state: KeeperState,
                             plan: OpenPlan, now: int,
                             min_remaining_seconds: int = 30) -> bool:
    """Reuse only a confirmed plan whose full arrays remain Q's latest hash."""
    record = state.configured_plans.get(plan.token)
    if record is None or record["staticHash"] != config_static_hash(plan.abi_config()):
        return False
    if record["deadline"] < now + min_remaining_seconds:
        return False
    if watch.block_hash(rpc, record["block"]) != record["blockHash"]:
        return False
    sqrt_price, spacing, deadline = existing_config(rpc, bindings.executor, plan.token)
    if (int(sqrt_price) != record["sqrtPrice"] or int(spacing) != record["spacing"] or
            int(deadline) != record["deadline"]):
        return False
    if q_launch_state(rpc, bindings.q, plan.token) != (1, True):
        return False
    topic_token = "0x" + plan.token[2:].rjust(64, "0")
    logs = rpc.call("eth_getLogs", [{"address": bindings.q,
                                     "fromBlock": hex(record["block"]), "toBlock": "latest",
                                     "topics": [OPEN_PRICE_CONFIGURED_TOPIC, topic_token]}])
    if not isinstance(logs, list):
        raise KeeperError("Q configuration event query is malformed")
    if not logs:
        return False
    latest = max(logs, key=lambda row: (watch.quantity(row.get("blockNumber"), "event block"),
                                        watch.quantity(row.get("transactionIndex"), "event transaction"),
                                        watch.quantity(row.get("logIndex"), "event index")))
    topics = latest.get("topics")
    if (not isinstance(topics, list) or len(topics) != 3 or
            str(latest.get("address", "")).lower() != bindings.q or
            str(topics[0]).lower() != OPEN_PRICE_CONFIGURED_TOPIC or
            str(topics[1]).lower() != topic_token):
        raise KeeperError("Q configuration event is malformed")
    return str(topics[2]).lower() == record["planHash"]


def make_plan(rpc: watch.Rpc, bindings: Bindings, token: str,
              settings: PlanSettings, quotes: ExecutableQuoteProvider | None = None) -> OpenPlan:
    anchor_head = watch.chain_head(rpc)
    anchor_hash = watch.block_hash(rpc, anchor_head)
    snapshot = PinnedRpc(rpc, anchor_head)
    launch = read_launch(snapshot, token)
    if watch.address(launch[4]) != ZERO:
        raise UnsupportedLaunch("Pons launch is not paired with native ETH")
    if int(launch[10]) == 1:
        raise WaitForPrice("Pons launch is in swept phase 1")
    if int(launch[10]) not in (0, 2):
        raise UnsupportedLaunch("Pons launch is no longer in phase 0 or 2")
    curve = watch.address(launch[1])
    if snapshot.call("eth_getCode", [curve, hex(anchor_head)]) == "0x":
        raise WaitForPrice("Pons curve has no code")
    if (read_address(snapshot, curve, ROUTER_FACTORY) != watch.PONS_FACTORY or
            read_address(snapshot, curve, CURVE_TOKEN) != token or
            read_address(snapshot, curve, CURVE_PAIR_TOKEN) != ZERO):
        raise WaitForPrice("Pons curve does not match factory launch")
    threshold = int(launch[5])
    if (threshold <= 0 or
            read_uint(snapshot, curve, CURVE_GRADUATION_THRESHOLD) != threshold):
        raise WaitForPrice("Pons graduation threshold mismatch")
    quote_reserve, _ = call_abi(snapshot, curve, GET_RESERVES,
                                outputs=["uint256", "uint256"])
    real_quote = read_uint(snapshot, curve, CURVE_REAL_QUOTE)
    if quote_reserve <= real_quote:
        raise WaitForPrice("Pons phantom quote reserve is unavailable")
    active = int(call_abi(snapshot, bindings.executor, ACTIVE_POSITION_COUNT,
                          ["address"], [token], ["uint8"])[0])
    if active != 0:
        raise UnsupportedLaunch("X/Q pool already has an active position")
    x_supply = read_uint(snapshot, token, TOTAL_SUPPLY)
    q_supply = read_uint(snapshot, bindings.q, TOTAL_SUPPLY)
    snapshot_time = latest_block_time(snapshot)
    plan = plan_position(token, bindings.q, x_supply, q_supply,
                         quote_reserve - real_quote, threshold, snapshot_time, settings,
                         bindings.total_supply_ceiling or None)
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
        if kind == "configure" and len(data) != CONFIGURE_DATA_HEX_LENGTH:
            raise KeeperError("configureOpen payload length is invalid")
        if kind == "configure":
            try:
                encoded_token, encoded = decode(["address", "bytes"], bytes.fromhex(data[10:]))
                config = decode(CONFIG_TYPES, encoded)
                canonical = CONFIGURE + encode(["address", "bytes"],
                                                [encoded_token, encode(CONFIG_TYPES, config)]).hex()
            except (ValueError, DecodingError) as exc:
                raise KeeperError("configureOpen payload is malformed") from exc
            if (watch.address(encoded_token) != token or data.lower() != canonical.lower() or
                    any(int(x) == 0 for x in config[1]) or
                    any(int(x) == 0 for x in config[2])):
                raise KeeperError("configureOpen payload is outside planned bounds")
        gas_estimate = watch.quantity(self.rpc.call("eth_estimateGas", [{"from": self.signer,
                                               "to": self.bindings.q, "data": data}]), "gas estimate")
        gas = ceil_div(gas_estimate * 120, 100)
        if kind == "process":
            gas = max(gas, process_gas_floor(self.rpc, self.bindings.q))
        if gas > self._gas_cap(kind):
            raise KeeperError(f"{kind} gas requirement exceeds configured cap")
        nonce = watch.quantity(self.rpc.call("eth_getTransactionCount", [self.signer, "pending"]), "nonce")
        latest = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict) or "baseFeePerGas" not in latest:
            raise KeeperError("RPC did not return an EIP-1559 base fee")
        base_fee = watch.quantity(latest["baseFeePerGas"], "base fee")
        priority = min(watch.quantity(self.rpc.call("eth_maxPriorityFeePerGas", []), "priority fee"),
                       self.max_priority_wei)
        if 2 * base_fee + priority > self.max_fee_wei:
            raise KeeperError("keeper fee exceeds configured cap")
        transaction = {"chainId": self.chain_id, "nonce": nonce,
                       "to": watch.signing_address(self.bindings.q),
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
        if kind == "configure":
            state.configured_plans[token] = _configured_plan_record(self.rpc, data, tx_hash)
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
        if (kind == "process" and data != PROCESS_NEXT) or (kind == "configure" and len(data) != CONFIGURE_DATA_HEX_LENGTH):
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
                encoded_token, config_bytes = decode(["address", "bytes"],
                                                     bytes.fromhex(data[10:]))
                config = decode(CONFIG_TYPES, config_bytes)
            except (ValueError, DecodingError) as exc:
                raise KeeperError("pending configureOpen payload cannot be decoded") from exc
            if (watch.address(encoded_token) != token or
                    any(int(value) == 0 for value in config[1]) or
                    any(int(value) == 0 for value in config[2])):
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
                    # The state file is written before the shared budget guard
                    # sees the raw transaction. If that guard rejected it
                    # before reservation, an aged price plan must be dropped
                    # and rebuilt instead of rebroadcasting stale calldata.
                    from pons_tx_budget import BudgetError, never_reserved_after_prior_nonce
                    pending_nonce = watch.quantity(
                        self.rpc.call("eth_getTransactionCount", [self.signer, "pending"]),
                        "pending nonce")
                    try:
                        never_sent = (confirmed_nonce == nonce and pending_nonce == nonce and
                                      never_reserved_after_prior_nonce(self.signer, tx_hash, nonce))
                    except BudgetError as exc:
                        raise KeeperError("budget journal cannot prove pending configure was unsent") from exc
                    if never_sent:
                        state.pending_tx = None
                        store.save(state)
                        return
                    if q_launch_state(self.rpc, state.q, token)[0] != 1:
                        raise KeeperError("unknown pending configureOpen no longer targets a queued launch")
                    fresh = make_plan(self.rpc, self.bindings, token, settings, quotes)
                    encoded = decode(["address", "bytes"], bytes.fromhex(pending["data"][10:]))[1]
                    config = decode(CONFIG_TYPES, encoded)
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
        if kind == "configure":
            state.configured_plans[token] = _configured_plan_record(self.rpc, pending["data"], tx_hash)
        state.pending_tx = None
        store.save(state)


def configure_data(plan: OpenPlan) -> str:
    return CONFIGURE + encode(["address", "bytes"],
                              [plan.token, encode(CONFIG_TYPES, plan.abi_config())]).hex()


def _take_scheduled(state: KeeperState, priority: bool, seen: set[str]) -> str | None:
    """Advance one durable round-robin lane, skipping checks already done."""
    items = state.priority_tokens if priority else state.tokens
    cursor = "priority_cursor" if priority else "scan_cursor"
    for _ in range(len(items)):
        index = getattr(state, cursor) % len(items)
        token = items[index]
        setattr(state, cursor, (index + 1) % len(items))
        if token not in seen:
            return token
    return None


def _drop_priority(state: KeeperState, token: str) -> bool:
    if token not in state.priority_tokens:
        return False
    index = state.priority_tokens.index(token)
    del state.priority_tokens[index]
    if index < state.priority_cursor:
        state.priority_cursor -= 1
    state.priority_cursor %= max(1, len(state.priority_tokens))
    state.priority_expires.pop(token, None)
    return True


def _prioritize(state: KeeperState, token: str) -> bool:
    if token in state.priority_tokens:
        return False
    if len(state.priority_tokens) >= MAX_PRIORITY_TOKENS:
        _drop_priority(state, state.priority_tokens[0])
    state.priority_tokens.append(token)
    state.priority_cursor = len(state.priority_tokens) - 1
    state.priority_expires[token] = state.schedule_round + PRIORITY_ROUNDS
    return True


def _expire_priority(state: KeeperState) -> bool:
    expired = [token for token in state.priority_tokens
               if state.priority_expires.get(token, 0) <= state.schedule_round]
    for token in expired:
        _drop_priority(state, token)
    return bool(expired)


def _remove_pending_token(state: KeeperState, token: str) -> None:
    _drop_priority(state, token)
    state.configured_plans.pop(token, None)
    index = state.tokens.index(token)
    del state.tokens[index]
    if index < state.scan_cursor:
        state.scan_cursor -= 1
    state.scan_cursor %= max(1, len(state.tokens))


def run_cycle(rpc: watch.Rpc, bindings: Bindings, state: KeeperState, store: KeeperStore,
              settings: PlanSettings, quotes: ExecutableQuoteProvider,
              signer: KeeperSigner | None, confirmations: int, block_span: int,
              process_next: bool, max_token_checks: int = 12,
              max_discovery_ranges: int = 2,
              max_plans: int = 2) -> dict[str, Any]:
    if max_token_checks < 1 or max_discovery_ranges < 1 or max_plans < 1:
        raise KeeperError("per-cycle work budgets must be positive")
    found = discover_launches(rpc, state, store, confirmations, block_span,
                              max_discovery_ranges)
    safe_head = watch.chain_head(rpc) - confirmations
    waiting: dict[str, int] = {}
    seen: set[str] = set()
    planned = 0
    # Two of every three slots favor newly found or already queued launches;
    # the third rotates across *all* pending tokens. Rotating the first slot
    # across cycles prevents early successful returns from starving either
    # lane. Both cursors and the phase survive a restart.
    schedule_phase = state.schedule_round % 3
    state.schedule_round += 1
    if _expire_priority(state):
        store.save(state)
    head_token = (urgent_open_token(rpc, bindings, state, settings, latest_block_time(rpc))
                  if signer is not None else None)
    head_refresh_seconds = min(180, max(30, settings.ttl_seconds // 2))
    def deferred_count() -> int:
        return sum(token not in seen for token in state.tokens)

    for slot in range(max_token_checks):
        priority = (schedule_phase + slot) % 3 != 2
        head_slot = (slot == 0 and head_token is not None and
                     (max_token_checks > 1 or schedule_phase != 2))
        token = head_token if head_slot else _take_scheduled(state, priority, seen)
        if token is None:
            token = _take_scheduled(state, not priority, seen)
        if token is None:
            break
        seen.add(token)
        store.save(state)
        stage, configured = q_launch_state(rpc, bindings.q, token, max(0, safe_head))
        if stage not in (0, 1):
            _remove_pending_token(state, token)
            store.save(state)
            continue
        if stage == 0:
            # The factory log can be confirmed before the owner's enqueue
            # transaction. Keep this fresh launch in its bounded priority
            # window so it is retried promptly once Q sees the enqueue.
            waiting["not_enqueued_yet"] = waiting.get("not_enqueued_yet", 0) + 1
            continue
        if _prioritize(state, token):
            store.save(state)
        if planned >= max_plans:
            waiting["plan_budget_deferred"] = waiting.get("plan_budget_deferred", 0) + 1
            continue
        planned += 1
        try:
            plan = make_plan(rpc, bindings, token, settings, quotes)
        except UnsupportedLaunch as error:
            _remove_pending_token(state, token)
            store.save(state)
            waiting[str(error)] = waiting.get(str(error), 0) + 1
            continue
        except WaitForPrice as error:
            reason = str(error)
            if reason == "Pons launch is in swept phase 1" and _drop_priority(state, token):
                store.save(state)
            waiting[reason] = waiting.get(reason, 0) + 1
            continue
        if signer is None:
            return {"status": "planned_read_only", "found": found, "pending": len(state.tokens),
                    "checked": len(seen), "planned": planned, "deferred": deferred_count(),
                    "plan": asdict(plan), "waiting": waiting}
        if q_launch_state(rpc, bindings.q, token)[0] != 1:
            waiting["launch_stage_changed_during_plan"] = waiting.get("launch_stage_changed_during_plan", 0) + 1
            continue
        # The public getter omits fixed arrays. The durable journal records
        # our confirmed calldata, and Q's latest plan-hash event proves that
        # those exact arrays have not been replaced since our transaction.
        should_configure = not configured_plan_reusable(
            rpc, bindings, state, plan, latest_block_time(rpc),
            head_refresh_seconds if head_slot else 30
        )
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
                "checked": len(seen), "planned": planned, "deferred": deferred_count(),
                "waiting": waiting}
    return {"status": "waiting", "found": found, "pending": len(state.tokens),
            "checked": len(seen), "planned": planned, "deferred": deferred_count(),
            "waiting": waiting}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("PONS_HTTP_RPC_URL"))
    parser.add_argument("--chain-id", type=int, default=os.environ.get("PONS_CHAIN_ID"))
    parser.add_argument("--q", default=os.environ.get("PONS_Q_ADDRESS"))
    parser.add_argument("--start-block", type=int)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--confirmations", type=int, default=3)
    parser.add_argument("--block-span", type=int, default=2000)
    parser.add_argument("--max-discovery-ranges", type=int, default=2)
    parser.add_argument("--max-token-checks", type=int, default=12)
    parser.add_argument("--max-plans-per-cycle", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--utilization-bps", type=int, default=9500)
    parser.add_argument("--tick-spacing", type=int, default=60)
    parser.add_argument("--ttl-seconds", type=int, default=600)
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=20)
    parser.add_argument("--max-config-gas", type=int, default=500000)
    parser.add_argument("--max-process-gas", type=int, default=10000000)
    parser.add_argument("--max-fee-gwei", default="5")
    parser.add_argument("--max-priority-gwei", default="1")
    parser.add_argument("--receipt-timeout", type=int, default=180)
    parser.add_argument("--no-process-next", action="store_true")
    parser.add_argument("--live", action="store_true", help="enable priceConfigurator-signed Q writes")
    parser.add_argument("--once", action="store_true", help="run one discovery/planning cycle")
    args = parser.parse_args(argv)
    if args.live and not args.once:
        parser.error("--live requires --once; run live price/exit cycles serially")
    if not args.http_url or not args.chain_id or not args.q:
        parser.error("HTTP URL, chain ID, and Q are required via flags or environment")
    if (args.confirmations < 1 or args.block_span < 1 or
            args.max_discovery_ranges < 1 or args.max_token_checks < 1 or
            args.max_plans_per_cycle < 1 or
            args.poll_seconds <= 0 or
            args.max_config_gas < 21000 or args.max_process_gas < 21000 or args.receipt_timeout < 1):
        parser.error("invalid confirmation, span, poll, gas, or receipt setting")
    settings = PlanSettings(args.utilization_bps, args.tick_spacing, args.ttl_seconds,
                            args.max_snapshot_age_seconds)
    settings.validate()
    q, guard = watch.address(args.q), ZERO
    rpc = watch.HttpRpc(args.http_url)
    private_key = os.environ.get("PONS_PRICE_CONFIGURATOR_PRIVATE_KEY") if args.live else None
    if args.live and not private_key:
        raise KeeperError("--live requires PONS_PRICE_CONFIGURATOR_PRIVATE_KEY in process environment")
    try:
        signer_address = Account.from_key(private_key).address.lower() if private_key else None
    except Exception as exc:
        raise KeeperError("priceConfigurator private key is invalid") from exc
    bindings = verify_bindings(rpc, args.chain_id, q, guard, QUOTER,
                               signer_address, None)
    quotes = None
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
                                   args.confirmations, args.block_span, not args.no_process_next,
                                   args.max_token_checks, args.max_discovery_ranges,
                                   args.max_plans_per_cycle)
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
