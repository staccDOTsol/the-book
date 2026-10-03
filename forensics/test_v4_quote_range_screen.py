"""Boundary cases for the passive one-tick LP price-path screen."""

from __future__ import annotations

import unittest

from v4_quote_range_screen import pool_screen, sqrt_price_at_tick, traverses_interior


class QuoteRangeScreenTests(unittest.TestCase):
    def test_exact_tick_math_boundaries(self):
        self.assertEqual(sqrt_price_at_tick(0), 1 << 96)
        self.assertEqual(sqrt_price_at_tick(-887272), 4295128739)
        self.assertEqual(sqrt_price_at_tick(887272),
                         1461446703485210103287273052203988822378723970342)

    def test_boundary_touch_does_not_traverse_interior(self):
        lower, upper = sqrt_price_at_tick(-60), sqrt_price_at_tick(0)
        self.assertFalse(traverses_interior(upper + 100, upper, lower, upper))
        self.assertFalse(traverses_interior(lower - 100, lower, lower, upper))
        self.assertTrue(traverses_interior(upper + 100, upper - 1, lower, upper))
        self.assertTrue(traverses_interior(lower - 100, lower + 1, lower, upper))

    def test_same_upper_tick_can_still_be_outside_range(self):
        upper = sqrt_price_at_tick(0)
        pool = {
            "poolId": "0x" + "1" * 64, "token": "0x" + "2" * 40,
            "entryBlock": 10, "entryTimestamp": 1000,
            "quoteAsset": "0x" + "3" * 40,
            "entryTick": 0, "entrySqrtPriceX96": str(upper + 100),
            "quoteOnlyOutsideTickRanges": {"widthInTickIntervals": {
                "1": {"tickLower": -60, "tickUpper": 0}}},
            "swapEvents": [{"block": 11, "timestamp": 1001,
                            "postTick": 0, "postSqrtPriceX96": str(upper + 1),
                            "amount0CallerDeltaRaw": "-100", "amount1CallerDeltaRaw": "1",
                            "tx": "0x" + "4" * 64}],
        }
        self.assertEqual(pool_screen(pool)["possibleFeeSwapCount30m"], 0)
        pool["swapEvents"].append({"block": 12, "timestamp": 1002,
                                   "postTick": -1, "postSqrtPriceX96": str(upper - 1),
                                   "amount0CallerDeltaRaw": "-100", "amount1CallerDeltaRaw": "1",
                                   "tx": "0x" + "5" * 64})
        self.assertEqual(pool_screen(pool)["possibleFeeSwapCount30m"], 1)


if __name__ == "__main__":
    unittest.main()
