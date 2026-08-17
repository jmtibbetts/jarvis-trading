"""MARK AUTHORITY IS NOT EXECUTION AUTHORITY.

The paper book is not crypto-only: 487 crypto, 94 equity and 86 futures
positions are open. But `_get_current_price()` resolves through Alpaca's last
price, a MarketAsset row, then a yfinance futures cache — three MARKS, none
of which can answer what an order would have filled at.

Equities are now served by Alpaca's real two-sided latest-quote endpoint.
Futures and forex are not, and `PAPER_VENUE=kraken` must never become "route
AAPL, ES=F and EURUSD=X through Kraken". A last price must not become a fill
for any of them; refusing the entry is the correct outcome of that gap.

These tests pin the refusals, because the refusals are the safety property.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import execution_policy as P
from lib import execution_snapshot as ES


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


class ProductRoutingTests(unittest.TestCase):
    """One environment variable must not capture every asset class."""

    def test_crypto_routes_to_the_configured_crypto_venue(self):
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            venue, ac = P.resolve_execution_venue("BTC/USD", "Crypto")
        self.assertEqual((venue, ac), ("kraken", "crypto"))

    def test_paper_venue_does_not_capture_equities(self):
        """THE NONSENSE THIS PREVENTS: an equity priced against a crypto
        book because a crypto setting was read globally."""
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            venue, ac = P.resolve_execution_venue("AAPL", "us_equity")
        self.assertEqual(ac, "equity")
        self.assertNotEqual(venue, "kraken")

    def test_paper_venue_does_not_capture_futures(self):
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            venue, ac = P.resolve_execution_venue("ES=F", "Futures")
        self.assertEqual(ac, "futures")
        self.assertNotEqual(venue, "kraken")

    def test_the_messy_stored_asset_class_vocabulary_still_classifies(self):
        """Thirty-odd spellings exist in trade_outcomes: "us_equity",
        "Equity", "equity", "Commodity/Defense". The symbol is asked first."""
        for label in ("us_equity", "Equity", "equity", "Equity ETF"):
            with self.subTest(label=label):
                self.assertEqual(P.classify_asset_class("AAPL", label), "equity")


class AProductIsNotAnAssetClassTests(unittest.TestCase):
    """A9. `execution_policy` answered "which product?" with "crypto".

    That is an asset class. It cannot distinguish a perpetual from a spot
    pair, so every layer that takes the product as established fact — the
    venue capability gate, the fee schedule, settlement, the learning row —
    was working from an identity that does not name a tradable thing.
    """

    def _readiness(self, symbol="BTC/USD", asset_class="Crypto", **env):
        import os
        with patch.dict(os.environ, env), \
             patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.2)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            return P.execution_readiness(symbol, asset_class)

    def test_the_product_is_a_real_product_not_an_asset_class(self):
        from lib import product_router as PR
        r = self._readiness()
        self.assertIn(r.product, PR.ALL_PRODUCTS)
        self.assertNotEqual(r.product, "crypto")

    def test_the_asset_class_survives_alongside_the_product(self):
        """Both are needed. Collapsing them was the defect; replacing one
        with the other would just move it."""
        r = self._readiness()
        self.assertEqual(r.asset_class, "crypto")
        self.assertNotEqual(r.product, r.asset_class)

    def test_the_vocabulary_comes_from_the_product_router(self):
        """Not a second taxonomy — the router already owned these names."""
        from lib import product_router as PR
        self.assertEqual(PR.CRYPTO_PERP, "CRYPTO_PERP")
        self.assertEqual(PR.ASSET_CLASS_OF[PR.CRYPTO_PERP], "crypto")
        self.assertEqual(PR.ASSET_CLASS_OF[PR.CRYPTO_SPOT], "crypto")

    def test_the_desk_setting_selects_spot_or_perp(self):
        from lib import product_router as PR
        self.assertEqual(
            P.resolve_product("BTC/USD", "Crypto",
                              signal={"product": PR.CRYPTO_SPOT}), PR.CRYPTO_SPOT)
        import os
        with patch.dict(os.environ, {"CRYPTO_PRODUCT": "spot"}):
            self.assertEqual(P.resolve_product("BTC/USD", "Crypto"),
                             PR.CRYPTO_SPOT)
        with patch.dict(os.environ, {"CRYPTO_PRODUCT": "perp"}):
            self.assertEqual(P.resolve_product("BTC/USD", "Crypto"),
                             PR.CRYPTO_PERP)

    def test_product_is_never_derived_from_leverage(self):
        """PRODUCT IS NOT LEVERAGE. A perp at 1x is still a perp, and spot at
        1x is still spot. Inferring it from `leverage > 1` once billed a perp
        book at spot rates every time the conviction ladder bottomed out."""
        from lib import product_router as PR
        import os
        with patch.dict(os.environ, {"CRYPTO_PRODUCT": "perp"}):
            for direction in ("Long", "Long_20x", "Short", "Short_10x"):
                with self.subTest(direction=direction):
                    got = P.resolve_product(
                        "BTC/USD", "Crypto",
                        signal={"paper_direction": direction, "leverage": 1.0})
                    self.assertEqual(got, PR.CRYPTO_PERP)

    def test_an_explicit_expression_beats_the_desk_default(self):
        """Product comes from the CHOSEN expression when there is one."""
        from lib import product_router as PR
        import os
        with patch.dict(os.environ, {"CRYPTO_PRODUCT": "perp"}):
            got = P.resolve_product("BTC/USD", "Crypto",
                                    signal={"product": PR.CRYPTO_SPOT})
        self.assertEqual(got, PR.CRYPTO_SPOT)

    def test_equity_resolves_to_equity_spot(self):
        from lib import product_router as PR
        self.assertEqual(P.resolve_product("AAPL", "us_equity"), PR.EQUITY_SPOT)

    def test_an_unestablished_product_is_refused_not_guessed(self):
        """A guessed product is laundered into the ledger as a measurement."""
        self.assertIsNone(P.resolve_product("ZZZZ=F", "Futures"))
        self.assertTrue(P.is_venue_data_failure(P.UNKNOWN_PRODUCT))

    def test_the_identities_reach_provenance_separately(self):
        from lib import canonical_entry as CE
        from lib import execution_snapshot as ES
        from lib import virtual_orders as VO
        snap = ES.ExecutionMarketSnapshot(
            venue="kraken", symbol="BTC/USD", product="CRYPTO_PERP",
            bid=99.9, ask=100.1, source="kraken_stream.latest_quote",
            venue_event_at="2026-08-17T00:00:00+00:00", age_ms=200.0,
            status=ES.AVAILABLE)
        ready = P.ExecutionReadiness(True, "kraken", "CRYPTO_PERP",
                                     asset_class="crypto",
                                     instrument="PF_XBTUSD", snapshot=snap)
        order = VO.VirtualOrder(symbol="BTC/USD", side="long", quantity=3.0,
                                order_type=VO.MARKET)
        ex = VO.execute_market(order, VO.Quote(bid=99.9, ask=100.1))
        doc = CE.build_provenance(signal={"id": "s1"}, ready=ready, snap=snap,
                                  execution=ex, decision_price=100.0,
                                  authorized_qty=3.0)
        self.assertEqual(doc["asset_class"], "crypto")
        self.assertEqual(doc["product"], "CRYPTO_PERP")
        self.assertEqual(doc["instrument"], "PF_XBTUSD")
        self.assertEqual(doc["venue"], "kraken")


class OnlyWhatCanBeFilledHonestlyIsFillableTests(unittest.TestCase):

    def test_crypto_with_a_fresh_kraken_quote_is_ready(self):
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": 100.0, "ask": 100.2, "at": _at(0.2)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            r = P.execution_readiness("BTC/USD", "Crypto")
        self.assertTrue(r.ok)
        self.assertEqual(r.venue, "kraken")
        self.assertEqual(r.snapshot.status, ES.AVAILABLE)

    def test_equities_fill_from_alpacas_two_sided_quote(self):
        """The gap CLOSED this pass. alpaca-py's data client exposes
        get_stock_latest_quote, which carries bid_price/ask_price/timestamp
        — a genuine executable quote, not the last price the mark chain had.
        Confirmed against the INSTALLED SDK, not from memory."""
        class _Q:
            bid_price, ask_price = 190.10, 190.14
            bid_size, ask_size = 300.0, 200.0
            timestamp = _at(0.3)

        with patch("lib.alpaca_client.get_alpaca_creds",
                   return_value=("k", "s", True)),              patch("alpaca.data.historical.StockHistoricalDataClient") as C:
            C.return_value.get_stock_latest_quote.return_value = {"AAPL": _Q()}
            r = P.execution_readiness("AAPL", "us_equity")
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.venue, "alpaca")
        self.assertEqual((r.snapshot.bid, r.snapshot.ask), (190.10, 190.14))

    def test_equities_without_credentials_refuse_rather_than_guess(self):
        with patch("lib.alpaca_client.get_alpaca_creds", return_value=("", "", True)):
            r = P.execution_readiness("AAPL", "us_equity")
        self.assertFalse(r.ok)
        self.assertEqual(r.venue, "alpaca")
        self.assertEqual(r.reason, P.EXECUTION_DATA_UNAVAILABLE)

    def test_the_equity_adapter_cannot_place_an_order(self):
        """READ-ONLY BY CONSTRUCTION. The order-capable TradingClient is a
        different class in a different module, and is not imported here —
        "we promise not to call submit_order" is not a safety property."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "execution_snapshot.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        self.assertNotIn("TradingClient", imported)
        self.assertIn("StockHistoricalDataClient", imported)

    def test_futures_have_no_simulated_venue_at_all(self):
        r = P.execution_readiness("ES=F", "Futures")
        self.assertFalse(r.ok)
        self.assertIsNone(r.venue)
        self.assertEqual(r.reason, P.UNSUPPORTED_VIRTUAL_VENUE)

    def test_forex_is_refused_too(self):
        r = P.execution_readiness("EURUSD=X", "Forex")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, P.UNSUPPORTED_VIRTUAL_VENUE)

    def test_a_yfinance_last_price_can_never_make_futures_fillable(self):
        """Even with a mark available, there is no executable quote. A last
        price answers 'what is it worth', never 'what would it fill at'."""
        with patch("jobs.paper_trading._get_current_price", return_value=5432.25):
            r = P.execution_readiness("ES=F", "Futures")
        self.assertFalse(r.ok)


class VenueFailuresAreNotThesisFailuresTests(unittest.TestCase):
    """A good signal blocked by an eight-second-stale quote is not a losing
    signal, and the learning set must be able to tell the difference."""

    def _crypto_with(self, quote):
        with patch("lib.kraken_stream.latest_quote", return_value=quote), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            return P.execution_readiness("BTC/USD", "Crypto", max_age_s=10.0)

    def test_a_stale_quote_refuses_and_says_stale(self):
        r = self._crypto_with({"bid": 100.0, "ask": 100.2, "at": _at(45.0)})
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, P.STALE_EXECUTION_DATA)

    def test_a_crossed_book_refuses_and_says_crossed(self):
        r = self._crypto_with({"bid": 101.0, "ask": 100.0, "at": _at(0.1)})
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, P.CROSSED_BOOK)

    def test_a_one_sided_book_refuses_and_says_one_sided(self):
        r = self._crypto_with({"bid": 100.0, "ask": None, "at": _at(0.1)})
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, P.ONE_SIDED_BOOK)

    def test_a_missing_quote_refuses_as_data_unavailable(self):
        r = self._crypto_with(None)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, P.EXECUTION_DATA_UNAVAILABLE)

    def test_every_refusal_here_is_classified_as_a_venue_failure(self):
        for reason in (P.UNSUPPORTED_VIRTUAL_VENUE, P.NO_EXECUTABLE_QUOTE,
                       P.EXECUTION_DATA_UNAVAILABLE, P.STALE_EXECUTION_DATA,
                       P.CROSSED_BOOK, P.ONE_SIDED_BOOK):
            with self.subTest(reason=reason):
                self.assertTrue(P.is_venue_data_failure(reason))

    def test_a_risk_rejection_is_not_a_venue_failure(self):
        """The two must not be conflated: one leaves the thesis eligible,
        the other is a verdict on the trade."""
        self.assertFalse(P.is_venue_data_failure("RISK_REJECTED"))
        self.assertFalse(P.is_venue_data_failure(None))


class TheFillableSetIsAClaimAboutWiringTests(unittest.TestCase):

    def test_the_fillable_set_is_exactly_what_has_a_quote_reader(self):
        """Adding an asset class here asserts execution_snapshot can produce
        an AVAILABLE snapshot for it. Nothing may be added silently."""
        self.assertEqual(set(P._FILLABLE_ASSET_CLASSES), {"crypto", "equity"})

    def test_every_fillable_asset_class_has_a_venue_reader(self):
        import os
        with patch.dict(os.environ, {"PAPER_VENUE": "kraken"}):
            for ac in P._FILLABLE_ASSET_CLASSES:
                sym = {"crypto": "BTC/USD", "equity": "AAPL"}[ac]
                venue, _ = P.resolve_execution_venue(sym, ac)
                with self.subTest(asset_class=ac):
                    self.assertIn(venue, ES._READERS,
                                  f"{ac} is claimed fillable but {venue} "
                                  f"has no execution-data reader")


if __name__ == "__main__":
    unittest.main()
