import unittest
from datetime import date

from lib.catalyst_calendar import (
    assemble_calendar, next_13f_deadlines, next_futures_expirations,
    next_options_expirations, third_friday,
)


class ThirdFridayTests(unittest.TestCase):
    def test_known_third_fridays(self):
        # Verifiable by calendar: Aug 2026's Fridays are 7,14,21,28.
        self.assertEqual(third_friday(2026, 8), date(2026, 8, 21))
        # Jan 2027: Fridays 1,8,15,22,29.
        self.assertEqual(third_friday(2027, 1), date(2027, 1, 15))
        # Month starting on Friday: May 2026 (May 1 is a Friday) -> 15th.
        self.assertEqual(third_friday(2026, 5), date(2026, 5, 15))


class OptionsExpirationTests(unittest.TestCase):
    def test_includes_current_month_when_not_yet_passed(self):
        out = next_options_expirations(date(2026, 8, 10), count=2)
        self.assertEqual(out[0]["date"], "2026-08-21")
        self.assertEqual(out[0]["days_away"], 11)

    def test_skips_current_month_when_passed(self):
        out = next_options_expirations(date(2026, 8, 25), count=1)
        self.assertEqual(out[0]["date"], "2026-09-18")

    def test_year_rollover(self):
        out = next_options_expirations(date(2026, 12, 20), count=2)
        self.assertTrue(out[0]["date"].startswith("2027-01"))

    def test_carries_approximation_label(self):
        out = next_options_expirations(date(2026, 8, 10), count=1)
        self.assertEqual(out[0]["approximation"], "holiday_shifts_not_applied")


class FuturesExpirationTests(unittest.TestCase):
    def test_only_quarterly_months(self):
        out = next_futures_expirations(date(2026, 8, 10), count=2)
        self.assertEqual(out[0]["date"], "2026-09-18")  # Sep 2026 3rd Friday
        self.assertEqual(out[1]["date"], "2026-12-18")


class Deadline13FTests(unittest.TestCase):
    def test_deadline_is_45_days_after_quarter_end(self):
        out = next_13f_deadlines(date(2026, 8, 10), count=1)
        # Q2 2026 ended 6/30; +45 days = 8/14.
        self.assertEqual(out[0]["date"], "2026-08-14")
        self.assertIsNone(out[0]["approximation"])  # statutory, exact

    def test_next_deadline_after_passing_one(self):
        out = next_13f_deadlines(date(2026, 8, 20), count=1)
        # Q3 ends 9/30; +45 = 11/14.
        self.assertEqual(out[0]["date"], "2026-11-14")


class AssembleTests(unittest.TestCase):
    def test_earnings_filtered_to_tracked_and_week_granular(self):
        cal = assemble_calendar(
            date(2026, 8, 10),
            earnings_tickers={"AAPL", "XYZ", "NVDA"},
            tracked_equities={"AAPL", "NVDA", "TSLA"},
        )
        earnings = [e for e in cal["events"] if e["type"] == "EARNINGS_THIS_WEEK"]
        self.assertEqual(len(earnings), 1)
        self.assertEqual(earnings[0]["tickers"], ["AAPL", "NVDA"])
        self.assertEqual(earnings[0]["granularity"], "week")

    def test_no_earnings_entry_when_none_relevant(self):
        cal = assemble_calendar(date(2026, 8, 10), earnings_tickers=set(),
                                tracked_equities={"AAPL"})
        self.assertEqual([e for e in cal["events"] if e["type"] == "EARNINGS_THIS_WEEK"], [])

    def test_events_sorted_by_date(self):
        cal = assemble_calendar(date(2026, 8, 10))
        dates = [e["date"] for e in cal["events"]]
        self.assertEqual(dates, sorted(dates))

    def test_ipo_entries_included(self):
        cal = assemble_calendar(date(2026, 8, 10), priced_ipos=[
            {"company_name": "Latigo Biotherapeutics, Inc.", "ticker": "LTGO",
             "latest_filed_at": "2026-08-07T10:00:00"},
        ])
        ipos = [e for e in cal["events"] if e["type"] == "IPO_PRICED"]
        self.assertEqual(len(ipos), 1)
        self.assertIn("LTGO", ipos[0]["title"])
        self.assertEqual(ipos[0]["date"], "2026-08-07")


if __name__ == "__main__":
    unittest.main()
