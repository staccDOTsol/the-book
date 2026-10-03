"""Offline price keeper tests; all RPC and transaction submission is mocked."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from eth_abi import decode, encode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_price_keeper as keeper
import pons_launch_watcher as watch


Q_LOW = "0x" + "11" * 20
Q_HIGH = "0x" + "33" * 20
X = "0x" + "22" * 20
EXE = "0x" + "44" * 20
GUARD = "0x" + "55" * 20
VIEW = "0x" + "66" * 20
HOOK = "0x" + "77" * 20
CURVE = "0x" + "88" * 20
POOL_ID = "0x" + "ab" * 32


def token(number: int) -> str:
    return "0x" + f"{number:040x}"


def bindings(q=Q_LOW):
    return keeper.Bindings(q, EXE, GUARD, VIEW, POOL_ID, 2500, 25, HOOK, keeper.QUOTER)


def launch(phase: int):
    return (X, CURVE, Q_LOW, Q_LOW, keeper.ZERO, 0, 3000, 60, 50,
            False, phase, 0, 0, 0, True)


def log(block: int, token: str):
    word = "0x" + token[2:].rjust(64, "0")
    return {"address": watch.PONS_FACTORY,
            "topics": [watch.TOKEN_LAUNCHED, word, word, word],
            "blockNumber": hex(block), "transactionIndex": "0x0", "logIndex": "0x0",
            "blockHash": "0x" + f"{block:064x}"}


class FakeStore:
    def __init__(self): self.saved = []
    def save(self, state): self.saved.append(copy.deepcopy(state.json()))


class LogRpc:
    def __init__(self, events=None, head=15):
        self.events, self.head, self.calls = events or [], head, []
        self.reorg = {}

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber": return hex(self.head)
        if method == "eth_getBlockByNumber":
            if params[0] == "latest": return {"timestamp": "0x64", "baseFeePerGas": "0x1"}
            number = int(params[0], 16)
            return {"hash": self.reorg.get(number, "0x" + f"{number:064x}")}
        if method == "eth_getLogs":
            first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return [row for row in self.events if first <= int(row["blockNumber"], 16) <= last]
        raise AssertionError(f"unexpected RPC {method}")


class KeeperTests(unittest.TestCase):
    def test_verify_bindings_requires_matching_executable_depth_guard(self):
        depth = "0x" + "99" * 20
        router = "0x" + "aa" * 20
        table = {
            (Q_LOW, keeper.EXECUTOR): EXE,
            (Q_LOW, watch.PONS_FACTORY_GETTER): watch.PONS_FACTORY,
            (EXE, keeper.PRICE_GUARD): GUARD,
            (GUARD, keeper.QUOTE_TOKEN): Q_LOW,
            (GUARD, watch.PONS_FACTORY_GETTER): watch.PONS_FACTORY,
            (GUARD, keeper.STATE_VIEW): VIEW,
            (watch.PONS_FACTORY, keeper.MEME_HOOK): HOOK,
            (EXE, keeper.DEPTH_GUARD): depth,
            (EXE, keeper.SETTLEMENT_ROUTER): router,
            (depth, keeper.DEPTH_SOURCE): EXE,
            (depth, keeper.PRICE_GUARD): GUARD,
            (depth, keeper.SETTLEMENT_ROUTER): router,
            (depth, keeper.QUOTE_TOKEN): Q_LOW,
            (depth, watch.PONS_FACTORY_GETTER): watch.PONS_FACTORY,
            (depth, keeper.DEPTH_QUOTER): keeper.QUOTER,
        }
        class Rpc:
            def call(self, method, params):
                if method == "eth_chainId": return "0x1237"
                if method == "eth_getCode": return "0x6001"
                if method == "eth_call": return POOL_ID
                raise AssertionError(method)
        def uint(_rpc, contract, method, _type="uint256"):
            return {keeper.QUOTE_ETH_FEE: 2500, keeper.QUOTE_ETH_SPACING: 25,
                    keeper.DEPTH_SAFETY_BPS: 1500}[method]
        with patch.object(keeper, "read_address", side_effect=lambda _rpc, contract, method: table[(contract, method)]), \
             patch.object(keeper, "read_uint", side_effect=uint):
            self.assertEqual(keeper.verify_bindings(Rpc(), 4663, Q_LOW, GUARD,
                                                     keeper.QUOTER, None).depth_guard, depth)
            with self.assertRaisesRegex(keeper.KeeperError, "haircut differs"):
                keeper.verify_bindings(Rpc(), 4663, Q_LOW, GUARD, keeper.QUOTER,
                                       None, 1400)
            table[(depth, keeper.DEPTH_QUOTER)] = X
            with self.assertRaisesRegex(keeper.KeeperError, "depth guard configuration"):
                keeper.verify_bindings(Rpc(), 4663, Q_LOW, GUARD, keeper.QUOTER, None)

    def test_tick_math_matches_v4_bounds(self):
        self.assertEqual(keeper.sqrt_at_tick(0), keeper.Q96)
        self.assertEqual(keeper.sqrt_at_tick(keeper.MIN_TICK), keeper.MIN_SQRT)
        self.assertEqual(keeper.sqrt_at_tick(keeper.MAX_TICK), keeper.MAX_SQRT)
        self.assertEqual(keeper.floor_tick(keeper.Q96), 0)
        self.assertEqual(keeper.floor_tick(keeper.sqrt_at_tick(60)), 60)

    def test_pinned_rpc_rewrites_all_planning_state_reads(self):
        class Rpc:
            def __init__(self): self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                return "0x"
        rpc = Rpc()
        pinned = keeper.PinnedRpc(rpc, 123)
        pinned.call("eth_call", [{"to": Q_LOW, "data": keeper.REFERENCE}, "latest"])
        pinned.call("eth_getBlockByNumber", ["latest", False])
        pinned.call("eth_blockNumber", [])
        self.assertEqual(rpc.calls[0][1][1], hex(123))
        self.assertEqual(rpc.calls[1][1][0], hex(123))
        self.assertEqual(rpc.calls[2][1], [])

    def test_pinned_quote_provider_passes_block_to_quoter(self):
        class Rpc:
            def __init__(self): self.tag = None
            def call(self, method, params):
                self.tag = params[1]
                return "0x" + encode(["uint256", "uint256"], [2, 1000]).hex()
        rpc = Rpc()
        provider = keeper.ExecutableQuoteProvider(rpc, bindings()).at_block(123)
        provider.quote_single((keeper.ZERO, Q_LOW, 2500, 25, keeper.ZERO), True, 1)
        self.assertEqual(rpc.tag, hex(123))

    def test_same_height_reorg_during_plan_is_retried(self):
        class Rpc:
            def __init__(self): self.hash_reads = 0
            def call(self, method, params):
                if method == "eth_blockNumber": return "0x20"
                if method == "eth_getBlockByNumber":
                    self.hash_reads += 1
                    return {"hash": "0x" + ("aa" if self.hash_reads < 3 else "bb") * 32,
                            "timestamp": "0x64"}
                raise AssertionError(method)
        class Quotes:
            def quote_x_to_q(self, _token, _launch, x): return x * 9 // 10
        rpc = Rpc()
        with patch.object(keeper, "read_launch", return_value=launch(0)), \
             patch.object(keeper, "quote_eth_liquidity", return_value=1), \
             patch.object(keeper, "guard_reference", return_value=keeper.Q96), \
             patch.object(keeper, "q_vault_balance", return_value=10**18):
            with self.assertRaisesRegex(keeper.WaitForPrice, "block changed"):
                keeper.make_plan(rpc, bindings(), X, keeper.PlanSettings(), Quotes())
        self.assertEqual(rpc.hash_reads, 3)

    def test_q_only_plan_both_address_orderings_uses_budgeted_quote_size(self):
        idle = 10**18
        settings = keeper.PlanSettings()
        for q in (Q_LOW, Q_HIGH):
            with self.subTest(q=q):
                samples = []
                def quote(x):
                    samples.append(x)
                    return x * 9 // 10
                plan = keeper.plan_position(X, q, keeper.Q96, idle, quote, 100, settings)
                budget = idle // 4
                self.assertEqual(plan.max_quote_in, budget)
                self.assertEqual(samples[0], budget * settings.quote_size_multiple)
                self.assertEqual(samples[-1], plan.sample_x_in)
                self.assertEqual(plan.safe_q_out, plan.executable_q_out * 85 // 100)
                lo, hi = keeper.sqrt_at_tick(plan.tick_lower), keeper.sqrt_at_tick(plan.tick_upper)
                self.assertEqual(plan.tick_lower % settings.tick_spacing, 0)
                self.assertEqual(plan.tick_upper % settings.tick_spacing, 0)
                if plan.quote_is_0:
                    self.assertLessEqual(plan.starting_sqrt_price_x96, lo)
                    self.assertGreaterEqual(lo * lo * plan.safe_q_out, plan.sample_x_in * keeper.Q192)
                    spent = keeper.amount0_ceil(plan.liquidity, lo, hi)
                    full_x = keeper.amount1_ceil(plan.liquidity, lo, hi)
                else:
                    self.assertGreaterEqual(plan.starting_sqrt_price_x96, hi)
                    self.assertLessEqual(hi * hi * plan.sample_x_in, plan.safe_q_out * keeper.Q192)
                    spent = keeper.amount1_ceil(plan.liquidity, lo, hi)
                    full_x = keeper.amount0_ceil(plan.liquidity, lo, hi)
                self.assertEqual(plan.sample_x_in, full_x)
                self.assertLessEqual(spent, budget * settings.utilization_bps // 10000)
                self.assertLessEqual(spent, plan.max_quote_in)
                self.assertGreater(spent, 0)

    def test_thin_route_full_band_exposure_fails_closed_in_both_orderings(self):
        # A quote for 2*budget X is insufficient: the shifted full band can
        # acquire far more X, and a thin constant-product exit loses to impact.
        reserve = 2 * 10**17
        for q in (Q_LOW, Q_HIGH):
            with self.subTest(q=q):
                quoted = []
                def quote(x):
                    quoted.append(x)
                    return reserve * x // (reserve + x)
                with self.assertRaisesRegex(keeper.WaitForPrice, "full X exposure"):
                    keeper.plan_position(X, q, keeper.Q96, 10**18, quote, 100,
                                         keeper.PlanSettings())
                self.assertGreater(max(quoted), 2 * (10**18 // 4))

    def test_no_quote_or_idle_balance_fails_closed(self):
        with self.assertRaisesRegex(keeper.WaitForPrice, "idle Q"):
            keeper.plan_position(X, Q_LOW, keeper.Q96, 0, lambda x: x, 100, keeper.PlanSettings())
        with self.assertRaisesRegex(keeper.WaitForPrice, "too little Q"):
            keeper.plan_position(X, Q_LOW, keeper.Q96, 10**18, lambda _x: 0, 100, keeper.PlanSettings())
        with self.assertRaises(keeper.WaitForPrice):
            keeper.plan_position(X, Q_LOW, keeper.Q96, 10**18, lambda _x: 1, 100, keeper.PlanSettings())

    def test_guard_spot_check_is_separate_from_executable_sale_floor(self):
        class Rpc:
            def __init__(self): self.methods = []
            def call(self, method, params):
                data = params[0]["data"]
                self.methods.append(data[:10])
                return "0x" + encode(["uint160"], [keeper.Q96]).hex()
        rpc = Rpc()
        self.assertEqual(keeper.guard_reference(rpc, bindings(), X), keeper.Q96)
        self.assertEqual(rpc.methods, [keeper.REFERENCE, keeper.VALIDATE])

    def test_phase_one_and_empty_qeth_liquidity_wait_without_plan(self):
        rpc = LogRpc()
        with patch.object(keeper, "read_launch", return_value=launch(1)), \
             patch.object(keeper, "quote_eth_liquidity", side_effect=AssertionError("must not quote")):
            with self.assertRaisesRegex(keeper.WaitForPrice, "phase 1"):
                keeper.make_plan(rpc, bindings(), X, keeper.PlanSettings(), object())
        with patch.object(keeper, "read_launch", return_value=launch(0)), \
             patch.object(keeper, "quote_eth_liquidity", return_value=0):
            with self.assertRaisesRegex(keeper.WaitForPrice, "no liquidity"):
                keeper.make_plan(rpc, bindings(), X, keeper.PlanSettings(), object())

    def test_phase_zero_sell_formula_and_v4_eth_to_q(self):
        class Rpc:
            def __init__(self): self.keys = []
            def call(self, method, params):
                self.assert_method(method)
                data = params[0]["data"]
                if data == keeper.GET_RESERVES:
                    return "0x" + encode(["uint256", "uint256"], [100 * 10**18, 1000 * 10**18]).hex()
                if data == keeper.FEE_BPS: return "0x" + encode(["uint256"], [100]).hex()
                if data == keeper.CREATOR_TAX_BPS: return "0x" + encode(["uint256"], [50]).hex()
                if data.startswith(keeper.QUOTE_EXACT_INPUT_SINGLE):
                    key, zero_for_one, amount, _ = decode(
                        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
                        bytes.fromhex(data[10:]))[0]
                    self.keys.append((key, zero_for_one, amount))
                    return "0x" + encode(["uint256", "uint256"], [amount * 100, 200000]).hex()
                raise AssertionError(data)
            def assert_method(self, method):
                if method != "eth_call": raise AssertionError(method)
        rpc = Rpc()
        provider = keeper.ExecutableQuoteProvider(rpc, bindings())
        x_in = 10**18
        got = provider.quote_x_to_q(X, launch(0), x_in)
        gross = x_in * 10000 * (100 * 10**18) // (1000 * 10**18 * 10000 + x_in * 10000)
        eth = gross - gross * 100 // 10000 - gross * 50 // 10000
        self.assertEqual(got, eth * 100)
        self.assertEqual(rpc.keys[0][1:], (True, eth))
        self.assertEqual(rpc.keys[0][0][0], keeper.ZERO)
        self.assertEqual(rpc.keys[0][0][1], Q_LOW)

    def test_phase_two_v4_quote_directions(self):
        class Rpc:
            def __init__(self): self.calls = []
            def call(self, method, params):
                value = decode(["((address,address,uint24,int24,address),bool,uint128,bytes)"],
                               bytes.fromhex(params[0]["data"][10:]))[0]
                self.calls.append(value)
                return "0x" + encode(["uint256", "uint256"], [value[2] * 2, 1]).hex()
        rpc = Rpc()
        got = keeper.ExecutableQuoteProvider(rpc, bindings()).quote_x_to_q(X, launch(2), 123)
        self.assertEqual(got, 492)
        self.assertEqual([v[1] for v in rpc.calls], [False, True])
        self.assertEqual(rpc.calls[0][0], (keeper.ZERO, X, 3000, 60, HOOK))
        self.assertEqual(rpc.calls[1][0], (keeper.ZERO, Q_LOW, 2500, 25, keeper.ZERO))

    def test_vault_budget_excludes_harvested_quote_reserve(self):
        class Rpc:
            def __init__(self, reserved): self.reserved = reserved
            def call(self, method, params):
                if params[0]["to"] == Q_LOW:
                    return "0x" + encode(["uint256"], [1000]).hex()
                return "0x" + encode(["uint256"], [self.reserved]).hex()
        self.assertEqual(keeper.q_vault_balance(Rpc(250), bindings()), 750)
        with self.assertRaisesRegex(keeper.KeeperError, "exceeds"):
            keeper.q_vault_balance(Rpc(1001), bindings())

    def test_durable_log_cursor_keeps_pending_tokens_until_q_enqueues(self):
        rpc = LogRpc([log(11, X)], head=14)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        store = FakeStore()
        self.assertEqual(keeper.discover_launches(rpc, state, store, 2, 100), 1)
        self.assertEqual(state.last_block, 12)
        self.assertEqual(state.tokens, [X])
        self.assertEqual(keeper.discover_launches(rpc, state, store, 2, 100), 0)
        self.assertEqual(state.tokens, [X])
        with patch.object(keeper, "q_launch_state", return_value=(0, False)):
            result = keeper.run_cycle(rpc, bindings(), state, store, keeper.PlanSettings(),
                                      object(), None, 2, 100, True)
        self.assertEqual(result["waiting"]["not_enqueued_yet"], 1)
        self.assertEqual(state.tokens, [X])

    def test_cursor_hash_reorg_stops_discovery(self):
        rpc = LogRpc([log(11, X)], head=14)
        rpc.reorg[10] = "0x" + "ff" * 32
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        with self.assertRaisesRegex(keeper.KeeperError, "cursor block hash changed"):
            keeper.discover_launches(rpc, state, FakeStore(), 2, 100)

    def test_discovery_caps_ranges_and_resumes_with_reorg_check(self):
        later = token(100)
        rpc = LogRpc([log(11, X), log(21, later)], head=32)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        store = FakeStore()
        self.assertEqual(keeper.discover_launches(rpc, state, store, 2, 5, 1), 1)
        self.assertEqual(state.last_block, 15)
        self.assertEqual(state.tokens, [X])
        self.assertEqual(len([call for call in rpc.calls if call[0] == "eth_getLogs"]), 1)
        rpc.reorg[15] = "0x" + "ff" * 32
        with self.assertRaisesRegex(keeper.KeeperError, "cursor block hash changed"):
            keeper.discover_launches(rpc, state, store, 2, 5, 1)
        self.assertEqual(state.last_block, 15)
        rpc.reorg.clear()
        keeper.discover_launches(rpc, state, store, 2, 5, 1)
        self.assertEqual(state.last_block, 20)
        keeper.discover_launches(rpc, state, store, 2, 5, 1)
        self.assertIn(later, state.tokens)

    def test_reorg_during_range_does_not_stage_uncommitted_tokens(self):
        class ReorgRpc(LogRpc):
            def __init__(self):
                super().__init__([log(11, X)], head=14)
                self.tip_hash_reads = 0
            def call(self, method, params):
                if method == "eth_getBlockByNumber" and params[0] == hex(12):
                    self.tip_hash_reads += 1
                    if self.tip_hash_reads == 3:
                        return {"hash": "0x" + "ff" * 32}
                return super().call(method, params)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        with self.assertRaisesRegex(keeper.KeeperError, "range changed"):
            keeper.discover_launches(ReorgRpc(), state, FakeStore(), 2, 10, 1)
        self.assertEqual(state.last_block, 10)
        self.assertEqual(state.tokens, [])
        self.assertEqual(state.priority_tokens, [])

    def test_dense_discovery_splits_range_and_bounds_priority_membership(self):
        events = [log(11 + index // 40, token(index + 1)) for index in range(160)]
        rpc = LogRpc(events, head=16)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        self.assertEqual(keeper.discover_launches(rpc, state, FakeStore(), 1, 4, 1), 80)
        self.assertEqual(state.last_block, 12)
        self.assertEqual(len(state.tokens), 80)
        self.assertEqual(len(state.priority_tokens), keeper.MAX_PRIORITY_TOKENS)
        ranges = [(int(call[1][0]["fromBlock"], 16), int(call[1][0]["toBlock"], 16))
                  for call in rpc.calls if call[0] == "eth_getLogs"]
        self.assertEqual(ranges, [(11, 14), (11, 12)])
        state.schedule_round = keeper.PRIORITY_ROUNDS
        self.assertTrue(keeper._expire_priority(state))
        self.assertEqual(state.priority_tokens, [])
        self.assertEqual(len(state.tokens), 80)

    def test_single_overfull_block_never_advances_cursor(self):
        events = [log(11, token(index + 1)) for index in range(keeper.MAX_DISCOVERY_LOGS + 1)]
        rpc = LogRpc(events, head=12)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [])
        with self.assertRaisesRegex(keeper.KeeperError, "single launch block"):
            keeper.discover_launches(rpc, state, FakeStore(), 1, 1, 1)
        self.assertEqual(state.last_block, 10)
        self.assertEqual(state.tokens, [])

    def test_token_check_budget_defers_backlog_without_repeating_in_cycle(self):
        tokens = [token(index) for index in range(1, 21)]
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", tokens)
        seen = []
        def stage(_rpc, _q, candidate, *_args):
            seen.append(candidate)
            return 0, False
        with patch.object(keeper, "q_launch_state", side_effect=stage):
            result = keeper.run_cycle(LogRpc(head=12), bindings(), state, FakeStore(),
                                      keeper.PlanSettings(), object(), None,
                                      2, 100, True, 3, 1)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["deferred"], 17)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 3)

    def test_expensive_plan_budget_caps_ready_backlog(self):
        tokens = [token(index) for index in range(1, 11)]
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", tokens)
        with patch.object(keeper, "q_launch_state", return_value=(1, False)) as stages, \
             patch.object(keeper, "make_plan",
                          side_effect=keeper.WaitForPrice("temporary quote wait")) as plans:
            result = keeper.run_cycle(LogRpc(head=12), bindings(), state, FakeStore(),
                                      keeper.PlanSettings(), object(), None,
                                      2, 100, True, 5, 1, 2)
        self.assertEqual(stages.call_count, 5)
        self.assertEqual(plans.call_count, 2)
        self.assertEqual(result["planned"], 2)
        self.assertEqual(result["waiting"]["plan_budget_deferred"], 3)

    def test_fresh_launch_waits_in_priority_for_owner_enqueue(self):
        fresh = token(901)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}",
                                   [fresh], priority_tokens=[fresh],
                                   priority_expires={fresh: keeper.PRIORITY_ROUNDS})
        rpc = LogRpc(head=12)
        plan = keeper.OpenPlan(fresh, 1, 1, 1, 60, 0, 60, 100,
                               1, 1, 1, True)
        with patch.object(keeper, "q_launch_state", return_value=(0, False)):
            first = keeper.run_cycle(rpc, bindings(), state, FakeStore(),
                                     keeper.PlanSettings(), object(), None,
                                     2, 100, True, 1, 1)
        self.assertEqual(first["waiting"]["not_enqueued_yet"], 1)
        self.assertIn(fresh, state.priority_tokens)
        with patch.object(keeper, "q_launch_state", return_value=(1, False)), \
             patch.object(keeper, "make_plan", return_value=plan):
            second = keeper.run_cycle(rpc, bindings(), state, FakeStore(),
                                      keeper.PlanSettings(), object(), None,
                                      2, 100, True, 1, 1)
        self.assertEqual(second["status"], "planned_read_only")
        self.assertEqual(second["plan"]["token"], fresh)

    def test_priority_action_cannot_starve_background_with_one_check_budget(self):
        tokens = [token(index) for index in range(1, 31)]
        hot = tokens[-1]
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}",
                                   tokens, priority_tokens=[hot],
                                   priority_expires={hot: keeper.PRIORITY_ROUNDS})
        rpc = LogRpc(head=12)
        calls = []
        plan = keeper.OpenPlan(hot, 1, 1, 1, 60, 0, 60, 100,
                               1, 1, 1, True)
        def stage(_rpc, _q, candidate, *_args):
            calls.append(candidate)
            return (1, False) if candidate == hot else (0, False)
        with patch.object(keeper, "q_launch_state", side_effect=stage), \
             patch.object(keeper, "make_plan", return_value=plan):
            for _ in range(3 * len(tokens) + 10):
                before = len(calls)
                result = keeper.run_cycle(rpc, bindings(), state, FakeStore(),
                                          keeper.PlanSettings(), object(), None,
                                          2, 100, True, 1, 1)
                self.assertEqual(result["checked"], 1)
                self.assertEqual(len(calls) - before, 1)
        self.assertEqual(calls[:2], [hot, hot])
        self.assertTrue(set(tokens).issubset(calls))
        self.assertLessEqual(len(state.priority_tokens), keeper.MAX_PRIORITY_TOKENS)

    def test_terminal_removal_preserves_both_schedule_cursors(self):
        a, b, c = token(1), token(2), token(3)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}",
                                   [a, b, c], priority_tokens=[b, c],
                                   scan_cursor=2, priority_cursor=1,
                                   priority_expires={b: 9, c: 9})
        keeper._remove_pending_token(state, b)
        self.assertEqual((state.tokens, state.priority_tokens,
                          state.scan_cursor, state.priority_cursor),
                         ([a, c], [c], 1, 0))
        keeper._remove_pending_token(state, c)
        self.assertEqual((state.tokens, state.priority_tokens,
                          state.scan_cursor, state.priority_cursor),
                         ([a], [], 0, 0))

    def test_selector_restriction_and_pending_raw_tx_recovery(self):
        account = keeper.Account.from_key("0x" + "01" * 32)
        class Rpc:
            def __init__(self):
                self.receipt = None
                self.sent = []
            def call(self, method, params):
                if method == "eth_estimateGas": return "0x186a0"
                if method == "eth_getTransactionCount": return "0x0"
                if method == "eth_getBlockByNumber":
                    if params[0] == "latest": return {"baseFeePerGas": "0x1", "timestamp": "0x64"}
                    return {"hash": "0x" + f"{int(params[0], 16):064x}"}
                if method == "eth_maxPriorityFeePerGas": return "0x1"
                if method == "eth_sendRawTransaction":
                    self.sent.append(params[0])
                    self.receipt = {"status": "0x1", "blockNumber": "0x13",
                                    "blockHash": "0x" + f"{19:064x}"}
                    return "0x" + keeper.keccak(bytes.fromhex(params[0][2:])).hex()
                if method == "eth_getTransactionReceipt": return self.receipt
                if method == "eth_getTransactionByHash": return None
                if method == "eth_blockNumber": return "0x16"
                raise AssertionError(method)
        rpc = Rpc()
        signer = keeper.KeeperSigner(rpc, bindings(), 1, "0x" + "01" * 32,
                                     2, 500000, 3500000, 10**9, 10**9, 1, .001)
        state = keeper.KeeperState(1, Q_LOW, GUARD, 10, "0x" + f"{10:064x}", [X])
        store = FakeStore()
        with self.assertRaisesRegex(keeper.KeeperError, "non-keeper"):
            signer.submit("other", X, "0x12345678", state, store)
        signer.submit("process", X, keeper.PROCESS_NEXT, state, store)
        self.assertEqual(len(rpc.sent), 1)
        self.assertIsNone(state.pending_tx)
        self.assertEqual([s["pendingTx"] is not None for s in store.saved], [True, False])
        raw = rpc.sent[0]
        signed = keeper.Account.recover_transaction(raw).lower()
        self.assertEqual(signed, account.address.lower())
        decoded = keeper.rlp.decode(bytes.fromhex(raw[4:]))
        self.assertEqual(decoded[7], bytes.fromhex(keeper.PROCESS_NEXT[2:]))
        # Simulate a crash after persisting but before broadcasting a fresh
        # same-nonce transaction. Recovery rebroadcasts the exact raw payload.
        pending = store.saved[0]["pendingTx"]
        state.pending_tx = pending
        rpc.receipt = None
        rpc.sent.clear()
        signer.recover(state, store, keeper.PlanSettings(), object())
        self.assertEqual(rpc.sent, [pending["rawTx"]])
        self.assertIsNone(state.pending_tx)

    def test_state_file_is_local_and_mode_600(self):
        path = keeper.LOCAL / f"test-pons-price-{uuid4().hex}.json"
        rpc = LogRpc()
        try:
            with keeper.KeeperStore(path) as store:
                state = store.load(rpc, 1, Q_LOW, GUARD, 11)
                state.tokens.append(X)
                state.priority_tokens.append(X)
                state.priority_expires[X] = 16
                state.schedule_round = 7
                store.save(state)
            with keeper.KeeperStore(path) as store:
                restored = store.load(rpc, 1, Q_LOW, GUARD, None)
            self.assertEqual(restored.tokens, [X])
            self.assertEqual(restored.priority_tokens, [X])
            self.assertEqual(restored.priority_expires, {X: 16})
            self.assertEqual(restored.schedule_round, 7)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            legacy = restored.json()
            for field_name in ("priorityTokens", "priorityExpires", "scanCursor",
                               "priorityCursor", "scheduleRound"):
                legacy.pop(field_name)
            path.write_text(json.dumps(legacy))
            with keeper.KeeperStore(path) as store:
                migrated = store.load(rpc, 1, Q_LOW, GUARD, None)
            self.assertEqual(migrated.tokens, [X])
            self.assertEqual(migrated.priority_tokens, [])
        finally:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
