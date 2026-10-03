"""Offline mocked tests for the Pons Q exit keeper; no live transaction calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from eth_abi import decode, encode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_exit_keeper as exit_keeper
import pons_price_keeper as price
import pons_launch_watcher as watch


Q = "0x" + "11" * 20
X = "0x" + "22" * 20
Y = "0x" + "33" * 20
EXE = "0x" + "44" * 20
GUARD = "0x" + "55" * 20
VIEW = "0x" + "66" * 20
HOOK = "0x" + "77" * 20
CURVE = "0x" + "88" * 20
INSPECTOR = "0x" + "99" * 20
ROUTER = "0x" + "aa" * 20
ACTIVE = "0x" + "bb" * 20
POOL_ID = "0x" + "ab" * 32


def bindings():
    base = price.Bindings(Q, EXE, GUARD, VIEW, POOL_ID, 2500, 25, HOOK, price.QUOTER)
    return exit_keeper.ExitBindings(base, INSPECTOR, ROUTER, ACTIVE)


def factory_launch(phase=0):
    return (X, CURVE, Q, Q, price.ZERO, 0, 3000, 60, 50,
            False, phase, 0, 0, 0, True)


def q_launch(ready=True, entered_at=50, next_action=0, active_at=2):
    return (2, True, 1, active_at, entered_at, next_action, 0, ready, False, 0, 0, False)


class PlannerRpc:
    def __init__(self, *, at_quote=False, burn_x=1000, burn_q=0,
                 harvested_x=100, harvested_q=25, reorg=False, tranche=0,
                 at_token=None, in_band=False, entered=True, timestamp=100,
                 q_state=None, timed_route=True):
        self.at_quote = at_quote
        self.at_token = not at_quote if at_token is None else at_token
        self.in_band, self.entered = in_band, entered
        self.timestamp, self.q_state, self.timed_route = timestamp, q_state, timed_route
        self.burn_x, self.burn_q = burn_x, burn_q
        self.harvested_x, self.harvested_q = harvested_x, harvested_q
        self.reorg = reorg
        self.tranche = tranche
        self.calls = []
        self.hash_reads = 0

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber": return "0x20"
        if method == "eth_getBlockByNumber":
            self.hash_reads += 1
            block_hash = "0x" + ("bb" if self.reorg and self.hash_reads >= 3 else "aa") * 32
            return {"hash": block_hash, "timestamp": hex(self.timestamp)}
        if method != "eth_call": raise AssertionError(method)
        data = params[0]["data"]
        if data.startswith(watch.LAUNCHES):
            return "0x" + encode(exit_keeper.LAUNCH_STATE_TYPES,
                                  self.q_state or q_launch()).hex()
        if data.startswith(exit_keeper.INSPECT):
            _, tranche = decode(["address", "uint8"], bytes.fromhex(data[10:]))
            if tranche != self.tranche:
                raise watch.WatcherError("tranche is inactive")
            return "0x" + encode(["uint160", "bool", "bool", "bool", "bool"],
                                  [price.Q96, self.in_band, self.at_quote,
                                   self.at_token, self.entered]).hex()
        if data.startswith((exit_keeper.WITHDRAW, exit_keeper.SIMULATE_TIMED_WITHDRAW)):
            if data.startswith(exit_keeper.SIMULATE_TIMED_WITHDRAW) and not self.timed_route:
                raise watch.WatcherError("timed route unavailable")
            if params[0].get("from") != Q:
                raise AssertionError("withdraw simulation must originate from Q")
            self.asserted_withdraw_minima = decode(
                ["address", "uint8", "uint128", "uint128", "uint64"], bytes.fromhex(data[10:]))
            if self.asserted_withdraw_minima[1] != self.tranche:
                raise AssertionError("wrong tranche was simulated")
            if (self.asserted_withdraw_minima[2] > self.burn_x or
                    self.asserted_withdraw_minima[3] > self.burn_q):
                raise watch.WatcherError("simulated burn minima too high")
            return "0x" + encode(["uint256", "uint256"],
                                  [self.burn_x, self.burn_q]).hex()
        if data.startswith(exit_keeper.HARVESTED):
            return "0x" + encode(["uint256", "uint256"],
                                  [self.harvested_x, self.harvested_q]).hex()
        if data.startswith(exit_keeper.PREVIEW_ACTIVE_SALE):
            token, amount = decode(["address", "uint256"], bytes.fromhex(data[10:]))
            if token.lower() != X or amount != self.burn_x + self.harvested_x:
                raise AssertionError("active sale must quote all X receipts")
            return "0x" + encode(["address", "uint256"], [CURVE, 500]).hex()
        raise AssertionError(data[:10])


class Quotes:
    def __init__(self): self.calls = []
    def at_block(self, block):
        self.block = block
        return self
    def quote_single(self, key, zero_for_one, amount):
        self.calls.append((key, zero_for_one, amount))
        return 1000 if zero_for_one else 500


def event(block, token, topic, step=None, index=0):
    word = "0x" + token[2:].rjust(64, "0")
    return {"address": Q, "topics": [topic, word],
            "data": "0x" + (encode(["uint8"], [step]).hex() if step is not None else
                            "" if topic == exit_keeper.ALL_POSITIONS_EXITED else
                            encode(["uint256", "uint256"], [0, 0]).hex()),
            "blockNumber": hex(block), "transactionIndex": "0x0",
            "logIndex": hex(index), "blockHash": "0x" + f"{block:064x}"}


class LogRpc:
    def __init__(self, logs, reorg=None):
        self.logs, self.reorg = logs, reorg or {}
    def call(self, method, params):
        if method == "eth_blockNumber": return "0x10"
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return {"hash": self.reorg.get(block, "0x" + f"{block:064x}")}
        if method == "eth_getLogs":
            first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return [log for log in self.logs if first <= int(log["blockNumber"], 16) <= last]
        raise AssertionError(method)


class Store:
    def __init__(self): self.saved = []
    def save(self, state): self.saved.append(copy.deepcopy(state.json()))


class ExitKeeperTests(unittest.TestCase):
    def test_exit_signer_binding_rejects_price_or_owner_account(self):
        owner, opening, exiting = "0x" + "01" * 20, "0x" + "02" * 20, "0x" + "03" * 20
        getters = {exit_keeper.OWNER: owner,
                   price.PRICE_CONFIGURATOR: opening,
                   exit_keeper.EXIT_CONFIGURATOR: exiting}
        with patch.object(price, "verify_bindings", return_value=bindings().price), \
             patch.object(price, "read_address", side_effect=lambda _rpc, _q, method: getters[method]):
            for signer in (owner, opening):
                with self.assertRaisesRegex(exit_keeper.ExitError, "isolated Q.exitConfigurator"):
                    exit_keeper.verify_bindings(object(), 4663, Q, GUARD, price.QUOTER, signer)

    def test_phase_zero_plan_includes_harvested_x_and_quotes_half_min_eth(self):
        rpc, quotes = PlannerRpc(), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(0)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertEqual(plan.total_x, 1100)
        self.assertEqual(plan.min_token_out, 900)
        self.assertEqual(plan.min_eth_out, 425)
        self.assertEqual(plan.min_q_out, 850)
        self.assertEqual(quotes.calls[0][2], 212)
        self.assertEqual(plan.harvested_q, 25)
        self.assertEqual(plan.abi_config(), (0, 900, 0, 425, 850, 220, False))
        self.assertEqual(quotes.block, 32)
        self.assertTrue(all(params[1] == "0x20" for method, params in rpc.calls
                            if method == "eth_call"))
        self.assertEqual(exit_keeper.configure_exit_data(plan)[:10], exit_keeper.CONFIGURE_EXIT)

    def test_phase_two_uses_size_aware_pons_and_qeth_v4_quotes(self):
        rpc, quotes = PlannerRpc(), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(2)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertEqual(quotes.calls[0][2], 1100)
        self.assertFalse(quotes.calls[0][1])
        self.assertEqual(quotes.calls[0][0][4], HOOK)
        self.assertEqual(quotes.calls[1][2], 212)
        self.assertTrue(quotes.calls[1][1])
        self.assertEqual(plan.expected_eth, 500)

    def test_second_tranche_is_selected_and_encoded_for_exit(self):
        rpc, quotes = PlannerRpc(tranche=1), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(0)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertEqual(plan.tranche, 1)
        self.assertEqual(rpc.asserted_withdraw_minima[1], 1)
        encoded = exit_keeper.configure_exit_data(plan)
        token, config = exit_keeper.decode_exit_config_data(encoded)
        self.assertEqual(token, X)
        self.assertEqual(config, plan.abi_config())
        exit_keeper.ExitSigner._check_payload("configure", X, encoded)
        with self.assertRaisesRegex(exit_keeper.ExitError, "not canonical"):
            exit_keeper.ExitSigner._check_payload("configure", X, encoded + "00")

    def test_all_q_boundary_can_settle_without_swap_minima(self):
        rpc, quotes = PlannerRpc(at_quote=True, burn_x=0, burn_q=1000,
                                 harvested_x=0, harvested_q=20), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(1)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertEqual(plan.min_quote_out, 900)
        self.assertEqual((plan.min_eth_out, plan.min_q_out), (0, 0))
        self.assertEqual(quotes.calls, [])

    def test_phase_one_with_x_receipts_waits_for_graduation(self):
        rpc = PlannerRpc()
        with patch.object(price, "read_launch", return_value=factory_launch(1)):
            with self.assertRaisesRegex(exit_keeper.WaitForExit, "phase 1"):
                exit_keeper.plan_exit(rpc, bindings(), X,
                                      exit_keeper.ExitSettings(), Quotes())

    def test_timed_untouched_q_only_tranche_uses_simulated_nft_receipt(self):
        rpc, quotes = PlannerRpc(
            at_quote=True, entered=False, burn_x=0, burn_q=1000,
            harvested_x=0, harvested_q=0, tranche=2, timestamp=7300,
            q_state=q_launch(True, 0, active_at=100)), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(1)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertTrue(plan.timed)
        self.assertEqual(plan.tranche, 2)
        self.assertEqual(plan.abi_config(), (2, 0, 900, 0, 0, 7420, True))
        self.assertEqual(quotes.calls, [])
        self.assertEqual(len([call for call in rpc.calls if call[0] == "eth_call" and
                              call[1][0]["data"].startswith(exit_keeper.SIMULATE_TIMED_WITHDRAW)]), 2)
        self.assertFalse(any(call[1][0]["data"].startswith(exit_keeper.WITHDRAW)
                             for call in rpc.calls if call[0] == "eth_call"))

    def test_timed_interior_tranche_bounds_both_receipts_and_quotes_x(self):
        rpc, quotes = PlannerRpc(
            in_band=True, at_token=False, entered=False, burn_x=1000, burn_q=500,
            tranche=1, timestamp=7300, q_state=q_launch(True, 0, active_at=100)), Quotes()
        with patch.object(price, "read_launch", return_value=factory_launch(2)):
            plan = exit_keeper.plan_exit(rpc, bindings(), X,
                                         exit_keeper.ExitSettings(), quotes)
        self.assertTrue(plan.timed)
        self.assertEqual(plan.tranche, 1)
        self.assertEqual((plan.min_token_out, plan.min_quote_out), (900, 450))
        self.assertEqual((plan.min_eth_out, plan.min_q_out), (425, 850))
        self.assertEqual(rpc.asserted_withdraw_minima[2:4], (900, 450))
        self.assertEqual(exit_keeper.decode_exit_config_data(
            exit_keeper.configure_exit_data(plan))[1], plan.abi_config())

    def test_timed_plan_waits_when_simulation_route_is_unavailable(self):
        rpc = PlannerRpc(entered=False, tranche=1, timestamp=7300,
                         q_state=q_launch(True, 0, active_at=100), timed_route=False)
        with patch.object(price, "read_launch", return_value=factory_launch(2)):
            with self.assertRaisesRegex(exit_keeper.WaitForExit, "simulateTimedWithdraw"):
                exit_keeper.plan_exit(rpc, bindings(), X,
                                      exit_keeper.ExitSettings(), Quotes())

    def test_unentered_q_only_position_cannot_exit_before_wind_down(self):
        rpc = PlannerRpc(at_quote=True, entered=False, burn_x=0, burn_q=1000,
                         timestamp=7299, q_state=q_launch(True, 0, active_at=100))
        with self.assertRaisesRegex(exit_keeper.WaitForExit, "not entered"):
            exit_keeper.plan_exit(rpc, bindings(), X,
                                  exit_keeper.ExitSettings(), Quotes())

    def test_same_height_reorg_rejects_exit_plan(self):
        rpc = PlannerRpc(reorg=True)
        with patch.object(price, "read_launch", return_value=factory_launch(0)):
            with self.assertRaisesRegex(exit_keeper.WaitForExit, "block changed"):
                exit_keeper.plan_exit(rpc, bindings(), X,
                                      exit_keeper.ExitSettings(), Quotes())

    def test_poke_only_after_entry_evidence_or_reported_boundary(self):
        opening = (price.Q96, False, True, False, False)
        self.assertFalse(exit_keeper.needs_poke(q_launch(False, 0), opening))
        self.assertTrue(exit_keeper.needs_poke(q_launch(False, 0),
                                               (price.Q96, True, False, False, False)))
        self.assertTrue(exit_keeper.needs_poke(q_launch(False, 50),
                                               (price.Q96, True, False, False, False)))
        self.assertTrue(exit_keeper.needs_poke(q_launch(False, 50),
                                               (price.Q96, False, True, False, True)))
        self.assertFalse(exit_keeper.needs_poke(q_launch(True),
                                                (price.Q96, False, True, False, True)))

    def test_wind_down_is_scheduled_after_120_minutes_without_band_entry(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [X])
        class Signer:
            def __init__(self): self.sent = []
            def submit(self, *args, **kwargs): self.sent.append((args[:3], kwargs))
        signer = Signer()
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", side_effect=lambda *_:
                          q_launch(bool(signer.sent), 0, active_at=100)), \
             patch.object(price, "latest_block_time", return_value=7300), \
             patch.object(exit_keeper, "inspect_active_tranches") as inspector:
            read_only = exit_keeper.run_cycle(object(), bindings(), state, Store(),
                                               exit_keeper.ExitSettings(), Quotes(),
                                               None, 3, 20, True)
            live_plan = exit_keeper.run_cycle(object(), bindings(), state, Store(),
                                               exit_keeper.ExitSettings(), Quotes(),
                                               signer, 3, 20, True)
        self.assertEqual(read_only["status"], "wind_down_ready_read_only")
        self.assertEqual(live_plan["status"], "wind_down_requested")
        self.assertEqual(signer.sent, [(("wind_down", X,
                                        exit_keeper.request_wind_down_data(X)), {})])
        inspector.assert_not_called()

    def test_wind_down_request_is_selector_limited_and_recovery_checks_eligibility(self):
        signer = exit_keeper.ExitSigner(object(), bindings(), 4663,
                                        "0x" + "01" * 32, 3, 500000, 500000,
                                        3500000, 5_000_000_000, 1_000_000_000,
                                        1, 0.01)
        request = exit_keeper.request_wind_down_data(X)
        signer._check_payload("wind_down", X, request)
        with self.assertRaisesRegex(exit_keeper.ExitError, "token mismatch"):
            signer._check_payload("wind_down", Y, request)
        with self.assertRaisesRegex(exit_keeper.ExitError, "length"):
            signer._check_payload("wind_down", X, request + "00")
        signed = signer.account.sign_transaction({"chainId": 4663, "nonce": 0,
            "to": Q, "value": 0, "data": request, "gas": 100000,
            "type": 2, "maxFeePerGas": 2_000_000_000,
            "maxPriorityFeePerGas": 1_000_000_000})
        pending = {"kind": "wind_down", "token": X, "data": request,
                   "nonce": 0, "txHash": "0x" + signed.hash.hex(),
                   "rawTx": "0x" + signed.raw_transaction.hex(), "purpose": None}
        state = price.KeeperState(4663, Q, GUARD, 1, "0x" + "aa" * 32, [X], pending)
        self.assertEqual(signer._validated_pending(pending, state)[:2], ("wind_down", X))
        with patch.object(exit_keeper, "q_launch", return_value=q_launch(False, 0, active_at=100)), \
             patch.object(price, "latest_block_time", return_value=7300):
            self.assertTrue(signer._unknown_safe("wind_down", X, request, None,
                                                  exit_keeper.ExitSettings(), Quotes()))
        with patch.object(exit_keeper, "q_launch", return_value=q_launch(True, 0, active_at=100)), \
             patch.object(price, "latest_block_time", return_value=7300):
            self.assertFalse(signer._unknown_safe("wind_down", X, request, None,
                                                   exit_keeper.ExitSettings(), Quotes()))

    def test_wind_down_signer_does_not_send_when_q_would_not_queue(self):
        class Rpc:
            def call(self, method, params):
                if method == "eth_call":
                    return "0x" + encode(["bool"], [False]).hex()
                raise AssertionError(f"unexpected {method}")
        signer = exit_keeper.ExitSigner(Rpc(), bindings(), 4663,
                                        "0x" + "01" * 32, 3, 500000, 500000,
                                        3500000, 5_000_000_000, 1_000_000_000,
                                        1, 0.01)
        state = price.KeeperState(4663, Q, GUARD, 1, "0x" + "aa" * 32, [X])
        with self.assertRaisesRegex(exit_keeper.WaitForExit, "would not queue"):
            signer.submit("wind_down", X, exit_keeper.request_wind_down_data(X),
                          state, Store())
        self.assertIsNone(state.pending_tx)

    def test_confirmed_wind_down_receipt_is_not_mistaken_for_queued_exit(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [X])
        class Signer:
            def submit(self, *_args, **_kwargs): pass
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=q_launch(False, 0, active_at=100)), \
             patch.object(exit_keeper, "q_next_action", return_value=(price.ZERO, 0, 0)), \
             patch.object(price, "latest_block_time", return_value=7300):
            result = exit_keeper.run_cycle(object(), bindings(), state, Store(),
                                            exit_keeper.ExitSettings(), Quotes(),
                                            Signer(), 3, 20, True)
        self.assertEqual(result["status"], "waiting")
        self.assertIn("Q wind-down request did not queue an exit", result["waiting"])

    def test_confirmed_q_events_track_active_tokens_and_reorgs(self):
        logs = [event(10, X, exit_keeper.STEP_SUCCEEDED, 0),
                event(11, Y, exit_keeper.STEP_SUCCEEDED, 0),
                event(12, X, exit_keeper.STEP_SUCCEEDED, 1),
                event(13, Y, exit_keeper.LAUNCH_ABORTED),
                event(14, X, exit_keeper.ALL_POSITIONS_EXITED),
                event(15, X, exit_keeper.STEP_SUCCEEDED, 0)]
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [])
        store = Store()
        self.assertEqual(exit_keeper.discover_active(LogRpc(logs), state, store, 1, 20), 3)
        self.assertEqual(state.tokens, [X])
        self.assertEqual(state.last_block, 15)
        self.assertEqual(len(store.saved), 1)
        state.last_hash = "0x" + "ff" * 32
        with self.assertRaisesRegex(exit_keeper.ExitError, "reorg"):
            exit_keeper.discover_active(LogRpc(logs), state, store, 1, 20)

    def test_partial_exit_keeps_token_active_until_final_marker(self):
        logs = [event(10, X, exit_keeper.STEP_SUCCEEDED, 0),
                event(11, X, exit_keeper.STEP_SUCCEEDED, 1),
                event(12, X, exit_keeper.STEP_SUCCEEDED, 1),
                event(13, X, exit_keeper.STEP_SUCCEEDED, 2)]
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [])
        self.assertEqual(exit_keeper.discover_active(LogRpc(logs), state, Store(), 1, 20), 1)
        self.assertEqual(state.tokens, [X])

    def test_unconfirmed_exit_reorg_does_not_lose_active_token(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [X])
        store = Store()
        closed = (3, *q_launch()[1:])
        class Rpc:
            def call(self, method, params):
                if method == "eth_blockNumber": return "0xc"
                if method == "eth_getBlockByNumber":
                    return {"hash": "0x" + f"{int(params[0], 16):064x}"}
                raise AssertionError(method)
        rpc = Rpc()
        def unconfirmed_close(request_rpc, _q, _token):
            return q_launch() if isinstance(request_rpc, price.PinnedRpc) else closed
        class Signer:
            def submit(self, *_args, **_kwargs):
                raise AssertionError("must not process an unconfirmed close")
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", side_effect=unconfirmed_close), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(price, "latest_block_time", return_value=100):
            result = exit_keeper.run_cycle(rpc, bindings(), state, store,
                                           exit_keeper.ExitSettings(), Quotes(),
                                           Signer(), 3, 20, True)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(state.tokens, [X])
        self.assertEqual(store.saved, [])
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=q_launch(False)), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(price, "latest_block_time", return_value=100), \
             patch.object(exit_keeper, "inspect_active_tranches", return_value=[
                 (0, (price.Q96, False, False, False, True))]):
            exit_keeper.run_cycle(rpc, bindings(), state, store,
                                  exit_keeper.ExitSettings(), Quotes(),
                                  None, 3, 20, True)
        self.assertEqual(state.tokens, [X])

    def test_confirmed_aborted_exit_root_is_cleared_even_without_active_tokens(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [])
        class Rpc:
            def call(self, method, params):
                if method == "eth_blockNumber": return "0xc"
                if method == "eth_getBlockByNumber":
                    return {"hash": "0x" + f"{int(params[0], 16):064x}"}
                raise AssertionError(method)
        class Signer:
            def __init__(self): self.sent = []
            def submit(self, *args, **kwargs): self.sent.append((args[:3], kwargs))
        signer = Signer()
        aborted = (5, *q_launch()[1:])
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(exit_keeper, "q_launch", return_value=aborted), \
             patch.object(price, "latest_block_time", return_value=100):
            result = exit_keeper.run_cycle(Rpc(), bindings(), state, Store(),
                                           exit_keeper.ExitSettings(), Quotes(),
                                           signer, 3, 20, True)
        self.assertEqual(result["status"], "stale_exit_cleared")
        self.assertEqual(signer.sent, [(("process", X, exit_keeper.PROCESS_NEXT),
                                        {"purpose": "stale_cleanup"})])
        self.assertEqual(state.tokens, [])

    def test_active_exit_root_without_config_is_not_cleared(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [])
        class Rpc:
            def call(self, method, params):
                if method == "eth_blockNumber": return "0xc"
                if method == "eth_getBlockByNumber":
                    return {"hash": "0x" + f"{int(params[0], 16):064x}"}
                raise AssertionError(method)
        class Signer:
            def submit(self, *_): raise AssertionError("must not process active exit")
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(exit_keeper, "q_launch", return_value=q_launch()), \
             patch.object(price, "latest_block_time", return_value=100):
            result = exit_keeper.run_cycle(Rpc(), bindings(), state, Store(),
                                           exit_keeper.ExitSettings(), Quotes(),
                                           Signer(), 3, 20, True)
        self.assertEqual(result["status"], "waiting")

    def test_plans_only_the_exit_selected_by_q_scheduler(self):
        state = price.KeeperState(4663, Q, GUARD, 9, "0x" + f"{9:064x}", [X, Y])
        plan = exit_keeper.ExitPlan(Y, 1, 0, 1, 1, 220, 2, 0, 0, 0,
                                    2, 3, 4, False)
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=q_launch()), \
             patch.object(exit_keeper, "inspect_active_tranches", return_value=[
                 (0, (price.Q96, False, False, True, True))]), \
             patch.object(exit_keeper, "q_next_action", return_value=(Y, 1, 0)), \
             patch.object(price, "latest_block_time", return_value=100), \
             patch.object(exit_keeper, "plan_exit", return_value=plan) as planner:
            result = exit_keeper.run_cycle(object(), bindings(), state, Store(),
                                           exit_keeper.ExitSettings(), Quotes(),
                                           None, 3, 20, True)
        self.assertEqual(result["plan"]["token"], Y)
        planner.assert_called_once()
        self.assertEqual(planner.call_args.args[2], Y)

    def test_signer_rejects_non_exit_selector_and_wrong_target_raw_tx(self):
        private_key = "0x" + "01" * 32  # public fixture, never used on a live chain
        signer = exit_keeper.ExitSigner(object(), bindings(), 4663, private_key,
                                        1, 500000, 500000, 3500000,
                                        5_000_000_000, 1_000_000_000, 1, 0.01)
        with self.assertRaisesRegex(exit_keeper.ExitError, "non-exit"):
            signer._check_payload("configure", X, watch.ENQUEUE + X[2:].rjust(64, "0"))
        with self.assertRaisesRegex(exit_keeper.ExitError, "no arguments"):
            signer._check_payload("process", X, exit_keeper.PROCESS_NEXT + "00")
        signed = signer.account.sign_transaction({"chainId": 4663, "nonce": 0,
            "to": Q, "value": 0, "data": exit_keeper.poke_data(X), "gas": 100000,
            "type": 2, "maxFeePerGas": 2_000_000_000,
            "maxPriorityFeePerGas": 1_000_000_000})
        raw = "0x" + signed.raw_transaction.hex()
        pending = {"kind": "poke", "token": X, "data": exit_keeper.poke_data(X),
                   "nonce": 0, "txHash": "0x" + signed.hash.hex(), "rawTx": raw}
        state = price.KeeperState(4663, Q, GUARD, 1, "0x" + "aa" * 32, [X], pending)
        with self.assertRaisesRegex(exit_keeper.ExitError, "permissions or caps"):
            signer._validated_pending(pending, state)

    def test_shared_configurator_lock_preserves_other_pending_nonce_owner(self):
        local = price.ROOT / ".local" / f"exit-lock-test-{uuid4().hex}"
        local.mkdir(parents=True)
        first, second = local / "price.json", local / "exit.json"
        first.write_text(json.dumps({"pendingTx": {"txHash": "0x" + "aa" * 32}}))
        try:
            with patch.object(price, "LOCAL", local):
                with price.ConfiguratorLock(Q, first):
                    pass
                with self.assertRaisesRegex(price.KeeperError, "unresolved signed"):
                    with price.ConfiguratorLock(Q, second):
                        pass
                first.write_text(json.dumps({"pendingTx": None}))
                with price.ConfiguratorLock(Q, second):
                    pass
        finally:
            shutil.rmtree(local)

    def test_unknown_signed_poke_rebroadcasts_exact_raw_tx_or_stops_on_nonce_replacement(self):
        private_key = "0x" + "01" * 32
        class Rpc:
            def __init__(self): self.nonce, self.sent = 0, []
            def call(self, method, params):
                if method in ("eth_getTransactionReceipt", "eth_getTransactionByHash"):
                    return None
                if method == "eth_getTransactionCount": return hex(self.nonce)
                if method == "eth_sendRawTransaction":
                    self.sent.append(params[0])
                    return "0x" + signed.hash.hex()
                raise AssertionError(method)
        rpc = Rpc()
        signer = exit_keeper.ExitSigner(rpc, bindings(), 4663, private_key,
                                        1, 500000, 500000, 3500000,
                                        5_000_000_000, 1_000_000_000, 1, 0.01)
        signed = signer.account.sign_transaction({"chainId": 4663, "nonce": 0,
            "to": INSPECTOR, "value": 0, "data": exit_keeper.poke_data(X),
            "gas": 100000, "type": 2, "maxFeePerGas": 2_000_000_000,
            "maxPriorityFeePerGas": 1_000_000_000})
        raw = "0x" + signed.raw_transaction.hex()
        pending = {"kind": "poke", "token": X, "data": exit_keeper.poke_data(X),
                   "nonce": 0, "txHash": "0x" + signed.hash.hex(), "rawTx": raw}
        state = price.KeeperState(4663, Q, GUARD, 1, "0x" + "aa" * 32, [X], pending)
        store = Store()
        with patch.object(price, "confirmed_receipt", return_value=False), \
             patch.object(signer, "_unknown_safe", return_value=True), \
             patch.object(signer, "_wait_receipt"):
            signer.recover(state, store, exit_keeper.ExitSettings(), Quotes())
        self.assertEqual(rpc.sent, [raw])
        self.assertIsNone(state.pending_tx)
        self.assertEqual(store.saved[-1]["pendingTx"], None)
        state.pending_tx = pending
        rpc.nonce = 1
        with patch.object(price, "confirmed_receipt", return_value=False):
            with self.assertRaisesRegex(exit_keeper.ExitError, "nonce consumed"):
                signer.recover(state, store, exit_keeper.ExitSettings(), Quotes())
        self.assertEqual(state.pending_tx, pending)

    def test_pending_process_recovery_accepts_only_confirmed_stale_exit(self):
        signer = exit_keeper.ExitSigner(object(), bindings(), 4663,
                                        "0x" + "01" * 32, 3, 500000, 500000,
                                        3500000, 5_000_000_000, 1_000_000_000,
                                        1, 0.01)
        aborted = (5, *q_launch()[1:])
        with patch.object(exit_keeper, "q_launch", return_value=aborted), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(exit_keeper, "confirmed_stale_exit", return_value=True), \
             patch.object(price, "latest_block_time", return_value=100):
            self.assertTrue(signer._unknown_safe("process", X, exit_keeper.PROCESS_NEXT,
                                                 "stale_cleanup", exit_keeper.ExitSettings(),
                                                 Quotes()))
        with patch.object(exit_keeper, "q_launch", return_value=aborted), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(exit_keeper, "confirmed_stale_exit", return_value=False), \
             patch.object(price, "latest_block_time", return_value=100):
            self.assertFalse(signer._unknown_safe("process", X, exit_keeper.PROCESS_NEXT,
                                                  "stale_cleanup", exit_keeper.ExitSettings(),
                                                  Quotes()))
        with patch.object(exit_keeper, "q_launch", return_value=q_launch()), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(exit_keeper, "confirmed_stale_exit", return_value=True), \
             patch.object(price, "latest_block_time", return_value=100):
            self.assertFalse(signer._unknown_safe("process", X, exit_keeper.PROCESS_NEXT,
                                                  "stale_cleanup", exit_keeper.ExitSettings(),
                                                  Quotes()))
        with patch.object(exit_keeper, "q_launch", return_value=q_launch()), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 0)), \
             patch.object(price, "call_abi", return_value=(0, 0, 0, 0, 0)), \
             patch.object(price, "latest_block_time", return_value=100):
            self.assertFalse(signer._unknown_safe("process", X, exit_keeper.PROCESS_NEXT,
                                                  "configured_exit", exit_keeper.ExitSettings(),
                                                  Quotes()))


if __name__ == "__main__":
    unittest.main()
