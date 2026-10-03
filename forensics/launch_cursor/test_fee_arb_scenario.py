"""Independent conservation and quote-identity checks for the fee scenario."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fee_arb_scenario as scenario


class FeeArbScenarioTests(unittest.TestCase):
    def test_illustrative_matrix_uses_point_one_percent_without_moving_p0(self):
        mints, deposits = scenario.illustrative_mints_and_deposits()
        self.assertEqual(mints, [Decimal("1000000"), Decimal("1001000"),
                                 Decimal("1002001")])
        self.assertEqual(deposits, [Decimal("950000"), Decimal("950950"),
                                    Decimal("951900.95")])
        self.assertEqual(scenario.ILLUSTRATIVE_P0, Decimal("0.01"))
        rows = scenario.illustrative_matrix_rows()
        self.assertEqual(len(rows), 46 * 4)
        self.assertEqual(rows[0]["fee_pct"], "5")
        self.assertEqual(rows[45]["fee_pct"], "50")
        self.assertEqual(rows[46]["pons_price_factor"], "0.75")
        first = scenario.analyze_pool(scenario.ILLUSTRATIVE_P0,
                                      scenario.ILLUSTRATIVE_R,
                                      scenario.ILLUSTRATIVE_EXTERNAL_Q_PER_X,
                                      50_000, deposits)
        self.assertEqual(Decimal(first["qDepositedTotal"]),
                         sum(deposits))
        with (Path(__file__).resolve().parent /
              "fee_arb_matrix_illustrative.csv").open(newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle)), rows)

    def test_half_fee_cannot_close_top_band_marginal_gap_at_external_p0(self):
        result = scenario.analyze_pool("1", "2", "1", 500_000, ["1", "1", "1"])
        self.assertEqual(Decimal(result["poolStopQPerX"]), Decimal(2))
        self.assertEqual(Decimal(result["bands"][2]["feeRequiredToStopFirstMarginalArb"]),
                         Decimal("0.95"))
        self.assertEqual(Decimal(result["bands"][1]["feeRequiredForFullSweepBreakEven"]),
                         Decimal("0.5"))
        self.assertEqual(Decimal(result["bands"][0]["qExtractedAtOptimalStop"]), 0)
        self.assertGreater(Decimal(result["bands"][2]["qExtractedAtOptimalStop"]),
                           Decimal("0.88"))
        self.assertEqual(Decimal(result["issuerLPMarkLossQ"]),
                         Decimal(result["arbitrageurGrossGainQBeforeGas"]))

    def test_external_price_above_fee_adjusted_top_has_no_curve_arb(self):
        result = scenario.analyze_pool("1", "2", "11", 500_000, ["1", "1", "1"])
        self.assertEqual(Decimal(result["qExtractedTotal"]), 0)
        self.assertEqual(Decimal(result["arbitrageurGrossGainQBeforeGas"]), 0)

    def test_executable_quote_bundle_requires_same_block_and_matching_sizes(self):
        block = "0x" + "ab" * 32
        bundle = {
            "xq": {"blockNumber": 42, "blockHash": block,
                   "xInputWei": "100", "qOutputWei": "700"},
            "ponsBuy": {"blockNumber": 42, "blockHash": block,
                        "xOutputWei": "100", "ethInputWei": "50"},
            "qSale": {"blockNumber": 42, "blockHash": block,
                      "qInputWei": "700", "ethOutputWei": "80"},
            "gasEthWei": "5",
        }
        result = scenario.check_executable_route(bundle)
        self.assertEqual(result["routeNetEthWei"], "25")
        self.assertTrue(result["profitableAtTheseExactSizes"])
        bundle["qSale"]["qInputWei"] = "699"
        with self.assertRaisesRegex(scenario.ScenarioError, "sizes do not match"):
            scenario.check_executable_route(bundle)


if __name__ == "__main__":
    unittest.main()
