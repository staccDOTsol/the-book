"""Offline tests for the Pons launch watcher; every provider is a mock."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_launch_watcher as watcher


Q = "0x" + "11" * 20
OWNER = "0x" + "22" * 20
X1 = "0x" + "33" * 20
X2 = "0x" + "44" * 20
TX_HASH = "0x" + "ab" * 32


def word_address(value: str) -> str:
    return "0x" + value[2:].rjust(64, "0")


def log(block: int, index: int, token: str,
        pair_token: str = watcher.ZERO_ADDRESS) -> dict:
    return {"address": watcher.PONS_FACTORY, "topics": [watcher.TOKEN_LAUNCHED,
            word_address(token), word_address(OWNER), word_address(OWNER)],
            "data": word_address(pair_token) + f"{1:064x}" + f"{2:064x}",
            "blockNumber": hex(block), "transactionIndex": "0x0", "logIndex": hex(index),
            "blockHash": "0x" + f"{block:064x}", "removed": False}


class MockRpc:
    def __init__(self, head: int = 16, logs: list[dict] | None = None):
        self.head = head
        self.logs = logs or []
        self.stages: dict[str, int] = {}
        self.receipt: dict | None = None
        self.calls: list[tuple[str, list]] = []
        self.reorg: dict[int, str] = {}
        self.known_tx = None
        self.send_hash = TX_HASH

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method == "eth_chainId": return "0x1337"
        if method == "eth_getCode": return "0x6000"
        if method == "eth_blockNumber": return hex(self.head)
        if method == "eth_getBlockByNumber":
            if params[0] == "latest": return {"baseFeePerGas": "0x1"}
            block = int(params[0], 16)
            return {"hash": self.reorg.get(block, "0x" + f"{block:064x}")}
        if method == "eth_getLogs":
            first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return [copy.deepcopy(row) for row in self.logs if first <= int(row["blockNumber"], 16) <= last]
        if method == "eth_call":
            data = params[0]["data"]
            if data == watcher.PONS_FACTORY_GETTER: return word_address(watcher.PONS_FACTORY)
            if data == watcher.OWNER: return word_address(OWNER)
            if data.startswith(watcher.LAUNCHES):
                token = "0x" + data[-40:]
                return "0x" + f"{self.stages.get(token, 0):064x}" + "0" * 64
            raise AssertionError(f"unexpected eth_call: {data}")
        if method == "eth_getTransactionReceipt": return self.receipt
        if method == "eth_getTransactionByHash": return self.known_tx
        if method == "eth_getTransactionCount": return "0x0"
        if method == "eth_estimateGas": return "0x186a0"
        if method == "eth_maxPriorityFeePerGas": return "0x1"
        if method == "eth_sendRawTransaction": return self.send_hash
        raise AssertionError(f"unexpected RPC method: {method}")


class MemoryStore:
    def __init__(self): self.saved: list[dict] = []
    def save(self, cursor: watcher.Cursor): self.saved.append(copy.deepcopy(cursor.json()))


class FakeEnqueuer:
    def __init__(self, rpc: MockRpc, fail: str | None = None):
        self.rpc, self.fail, self.calls = rpc, fail, []

    def enqueue(self, token: str, source_block: int, cursor, store):
        if self.rpc.stages.get(token, 0): return False
        self.calls.append((token, source_block))
        if token == self.fail: raise watcher.WatcherError("simulated enqueue failure")
        self.rpc.stages[token] = 1
        return True


class WatcherTests(unittest.TestCase):
    def cursor(self) -> watcher.Cursor:
        return watcher.Cursor(0x1337, Q, 10, "0x" + f"{10:064x}")

    def test_backfill_orders_logs_and_persists_each_complete_range(self):
        rpc = MockRpc(logs=[log(12, 2, X2), log(12, 1, X1)])
        cursor, store, sender = self.cursor(), MemoryStore(), FakeEnqueuer(rpc)
        count = watcher.backfill_once(rpc, sender, cursor, store, confirmations=2, block_span=2)
        self.assertEqual(count, 2)
        self.assertEqual(sender.calls, [(X1, 12), (X2, 12)])
        self.assertEqual([item["lastBlock"] for item in store.saved], [12, 14])
        self.assertEqual(cursor.last_block, 14)
        filters = [params[0] for method, params in rpc.calls if method == "eth_getLogs"]
        self.assertTrue(all(item["address"] == watcher.PONS_FACTORY and
                            item["topics"] == [watcher.TOKEN_LAUNCHED] for item in filters))

    def test_failure_keeps_cursor_and_replay_skips_already_enqueued(self):
        rpc = MockRpc(logs=[log(11, 0, X1), log(12, 0, X2)])
        cursor, store = self.cursor(), MemoryStore()
        with self.assertRaisesRegex(watcher.WatcherError, "simulated"):
            watcher.backfill_once(rpc, FakeEnqueuer(rpc, fail=X2), cursor, store, 2, 10)
        self.assertEqual(cursor.last_block, 10)
        self.assertEqual(store.saved, [])
        sender = FakeEnqueuer(rpc)
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10)
        self.assertEqual(sender.calls, [(X2, 12)])
        self.assertEqual(cursor.last_block, 14)

    def test_cursor_hash_change_stops_before_new_enqueue(self):
        rpc = MockRpc(logs=[log(11, 0, X1)])
        rpc.reorg[10] = "0x" + "ff" * 32
        sender = FakeEnqueuer(rpc)
        with self.assertRaisesRegex(watcher.WatcherError, "cursor block hash changed"):
            watcher.backfill_once(rpc, sender, self.cursor(), MemoryStore(), 2, 10)
        self.assertEqual(sender.calls, [])

    def test_noncanonical_log_stops_without_advancing(self):
        bad = log(11, 0, X1)
        bad["blockHash"] = "0x" + "ee" * 32
        rpc, cursor = MockRpc(logs=[bad]), self.cursor()
        with self.assertRaisesRegex(watcher.WatcherError, "noncanonical"):
            watcher.backfill_once(rpc, FakeEnqueuer(rpc), cursor, MemoryStore(), 2, 10)
        self.assertEqual(cursor.last_block, 10)

    def test_non_eth_launch_is_skipped_before_enqueue_and_cursor_advances(self):
        rpc = MockRpc(logs=[log(11, 0, X1, X2), log(12, 0, X2)])
        cursor, store, sender = self.cursor(), MemoryStore(), FakeEnqueuer(rpc)
        count = watcher.backfill_once(rpc, sender, cursor, store, 2, 10)
        self.assertEqual(count, 2)
        self.assertEqual(sender.calls, [(X2, 12)])
        self.assertEqual(cursor.last_block, 14)
        self.assertEqual(store.saved[-1]["lastBlock"], 14)

    def test_bounded_cycle_resumes_after_exact_log_within_same_block(self):
        rpc = MockRpc(logs=[log(11, 0, X1, X2), log(12, 1, X1),
                            log(12, 2, X2), log(13, 0, Q)])
        cursor, store, sender = self.cursor(), MemoryStore(), FakeEnqueuer(rpc)
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10, max_enqueues=1)
        self.assertEqual(sender.calls, [(X1, 12)])
        self.assertEqual((cursor.last_block, cursor.last_log_index), (12, 1))
        self.assertEqual(store.saved[-1]["lastLogIndex"], 1)
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10, max_enqueues=1)
        self.assertEqual(sender.calls, [(X1, 12), (X2, 12)])
        self.assertEqual((cursor.last_block, cursor.last_log_index), (12, 2))
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10, max_enqueues=1)
        self.assertEqual(sender.calls, [(X1, 12), (X2, 12), (Q, 13)])
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10, max_enqueues=1)
        self.assertEqual((cursor.last_block, cursor.last_log_index), (14, None))

    def test_bounded_cycle_skips_already_enqueued_without_using_quota(self):
        rpc = MockRpc(logs=[log(11, 0, X1), log(12, 0, X2)])
        rpc.stages[X1] = 1
        cursor, store, sender = self.cursor(), MemoryStore(), FakeEnqueuer(rpc)
        watcher.backfill_once(rpc, sender, cursor, store, 2, 10, max_enqueues=1)
        self.assertEqual(sender.calls, [(X2, 12)])
        self.assertEqual((cursor.last_block, cursor.last_log_index), (12, 0))

    def test_bounded_cycle_reloads_mid_block_position_after_restart(self):
        path = watcher.LOCAL / f"test-pons-watcher-{uuid4().hex}.json"
        rpc = MockRpc(logs=[log(12, 1, X1), log(12, 2, X2)])
        sender = FakeEnqueuer(rpc)
        try:
            with watcher.CursorStore(path) as store:
                cursor = store.load(rpc, 0x1337, Q, 11)
                watcher.backfill_once(rpc, sender, cursor, store, 2, 10,
                                      max_enqueues=1)
            with watcher.CursorStore(path) as store:
                cursor = store.load(rpc, 0x1337, Q, None)
                self.assertEqual((cursor.last_block, cursor.last_log_index), (12, 1))
                watcher.backfill_once(rpc, sender, cursor, store, 2, 10,
                                      max_enqueues=1)
            self.assertEqual(sender.calls, [(X1, 12), (X2, 12)])
        finally:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)

    def test_malformed_pair_token_data_stops_before_enqueue_or_cursor_commit(self):
        bad = log(11, 0, X1)
        bad["data"] = "0x" + "0" * 23 + "1" + "0" * (3 * 64 - 24)
        rpc, cursor, store = MockRpc(logs=[bad]), self.cursor(), MemoryStore()
        sender = FakeEnqueuer(rpc)
        with self.assertRaisesRegex(watcher.WatcherError, "pair token"):
            watcher.backfill_once(rpc, sender, cursor, store, 2, 10)
        self.assertEqual(sender.calls, [])
        self.assertEqual(store.saved, [])
        self.assertEqual(cursor.last_block, 10)

    def test_contract_binding_and_owner_checked_before_writes(self):
        rpc = MockRpc()
        watcher.verify_contract(rpc, 0x1337, Q, OWNER)
        with self.assertRaisesRegex(watcher.WatcherError, "chain ID"):
            watcher.verify_contract(rpc, 1, Q, OWNER)
        with self.assertRaisesRegex(watcher.WatcherError, "not Q.owner"):
            watcher.verify_contract(rpc, 0x1337, Q, X1)
        self.assertFalse(any(method == "eth_sendRawTransaction" for method, _ in rpc.calls))

    def test_pending_receipt_recovery_and_reverted_retry_path(self):
        from eth_account import Account
        account = Account.from_key("0x" + "01" * 32)
        tx = {"chainId": 0x1337, "nonce": 0, "to": Q, "value": 0, "type": 2,
              "data": watcher.ENQUEUE + X1[2:].rjust(64, "0"), "gas": 100000,
              "maxFeePerGas": 2, "maxPriorityFeePerGas": 1}
        signed = account.sign_transaction(tx)
        pending = {"token": X1, "sourceBlock": 11, "txHash": "0x" + signed.hash.hex().removeprefix("0x"),
                   "nonce": 0, "rawTx": "0x" + signed.raw_transaction.hex().removeprefix("0x")}
        rpc, cursor, store = MockRpc(head=22), self.cursor(), MemoryStore()
        cursor.pending = pending.copy()
        rpc.receipt = {"status": "0x1", "blockNumber": "0x13", "blockHash": "0x" + f"{19:064x}"}
        rpc.stages[X1] = 1
        watcher.recover_pending(rpc, cursor, store, 2, account.address.lower(), 500000, 5, 2, 1, 0.001)
        self.assertIsNone(cursor.pending)
        self.assertEqual(store.saved[-1]["lastBlock"], 10)
        cursor.pending = pending.copy()
        rpc.receipt["status"] = "0x0"
        watcher.recover_pending(rpc, cursor, store, 2, account.address.lower(), 500000, 5, 2, 1, 0.001)
        self.assertIsNone(cursor.pending)
        self.assertEqual(cursor.last_block, 10)
        cursor.pending = pending.copy()
        rpc.receipt = None
        rpc.known_tx = {}
        with self.assertRaisesRegex(watcher.WatcherError, "unconfirmed"):
            watcher.recover_pending(rpc, cursor, store, 2, account.address.lower(), 500000, 5, 2, 0, 0.001)
        self.assertIsNotNone(cursor.pending)

    def test_crash_before_broadcast_replays_exact_signed_enqueue(self):
        from eth_account import Account
        account = Account.from_key("0x" + "01" * 32)
        tx = {"chainId": 0x1337, "nonce": 0, "to": Q, "value": 0, "type": 2,
              "data": watcher.ENQUEUE + X1[2:].rjust(64, "0"), "gas": 100000,
              "maxFeePerGas": 2, "maxPriorityFeePerGas": 1}
        signed = account.sign_transaction(tx)
        raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
        tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
        rpc, cursor, store = MockRpc(head=22), self.cursor(), MemoryStore()
        cursor.pending = {"token": X1, "sourceBlock": 11, "txHash": tx_hash, "nonce": 0, "rawTx": raw}
        rpc.send_hash = tx_hash
        original_call = rpc.call
        def call(method, params):
            result = original_call(method, params)
            if method == "eth_sendRawTransaction":
                rpc.receipt = {"status": "0x1", "blockNumber": "0x13",
                               "blockHash": "0x" + f"{19:064x}"}
                rpc.stages[X1] = 1
            return result
        rpc.call = call
        watcher.recover_pending(rpc, cursor, store, 2, account.address.lower(), 500000, 5, 2, 1, 0.001)
        self.assertIsNone(cursor.pending)
        self.assertEqual([params[0] for method, params in rpc.calls if method == "eth_sendRawTransaction"], [raw])
        cursor.pending = {"token": X1, "sourceBlock": 11, "txHash": tx_hash, "nonce": 0,
                          "rawTx": raw[:-2] + "00"}
        with self.assertRaisesRegex(watcher.WatcherError, "hash mismatch"):
            watcher.recover_pending(rpc, cursor, store, 2, account.address.lower(), 500000, 5, 2, 1, 0.001)

    def test_factory_event_abi_matches_filter_and_indexed_token(self):
        from eth_utils import keccak
        signature = "TokenLaunched(address,address,address,address,uint256,uint256)"
        self.assertEqual("0x" + keccak(text=signature).hex(), watcher.TOKEN_LAUNCHED)
        self.assertEqual(watcher.token_from_log(log(11, 0, X1), watcher.PONS_FACTORY), X1)
        self.assertEqual(watcher.pair_token_from_log(log(11, 0, X1)), watcher.ZERO_ADDRESS)
        self.assertEqual(watcher.pair_token_from_log(log(11, 0, X1, X2)), X2)

    def test_signed_transaction_targets_only_q_enqueue(self):
        rpc, cursor, store = MockRpc(head=22), self.cursor(), MemoryStore()
        rpc.receipt = {"status": "0x1", "blockNumber": "0x13", "blockHash": "0x" + f"{19:064x}"}
        class Signer:
            address = OWNER
            def sign_transaction(self, tx):
                self.tx = tx
                class Signed:
                    hash = bytes.fromhex("ab" * 32)
                    raw_transaction = bytes.fromhex("01")
                return Signed()
        class Account:
            @staticmethod
            def from_key(_key): return Signer()
        import types
        with patch.dict(sys.modules, {"eth_account": types.SimpleNamespace(Account=Account)}):
            sender = watcher.LiveEnqueuer(rpc, Q, 0x1337, "unused", 2, 500000,
                                          5_000_000_000, 1_000_000_000, 1, 0.001, 1)
        original_call = rpc.call
        def call(method, params):
            result = original_call(method, params)
            if method == "eth_sendRawTransaction": rpc.stages[X1] = 1
            return result
        rpc.call = call
        sender.enqueue(X1, 11, cursor, store)
        signed_tx = sender.account.tx
        self.assertEqual(signed_tx["to"], Q)
        self.assertEqual(signed_tx["data"], watcher.ENQUEUE + X1[2:].rjust(64, "0"))
        self.assertEqual(signed_tx["value"], 0)
        self.assertEqual([row["pending"] is not None for row in store.saved], [True, False])
        self.assertEqual([params[0] for method, params in rpc.calls if method == "eth_sendRawTransaction"], ["0x01"])

    def test_live_sender_skips_existing_stage_without_signing(self):
        rpc, cursor, store = MockRpc(head=22), self.cursor(), MemoryStore()
        rpc.stages[X1] = 1
        class Signer:
            address = OWNER
            def sign_transaction(self, _tx): raise AssertionError("must not sign")
        class Account:
            @staticmethod
            def from_key(_key): return Signer()
        import types
        with patch.dict(sys.modules, {"eth_account": types.SimpleNamespace(Account=Account)}):
            sender = watcher.LiveEnqueuer(rpc, Q, 0x1337, "unused", 2, 500000,
                                          5_000_000_000, 1_000_000_000, 1, 0.001, 1)
        sender.enqueue(X1, 11, cursor, store)
        self.assertEqual(store.saved, [])
        self.assertFalse(any(method == "eth_sendRawTransaction" for method, _ in rpc.calls))

    def test_websocket_handshake_drop_uses_http_backfill(self):
        from websockets.exceptions import InvalidHandshake
        rpc, cursor, store = MockRpc(head=14, logs=[log(11, 0, X1)]), self.cursor(), MemoryStore()
        sender = FakeEnqueuer(rpc)
        with patch("websockets.sync.client.connect",
                   side_effect=[InvalidHandshake(), watcher.WatcherError("stop test")]), \
             patch.object(watcher.time, "sleep", return_value=None):
            with self.assertRaisesRegex(watcher.WatcherError, "stop test"):
                watcher.watch_ws("wss://example.invalid", rpc, sender, cursor, store, 2, 10, 0.001)
        self.assertEqual(sender.calls, [(X1, 11)])
        self.assertEqual(cursor.last_block, 12)

    def test_cursor_file_is_durable_and_stays_in_local(self):
        path = watcher.LOCAL / f"test-pons-watcher-{uuid4().hex}.json"
        rpc = MockRpc()
        try:
            with watcher.CursorStore(path) as store:
                cursor = store.load(rpc, 0x1337, Q, 11)
                self.assertEqual(cursor.last_block, 10)
                cursor.last_block, cursor.last_hash = 12, "0x" + f"{12:064x}"
                cursor.last_log_index = 3
                store.save(cursor)
            with watcher.CursorStore(path) as store:
                restored = store.load(rpc, 0x1337, Q, None)
            self.assertEqual(restored.last_block, 12)
            self.assertEqual(restored.last_log_index, 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
