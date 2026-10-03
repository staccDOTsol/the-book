"""Synthetic evidence tests; no RPC calls or transactions."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fee_outcome_ledger as ledger
import pons_fee_feedback_keeper as feedback


Q = 10**18
TOKEN = "0x" + "11" * 20
PAYER = "0x" + "22" * 20
EXTERNAL = "0x" + "44" * 20
OPEN_TX = "0x" + "aa" * 32
EXIT_TX = "0x" + "bb" * 32
EXTERNAL_TX = "0x" + "cc" * 32


def example() -> dict:
    return {
        "schema_version": 1,
        "chain_id": 4663,
        "as_of_block": 200,
        "strategy_gas_payers": [PAYER],
        "observations": [{
            "token": TOKEN,
            "fee_pips": 100_000,
            "selection_tx_hash": OPEN_TX,
            "stage": "exited",
            "open": {"tx_hash": OPEN_TX, "block_number": 100,
                     "q_spent_wei": str(1000 * Q)},
            "exit": {"tx_hash": EXIT_TX, "block_number": 150,
                     "x_settled_wei": str(10 * Q), "q_settled_wei": str(300 * Q),
                     "x_sold_wei": str(10 * Q), "eth_out_wei": str(400 * Q),
                     "q_bought_wei": str(100 * Q), "q_burned_wei": str(400 * Q),
                     "wizard_weth_wei": str(100 * Q),
                     "developer_eth_wei": str(100 * Q)},
            "q_cost_lots": [{"q_amount_wei": str(1000 * Q),
                             "eth_paid_wei": str(250 * Q),
                             "acquisition_ref": "synthetic acquisition receipt"}],
            "gas_complete": True,
            "gas_scope_ref": "synthetic all-attributable-tx audit",
            "gas_receipts": [
                {"tx_hash": OPEN_TX, "payer": PAYER, "role": "open",
                 "gas_used": 100_000, "effective_gas_price_wei": 10**9,
                 "extra_fee_wei": 0},
                {"tx_hash": EXIT_TX, "payer": PAYER, "role": "exit",
                 "gas_used": 200_000, "effective_gas_price_wei": 10**9,
                 "extra_fee_wei": 0},
                {"tx_hash": EXTERNAL_TX, "payer": EXTERNAL, "role": "external transfer trigger",
                 "gas_used": 100_000, "effective_gas_price_wei": 10**9,
                 "extra_fee_wei": 0},
            ],
            "entry_q_eth_quote": {"q_amount_wei": str(1000 * Q),
                                  "eth_out_wei": str(250 * Q),
                                  "block_number": 100, "route": "Q/ETH",
                                  "source_ref": "synthetic exact-input quote"},
            "exit_burn_q_eth_quote": {"q_amount_wei": str(400 * Q),
                                      "eth_out_wei": str(120 * Q),
                                      "block_number": 150, "route": "Q/ETH",
                                      "source_ref": "synthetic exact-input quote"},
            "swap_volume": {"swap_count": 4, "x_in_wei": str(11 * Q),
                            "q_in_wei": str(6 * Q), "through_block": 150,
                            "source_ref": "synthetic complete Swap logs"},
        }],
    }


class OutcomeLedgerTests(unittest.TestCase):
    def test_reconciles_cash_burn_gas_and_candidate_separately(self):
        result = ledger.analyze(example())
        row = result["observations"][0]
        self.assertEqual(row["classification"], "cash_feedback_eligible_exit")
        self.assertEqual(row["realized_recipient_cash_eth_wei"], str(200 * Q))
        self.assertEqual(row["eth_spent_buying_q_wei"], str(200 * Q))
        self.assertEqual(row["q_recovered_and_burned_wei"], str(300 * Q))
        self.assertEqual(row["q_bought_and_burned_wei"], str(100 * Q))
        self.assertEqual(row["known_attributed_gas_eth_wei"], str(400_000 * 10**9))
        self.assertEqual(row["strategy_paid_gas_eth_wei"], str(300_000 * 10**9))
        self.assertEqual(row["external_payer_gas_eth_wei"], str(100_000 * 10**9))
        self.assertEqual(result["unique_linked_receipts_network_gas_eth_wei"], str(400_000 * 10**9))
        self.assertEqual(row["recipient_cash_after_q_cost_and_strategy_gas_wei"],
                         str(-50 * Q - 300_000 * 10**9))
        self.assertEqual(row["system_cash_after_q_cost_and_all_gas_wei"],
                         str(-50 * Q - 400_000 * 10**9))
        self.assertEqual(row["system_cash_plus_burn_valuation_estimate_wei"],
                         str(70 * Q - 400_000 * 10**9))
        self.assertEqual(row["cash_return_on_actual_q_cost_bps_clipped"], -2000)
        self.assertEqual(result["arms"][0]["cash_feedback_eligible_exits"], 1)
        self.assertEqual(result["arms"][0]["known_recipient_cash_eth_wei"], str(200 * Q))
        self.assertEqual(result["arms"][0]["known_q_burned_wei"], str(400 * Q))
        self.assertEqual(result["arms"][0]["swap_count_from_complete_windows"], 4)

    def test_cash_feedback_does_not_require_hypothetical_burn_quote(self):
        document = example()
        del document["observations"][0]["exit_burn_q_eth_quote"]
        row = ledger.analyze(document)["observations"][0]
        self.assertEqual(row["classification"], "cash_feedback_eligible_exit")
        self.assertIsNotNone(row["cash_return_on_actual_q_cost_bps_clipped"])
        self.assertIsNone(row["system_cash_plus_burn_valuation_estimate_wei"])
        self.assertIn("size-aware exit burn Q/ETH liquidation quote", row["missing_for_burn_valuation"])

    def test_open_idle_position_is_censored_not_zero_return(self):
        document = example()
        row = document["observations"][0]
        row["stage"] = "active"
        del row["exit"]
        del row["exit_burn_q_eth_quote"]
        row["gas_receipts"] = row["gas_receipts"][:1]
        row["swap_volume"] = {"swap_count": 0, "x_in_wei": "0", "q_in_wei": "0",
                              "through_block": 200, "source_ref": "synthetic complete Swap logs"}
        report = ledger.analyze(document)
        self.assertEqual(report["observations"][0]["classification"], "right_censored_active")
        self.assertIsNone(report["observations"][0]["cash_return_on_actual_q_cost_bps_clipped"])
        self.assertEqual(report["arms"][0]["idle_open_at_horizon"], 1)

    def test_rejects_router_or_executor_mismatch(self):
        document = example()
        document["observations"][0]["exit"]["q_burned_wei"] = str(401 * Q)
        with self.assertRaisesRegex(ledger.EvidenceError, "settlement"):
            ledger.analyze(document)
        document = example()
        document["observations"][0]["exit"]["developer_eth_wei"] = str(101 * Q)
        with self.assertRaisesRegex(ledger.EvidenceError, "payouts"):
            ledger.analyze(document)

    def test_unattributed_gas_cannot_make_candidate(self):
        document = example()
        row = document["observations"][0]
        row["gas_complete"] = False
        row.pop("gas_scope_ref")
        row["gas_receipts"] = row["gas_receipts"][:1]
        outcome = ledger.analyze(document)["observations"][0]
        self.assertIsNone(outcome["recipient_cash_after_q_cost_and_strategy_gas_wei"])
        self.assertIsNone(outcome["cash_return_on_actual_q_cost_bps_clipped"])

    def test_zero_cost_q_is_not_a_cash_return_candidate(self):
        document = example()
        document["observations"][0]["q_cost_lots"][0]["eth_paid_wei"] = "0"
        row = ledger.analyze(document)["observations"][0]
        self.assertIsNotNone(row["recipient_cash_after_q_cost_and_strategy_gas_wei"])
        self.assertIsNone(row["cash_return_on_actual_q_cost_bps_clipped"])
        self.assertIn("positive actual Q acquisition cost", row["missing_for_cash_feedback"])

    def test_partial_gas_attribution_requires_audit_reference(self):
        document = example()
        receipt = document["observations"][0]["gas_receipts"][2]
        receipt["attributed_fee_wei"] = 50_000 * 10**9
        with self.assertRaisesRegex(ledger.EvidenceError, "allocation_ref"):
            ledger.analyze(document)
        receipt["allocation_ref"] = "synthetic incremental-gas allocation"
        row = ledger.analyze(document)["observations"][0]
        self.assertEqual(row["known_attributed_gas_eth_wei"], str(350_000 * 10**9))
        self.assertEqual(row["linked_receipts_full_network_gas_eth_wei"], str(400_000 * 10**9))

    def test_unsigned_call_encodes_negative_cash_bps_and_content_hash(self):
        document = example()
        document["cursor_address"] = "0x" + "55" * 20
        calls = ledger.draft_report_calls(document, ledger.analyze(document))
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["to"], document["cursor_address"])
        self.assertEqual(call["cash_return_bps"], -2000)
        self.assertEqual(call["calldata"][:10], "0x173510f8")
        self.assertEqual(int(call["calldata"][74:138], 16), (1 << 256) - 2000)
        self.assertEqual(call["calldata"][-64:], call["evidence_hash"][2:])
        self.assertIn("unsigned draft", call["status"])

    def test_complete_gas_requires_selection_open_and_exit_receipts(self):
        document = example()
        document["observations"][0]["gas_receipts"] = document["observations"][0]["gas_receipts"][:1]
        with self.assertRaisesRegex(ledger.EvidenceError, "omit"):
            ledger.analyze(document)

    def test_duplicate_or_overallocated_receipt_is_rejected(self):
        document = example()
        other = copy.deepcopy(document["observations"][0])
        other["token"] = "0x" + "33" * 20
        other["selection_tx_hash"] = "0x" + "dd" * 32
        other["open"]["tx_hash"] = other["selection_tx_hash"]
        other["exit"]["tx_hash"] = "0x" + "ee" * 32
        other["gas_receipts"][0]["tx_hash"] = other["selection_tx_hash"]
        other["gas_receipts"][1]["tx_hash"] = other["exit"]["tx_hash"]
        other["gas_receipts"] = other["gas_receipts"][:2]
        shared = {"tx_hash": "0x" + "ff" * 32, "payer": PAYER,
                  "role": "shared acquisition", "gas_used": 100_000,
                  "effective_gas_price_wei": 10**9, "extra_fee_wei": 0}
        document["observations"][0]["gas_receipts"].append(shared)
        other["gas_receipts"].append(copy.deepcopy(shared))
        document["observations"].append(other)
        with self.assertRaisesRegex(ledger.EvidenceError, "multiply attributed"):
            ledger.analyze(document)

    def test_rejects_spot_multiplication_masked_as_wrong_size_quote(self):
        document = example()
        document["observations"][0]["entry_q_eth_quote"]["q_amount_wei"] = str(Q)
        with self.assertRaisesRegex(ledger.EvidenceError, "whole relevant Q"):
            ledger.analyze(document)


class MintedQuoteLedgerTests(unittest.TestCase):
    def evidence(self) -> dict:
        observed = {"token": TOKEN, "feePips": 100_000, "trancheCount": 3,
                    "qMintedWei": "1200", "qUnusedBurnedAtOpenWei": "200",
                    "qSpentWei": "1000", "qBurnedWei": "800",
                    "settlementBurns": [
                        {"kind": "position_exit", "block": 120, "qBurnedWei": "200"},
                        {"kind": "position_exit", "block": 130, "qBurnedWei": "200"},
                        {"kind": "position_exit", "block": 150, "qBurnedWei": "400"}],
                    "recipientCashEthWei": "400", "openBlock": 100,
                    "exitBlock": 150}
        return {"schemaVersion": 1, "chainId": 4663, "token": TOKEN,
                "observation": observed,
                "entryQuote": {"route": "zero-hook Q/ETH exact-input Q sale",
                               "sourceBlock": 99, "sourceBlockHash": "0x" + "aa" * 32,
                               "qInputWei": "1000", "ethOutputWei": "500"},
                "burnQuotes": [
                    {"route": "zero-hook Q/ETH exact-input Q sale",
                     "sourceBlock": 119, "sourceBlockHash": "0x" + "bb" * 32,
                     "qInputWei": "200", "ethOutputWei": "75"},
                    {"route": "zero-hook Q/ETH exact-input Q sale",
                     "sourceBlock": 129, "sourceBlockHash": "0x" + "cc" * 32,
                     "qInputWei": "200", "ethOutputWei": "75"},
                    {"route": "zero-hook Q/ETH exact-input Q sale",
                     "sourceBlock": 149, "sourceBlockHash": "0x" + "dd" * 32,
                     "qInputWei": "400", "ethOutputWei": "150"}],
                "score": feedback.gross_mark_score(observed, 500, 300)}

    def test_minted_q_score_includes_idle_and_censored_assignments(self):
        document = {"schema_version": 2, "chain_id": 4663,
                    "as_of_block": 200, "cursor_address": "0x" + "55" * 20,
                    "observations": [
                        {"token": TOKEN, "fee_pips": 100_000, "stage": "exited",
                         "evidence": self.evidence()},
                        {"token": "0x" + "33" * 20, "fee_pips": 100_000,
                         "stage": "active", "observed_swap_count": 0},
                        {"token": "0x" + "44" * 20, "fee_pips": 100_000,
                         "stage": "exited"},
                    ]}
        outcome = ledger.analyze(document)
        arm = outcome["arms"][0]
        self.assertEqual(outcome["observations"][0]["gross_mark_score_bps"], 4000)
        self.assertEqual(arm["assigned"], 3)
        self.assertEqual(arm["completed_with_gross_mark"], 1)
        self.assertEqual(arm["idle_open_at_horizon"], 1)
        self.assertEqual(arm["unvalued_or_censored"], 1)
        self.assertEqual(arm["selection_score_sum_bps"], -6000)
        self.assertFalse(outcome["profit_claim"])
        calls = ledger.draft_report_calls(document, outcome)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["gross_mark_score_bps"], 4000)

    def test_minted_q_quote_must_cover_full_burn_at_previous_block(self):
        document = {"schema_version": 2, "chain_id": 4663, "as_of_block": 200,
                    "observations": [{"token": TOKEN, "fee_pips": 100_000,
                                      "stage": "exited", "evidence": self.evidence()}]}
        document["observations"][0]["evidence"]["burnQuotes"][0]["qInputWei"] = "199"
        with self.assertRaisesRegex(ledger.EvidenceError, "quotes do not cover exact"):
            ledger.analyze(document)


if __name__ == "__main__":
    unittest.main()
