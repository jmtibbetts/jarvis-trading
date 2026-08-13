"""Crypto Alpaca doesn't list routes to paper — allowlist, never denylist.

The live venue lists 73 pairs; the crypto universe is unbounded (hundreds
of thousands counting meme coins). Routing must therefore be decided
against the broker's own listing, so a new coin needs no code change:
not listed = paper, priced at Kraken's fee model.
"""
import unittest
from unittest.mock import patch

from lib.venues import crypto_requires_paper


def _listing(syms):
    return patch("lib.alpaca_client.tradable_crypto_symbols",
                 return_value=syms)


class RoutingTests(unittest.TestCase):
    LISTED = {"BTC/USD", "ETH/USD", "LINK/USD"}

    def test_listed_pairs_stay_live(self):
        with _listing(self.LISTED):
            self.assertFalse(crypto_requires_paper("BTC/USD"))
            self.assertFalse(crypto_requires_paper("link/usd".upper()))

    def test_unlisted_crypto_goes_paper(self):
        with _listing(self.LISTED):
            self.assertTrue(crypto_requires_paper("SUI/USD"))
            self.assertTrue(crypto_requires_paper("WIF/USD"))
            self.assertTrue(crypto_requires_paper("SOME_BRAND_NEW_MEME/USD"))

    def test_equities_are_untouched(self):
        with _listing(self.LISTED):
            self.assertFalse(crypto_requires_paper("NVDA"))
            self.assertFalse(crypto_requires_paper("SPY"))

    def test_a_failed_listing_lookup_never_flips_the_book(self):
        """Unknown is not 'unlisted': an Alpaca hiccup must not send every
        crypto signal to paper. The submit-time guard remains the backstop."""
        with _listing(None):
            self.assertFalse(crypto_requires_paper("BTC/USD"))
            self.assertFalse(crypto_requires_paper("SUI/USD"))

    def test_the_scanner_and_generator_both_consult_it(self):
        import inspect

        from jobs import generate_signals, scan_opportunities
        self.assertIn("crypto_requires_paper",
                      inspect.getsource(generate_signals))
        self.assertIn("crypto_requires_paper",
                      inspect.getsource(scan_opportunities._classify_symbol))


if __name__ == "__main__":
    unittest.main()
