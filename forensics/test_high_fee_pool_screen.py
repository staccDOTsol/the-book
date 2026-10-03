"""Deterministic checks for the read-only LP screen's causal boundaries."""

import unittest

from export_high_fee_screen import classify_quote_band_30m
from high_fee_pool_screen import (USDG, block_crossings,
                                  quote_deposit_from_liquidity, quote_only_ranges)
from high_fee_window_counts import is_within
from v4_quote_range_screen import sqrt_price_at_tick


class HighFeePoolScreenTests(unittest.TestCase):
    def test_launch_seed_quote_estimate_matches_receipt_within_one_raw_unit(self):
        # LAUNCH high-fee pool #2: the independent birth transaction receipt
        # transferred 21,960 raw USDG to PoolManager.
        birth = {"quoteSide": 0, "quoteAsset": USDG}
        seed = {"delta": 9_820_390_340_530, "tickLower": -881100,
                "tickUpper": 881100}
        sqrt_price = 35_430_395_347_332_058_992_410_889_015_847_131_205
        result = quote_deposit_from_liquidity(birth, seed, sqrt_price)
        self.assertEqual(result["status"], "theoretical_from_L_and_price")
        self.assertLessEqual(abs(int(result["estimatedQuoteRaw"]) - 21_960), 1)

    def test_quote_only_range_is_outside_completed_tick_on_correct_side(self):
        quote0 = quote_only_ranges(398390, 9900, 0)["widthInTickIntervals"]["1"]
        quote1 = quote_only_ranges(398390, 9900, 1)["widthInTickIntervals"]["1"]
        self.assertEqual(quote0, {"tickLower": 405900, "tickUpper": 415800})
        self.assertEqual(quote1, {"tickLower": 386100, "tickUpper": 396000})
        self.assertIsNone(quote_only_ranges(881100, 9900, 0)["widthInTickIntervals"]["1"])

    def test_later_pool_addition_resets_peak_and_strict_drawdown(self):
        # Initial pool adds 100; a later pool adds 100. A withdrawal to 150
        # equals 75% and must not cross; the next unit at block 103 does.
        series = block_crossings([(100, 100), (101, 100), (102, -50), (103, -1)], 100)
        self.assertEqual(series["peakOutstanding"], "200")
        self.assertEqual(series["crossings"]["0.75"]["block"], 103)

    def test_adjacent_time_bounds_prove_window_or_require_exact_lookup(self):
        times = {100: 1000, 110: 1002, 120: 1004}
        keys = [100, 110, 120]
        self.assertIs(is_within(105, 1002, times, keys), True)
        self.assertIs(is_within(115, 1001, times, keys), False)
        self.assertIsNone(is_within(105, 1001, times, keys))

    def test_boundary_touch_is_not_interior_crossing(self):
        lower = sqrt_price_at_tick(10)
        upper = sqrt_price_at_tick(20)
        pool = {"quoteAsset": USDG, "quoteSide": 0, "entryBlock": 100,
                "entryTimestamp": 1000, "entrySqrtPriceX96": str(lower - 100),
                "swapEvents": [{"block": 101, "timestamp": 1100, "postTick": 10,
                                "postSqrtPriceX96": str(lower)}]}
        band = {"tickLower": 10, "tickUpper": 20}
        self.assertEqual(classify_quote_band_30m(pool, band)[0], "boundary_only")
        pool["swapEvents"].append({"block": 102, "timestamp": 1200, "postTick": 15,
                                  "postSqrtPriceX96": str((lower + upper) // 2)})
        self.assertEqual(classify_quote_band_30m(pool, band)[0], "interior_crossed_end_mixed")

    def test_boundary_tick_can_still_be_outside_band(self):
        lower = sqrt_price_at_tick(10)
        pool = {"quoteAsset": USDG, "quoteSide": 0, "entryBlock": 100,
                "entryTimestamp": 1000, "entrySqrtPriceX96": str(lower - 100),
                "swapEvents": [{"block": 101, "timestamp": 1100, "postTick": 10,
                                "postSqrtPriceX96": str(lower - 1)}]}
        self.assertEqual(classify_quote_band_30m(pool, {"tickLower": 10, "tickUpper": 20})[0],
                         "swap_no_boundary")


if __name__ == "__main__":
    unittest.main()
