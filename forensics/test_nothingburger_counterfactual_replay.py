"""Accounting and direction checks for the synthetic X/N LP replay."""

import json
import math
import unittest

try:
    from . import nothingburger_counterfactual_replay as replay
except ImportError:
    import nothingburger_counterfactual_replay as replay


class CounterfactualReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = json.loads(replay.SOURCE.read_text())["paths"]

    def test_no_flow_is_launch_hold_less_lp_gas(self):
        paths = [{**p, "swaps": []} for p in self.paths]
        for tax in (0, 500, 1000):
            row = replay.replay(paths, 100, tax)
            self.assertEqual(row["hypotheticalArbTrades"], 0)
            self.assertEqual(row["activatedPositions"], 0)
            self.assertEqual(row["entryCount"], 19)
            self.assertEqual(row["exitCount"], 19)
            expected = replay.launch_hold_baseline(tax) - 19 * replay.MINT_BURN_GAS_ETH
            self.assertAlmostEqual(row["walletEndEth"], expected, places=10)

    def test_matrix_wallet_conserves_reported_cash_flows(self):
        data = json.loads(replay.OUTPUT.read_text())
        for row in data["grid"] + data["exactExternalSingleTickSensitivity"]:
            expected = (replay.GAS_RESERVE_ETH + row["externalXLiquidationEth"] +
                        (row["endingNRedeemNetEth"] or 0) +
                        row["totalCreatorFeesEth"] - row["sampledMintBurnGasEth"])
            self.assertAlmostEqual(row["walletEndEth"], expected, places=12)
            self.assertTrue(math.isfinite(row["walletProfitUsd"]))
            self.assertEqual(row["entryCount"], row["exitCount"])
            self.assertGreaterEqual(row["unredeemedNUnits"], 0)

    def test_positive_row_depends_on_one_historical_rebound(self):
        best = replay.replay(self.paths, 5000, 0)
        without_rebound = replay.replay(
            [p for p in self.paths if not p["token"].startswith("0xb3d19008")],
            5000, 0)
        self.assertGreater(best["walletProfitUsd"], 0)
        self.assertLess(without_rebound["walletProfitUsd"], 0)
        one_tick = replay.replay(self.paths, 5000, 0, external_mode="one_tick")
        self.assertEqual(one_tick["xUnitsBeyondKnownExternalExitTick"], 0)

    def test_exact_external_same_state_roundtrip_loses(self):
        path = self.paths[0]
        dev = replay.quote_buy(replay.CONFIG, replay.opening_state(replay.CONFIG),
                               replay.to_wei(replay.DEV_BUY_ETH), 0)
        state = replay.after_buy(replay.opening_state(replay.CONFIG), dev)
        pos = replay.make_position(path, 10_000, state, 100, 200)
        x = 100.0
        cost = replay.external_buy_x_cost(pos, x, "one_tick")
        proceeds = replay.external_sell_x_proceeds(pos, x, "one_tick")
        self.assertIsNotNone(cost)
        self.assertIsNotNone(proceeds)
        self.assertGreater(cost, proceeds)


if __name__ == "__main__":
    unittest.main()
