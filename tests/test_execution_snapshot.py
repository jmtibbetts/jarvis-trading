"""CROSS-VENUE EVIDENCE DOES NOT BECOME VENUE EXECUTION TRUTH.

`execution_recorder.capture_microstructure()` took a venue, defaulted it to
"alpaca", and read market state from two functions that do not exist —
`orderbook_stream.get_book_snapshot` and `kraken_stream.get_tape_stats`. Both
raised into a swallowed except, so every capture had silently been empty.

Repairing the NAMES alone would have been worse than the outage. The real
accessors are `get_latest_snapshot(exchange, symbol)`, keyed by exchange and
serving Binance and Coinbase, and `trade_flow(symbol)`, which is Kraken-only.
Wired into an "alpaca"-labelled row they would have produced execution
observations that were correctly fetched and wrongly attributed: Kraken's
tape and Coinbase's book, filed under a venue that showed neither, teaching
the learning set that one exchange had displayed all of it.

These tests pin the rule that replaced it: a venue's snapshot contains that
venue's data or a stated refusal. Never a substitute.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import execution_snapshot as ES


def _at(seconds_ago: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


class VenueAuthorityIsNotShareableTests(unittest.TestCase):

    def test_kraken_reads_kraken_and_reports_no_depth(self):
        """Kraken has a quote and a tape here, and NO L2 adapter. The
        absence is reported, not filled in from a venue that has one."""
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.5)}), \
             patch("lib.kraken_stream.trade_flow",
                   return_value={"buy_volume": 3.0, "sell_volume": 1.0,
                                 "prints": 40, "flow_imbalance": 0.5}):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken")

        self.assertEqual(snap.status, ES.AVAILABLE)
        self.assertEqual((snap.bid, snap.ask), (100.0, 100.2))
        self.assertIn("kraken", snap.source)
        self.assertIsNone(snap.depth, "Kraken has no depth feed here")
        self.assertEqual(snap.depth_status, ES.UNAVAILABLE)
        self.assertIsNone(snap.depth_source)

    def test_a_coinbase_book_never_becomes_kraken_depth(self):
        """THE BUG, as a test. Coinbase is streaming a full book; a Kraken
        snapshot must still report no depth."""
        coinbase_book = {"best_bid": 100.0, "best_ask": 100.1,
                         "bid_depth": 500.0, "ask_depth": 480.0,
                         "imbalance": 0.02, "bids": [[100.0, 5]], "asks": [[100.1, 5]],
                         "health": {"valid": True, "age_seconds": 0.2}}
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 99.9, "ask": 100.3, "at": _at(0.2)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None), \
             patch("lib.orderbook_stream.get_latest_snapshot",
                   return_value=coinbase_book):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken")

        self.assertIsNone(snap.depth)
        self.assertEqual(snap.depth_status, ES.UNAVAILABLE)
        self.assertIsNone(snap.imbalance, "Coinbase imbalance is not Kraken's")
        # And the prices are Kraken's, not Coinbase's.
        self.assertEqual((snap.bid, snap.ask), (99.9, 100.3))

    def test_an_unknown_venue_gets_a_refusal_not_someone_elses_book(self):
        """Alpaca has no execution-data authority here. The old default."""
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.1)}):
            snap = ES.execution_market_snapshot("BTC/USD", "alpaca")
        self.assertEqual(snap.status, ES.UNAVAILABLE)
        self.assertIsNone(snap.bid)
        self.assertIsNone(snap.ask)
        self.assertIn("no execution-data authority", snap.reason)

    def test_binance_and_coinbase_are_read_separately(self):
        seen = []

        def fake(exchange, symbol):
            seen.append(exchange)
            return {"best_bid": 1.0, "best_ask": 1.1, "bid_depth": 10.0,
                    "ask_depth": 9.0, "imbalance": 0.05,
                    "health": {"valid": True, "age_seconds": 0.1}}

        with patch("lib.orderbook_stream.get_latest_snapshot", fake):
            ES.execution_market_snapshot("BTC/USD", "coinbase")
            ES.execution_market_snapshot("BTC/USD", "binance")
        self.assertEqual(seen, ["coinbase", "binance"])


class MissingIsNotZeroTests(unittest.TestCase):

    def test_no_quote_is_unavailable_rather_than_a_zero_spread(self):
        with patch("lib.kraken_stream.latest_quote", return_value=None), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken")
        self.assertEqual(snap.status, ES.UNAVAILABLE)
        self.assertIsNone(snap.spread, "a missing spread is not a tight market")
        self.assertIsNone(snap.spread_pct)
        self.assertIsNone(snap.mid)
        self.assertFalse(snap.fillable)

    def test_depth_absent_is_none_not_zero(self):
        """depth=0 would read as a market with no liquidity — a different
        and much more tradeable-looking claim than 'we have no feed'."""
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 10.0, "ask": 10.1, "at": _at(0.1)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken")
        self.assertIsNone(snap.depth)
        self.assertNotEqual(snap.depth, 0)


class OnlyAnAvailableSnapshotMayPriceAFillTests(unittest.TestCase):

    def _kraken(self, bid, ask, age_s=0.1, max_age_s=ES.DEFAULT_MAX_AGE_S):
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": bid, "ask": ask, "at": _at(age_s)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            return ES.execution_market_snapshot("BTC/USD", "kraken",
                                                max_age_s=max_age_s)

    def test_a_stale_quote_is_stale_not_available(self):
        """A 30-second-old crypto quote is a historical fact, not an offer."""
        snap = self._kraken(100.0, 100.2, age_s=30.0, max_age_s=10.0)
        self.assertEqual(snap.status, ES.STALE)
        self.assertFalse(snap.fillable)
        self.assertIn("older than", snap.reason)
        # The prices are still REPORTED — staleness is not erasure.
        self.assertEqual(snap.bid, 100.0)

    def test_a_crossed_book_is_refused_regardless_of_freshness(self):
        snap = self._kraken(100.5, 100.0, age_s=0.01)
        self.assertEqual(snap.status, ES.CROSSED)
        self.assertFalse(snap.fillable)

    def test_a_one_sided_book_cannot_price_either_direction(self):
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": None, "at": _at(0.1)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken")
        self.assertEqual(snap.status, ES.ONE_SIDED)
        self.assertFalse(snap.fillable)

    def test_only_available_is_fillable(self):
        for status in (ES.STALE, ES.CROSSED, ES.ONE_SIDED,
                       ES.UNAVAILABLE, ES.FALLBACK):
            with self.subTest(status=status):
                self.assertNotIn(status, ES.FILLABLE)
        self.assertIn(ES.AVAILABLE, ES.FILLABLE)


class TheRecorderCarriesTheAttributionTests(unittest.TestCase):

    def test_the_stale_accessors_are_gone_from_executable_code(self):
        """By AST, so the docstring that names them in order to explain the
        bug cannot itself trip the check."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "execution_recorder.py").read_text(encoding="utf-8")
        called = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute) else None)
                if name:
                    called.add(name)
            if isinstance(node, ast.ImportFrom):
                called.update(a.name for a in node.names)
        self.assertNotIn("get_book_snapshot", called)
        self.assertNotIn("get_tape_stats", called)

    def test_capture_records_the_venue_and_its_status(self):
        from lib import execution_recorder as rec
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.2)}), \
             patch("lib.kraken_stream.trade_flow",
                   return_value={"buy_volume": 2.0, "sell_volume": 1.0,
                                 "prints": 10, "flow_imbalance": 0.33}):
            snap = rec.capture_microstructure("BTC/USD", venue="kraken")
        self.assertEqual(snap["venue"], "kraken")
        self.assertEqual(snap["execution_status"], ES.AVAILABLE)
        self.assertEqual(snap["depth_status"], ES.UNAVAILABLE)
        self.assertIn("kraken", snap["tape_source"])
        self.assertNotIn("bid_depth", snap, "Kraken has no depth to record")

    def test_capture_on_an_unauthoritative_venue_records_no_prices(self):
        from lib import execution_recorder as rec
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.1)}):
            snap = rec.capture_microstructure("BTC/USD", venue="alpaca")
        self.assertEqual(snap["execution_status"], ES.UNAVAILABLE)
        self.assertNotIn("bid", snap)
        self.assertNotIn("spread_pct", snap,
                         "an unavailable market has no spread, not a zero one")


if __name__ == "__main__":
    unittest.main()
