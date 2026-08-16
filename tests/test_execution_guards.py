"""Entry guards, written from a live production failure.

2026-08-13: a LINK/USD entry was wash-trade-rejected against order
c5d68b81 — the stop-loss protecting an open 113-LINK position. The
pre-entry sweep was supposed to clear orphans but filtered orders by
'LINKUSD' while Alpaca order objects carry 'LINK/USD' (positions come
back as 'LINKUSD' — the broker is inconsistent about the slash, verified
live). Had the filter matched, the sweep would have CANCELLED a live
position's stop and pyramided it unprotected.

Meanwhile SUI/USD burned a rejected API call every cycle because nothing
checked Alpaca's 73-asset crypto listing first.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from lib.alpaca_client import _symbol_variants, submit_bracket_order, _tradable_cache


class SymbolVariantTests(unittest.TestCase):
    def test_both_broker_formats_are_produced(self):
        self.assertEqual(set(_symbol_variants("LINK/USD")), {"LINK/USD", "LINKUSD"})

    def test_slashless_input_yields_both_forms(self):
        """Phase 1 registry upgrade: the old helper returned only
        {"LINKUSD"} for slashless input, so an order sweep fed a
        position-style symbol could never match order-style "LINK/USD" —
        the exact gap behind the wash-trade incident. The identity
        registry produces every venue spelling from either input."""
        self.assertEqual(set(_symbol_variants("LINKUSD")), {"LINKUSD", "LINK/USD"})


class EntryGuardTests(unittest.TestCase):
    def setUp(self):
        _tradable_cache["syms"] = {"LINK/USD", "BTC/USD", "ETH/USD"}
        _tradable_cache["at"] = 9e12    # never expires during the test
        # These exercise the LIVE executor's own guards — duplicate
        # positions, unlisted symbols, orphan sweeps — so the platform
        # mode must permit reaching them. The VIRTUAL_ONLY boundary is
        # tested separately in test_platform_mode.py; here it would just
        # short-circuit the code under test.
        self._mode = os.environ.get("JARVIS_PLATFORM_MODE")
        os.environ["JARVIS_PLATFORM_MODE"] = "LIVE_ENABLED"

    def tearDown(self):
        _tradable_cache["syms"] = None
        _tradable_cache["at"] = 0.0
        if self._mode is None:
            os.environ.pop("JARVIS_PLATFORM_MODE", None)
        else:
            os.environ["JARVIS_PLATFORM_MODE"] = self._mode

    def test_an_unlisted_symbol_is_refused_before_any_submit(self):
        with patch("lib.alpaca_client.get_trading_client") as gtc:
            with self.assertRaises(ValueError) as ctx:
                submit_bracket_order("SUI/USD", 10, 1.0, 1.2, 0.9)
            self.assertIn("not listed", str(ctx.exception))
            gtc.return_value.submit_order.assert_not_called()

    def test_an_open_position_refuses_the_entry_and_touches_nothing(self):
        """The LINK case: position exists (slashless symbol), protective
        sell rests. The entry must refuse — not cancel, not pyramid."""
        client = MagicMock()
        pos = MagicMock()
        pos.qty = "113.095238759"
        # position lookups answer on the slashless variant only, like Alpaca
        def get_pos(v):
            if v == "LINKUSD":
                return pos
            raise Exception("no position")
        client.get_open_position.side_effect = get_pos
        with patch("lib.alpaca_client.get_trading_client", return_value=client):
            with self.assertRaises(ValueError) as ctx:
                submit_bracket_order("LINK/USD", 5, 14.0, 16.0, 13.0)
        msg = str(ctx.exception)
        self.assertIn("open position", msg)
        self.assertIn("refusing", msg)
        client.cancel_order_by_id.assert_not_called()
        client.submit_order.assert_not_called()

    def test_true_orphans_are_swept_with_both_symbol_formats(self):
        """No position -> resting sells are leftovers; the sweep must query
        with BOTH formats or it matches nothing (the original bug)."""
        client = MagicMock()
        client.get_open_position.side_effect = Exception("no position")
        orphan = MagicMock(); orphan.id = "orphan-1"
        client.get_orders.return_value = [orphan]
        entry = MagicMock(); entry.id = "e-1"; entry.status = "accepted"; entry.side = "buy"
        entry.filled_qty = "5"
        client.submit_order.return_value = entry
        client.get_order_by_id.return_value = entry
        with patch("lib.alpaca_client.get_trading_client", return_value=client):
            submit_bracket_order("LINK/USD", 5, 14.0, 16.0, 13.0)
        req = client.get_orders.call_args[0][0]
        self.assertEqual(set(req.symbols), {"LINK/USD", "LINKUSD"})
        client.cancel_order_by_id.assert_called_once_with("orphan-1")

    def test_unknown_listing_never_rejects(self):
        """A failed asset-list lookup means 'unknown' — refusing trades on
        unknown would turn an Alpaca hiccup into a trading halt."""
        _tradable_cache["syms"] = None
        _tradable_cache["at"] = 0.0
        client = MagicMock()
        client.get_all_assets.side_effect = Exception("api down")
        client.get_open_position.side_effect = Exception("no position")
        client.get_orders.return_value = []
        entry = MagicMock(); entry.id = "e"; entry.status = "accepted"; entry.side = "buy"
        entry.filled_qty = "5"
        client.submit_order.return_value = entry
        client.get_order_by_id.return_value = entry
        with patch("lib.alpaca_client.get_trading_client", return_value=client):
            submit_bracket_order("BTC/USD", 1, 60000, 66000, 57000)
        client.submit_order.assert_called()


if __name__ == "__main__":
    unittest.main()
