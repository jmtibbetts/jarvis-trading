import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lib.options_analytics import (
    compute_expected_move, get_chain_summary, parse_occ_symbol,
    snapshot_to_row, summarize_chain,
)


class ParseOccSymbolTests(unittest.TestCase):
    def test_parses_real_call_symbol(self):
        """Verified live against Alpaca's real options chain while building
        this: AAPL, exp 2026-08-10, Call, strike $255.00."""
        result = parse_occ_symbol("AAPL260810C00255000")
        self.assertEqual(result, {"root": "AAPL", "expiration": "2026-08-10", "type": "call", "strike": 255.0})

    def test_parses_real_put_symbol(self):
        result = parse_occ_symbol("SPY260811P00702000")
        self.assertEqual(result["type"], "put")
        self.assertEqual(result["strike"], 702.0)

    def test_handles_fractional_strike(self):
        result = parse_occ_symbol("SPY260810C00754500")
        self.assertEqual(result["strike"], 754.5)

    def test_malformed_symbol_returns_none(self):
        self.assertIsNone(parse_occ_symbol("NOT-A-VALID-SYMBOL"))
        self.assertIsNone(parse_occ_symbol(""))
        self.assertIsNone(parse_occ_symbol("AAPL"))


class SnapshotToRowTests(unittest.TestCase):
    def _snapshot(self, bid=None, ask=None, trade_price=None, trade_size=None, iv=None, delta=None):
        quote = SimpleNamespace(bid_price=bid, ask_price=ask) if bid is not None or ask is not None else None
        trade = SimpleNamespace(price=trade_price, size=trade_size) if trade_price is not None else None
        greeks = SimpleNamespace(delta=delta, gamma=0.01, theta=-0.5, vega=0.02) if delta is not None else None
        return SimpleNamespace(latest_quote=quote, latest_trade=trade, implied_volatility=iv, greeks=greeks)

    def test_flattens_real_shaped_snapshot(self):
        snap = self._snapshot(bid=18.99, ask=19.24, trade_price=18.82, trade_size=2.0, iv=0.8357, delta=0.987)
        row = snapshot_to_row("SPY260811C00702000", snap)
        self.assertEqual(row["strike"], 702.0)
        self.assertEqual(row["type"], "call")
        self.assertAlmostEqual(row["mid"], 19.115)
        self.assertTrue(row["has_recent_trade"])
        self.assertEqual(row["implied_volatility"], 0.8357)
        self.assertEqual(row["delta"], 0.987)

    def test_no_trade_and_no_greeks_handled_gracefully(self):
        """Real observed case: some contracts return latest_trade=None and
        greeks=None (no recent activity) — must not crash on missing data."""
        snap = self._snapshot(bid=0.0, ask=0.03)
        row = snapshot_to_row("AAPL260810P00255000", snap)
        self.assertFalse(row["has_recent_trade"])
        self.assertIsNone(row["delta"])
        self.assertIsNone(row["implied_volatility"])

    def test_zero_bid_and_ask_gives_no_mid(self):
        snap = self._snapshot(bid=0.0, ask=0.0)
        row = snapshot_to_row("AAPL260810P00100000", snap)
        self.assertIsNone(row["mid"])

    def test_invalid_symbol_returns_none(self):
        self.assertIsNone(snapshot_to_row("GARBAGE", self._snapshot()))


def _row(symbol, type_, strike, expiration="2026-08-10", mid=1.0, iv=0.3, delta=0.5, gamma=0.01, has_trade=True):
    return {
        "symbol": symbol, "root": symbol[:4], "expiration": expiration, "type": type_, "strike": strike,
        "bid": mid - 0.05, "ask": mid + 0.05, "mid": mid,
        "last_trade_price": mid, "last_trade_size": 1.0, "has_recent_trade": has_trade,
        "implied_volatility": iv, "delta": delta if type_ == "call" else -delta, "gamma": gamma, "theta": -0.1, "vega": 0.02,
    }


class SummarizeChainTests(unittest.TestCase):
    def test_put_call_ratio_from_traded_contracts_only(self):
        rows = [
            _row("A", "call", 100, has_trade=True), _row("B", "call", 105, has_trade=False),
            _row("C", "put", 95, has_trade=True), _row("D", "put", 90, has_trade=True),
        ]
        summary = summarize_chain(rows)
        self.assertEqual(summary["traded_call_count"], 1)
        self.assertEqual(summary["traded_put_count"], 2)
        self.assertEqual(summary["put_call_ratio"], 2.0)

    def test_no_traded_calls_gives_none_ratio_not_division_error(self):
        rows = [_row("A", "call", 100, has_trade=False), _row("C", "put", 95, has_trade=True)]
        summary = summarize_chain(rows)
        self.assertIsNone(summary["put_call_ratio"])

    def test_iv_skew_positive_when_puts_richer(self):
        """Put IV > call IV (the common 'crash insurance premium' shape) ->
        positive skew. This is a real, standard options-market observation,
        not something this module invents."""
        rows = [_row("A", "call", 100, iv=0.20), _row("B", "put", 100, iv=0.28)]
        summary = summarize_chain(rows)
        self.assertAlmostEqual(summary["iv_skew"], 0.08, places=4)

    def test_missing_iv_excluded_from_average_not_treated_as_zero(self):
        rows = [_row("A", "call", 100, iv=0.20), _row("B", "call", 105, iv=None)]
        summary = summarize_chain(rows)
        self.assertEqual(summary["avg_call_iv"], 0.20)  # not (0.20+0)/2

    def test_delta_summed_by_side(self):
        rows = [_row("A", "call", 100, delta=0.5), _row("B", "call", 105, delta=0.3), _row("C", "put", 95, delta=0.4)]
        summary = summarize_chain(rows)
        self.assertAlmostEqual(summary["total_call_delta"], 0.8)
        self.assertAlmostEqual(summary["total_put_delta"], -0.4)  # puts stored as negative delta, matches real Alpaca convention

    def test_top_iv_contracts_sorted_descending(self):
        rows = [_row("A", "call", 100, iv=0.20), _row("B", "call", 105, iv=0.90), _row("C", "put", 95, iv=0.50)]
        summary = summarize_chain(rows)
        self.assertEqual(summary["top_iv_contracts"][0]["symbol"], "B")

    def test_empty_chain_returns_sane_defaults_not_exception(self):
        summary = summarize_chain([])
        self.assertEqual(summary["contracts_analyzed"], 0)
        self.assertIsNone(summary["put_call_ratio"])
        self.assertIsNone(summary["nearest_expiration"])


class ComputeExpectedMoveTests(unittest.TestCase):
    def test_computes_straddle_based_expected_move(self):
        """Standard ATM-straddle expected-move calculation: call + put
        prices at the strike nearest spot sum to the market's implied
        move for that expiration."""
        rows = [
            _row("C1", "call", 100, expiration="2026-09-01", mid=3.0),
            _row("P1", "put", 100, expiration="2026-09-01", mid=2.5),
            _row("C2", "call", 105, expiration="2026-09-01", mid=1.0),  # not ATM, should be ignored
        ]
        result = compute_expected_move(rows, current_price=101.0, expiration="2026-09-01")
        self.assertEqual(result["strike"], 100)
        self.assertEqual(result["straddle_price"], 5.5)
        self.assertAlmostEqual(result["expected_move_pct"], 5.5 / 101.0 * 100, places=2)

    def test_picks_strike_closest_to_current_price(self):
        rows = [
            _row("C1", "call", 90, expiration="2026-09-01", mid=1.0), _row("P1", "put", 90, expiration="2026-09-01", mid=1.0),
            _row("C2", "call", 110, expiration="2026-09-01", mid=1.0), _row("P2", "put", 110, expiration="2026-09-01", mid=1.0),
        ]
        result = compute_expected_move(rows, current_price=95.0, expiration="2026-09-01")
        self.assertEqual(result["strike"], 90)

    def test_missing_call_or_put_at_atm_strike_returns_none(self):
        rows = [_row("C1", "call", 100, expiration="2026-09-01", mid=1.0)]  # no matching put
        self.assertIsNone(compute_expected_move(rows, current_price=100.0, expiration="2026-09-01"))

    def test_no_rows_for_expiration_returns_none(self):
        rows = [_row("C1", "call", 100, expiration="2026-08-01", mid=1.0)]
        self.assertIsNone(compute_expected_move(rows, current_price=100.0, expiration="2026-09-01"))


class GetChainSummaryTests(unittest.TestCase):
    def test_returns_none_on_fetch_failure_not_exception(self):
        """If the account lacks options data entitlement, or Alpaca is
        unreachable, this must degrade gracefully — not crash whatever
        called it (e.g. the Signal Analysis Modal)."""
        with patch("lib.alpaca_client.get_option_data_client", side_effect=Exception("no entitlement")):
            result = get_chain_summary("AAPL", current_price=200.0)
        self.assertIsNone(result)

    def test_empty_chain_returns_none(self):
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = {}
        with patch("lib.alpaca_client.get_option_data_client", return_value=mock_client):
            result = get_chain_summary("AAPL", current_price=200.0)
        self.assertIsNone(result)

    def test_real_shaped_chain_produces_full_summary(self):
        """Two contracts shaped exactly like the real live Alpaca response
        captured while building this (AAPL/SPY snapshots)."""
        call_snap = SimpleNamespace(
            latest_quote=SimpleNamespace(bid_price=18.99, ask_price=19.24),
            latest_trade=SimpleNamespace(price=18.82, size=2.0),
            implied_volatility=0.35, greeks=SimpleNamespace(delta=0.55, gamma=0.02, theta=-0.3, vega=0.1),
        )
        put_snap = SimpleNamespace(
            latest_quote=SimpleNamespace(bid_price=2.0, ask_price=2.2),
            latest_trade=None,
            implied_volatility=0.40, greeks=SimpleNamespace(delta=-0.45, gamma=0.02, theta=-0.2, vega=0.1),
        )
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = {
            "AAPL260910C00200000": call_snap,
            "AAPL260910P00200000": put_snap,
        }
        with patch("lib.alpaca_client.get_option_data_client", return_value=mock_client):
            result = get_chain_summary("AAPL", current_price=200.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["underlying"], "AAPL")
        self.assertEqual(result["contracts_analyzed"], 2)
        self.assertIsNotNone(result["expected_move"])


if __name__ == "__main__":
    unittest.main()
