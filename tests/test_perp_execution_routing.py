"""CRYPTO_PERP fills come from the PERPETUAL book, CRYPTO_SPOT from spot.

The proof is deliberately arithmetic rather than structural: the two books
are given DIFFERENT prices, and the fill says which one priced it. A test
that only checked "a bitnomial function was called" would still pass if the
spot quote leaked through somewhere downstream.

    SPOT   bid 64,400  ask 64,410
    PERP   bid 64,500  ask 64,600

A perp BUY that fills below 64,600 is being priced off spot. There is no
arrangement of these numbers in which the wrong book produces the right
answer, which is the point.

THE MAGNITUDES ARE REALISTIC ON PURPOSE, and the perp side is tick-aligned.
Two constraints make the tidy 100/101/102/103 version untestable here. BTC's
US contract quotes in $5 increments, so 102 and 103 both truncate to the
same raw level and produce a book crossed against itself. And the contract
is 0.01 BTC, so a "BTC" priced at $110 is a $1.10 contract against a
$0.15/side fee — 13.6% per side, which the catastrophic-product gate
correctly refuses. Numbers the instrument could never quote would be testing
arithmetic the exchange never emits.
"""
import json
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import execution_policy as POL
from lib import execution_snapshot as ES
from lib import product_router as PR

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "bitnomial_ws_capture.json").read_text(encoding="utf-8"))

# BTC's US perpetual: 0.01 BTC per contract, $5 tick.
PERP_SYM = "PBTCUCZ50"
TICK = 5.0

SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
# Multiples of the $5 tick, and strictly above the spot book on both sides.
PERP_BID_USD, PERP_ASK_USD = 64_500.0, 64_600.0


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _spot_feed(bid=SPOT_BID, ask=SPOT_ASK, age_s=0.2):
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": bid, "ask": ask, "at": _at(age_s)},
        trade_flow=lambda symbol, window=200: None)


def _seed_perp_book(bid_usd=PERP_BID_USD, ask_usd=PERP_ASK_USD,
                    market_state=MD.STATE_OPEN):
    """Put a real, coherent book in place at chosen USD prices."""
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid_usd / TICK), 50]],
                "asks": [[int(ask_usd / TICK), 50]]})
    if market_state != MD.STATE_OPEN:
        book.apply({"type": "status", "ack_id": "1001", "symbol": PERP_SYM,
                    "state": market_state})
    return book


SIGNAL = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
          "paper_direction": "Long", "entry_price": 64_400.0,
          "stop_loss": 61_000.0, "target_price": 70_000.0, "timeframe": "4H",
          "id": "sig-perp"}
PERP_SIGNAL = dict(SIGNAL, product=PR.CRYPTO_PERP)
SPOT_SIGNAL = dict(SIGNAL, product=PR.CRYPTO_SPOT)


class TheVenueFollowsTheProductTests(unittest.TestCase):

    def test_a_perp_routes_to_the_us_derivatives_venue(self):
        venue, ac = POL.resolve_execution_venue("BTC/USD", "Crypto",
                                                product=PR.CRYPTO_PERP)
        self.assertEqual(venue, BP.KRAKEN_US_VENUE)
        self.assertEqual(ac, "crypto")

    def test_spot_still_routes_to_the_spot_venue(self):
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            venue, _ = POL.resolve_execution_venue("BTC/USD", "Crypto",
                                                   product=PR.CRYPTO_SPOT)
        self.assertEqual(venue, "kraken")

    def test_paper_venue_cannot_capture_a_derivative(self):
        """PAPER_VENUE is a SPOT setting. It must not drag a perpetual onto
        the spot book any more than it may capture an equity."""
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            venue, _ = POL.resolve_execution_venue("BTC/USD", "Crypto",
                                                   product=PR.CRYPTO_PERP)
        self.assertNotEqual(venue, "kraken")

    def test_each_reader_speaks_for_exactly_one_product_family(self):
        self.assertEqual(ES.products_for("kraken"), frozenset({"CRYPTO_SPOT"}))
        self.assertEqual(ES.products_for(BP.KRAKEN_US_VENUE),
                         frozenset({"CRYPTO_PERP"}))

    def test_the_perp_venue_may_not_price_spot_either(self):
        """The substitution is refused in BOTH directions. A perpetual book
        is no more a spot price than a spot book was a perpetual price."""
        self.assertFalse(ES.prices_product(BP.KRAKEN_US_VENUE, "CRYPTO_SPOT"))


class TheTwoBooksProduceDifferentPricesTests(unittest.TestCase):

    def setUp(self):
        _seed_perp_book()
        self.addCleanup(MD.reset_books)

    def _readiness(self, signal):
        with _spot_feed():
            return POL.execution_readiness("BTC/USD", "Crypto", signal=signal)

    def test_a_perp_is_priced_from_the_perpetual_book(self):
        r = self._readiness(PERP_SIGNAL)
        self.assertTrue(r.ok, f"{r.reason}: {r.detail}")
        self.assertEqual(r.product, PR.CRYPTO_PERP)
        self.assertEqual(r.snapshot.bid, PERP_BID_USD)
        self.assertEqual(r.snapshot.ask, PERP_ASK_USD)

    def test_a_spot_signal_is_priced_from_the_spot_book(self):
        r = self._readiness(SPOT_SIGNAL)
        self.assertTrue(r.ok, f"{r.reason}: {r.detail}")
        self.assertEqual(r.snapshot.bid, SPOT_BID)
        self.assertEqual(r.snapshot.ask, SPOT_ASK)

    def test_the_perp_never_shows_the_spot_price(self):
        r = self._readiness(PERP_SIGNAL)
        self.assertNotEqual(r.snapshot.bid, SPOT_BID)
        self.assertNotEqual(r.snapshot.ask, SPOT_ASK)

    def test_the_snapshot_names_bitnomial_as_the_data_source(self):
        """Execution venue and market-data source are different facts, and
        neither may be read off the other."""
        r = self._readiness(PERP_SIGNAL)
        self.assertEqual(r.snapshot.source, "bitnomial_public_book")
        self.assertEqual(r.venue, BP.KRAKEN_US_VENUE)
        self.assertEqual(r.snapshot.provenance["execution_venue"],
                         BP.KRAKEN_US_VENUE)
        self.assertEqual(r.snapshot.provenance["market_data_source"],
                         BP.BITNOMIAL_SOURCE)

    def test_the_snapshot_carries_contract_identity_and_sequence(self):
        r = self._readiness(PERP_SIGNAL)
        p = r.snapshot.provenance
        self.assertEqual(p["bitnomial_symbol"], PERP_SYM)
        self.assertEqual(p["contract_size"], 0.01)
        self.assertEqual(p["price_increment"], TICK)
        self.assertIsNotNone(p["ack_id"])

    def test_the_instrument_identity_is_the_contract_not_the_pair(self):
        """"crypto:BTC/USD" names the thesis. A ledger recording that cannot
        tell two contracts on the same underlying apart."""
        r = self._readiness(PERP_SIGNAL)
        self.assertEqual(r.instrument, PERP_SYM)
        spot = self._readiness(SPOT_SIGNAL)
        self.assertNotEqual(spot.instrument, PERP_SYM)

    def test_depth_is_recorded_but_not_claimed_as_consumed(self):
        """Claiming depth-aware impact while filling at top of book would
        misdescribe the simulator."""
        r = self._readiness(PERP_SIGNAL)
        self.assertTrue(r.snapshot.provenance["depth_recorded_not_consumed"])
        self.assertIsNone(r.snapshot.depth)
        self.assertTrue(r.snapshot.provenance["depth_bids"])


class ABuyLiftsThePerpAskAndASellHitsThePerpBidTests(unittest.TestCase):

    def setUp(self):
        _seed_perp_book()
        self.addCleanup(MD.reset_books)

    def _fill(self, signal):
        from lib import canonical_entry as CE
        captured = {}

        def fake_settle(auth, *, fill_price, execution_provenance=None,
                        canonical_entry_fee_usd=None, observation_id=None,
                        execution_id=None):
            captured["fill"] = fill_price
            captured["qty"] = auth.qty
            captured["provenance"] = execution_provenance
            captured["fee"] = canonical_entry_fee_usd
            return {"ok": True, "position": {"id": "pos-test"}}

        with _spot_feed(), \
             patch("lib.paper_engine.settle_position_entry", fake_settle):
            res = CE.open_canonical_position(signal, decision_price=100.0)
        return res, captured

    def test_a_perp_buy_fills_at_or_above_the_perp_ask(self):
        res, cap = self._fill(PERP_SIGNAL)
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(cap["fill"], PERP_ASK_USD)

    def test_a_perp_buy_is_never_priced_off_the_cheaper_spot_ask(self):
        """THE WHOLE POINT. Filling a perp at the spot ask would hand the
        model a price the perpetual never traded at."""
        _, cap = self._fill(PERP_SIGNAL)
        self.assertGreater(cap["fill"], SPOT_ASK)

    def test_a_perp_short_fills_at_or_below_the_perp_bid(self):
        short = dict(PERP_SIGNAL, paper_direction="Short",
                     stop_loss=68_000.0, target_price=58_000.0)
        res, cap = self._fill(short)
        self.assertTrue(res.get("ok"), res)
        self.assertLessEqual(cap["fill"], PERP_BID_USD)

    def test_a_spot_buy_still_fills_against_the_spot_ask(self):
        res, cap = self._fill(SPOT_SIGNAL)
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(cap["fill"], SPOT_ASK)
        self.assertLess(cap["fill"], PERP_ASK_USD)

    def test_the_settled_position_records_the_perp_product_and_venue(self):
        _, cap = self._fill(PERP_SIGNAL)
        doc = cap["provenance"]
        self.assertEqual(doc["product"], PR.CRYPTO_PERP)
        self.assertEqual(doc["venue"], BP.KRAKEN_US_VENUE)
        self.assertEqual(doc["market_source"], "bitnomial_public_book")

    def test_the_perp_entry_fee_is_per_contract_not_a_spot_percentage(self):
        """A8 preserved: the US perpetual is priced per contract, and the
        spot schedule is never substituted."""
        from lib import fee_authority as FA
        _, cap = self._fill(PERP_SIGNAL)
        doc = cap["provenance"]
        self.assertEqual(doc["entry_fee_basis"], FA.PER_CONTRACT)
        self.assertIsNotNone(doc["entry_fee_contract_count"])
        self.assertIsNone(doc["entry_fee_rate"], "a per-contract fee has no rate")
        self.assertGreater(cap["fee"], 0.0)


class TheFeedFailsClosedTests(unittest.TestCase):

    def _attempt(self, signal=None):
        from lib import canonical_entry as CE
        captured = {}

        def fake_settle(auth, *, fill_price, execution_provenance=None,
                        canonical_entry_fee_usd=None, observation_id=None,
                        execution_id=None):
            captured["fill"] = fill_price
            return {"ok": True, "position": {"id": "pos-test"}}

        with _spot_feed(), \
             patch("lib.paper_engine.settle_position_entry", fake_settle):
            res = CE.open_canonical_position(signal or PERP_SIGNAL,
                                             decision_price=64_400.0)
        return res, captured

    def tearDown(self):
        MD.reset_books()

    def test_no_book_at_all_opens_nothing(self):
        MD.reset_books()
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertTrue(POL.is_venue_data_failure(res["error"]))

    def test_a_desynced_book_refuses_by_name(self):
        book = _seed_perp_book()
        book.invalidate("injected gap")
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.BOOK_DESYNCED)
        self.assertTrue(res["venue_failure"])

    def test_a_closed_session_refuses_by_name(self):
        _seed_perp_book(market_state=MD.STATE_CLOSE)
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.MARKET_NOT_OPEN)

    def test_a_halted_market_refuses_by_name(self):
        _seed_perp_book(market_state=MD.STATE_HALT)
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.MARKET_HALTED)

    def test_a_crossed_perp_book_refuses(self):
        _seed_perp_book(bid_usd=64_700.0, ask_usd=64_600.0)
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.CROSSED_BOOK)

    def test_a_one_sided_perp_book_refuses(self):
        MD.reset_books()
        MD.book_for(PERP_SYM, create=True).apply(
            {"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
             "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
             "bids": [[int(PERP_BID_USD / TICK), 50]], "asks": []})
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.ONE_SIDED_BOOK)

    def test_a_stale_book_refuses(self):
        MD.reset_books()
        book = MD.book_for(PERP_SYM, create=True)
        book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                    "timestamp": _at(600).isoformat().replace("+00:00", "Z"),
                    "bids": [[int(PERP_BID_USD / TICK), 50]],
                    "asks": [[int(PERP_ASK_USD / TICK), 50]]})
        book.received_at = _at(600)
        res, cap = self._attempt()
        self.assertNotIn("fill", cap)
        self.assertEqual(res["error"], POL.STALE_EXECUTION_DATA)

    def test_an_unverified_price_scale_opens_nothing(self):
        """SHIB. Its published increment implies a price ~1000x the market,
        and a guessed divisor would corrupt every downstream number."""
        shib = dict(PERP_SIGNAL, asset_symbol="SHIB/USD",
                    entry_price=0.0000045, stop_loss=0.0000040,
                    target_price=0.0000060)
        res, cap = self._attempt(shib)
        self.assertNotIn("fill", cap)
        self.assertTrue(POL.is_venue_data_failure(res["error"]))

    def test_every_perp_refusal_stays_off_the_thesis_record(self):
        for name in (POL.BOOK_DESYNCED, POL.MARKET_NOT_OPEN,
                     POL.MARKET_HALTED, POL.NO_EXECUTABLE_PERP_QUOTE):
            with self.subTest(reason=name):
                self.assertTrue(POL.is_venue_data_failure(name))


class TheStalenessPolicyIsMeasuredNotCopiedTests(unittest.TestCase):

    def test_the_perp_window_differs_from_the_spot_window(self):
        """A ticker that stops arriving means the data stopped. A book that
        stops changing means nobody moved a price."""
        self.assertNotEqual(ES.DEFAULT_PERP_MAX_AGE_S, ES.DEFAULT_MAX_AGE_S)

    def test_the_window_clears_the_measured_quiet_period(self):
        """Measured 2026-08-17 across four products: p99 gap 1.43s, longest
        genuine quiet period 11.12s."""
        self.assertGreater(ES.DEFAULT_PERP_MAX_AGE_S, 11.12)
        self.assertEqual(ES.PERP_AGE_MEASURED_ON, "2026-08-17")


if __name__ == "__main__":
    unittest.main()
