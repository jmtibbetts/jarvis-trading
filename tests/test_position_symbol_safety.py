import unittest
from types import SimpleNamespace

from jobs.manage_positions import (
    _exit_levels_are_sane,
    _open_exit_orders,
    _order_is_filled,
    _pending_market_exit,
    _position_is_crypto,
    _replace_or_submit_stop,
    _safe_qty,
    _sym_variants,
)
from lib.signal_levels import validate_signal_levels


class PositionSymbolSafetyTests(unittest.TestCase):
    def test_btc_and_eth_equity_tickers_are_not_crypto_positions(self):
        btc_etf = SimpleNamespace(symbol="BTC", asset_class="AssetClass.US_EQUITY")
        eth_coin = SimpleNamespace(symbol="ETHUSD", asset_class="AssetClass.CRYPTO")
        self.assertFalse(_position_is_crypto(btc_etf))
        self.assertTrue(_position_is_crypto(eth_coin))

    def test_equity_lookup_does_not_expand_to_crypto_pair(self):
        self.assertEqual(_sym_variants("BTC", is_crypto=False), ["BTC"])
        self.assertEqual(set(_sym_variants("BTCUSD", is_crypto=True)), {"BTCUSD", "BTC/USD"})

    def test_fractional_equity_quantity_is_never_rounded_up(self):
        self.assertEqual(_safe_qty(0.0918, is_crypto=False), 0)
        self.assertEqual(_safe_qty(24, is_crypto=False), 24)

    def test_nested_bracket_legs_are_discovered(self):
        stop = SimpleNamespace(symbol="RTX", side="sell", order_type="stop", stop_price=90, limit_price=None, legs=[])
        target = SimpleNamespace(symbol="RTX", side="sell", order_type="limit", stop_price=None, limit_price=120, legs=[])
        parent = SimpleNamespace(symbol="RTX", side="buy", order_type="limit", stop_price=None, limit_price=100,
                                 legs=[stop, target])
        client = SimpleNamespace(get_orders=lambda request: [parent])
        self.assertEqual(_open_exit_orders(client, "RTX", "sell"), (stop, target))

    def test_existing_trailing_stop_is_retained(self):
        existing = SimpleNamespace(order_type="trailing_stop", stop_price=None)
        client = SimpleNamespace(
            replace_order_by_id=lambda *args, **kwargs: self.fail("should not replace trailing stop")
        )
        self.assertTrue(
            _replace_or_submit_stop(client, "SPY", 3, "sell", 700, existing=existing)
        )

    def test_existing_order_holding_quantity_is_not_replaced_with_duplicate(self):
        existing = SimpleNamespace(id="stop-1", order_type="stop", stop_price=90)

        def reject_replace(*args, **kwargs):
            raise RuntimeError("insufficient qty available for order; held_for_orders=24")

        client = SimpleNamespace(replace_order_by_id=reject_replace)
        self.assertTrue(
            _replace_or_submit_stop(client, "RTX", 24, "sell", 95, existing=existing)
        )

    def test_absurd_cross_asset_exit_levels_are_rejected(self):
        self.assertFalse(_exit_levels_are_sane(28, "long", 16.7, 1877.28))
        self.assertTrue(_exit_levels_are_sane(1909, "short", 1920, 1885, is_crypto=True))

    def test_signal_gate_rejects_reported_btc_and_eth_levels(self):
        btc = {"direction": "Short", "timeframe": "5m", "entry_price": 64915.1,
               "target_price": 1877.28, "stop_loss": 16.7}
        eth = {"direction": "Short", "timeframe": "5m", "entry_price": 1909.41,
               "target_price": 110.3, "stop_loss": 0}
        self.assertFalse(validate_signal_levels(btc)[0])
        self.assertFalse(validate_signal_levels(eth)[0])

    def test_pending_market_exit_detects_only_matching_market_side(self):
        market_exit = SimpleNamespace(
            id="exit-1", side="OrderSide.SELL", type="OrderType.MARKET", legs=[]
        )
        stop = SimpleNamespace(
            id="stop-1", side="OrderSide.SELL", type="OrderType.STOP", legs=[]
        )
        client = SimpleNamespace(get_orders=lambda request: [stop, market_exit])

        self.assertIs(_pending_market_exit(client, "AAPL", "sell"), market_exit)
        self.assertIsNone(_pending_market_exit(client, "AAPL", "buy"))

    def test_order_fill_requires_filled_not_partially_filled(self):
        self.assertTrue(_order_is_filled(None))
        self.assertTrue(_order_is_filled(SimpleNamespace(status="OrderStatus.FILLED")))
        self.assertFalse(_order_is_filled(
            SimpleNamespace(status="OrderStatus.PARTIALLY_FILLED")
        ))


if __name__ == "__main__":
    unittest.main()


class CryptoOrderSymbolFormatTests(unittest.TestCase):
    """Alpaca stores crypto POSITIONS as 'ARBUSD' and crypto ORDERS as
    'ARB/USD'. The lookup must bridge that, or the sweep believes every
    crypto position is unprotected and submits duplicate stops."""

    def _client(self, order_symbol):
        stop = SimpleNamespace(symbol=order_symbol, side="sell", order_type="stop",
                               stop_price=0.07, limit_price=None, legs=[])
        return SimpleNamespace(get_orders=lambda request: [stop]), stop

    def test_slashed_order_matches_unslashed_position(self):
        client, stop = self._client("ARB/USD")
        found_stop, _ = _open_exit_orders(client, "ARBUSD", "sell")
        self.assertIs(found_stop, stop)

    def test_unslashed_order_still_matches(self):
        client, stop = self._client("ARBUSD")
        found_stop, _ = _open_exit_orders(client, "ARBUSD", "sell")
        self.assertIs(found_stop, stop)

    def test_other_symbols_are_not_matched(self):
        client, _ = self._client("ETH/USD")
        self.assertEqual(_open_exit_orders(client, "ARBUSD", "sell"), (None, None))

    def test_malformed_order_does_not_blind_the_lookup(self):
        good = SimpleNamespace(symbol="ARB/USD", side="sell", order_type="stop",
                               stop_price=0.07, limit_price=None, legs=[])
        broken = SimpleNamespace(side="sell", order_type="stop")  # no symbol at all
        client = SimpleNamespace(get_orders=lambda request: [broken, good])
        found_stop, _ = _open_exit_orders(client, "ARBUSD", "sell")
        self.assertIs(found_stop, good)


class ReplacePriceOnlyTests(unittest.TestCase):
    """ReplaceOrderRequest.qty is an int in the SDK, so passing a fractional
    crypto quantity fails validation. Re-pricing must omit qty entirely."""

    def test_fractional_qty_would_have_failed_validation(self):
        from alpaca.trading.requests import ReplaceOrderRequest
        with self.assertRaises(Exception):
            ReplaceOrderRequest(qty=118.894856902, stop_price=8.5)

    def test_price_only_replace_omits_qty(self):
        from jobs.manage_positions import _replace_price_only
        captured = {}

        class _C:
            def replace_order_by_id(self, oid, req):
                captured["id"] = oid
                captured["req"] = req
                return req

        _replace_price_only(_C(), "abc", stop_price=8.512345678901)
        self.assertEqual(captured["id"], "abc")
        self.assertIsNone(captured["req"].qty)
        # 8 SIGNIFICANT figures, not 8 decimals - see _round_price
        self.assertEqual(captured["req"].stop_price, 8.5123457)

    def test_price_only_replace_handles_targets(self):
        from jobs.manage_positions import _replace_price_only
        captured = {}

        class _C:
            def replace_order_by_id(self, oid, req):
                captured["req"] = req
                return req

        _replace_price_only(_C(), "abc", limit_price=12.5)
        self.assertIsNone(captured["req"].qty)
        self.assertEqual(captured["req"].limit_price, 12.5)


class PriceRoundingTests(unittest.TestCase):
    """Prices must round by significant figures. A flat 8-decimal round
    turns 0.0000000123 into 0.00000001 - an 18.7% error that would place a
    stop nowhere near where it was calculated."""

    def test_cheap_coins_keep_their_precision(self):
        from jobs.manage_positions import _round_price
        for px in (1.23e-08, 8.91e-06, 3.9e-05, 0.0001234567):
            out = _round_price(px)
            self.assertLess(abs(out - px) / px * 100, 0.01, f"lossy for {px}")

    def test_dollar_prices_are_sane(self):
        from jobs.manage_positions import _round_price
        self.assertEqual(_round_price(12991.9), 12991.9)
        self.assertAlmostEqual(_round_price(8.512345678), 8.5123457, places=7)

    def test_never_exceeds_twelve_decimals(self):
        from jobs.manage_positions import _round_price
        out = _round_price(1.23456789e-15)
        self.assertEqual(out, round(out, 12))

    def test_zero_and_negative_pass_through(self):
        from jobs.manage_positions import _round_price
        self.assertEqual(_round_price(0), 0)
        self.assertEqual(_round_price(-5), -5)
