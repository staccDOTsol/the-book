import unittest
from decimal import Decimal

from pons_arb_fee_math import build, fee_hurdle


class PonsArbFeeMathTest(unittest.TestCase):
    def test_one_percent_each_hop_requires_more_than_two_percent_gap(self):
        expected = Decimal(1) / (Decimal("0.99") ** 2) - 1
        self.assertLess(abs(fee_hurdle(100, 100, 0) - expected), Decimal("1e-25"))

    def test_tax_and_external_fee_raise_hurdle(self):
        baseline = fee_hurdle(100, 700, 0)
        self.assertGreater(fee_hurdle(200, 700, 0), baseline)
        self.assertGreater(fee_hurdle(100, 700, 30), baseline)

    def test_creator_cash_is_separate_from_protocol_and_buyback(self):
        row = next(row for row in build()["rows"]
                   if row["creatorTaxBps"] == 1000 and row["independentPoolFeeBps"] == 100)
        self.assertEqual(Decimal(row["taxOn100UsdEquivalent"]), Decimal("10"))
        self.assertEqual(Decimal(row["creatorBaseFeeCashOn100UsdEquivalent"]),
                         Decimal("0.7"))
        self.assertEqual(Decimal(row["creatorTotalCashOn100UsdEquivalent"]),
                         Decimal("10.7"))
        with_buyback = next(row for row in build(buyback_from_creator_bps=5000)["rows"]
                            if row["creatorTaxBps"] == 1000 and row["independentPoolFeeBps"] == 100)
        self.assertEqual(Decimal(with_buyback["creatorTotalCashOn100UsdEquivalent"]),
                         Decimal("10.35"))


if __name__ == "__main__":
    unittest.main()
