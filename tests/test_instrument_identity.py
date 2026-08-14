"""One authority for "which instrument is this string".

Three scattered implementations (execute_signals._both_formats,
alpaca_client._symbol_variants, per-module slash heuristics) let the
LINKUSD-vs-LINK/USD split nearly cancel a live stop-loss. The registry
answers identity questions once, for everyone.
"""
import unittest

from lib.instruments import asset_class_of, canonical, variants


class CanonicalTests(unittest.TestCase):
    def test_slashless_crypto_gets_its_slash_back(self):
        self.assertEqual(canonical("LINKUSD"), "LINK/USD")
        self.assertEqual(canonical("btcusd"), "BTC/USD")

    def test_equities_stay_bare(self):
        self.assertEqual(canonical("NVDA"), "NVDA")
        # SPCX the EQUITY must not become SP/CX or SPCX/USD
        self.assertEqual(canonical("SPCX"), "SPCX")

    def test_futures_and_fx_formats_untouched(self):
        self.assertEqual(canonical("HG=F"), "HG=F")
        self.assertEqual(canonical("^VIX"), "^VIX")
        self.assertEqual(canonical("EURUSD=X"), "EURUSD=X")

    def test_aliases_route_first(self):
        # SPCX/USD is the tokenized stock listed as XSPCX/USD (symbol_aliases)
        self.assertEqual(canonical("SPCX/USD"), "XSPCX/USD")


class VariantsTests(unittest.TestCase):
    def test_every_venue_spelling_from_either_input(self):
        self.assertEqual(variants("LINK/USD"), {"LINK/USD", "LINKUSD"})
        self.assertEqual(variants("LINKUSD"), {"LINK/USD", "LINKUSD"})

    def test_equities_yield_themselves(self):
        self.assertEqual(variants("NVDA"), {"NVDA"})


class AssetClassTests(unittest.TestCase):
    def test_classes_from_shape(self):
        self.assertEqual(asset_class_of("BTC/USD"), "Crypto")
        self.assertEqual(asset_class_of("LINKUSD"), "Crypto")
        self.assertEqual(asset_class_of("NVDA"), "Equity")
        self.assertEqual(asset_class_of("HG=F"), "Futures")
        self.assertEqual(asset_class_of("EURUSD=X"), "Forex")
        self.assertEqual(asset_class_of("^TNX"), "Futures")


class DuplicatesDelegateTests(unittest.TestCase):
    def test_the_old_helpers_now_delegate(self):
        import inspect

        from jobs import execute_signals
        from lib import alpaca_client
        self.assertIn("variants", inspect.getsource(execute_signals._both_formats))
        self.assertIn("variants", inspect.getsource(alpaca_client._symbol_variants))


if __name__ == "__main__":
    unittest.main()
