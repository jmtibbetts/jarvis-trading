"""Price-shock modelling over verified Kamino positions.

Answers what a health factor cannot: not "is this safe now" but "how far
must the market move before forced selling starts, and how large is it".

Everything here is MODELLED and labelled so. The positions are verified and
the health rule is Kamino's own; the shock ladder is a scenario, and the
distinction has to survive into the output or a projection gets read as a
measurement.
"""
import unittest

from lib.liquidation_stress import (DEFAULT_SHOCKS, SOL_FAMILY,
                                    aggregate_by_asset, family_of,
                                    forced_sale_impact, stress_ladder)


def pos(oid, collateral, debt, threshold, sym="SOL"):
    return {"obligation": oid, "collateral_value_usd": collateral,
            "debt_value_usd": debt, "liquidation_threshold_usd": threshold,
            "health_factor": threshold / debt if debt else None,
            "assets": {"deposits": [{"symbol": sym, "value_usd": collateral}],
                       "borrows": [{"symbol": "USDC", "value_usd": debt}]}}


class FamilyTests(unittest.TestCase):
    def test_every_sol_derivative_shares_a_family(self):
        """A broad SOL decline hits SOL, JitoSOL, mSOL and bSOL together.
        Shocking one while holding the others flat would understate risk
        for exactly the wallets most exposed."""
        for s in ("SOL", "JitoSOL", "mSOL", "bSOL"):
            self.assertEqual(family_of(s), "SOL_FAMILY", s)

    def test_stables_are_their_own_family(self):
        self.assertEqual(family_of("USDC"), "STABLE")
        self.assertEqual(family_of("USDT"), "STABLE")

    def test_an_unknown_symbol_is_its_own_family_not_lumped_in(self):
        self.assertEqual(family_of("BONK"), "BONK")
        self.assertEqual(family_of(None), "unknown")


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.book = [
            pos("fragile", 1_000_000, 735_000, 750_000),        # health 1.020
            pos("mid", 2_000_000, 1_350_000, 1_500_000),        # health 1.111
            pos("large", 3_160_000, 2_490_000, 2_908_000),      # health 1.168
            pos("safe", 5_000_000, 1_000_000, 3_750_000),       # health 3.75
            pos("gone", 500_000, 400_000, 375_000),             # health 0.938
            pos("usdc", 1_000_000, 500_000, 900_000, sym="USDC"),
        ]

    def test_only_positions_holding_the_shocked_family_are_considered(self):
        l = stress_ladder(self.book, family="SOL_FAMILY")
        self.assertEqual(l["positions_considered"], 5)   # the USDC one is out

    def test_a_fragile_position_breaks_early_and_a_safe_one_never_does(self):
        l = stress_ladder(self.book, family="SOL_FAMILY")
        by_shock = {r["shock_pct"]: r for r in l["ladder"]}
        self.assertEqual(by_shock[2.0]["newly_liquidatable"], 1)
        self.assertEqual(by_shock[2.0]["newly_liquidatable_debt_usd"], 735_000)
        # 3.75 health needs a ~73% decline; nothing on this ladder reaches it.
        self.assertEqual(sum(r["newly_liquidatable"] for r in l["ladder"]), 3)

    def test_each_position_is_counted_once_at_the_shock_that_breaks_it(self):
        """Otherwise every deeper rung re-counts everything above it and
        the ladder reads as though exposure multiplies with depth."""
        l = stress_ladder(self.book, family="SOL_FAMILY")
        self.assertEqual(sum(r["newly_liquidatable"] for r in l["ladder"]), 3)

    def test_cumulative_exposure_never_decreases(self):
        l = stress_ladder(self.book, family="SOL_FAMILY")
        seen = [r["cumulative_liquidatable_debt_usd"] for r in l["ladder"]]
        self.assertEqual(seen, sorted(seen))

    def test_an_already_liquidatable_position_is_not_counted_as_newly(self):
        l = stress_ladder(self.book, family="SOL_FAMILY")
        self.assertEqual(l["already_liquidatable"], 1)
        self.assertGreater(l["ladder"][0]["cumulative_liquidatable_debt_usd"], 0)
        self.assertEqual(l["ladder"][0]["newly_liquidatable"], 0)

    def test_break_point_follows_the_health_factor(self):
        """A position with health H breaks at roughly (1 - 1/H)."""
        l = stress_ladder([pos("h", 1_000_000, 500_000, 1_000_000)],
                          family="SOL_FAMILY", shocks=(10.0, 40.0, 60.0))
        by = {r["shock_pct"]: r["newly_liquidatable"] for r in l["ladder"]}
        self.assertEqual(by[10.0], 0)      # health 2.0 -> needs ~50%
        self.assertEqual(by[60.0], 1)

    def test_the_output_declares_itself_a_model(self):
        l = stress_ladder(self.book, family="SOL_FAMILY")
        self.assertIn("MODELLED", l["basis"])
        self.assertIn("not a forecast", l["basis"].lower())

    def test_an_empty_book_produces_an_empty_ladder_not_an_error(self):
        l = stress_ladder([], family="SOL_FAMILY")
        self.assertEqual(l["positions_considered"], 0)
        self.assertEqual(len(l["ladder"]), len(DEFAULT_SHOCKS))


class ForcedSaleTests(unittest.TestCase):
    def test_impact_is_priced_against_real_depth(self):
        """A liquidation walks the curve like any other order; it does not
        fill at mid."""
        r = forced_sale_impact(50_000, 500_000, dex="raydium")
        self.assertTrue(r["available"])
        self.assertGreater(r["price_impact_pct"], 10)
        self.assertLess(r["proceeds_usd"], 50_000)

    def test_unknown_depth_returns_unavailable_rather_than_a_guess(self):
        for depth in (None, 0, -1):
            r = forced_sale_impact(50_000, depth)
            self.assertFalse(r["available"])
            self.assertIn("UNAVAILABLE", r["reason"])

    def test_a_deeper_pool_absorbs_the_same_sale_better(self):
        thin = forced_sale_impact(50_000, 500_000)["price_impact_pct"]
        deep = forced_sale_impact(50_000, 50_000_000)["price_impact_pct"]
        self.assertGreater(thin, deep * 5)


class AggregationTests(unittest.TestCase):
    def test_assets_and_families_are_both_reported(self):
        """Merging hides an LST depeg; separating hides a broad decline."""
        agg = aggregate_by_asset([
            pos("a", 100, 50, 75, sym="bSOL"),
            pos("b", 200, 100, 150, sym="SOL"),
        ])
        self.assertIn("bSOL", agg["by_asset"])
        self.assertIn("SOL", agg["by_asset"])
        self.assertAlmostEqual(agg["by_family"]["SOL_FAMILY"]["collateral_usd"], 300)

    def test_an_unresolved_asset_is_counted_not_dropped(self):
        p = pos("x", 100, 50, 75)
        p["assets"]["deposits"][0]["symbol"] = None
        agg = aggregate_by_asset([p])
        self.assertEqual(agg["by_asset"]["UNRESOLVED"]["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
