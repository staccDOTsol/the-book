"""Birth-signal cohort and completed-block lookahead checks."""

from __future__ import annotations

import unittest

from v4_all_fee_bursts import build, safe_lp_targets


def birth(block: int, index: int, *, hooked: bool = False) -> dict:
    return {"poolId": "0x" + f"{block * 10 + index:064x}",
            "token": "0x" + "a" * 40, "quoteAsset": "0x" + "b" * 40,
            "currency0": "0x" + "b" * 40, "currency1": "0x" + "a" * 40,
            "feePips": 3_000, "tickSpacing": 60,
            "hooks": "0x" + ("1" if hooked else "0") * 40,
            "birthBlock": block, "birthTimestamp": 1000 + block,
            "birthTime": f"t{block}", "birthTx": "0x" + f"{block:064x}",
            "birthTransactionIndex": 0, "birthLogIndex": index}


class AllFeeBurstTests(unittest.TestCase):
    def test_hooked_birth_can_form_signal_but_is_not_lp_target(self):
        births = [birth(10, 0), birth(11, 0, hooked=True), birth(12, 0),
                  birth(13, 0), birth(13, 1), birth(14, 0)]
        result = build({"fromBlock": 9, "birthFromBlock": 10,
                        "toBlock": 14, "births": births})
        static = result["cohorts"]["static_quote"]["rules"]["4/300"]["signals"]
        strict = result["cohorts"]["zero_hook_static_quote"]["rules"]["4/300"]["signals"]
        self.assertEqual(static[0]["entryBlock"], 13)
        self.assertEqual(strict[0]["entryBlock"], 13)
        # Later logs in the completed signal block are known, future blocks
        # are not; the hooked pool is signal evidence but no LP destination.
        self.assertEqual(len(static[0]["poolIds"]), 5)
        self.assertEqual(len(static[0]["laterPoolsNotKnownAtSignal"]), 1)
        targets = safe_lp_targets(result)["signals"]
        row = next(item for item in targets if item["rule"] == "4/300")
        self.assertEqual(len(row["signalPoolIdsIncludingHooked"]), 5)
        self.assertEqual(len(row["poolIds"]), 4)
        self.assertNotIn(births[1]["poolId"], row["poolIds"])


if __name__ == "__main__":
    unittest.main()
