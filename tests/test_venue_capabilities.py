"""UI availability is not API availability.

Kraken Pro offers 11,000+ US stocks and ETFs to eligible clients. Kraken's
public API Center documents Spot REST/WS, Futures REST/WS and FIX — and no
stock trading contract. Those are different claims, and the gap between
them is exactly what a system assumes its way across:

    "the UI can do it" -> "the account can do it" -> "the API can do it"

Each arrow is an inference and the last one is false today. A desk that
made it would build an equity execution path against an endpoint that does
not exist, discover that at the worst possible moment, and until then
train on economics nothing can reproduce.

ENTITLEMENT IS ALSO NOT CAPABILITY. Kraken separates Level I from Level II
futures data by subscription, so "we can trade futures" does not imply "we
can see full depth". A fill model assuming depth it is not entitled to is
guessing — and guessing in the flattering direction, since assumed depth
is always sufficient.
"""
import unittest

from lib.product_router import (BELOW_MIN_SIZE, NO_CAPABILITY,
                                price_expression, route)
from lib.venue_capabilities import (ASSUMED, DISCOVERED, DOCUMENTED,
                                    ENTITLEMENT_UNKNOWN, LEVEL_2, UI_ONLY,
                                    UNSUPPORTED, VenueCapabilityError,
                                    assert_executable, capability,
                                    depth_available, executable_products,
                                    record_discovery, research_only_products,
                                    snapshot)


class UiIsNotApiTests(unittest.TestCase):
    """THE distinction this module exists for."""

    def test_kraken_equity_is_ui_only_and_not_executable(self):
        c = capability("kraken", "EQUITY_SPOT")
        self.assertEqual(c.status, UI_ONLY)
        self.assertFalse(c.executable)
        self.assertIn("not API availability", c.reason)

    def test_kraken_etfs_are_ui_only_too(self):
        self.assertFalse(capability("kraken", "ETF_SPOT").executable)

    def test_ui_only_is_research_not_unsupported(self):
        """The honest middle ground: observable, not executable. JARVIS may
        still form a thesis and measure what would have happened."""
        c = capability("kraken", "EQUITY_SPOT")
        self.assertTrue(c.research_only)
        self.assertNotEqual(c.status, UNSUPPORTED)

    def test_claiming_execution_on_it_raises(self):
        with self.assertRaises(VenueCapabilityError):
            assert_executable("kraken", "EQUITY_SPOT")

    def test_documented_products_are_executable(self):
        for p in ("CRYPTO_SPOT", "CRYPTO_PERP", "COMMODITY_FUTURE",
                  "INDEX_FUTURE"):
            self.assertTrue(capability("kraken", p).executable, p)

    def test_the_two_lists_do_not_overlap(self):
        ex = set(executable_products("kraken"))
        ro = set(research_only_products("kraken"))
        self.assertTrue(ex)
        self.assertTrue(ro)
        self.assertEqual(ex & ro, set())


class UnknownIsNotPermissiveTests(unittest.TestCase):
    def test_an_uncharacterised_pair_is_assumed_not_allowed(self):
        """A venue nobody characterised is not a venue anybody verified."""
        c = capability("some_new_exchange", "CRYPTO_SPOT")
        self.assertEqual(c.status, ASSUMED)
        self.assertFalse(c.executable)

    def test_an_unknown_product_on_a_known_venue_is_also_refused(self):
        self.assertFalse(capability("kraken", "OPTIONS").executable)


class DiscoveryPromotesAndDemotesTests(unittest.TestCase):
    def test_a_successful_probe_promotes_to_discovered(self):
        """UI_ONLY becomes executable by a probe SUCCEEDING, never by
        anybody deciding it probably works."""
        before = capability("kraken", "EQUITY_SPOT").executable
        try:
            c = record_discovery("kraken", "EQUITY_SPOT", works=True,
                                 api_surface="REST", checked_at="now")
            self.assertEqual(c.status, DISCOVERED)
            self.assertTrue(c.executable)
        finally:
            record_discovery("kraken", "EQUITY_SPOT", works=False,
                             reason="restored to UI_ONLY for other tests")
            capability("kraken", "EQUITY_SPOT").status = UI_ONLY
            capability("kraken", "EQUITY_SPOT").reason = (
                "no stock trading contract published in the Kraken API "
                "Center — UI availability is not API availability")
        self.assertFalse(before)

    def test_a_failed_probe_records_unsupported(self):
        c = record_discovery("testvenue", "CRYPTO_SPOT", works=False)
        self.assertEqual(c.status, UNSUPPORTED)
        self.assertFalse(c.executable)
        self.assertFalse(c.research_only)


class EntitlementIsNotCapabilityTests(unittest.TestCase):
    def test_spot_has_confirmed_depth(self):
        d = depth_available("kraken", "CRYPTO_SPOT")
        self.assertEqual(d["entitlement"], LEVEL_2)
        self.assertTrue(d["book_depth_usable"])
        self.assertEqual(d["fill_model"], "ORDERBOOK_SIMULATED")

    def test_futures_depth_is_unknown_until_queried(self):
        """Trading approval does not imply Level II."""
        d = depth_available("kraken", "CRYPTO_PERP")
        self.assertEqual(d["entitlement"], ENTITLEMENT_UNKNOWN)
        self.assertFalse(d["book_depth_usable"])

    def test_unknown_entitlement_falls_to_the_conservative_fill_model(self):
        """Assumed depth is always sufficient, which is why assuming it
        flatters every fill."""
        self.assertEqual(depth_available("kraken", "CRYPTO_PERP")["fill_model"],
                         "CONSERVATIVE_BAR_TOUCH")

    def test_a_dex_pool_has_no_book_at_all(self):
        self.assertFalse(depth_available("dex", "DEX_SPOT")["book_depth_usable"])


class ProductRouterTests(unittest.TestCase):
    """One thesis, several expressions, ranked on economics."""

    def _route(self, **kw):
        base = dict(symbol="BTC/USD", side="Long", entry=100_000.0,
                    stop=97_000.0, risk_budget_usd=1_000.0, gross_r=0.45,
                    candidates=[("kraken", "CRYPTO_SPOT"),
                                ("kraken", "CRYPTO_PERP"),
                                ("kraken", "EQUITY_SPOT")])
        base.update(kw)
        return route(**base)

    def test_capability_is_checked_before_economics(self):
        """Pricing a trade the venue cannot place produces a number that
        looks like a comparison and is not one."""
        r = self._route()
        eq = next(e for e in r["expressions"]
                  if e["product"] == "EQUITY_SPOT")
        self.assertFalse(eq["eligible"])
        self.assertEqual(eq["reason"], NO_CAPABILITY)
        self.assertIsNone(eq["cost_r"], "an unroutable product was priced")

    def test_a_documented_product_is_priced(self):
        r = self._route()
        spot = next(e for e in r["expressions"]
                    if e["product"] == "CRYPTO_SPOT")
        self.assertTrue(spot["eligible"])
        self.assertIsNotNone(spot["cost_r"])

    def test_refusals_are_reported_not_hidden(self):
        """"This thesis is only expressible as spot" is a finding."""
        r = self._route()
        self.assertIn(NO_CAPABILITY, r["refused"])

    def test_the_best_expression_is_chosen_on_net(self):
        r = self._route()
        self.assertIsNotNone(r["best"])
        self.assertIn("net", r["best_reason"])

    def test_provenance_travels_with_every_expression(self):
        """Generic simulated crypto and Kraken-realistic simulated crypto
        must never pool."""
        r = self._route(market_data_source="alpaca_sip")
        spot = next(e for e in r["expressions"]
                    if e["product"] == "CRYPTO_SPOT")
        self.assertEqual(spot["capability_status"], DOCUMENTED)
        self.assertEqual(spot["market_data_source"], "alpaca_sip")
        self.assertTrue(spot["execution_model_version"])
        self.assertTrue(spot["router_version"])

    def test_contracts_are_whole_and_a_short_budget_refuses(self):
        e = price_expression(venue="kraken", product="INDEX_FUTURE",
                             symbol="MES=F", side="Long", entry=5_000.0,
                             stop=4_990.0, risk_budget_usd=10.0)
        self.assertFalse(e.eligible)
        self.assertEqual(e.reason, BELOW_MIN_SIZE)

    def test_a_sufficient_budget_sizes_whole_contracts(self):
        e = price_expression(venue="kraken", product="INDEX_FUTURE",
                             symbol="MES=F", side="Long", entry=5_000.0,
                             stop=4_990.0, risk_budget_usd=1_000.0)
        self.assertTrue(e.eligible, e.detail)
        self.assertEqual(e.quantity, float(int(e.quantity)))
        self.assertEqual(e.quantity_unit, "CONTRACTS")
        self.assertEqual(e.multiplier, 5)

    def test_notional_carries_the_multiplier(self):
        e = price_expression(venue="kraken", product="INDEX_FUTURE",
                             symbol="MES=F", side="Long", entry=5_000.0,
                             stop=4_990.0, risk_budget_usd=1_000.0)
        self.assertAlmostEqual(e.notional_usd, e.quantity * 5_000.0 * 5)

    def test_nothing_executable_says_so_rather_than_picking_one(self):
        r = route(symbol="NVDA", side="Long", entry=100.0, stop=98.0,
                  risk_budget_usd=1_000.0,
                  candidates=[("kraken", "EQUITY_SPOT")])
        self.assertIsNone(r["best"])
        self.assertIn("no expression", r["best_reason"])


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_states_the_rule(self):
        s = snapshot()
        self.assertIn("kraken", s["venues"])
        self.assertIn("UI availability is not API availability", s["note"])


if __name__ == "__main__":
    unittest.main()
