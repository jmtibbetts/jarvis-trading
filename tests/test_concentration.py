"""Portfolio concentration — the limit that belongs to the BOOK.

Measured 2026-08-16 on a $100k paper book: XAUT/USD controlled 146% of
equity, LTC 74%, BNB 53%, each with correct 1-3% margin and correct
sub-budget risk. Exposure was the unbounded quantity, and exposure is
what gaps through a stop.

The earlier attempt put this ceiling inside solve_position and it broke
risk parity — proven, four tests refused it. So these tests also pin the
DIVISION: sizing stays pure, concentration is judged against the book.
"""
import unittest

from lib.concentration import (
    MAX_GROSS_EXPOSURE_PCT,
    MAX_SYMBOL_EXPOSURE_PCT,
    check,
)

EQUITY = 100_000.0
OK_RISK = EQUITY * 0.01     # 1% — a normal, meaningful risk


def _pos(symbol, notional):
    return {"symbol": symbol, "notional": notional}


class SymbolLimitTests(unittest.TestCase):
    def test_the_xaut_position_that_started_this_is_refused(self):
        v = check("XAUT/USD", 146_487, OK_RISK, EQUITY, [])
        self.assertFalse(v["ok"])
        self.assertEqual(v["limit"], "symbol")
        self.assertIn("146", v["reason"])

    def test_an_ordinary_position_passes(self):
        v = check("BTC/USD", 20_000, OK_RISK, EQUITY, [])
        self.assertTrue(v["ok"], v["reason"])
        self.assertEqual(v["symbol_exposure_pct"], 20.0)

    def test_stacking_the_same_symbol_counts_as_one_bet(self):
        """Two 20% positions in one instrument is a 40% bet on one thing,
        however it is spread across rows."""
        book = [_pos("BTC/USD", 20_000)]
        v = check("BTC/USD", 20_000, OK_RISK, EQUITY, book)
        self.assertFalse(v["ok"])
        self.assertEqual(v["limit"], "symbol")
        self.assertEqual(v["symbol_exposure_pct"], 40.0)

    def test_symbol_matching_is_case_insensitive(self):
        book = [_pos("btc/usd", 20_000)]
        v = check("BTC/USD", 20_000, OK_RISK, EQUITY, book)
        self.assertFalse(v["ok"], "case must not create a second bucket")


class GrossLimitTests(unittest.TestCase):
    def test_many_small_positions_still_hit_the_gross_ceiling(self):
        book = [_pos(f"SYM{i}/USD", 24_000) for i in range(17)]  # 408%
        v = check("NEW/USD", 10_000, OK_RISK, EQUITY, book)
        self.assertFalse(v["ok"])
        self.assertEqual(v["limit"], "gross")

    def test_a_diversified_book_under_the_ceiling_passes(self):
        book = [_pos(f"SYM{i}/USD", 20_000) for i in range(10)]  # 200%
        v = check("NEW/USD", 20_000, OK_RISK, EQUITY, book)
        self.assertTrue(v["ok"], v["reason"])
        self.assertEqual(v["gross_exposure_pct"], 220.0)


class TrivialPositionTests(unittest.TestCase):
    def test_a_position_risking_almost_nothing_is_refused(self):
        """XAUT risked ~$141 of a $1,000 budget while controlling $146k.
        Shrunk to fit, such a trade pays real fees and teaches nothing."""
        v = check("XAUT/USD", 5_000, 1.50, EQUITY, [])
        self.assertFalse(v["ok"])
        self.assertEqual(v["limit"], "trivial")

    def test_a_meaningful_risk_passes(self):
        v = check("BTC/USD", 5_000, EQUITY * 0.005, EQUITY, [])
        self.assertTrue(v["ok"], v["reason"])


class FailClosedTests(unittest.TestCase):
    def test_zero_equity_refuses(self):
        self.assertFalse(check("BTC/USD", 1000, 10, 0.0, [])["ok"])

    def test_an_unreadable_book_refuses_rather_than_permits(self):
        from unittest.mock import patch
        from lib.concentration import check_against_book
        with patch("app.database.get_db", side_effect=RuntimeError("db down")):
            v = check_against_book("BTC/USD", 1000, 10, EQUITY)
        self.assertFalse(v["ok"], "an unknown book is not permission")
        self.assertEqual(v["limit"], "error")

    def test_malformed_book_rows_do_not_crash_the_check(self):
        book = [{"symbol": None, "notional": "junk"}, {}, _pos("X/USD", 1000)]
        v = check("BTC/USD", 10_000, OK_RISK, EQUITY, book)
        self.assertTrue(v["ok"], v["reason"])


class LimitsAreCoherentTests(unittest.TestCase):
    def test_symbol_cap_admits_the_ordinary_trade_sizing_produces(self):
        """1% budget / 5% stop = 20% notional at 1x. A symbol cap below
        that would strangle normal trades — the exact mistake the
        in-sizing version made."""
        self.assertGreater(MAX_SYMBOL_EXPOSURE_PCT, 20.0)

    def test_gross_ceiling_admits_a_diversified_leveraged_book(self):
        self.assertGreaterEqual(MAX_GROSS_EXPOSURE_PCT,
                                MAX_SYMBOL_EXPOSURE_PCT * 4)


if __name__ == "__main__":
    unittest.main()
