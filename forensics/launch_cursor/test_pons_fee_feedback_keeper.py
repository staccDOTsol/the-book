"""Gross quote scoring, evidence archive, and signed-journal tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from eth_abi import decode
from eth_account import Account
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_fee_feedback_keeper as feedback
import pons_fee_reporter as report
import pons_price_keeper as price


Q = "0x" + "11" * 20
X = "0x" + "77" * 20
POOL = "0x" + "aa" * 32
H = {n: "0x" + f"{n:064x}" for n in range(1, 20)}
Q_CREATION_BLOCK = 79_266_048
Q_ETH_LAUNCH_BLOCK = 79_267_280


def observation() -> dict:
    return {"token": X, "openBlock": 5, "exitBlock": 10,
            "evidenceBlockHash": H[10],
            "qSpentWei": "450", "qBurnedWei": "700",
            "settlementBurns": [
                {"kind": "position_exit", "block": 8, "qBurnedWei": "150"},
                {"kind": "position_exit", "block": 9, "qBurnedWei": "150"},
                {"kind": "position_exit", "block": 10, "qBurnedWei": "400"}],
            "recipientCashEthWei": "500", "knownReceipts": [],
            "grossMarkEvidenceComplete": True}


class QuoteRpc:
    def call(self, method: str, params: list):
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"hash": H[n], "timestamp": hex(n * 12)}
        raise AssertionError(method)


class FreshDeploymentRpc:
    def __init__(self):
        self.head = Q_CREATION_BLOCK + 6
        self.log_queries = []

    def call(self, method: str, params: list):
        if method == "eth_getCode":
            return "0x" if int(params[1], 16) < Q_CREATION_BLOCK else "0x6000"
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"hash": "0x" + f"{n:064x}", "timestamp": hex(n * 12)}
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            self.log_queries.append(params[0])
            return []
        raise AssertionError(method)


class FakeQuoteProvider:
    def __init__(self, rpc, _bindings):
        self.rpc = rpc

    def quote_single(self, _key, zero_for_one, amount):
        assert not zero_for_one
        return {4: 500, 7: 75, 8: 75, 9: 190}[self.rpc.block]


class SigningRpc:
    def __init__(self, path: Path):
        self.path = path
        self.sent = None
        self.signer = None

    def call(self, method: str, params: list):
        if method == "eth_estimateGas":
            return hex(100000)
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_getBlockByNumber":
            return {"hash": H[12], "baseFeePerGas": "0x1"}
        if method == "eth_maxPriorityFeePerGas":
            return "0x1"
        if method == "eth_sendRawTransaction":
            saved = json.loads(self.path.read_text())
            assert saved["pendingTx"]["rawTx"] == params[0]
            self.sent = "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
            return self.sent
        if method == "eth_getTransactionReceipt":
            if params[0] != self.sent:
                return None
            return {"transactionHash": self.sent, "blockNumber": "0xc",
                    "blockHash": H[12], "status": "0x1"}
        if method == "eth_blockNumber":
            return "0x10"
        raise AssertionError(method)


class RecoveryRpc(SigningRpc):
    def __init__(self, path: Path, old_hash: str):
        super().__init__(path)
        self.old_hash = old_hash

    def call(self, method: str, params: list):
        if method == "eth_getTransactionReceipt" and params[0] == self.old_hash:
            return None
        if method == "eth_getTransactionByHash":
            return None
        if method == "eth_getTransactionCount":
            return "0x0" if params[1] == "latest" else "0x1"
        if method == "eth_chainId":
            return "0x1237"
        if method == "eth_call":
            calldata = params[0]["data"].lower()
            if calldata.startswith(report.Q_DEADLINE):
                from eth_abi import encode
                return "0x" + encode(["uint64"], [100]).hex()
            if calldata.startswith(report.Q_LAUNCHES):
                from eth_abi import encode
                return "0x" + encode(
                    ["uint8", "bool", "uint64", "uint64", "uint64", "uint64",
                     "uint32", "bool", "bool", "uint64", "uint32", "bool"],
                    [3, False, 0, 0, 0, 0, 0, False, False, 0, 0, False]).hex()
            if calldata.startswith(report.FEE_ASSIGNMENTS):
                from eth_abi import encode
                return "0x" + encode(["uint24", "uint8", "uint8"], [100000, 5, 1]).hex()
            raise AssertionError(calldata)
        if method == "eth_getBlockByNumber":
            if params[0] == "latest":
                return {"hash": H[16], "baseFeePerGas": "0x1"}
            n = int(params[0], 16)
            return {"hash": H[n], "timestamp": hex(n * 12)}
        return super().call(method, params)


class FeeFeedbackTests(unittest.TestCase):
    def test_deployment_start_with_no_outcomes_waits_and_persists_cursor(self):
        rpc = FreshDeploymentRpc()
        binding = report.Bindings(Q, "0x" + "22" * 20, "0x" + "33" * 20,
                                  "0x" + "44" * 20, report.ZERO, "0x" + "55" * 20,
                                  "0x" + "00" * 32, "0x" + "00" * 32)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(feedback, "LOCAL", Path(tmp)), \
             patch.object(report, "LOCAL", Path(tmp)):
            path = Path(tmp) / "pons-fee-feedback.json"
            safe_head = Q_CREATION_BLOCK + 3
            state = feedback._load_state(rpc, path, Q, "0x" + "66" * 20,
                                         Q_CREATION_BLOCK, safe_head)
            first = feedback.cycle(rpc, binding, None, state, None, 3, 1000, 100, 2)
            self.assertEqual(first["scannedThrough"], safe_head)
            self.assertEqual(first["pending"], 0)
            self.assertEqual(first["actions"], [])
            self.assertEqual(first["writesSent"], 0)
            self.assertTrue(path.exists())
            self.assertEqual(rpc.log_queries[0]["fromBlock"], hex(Q_CREATION_BLOCK))
            self.assertEqual(rpc.log_queries[0]["toBlock"], hex(safe_head))
            resumed = feedback._load_state(rpc, path, Q, "0x" + "66" * 20,
                                           None, safe_head)
            rpc.head += 1
            second = feedback.cycle(rpc, binding, None, resumed, None, 3, 1000, 100, 2)
            self.assertEqual(second["scannedThrough"], safe_head + 1)
            self.assertEqual(second["actions"], [])

    def test_post_deployment_start_is_rejected_to_preserve_mint_history(self):
        rpc = FreshDeploymentRpc()
        with tempfile.TemporaryDirectory() as tmp, patch.object(feedback, "LOCAL", Path(tmp)):
            path = Path(tmp) / "pons-fee-feedback.json"
            with self.assertRaisesRegex(feedback.FeedbackError, "start block follows Q deployment"):
                feedback._load_state(rpc, path, Q, "0x" + "66" * 20,
                                     Q_ETH_LAUNCH_BLOCK, Q_ETH_LAUNCH_BLOCK + 3)
            self.assertFalse(path.exists())

    def test_gross_score_uses_minted_q_and_all_lifetime_burns(self):
        row = observation()
        score = feedback.gross_mark_score(row, 500, 400)
        self.assertEqual(score["grossEstimatedSurplusEthWei"], "400")
        self.assertEqual(score["grossReturnBpsUnclipped"], 8000)
        self.assertFalse(score["profitClaim"])
        self.assertIn("excluding_gas", score["metric"])

    def test_historical_quotes_are_pinned_and_evidence_is_stable(self):
        bindings = price.Bindings(Q, "0x" + "22" * 20, "0x" + "33" * 20,
                                  "0x" + "44" * 20, POOL, 2500, 10,
                                  "0x" + "55" * 20, "0x" + "66" * 20)
        with patch.object(price, "ExecutableQuoteProvider", FakeQuoteProvider):
            evidence = feedback.build_evidence(QuoteRpc(), bindings, observation())
        self.assertEqual(evidence["entryQuote"]["sourceBlock"], 4)
        self.assertEqual([row["sourceBlock"] for row in evidence["burnQuotes"]], [7, 8, 9])
        self.assertEqual(evidence["score"]["grossReturnBpsForFeePolicy"], 6800)
        with tempfile.TemporaryDirectory() as tmp, patch.object(feedback, "EVIDENCE_DIR", Path(tmp)):
            digest = feedback.archive_evidence(evidence)
            self.assertEqual(feedback.archive_evidence(evidence), digest)
            self.assertEqual(digest, "0x" + hashlib.sha256(
                feedback.canonical_evidence(evidence)).hexdigest())

    def test_signer_journals_exact_report_before_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            state_path = local / "pons-fee-feedback.json"
            state = {"version": 2, "statePath": str(state_path), "pendingTx": None}
            rpc = SigningRpc(state_path)
            key = "0x" + "01" * 32
            signer = feedback.FeedbackSigner(rpc, Q, key, 2, 200000, 100,
                                             10, 2, 0.01)
            digest = "0x" + "ab" * 32
            data = feedback.report_data(X, 6800, digest)
            with patch.object(report, "LOCAL", local):
                signer.submit("report", X, data, digest, state)
            self.assertIsNone(state["pendingTx"])
            self.assertEqual(json.loads(state_path.read_text())["pendingTx"], None)
            self.assertIsNotNone(rpc.sent)
            token, score, proof = decode(["address", "int32", "bytes32"], bytes.fromhex(data[10:]))
            self.assertEqual(token.lower(), X)
            self.assertEqual(score, 6800)
            self.assertEqual("0x" + proof.hex(), digest)

    def test_expired_unknown_report_is_replaced_by_censor_at_same_nonce(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            state_path = local / "pons-fee-feedback.json"
            key = "0x" + "01" * 32
            account = Account.from_key(key)
            digest = "0x" + "ab" * 32
            old_data = feedback.report_data(X, 6800, digest)
            old = account.sign_transaction({
                "chainId": 4663, "nonce": 0, "to": Q, "value": 0,
                "data": old_data, "gas": 100000, "type": 2,
                "maxFeePerGas": 100, "maxPriorityFeePerGas": 10})
            old_hash = "0x" + old.hash.hex().removeprefix("0x")
            state = {"version": 2, "statePath": str(state_path),
                     "pendingTx": {"kind": "report", "token": X,
                                   "data": old_data, "evidenceHash": digest,
                                   "nonce": 0, "txHash": old_hash,
                                   "rawTx": "0x" + old.raw_transaction.hex()}}
            rpc = RecoveryRpc(state_path, old_hash)
            signer = feedback.FeedbackSigner(rpc, Q, key, 2, 200000, 1000,
                                             1000, 2, 0.01)
            binding = report.Bindings(Q, "0x" + "22" * 20,
                                      "0x" + "33" * 20, "0x" + "44" * 20,
                                      report.ZERO, report.ZERO,
                                      "0x" + "00" * 32, "0x" + "00" * 32)
            with patch.object(report, "LOCAL", local):
                signer.recover(state, binding)
            self.assertIsNone(state["pendingTx"])
            self.assertNotEqual(rpc.sent, old_hash)
            self.assertIsNone(json.loads(state_path.read_text())["pendingTx"])


if __name__ == "__main__":
    unittest.main()
