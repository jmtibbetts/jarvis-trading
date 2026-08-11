import unittest

from lib.ev_model import (
    MIN_DECIDED, bucket_key, compute_ev_buckets, realized_move_pct, wilson_interval,
)


def _row(outcome="TARGET_HIT", score=75.0, asset_class="Equity", direction="Long",
         entry=100.0, target=110.0, stop=95.0):
    return {
        "outcome": outcome, "composite_score": score, "asset_class": asset_class,
        "direction": direction, "entry_price": entry, "target_price": target, "stop_loss": stop,
    }


class BucketKeyTests(unittest.TestCase):
    def test_score_bands(self):
        self.assertEqual(bucket_key(_row(score=30))[0], "score_under_50")
        self.assertEqual(bucket_key(_row(score=60))[0], "score_50_70")
        self.assertEqual(bucket_key(_row(score=85))[0], "score_70_plus")
        self.assertEqual(bucket_key(_row(score=None))[0], "score_unknown")

    def test_direction_normalized(self):
        self.assertEqual(bucket_key(_row(direction="Short_Leveraged"))[2], "short")
        self.assertEqual(bucket_key(_row(direction="Long"))[2], "long")


class RealizedMoveTests(unittest.TestCase):
    def test_long_target_hit(self):
        self.assertEqual(realized_move_pct(_row()), 10.0)  # (110-100)/100

    def test_long_stop_hit_is_negative(self):
        self.assertEqual(realized_move_pct(_row(outcome="STOP_HIT")), -5.0)

    def test_short_target_hit(self):
        r = _row(direction="Short", entry=100.0, target=90.0, stop=105.0)
        self.assertEqual(realized_move_pct(r), 10.0)  # (100-90)/100

    def test_short_stop_hit_is_negative(self):
        r = _row(direction="Short", outcome="STOP_HIT", entry=100.0, target=90.0, stop=105.0)
        self.assertEqual(realized_move_pct(r), -5.0)

    def test_undecided_outcomes_have_no_move(self):
        self.assertIsNone(realized_move_pct(_row(outcome="OPEN")))
        self.assertIsNone(realized_move_pct(_row(outcome="AMBIGUOUS")))

    def test_zero_entry_returns_none(self):
        self.assertIsNone(realized_move_pct(_row(entry=0)))


class WilsonIntervalTests(unittest.TestCase):
    def test_interval_brackets_the_proportion(self):
        low, high = wilson_interval(7, 10)
        self.assertLess(low, 0.7)
        self.assertGreater(high, 0.7)

    def test_small_sample_gives_wide_interval(self):
        low_s, high_s = wilson_interval(7, 10)
        low_l, high_l = wilson_interval(70, 100)
        self.assertGreater(high_s - low_s, high_l - low_l)

    def test_zero_n_returns_none(self):
        self.assertIsNone(wilson_interval(0, 0))

    def test_bounds_clamped_to_unit_interval(self):
        low, high = wilson_interval(10, 10)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)


class ComputeEvBucketsTests(unittest.TestCase):
    def test_small_bucket_reports_counts_but_no_probability(self):
        """The core honesty rule: 3 wins from 4 decided is an anecdote and
        must never surface as a 75% win rate."""
        rows = [_row()] * 3 + [_row(outcome="STOP_HIT")]
        b = compute_ev_buckets(rows)[0]
        self.assertTrue(b["insufficient_sample"])
        self.assertIsNone(b["win_probability"])
        self.assertEqual(b["wins"], 3)
        self.assertEqual(b["decided"], 4)
        self.assertIn("note", b)

    def test_sufficient_bucket_reports_probability_ev_and_ci(self):
        rows = [_row()] * 7 + [_row(outcome="STOP_HIT")] * 3  # 10 decided
        b = compute_ev_buckets(rows)[0]
        self.assertFalse(b["insufficient_sample"])
        self.assertEqual(b["win_probability"], 0.7)
        self.assertEqual(b["avg_win_pct"], 10.0)
        self.assertEqual(b["avg_loss_pct"], -5.0)
        # EV = 0.7*10 + 0.3*(-5) = 5.5
        self.assertEqual(b["expected_value_pct"], 5.5)
        self.assertIsNotNone(b["win_probability_ci95"])

    def test_undecided_outcomes_excluded_from_denominator(self):
        rows = ([_row()] * 7 + [_row(outcome="STOP_HIT")] * 3
                + [_row(outcome="OPEN")] * 5 + [_row(outcome="EXPIRED")] * 2
                + [_row(outcome="AMBIGUOUS")])
        b = compute_ev_buckets(rows)[0]
        self.assertEqual(b["decided"], 10)
        self.assertEqual(b["win_probability"], 0.7)  # unchanged by the 8 undecided
        self.assertEqual(b["open"], 5)
        self.assertEqual(b["expired"], 2)
        self.assertEqual(b["ambiguous"], 1)
        self.assertEqual(b["total"], 18)

    def test_separate_buckets_for_different_conditions(self):
        rows = [_row(score=85)] * MIN_DECIDED + [_row(score=40)] * MIN_DECIDED
        buckets = compute_ev_buckets(rows)
        self.assertEqual(len(buckets), 2)
        bands = {b["score_band"] for b in buckets}
        self.assertEqual(bands, {"score_70_plus", "score_under_50"})

    def test_empty_input(self):
        self.assertEqual(compute_ev_buckets([]), [])


if __name__ == "__main__":
    unittest.main()
