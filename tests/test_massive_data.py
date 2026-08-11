import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import lib.massive_data as md
from lib.massive_data import _to_massive_symbol, get_market_summary


class SymbolConversionTests(unittest.TestCase):
    def test_equity_passthrough(self):
        self.assertEqual(_to_massive_symbol("SPY"), "SPY")
        self.assertEqual(_to_massive_symbol("nvda"), "NVDA")

    def test_crypto_pair_converted(self):
        """App-native BTC/USD becomes Massive's X:BTCUSD — verified live
        (X:BTCUSD returned a real previous close)."""
        self.assertEqual(_to_massive_symbol("BTC/USD"), "X:BTCUSD")
        self.assertEqual(_to_massive_symbol("eth/usd"), "X:ETHUSD")


class GetMarketSummaryTests(unittest.TestCase):
    def setUp(self):
        md._cache.clear()

    def _agg(self, close, ts=1786000000000):
        return SimpleNamespace(open=close - 1, high=close + 1, low=close - 2,
                               close=close, volume=1e6, vwap=close, transactions=5000,
                               timestamp=ts)

    def test_unconfigured_returns_none(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MASSIVE_API_KEY", None)
            self.assertIsNone(get_market_summary("SPY"))

    @patch.dict("os.environ", {"MASSIVE_API_KEY": "k" * 32})
    @patch("massive.RESTClient")
    def test_summary_shape_and_change_pct(self, client_cls):
        client = client_cls.return_value
        client.get_previous_close_agg.return_value = [
            SimpleNamespace(close=773.03, open=772.6, high=775.05, low=771.62,
                            volume=39_249_478.0, vwap=773.24)
        ]
        client.list_aggs.return_value = [self._agg(757.67), self._agg(773.26), self._agg(773.03)]
        s = get_market_summary("SPY")
        self.assertEqual(s["provider"], "massive")
        self.assertEqual(s["previous_close"]["close"], 773.03)
        self.assertEqual(len(s["daily_bars"]), 3)
        self.assertEqual(s["daily_bars"][0]["transactions"], 5000)
        # change = (773.03 - 773.26)/773.26
        self.assertAlmostEqual(s["last_close_change_pct"], -0.03, places=2)
        self.assertIn("not live quotes", s["note"])

    @patch.dict("os.environ", {"MASSIVE_API_KEY": "k" * 32})
    @patch("massive.RESTClient")
    def test_cached_second_call_makes_no_api_calls(self, client_cls):
        client = client_cls.return_value
        client.get_previous_close_agg.return_value = []
        client.list_aggs.return_value = [self._agg(100.0)]
        first = get_market_summary("AAPL")
        self.assertIsNotNone(first)
        calls_before = client.list_aggs.call_count
        second = get_market_summary("AAPL")
        self.assertEqual(client.list_aggs.call_count, calls_before)  # served from cache
        self.assertEqual(first, second)

    @patch.dict("os.environ", {"MASSIVE_API_KEY": "k" * 32})
    @patch("massive.RESTClient")
    def test_api_failure_returns_none_not_exception(self, client_cls):
        client_cls.return_value.get_previous_close_agg.side_effect = RuntimeError("rate limited")
        self.assertIsNone(get_market_summary("SPY"))

    @patch.dict("os.environ", {"MASSIVE_API_KEY": "k" * 32})
    @patch("massive.RESTClient")
    def test_empty_results_return_none(self, client_cls):
        client = client_cls.return_value
        client.get_previous_close_agg.return_value = []
        client.list_aggs.return_value = []
        self.assertIsNone(get_market_summary("ZZZQ"))


if __name__ == "__main__":
    unittest.main()
