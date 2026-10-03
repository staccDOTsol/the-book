"""Offline tests for the pinned, gas-bounded interim harvest planner."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eth_abi import decode, encode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_harvest_keeper as harvest
import pons_exit_keeper as exit_keeper
import pons_price_keeper as price


Q = "0x" + "11" * 20
X = "0x" + "22" * 20
EXE = "0x" + "44" * 20
GUARD = "0x" + "55" * 20
VIEW = "0x" + "66" * 20
HOOK = "0x" + "77" * 20
CURVE = "0x" + "88" * 20
INSPECTOR = "0x" + "99" * 20
ROUTER = "0x" + "aa" * 20
ACTIVE = "0x" + "bb" * 20


def bindings():
    base = price.Bindings(Q, EXE, GUARD, VIEW, "0x" + "ab" * 32,
                          2500, 25, HOOK, price.QUOTER)
    return exit_keeper.ExitBindings(base, INSPECTOR, ROUTER, ACTIVE)


class Rpc:
    def __init__(self, *, x_fee=10**18, q_fee=10**18, phase=0, reorg=False):
        self.x_fee, self.q_fee, self.phase, self.reorg = x_fee, q_fee, phase, reorg
        self.hash_reads = 0
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x20"
        if method == "eth_getBlockByNumber":
            self.hash_reads += 1
            block_hash = "0x" + ("bb" if self.reorg and self.hash_reads >= 3 else "aa") * 32
            return {"hash": block_hash, "timestamp": "0x64"}
        if method != "eth_call":
            raise AssertionError(method)
        target, data = params[0].get("to"), params[0]["data"]
        if data.startswith(harvest.SIMULATE_HARVEST):
            if target != EXE or params[0].get("from") != Q or params[1] != "0x20":
                raise AssertionError("fee simulation must be pinned and originate from Q")
            return "0x" + encode(["uint256", "uint256"],
                                  [self.x_fee, self.q_fee]).hex()
        if data.startswith(exit_keeper.PREVIEW_ACTIVE_SALE):
            token, amount = decode(["address", "uint256"], bytes.fromhex(data[10:]))
            if target != ACTIVE or token.lower() != X or amount != self.x_fee:
                raise AssertionError("wrong active sale quote")
            return "0x" + encode(["address", "uint256"], [CURVE, 10**17]).hex()
        if data.startswith(exit_keeper.watch.LAUNCHES):
            state = (2, True, 1, 2, 0, 0, 0, False, False, 0, 0, False)
            return "0x" + encode(exit_keeper.LAUNCH_STATE_TYPES, state).hex()
        if data.startswith(price.GET_LAUNCHED):
            record = (X, CURVE, Q, Q, price.ZERO, 0, 3000, 60, 50,
                      False, self.phase, 0, 0, 0, True)
            return "0x" + encode(price.LAUNCH_TYPES, record).hex()
        raise AssertionError(data[:10])


class Quotes:
    def __init__(self):
        self.calls = []

    def at_block(self, block):
        self.block = block
        return self

    def quote_single(self, key, zero_for_one, amount):
        self.calls.append((key, zero_for_one, amount))
        if key[1] == Q and zero_for_one:
            return 2 * 10**18
        if key[1] == Q:
            return 10**16
        raise AssertionError("unexpected graduated quote")


class HarvestTests(unittest.TestCase):
    def test_pinned_active_fee_plan_covers_whole_cycle_gas(self):
        rpc, quotes = Rpc(), Quotes()
        settings = harvest.HarvestSettings(1000, 1500, 120, 20,
                                           4_000_000, 5_000_000_000)
        plan = harvest.plan_harvest(rpc, bindings(), X, settings, quotes)
        self.assertEqual(plan.simulated_x_fee, 10**18)
        self.assertEqual(plan.simulated_q_fee, 10**18)
        self.assertEqual(plan.min_token_fee, 9 * 10**17)
        self.assertEqual(plan.min_quote_fee, 9 * 10**17)
        self.assertEqual(plan.min_eth_out, 85 * 10**15)
        self.assertEqual(plan.gross_eth_value, 11 * 10**16)
        self.assertEqual(plan.estimated_gas_units, 4_000_000)
        self.assertEqual(plan.deadline, 220)
        self.assertEqual(quotes.block, 32)
        self.assertEqual(quotes.calls[0][2], plan.min_eth_out // 2)
        self.assertTrue(quotes.calls[0][1])
        self.assertEqual(quotes.calls[1][2], 10**18)
        data = harvest.configure_harvest_data(plan)
        self.assertTrue(data.startswith(harvest.CONFIGURE_HARVEST))
        _, config = decode(["address", harvest.HARVEST_TUPLE], bytes.fromhex(data[10:]))
        self.assertEqual(config, plan.abi_config())

    def test_no_fee_or_insufficient_cash_never_creates_plan(self):
        settings = harvest.HarvestSettings(1000, 1500, 120, 20,
                                           4_000_000, 5_000_000_000)
        with self.assertRaisesRegex(harvest.WaitForHarvest, "no claimable"):
            harvest.plan_harvest(Rpc(x_fee=0, q_fee=0), bindings(), X, settings, Quotes())
        with self.assertRaisesRegex(harvest.WaitForHarvest, "whole-cycle gas"):
            harvest.plan_harvest(Rpc(phase=1), bindings(), X, settings, Quotes())
        with self.assertRaisesRegex(harvest.WaitForHarvest, "whole-cycle gas"):
            harvest.plan_harvest(Rpc(), bindings(), X,
                                 harvest.HarvestSettings(1000, 1500, 120, 20,
                                                         8_000_000, 20_000_000_000), Quotes())

    def test_q_only_fee_burn_uses_mark_to_market_value_and_retains_x(self):
        class QOnlyQuotes(Quotes):
            def quote_single(self, key, zero_for_one, amount):
                self.calls.append((key, zero_for_one, amount))
                if key[1] == Q and not zero_for_one:
                    return 10**17
                raise AssertionError("no X sale or ETH-to-Q buy in phase 1")

        rpc, quotes = Rpc(phase=1), QOnlyQuotes()
        settings = harvest.HarvestSettings(1000, 1500, 120, 20,
                                           4_000_000, 5_000_000_000)
        plan = harvest.plan_harvest(rpc, bindings(), X, settings, quotes)
        self.assertEqual((plan.min_token_fee, plan.min_quote_fee), (0, 9 * 10**17))
        self.assertEqual((plan.min_eth_out, plan.min_q_out), (0, 0))
        self.assertEqual(plan.gross_eth_value, 10**17)
        self.assertEqual(plan.expected_x_eth, 0)
        self.assertEqual(plan.expected_q_eth, 10**17)
        self.assertEqual(quotes.calls, [((price.ZERO, Q, 2500, 25, price.ZERO), False, 10**18)])

    def test_reorg_and_payload_scope(self):
        settings = harvest.HarvestSettings(1000, 1500, 120, 20,
                                           4_000_000, 5_000_000_000)
        with self.assertRaisesRegex(harvest.WaitForHarvest, "pinned harvest block changed"):
            harvest.plan_harvest(Rpc(reorg=True), bindings(), X, settings, Quotes())
        plan = harvest.plan_harvest(Rpc(), bindings(), X, settings, Quotes())
        data = harvest.configure_harvest_data(plan)
        harvest.HarvestSigner._check_payload("configure", X, data)
        harvest.HarvestSigner._check_payload("poke", X, harvest.poke_harvest_data(X))
        with self.assertRaises(harvest.HarvestError):
            harvest.HarvestSigner._check_payload("configure", X,
                                                 exit_keeper.configure_exit_data(
                                                     exit_keeper.ExitPlan(X, 1, 0, 1, 1, 220,
                                                                          1, 0, 0, 0, 1,
                                                                          1, 1, False)))
        self.assertTrue(harvest.is_harvest_pending({"kind": "configure", "data": data}))
        self.assertTrue(harvest.is_harvest_pending({"kind": "process",
                                                    "data": exit_keeper.PROCESS_NEXT,
                                                    "purpose": "configured_harvest"}))
        self.assertFalse(harvest.is_harvest_pending({"kind": "process",
                                                     "data": exit_keeper.PROCESS_NEXT,
                                                     "purpose": "configured_exit"}))
        self.assertEqual(harvest.DEFAULT_STATE, exit_keeper.DEFAULT_STATE)

    def test_queued_harvest_waits_for_scheduler_without_reconfiguring(self):
        class State:
            tokens = [X]

        class Signer:
            def submit(self, *_args, **_kwargs):
                raise AssertionError("configured harvest must not be signed again")

        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=(2, 0, 0, 0, 0, 0, 0,
                                                                True, False, 0, 0, True)), \
             patch.object(price, "latest_block_time", return_value=100), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 1, 100)), \
             patch.object(harvest, "configured_harvest", return_value=True), \
             patch.object(harvest, "plan_harvest", side_effect=AssertionError("duplicate plan")):
            result = harvest.run_cycle(object(), bindings(), State(), object(),
                                       harvest.HarvestSettings(), Quotes(), Signer(), 1, 20, True)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["waiting"], {"configured harvest awaits Q scheduler": 1})

    def test_ready_harvest_waits_when_process_gas_is_temporarily_high(self):
        class State:
            tokens = [X]

        class Signer:
            def submit(self, kind, *_args, **_kwargs):
                self.kind = kind
                raise exit_keeper.WaitForExit("process gas estimate exceeds configured cap")

        signer = Signer()
        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=(2, 0, 0, 0, 0, 0, 0,
                                                                False, False, 0, 0, True)), \
             patch.object(price, "latest_block_time", return_value=100), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 2, 100)), \
             patch.object(harvest, "configured_harvest", return_value=True), \
             patch.object(harvest, "harvest_config", return_value=(1, 1, 1, 1, 1, 1, 200)):
            result = harvest.run_cycle(object(), bindings(), State(), object(),
                                       harvest.HarvestSettings(), Quotes(), signer, 1, 20, True)
        self.assertEqual(signer.kind, "process")
        self.assertEqual(result["status"], "waiting")
        self.assertIn("process gas estimate exceeds configured cap", result["waiting"])

    def test_queued_q_only_burn_rechecks_market_value_before_process(self):
        class State:
            tokens = [X]

        class Signer:
            def submit(self, *_args, **_kwargs):
                raise AssertionError("stale Q-only burn must not be sent")

        with patch.object(exit_keeper, "discover_active", return_value=0), \
             patch.object(exit_keeper, "q_launch", return_value=(2, 0, 0, 0, 0, 0, 0,
                                                                False, False, 0, 0, True)), \
             patch.object(price, "latest_block_time", return_value=100), \
             patch.object(exit_keeper, "q_next_action", return_value=(X, 2, 100)), \
             patch.object(harvest, "configured_harvest", return_value=True), \
             patch.object(harvest, "harvest_config", return_value=(0, 9 * 10**17, 0, 0,
                                                                    10**17, 4_000_000, 200)), \
             patch.object(harvest, "plan_harvest", return_value=SimpleNamespace(
                 simulated_q_fee=10**18, expected_q_eth=10**16)):
            result = harvest.run_cycle(object(), bindings(), State(), object(),
                                       harvest.HarvestSettings(1000, 1500, 120, 20,
                                                               4_000_000, 5_000_000_000),
                                       Quotes(), Signer(), 1, 20, True)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["waiting"], {"Q-only market value fell below gas margin": 1})


if __name__ == "__main__":
    unittest.main()
