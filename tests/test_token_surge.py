"""Detect CHANGE, not SIZE.

The spec's own worked example, which is also the regression that matters:

    TOKEN A   $20M/day, and $20M/day is normal for it     -> LOW
    TOKEN B   $600k/day, but 5m volume $1,200 -> $85,000
              and 20 trades/hour -> 1,400                  -> EXTREME

B must rank far above A even though A is 30x larger.
"""
import unittest

from lib.token_surge import (MIN_SNAPSHOTS_FOR_BASELINE, baseline_from,
                             score_snapshot)


def snap(*, vol_m5, buys, sells, buyers, sellers, liq=500_000.0,
         price_chg_m5=0.0, price_chg_h1=0.0):
    return {
        "mint": "M" * 32, "symbol": "TEST/SOL",
        "liquidity_usd": liq, "volume_m5": vol_m5,
        "buys_m5": buys, "sells_m5": sells,
        "buyers_m5": buyers, "sellers_m5": sellers,
        "price_change_m5": price_chg_m5, "price_change_h1": price_chg_h1,
    }


def history_of(s, n=MIN_SNAPSHOTS_FOR_BASELINE + 2):
    return [dict(s) for _ in range(n)]


class TheCriticalRegressionTest(unittest.TestCase):
    def test_a_smaller_token_that_is_accelerating_beats_a_huge_flat_one(self):
        # TOKEN A — enormous and utterly steady.
        a_normal = snap(vol_m5=69_444, buys=400, sells=400,
                        buyers=300, sellers=300, liq=20_000_000)
        a = score_snapshot(a_normal, history_of(a_normal))

        # TOKEN B — small, but 5m volume 1,200 -> 85,000 and trades exploding.
        b_normal = snap(vol_m5=1_200, buys=10, sells=10, buyers=8, sellers=8,
                        liq=400_000)
        b_now = snap(vol_m5=85_000, buys=950, sells=210,
                     buyers=520, sellers=140, liq=460_000,
                     price_chg_m5=6.0, price_chg_h1=14.0)
        b = score_snapshot(b_now, history_of(b_normal))

        self.assertGreater(b["surge_score"], a["surge_score"] * 3,
                           f"B={b['surge_score']} must dominate A={a['surge_score']}")
        self.assertLess(a["surge_score"], 25, "a steady token is not surging")
        self.assertGreater(b["surge_score"], 60)
        self.assertEqual(b["bias"], "bullish")


class BaselineTests(unittest.TestCase):
    def test_too_few_snapshots_is_not_a_baseline(self):
        s = snap(vol_m5=5_000, buys=50, sells=50, buyers=40, sellers=40)
        self.assertIsNone(baseline_from([s] * (MIN_SNAPSHOTS_FOR_BASELINE - 1)))
        self.assertIsNotNone(baseline_from([s] * MIN_SNAPSHOTS_FOR_BASELINE))

    def test_the_baseline_is_a_median_so_one_spike_cannot_hide_the_next(self):
        quiet = snap(vol_m5=1_000, buys=10, sells=10, buyers=8, sellers=8)
        hist = history_of(quiet)
        hist[2]["volume_m5"] = 200_000          # an earlier spike
        base = baseline_from(hist)
        self.assertAlmostEqual(base["volume_m5"], 1_000, delta=1)

    def test_a_new_token_is_labelled_and_capped_not_penalised(self):
        """No history is not the same as no activity. Penalising a launch
        for being new misses every launch."""
        s = snap(vol_m5=190_000, buys=620, sells=90, buyers=311, sellers=60,
                 liq=180_000)
        r = score_snapshot(s, [])
        self.assertEqual(r["baseline_quality"], "new_token")
        self.assertGreater(r["surge_score"], 40)
        self.assertLessEqual(r["surge_score"], 85, "unproven, so capped")


class SignificanceGateTests(unittest.TestCase):
    def test_dust_liquidity_cannot_score_however_large_the_multiplier(self):
        """$50 -> $5,000 is 100x and means nothing."""
        quiet = snap(vol_m5=50, buys=2, sells=1, buyers=2, sellers=1, liq=4_000)
        now = snap(vol_m5=5_000, buys=40, sells=5, buyers=30, sellers=4, liq=4_000)
        r = score_snapshot(now, history_of(quiet))
        self.assertEqual(r["surge_score"], 0.0)
        self.assertIn("liquidity", r["reasons"][0])

    def test_a_liquid_token_with_real_acceleration_does_score(self):
        quiet = snap(vol_m5=2_000, buys=15, sells=15, buyers=12, sellers=12,
                     liq=800_000)
        now = snap(vol_m5=60_000, buys=700, sells=180, buyers=400, sellers=120,
                   liq=850_000, price_chg_h1=9.0)
        r = score_snapshot(now, history_of(quiet))
        self.assertGreater(r["surge_score"], 55)
        self.assertGreater(r["volume_accel"], 25)


class BiasTests(unittest.TestCase):
    def test_a_dump_is_a_real_surge_and_a_bearish_one(self):
        """Activity and direction are different questions. Calling a dump
        bullish because it is busy buys exit liquidity."""
        quiet = snap(vol_m5=2_000, buys=15, sells=15, buyers=12, sellers=12,
                     liq=900_000)
        dumping = snap(vol_m5=70_000, buys=120, sells=900,
                       buyers=90, sellers=500, liq=600_000, price_chg_h1=-22.0)
        r = score_snapshot(dumping, history_of(quiet))
        self.assertGreater(r["surge_score"], 40, "it IS surging")
        self.assertEqual(r["bias"], "bearish")

    def test_balanced_flow_is_mixed_not_bullish(self):
        quiet = snap(vol_m5=2_000, buys=15, sells=15, buyers=12, sellers=12,
                     liq=900_000)
        now = snap(vol_m5=40_000, buys=500, sells=480, buyers=300, sellers=290,
                   liq=910_000)
        self.assertEqual(score_snapshot(now, history_of(quiet))["bias"], "mixed")


class QualityHeuristicTests(unittest.TestCase):
    def test_many_trades_from_few_wallets_reduces_confidence(self):
        quiet = snap(vol_m5=2_000, buys=15, sells=15, buyers=12, sellers=12,
                     liq=900_000)
        washy = snap(vol_m5=60_000, buys=600, sells=400, buyers=4, sellers=3,
                     liq=900_000)
        broad = snap(vol_m5=60_000, buys=600, sells=400, buyers=380, sellers=260,
                     liq=900_000)
        r_wash = score_snapshot(washy, history_of(quiet))
        r_broad = score_snapshot(broad, history_of(quiet))
        self.assertLess(r_wash["surge_score"], r_broad["surge_score"])
        self.assertTrue(any("breadth" in x for x in r_wash["reasons"]))


if __name__ == "__main__":
    unittest.main()
