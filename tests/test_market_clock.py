"""Market hours come from the venue's calendar, and unknown means closed.

The hard-coded check this replaced — weekday && 13:30 <= UTC < 20:00,
copy-pasted five times across three jobs — was only correct during US
daylight time. On 2026-11-01 the session becomes 14:30-21:00 UTC and the
old code would have traded the first hour of a closed market every day,
plus every weekday holiday, at full length on half-days.
"""
import time
import unittest
from unittest.mock import patch

from lib import market_clock


def _reset_cache():
    market_clock._cache["at"] = 0.0
    market_clock._cache["clock"] = None


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_unreachable_clock_means_market_closed(self):
        with patch.object(market_clock, "_fetch_clock",
                          side_effect=ConnectionError("down")):
            self.assertFalse(market_clock.is_equity_market_open())

    def test_open_when_the_venue_says_open(self):
        with patch.object(market_clock, "_fetch_clock",
                          return_value={"is_open": True, "next_open": "x",
                                        "next_close": "y", "venue_timestamp": "z"}):
            self.assertTrue(market_clock.is_equity_market_open())

    def test_closed_when_the_venue_says_closed(self):
        with patch.object(market_clock, "_fetch_clock",
                          return_value={"is_open": False, "next_open": "x",
                                        "next_close": "y", "venue_timestamp": "z"}):
            self.assertFalse(market_clock.is_equity_market_open())

    def test_a_recent_cache_survives_an_outage(self):
        with patch.object(market_clock, "_fetch_clock",
                          return_value={"is_open": True}):
            market_clock.equity_clock()
        with patch.object(market_clock, "_fetch_clock",
                          side_effect=ConnectionError("down")):
            market_clock._cache["at"] = time.time() - 120   # past TTL, within stale limit
            self.assertTrue(market_clock.is_equity_market_open())

    def test_a_stale_cache_does_not_answer_forever(self):
        with patch.object(market_clock, "_fetch_clock",
                          return_value={"is_open": True}):
            market_clock.equity_clock()
        with patch.object(market_clock, "_fetch_clock",
                          side_effect=ConnectionError("down")):
            market_clock._cache["at"] = time.time() - (market_clock.CLOCK_STALE_LIMIT_SEC + 60)
            self.assertFalse(market_clock.is_equity_market_open())


class AssetClassTests(unittest.TestCase):
    def test_crypto_is_always_open(self):
        self.assertTrue(market_clock.market_status("Crypto")["is_open"])

    def test_unknown_equity_fails_closed(self):
        _reset_cache()
        with patch.object(market_clock, "_fetch_clock",
                          side_effect=ConnectionError("down")):
            s = market_clock.market_status("Equity")
        self.assertFalse(s["is_open"])
        self.assertEqual(s["source"], "unknown-fail-closed")
        _reset_cache()


class NoHardCodedHoursRemainTests(unittest.TestCase):
    def test_the_utc_window_is_gone_from_every_job(self):
        import inspect

        from jobs import execute_signals, generate_signals, scan_opportunities
        for mod in (execute_signals, generate_signals, scan_opportunities):
            self.assertNotIn("hour > 13", inspect.getsource(mod),
                             f"{mod.__name__} still hand-codes UTC market hours")


if __name__ == "__main__":
    unittest.main()
