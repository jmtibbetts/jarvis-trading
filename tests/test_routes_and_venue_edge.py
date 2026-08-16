"""P17 + P20 + P21 — routes, venue verdicts, and edge decomposition.

Three defects a single-pool, single-venue, single-number model creates:

ROUTES. Pricing a multi-hop trade as one pool understates cost (every hop
charges its own fee and takes its own impact) and overstates availability
(a route is only as deep as its thinnest hop). Impact COMPOUNDS — three 5%
hops are 14.26%, not 15% — and the error grows with hop count, so the model
degrades exactly as routes get more exotic.

VENUE. A thesis with real gross edge can be untradeable on one venue and
comfortably tradeable on another. Reporting one blended verdict retires
strategies whose actual fault was routing.

EDGE. "The portfolio went up" cannot tell you whether the AI's sizing
helped and its entry review did not. Only decomposition can.
"""
import unittest

from lib.dex_routes import (Hop, best_route, quote_route, route_confidence)
from lib.venue_expectancy import (TRADEABLE, UNKNOWN, UNTRADEABLE,
                                  compare_venues, decompose_edge,
                                  venue_verdict)


def hop(rin=100_000.0, rout=200_000.0, fee=25, conf="VERIFIED", venue="raydium"):
    return Hop(venue=venue, pool=f"pool-{venue}", input_mint="A",
               output_mint="B", fee_bps=fee, depth_confidence=conf,
               reserve_in=rin, reserve_out=rout)


class RouteTests(unittest.TestCase):
    def test_each_hop_feeds_the_next(self):
        r = quote_route([hop(), hop()], 1_000)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.hops), 2)
        self.assertAlmostEqual(r.hops[1].input_amount, r.hops[0].output_amount)

    def test_impact_compounds_rather_than_adding(self):
        """Three 5% hops are 14.26%, not 15%. The error grows with hops."""
        thin = hop(rin=10_000.0, rout=20_000.0)
        r = quote_route([thin, hop(rin=10_000.0, rout=20_000.0),
                         hop(rin=10_000.0, rout=20_000.0)], 500, )
        summed = sum(h.impact_pct for h in r.hops)
        self.assertLess(r.aggregate_price_impact_pct, summed)

    def test_more_hops_cost_more(self):
        one = quote_route([hop()], 1_000)
        three = quote_route([hop(), hop(), hop()], 1_000)
        self.assertGreater(three.aggregate_price_impact_pct,
                           one.aggregate_price_impact_pct)

    def test_the_thinnest_hop_is_recorded(self):
        r = quote_route([hop(rin=2_000_000.0, rout=2_000_000.0),
                         hop(rin=30_000.0, rout=60_000.0)], 1_000)
        self.assertAlmostEqual(r.thinnest_hop_usd, 30_000.0)

    def test_a_thin_middle_hop_degrades_everything_after_it(self):
        fat = quote_route([hop(rin=1_000_000.0, rout=2_000_000.0),
                           hop(rin=1_000_000.0, rout=2_000_000.0)], 10_000)
        thin = quote_route([hop(rin=1_000_000.0, rout=2_000_000.0),
                            hop(rin=20_000.0, rout=40_000.0)], 10_000)
        self.assertLess(thin.expected_output_amount, fat.expected_output_amount)

    def test_minimum_output_reflects_slippage_tolerance(self):
        r = quote_route([hop()], 1_000, slippage_tolerance_pct=2.0)
        self.assertAlmostEqual(r.minimum_output_amount,
                               r.expected_output_amount * 0.98)

    def test_a_hop_without_reserves_refuses_the_whole_route(self):
        bad = hop()
        bad.reserve_in = None
        self.assertFalse(quote_route([hop(), bad], 1_000).ok)

    def test_failure_risk_grows_with_hop_count(self):
        one = quote_route([hop()], 1_000)
        four = quote_route([hop()] * 4, 1_000)
        self.assertGreater(four.failure_risk_pct, one.failure_risk_pct)

    def test_gas_is_not_taken_out_of_route_output(self):
        a = quote_route([hop()], 1_000, priority_lamports=0)
        b = quote_route([hop()], 1_000, priority_lamports=5_000_000)
        self.assertAlmostEqual(a.expected_output_amount, b.expected_output_amount)
        self.assertGreater(b.network_fee_sol, a.network_fee_sol)


class RouteConfidenceTests(unittest.TestCase):
    def test_the_weakest_hop_governs(self):
        """A VERIFIED first hop must not launder a MODELLED second one."""
        self.assertEqual(
            route_confidence([hop(conf="VERIFIED"),
                              hop(conf="MODELLED_ESTIMATE")]),
            "MODELLED_ESTIMATE")

    def test_an_all_verified_route_stays_verified(self):
        self.assertEqual(route_confidence([hop(), hop()]), "VERIFIED")

    def test_an_empty_route_is_not_optimistic(self):
        self.assertEqual(route_confidence([]), "MODELLED_ESTIMATE")


class BestRouteTests(unittest.TestCase):
    def test_more_output_wins_between_equal_certainties(self):
        a = quote_route([hop(rin=1_000_000.0, rout=2_000_000.0)], 1_000)
        b = quote_route([hop(rin=50_000.0, rout=100_000.0)], 1_000)
        self.assertIs(best_route([a, b]), a)

    def test_an_unmeasured_route_must_beat_a_measured_one_by_more(self):
        """Otherwise the router systematically prefers whichever pool it
        knows least about — selection bias toward being wrong."""
        measured = quote_route([hop(rin=100_000.0, rout=200_000.0,
                                    conf="VERIFIED")], 20_000)
        modelled = quote_route([hop(rin=103_000.0, rout=206_000.0,
                                    conf="MODELLED_ESTIMATE")], 20_000)
        self.assertGreater(modelled.expected_output_amount,
                           measured.expected_output_amount)
        self.assertIs(best_route([measured, modelled]), measured)

    def test_no_usable_route_returns_none(self):
        self.assertIsNone(best_route([]))


class VenueVerdictTests(unittest.TestCase):
    def _rows(self, venue, gross, cost, n=10, **kw):
        return [{"venue_type": venue, "gross_r": gross, "cost_r": cost,
                 "strategy": kw.get("strategy", "breakout"),
                 "asset_class": kw.get("asset_class", "CRYPTO"),
                 "product": kw.get("product", "CRYPTO_PERP"),
                 "timeframe": kw.get("timeframe", "15m")} for _ in range(n)]

    def test_a_cheap_venue_is_tradeable(self):
        v = venue_verdict(self._rows("CEX", 0.40, 0.08), venue_type="CEX",
                          strategy="breakout", asset_class="CRYPTO",
                          timeframe="15m")
        self.assertEqual(v.verdict, TRADEABLE)
        self.assertAlmostEqual(v.net_r, 0.32, places=3)

    def test_the_same_edge_is_untradeable_on_an_expensive_venue(self):
        v = venue_verdict(self._rows("DEX", 0.40, 0.55), venue_type="DEX",
                          strategy="breakout", asset_class="CRYPTO",
                          timeframe="15m")
        self.assertEqual(v.verdict, UNTRADEABLE)
        self.assertLess(v.net_r, 0)

    def test_a_thin_bucket_is_unknown_not_borrowed(self):
        v = venue_verdict(self._rows("DEX", 0.4, 0.1, n=2), venue_type="DEX",
                          strategy="breakout", asset_class="CRYPTO",
                          timeframe="15m")
        self.assertEqual(v.verdict, UNKNOWN)
        self.assertIn("incompatible", v.reason)

    def test_it_widens_one_step_rather_than_leaping(self):
        """Thin at strategy+product+timeframe, adequate one level out."""
        rows = self._rows("CEX", 0.4, 0.05, n=12, timeframe="5m")
        v = venue_verdict(rows, venue_type="CEX", strategy="breakout",
                          asset_class="CRYPTO", timeframe="4H")
        self.assertIn(v.level_used, (None, "strategy+class", "class"))
        if v.verdict != UNKNOWN:
            self.assertNotEqual(v.level_used, "strategy+product+timeframe")

    def test_edge_cost_ratio_exposes_a_venue_eating_the_trade(self):
        good = venue_verdict(self._rows("CEX", 0.40, 0.08), venue_type="CEX",
                             strategy="breakout", asset_class="CRYPTO",
                             timeframe="15m")
        bad = venue_verdict(self._rows("DEX", 0.40, 0.38), venue_type="DEX",
                            strategy="breakout", asset_class="CRYPTO",
                            timeframe="15m")
        self.assertGreater(good.edge_cost_ratio, bad.edge_cost_ratio)


class VenueComparisonTests(unittest.TestCase):
    def test_bad_venue_is_distinguished_from_no_edge(self):
        """THE distinction. One conclusion moves the trade; the other
        retires the setup."""
        rows = ([{"venue_type": "CEX", "gross_r": 0.40, "cost_r": 0.08,
                  "strategy": "s", "asset_class": "CRYPTO", "timeframe": "15m"}
                 for _ in range(10)]
                + [{"venue_type": "DEX", "gross_r": 0.40, "cost_r": 0.55,
                    "strategy": "s", "asset_class": "CRYPTO", "timeframe": "15m"}
                   for _ in range(10)])
        out = compare_venues(rows, strategy="s", asset_class="CRYPTO",
                             timeframe="15m")
        self.assertEqual(out["lesson"], "BAD_VENUE")
        self.assertEqual(out["best_venue"], "CEX")
        self.assertIn("routing", out["detail"])

    def test_no_edge_anywhere_is_reported_as_such(self):
        rows = [{"venue_type": v, "gross_r": 0.05, "cost_r": 0.40,
                 "strategy": "s", "asset_class": "CRYPTO", "timeframe": "15m"}
                for v in ("CEX", "DEX") for _ in range(10)]
        out = compare_venues(rows, strategy="s", asset_class="CRYPTO",
                             timeframe="15m")
        self.assertEqual(out["lesson"], "NO_EDGE_ANYWHERE")


class EdgeDecompositionTests(unittest.TestCase):
    def test_a_positive_agent_delta_is_reported_as_value_added(self):
        paired = [{"thesis_id": f"t{i}", "delta_net_r": 0.6}
                  for i in range(20)]
        d = decompose_edge(paired)
        self.assertEqual(d["verdict"], "AGENT_ADDS_VALUE")
        self.assertAlmostEqual(d["total_delta_r"]["mean"], 0.6)

    def test_a_negative_delta_is_reported_honestly(self):
        """The finding must be expressible in both directions."""
        paired = [{"thesis_id": f"t{i}", "delta_net_r": -0.4}
                  for i in range(20)]
        self.assertEqual(decompose_edge(paired)["verdict"],
                         "AGENT_SUBTRACTS_VALUE")

    def test_consistency_is_reported_beside_the_mean(self):
        """One huge win among many small losses is not skill."""
        paired = [{"thesis_id": f"t{i}", "delta_net_r": -0.1}
                  for i in range(19)]
        paired.append({"thesis_id": "big", "delta_net_r": 10.0})
        d = decompose_edge(paired)
        self.assertGreater(d["total_delta_r"]["mean"], 0)
        self.assertLess(d["total_delta_r"]["positive_pct"], 10.0)

    def test_market_samples_counts_theses(self):
        paired = [{"thesis_id": "same", "delta_net_r": 0.2} for _ in range(5)]
        self.assertEqual(decompose_edge(paired)["market_samples"], 1)

    def test_an_empty_set_says_so(self):
        self.assertEqual(decompose_edge([])["n"], 0)


if __name__ == "__main__":
    unittest.main()
