"""Ranking the whole Kamino book by what actually matters.

Size is the wrong sort on its own. Measured live:

    E3NiM5n6…  $89.4M collateral, health 1.409, 29% away  -> not in the top 10
    HDG5LNix…  $46.2M collateral, health 1.022, 2.1% away -> ranks 5th

The largest position on the protocol contributes nothing to a cascade; a
smaller fragile one starts it. Significance is size AND proximity, weighted
by whether the collateral can actually move that far.

Fixtures are three real positions of deliberately different shape — very
large and safe, very large and critical, mid-sized and already liquidatable
— captured with rawBase64 so these run offline.
"""
import json
import pathlib
import unittest

from lib.kamino_sweep import (DEFAULT_BANDS, VOLATILITY_BY_FAMILY,
                              band_summary, rank_by_significance,
                              significance)

FIX = pathlib.Path(__file__).parent / "fixtures" / "kamino_large_positions.json"


def large_fixtures():
    return json.loads(FIX.read_text(encoding="utf-8"))


def pos(debt, distance, collateral=None):
    return {"obligation": f"o{debt}{distance}", "owner": "w",
            "debt_value_usd": debt,
            "collateral_value_usd": collateral if collateral is not None else debt * 1.5,
            "distance_to_liquidation_pct": distance}


class SignificanceTests(unittest.TestCase):
    def test_a_fragile_position_outranks_a_far_larger_safe_one(self):
        """THE ranking requirement, in the shape observed live."""
        huge_safe = pos(35_651_149, 29.0, collateral=89_442_836)
        smaller_fragile = pos(42_478_584, 2.14, collateral=46_177_896)
        a = significance(huge_safe, collateral_family="SOL_FAMILY")
        b = significance(smaller_fragile, collateral_family="SOL_FAMILY")
        self.assertGreater(b["significance_score"], a["significance_score"])

    def test_size_still_matters_at_equal_proximity(self):
        small = significance(pos(50_000, 3.0), collateral_family="SOL_FAMILY")
        large = significance(pos(5_000_000, 3.0), collateral_family="SOL_FAMILY")
        self.assertGreater(large["significance_score"], small["significance_score"])

    def test_proximity_dominates_within_a_size_band(self):
        near = significance(pos(1_000_000, 1.0), collateral_family="SOL_FAMILY")
        far = significance(pos(1_000_000, 40.0), collateral_family="SOL_FAMILY")
        self.assertGreater(near["significance_score"], far["significance_score"] * 3)

    def test_stable_collateral_is_discounted(self):
        """A stable-collateralised position near its threshold is far less
        likely to actually cross it."""
        volatile = significance(pos(1_000_000, 2.0), collateral_family="SOL_FAMILY")
        stable = significance(pos(1_000_000, 2.0), collateral_family="STABLE")
        self.assertGreater(volatile["significance_score"],
                           stable["significance_score"] * 2)
        self.assertLess(VOLATILITY_BY_FAMILY["STABLE"],
                        VOLATILITY_BY_FAMILY["SOL_FAMILY"])

    def test_an_unknown_wallet_is_neutral_not_penalised(self):
        anon = significance(pos(1_000_000, 2.0), collateral_family="SOL_FAMILY")
        known = significance(pos(1_000_000, 2.0), collateral_family="SOL_FAMILY",
                             wallet_score=90)
        self.assertGreater(known["significance_score"], anon["significance_score"])
        self.assertEqual(anon["components"]["wallet_multiplier"], 1.0)

    def test_dust_cannot_score_however_close_to_liquidation(self):
        self.assertLess(significance(pos(900, 0.0),
                                     collateral_family="SOL_FAMILY")["significance_score"],
                        5.0)

    def test_the_score_reports_its_components(self):
        s = significance(pos(1_000_000, 2.0), collateral_family="SOL_FAMILY")
        for k in ("size", "proximity", "collateral_volatility", "wallet_multiplier"):
            self.assertIn(k, s["components"])
        self.assertIn("ASSUMED", s["basis"])

    def test_ranking_orders_by_significance_not_size(self):
        book = [pos(50_000_000, 45.0), pos(2_000_000, 1.0), pos(500_000, 30.0)]
        ranked = rank_by_significance(book, limit=3,
                                      families={p["obligation"]: "SOL_FAMILY" for p in book})
        self.assertEqual(ranked[0]["debt_value_usd"], 2_000_000)


class BandTests(unittest.TestCase):
    def test_bands_are_cumulative_and_shrink(self):
        book = [pos(d, 10.0, collateral=c) for d, c in
                ((80_000, 150_000), (400_000, 800_000), (900_000, 2_000_000),
                 (4_000_000, 12_000_000))]
        bands = band_summary(book)
        counts = [b["positions"] for b in bands]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(bands[0]["min_collateral_usd"], DEFAULT_BANDS[0])

    def test_near_liquidation_debt_is_reported_separately(self):
        book = [pos(1_000_000, 2.0, collateral=3_000_000),
                pos(1_000_000, 40.0, collateral=3_000_000)]
        b = band_summary(book, bands=(100_000,))[0]
        self.assertEqual(b["positions"], 2)
        self.assertEqual(b["within_5pct_of_liquidation"], 1)
        self.assertEqual(b["debt_within_5pct_usd"], 1_000_000)


class LargeFixtureTests(unittest.TestCase):
    def test_fixtures_span_distinct_risk_shapes(self):
        fx = large_fixtures()
        self.assertGreaterEqual(len(fx), 3)
        states = {f["risk_state"] for f in fx}
        self.assertIn("SAFE", states)
        self.assertTrue({"CRITICAL", "LIQUIDATION_IN_PROGRESS"} & states)

    def test_at_least_one_fixture_is_a_multi_million_dollar_position(self):
        """Small fixtures prove the parser; only large ones exercise
        cascade and market-impact reasoning."""
        self.assertTrue(any(f["collateral_value_usd"] > 1_000_000
                            for f in large_fixtures()))

    def test_the_largest_position_is_not_the_most_at_risk(self):
        fx = sorted(large_fixtures(), key=lambda f: -f["collateral_value_usd"])
        biggest = fx[0]
        riskiest = min(large_fixtures(), key=lambda f: f["health_factor"])
        self.assertGreater(biggest["health_factor"], riskiest["health_factor"])
        self.assertNotEqual(biggest["obligation"], riskiest["obligation"])

    def test_every_fixture_leg_is_identified_even_when_unnamed(self):
        """A mint outside the local symbol registry still resolves — the
        mint IS the identity, so it shows as a short address rather than
        None, which would read as a decode failure."""
        for f in large_fixtures():
            for leg in f["deposits"] + f["borrows"]:
                self.assertIsNotNone(leg["symbol"], f["obligation"])
                self.assertIsNotNone(leg["asset"], "every leg must carry its mint")

    def test_collateral_exceeds_debt_on_every_fixture(self):
        for f in large_fixtures():
            self.assertGreater(f["collateral_value_usd"], f["debt_value_usd"] * 0.9)


if __name__ == "__main__":
    unittest.main()
