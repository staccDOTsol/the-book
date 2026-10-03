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
    def test_verify_bindings_uses_q_factory_and_router_without_fmv_gate(self):
        router = "0x" + "aa" * 20
        policy = "0x" + "bb" * 20
        table = {
            (Q_LOW, keeper.EXECUTOR): EXE,
            (Q_LOW, watch.PONS_FACTORY_GETTER): watch.PONS_FACTORY,
            (EXE, keeper.CONTROLLER): Q_LOW,
            (EXE, keeper.QUOTE_TOKEN): Q_LOW,
            (EXE, keeper.SETTLEMENT_ROUTER): router,
            (EXE, keeper.POOL_MANAGER): keeper.POOL_MANAGER_ADDRESS,
            (EXE, keeper.POSITION_MANAGER): keeper.POSITION_MANAGER_ADDRESS,
            (EXE, keeper.STATE_VIEW): keeper.STATE_VIEW_ADDRESS,
            (Q_LOW, keeper.FEE_POLICY): policy,
            (policy, keeper.CONTROLLER): Q_LOW,
            (router, keeper.ROUTER_SOURCE): EXE,
            (router, keeper.QUOTE_TOKEN): Q_LOW,
            (router, keeper.ROUTER_FACTORY): watch.PONS_FACTORY,
        }
        class Rpc:
            def call(self, method, params):
                if method == "eth_chainId": return "0x1237"
                if method == "eth_getCode": return "0x6001"
                raise AssertionError((method, params))
        with patch.object(keeper, "read_address", side_effect=lambda _rpc, contract, method: table[(contract, method)]), \
             patch.object(keeper, "read_uint", side_effect=lambda _rpc, _contract, method, _type="uint256": {
                 keeper.OPEN_MINT_BPS: 100,
                 keeper.MIN_FEE_PIPS: 50_000,
                 keeper.MAX_FEE_PIPS: 500_000,
                 keeper.FEE_STEP_PIPS: 10_000,
             }[method]):
            bound = keeper.verify_bindings(Rpc(), 4663, Q_LOW, keeper.ZERO,
                                           keeper.QUOTER, None)
            self.assertEqual((bound.executor, bound.guard, bound.depth_guard),
                             (EXE, keeper.ZERO, keeper.ZERO))
            table[(EXE, keeper.QUOTE_TOKEN)] = X
            with self.assertRaisesRegex(keeper.KeeperError, "controller/Q"):
                keeper.verify_bindings(Rpc(), 4663, Q_LOW, keeper.ZERO,
                                       keeper.QUOTER, None)

    def test_tick_math_and_exact_ratio_bounds(self):
        self.assertEqual(keeper.sqrt_at_tick(0), keeper.Q96)
        self.assertEqual(keeper.sqrt_at_tick(keeper.MIN_TICK), keeper.MIN_SQRT)
        self.assertEqual(keeper.sqrt_at_tick(keeper.MAX_TICK), keeper.MAX_SQRT)
        self.assertEqual(keeper.floor_tick(keeper.Q96), 0)
        self.assertEqual(keeper.floor_tick_ratio(1, 1), 0)
        self.assertEqual(keeper.ceil_tick_ratio(1, 1), 0)
        self.assertLessEqual(keeper.sqrt_at_tick(keeper.floor_tick_ratio(1, 2)) ** 2 * 2,
                             keeper.Q192)

    def test_pinned_rpc_rewrites_all_planning_state_reads(self):
        class Rpc:
            def __init__(self): self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                return "0x"
        rpc = Rpc()
        pinned = keeper.PinnedRpc(rpc, 123)
        pinned.call("eth_call", [{"to": Q_LOW, "data": keeper.TOTAL_SUPPLY}, "latest"])
        pinned.call("eth_getBlockByNumber", ["latest", False])
        self.assertEqual(rpc.calls[0][1][1], hex(123))
        self.assertEqual(rpc.calls[1][1][0], hex(123))

    def test_three_bands_use_p0_and_pons_multiplier_for_both_token_orders(self):
        supply = 10**27
        x_supply = 10**27
        expected_mints = (10**25, 101 * 10**23, 10201 * 10**21)
        for q in (Q_LOW, Q_HIGH):
            with self.subTest(q=q):
                plan = keeper.plan_position(X, q, x_supply, supply,
                                            10**18, 2 * 10**18, 100,
                                            keeper.PlanSettings())
                self.assertEqual(plan.minted_quote, expected_mints)
                self.assertEqual(plan.max_quote_in, expected_mints)
                self.assertEqual(plan.deadline, 220)
                self.assertEqual(len(plan.liquidity), 3)
                for i, multiple in enumerate((1, 2, 10)):
                    lo = keeper.sqrt_at_tick(plan.tick_lower[i])
                    hi = keeper.sqrt_at_tick(plan.tick_upper[i])
                    self.assertEqual(plan.tick_lower[i] % 60, 0)
                    self.assertEqual(plan.tick_upper[i] % 60, 0)
                    if plan.quote_is_0:
                        # Reciprocal X/Q: p0=.01, R=9. Ranges are rounded outward.
                        self.assertLessEqual(lo * lo * multiple * 9,
                                             100 * keeper.Q192)
                        self.assertGreaterEqual(hi * hi, 100 * keeper.Q192)
                        self.assertLess(plan.starting_sqrt_price_x96, lo)
                        spent = keeper.amount0_ceil(plan.liquidity[i], lo, hi)
                    else:
                        self.assertLessEqual(lo * lo * 100, keeper.Q192)
                        self.assertGreaterEqual(hi * hi * 100,
                                                multiple * 9 * keeper.Q192)
                        self.assertGreater(plan.starting_sqrt_price_x96, hi)
                        spent = keeper.amount1_ceil(plan.liquidity[i], lo, hi)
                    self.assertGreater(spent, 0)
                    self.assertLessEqual(spent, expected_mints[i] * 95 // 100)
                    self.assertLessEqual(spent, plan.max_quote_in[i])
                if plan.quote_is_0:
                    self.assertGreater(plan.tick_lower[0], plan.tick_lower[1])
                    self.assertGreater(plan.tick_lower[1], plan.tick_lower[2])
                else:
                    self.assertLess(plan.tick_upper[0], plan.tick_upper[1])
                    self.assertLess(plan.tick_upper[1], plan.tick_upper[2])
                self.assertEqual(keeper.decode(keeper.CONFIG_TYPES,
                                 keeper.encode(keeper.CONFIG_TYPES, plan.abi_config()))[1],
                                 plan.liquidity)

    def test_invalid_static_economics_fail_closed(self):
        args = (X, Q_LOW, 10**27, 10**27, 10**18, 2 * 10**18, 100, keeper.PlanSettings())
        for index, value in ((2, 0), (3, 0), (4, 0), (5, 0)):
            with self.subTest(index=index):
                altered = list(args)
                altered[index] = value
                with self.assertRaises(keeper.WaitForPrice):
                    keeper.plan_position(*altered)
        with self.assertRaisesRegex(keeper.WaitForPrice, "tick range"):
            keeper.plan_position(X, Q_LOW, 10**27, 10**27, 1, 10**80,
                                 100, keeper.PlanSettings())

    def test_make_plan_authenticates_curve_and_ignores_qeth_fmv(self):
        record = list(launch(0))
        record[5] = 2 * 10**18
        calls = []
        class Rpc(LogRpc):
            def call(self, method, params):
                if method == "eth_getCode": return "0x6001"
                if method == "eth_getBlockByNumber" and params[0] == hex(self.head):
                    return {"hash": "0x" + f"{self.head:064x}", "timestamp": "0x64"}
                return super().call(method, params)
        def address(_rpc, contract, method):
            calls.append((contract, method))
            return {
                (CURVE, keeper.ROUTER_FACTORY): watch.PONS_FACTORY,
                (CURVE, keeper.CURVE_TOKEN): X,
                (CURVE, keeper.CURVE_PAIR_TOKEN): keeper.ZERO,
            }[(contract, method)]
        def number(_rpc, contract, method, _type="uint256"):
            return {
                (CURVE, keeper.CURVE_GRADUATION_THRESHOLD): 2 * 10**18,
                (CURVE, keeper.CURVE_REAL_QUOTE): 5 * 10**17,
                (X, keeper.TOTAL_SUPPLY): 10**27,
                (Q_LOW, keeper.TOTAL_SUPPLY): 10**27,
            }[(contract, method)]
        def abi(_rpc, contract, method, *_args, **_kwargs):
            if (contract, method) == (CURVE, keeper.GET_RESERVES):
                return (15 * 10**17, 9 * 10**26)
            if (contract, method) == (EXE, keeper.ACTIVE_POSITION_COUNT):
                return (0,)
            raise AssertionError((contract, method))
        rpc = Rpc(head=32)
        class ReorgRpc(Rpc):
            def __init__(self):
                super().__init__(head=32)
                self.block_reads = 0
            def call(self, method, params):
                if method == "eth_getBlockByNumber" and params[0] == hex(self.head):
                    self.block_reads += 1
                    return {"hash": "0x" + ("ff" if self.block_reads == 3 else "00") * 32,
                            "timestamp": "0x64"}
                return super().call(method, params)
        with patch.object(keeper, "read_launch", return_value=tuple(record)), \
             patch.object(keeper, "read_address", side_effect=address), \
             patch.object(keeper, "read_uint", side_effect=number), \
             patch.object(keeper, "call_abi", side_effect=abi):
            plan = keeper.make_plan(rpc, bindings(), X, keeper.PlanSettings())
            with self.assertRaisesRegex(keeper.WaitForPrice, "block changed"):
                keeper.make_plan(ReorgRpc(), bindings(), X, keeper.PlanSettings())
        self.assertEqual(plan.phantom_quote, 10**18)
        self.assertFalse(any(contract == GUARD for contract, _ in calls))
        self.assertFalse(any(method == "eth_call" for method, _ in rpc.calls))
        record[4] = Q_LOW
        with patch.object(keeper, "read_launch", return_value=tuple(record)):
            with self.assertRaisesRegex(keeper.UnsupportedLaunch, "native ETH"):
                keeper.make_plan(rpc, bindings(), X, keeper.PlanSettings())

    def test_phase_one_waits_without_curve_reads(self):
        with patch.object(keeper, "read_launch", return_value=launch(1)), \
             patch.object(keeper, "read_address", side_effect=AssertionError("curve read")):
            with self.assertRaisesRegex(keeper.WaitForPrice, "phase 1"):
                keeper.make_plan(LogRpc(), bindings(), X, keeper.PlanSettings())

    def test_new_configure_abi_and_existing_bounds(self):
        plan = keeper.plan_position(X, Q_LOW, 10**27, 10**27,
                                    10**18, 2 * 10**18, 100, keeper.PlanSettings())
        data = keeper.configure_data(plan)
        self.assertTrue(data.startswith(keeper.CONFIGURE))
        self.assertEqual(len(data), keeper.CONFIGURE_DATA_HEX_LENGTH)
        encoded_token, encoded = decode(["address", "bytes"], bytes.fromhex(data[10:]))
        self.assertEqual(encoded_token.lower(), X)
        self.assertEqual(decode(keeper.CONFIG_TYPES, encoded), plan.abi_config())
        self.assertTrue(keeper.existing_config_safe(None, bindings(), plan, plan.abi_config(), 100))
        stale = (*plan.abi_config()[:6], 120)
        self.assertFalse(keeper.existing_config_safe(None, bindings(), plan, stale, 100))
        oversized = list(plan.abi_config())
        oversized[2] = (plan.max_quote_in[0] + 1, *plan.max_quote_in[1:])
        self.assertFalse(keeper.existing_config_safe(None, bindings(), plan, tuple(oversized), 100))

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
        plan = keeper.plan_position(fresh, Q_LOW, 10**27, 10**27,
                                    10**18, 2 * 10**18, 100, keeper.PlanSettings())
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
        plan = keeper.plan_position(hot, Q_LOW, 10**27, 10**27,
                                    10**18, 2 * 10**18, 100, keeper.PlanSettings())
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
        plan = keeper.plan_position(X, Q_LOW, 10**27, 10**27,
                                    10**18, 2 * 10**18, 100, keeper.PlanSettings())
        signer.submit("configure", X, keeper.configure_data(plan), state, store)
        self.assertEqual(len(rpc.sent), 2)

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
