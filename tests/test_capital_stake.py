"""Native staking, and the interpretation mistake it must not make.

A wallet deactivating a large stake is freeing capital. That is
CAPITAL_LIQUIDITY_INCREASING — not "about to sell". Deactivated SOL is
commonly restaked, lent, posted as collateral or redeployed, and calling
it bearish because the stake shrank is the same error as reading
SOL -> JitoSOL as a sale.
"""
import unittest
from unittest.mock import patch

from lib.capital_stake import (MIN_MATERIAL_SOL, NOT_DEACTIVATING,
                               OFFSET_STAKER, OFFSET_WITHDRAWER,
                               capital_liquidity_signal, stake_accounts_for)

WALLET = "AohChej14wwqmkbYzuGE4YZVNH9tfFaZuKUN8nxEfRVU"
LAMPORTS = 1_000_000_000


def acct(sol, *, deact=NOT_DEACTIVATING, act=100, voter="V" * 32,
         staker="S" * 32, withdrawer=WALLET):
    return {
        "pubkey": "P" * 32,
        "account": {
            "lamports": int(sol * LAMPORTS),
            "data": {"parsed": {"type": "delegated", "info": {
                "meta": {"authorized": {"staker": staker, "withdrawer": withdrawer},
                         "lockup": {"unixTimestamp": 0}},
                "stake": {"delegation": {"voter": voter, "activationEpoch": str(act),
                                         "deactivationEpoch": str(deact),
                                         "stake": str(int(sol * LAMPORTS))}},
            }}},
        },
    }


class OffsetTests(unittest.TestCase):
    def test_the_withdrawer_offset_is_the_one_used(self):
        """The staker can be delegated to a staking service; the withdrawer
        controls the capital. Keying on the staker would attribute a
        custodian's stake to the wrong wallet."""
        self.assertEqual(OFFSET_STAKER, 12)
        self.assertEqual(OFFSET_WITHDRAWER, 44)
        self.assertNotEqual(OFFSET_STAKER, OFFSET_WITHDRAWER)

    def test_the_query_filters_on_the_withdrawer(self):
        captured = {}

        def fake_rpc(method, params):
            if method == "getProgramAccounts":
                captured["filters"] = params[1]["filters"]
                return []
            return {"epoch": 500}

        with patch("lib.helius_client.rpc", side_effect=fake_rpc):
            stake_accounts_for(WALLET)
        memcmp = [f for f in captured["filters"] if "memcmp" in f][0]
        self.assertEqual(memcmp["memcmp"]["offset"], OFFSET_WITHDRAWER)
        self.assertEqual(memcmp["memcmp"]["bytes"], WALLET)


class StateClassificationTests(unittest.TestCase):
    def _run(self, accounts, epoch=500):
        def fake_rpc(method, params):
            return accounts if method == "getProgramAccounts" else {"epoch": epoch}
        with patch("lib.helius_client.rpc", side_effect=fake_rpc):
            return stake_accounts_for(WALLET, current_epoch=epoch)

    def test_u64_max_deactivation_means_not_deactivating(self):
        r = self._run([acct(500)])
        self.assertEqual(r["accounts"][0]["state"], "active")
        self.assertIsNone(r["accounts"][0]["deactivation_epoch"])
        self.assertEqual(r["active_sol"], 500)

    def test_a_future_deactivation_epoch_is_deactivating(self):
        r = self._run([acct(800, deact=505)], epoch=500)
        self.assertEqual(r["accounts"][0]["state"], "deactivating")
        self.assertEqual(r["deactivating_sol"], 800)

    def test_a_past_deactivation_epoch_is_already_inactive(self):
        r = self._run([acct(300, deact=480)], epoch=500)
        self.assertEqual(r["accounts"][0]["state"], "inactive")
        self.assertEqual(r["inactive_sol"], 300)

    def test_totals_split_across_states(self):
        r = self._run([acct(1000), acct(400, deact=505), acct(100, deact=490)],
                      epoch=500)
        self.assertEqual(r["total_sol"], 1500)
        self.assertEqual(r["active_sol"], 1000)
        self.assertEqual(r["deactivating_sol"], 400)
        self.assertEqual(r["inactive_sol"], 100)

    def test_an_rpc_failure_is_reported_not_swallowed(self):
        with patch("lib.helius_client.rpc", side_effect=RuntimeError("boom")):
            r = stake_accounts_for(WALLET)
        self.assertIsNotNone(r["error"])
        self.assertEqual(r["total_sol"], 0.0)


class LiquiditySignalTests(unittest.TestCase):
    def test_unstaking_is_capital_freed_not_a_sell_signal(self):
        """THE interpretation requirement."""
        sig = capital_liquidity_signal(
            {"total_sol": 10_000, "active_sol": 4_000,
             "deactivating_sol": 6_000, "inactive_sol": 0}, sol_price_usd=75)
        self.assertEqual(sig["state"], "CAPITAL_LIQUIDITY_INCREASING")
        self.assertEqual(sig["freed_sol"], 6_000)
        self.assertEqual(sig["freed_usd"], 450_000)
        self.assertNotIn("bear", sig["state"].lower())
        self.assertIn("NOT a sale", sig["note"])

    def test_a_trivial_unlock_is_not_a_signal(self):
        sig = capital_liquidity_signal(
            {"total_sol": 500, "active_sol": 499.9,
             "deactivating_sol": 0.1, "inactive_sol": 0})
        self.assertEqual(sig["state"], "stable")
        self.assertIn("materiality floor", sig["reason"])

    def test_a_failed_lookup_is_unknown_not_none(self):
        sig = capital_liquidity_signal({"error": "Timeout", "total_sol": 0})
        self.assertEqual(sig["state"], "unknown")

    def test_a_wallet_with_no_stake_says_so(self):
        sig = capital_liquidity_signal({"total_sol": 0})
        self.assertEqual(sig["state"], "none")

    def test_share_of_stake_is_reported(self):
        sig = capital_liquidity_signal(
            {"total_sol": 1_000, "deactivating_sol": 250, "inactive_sol": 0})
        self.assertAlmostEqual(sig["share_of_stake"], 0.25, places=3)
        self.assertEqual(sig["still_staked_sol"], 750)


if __name__ == "__main__":
    unittest.main()
