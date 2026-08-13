"""Read-only Kraken reconciliation: real fills become ground truth.

The execution model starved while real fills — with real prices and real
fees — accumulated unseen in the operator's Kraken trades history. This
sync pulls them in idempotently. Read-only is a load-bearing property:
nothing in kraken_sync or kraken_account may ever place, amend, or cancel
an order, and a test pins that.
"""
import unittest
import uuid
from unittest.mock import patch

from lib import kraken_sync
from lib.kraken_account import trades_history


def _fake_history_page(trades, total=None):
    return {"ok": True, "trades": trades, "total": total or len(trades)}


def _tid(label):
    """Unique per test run. Fixed ids leaked into the dev DB on an earlier
    run and made the idempotency test fail against its own leftovers —
    these tests hit the real get_db, so they must clean up after themselves
    and never reuse an id."""
    return f"TEST-{label}-{uuid.uuid4().hex[:8]}"


def _trade(tid, ts, pair="XXBTZUSD", price=63000.0, vol=0.01, fee=2.5):
    return {"trade_id": tid, "order_id": f"O-{tid}", "pair": pair,
            "side": "buy", "order_type": "limit", "price": price,
            "cost": price * vol, "fee": fee, "volume": vol,
            "margin": 0.0, "executed_at": ts}


class SyncIdempotencyTests(unittest.TestCase):
    def tearDown(self):
        from app.database import engine
        from sqlalchemy import text
        with engine.begin() as c:
            c.execute(text("DELETE FROM kraken_trades WHERE trade_id LIKE 'TEST-%'"))

    def _sync(self, pages):
        """Run sync_trades against a canned page sequence, in-memory DB."""
        calls = []

        def fake_history(start=None, offset=0):
            calls.append((start, offset))
            idx = offset // 50
            return _fake_history_page(pages[idx] if idx < len(pages) else [])

        with patch("lib.kraken_account.trades_history", side_effect=fake_history), \
             patch("lib.kraken_sync.trades_history", create=True), \
             patch("lib.kraken_account.is_configured", return_value=True):
            # kraken_sync imports inside the function, so patch the source module
            with patch("lib.kraken_sync._newest_stored_ts", return_value=0.0):
                return kraken_sync.sync_trades(), calls

    def test_new_fills_are_inserted_once(self):
        pages = [[_trade(_tid("a"), 1000.0), _trade(_tid("b"), 1001.0)]]
        r, _ = self._sync(pages)
        self.assertTrue(r["ok"])
        self.assertEqual(r["inserted"], 2)

    def test_resyncing_the_same_window_inserts_nothing(self):
        pages = [[_trade(_tid("a"), 1000.0), _trade(_tid("b"), 1001.0)]]
        first, _ = self._sync(pages)
        second, _ = self._sync(pages)
        self.assertEqual(second["inserted"], 0,
                         "same trade ids must not insert twice")
        self.assertEqual(second["scanned"], 2)

    def test_unconfigured_credentials_fail_closed(self):
        with patch("lib.kraken_account.is_configured", return_value=False):
            r = kraken_sync.sync_trades()
        self.assertFalse(r["ok"])
        self.assertIn("not configured", r["reason"])


class PaginationParsingTests(unittest.TestCase):
    def test_pages_are_sorted_oldest_first(self):
        raw = {"ok": True, "result": {"count": 2, "trades": {
            "TB": {"ordertxid": "O2", "pair": "XETHZUSD", "type": "sell",
                   "ordertype": "market", "price": "1900", "cost": "190",
                   "fee": "0.3", "vol": "0.1", "margin": "0", "time": 2000.0},
            "TA": {"ordertxid": "O1", "pair": "XETHZUSD", "type": "buy",
                   "ordertype": "limit", "price": "1880", "cost": "188",
                   "fee": "0.3", "vol": "0.1", "margin": "0", "time": 1000.0},
        }}}
        with patch("lib.kraken_account._private", return_value=raw):
            out = trades_history()
        self.assertTrue(out["ok"])
        self.assertEqual([t["trade_id"] for t in out["trades"]], ["TA", "TB"])
        self.assertEqual(out["trades"][0]["price"], 1880.0)


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """No code path in the sync modules may place, amend, or cancel an
    order. This is the property that makes 'read-only reconciliation'
    true rather than aspirational — trading would need a separately keyed,
    explicitly decided integration."""

    def test_no_order_endpoints_are_referenced(self):
        import inspect

        from lib import kraken_account
        src = inspect.getsource(kraken_account) + inspect.getsource(kraken_sync)
        for forbidden in ("AddOrder", "CancelOrder", "EditOrder", "AmendOrder",
                          "CancelAll", "WithdrawFunds", "Withdraw"):
            self.assertNotIn(forbidden, src,
                             f"read-only module references {forbidden}")


if __name__ == "__main__":
    unittest.main()
