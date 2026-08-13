"""Replaying history under the CURRENT rules, to break a bootstrap deadlock.

Quarantining 8,904 pre-fix outcomes left calibration with no history, so
confidence capped at the no-evidence ceiling and almost nothing cleared the
gates: no data, so no trades, so no data.

The bars were always real — what was wrong was the exit logic applied to
them. So each historical signal is walked forward through its own
subsequent bars and closed the way the system would close it today.
"""
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from lib.signal_replay import (MIN_BARS_TO_RESOLVE, SOURCE_REPLAY, replay_signal)


def _bars(rows, start=None):
    """rows = [(high, low, close)] on consecutive hours."""
    start = start or datetime(2026, 8, 1, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(hours=i + 1) for i in range(len(rows))])
    return pd.DataFrame(
        {"high": [r[0] for r in rows], "low": [r[1] for r in rows],
         "close": [r[2] for r in rows], "open": [r[2] for r in rows],
         "volume": [1] * len(rows)}, index=idx)


def _sig(**over):
    s = {"id": "x", "asset_symbol": "TEST/USD", "direction": "Long",
         "timeframe": "1H", "entry_price": 100.0, "target_price": 110.0,
         "stop_loss": 95.0, "generated_at": "2026-08-01T00:00:00+00:00"}
    s.update(over)
    return s


class NoLookaheadTests(unittest.TestCase):
    """A bar overlapping the moment of generation may contain the very move
    the signal was reacting to."""

    def test_only_bars_after_the_signal_are_used(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        before = pd.DataFrame(
            {"high": [999.0], "low": [1.0], "close": [100.0], "open": [100.0], "volume": [1]},
            index=pd.DatetimeIndex([start - timedelta(hours=1)]))
        self.assertIsNone(replay_signal(_sig(), before))

    def test_no_history_returns_none_rather_than_a_guess(self):
        self.assertIsNone(replay_signal(_sig(), _bars([])))
        self.assertIsNone(replay_signal(_sig(), None))


class ExitResolutionTests(unittest.TestCase):
    def test_a_target_reached_is_a_win(self):
        out = replay_signal(_sig(), _bars([(101, 99, 100), (111, 100, 110)]))
        self.assertEqual(out["exit_reason"], "take_profit")
        self.assertGreater(out["pnl_pct"], 0)

    def test_a_stop_hit_is_a_loss(self):
        out = replay_signal(_sig(), _bars([(101, 99, 100), (100, 94, 95)]))
        self.assertEqual(out["exit_reason"], "stop_loss")
        self.assertLess(out["pnl_pct"], 0)

    def test_the_stop_is_assumed_first_within_a_bar(self):
        """Within one bar the order of touches is unknowable. Assuming the
        favourable one is how a backtest flatters itself."""
        out = replay_signal(_sig(), _bars([(101, 99, 100), (111, 94, 100)]))
        self.assertEqual(out["exit_reason"], "stop_loss")

    def test_an_unresolved_signal_closes_on_the_hold_window(self):
        out = replay_signal(_sig(), _bars([(101, 99, 100)] * 6))
        self.assertEqual(out["exit_reason"], "hold_window_elapsed")

    def test_a_short_is_mirrored(self):
        out = replay_signal(
            _sig(direction="Short", target_price=90.0, stop_loss=105.0),
            _bars([(101, 99, 100), (100, 89, 90)]))
        self.assertEqual(out["exit_reason"], "take_profit")
        self.assertGreater(out["pnl_pct"], 0)


class InstantResolutionIsNotEvidenceTests(unittest.TestCase):
    """A signal resolving in its FIRST bar predicted nothing — the level was
    already within reach when it was written. Every 1m outcome in the first
    full replay resolved in one bar and the bucket read 98.2%."""

    def test_a_first_bar_resolution_is_dropped(self):
        self.assertIsNone(replay_signal(_sig(), _bars([(111, 99, 110)])))

    def test_the_threshold_requires_more_than_one_bar(self):
        self.assertGreaterEqual(MIN_BARS_TO_RESOLVE, 2)

    def test_a_second_bar_resolution_is_kept(self):
        out = replay_signal(_sig(), _bars([(101, 99, 100), (111, 100, 110)]))
        self.assertIsNotNone(out)
        self.assertEqual(out["bars_held"], 2)


class CostsAndLabellingTests(unittest.TestCase):
    def test_costs_are_charged(self):
        out = replay_signal(_sig(), _bars([(101, 99, 100), (111, 100, 110)]))
        self.assertGreaterEqual(out["fees"], 0)
        self.assertLessEqual(out["pnl_usd"], out["gross_usd"])

    def test_every_outcome_is_labelled_as_replay(self):
        """A simulated fill must never be mistaken for an observed one."""
        out = replay_signal(_sig(), _bars([(101, 99, 100), (111, 100, 110)]))
        self.assertEqual(out["source"], SOURCE_REPLAY)

    def test_replay_counts_for_less_than_live(self):
        from lib.calibration import REPLAY_WEIGHT
        self.assertLess(REPLAY_WEIGHT, 1.0)
        self.assertGreater(REPLAY_WEIGHT, 0.0)


class BootstrapGateTests(unittest.TestCase):
    """Paper trades at a lower bar until observed outcomes exist; the live
    gate is untouched."""

    def test_the_bootstrap_bar_is_below_the_live_bar(self):
        from jobs.paper_trading import BOOTSTRAP_MIN_SCORE, PAPER_MIN_CONFIDENCE
        self.assertLess(BOOTSTRAP_MIN_SCORE, PAPER_MIN_CONFIDENCE)

    def test_replayed_outcomes_do_not_end_the_bootstrap(self):
        """A simulation cannot certify that the system is ready to stop
        bootstrapping."""
        import inspect
        from jobs import paper_trading
        src = inspect.getsource(paper_trading._observed_outcome_count)
        self.assertIn('!= "replay"', src)


if __name__ == "__main__":
    unittest.main()
