"""The deep-history client, tested offline.

Network behaviour was verified live 2026-08-13 (NVDA 15m: 4,792 bars in one
call; overlapping Alpaca bars survived the upsert). These tests pin the
contracts that must not drift: interval coverage, source-priority ordering,
budget discipline, and the parsing/paging rules.
"""
import unittest
from unittest.mock import patch

import pandas as pd

from lib.twelvedata import (DAILY_CREDIT_FLOOR, MAX_BARS_PER_CALL,
                            TD_INTERVALS, CreditFloorReached, TwelveDataError,
                            backfill_symbol, fetch_series)


class IntervalMapTests(unittest.TestCase):
    # Timeframes with no Twelve Data interval, each for a stated reason:
    # 2D/1W are resampled locally from 1D so the week boundary stays under
    # our control; 3m simply does not exist at the vendor (1/5/15/30/45min).
    UNMAPPED = {"2D", "1W", "3m"}

    def test_every_native_timeframe_is_mapped_or_excused(self):
        """A timeframe that is neither mapped nor on the documented
        exception list means the backfill silently cannot cover it."""
        from lib.ohlcv import TF_CONFIG
        for tf in TF_CONFIG:
            if tf in self.UNMAPPED:
                self.assertNotIn(tf, TD_INTERVALS, tf)
            else:
                self.assertIn(tf, TD_INTERVALS, tf)

    def test_an_unmapped_timeframe_raises_rather_than_guessing(self):
        with self.assertRaises(TwelveDataError):
            fetch_series("NVDA", "3m")


class SourcePriorityTests(unittest.TestCase):
    """A backfilled bar must never clobber a live Alpaca bar, and must beat
    every fallback source — that ordering is the whole trust model."""

    def test_twelvedata_sits_below_alpaca_and_above_the_rest(self):
        from lib.ohlcv_cache import _source_priority
        self.assertLess(_source_priority("twelvedata"), _source_priority("alpaca"))
        self.assertGreater(_source_priority("twelvedata"), _source_priority("yfinance"))
        self.assertGreater(_source_priority("twelvedata"), _source_priority("binance"))
        self.assertGreater(_source_priority("twelvedata"), _source_priority(None))


class ParsingTests(unittest.TestCase):
    def _payload(self, n=3, start="2021-03-01 10:00:00"):
        idx = pd.date_range(start, periods=n, freq="15min")
        return {"values": [
            {"datetime": str(t), "open": "10", "high": "11",
             "low": "9", "close": "10.5", "volume": "100"}
            for t in reversed(idx)          # TD returns newest first
        ]}

    @patch("lib.twelvedata._throttled_get")
    def test_bars_come_back_ascending_utc_with_numeric_columns(self, get):
        get.return_value = self._payload()
        df = fetch_series("NVDA", "15m")
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(df.iloc[0]["close"], 10.5)
        self.assertEqual(df.attrs["source"], "twelvedata")

    @patch("lib.twelvedata._throttled_get")
    def test_empty_response_is_none_not_an_empty_frame(self, get):
        get.return_value = {"values": []}
        self.assertIsNone(fetch_series("NVDA", "15m"))

    @patch("lib.twelvedata._throttled_get")
    def test_fx_series_without_volume_gets_zero_volume(self, get):
        p = self._payload()
        for v in p["values"]:
            del v["volume"]
        get.return_value = p
        df = fetch_series("EUR/USD", "1H")
        self.assertIsNotNone(df)
        self.assertTrue((df["volume"] == 0).all())


class BudgetDisciplineTests(unittest.TestCase):
    """The 800/day pool is shared with everything else that will ever use
    this client. Draining it to zero for a backfill is refused."""

    @patch("lib.twelvedata.credits_remaining", return_value=DAILY_CREDIT_FLOOR)
    def test_the_backfill_stops_at_the_reserve_floor(self, _cr):
        with self.assertRaises(CreditFloorReached):
            backfill_symbol("NVDA", "15m", years=1.0)

    def test_the_floor_is_a_meaningful_reserve(self):
        self.assertGreaterEqual(DAILY_CREDIT_FLOOR, 25)


class PagingTests(unittest.TestCase):
    @patch("lib.twelvedata.credits_remaining", return_value=700)
    @patch("lib.twelvedata.earliest_timestamp", return_value=None)
    @patch("lib.ohlcv_cache._store_bars", return_value=5000)
    @patch("lib.twelvedata.fetch_series")
    def test_a_short_page_ends_the_walk(self, fetch, _store, _et, _cr):
        """Fewer than MAX_BARS_PER_CALL means the vendor has nothing older
        — continuing would spend credits asking again."""
        idx = pd.date_range("2024-01-01", periods=100, freq="15min", tz="UTC")
        df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                           "close": 1.0, "volume": 1.0}, index=idx)
        fetch.return_value = df
        r = backfill_symbol("NVDA", "15m", years=10.0)
        self.assertEqual(r["pages"], 1)

    @patch("lib.twelvedata.credits_remaining", return_value=700)
    @patch("lib.twelvedata.earliest_timestamp", return_value=None)
    @patch("lib.ohlcv_cache._store_bars", return_value=5000)
    @patch("lib.twelvedata.fetch_series")
    def test_pages_walk_backward_from_each_pages_earliest_bar(self, fetch, _s, _e, _c):
        calls = []

        def page(symbol, tf, end_date=None, **kw):
            calls.append(end_date)
            end = pd.Timestamp(end_date, tz="UTC") if end_date else pd.Timestamp.now(tz="UTC")
            idx = pd.date_range(end=end, periods=MAX_BARS_PER_CALL, freq="15min")
            if len(calls) >= 3:
                idx = idx[-10:]     # third page is short -> stop
            return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0,
                                 "close": 1.0, "volume": 1.0}, index=idx)

        fetch.side_effect = page
        r = backfill_symbol("NVDA", "15m", years=50.0)
        self.assertEqual(r["pages"], 3)
        self.assertIsNone(calls[0])                    # first page: from now
        self.assertIsNotNone(calls[1])                 # then anchored backward
        self.assertLess(pd.Timestamp(calls[2]), pd.Timestamp(calls[1]))


class PlanAwarenessTests(unittest.TestCase):
    """The client follows the PLAN the operator configured — pacing from
    TWELVEDATA_RPM, and a credit floor that goes inert on paid plans."""

    def test_default_pacing_is_free_tier(self):
        import os
        from lib.twelvedata import _min_call_spacing_s
        os.environ.pop("TWELVEDATA_RPM", None)
        self.assertAlmostEqual(_min_call_spacing_s(), 60 / 8 + 0.1, places=3)

    def test_rpm_env_reshapes_pacing(self):
        import os
        from lib.twelvedata import _min_call_spacing_s
        os.environ["TWELVEDATA_RPM"] = "55"
        try:
            self.assertAlmostEqual(_min_call_spacing_s(), 60 / 55 + 0.1, places=3)
        finally:
            os.environ.pop("TWELVEDATA_RPM", None)

    def test_garbage_rpm_falls_back_to_free_tier(self):
        import os
        from lib.twelvedata import _min_call_spacing_s
        os.environ["TWELVEDATA_RPM"] = "fast"
        try:
            self.assertAlmostEqual(_min_call_spacing_s(), 60 / 8 + 0.1, places=3)
        finally:
            os.environ.pop("TWELVEDATA_RPM", None)

    def test_paid_plan_reports_unlimited_not_negative(self):
        """A paid account reports no daily limit; the old default-800
        arithmetic would go NEGATIVE and trip the floor on the first day
        of the upgrade — the exact opposite of what was bought."""
        from unittest.mock import patch
        from lib.twelvedata import UNLIMITED_CREDITS, credits_remaining
        with patch("lib.twelvedata.api_usage",
                   return_value={"daily_usage": 5000}):
            self.assertEqual(credits_remaining(), UNLIMITED_CREDITS)
        with patch("lib.twelvedata.api_usage",
                   return_value={"plan_daily_limit": 800, "daily_usage": 100}):
            self.assertEqual(credits_remaining(), 700)


if __name__ == "__main__":
    unittest.main()
