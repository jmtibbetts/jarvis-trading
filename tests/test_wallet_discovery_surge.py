"""Discovery must find tokens that just WOKE UP, not tokens that are big.

Absolute 24h volume answers "which pair is largest", which is a different
question and mostly the wrong one: a pair doing $2M every day outranks one
that went $5k -> $500k in an hour, and the second is where wallets that
were early are still visible.

Both regressions pinned here were found on live data, not by reasoning.
"""
import unittest

from lib.wallet_discovery import MIN_H1_VOLUME_USD, surge_metrics


def attrs(*, m5=0.0, h1=0.0, h6=0.0, h24=0.0,
          tx_h1=(0, 0), tx_h24=(0, 0),
          chg_h1=0.0, chg_h6=0.0, chg_h24=0.0):
    return {
        "volume_usd": {"m5": m5, "h1": h1, "h6": h6, "h24": h24},
        "transactions": {
            "h1": {"buys": tx_h1[0], "sells": tx_h1[1]},
            "h24": {"buys": tx_h24[0], "sells": tx_h24[1]},
        },
        "price_change_percentage": {"h1": chg_h1, "h6": chg_h6, "h24": chg_h24},
    }


class SurgeDetectionTests(unittest.TestCase):
    def test_steady_state_scores_about_one(self):
        """A token trading at a constant pace is not surging, however
        large. h1 == h24/24 is the definition of steady."""
        m = surge_metrics(attrs(h24=2_400_000, h6=600_000, h1=100_000,
                                m5=8_333, tx_h1=(500, 500),
                                tx_h24=(12_000, 12_000)))
        self.assertAlmostEqual(m["vol_accel_1h"], 1.0, places=1)
        self.assertLess(m["surge_score"], 1.3)

    def test_a_waking_token_outranks_a_permanently_large_one(self):
        """THE requirement, stated as a comparison."""
        big_and_flat = surge_metrics(attrs(
            h24=48_000_000, h6=12_000_000, h1=2_000_000, m5=166_000,
            tx_h1=(1_000, 1_000), tx_h24=(24_000, 24_000)))
        small_and_waking = surge_metrics(attrs(
            h24=600_000, h6=400_000, h1=300_000, m5=30_000,
            tx_h1=(900, 300), tx_h24=(3_000, 1_500), chg_h1=8, chg_h6=15))
        self.assertGreater(small_and_waking["surge_score"],
                           big_and_flat["surge_score"])

    def test_a_pool_minutes_old_cannot_pin_the_ratio_at_its_ceiling(self):
        """Found live: the entire top 8 sat at exactly vol_accel_5m = 12.0.

        m5/(h1/12) has a hard ceiling of 12, reached whenever m5 IS h1 —
        which is true of every pool that launched minutes ago. Ranking by
        it silently became "newest first"."""
        newborn = attrs(h24=50_000, h6=50_000, h1=50_000, m5=50_000,
                        tx_h1=(200, 50), tx_h24=(200, 50))
        m = surge_metrics(newborn)
        self.assertTrue(m["minutes_old"])
        self.assertIsNone(m["vol_accel_5m"],
                          "no baseline exists, so no ratio is reported")

    def test_a_newborn_does_not_outrank_a_token_with_a_real_baseline(self):
        newborn = surge_metrics(attrs(h24=80_000, h6=80_000, h1=80_000,
                                      m5=80_000, tx_h1=(300, 60),
                                      tx_h24=(300, 60)))
        measured = surge_metrics(attrs(h24=600_000, h6=400_000, h1=300_000,
                                       m5=30_000, tx_h1=(900, 300),
                                       tx_h24=(3_000, 1_500)))
        self.assertGreater(measured["surge_score"], newborn["surge_score"])

    def test_a_completed_pump_is_penalised_not_promoted(self):
        """Live: TOADLAYER at +113% h24 and -44.9% h6 was ranking top.
        Buying into that is buying exit liquidity."""
        m = surge_metrics(attrs(h24=2_000_000, h6=1_000_000, h1=200_000,
                                m5=16_000, tx_h1=(800, 600),
                                tx_h24=(20_000, 18_000),
                                chg_h24=113.3, chg_h6=-44.9))
        self.assertTrue(m["post_peak"])
        self.assertLess(m["surge_score"], 0.8)

    def test_a_young_pool_crashing_is_caught_too(self):
        """On a young pool h24 == h6, so the h24>50 clause never fires.
        GIF/SOL sat at -77% and still scored 12.9 before the second
        condition existed."""
        m = surge_metrics(attrs(h24=100_000, h6=100_000, h1=40_000,
                                m5=3_000, tx_h1=(100, 400),
                                tx_h24=(100, 400),
                                chg_h24=-77.1, chg_h6=-77.1))
        self.assertTrue(m["post_peak"])

    def test_acceleration_on_a_trickle_is_discounted(self):
        """300% more of $200 is still $600."""
        trickle = surge_metrics(attrs(h24=2_000, h6=1_000, h1=600, m5=100,
                                      tx_h1=(30, 5), tx_h24=(80, 20)))
        real = surge_metrics(attrs(h24=600_000, h6=400_000, h1=300_000,
                                   m5=30_000, tx_h1=(900, 300),
                                   tx_h24=(3_000, 1_500)))
        self.assertLess(trickle["h1_volume_usd"] if "h1_volume_usd" in trickle
                        else 600, MIN_H1_VOLUME_USD)
        self.assertGreater(real["surge_score"], trickle["surge_score"] * 5)

    def test_selling_pressure_reduces_the_score(self):
        base = dict(h24=600_000, h6=400_000, h1=300_000, m5=30_000,
                    tx_h24=(3_000, 1_500))
        buying = surge_metrics(attrs(**base, tx_h1=(900, 100)))
        selling = surge_metrics(attrs(**base, tx_h1=(100, 900)))
        self.assertGreater(buying["surge_score"], selling["surge_score"])

    def test_missing_data_scores_zero_rather_than_guessing(self):
        self.assertEqual(surge_metrics({})["surge_score"], 0.0)
        self.assertEqual(surge_metrics(attrs())["surge_score"], 0.0)


class DiscoveryIsScheduledTests(unittest.TestCase):
    def test_discovery_is_wired_into_the_scheduler(self):
        """It existed for a while and nothing ran it — the pipeline worked
        perfectly and had been executed exactly once, by hand."""
        from pathlib import Path
        src = Path("app/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("discover_from_tokens", src)
        self.assertIn("id='wallet_discovery'", src)


if __name__ == "__main__":
    unittest.main()
