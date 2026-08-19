"""`POST /api/paper/open` is the operator's manual entry. It must not be a
second economy, and it must not turn missing market state into a 500.

TWO DEFECTS THIS PINS.

1. IT USED THE LEGACY OPENER. `open_paper_position`'s own docstring says
   "whatever it is handed becomes the fill ... precisely the mark-as-fill
   behaviour `lib/canonical_entry` exists to replace." In the canonical
   epoch that silently creates positions with no settlement header — a
   second book, priced by whoever called the API, inside the book that is
   supposed to be canonical.

2. IT 500'd ON A MISSING PRICE. `market_assets` crosses the cutover without
   its transient market columns by design, so `a.price` is NULL on a fresh
   book and `float(a.price)` raised TypeError. A missing reference price is
   an ordinary, expected state — not a server fault. And it must never
   quietly become 0.0, because zero is a PRICE and missing is not.
"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException


def _body(**over):
    from app.routers.common import PaperOpenRequest
    base = {"symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_000.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "signal_id": "manual-test"}
    base.update(over)
    return PaperOpenRequest(**base)


class MissingMarketPriceIsNotAServerFaultTests(unittest.TestCase):

    def _seed_asset(self, price):
        from app.database import MarketAsset, get_db, new_id
        with get_db() as db:
            db.query(MarketAsset).filter(
                MarketAsset.symbol == "NULLPX/USD").delete()
            db.add(MarketAsset(id=new_id(), symbol="NULLPX/USD",
                               name="Null Price Asset", asset_class="Crypto",
                               price=price))
            db.commit()

    def _open(self, **over):
        from app.routers.trading import paper_open
        return paper_open(_body(symbol="NULLPX/USD", entry_price=None, **over))

    def test_a_null_stored_price_is_a_named_refusal_not_a_500(self):
        self._seed_asset(None)
        with self.assertRaises(HTTPException) as caught:
            self._open()
        self.assertEqual(caught.exception.status_code, 400,
                         "a missing reference price became a server error")
        self.assertIn("MARKET_PRICE_UNAVAILABLE", str(caught.exception.detail))

    def test_a_zero_stored_price_is_refused_not_used(self):
        """Zero is a PRICE. Missing is not. `float(a.price or 0)` would
        conflate them and size a position against nothing."""
        self._seed_asset(0.0)
        with self.assertRaises(HTTPException) as caught:
            self._open()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("MARKET_PRICE_UNAVAILABLE", str(caught.exception.detail))

    def test_a_non_finite_stored_price_is_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(price=bad):
                self._seed_asset(bad)
                with self.assertRaises(HTTPException) as caught:
                    self._open()
                self.assertEqual(caught.exception.status_code, 400)

    def test_a_negative_stored_price_is_refused(self):
        self._seed_asset(-10.0)
        with self.assertRaises(HTTPException) as caught:
            self._open()
        self.assertEqual(caught.exception.status_code, 400)

    def test_an_absent_asset_row_is_a_named_refusal(self):
        from app.database import MarketAsset, get_db
        with get_db() as db:
            db.query(MarketAsset).filter(
                MarketAsset.symbol == "NOSUCH/USD").delete()
            db.commit()
        from app.routers.trading import paper_open
        with self.assertRaises(HTTPException) as caught:
            paper_open(_body(symbol="NOSUCH/USD", entry_price=None))
        self.assertEqual(caught.exception.status_code, 400)

    def test_an_invalid_caller_price_is_refused(self):
        from app.routers.trading import paper_open
        for bad in (0.0, -5.0, float("nan"), float("inf")):
            with self.subTest(price=bad):
                with self.assertRaises(HTTPException) as caught:
                    paper_open(_body(entry_price=bad))
                self.assertEqual(caught.exception.status_code, 400)


class TheManualOpenRoutesCanonicallyTests(unittest.TestCase):
    """The caller's price is a DECISION reference. The fill comes from the
    execution market, through the same door the automated loop uses."""

    def test_it_calls_the_canonical_opener(self):
        from app.routers.trading import paper_open
        from lib import canonical_entry as CE
        seen = {}

        def spy(signal, **kw):
            seen["signal"] = signal
            seen["kw"] = kw
            return {"ok": False, "error": "STOPPED_IN_TEST"}

        with patch.object(CE, "open_canonical_position", spy):
            try:
                paper_open(_body())
            except HTTPException:
                pass
        self.assertIn("signal", seen,
                      "the manual open did not reach canonical entry")
        self.assertEqual(seen["kw"].get("decision_price"), 64_000.0,
                         "the caller price must arrive as a DECISION price")

    def test_the_legacy_mark_as_fill_opener_is_not_used(self):
        """A poison on the legacy opener. If the endpoint still routes
        there, this fires."""
        from app.routers.trading import paper_open
        from lib import canonical_entry as CE
        from lib import paper_engine as PE

        def poison(*a, **k):
            raise AssertionError("the manual open used the legacy "
                                 "mark-as-fill opener")

        with patch.object(PE, "open_paper_position", poison), \
             patch.object(CE, "open_canonical_position",
                          lambda *a, **k: {"ok": True, "position_id": "x"}):
            paper_open(_body())

    def test_the_control_the_poison_is_live(self):
        from lib import paper_engine as PE

        def poison(*a, **k):
            raise AssertionError("poison reached")
        with patch.object(PE, "open_paper_position", poison):
            with self.assertRaises(AssertionError):
                PE.open_paper_position({}, current_price=1.0)


if __name__ == "__main__":
    unittest.main()
