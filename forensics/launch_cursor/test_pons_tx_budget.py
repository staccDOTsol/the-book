"""Offline tests for durable keeper spend caps at the shared RPC send boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_tx_budget as budget
import pons_launch_watcher as watcher


class Rpc:
    def __init__(self, balance: int):
        self.balance = balance
        self.calls: list[tuple[str, list]] = []

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method == "eth_getBalance":
            return hex(self.balance)
        raise AssertionError(method)


def signed_tx(key: str, nonce: int = 0, *, gas: int = 21_000,
              fee: int = 1_000_000_000) -> str:
    tx = {"chainId": 4663, "nonce": nonce,
          "to": "0x" + "22" * 20, "value": 0, "gas": gas, "type": 2,
          "maxPriorityFeePerGas": 1, "maxFeePerGas": fee}
    return "0x" + Account.from_key(key).sign_transaction(tx).raw_transaction.hex()


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pons-keeper-budget.json"
        os.chmod(self.tmp.name, 0o700)
        self.key = "0x" + "01" * 32
        self.sender = Account.from_key(self.key).address.lower()
        self.env = {
            "PONS_BUDGET_SIGNER": self.sender,
            "PONS_BUDGET_DAILY_WEI": str(50_000_000_000_000),
            "PONS_BUDGET_TOTAL_WEI": str(100_000_000_000_000),
            "PONS_BUDGET_TX_WEI": str(25_000_000_000_000),
            "PONS_BUDGET_FLOOR_WEI": str(50_000_000_000_000),
            "PONS_BUDGET_MAX_FEE_WEI": str(1_000_000_000),
            "PONS_BUDGET_JOURNAL": str(self.path),
        }
        self.rpc = Rpc(120_000_000_000_000)

    def test_reserves_before_send_and_exact_rebroadcast_is_not_double_charged(self):
        raw = signed_tx(self.key)
        sent = []
        with patch.dict(os.environ, self.env):
            self.assertEqual(budget.budgeted_send_raw(self.rpc, [raw],
                             lambda: sent.append(raw) or "hash"), "hash")
            self.assertEqual(budget.budgeted_send_raw(self.rpc, [raw],
                             lambda: sent.append(raw) or "hash"), "hash")
        journal = json.loads(self.path.read_text())
        entries = journal["signers"][self.sender]["txs"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(next(iter(entries.values()))["maxCostWei"], 21_000_000_000_000)
        self.assertEqual(len(sent), 2)

    def test_daily_and_balance_floor_block_new_send(self):
        sent = []
        with patch.dict(os.environ, self.env):
            budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, 0)],
                                     lambda: sent.append(0))
            budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, 1)],
                                     lambda: sent.append(1))
            with self.assertRaisesRegex(budget.BudgetError, "daily, total, or reserve"):
                budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, 2)],
                                         lambda: sent.append(2))
        self.assertEqual(sent, [0, 1])

    def test_failed_network_send_still_reserves_and_wrong_signer_is_blocked(self):
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(OSError, "offline"):
                budget.budgeted_send_raw(self.rpc, [signed_tx(self.key)],
                                         lambda: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual(len(json.loads(self.path.read_text())["signers"][self.sender]["txs"]), 1)
            other = signed_tx("0x" + "02" * 32)
            with self.assertRaisesRegex(budget.BudgetError, "signer or chain"):
                budget.budgeted_send_raw(self.rpc, [other], lambda: self.fail("sent"))

    def test_tx_cap_and_live_balance_floor(self):
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(budget.BudgetError, "per-transaction"):
                budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, gas=30_000)],
                                         lambda: self.fail("sent"))
            budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, 0)], lambda: None)
            self.rpc.balance = 60_000_000_000_000
            with self.assertRaisesRegex(budget.BudgetError, "balance is below"):
                budget.budgeted_send_raw(self.rpc, [signed_tx(self.key, 1)],
                                         lambda: self.fail("sent"))

    def test_symlinked_journal_is_rejected(self):
        target = Path(self.tmp.name) / "target.json"
        target.write_text('{"schemaVersion":1,"signers":{}}')
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        with patch.dict(os.environ, self.env):
            with self.assertRaisesRegex(budget.BudgetError, "private regular"):
                budget.budgeted_send_raw(self.rpc, [signed_tx(self.key)],
                                         lambda: self.fail("sent"))

    def test_shared_keeper_http_transport_routes_signed_write_through_budget(self):
        rpc = watcher.HttpRpc("https://rpc.example")
        raw = signed_tx(self.key)
        calls = []
        def offline_http(method, params):
            calls.append(method)
            if method == "eth_getBalance":
                return hex(120_000_000_000_000)
            if method == "eth_sendRawTransaction":
                return "0x" + "ab" * 32
            raise AssertionError(method)
        with patch.dict(os.environ, {**self.env, "PONS_BUDGET_REQUIRED": "1"}), \
             patch.object(rpc, "_call_http", side_effect=offline_http):
            result = rpc.call("eth_sendRawTransaction", [raw])
        self.assertEqual(result, "0x" + "ab" * 32)
        self.assertEqual(calls, ["eth_getBalance", "eth_getBalance", "eth_sendRawTransaction"])


if __name__ == "__main__":
    unittest.main()
