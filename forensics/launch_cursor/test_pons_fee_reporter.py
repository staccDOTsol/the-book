"""Canonical minted-Q and settlement tests; all RPC traffic is synthetic."""

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
FIRST_EXIT_TX = "0x" + "12" * 32
SECOND_EXIT_TX = "0x" + "13" * 32
HARVEST_TX = "0x" + "ee" * 32
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
        self.transactions = {
            OPEN_TX: {"hash": OPEN_TX, "blockHash": H[5], "from": OWNER,
                      "to": Q, "value": "0x0", "input": "0x"},
            EXIT_TX: {"hash": EXIT_TX, "blockHash": H[10], "from": OWNER,
                      "to": Q, "value": "0x0", "input": "0x"},
            FIRST_EXIT_TX: {"hash": FIRST_EXIT_TX, "blockHash": H[8], "from": OWNER,
                            "to": Q, "value": "0x0", "input": "0x"},
            SECOND_EXIT_TX: {"hash": SECOND_EXIT_TX, "blockHash": H[9], "from": OWNER,
                             "to": Q, "value": "0x0", "input": "0x"},
        }
        self.logs = [event(POLICY, [report.FEE_SELECTED, addr(TOKEN)],
                           ["uint24", "uint8", "bool"], [100000, 5, True], 5, OPEN_TX, 0)]
        index = 1
        for tranche in range(3):
            self.logs.extend([
                event(Q, [report.TRANSFER, addr(report.ZERO), addr(VAULT)],
                      ["uint256"], [200], 5, OPEN_TX, index),
                event(Q, [report.TRANSFER, addr(VAULT), addr(report.POOL_MANAGER)],
                      ["uint256"], [150], 5, OPEN_TX, index + 1),
                event(VAULT, [report.POSITION_OPENED, addr(TOKEN), POOL, word(tranche + 1)],
                      ["uint24", "uint256"], [100000, 150], 5, OPEN_TX, index + 2),
                event(Q, [report.TRANSFER, addr(VAULT), addr(report.ZERO)],
                      ["uint256"], [50], 5, OPEN_TX, index + 3),
                event(Q, [report.OPEN_TRANCHE_MINTED, addr(TOKEN), word(tranche)],
                      ["uint256", "uint256"], [200, 150], 5, OPEN_TX, index + 4),
            ])
            index += 5
        self.logs.extend([
            event(Q, [report.OPEN_MINTED, addr(TOKEN)],
                  ["uint256", "uint256"], [600, 450], 5, OPEN_TX, index),
            event(report.POOL_MANAGER, [report.SWAP, POOL, addr(OWNER)],
                  ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                  [10, -5, 1 << 96, 1000, 0, 100000], 7, "0x" + "dd" * 32, 0),
        ])
        for block, tx_hash in ((8, FIRST_EXIT_TX), (9, SECOND_EXIT_TX)):
            self.logs.extend([
                event(VAULT, [report.POSITION_SETTLED, addr(TOKEN)],
                      ["uint256"] * 4, [10, 100, 100, 150], block, tx_hash, 0),
                event(ROUTER, [report.EXIT_SETTLED, addr(TOKEN)],
                      ["uint256"] * 6, [10, 100, 50, 150, 25, 25], block, tx_hash, 1),
                event(Q, [report.TRANSFER, addr(ROUTER), addr(report.ZERO)],
                      ["uint256"], [150], block, tx_hash, 2),
            ])
        self.logs.extend([
            event(VAULT, [report.POSITION_SETTLED, addr(TOKEN)],
                  ["uint256"] * 4, [10, 100, 800, 400], 10, EXIT_TX, 0),
            event(ROUTER, [report.EXIT_SETTLED, addr(TOKEN)],
                  ["uint256"] * 6, [10, 800, 300, 400, 200, 200], 10, EXIT_TX, 1),
            event(Q, [report.TRANSFER, addr(ROUTER), addr(report.ZERO)],
                  ["uint256"], [400], 10, EXIT_TX, 2),
            event(Q, [report.PENDING, addr(TOKEN)],
                  ["uint64"], [100000], 10, EXIT_TX, 3),
            event(Q, [report.ALL_POSITIONS_EXITED, addr(TOKEN)],
                  [], [], 10, EXIT_TX, 4),
        ])
        self.receipts = {}
        for tx_hash, tx in self.transactions.items():
            block = {OPEN_TX: 5, FIRST_EXIT_TX: 8, SECOND_EXIT_TX: 9,
                     EXIT_TX: 10}[tx_hash]
            self.receipts[tx_hash] = {"transactionHash": tx_hash,
                                      "blockNumber": hex(block), "blockHash": H[block],
                                      "status": "0x1", "logs": [row for row in self.logs
                                                             if row["transactionHash"] == tx_hash],
                                      "gasUsed": "0x186a0", "effectiveGasPrice": "0x3b9aca00"}
        swap = next(item for item in self.logs if item["topics"][0] == report.SWAP)
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

    def test_receipt_authenticated_three_mints_and_exit(self):
        rpc = FakeRpc()
        row = report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)
        self.assertEqual(row["qMintedWei"], "600")
        self.assertEqual(row["qSpentWei"], "450")
        self.assertEqual(row["qUnusedBurnedAtOpenWei"], "150")
        self.assertEqual(row["positionTokenIds"], [1, 2, 3])
        self.assertEqual(row["recipientCashEthWei"], "500")
        self.assertEqual(row["customPoolSwapCount"], 1)
        self.assertEqual(row["customPoolQInWei"], "10")
        self.assertEqual(row["customPoolXOutWei"], "5")
        self.assertEqual(row["qBurnedWei"], "700")
        self.assertEqual(len(row["positionExits"]), 3)
        self.assertEqual(len(row["settlementBurns"]), 3)
        self.assertEqual(len(row["knownReceipts"]), 4)
        self.assertTrue(row["grossMarkEvidenceComplete"])
        self.assertFalse(row["reportable"])
        self.assertFalse(row["gasComplete"])

    def test_missing_tranche_mint_event_blocks_feedback(self):
        rpc = FakeRpc()
        missing = next(item for item in rpc.logs if item["topics"][0] == report.OPEN_TRANCHE_MINTED)
        rpc.logs.remove(missing)
        rpc.receipts[OPEN_TX]["logs"].remove(missing)
        with self.assertRaisesRegex(report.ReportError, "three tranche mint events"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_unreconciled_mint_transfer_blocks_feedback(self):
        rpc = FakeRpc()
        minted = next(item for item in rpc.logs if item["topics"][0] == report.TRANSFER and
                      item["topics"][1] == addr(report.ZERO))
        minted["data"] = "0x" + encode(["uint256"], [199]).hex()
        with self.assertRaisesRegex(report.ReportError, "Transfers do not reconcile"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_tranches_cannot_use_different_pool_ids(self):
        rpc = FakeRpc()
        opened = [item for item in rpc.logs if item["topics"][0] == report.POSITION_OPENED]
        opened[1]["topics"][2] = "0x" + "ff" * 32
        with self.assertRaisesRegex(report.ReportError, "one pool"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_noncanonical_receipt_blocks_accounting(self):
        rpc = FakeRpc()
        rpc.receipts[OPEN_TX]["blockHash"] = H[4]
        with self.assertRaisesRegex(report.ReportError, "noncanonical"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_router_payout_mismatch_blocks_accounting(self):
        rpc = FakeRpc()
        router = next(item for item in rpc.logs if item["topics"][0] == report.EXIT_SETTLED)
        router["data"] = "0x" + encode(["uint256"] * 6,
                                          [10, 800, 100, 400, 199, 200]).hex()
        with self.assertRaisesRegex(report.ReportError, "do not reconcile"):
            report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)

    def test_interim_harvest_cash_and_burn_are_included_once(self):
        rpc = FakeRpc()
        rows = [
            event(VAULT, [report.FEES_SETTLED, addr(TOKEN)],
                  ["uint256"] * 4, [2, 30, 80, 40], 8, HARVEST_TX, 0),
            event(ROUTER, [report.EXIT_SETTLED, addr(TOKEN)],
                  ["uint256"] * 6, [2, 80, 10, 40, 20, 20], 8, HARVEST_TX, 1),
            event(Q, [report.TRANSFER, addr(ROUTER), addr(report.ZERO)],
                  ["uint256"], [40], 8, HARVEST_TX, 2),
        ]
        rpc.logs.extend(rows)
        rpc.transactions[HARVEST_TX] = {"hash": HARVEST_TX, "blockHash": H[8],
                                        "from": OWNER, "to": Q, "value": "0x0", "input": "0x"}
        rpc.receipts[HARVEST_TX] = {"transactionHash": HARVEST_TX,
                                     "blockNumber": "0x8", "blockHash": H[8],
                                     "status": "0x1", "logs": rows,
                                     "gasUsed": "0x186a0", "effectiveGasPrice": "0x3b9aca00"}
        row = report.collect_exit(rpc, binding(), TOKEN, pending(), 1, 18, 100)
        self.assertEqual(row["recipientCashEthWei"], "540")
        self.assertEqual(row["qBurnedWei"], "740")
        self.assertEqual(row["exitRecipientCashEthWei"], "400")
        self.assertEqual(row["exitQBurnedWei"], "400")
        self.assertEqual(len(row["interimHarvests"]), 1)
        self.assertEqual(len(row["settlementBurns"]), 4)
        self.assertEqual(len(row["knownReceipts"]), 5)

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
