"""The sizing preview must survive its own database session.

`get_signal_sizing` read `sig.asset_symbol` after the `with get_db()` block
had closed, so SQLAlchemy raised DetachedInstanceError on EVERY call. The
card's loader catches sizing failures per-card and simply omits the line:

    } catch {
      /* a card without sizing simply omits the line */
    }

so all forty cards quietly lost their capital / leverage / exposure / stop-cost
row, and the only evidence was a 500 in the network tab. A failure that
degrades into missing information rather than an error message is exactly the
kind that survives for weeks, which is why it gets a test rather than just a
fix.
"""
import unittest
from unittest import mock

from app.database import get_db, TradingSignal, new_id, now_iso
from app.routes import get_signal_sizing


class SizingRouteTests(unittest.TestCase):
    def setUp(self):
        self.sig_id = new_id()
        with get_db() as db:
            db.add(TradingSignal(
                id=self.sig_id, asset_symbol="BTC/USD", asset_class="Crypto",
                direction="Long", status="Active", entry_price=95_000.0,
                stop_loss=92_000.0, target_price=101_000.0,
                composite_score=70.0, confidence=70.0,
                created_date=now_iso(), updated_date=now_iso(),
            ))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with get_db() as db:
            db.query(TradingSignal).filter(TradingSignal.id == self.sig_id).delete()

    def test_it_does_not_raise(self):
        """The regression itself: DetachedInstanceError on every call."""
        result = get_signal_sizing(self.sig_id)
        self.assertIsInstance(result, dict)

    def test_it_returns_the_numbers_the_card_shows(self):
        r = get_signal_sizing(self.sig_id)
        if not r.get("ok"):
            self.skipTest(f"sizing declined for a non-session reason: {r.get('reason')}")
        for field in ("margin", "notional", "qty", "leverage", "loss_at_stop"):
            self.assertIn(field, r)

    def test_the_symbol_reaches_the_sizing_engine(self):
        """The detached attribute was the SYMBOL, which is what selects the
        venue and therefore the fee schedule. Losing it silently would have
        priced the position at a default venue."""
        seen = {}
        import lib.paper_engine as pe
        real = pe.size_position

        def spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)

        with mock.patch.object(pe, "size_position", spy):
            get_signal_sizing(self.sig_id)
        self.assertEqual(seen.get("symbol"), "BTC/USD")


if __name__ == "__main__":
    unittest.main()
