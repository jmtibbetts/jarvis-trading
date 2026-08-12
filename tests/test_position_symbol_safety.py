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
