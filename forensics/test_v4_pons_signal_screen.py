import unittest

from v4_pons_signal_screen import PONS_HOOK, ZERO_HOOK, build


ETH = "0x" + "0" * 40
TOKEN = "0x" + "a" * 40
CREATOR = "0x" + "b" * 40


def birth(i, *, hook=ZERO_HOOK, fee=1000):
    block = 100 + i
    return {"poolId": "0x" + f"{i + 1:064x}", "token": TOKEN,
            "currency0": ETH, "currency1": TOKEN, "quoteAsset": ETH,
            "feePips": fee, "tickSpacing": 200, "hooks": hook,
            "birthBlock": block, "birthTimestamp": 1000 + 10 * i,
            "birthTime": f"time-{i}", "birthTx": "0x" + f"{i + 10:064x}",
            "birthTransactionIndex": 0, "birthLogIndex": i}


def registration(pool):
    def word(addr):
        return addr[2:].rjust(64, "0")
    return {"topics": ["0x" + "f" * 64, pool["poolId"]],
            "data": "0x" + word(TOKEN) + word(ETH) + word(CREATOR),
            "blockNumber": hex(pool["birthBlock"]),
            "transactionHash": pool["birthTx"]}


class PonsSignalScreenTest(unittest.TestCase):
    def test_hook_birth_precedes_completed_burst_and_independent_pool_can_precede_hook(self):
        pools = [birth(0), birth(1, hook=PONS_HOOK, fee=0),
                 birth(2), birth(3), birth(4)]
        funded = [{"rule": "4/300", "token": TOKEN,
                   "funded": {"status": "funded_signal", "entryBlock": 103,
                              "entryTimestamp": 1030}},
                  {"rule": "5/600", "token": TOKEN,
                   "funded": {"status": "funded_signal", "entryBlock": 104,
                              "entryTimestamp": 1040}}]
        result = build({"birthFromBlock": 100, "toBlock": 104, "births": pools},
                       [registration(pools[1])], funded)
        self.assertEqual(result["hookAllQuotes"]["firstIndependentPoolBeforeHook"], 1)
        self.assertEqual(result["rules"]["3/300"]["rawSignalTokens"], 1)
        self.assertEqual(result["rules"]["4/300"]["strictFundedTokens"], 1)
        self.assertEqual(result["rules"]["5/600"]["strictFundedTokens"], 1)
        by_rule = {row["rule"]: row for row in result["signals"]}
        self.assertEqual(by_rule["3/300"]["funded"]["status"],
                         "not_scanned_for_strict_funding")
        self.assertEqual(by_rule["4/300"]["secondsPonsBeforeFundedSignal"], 20)
        self.assertEqual(by_rule["5/600"]["secondsPonsBeforeFundedSignal"], 30)

    def test_rejects_registration_not_in_initialize_transaction(self):
        hook = birth(0, hook=PONS_HOOK, fee=0)
        log = registration(hook)
        log["transactionHash"] = "0x" + "f" * 64
        with self.assertRaisesRegex(ValueError, "registration mismatch"):
            build({"birthFromBlock": 100, "toBlock": 100, "births": [hook]},
                  [log], [])


if __name__ == "__main__":
    unittest.main()
