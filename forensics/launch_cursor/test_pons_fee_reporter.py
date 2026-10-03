"""Canonical receipt and cost-lot tests; all RPC traffic is synthetic."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from eth_abi import encode
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pons_fee_reporter as report


Q = "0x" + "11" * 20
VAULT = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20
BUYER = "0x" + "44" * 20
OWNER = "0x" + "55" * 20
POLICY = "0x" + "66" * 20
TOKEN = "0x" + "77" * 20
POOL = "0x" + keccak(encode(
    ["address", "address", "uint24", "int24", "address"],
    [Q, TOKEN, 100000, 10, "0x" + "00" * 20])).hex()
BUY_POOL = "0x" + "99" * 32
BUY_TX = "0x" + "aa" * 32
OPEN_TX = "0x" + "bb" * 32
EXIT_TX = "0x" + "cc" * 32
H = {n: "0x" + f"{n:064x}" for n in range(30)}


def addr(value: str) -> str:
    return "0x" + value[2:].rjust(64, "0")


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


def event(address: str, topics: list[str], types: list[str], values: list,
          block: int, tx_hash: str, index: int) -> dict:
    return {"address": address, "topics": topics,
            "data": "0x" + encode(types, values).hex(),
            "blockNumber": hex(block), "blockHash": H[block],
            "transactionHash": tx_hash, "transactionIndex": "0x0",
            "logIndex": hex(index), "removed": False}


class FakeRpc:
    def __init__(self):
        buy_input = report.SELECTOR_BUY_Q + encode(["uint256", "address", "uint64"],
                                                   [900, VAULT, 100]).hex()
        self.transactions = {
            BUY_TX: {"hash": BUY_TX, "blockHash": H[3], "from": OWNER,
                     "to": BUYER, "value": hex(1000), "input": buy_input},
            OPEN_TX: {"hash": OPEN_TX, "blockHash": H[5], "from": OWNER,
                      "to": Q, "value": "0x0", "input": "0x"},
            EXIT_TX: {"hash": EXIT_TX, "blockHash": H[10], "from": OWNER,
                      "to": Q, "value": "0x0", "input": "0x"},
        }
        self.logs = [
            event(Q, [report.TRANSFER, addr(BUYER), addr(VAULT)],
                  ["uint256"], [1000], 3, BUY_TX, 0),
            event(BUYER, [report.QUOTE_BOUGHT, addr(VAULT), BUY_POOL],
                  ["uint256", "uint256"], [1000, 1000], 3, BUY_TX, 1),
            event(POLICY, [report.FEE_SELECTED, addr(TOKEN)],
                  ["uint24", "uint8", "bool"], [100000, 5, True], 5, OPEN_TX, 0),
            event(Q, [report.TRANSFER, addr(VAULT), addr(report.POOL_MANAGER)],
                  ["uint256"], [500], 5, OPEN_TX, 1),
            event(VAULT, [report.POSITION_OPENED, addr(TOKEN), POOL, word(1)],
                  ["uint24", "uint256"], [100000, 500], 5, OPEN_TX, 2),
            event(report.POOL_MANAGER, [report.SWAP, POOL, addr(OWNER)],
                  ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                  [10, -5, 1 << 96, 1000, 0, 100000], 7, "0x" + "dd" * 32, 0),
            event(VAULT, [report.POSITION_SETTLED, addr(TOKEN)],
                  ["uint256"] * 4, [10, 300, 800, 400], 10, EXIT_TX, 0),
            event(ROUTER, [report.EXIT_SETTLED, addr(TOKEN)],
                  ["uint256"] * 6, [10, 800, 100, 400, 200, 200], 10, EXIT_TX, 1),
            event(Q, [report.TRANSFER, addr(ROUTER), addr(report.ZERO)],
                  ["uint256"], [400], 10, EXIT_TX, 2),
            event(Q, [report.PENDING, addr(TOKEN)],
                  ["uint64"], [100000], 10, EXIT_TX, 3),
        ]
        self.receipts = {}
        for tx_hash, tx in self.transactions.items():
            block = {BUY_TX: 3, OPEN_TX: 5, EXIT_TX: 10}[tx_hash]
            self.receipts[tx_hash] = {"transactionHash": tx_hash,
                                      "blockNumber": hex(block), "blockHash": H[block],
                                      "status": "0x1", "logs": [row for row in self.logs
                                                             if row["transactionHash"] == tx_hash],
                                      "gasUsed": "0x186a0", "effectiveGasPrice": "0x3b9aca00"}
        swap = self.logs[5]
        self.receipts[swap["transactionHash"]] = {
            "transactionHash": swap["transactionHash"], "blockNumber": "0x7",
            "blockHash": H[7], "status": "0x1", "logs": [swap],
            "gasUsed": "0x186a0", "effectiveGasPrice": "0x3b9aca00"}

    def call(self, method: str, params: list):
        if method == "eth_call":
            data = params[0]["data"].lower()
            if data.startswith(report.Q_DEADLINE):
                return "0x" + encode(["uint64"], [100000]).hex()
            if data.startswith(report.Q_LAUNCHES):
                return "0x" + encode(
                    ["uint8", "bool", "uint64", "uint64", "uint64", "uint64",
                     "uint32", "bool", "bool", "uint64", "uint32", "bool"],
                    [3, False, 0, 0, 0, 0, 0, False, False, 0, 0, False]).hex()
            if data.startswith(report.FEE_ASSIGNMENTS):
                return "0x" + encode(["uint24", "uint8", "uint8"], [100000, 5, 1]).hex()
            if data.startswith(report.EXEC_POSITIONS):
                return "0x" + encode(
                    ["uint256", "bytes32", "uint24", "int24", "int24", "int24", "bool",
                     "bool", "uint256", "uint256", "uint256"],
                    [1, bytes.fromhex(POOL[2:]), 100000, 10, -100, 100,
                     False, True, 500, 10, 300]).hex()
            raise AssertionError(f"unexpected eth_call selector {data[:10]}")
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"hash": H[n], "timestamp": hex(n * 12)}
        if method == "eth_blockNumber":
            return hex(20)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            return self.transactions.get(params[0])
        if method == "eth_getLogs":
            spec = params[0]
            def matches(row):
                if row["address"].lower() != spec["address"].lower():
                    return False
                if not int(spec["fromBlock"], 16) <= int(row["blockNumber"], 16) <= int(spec["toBlock"], 16):
                    return False
                for i, expected in enumerate(spec.get("topics", [])):
                    if expected is None:
                        continue
                    if i >= len(row["topics"]):
                        return False
                    if isinstance(expected, list):
                        if row["topics"][i].lower() not in [value.lower() for value in expected]:
                            return False
                    elif row["topics"][i].lower() != expected.lower():
                        return False
                return True
            return [row for row in self.logs if matches(row)]
        raise AssertionError(f"unexpected RPC method {method}")


def binding() -> report.Bindings:
    return report.Bindings(Q, VAULT, ROUTER, POLICY, BUYER, OWNER,
                           "0x" + "ef" * 32, BUY_POOL)


def pending() -> dict:
    return {"txHash": EXIT_TX, "block": 10, "deadline": 100000}


class FeeReporterTests(unittest.TestCase):
    def test_bounded_cursor_discovers_pending_and_never_writes(self):
        rpc = FakeRpc()
        state = {"startBlock": 1, "lastBlock": 9, "lastHash": H[9], "pending": {}}
        result = report.scan_once(rpc, binding(), state, 1, 100, 2, 2)
        self.assertEqual(result["scannedThrough"], 10)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["writesSent"], 0)
        self.assertFalse(result["observations"][0]["reportable"])

    def test_expired_outcome_stays_censor_due_when_evidence_lookback_is_exceeded(self):
        class ExpiredRpc(FakeRpc):
            def call(self, method: str, params: list):
                result = super().call(method, params)
                if method == "eth_getBlockByNumber" and params[0] == hex(18):
                    result["timestamp"] = hex(100000)
                return result

        state = {"startBlock": 1, "lastBlock": 9, "lastHash": H[9], "pending": {}}
        result = report.scan_once(ExpiredRpc(), binding(), state, 1, 5, 2, 2)
        row = result["observations"][0]
        self.assertEqual(row["status"], "censor_due")
        self.assertIn("bounded evidence lookback", row["reason"])
        self.assertFalse(row["reportable"])
        self.assertEqual(result["writesSent"], 0)

    def test_reorged_cursor_halts_before_rpc_log_scan(self):
        rpc = FakeRpc()
        state = {"startBlock": 1, "lastBlock": 9,
                 "lastHash": "0x" + "fe" * 32, "pending": {}}
        with self.assertRaisesRegex(report.ReportError, "reorged"):
            report.scan_once(rpc, binding(), state, 1, 100, 2, 2)

    def test_receipt_authenticated_buy_and_exit_remain_non_reportable_without_gas_audit(self):
        rpc = FakeRpc()
        row = report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)
        self.assertEqual(row["actualEthQCostWei"], "500")
        self.assertEqual(row["recipientCashEthWei"], "400")
        self.assertEqual(row["customPoolSwapCount"], 1)
        self.assertEqual(row["customPoolQInWei"], "10")
        self.assertEqual(row["customPoolXOutWei"], "5")
        self.assertEqual(row["qBurnedWei"], "400")
        self.assertEqual(len(row["knownReceipts"]), 3)
        self.assertFalse(row["reportable"])
        self.assertFalse(row["gasComplete"])

    def test_unknown_q_inflow_censors_exact_cost(self):
        rpc = FakeRpc()
        gift = event(Q, [report.TRANSFER, addr(OWNER), addr(VAULT)],
                     ["uint256"], [600], 2, "0x" + "ee" * 32, 0)
        rpc.logs.insert(0, gift)
        rpc.receipts[gift["transactionHash"]] = {
            "transactionHash": gift["transactionHash"], "blockNumber": "0x2",
            "blockHash": H[2], "status": "0x1", "logs": [gift],
            "gasUsed": "0x1", "effectiveGasPrice": "0x1"}
        rpc.transactions[gift["transactionHash"]] = {
            "hash": gift["transactionHash"], "blockHash": H[2], "from": OWNER,
            "to": Q, "value": "0x0", "input": "0x"}
        row = report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)
        self.assertIsNone(row["actualEthQCostWei"])
        self.assertIn("entry Q spent from unpriced or unexplained inflow", row["costGaps"])
        self.assertFalse(row["reportable"])

    def test_wrong_owner_buyer_pool_does_not_create_cost_basis(self):
        rpc = FakeRpc()
        rpc.logs[1]["topics"][2] = "0x" + "ef" * 32
        row = report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)
        self.assertIsNone(row["actualEthQCostWei"])
        self.assertIn("entry Q spent from unpriced or unexplained inflow", row["costGaps"])

    def test_non_hookless_position_metadata_is_rejected(self):
        rpc = FakeRpc()
        original = rpc.call
        def changed(method, params):
            if method == "eth_call" and params[0]["data"].lower().startswith(report.EXEC_POSITIONS):
                return "0x" + encode(
                    ["uint256", "bytes32", "uint24", "int24", "int24", "int24", "bool",
                     "bool", "uint256", "uint256", "uint256"],
                    [1, bytes.fromhex(("0x" + "ff" * 32)[2:]), 100000, 10,
                     -100, 100, False, True, 500, 10, 300]).hex()
            return original(method, params)
        rpc.call = changed
        with self.assertRaisesRegex(report.ReportError, "zero-hook"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_noncanonical_receipt_blocks_accounting(self):
        rpc = FakeRpc()
        rpc.receipts[BUY_TX]["blockHash"] = H[4]
        with self.assertRaisesRegex(report.ReportError, "noncanonical"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_router_payout_mismatch_blocks_accounting(self):
        rpc = FakeRpc()
        router = rpc.logs[7]
        router["data"] = "0x" + encode(["uint256"] * 6,
                                          [10, 800, 100, 400, 199, 200]).hex()
        with self.assertRaisesRegex(report.ReportError, "do not reconcile"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_log_query_overflow_fails_closed(self):
        rpc = FakeRpc()
        original = rpc.call
        def many(method, params):
            if method == "eth_getLogs":
                return [rpc.logs[0]] * (report.MAX_LOGS_PER_QUERY + 1)
            return original(method, params)
        rpc.call = many
        with self.assertRaisesRegex(report.ReportError, "overflow"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)


if __name__ == "__main__":
    unittest.main()
