"""Offline regression tests for eth_account's checksummed transaction destinations."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from eth_abi import encode
import rlp

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_launch_watcher as watch
import pons_price_keeper as price
import pons_exit_keeper as exit_keeper
import pons_harvest_keeper as harvest
import pons_fee_feedback_keeper as feedback


Q = "0x0e34d0792032ffc54c058751cf048da347193472"
INSPECTOR = "0x" + "ab" * 20
TOKEN = "0x" + "11" * 20
GUARD = "0x" + "22" * 20
KEY = "0x" + "01" * 32  # Public offline fixture.
BLOCK_HASH = "0x" + "33" * 32


class SendIntercepted(Exception):
    pass


class OfflineRpc:
    def __init__(self):
        self.raw = None

    def call(self, method, params):
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_estimateGas":
            return hex(100_000)
        if method == "eth_call":
            assert params[0]["data"] == price.TRANSFER_STEP_GAS_LIMIT
            return "0x" + encode(["uint32"], [9_000_000]).hex()
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": "0x1"}
        if method == "eth_maxPriorityFeePerGas":
            return "0x1"
        if method == "eth_sendRawTransaction":
            self.raw = params[0]
            raise SendIntercepted("offline transport intercepted send")
        raise AssertionError(f"unexpected RPC method: {method}")


class MemoryStore:
    def __init__(self):
        self.rows = []

    def save(self, state):
        self.rows.append(state.json())


def bindings():
    base = price.Bindings(Q, "0x" + "44" * 20, GUARD, "0x" + "55" * 20,
                          "0x" + "66" * 32, 2500, 25, "0x" + "77" * 20,
                          price.QUOTER)
    return exit_keeper.ExitBindings(base, INSPECTOR, "0x" + "88" * 20,
                                    "0x" + "99" * 20)


class SigningDestinationTests(unittest.TestCase):
    def assert_destination(self, rpc, destination):
        self.assertIsNotNone(rpc.raw, "signing did not reach the offline send boundary")
        fields = rlp.decode(bytes.fromhex(rpc.raw[4:]))  # EIP-1559 type byte follows 0x.
        self.assertEqual(fields[5], bytes.fromhex(destination[2:]))

    def test_watcher_signs_lowercase_q_and_journals_before_send(self):
        rpc, store = OfflineRpc(), MemoryStore()
        cursor = watch.Cursor(4663, Q, 1, BLOCK_HASH)
        signer = watch.LiveEnqueuer(rpc, Q, 4663, KEY, 2, 500_000,
                                    100, 10, 1, .001, 1)
        with self.assertRaises(SendIntercepted):
            signer._submit_once(TOKEN, 2, cursor, store)
        self.assert_destination(rpc, Q)
        self.assertEqual(store.rows[-1]["pending"]["rawTx"], rpc.raw)

    def test_price_signer_accepts_lowercase_q(self):
        rpc, store = OfflineRpc(), MemoryStore()
        signer = price.KeeperSigner(rpc, bindings().price, 4663, KEY, 2,
                                    500_000, 10_000_000, 100, 10, 1, .001)
        state = price.KeeperState(4663, Q, GUARD, 1, BLOCK_HASH, [TOKEN])
        with self.assertRaises(SendIntercepted):
            signer.submit("process", TOKEN, price.PROCESS_NEXT, state, store)
        self.assert_destination(rpc, Q)
        self.assertEqual(store.rows[-1]["pendingTx"]["rawTx"], rpc.raw)

    def test_exit_and_harvest_signers_accept_lowercase_inspector(self):
        for signer_type, data in ((exit_keeper.ExitSigner, exit_keeper.poke_data(TOKEN)),
                                  (harvest.HarvestSigner, harvest.poke_harvest_data(TOKEN))):
            with self.subTest(signer=signer_type.__name__):
                rpc, store = OfflineRpc(), MemoryStore()
                signer = signer_type(rpc, bindings(), 4663, KEY, 2,
                                     500_000, 500_000, 500_000, 100, 10, 1, .001)
                state = price.KeeperState(4663, Q, GUARD, 1, BLOCK_HASH, [TOKEN])
                with self.assertRaises(SendIntercepted):
                    signer.submit("poke", TOKEN, data, state, store)
                self.assert_destination(rpc, INSPECTOR)
                self.assertEqual(store.rows[-1]["pendingTx"]["rawTx"], rpc.raw)

    def test_feedback_signer_accepts_lowercase_q(self):
        rpc = OfflineRpc()
        signer = feedback.FeedbackSigner(rpc, Q, KEY, 2, 500_000, 100, 10,
                                         1, .001)
        digest = "0x" + "aa" * 32
        state = {"statePath": "/offline/feedback.json", "pendingTx": None}
        saved = []
        with patch.object(feedback.report, "_save_state",
                          side_effect=lambda _path, row: saved.append(row["pendingTx"].copy())):
            with self.assertRaises(SendIntercepted):
                signer.submit("report", TOKEN,
                              feedback.report_data(TOKEN, 100, digest), digest, state)
        self.assert_destination(rpc, Q)
        self.assertEqual(saved[-1]["rawTx"], rpc.raw)


if __name__ == "__main__":
    unittest.main()
