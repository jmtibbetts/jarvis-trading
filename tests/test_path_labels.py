"""MFE, MAE and first-touch — measured, not inferred from the outcome.

Entry and exit alone cannot distinguish a trade that ran straight to target
from one that sat two bars from being stopped out first. Identical P&L,
entirely different risk, and only one of them is repeatable. `trade_outcomes`
recorded entry and exit and nothing in between, so the path was invisible.

The replay loop already walks exactly the bars needed, so the labels are
computed there.

Two properties these tests exist to defend:

  A bar containing BOTH the stop and the target is AMBIGUOUS. OHLC cannot
  reveal intrabar ordering, and resolving it in the trade's favour is
  precisely how a backtest manufactures an edge it will never realise.

  MFE/MAE are in R. Percent cannot be pooled across a 15m scalp and a
  weekly position; R can.
"""
import unittest

import pandas as pd

from lib.signal_replay import (MIN_BARS_TO_RESOLVE, PATH_SOURCE_REPLAY,
                               replay_signal)

START = "2026-01-01T00:00:00+00:00"


def bars(rows, freq="h"):
    """rows: list of (high, low). open/close sit mid-range; volume flat."""
    idx = pd.date_range("2026-01-01T01:00:00+00:00", periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": [(h + l) / 2 for h, l in rows],
         "high": [h for h, _ in rows],
         "low": [l for _, l in rows],
         "close": [(h + l) / 2 for h, l in rows],
         "volume": [1000.0] * len(rows)},
        index=idx,
    )


def signal(direction="Long", entry=100.0, stop=98.0, target=106.0, tf="1H"):
    return {"id": "sig-1", "asset_symbol": "TEST/USD", "asset_class": "crypto",
            "direction": direction, "timeframe": tf, "entry_price": entry,
            "stop_loss": stop, "target_price": target, "generated_at": START}


class ExcursionsAreMeasuredTests(unittest.TestCase):

    def test_a_long_records_how_far_it_ran_in_favour(self):
        # risk = 2.0; best high 104 => +4.0 => 2.0R
        out = replay_signal(signal(), bars([(101, 100), (104, 100), (103, 99)]))
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["mfe_r"], 2.0, places=3)

    def test_a_long_records_how_far_it_ran_against(self):
        # worst low 99 => 1.0 adverse => 0.5R on risk 2.0
        out = replay_signal(signal(), bars([(101, 100), (102, 99), (103, 100)]))
        self.assertAlmostEqual(out["mae_r"], 0.5, places=3)

    def test_shorts_measure_in_the_mirror(self):
        # short from 100, stop 102, target 94. risk = 2.0
        # favourable = entry - low; best low 97 => 3.0 => 1.5R
        out = replay_signal(signal("Short", 100.0, 102.0, 94.0),
                            bars([(100.5, 99), (101, 97), (101, 98)]))
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["mfe_r"], 1.5, places=3)

    def test_the_bar_index_of_each_extreme_is_recorded(self):
        out = replay_signal(signal(), bars([(101, 100), (104, 100), (102, 99)]))
        self.assertEqual(out["mfe_bar"], 2)
        self.assertEqual(out["mae_bar"], 3)

    def test_a_trade_that_never_moved_reports_zero_not_none(self):
        out = replay_signal(signal(), bars([(100, 100), (100, 100), (100, 100)]))
        self.assertEqual(out["mfe_r"], 0.0)
        self.assertEqual(out["mae_r"], 0.0)

    def test_two_trades_with_identical_pnl_can_differ_in_path(self):
        """The whole point. Both end at the target; one nearly stopped out."""
        smooth = replay_signal(signal(), bars([(102, 101), (104, 102), (107, 105)]))
        rough = replay_signal(signal(), bars([(101, 98.5), (102, 98.2), (107, 100)]))
        self.assertEqual(smooth["exit_reason"], rough["exit_reason"])
        self.assertGreater(rough["mae_r"], smooth["mae_r"])


class AmbiguousBarsAreNotResolvedFavourablyTests(unittest.TestCase):
    """A bar spanning both levels cannot say which came first. Choosing the
    profitable one is how a backtest invents an edge."""

    def test_a_bar_containing_both_levels_is_ambiguous(self):
        # entry 100, stop 98, target 106 — this bar spans 97..107
        out = replay_signal(signal(), bars([(101, 100), (107, 97)]))
        self.assertIsNotNone(out)
        self.assertEqual(out["first_touch"], "AMBIGUOUS")

    def test_it_is_not_recorded_as_the_profitable_touch(self):
        out = replay_signal(signal(), bars([(101, 100), (107, 97)]))
        self.assertNotEqual(out["first_touch"], "TARGET")

    def test_an_unambiguous_stop_is_named(self):
        out = replay_signal(signal(), bars([(101, 100), (101, 97)]))
        self.assertEqual(out["first_touch"], "STOP")

    def test_an_unambiguous_target_is_named(self):
        out = replay_signal(signal(), bars([(101, 100), (107, 101)]))
        self.assertEqual(out["first_touch"], "TARGET")

    def test_a_trade_that_resolves_neither_way_has_no_first_touch(self):
        out = replay_signal(signal(), bars([(101, 100), (102, 99), (101, 100)]))
        self.assertIsNone(out["first_touch"])
        self.assertEqual(out["exit_reason"], "hold_window_elapsed")


class ProvenanceAndUnitsTests(unittest.TestCase):

    def test_every_replayed_path_is_labelled_as_replay(self):
        out = replay_signal(signal(), bars([(101, 100), (104, 100), (102, 99)]))
        self.assertEqual(out["path_source"], PATH_SOURCE_REPLAY)

    def test_replay_is_never_labelled_as_observed(self):
        out = replay_signal(signal(), bars([(101, 100), (104, 100), (102, 99)]))
        self.assertNotEqual(out["path_source"], "LIVE_OBSERVED")

    def test_no_risk_distance_means_no_r(self):
        """Zero is a claim — it would train as "never moved"."""
        out = replay_signal(signal(entry=100.0, stop=100.0, target=106.0),
                            bars([(101, 100), (104, 100), (107, 100)]))
        if out is not None:
            self.assertIsNone(out["mfe_r"])
            self.assertIsNone(out["mae_r"])

    def test_the_extreme_prices_are_recorded_too(self):
        out = replay_signal(signal(), bars([(101, 100), (104, 99), (102, 100)]))
        self.assertAlmostEqual(out["max_favorable_price"], 104.0, places=4)
        self.assertAlmostEqual(out["max_adverse_price"], 99.0, places=4)


class NoLookaheadTests(unittest.TestCase):
    """Path labels must come only from bars that printed AFTER the signal."""

    def test_bars_before_the_signal_are_not_walked(self):
        idx = pd.date_range("2025-12-31T00:00:00+00:00", periods=3, freq="h", tz="UTC")
        past = pd.DataFrame({"open": [100.0] * 3, "high": [200.0] * 3,
                             "low": [50.0] * 3, "close": [100.0] * 3,
                             "volume": [1.0] * 3}, index=idx)
        self.assertIsNone(replay_signal(signal(), past))

    def test_an_enormous_prior_bar_cannot_inflate_mfe(self):
        pre = pd.date_range("2025-12-30T00:00:00+00:00", periods=2, freq="h", tz="UTC")
        post = pd.date_range("2026-01-01T01:00:00+00:00", periods=3, freq="h", tz="UTC")
        df = pd.concat([
            pd.DataFrame({"open": [100.0] * 2, "high": [500.0] * 2, "low": [100.0] * 2,
                          "close": [100.0] * 2, "volume": [1.0] * 2}, index=pre),
            pd.DataFrame({"open": [100.0] * 3, "high": [101.0, 104.0, 102.0],
                          "low": [100.0, 100.0, 99.0], "close": [100.0] * 3,
                          "volume": [1.0] * 3}, index=post),
        ])
        out = replay_signal(signal(), df)
        self.assertIsNotNone(out)
        self.assertLess(out["mfe_r"], 3.0, "a pre-signal bar leaked into MFE")


class PersistenceTests(unittest.TestCase):
    def test_the_outcome_table_can_hold_every_label(self):
        from app.database import TradeOutcome
        for col in ("mfe_r", "mae_r", "mfe_bar", "mae_bar",
                    "first_touch", "path_source"):
            self.assertTrue(hasattr(TradeOutcome, col), f"TradeOutcome lacks {col}")

    def test_persist_writes_them(self):
        import inspect
        from lib import signal_replay
        src = inspect.getsource(signal_replay.persist)
        for col in ("mfe_r", "mae_r", "first_touch", "path_source"):
            self.assertIn(col, src, f"persist() drops {col}")


if __name__ == "__main__":
    unittest.main()
