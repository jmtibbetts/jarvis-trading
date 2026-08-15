"""The two routes that surface Wallet Alpha and the Helius client.

These were added with no tests at all — the route bodies were smoke-tested
by hand against the live API and pushed. That is exactly the gap this file
closes, because the parts most worth pinning are the ones a live smoke test
cannot show you: what the endpoint does when the provider is MISSING, when
it is BROKEN, and whether the §116/§117 invariants survive the trip through
the route rather than only inside lib/wallet_intel.

Every external call is stubbed. No network, no key required.
"""
import unittest
from unittest import mock

from app.routers import intel as intel_routes


# One page of transfers in the shape lib/wallet_activity.parse_transfers
# accepts — two wallets moving the same mint, close together in time.
def _transfers(address: str, counterparty: str, ts: int) -> dict:
    return {"data": [{
        "signature": f"sig-{address[:4]}-{ts}",
        "timestamp": ts,
        "direction": "out",
        "counterparty": counterparty,
        "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        "symbol": None,
        "amount": 250_000.0,
    }]}


class HeliusHealthRouteTests(unittest.TestCase):
    def test_unconfigured_reports_itself_and_never_calls_out(self):
        """No key is a CONFIGURATION state, not an error and not empty.

        The route must also not attempt a health check without one — that
        would be a guaranteed failed request on every Ops page load.
        """
        with mock.patch.object(intel_routes, "__name__", intel_routes.__name__):
            with mock.patch("lib.helius_client.configured", return_value=False), \
                 mock.patch("lib.helius_client.metrics", return_value={}), \
                 mock.patch("lib.helius_client.health") as health:
                out = intel_routes.helius_health()
        self.assertFalse(out["configured"])
        self.assertIn("detail", out)
        health.assert_not_called()

    def test_configured_returns_health_and_metrics(self):
        with mock.patch("lib.helius_client.configured", return_value=True), \
             mock.patch("lib.helius_client.metrics",
                        return_value={"wallet/transfers": {"calls": 3, "errors": 1}}), \
             mock.patch("lib.helius_client.health",
                        return_value={"configured": True, "rpc": {"ok": True, "ms": 12}}):
            out = intel_routes.helius_health()
        self.assertTrue(out["configured"])
        self.assertEqual(out["metrics"]["wallet/transfers"]["calls"], 3)
        self.assertTrue(out["health"]["rpc"]["ok"])


class WalletIntelRouteTests(unittest.TestCase):
    def _run(self, wallets, **patches):
        cfg = ("https://api.helius.xyz", "key" if wallets else "", wallets, 100)
        defaults = {
            "lib.helius_client.batch_identity": {},
            "lib.helius_client.funded_by": {},
            "lib.token_pricing.resolve_prices": {},
        }
        defaults.update(patches)
        stack = [mock.patch("lib.wallet_activity._config", return_value=cfg)]
        for target, value in defaults.items():
            stack.append(mock.patch(target, **(value if isinstance(value, dict)
                                               and "side_effect" in value
                                               else {"return_value": value})))
        for p in stack:
            p.start()
        try:
            return intel_routes.wallet_intel_report(limit=50)
        finally:
            for p in reversed(stack):
                p.stop()

    def test_no_key_is_not_configured_not_empty(self):
        """The distinction the whole P0 sweep exists to preserve: a missing
        provider must never render as 'nothing is happening on-chain'."""
        out = self._run([])
        self.assertFalse(out["configured"])
        self.assertIn("HELIUS_API_KEY", out["detail"])
        # Even the refusal carries the population stamp.
        self.assertEqual(out["research_population"], "WALLET_ALPHA")

    def test_empty_watchlist_says_so_specifically(self):
        with mock.patch("lib.wallet_activity._config",
                        return_value=("https://x", "key", [], 100)):
            out = intel_routes.wallet_intel_report()
        self.assertFalse(out["configured"])
        self.assertIn("HELIUS_WATCH_WALLETS", out["detail"])

    def test_transfer_failure_is_reported_not_swallowed(self):
        """A wallet whose transfers 500 must appear in `errors`. Returning
        a clean empty report would be the silent failure in a new place."""
        out = self._run(
            ["Wa11etAAA", "Wa11etBBB"],
            **{"lib.helius_client.transfers": {"side_effect": RuntimeError("boom")}},
        )
        self.assertEqual(out["transfers"], 0)
        self.assertEqual(len(out["errors"]), 2)
        self.assertTrue(all("boom" in e for e in out["errors"]))

    def test_reports_both_wallet_and_cluster_counts(self):
        """§117 — a raw wallet count without the independent cluster count
        beside it is how a single actor manufactures its own consensus."""
        out = self._run(
            ["Wa11etAAA", "Wa11etBBB"],
            **{"lib.helius_client.transfers": {
                "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)}},
        )
        self.assertTrue(out["configured"])
        ind = out["independence"]
        self.assertIn("raw_wallets", ind)
        self.assertIn("independent_clusters", ind)
        self.assertEqual(ind["raw_wallets"], 2)

    def test_one_actor_on_two_addresses_collapses_to_one_cluster(self):
        """The §117 invariant with teeth: two addresses sharing a
        non-infrastructure funder are ONE opinion, and the response must say
        so rather than reporting two independent participants."""
        funder = {"funder": "SharedFunderZZZ", "funderType": "wallet",
                  "funderName": None}
        out = self._run(
            ["Wa11etAAA", "Wa11etBBB"],
            **{"lib.helius_client.transfers": {
                   "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)},
               "lib.helius_client.funded_by": funder},
        )
        ind = out["independence"]
        self.assertEqual(ind["raw_wallets"], 2)
        self.assertEqual(ind["independent_clusters"], 1)
        self.assertEqual(ind["collapsed"], 1)

    def test_unpriced_transfers_produce_no_whales(self):
        """Not a bug — the point. With no USD value and too short a history
        for a per-wallet baseline, `whale_score` has nothing to judge size
        against and must decline to call anything a whale. This test exists
        because the first version of the stamp test below assumed a whale
        would appear here, and the code was right and the test was wrong.
        """
        out = self._run(
            ["Wa11etAAA"],
            **{"lib.helius_client.transfers": {
                "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)}},
        )
        self.assertEqual(out["transfers"], 1)
        self.assertEqual(out["whales"], [])
        self.assertEqual(out["pricing"]["unpriced"], 1)

    def test_every_record_carries_the_research_population_stamp(self):
        """§116 — UI connection is not model contamination, but only because
        the stamp makes a leak into a majors population detectable."""
        from lib.wallet_intel import assert_not_majors_population

        # USDT is a pegged mint, so this transfer prices at $250k and clears
        # the absolute whale floor — giving us a stamped record to test.
        usdt = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        out = self._run(
            ["Wa11etAAA"],
            **{"lib.helius_client.transfers": {
                   "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)},
               "lib.token_pricing.resolve_prices": {
                   usdt: {"price": 1.0, "source": "peg", "confidence": 1.0}}},
        )
        self.assertEqual(out["research_population"], "WALLET_ALPHA")
        self.assertTrue(out["whales"], "expected a priced $250k transfer to score")
        for whale in out["whales"]:
            self.assertEqual(whale["research_population"], "WALLET_ALPHA")
        # The boundary guard must actually fire on this payload.
        with self.assertRaises(ValueError):
            assert_not_majors_population(out["whales"], where="test")

    def test_wallet_count_is_bounded_and_truncation_is_declared(self):
        """Silently querying only the first N wallets would make the report
        look complete while ignoring most of the watchlist."""
        many = [f"Wa11et{i:03d}" for i in range(30)]
        out = self._run(
            many,
            **{"lib.helius_client.transfers": {
                "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)}},
        )
        self.assertEqual(out["wallets_watched"], 30)
        self.assertLessEqual(out["wallets_queried"], 12)
        self.assertEqual(out["wallets_truncated"],
                         30 - out["wallets_queried"])
        self.assertGreater(out["wallets_truncated"], 0)

    def test_pricing_failure_degrades_without_taking_the_report_down(self):
        out = self._run(
            ["Wa11etAAA"],
            **{"lib.helius_client.transfers": {
                   "side_effect": lambda a, l: _transfers(a, "CounterpartyX", 1_700_000_000)},
               "lib.token_pricing.resolve_prices": {"side_effect": RuntimeError("price feed down")}},
        )
        self.assertTrue(out["configured"])
        self.assertEqual(out["transfers"], 1)
        self.assertTrue(any("price feed down" in e for e in out["errors"]))


if __name__ == "__main__":
    unittest.main()
