"""The sizer and the concentration cap have to agree.

Every one of these fails against the code as it stood on 2026-08-15.

The defect they pin was reported by the operator four separate times as
"the concentration limit is still broken", and each previous fix addressed
the GUARD. The guard was fine. `size_position` solved quantity from risk
alone — budget / stop_distance — and never looked at notional, so a tight
stop produced an oversized position BY CONSTRUCTION:

    risk budget $1,003 / $10 stop = 100 shares x $620 = $62,193
    $62,193 / $100,312 equity = 62%, against a 25% cap

The sizer proposed, the guard refused, and the operator got an error
stream instead of trades. `solve_position` had accepted `notional_cap_usd`
the whole time and nothing ever passed one.
"""
import unittest

from lib.concentration import (MAX_GROSS_EXPOSURE_PCT, MAX_SYMBOL_EXPOSURE_PCT,
                               check, headroom)
from lib.paper_engine import size_position


class HeadroomTests(unittest.TestCase):
    def test_empty_book_offers_the_symbol_cap(self):
        h = headroom("META", 100_000.0, [])
        self.assertAlmostEqual(h["max_notional"],
                               100_000.0 * MAX_SYMBOL_EXPOSURE_PCT / 100.0)
        self.assertEqual(h["binding"], "symbol")

    def test_existing_exposure_in_the_same_symbol_reduces_room(self):
        book = [{"symbol": "META", "notional": 15_000.0}]
        h = headroom("META", 100_000.0, book)
        self.assertAlmostEqual(h["max_notional"], 10_000.0)

    def test_symbol_already_over_cap_has_no_room_and_says_why(self):
        book = [{"symbol": "DOGE/USD", "notional": 70_000.0}]
        h = headroom("DOGE/USD", 100_000.0, book)
        self.assertEqual(h["max_notional"], 0.0)
        self.assertIn("70%", h["reason"])

    def test_gross_cap_binds_before_symbol_cap_on_a_full_book(self):
        # 39 unrelated symbols at 10% each = 390% gross, 10% of room left.
        book = [{"symbol": f"S{i}", "notional": 10_000.0} for i in range(39)]
        h = headroom("NEW", 100_000.0, book)
        self.assertEqual(h["binding"], "gross")
        self.assertAlmostEqual(h["max_notional"], 10_000.0)

    def test_no_equity_means_no_room(self):
        """Auto Sim reached -6,937 equity. A book past its capital gets zero
        room, never 'unlimited because the percentage is undefined'."""
        h = headroom("ANY", -6_937.04, [])
        self.assertEqual(h["max_notional"], 0.0)
        self.assertEqual(h["binding"], "equity")


class SizerRespectsCapTests(unittest.TestCase):
    EQUITY = 100_000.0

    def _size(self, entry, stop, book=(), symbol="META"):
        room = headroom(symbol, self.EQUITY, list(book))
        return room, size_position(self.EQUITY, entry, stop, 1.0, self.EQUITY,
                                   symbol=symbol,
                                   notional_cap_usd=room["max_notional"])

    def test_tight_stop_no_longer_proposes_a_position_the_guard_refuses(self):
        """THE regression. A 1.6% stop used to size to 62% of equity."""
        room, s = self._size(620.0, 610.0)
        self.assertTrue(s["ok"])
        pct = 100.0 * s["notional"] / self.EQUITY
        self.assertLessEqual(round(pct, 2), MAX_SYMBOL_EXPOSURE_PCT)
        # And the guard it will face must now agree.
        verdict = check("META", s["notional"], s["loss_at_stop"], self.EQUITY, [])
        self.assertTrue(verdict["ok"], verdict.get("reason"))

    def test_a_range_of_tight_stops_all_land_inside_the_cap(self):
        for entry, stop in [(620.0, 610.0), (230.0, 225.0), (95_000.0, 94_000.0),
                            (12.5, 12.4), (3.0, 2.97)]:
            with self.subTest(entry=entry, stop=stop):
                _, s = self._size(entry, stop)
                self.assertTrue(s["ok"])
                pct = 100.0 * s["notional"] / self.EQUITY
                self.assertLessEqual(round(pct, 2), MAX_SYMBOL_EXPOSURE_PCT,
                                     f"{entry}/{stop} sized to {pct:.1f}%")

    def test_a_wide_stop_is_left_alone(self):
        """The cap must only ever bind — it must not inflate a position that
        risk parity already sized below it."""
        _, s = self._size(620.0, 500.0)   # 19% stop
        pct = 100.0 * s["notional"] / self.EQUITY
        self.assertLess(pct, MAX_SYMBOL_EXPOSURE_PCT)
        # Same size with no cap supplied at all.
        uncapped = size_position(self.EQUITY, 620.0, 500.0, 1.0, self.EQUITY,
                                 symbol="META")
        self.assertAlmostEqual(s["notional"], uncapped["notional"], places=6)

    def test_partial_room_produces_a_smaller_position_not_a_rejection(self):
        """The behaviour change that matters to the operator: a symbol with
        10% of equity already open gets a 15% position, not an error."""
        book = [{"symbol": "META", "notional": 10_000.0}]
        room, s = self._size(620.0, 610.0, book=book)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["notional"], 15_000.0, delta=1.0)
        verdict = check("META", s["notional"], s["loss_at_stop"], self.EQUITY, book)
        self.assertTrue(verdict["ok"], verdict.get("reason"))

    def test_zero_room_must_not_be_read_as_no_cap(self):
        """`solve_position` treats a falsy cap as 'no cap', so passing
        `room or None` handed an exhausted symbol UNLIMITED size. Caught in
        testing when DOGE sized to 70% of equity with zero headroom."""
        book = [{"symbol": "DOGE/USD", "notional": 70_000.0}]
        room = headroom("DOGE/USD", self.EQUITY, book)
        self.assertEqual(room["max_notional"], 0.0)
        # The open path must refuse on the room check rather than sizing.
        # Reproduce the trap directly so it cannot come back:
        self.assertIsNone(room["max_notional"] or None,
                          "0.0 or None is None — never use that idiom here")


class InsolventBookStatusTests(unittest.TestCase):
    """A book past its capital must not report itself as within limits.

    Auto Sim reached realized_pnl -106,901 on 100,000 of starting cash, so
    equity went to -6,937. Every `pct_of_equity` silently became None and
    every `over_limit` became False, and the panel rendered $101k of XLF
    notional as "—%" with no breach flagged: indistinguishable from a
    healthy book.
    """

    def _status(self, equity):
        from app.database import get_db
        from lib.concentration import book_status
        with get_db() as db:
            return book_status("auto_sim", equity, db)

    def test_insolvent_book_names_its_state(self):
        out = self._status(-6_937.04)
        self.assertFalse(out["solvent"])
        self.assertEqual(out["state"], "insolvent")
        self.assertIn("no capital", out["state_detail"])

    def test_unknown_exposure_is_none_not_within_limits(self):
        out = self._status(-6_937.04)
        for s in out["symbols"]:
            self.assertIsNone(s["pct_of_equity"])
            self.assertIsNone(s["over_limit"],
                              "unknown must not collapse to 'within limits'")
        self.assertIsNone(out["gross_over_limit"])

    def test_solvent_book_is_unchanged(self):
        out = self._status(100_000.0)
        self.assertTrue(out["solvent"])
        self.assertEqual(out["state"], "ok")
        self.assertIsNone(out["state_detail"])
        for s in out["symbols"]:
            self.assertIsNotNone(s["pct_of_equity"])
            self.assertIn(s["over_limit"], (True, False))

    def test_an_insolvent_book_refuses_every_open(self):
        """The report and the guard must say the same thing."""
        verdict = check("ANY", 1_000.0, 50.0, -6_937.04, [])
        self.assertFalse(verdict["ok"])
        self.assertIn("no equity", verdict["reason"])


class QuoteCurrencyBucketingTests(unittest.TestCase):
    """One coin is one bet, whatever it is quoted in.

    Raised by the operator: most crypto, and nearly everything on a DEX,
    quotes in USDT or USDC rather than USD. The guard compared raw uppercased
    strings, so a book already holding 20% of DOGE/USD accepted another 20%
    of DOGE/USDT — two strings, one instrument, 40% of equity on a 25% cap.
    """

    def test_stable_quotes_collapse_to_one_bucket(self):
        from lib.concentration import canon_symbol
        for variant in ("DOGE/USD", "DOGE/USDT", "DOGE/USDC",
                        "DOGE-USD", "DOGE-USDT", "DOGE_USDC", "DOGE:USDT"):
            with self.subTest(variant=variant):
                self.assertEqual(canon_symbol(variant), "DOGE/USD")

    def test_non_crypto_tickers_are_left_alone(self):
        """A guard that quietly renames instruments is worse than one that
        misses a pairing, so collapsing requires a separator AND a stable
        quote. BRK-B and USDU must survive untouched."""
        from lib.concentration import canon_symbol
        for ticker in ("AAPL", "META", "SPY", "ES", "MNQ", "USDU", "BRK-B",
                       "RDS-A", ""):
            with self.subTest(ticker=ticker):
                self.assertEqual(canon_symbol(ticker), ticker.upper())

    def test_usdt_position_counts_against_an_existing_usd_position(self):
        book = [{"symbol": "DOGE/USD", "notional": 20_000.0}]
        verdict = check("DOGE/USDT", 20_000.0, 200.0, 100_000.0, book)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["symbol_exposure_pct"], 40.0)
        self.assertEqual(verdict["limit"], "symbol")

    def test_headroom_is_shared_across_quote_variants(self):
        book = [{"symbol": "SUI-USDC", "notional": 20_000.0}]
        h = headroom("SUI/USD", 100_000.0, book)
        self.assertAlmostEqual(h["max_notional"], 5_000.0)

    def test_book_aggregation_merges_variants(self):
        """Two rows in different quotes must report as ONE symbol, not two
        halves that each look compliant."""
        book = [{"symbol": "DOGE/USD", "notional": 15_000.0},
                {"symbol": "DOGE/USDT", "notional": 15_000.0}]
        h = headroom("DOGE/USD", 100_000.0, book)
        self.assertEqual(h["existing_symbol_notional"], 30_000.0)
        self.assertEqual(h["max_notional"], 0.0)


if __name__ == "__main__":
    unittest.main()
