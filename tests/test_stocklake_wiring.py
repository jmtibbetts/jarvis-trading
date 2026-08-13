"""Stocklake joins the analyst's tool belt — context, never arithmetic.

Its numbers (P/E, margins, beta) reach the deep-check prompt as a labelled
context line. The label matters: the LLM authority boundary says models
never compute with vendor numbers, and the line says so in-band.
"""
import unittest
from unittest.mock import patch

from lib.signal_verification import _stocklake_fundamentals_block

NVDA = ('{"symbol":"NVDA","pe_forward":17.62,"profit_margins":0.6296,'
        '"beta":2.215,"week52_low":164.07,"week52_high":236.54,'
        '"prev_close":224.09,"sector":"Technology"}')


class FundamentalsBlockTests(unittest.TestCase):
    @patch("lib.mcp_client.call_tool", return_value=NVDA)
    def test_a_compact_labelled_line_is_produced(self, _ct):
        out = _stocklake_fundamentals_block("NVDA")
        self.assertIn("fwd P/E 17.6", out)
        self.assertIn("52w position 83%", out)
        self.assertIn("context only", out)
        self.assertIn("not for arithmetic", out)

    def test_crypto_and_futures_symbols_are_skipped_without_a_call(self):
        """No '/' or '=' symbol should ever reach the vendor — Stocklake
        lists equities, and a failed lookup would burn a call per verify."""
        with patch("lib.mcp_client.call_tool") as ct:
            self.assertIsNone(_stocklake_fundamentals_block("BTC/USD"))
            self.assertIsNone(_stocklake_fundamentals_block("NG=F"))
            ct.assert_not_called()

    @patch("lib.mcp_client.call_tool", return_value=None)
    def test_vendor_silence_is_none_not_an_empty_label(self, _ct):
        self.assertIsNone(_stocklake_fundamentals_block("NVDA"))

    @patch("lib.mcp_client.call_tool", return_value='{"error":"rate limit"}')
    def test_error_payloads_are_none(self, _ct):
        self.assertIsNone(_stocklake_fundamentals_block("NVDA"))


class RegistryTests(unittest.TestCase):
    def test_stocklake_is_registered_with_bearer_auth(self):
        from lib.mcp_client import MCP_SERVERS
        cfg = MCP_SERVERS.get("stocklake")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["key_env"], "STOCKLAKE_API_KEY")
        self.assertNotIn("key_header", cfg)   # Bearer, not vendor header


if __name__ == "__main__":
    unittest.main()
