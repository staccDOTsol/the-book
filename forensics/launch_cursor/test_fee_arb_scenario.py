"""Independent conservation and quote-identity checks for the fee scenario."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fee_arb_scenario as scenario


class FeeArbScenarioTests(unittest.TestCase):
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
