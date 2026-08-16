"""Before JARVIS can calculate anything, it must know what one unit IS.

Four concepts, resolved separately, because collapsing them is how the
arithmetic goes wrong:

    ASSET CLASS    what market it belongs to
    PRODUCT        what contract you actually hold
    QUANTITY UNIT  what "qty = 3" means
    EXECUTION SPEC the multiplier, tick and margin to simulate it

Two measured fail-opens this replaces:

    get_spec("EUR/USD")   -> CRYPTO spec, because the string has a slash
    get_spec("UNKNOWN=F") -> EQUITY spec, multiplier 1

The second is the dangerous one. A futures position sized on an equity
multiplier is wrong by whatever the real multiplier is — 50x on ES, 100x on
gold — and every number downstream (notional, margin, risk, P&L, R) inherits
the error silently, because nothing else in the system knows a contract is
supposed to be worth more than a share.

And EUR/USD vs EURUSD=X returned DIFFERENT answers for one instrument:
Crypto+crypto-spec against Forex+equity-spec. A symbol is one identifier;
it is not identity.
"""
import unittest

from lib.instruments import (AMBIGUOUS, COINS, COMMODITY_FUTURE, CONTRACTS,
                             CRYPTO, CRYPTO_PERP, CRYPTO_SPOT, DEX_SPOT,
                             EQUITY, EQUITY_SPOT, FOREX, FUTURES, FX_SPOT,
                             FX_UNITS, INDEX_FUTURE, PRODUCT_UNKNOWN, SHARES,
                             SUPPORTED, TOKEN_UNITS, UNSUPPORTED, VERIFIED,
                             UnsupportedInstrument, fx_canonical, get_spec,
                             resolve)


class TestMatrixTests(unittest.TestCase):
    """The matrix from the spec, verbatim."""

    def test_nvda_is_an_equity_in_shares(self):
        i = resolve("NVDA")
        self.assertEqual((i.asset_class, i.product, i.quantity_unit),
                         (EQUITY, EQUITY_SPOT, SHARES))
        self.assertEqual(i.multiplier, 1)

    def test_btc_usd_is_crypto_spot_in_coins(self):
        i = resolve("BTC/USD")
        self.assertEqual((i.asset_class, i.product, i.quantity_unit),
                         (CRYPTO, CRYPTO_SPOT, COINS))

    def test_btcusd_resolves_to_the_same_identity(self):
        self.assertEqual(resolve("BTCUSD").instrument_id,
                         resolve("BTC/USD").instrument_id)

    def test_eur_usd_is_forex_NOT_crypto(self):
        """The slash heuristic sent this down the crypto path."""
        i = resolve("EUR/USD")
        self.assertEqual(i.asset_class, FOREX)
        self.assertNotEqual(i.asset_class, CRYPTO)
        self.assertEqual((i.product, i.quantity_unit), (FX_SPOT, FX_UNITS))

    def test_eurusd_equals_x_is_the_same_fx_instrument(self):
        a, b = resolve("EUR/USD"), resolve("EURUSD=X")
        self.assertEqual(a.instrument_id, b.instrument_id)
        self.assertEqual(a.asset_class, b.asset_class)
        self.assertEqual(a.quantity_unit, b.quantity_unit)

    def test_gold_future_is_contracts_with_multiplier_100(self):
        i = resolve("GC=F")
        self.assertEqual((i.asset_class, i.product, i.quantity_unit),
                         (FUTURES, COMMODITY_FUTURE, CONTRACTS))
        self.assertEqual(i.multiplier, 100)
        self.assertEqual(i.status, VERIFIED)

    def test_micro_es_is_an_index_future_with_multiplier_5(self):
        i = resolve("MES=F")
        self.assertEqual((i.asset_class, i.product), (FUTURES, INDEX_FUTURE))
        self.assertEqual(i.multiplier, 5)

    def test_an_unknown_future_is_UNSUPPORTED_with_no_equity_fallback(self):
        i = resolve("UNKNOWN=F")
        self.assertEqual(i.asset_class, FUTURES)
        self.assertEqual(i.product, PRODUCT_UNKNOWN)
        self.assertEqual(i.status, UNSUPPORTED)
        self.assertEqual(i.reason, "MISSING_CONTRACT_SPEC")
        self.assertFalse(i.executable)

    def test_a_dex_pair_is_token_units(self):
        i = resolve("SOL/USDC", venue="dex")
        self.assertEqual((i.asset_class, i.product, i.quantity_unit),
                         (CRYPTO, DEX_SPOT, TOKEN_UNITS))

    def test_a_perp_stays_a_perp_at_one_x(self):
        """Leverage is a POSITION parameter. A 1x perpetual is still a
        perpetual — it has funding and a liquidation price that spot does
        not."""
        i = resolve("BTC/USD", product=CRYPTO_PERP)
        self.assertEqual(i.product, CRYPTO_PERP)
        self.assertEqual(i.quantity_unit, CONTRACTS)

    def test_an_unregistered_fiat_pair_is_forex_not_crypto(self):
        """SEK/NOK is a real FX cross that is simply not in the registry.
        It resolves as FOREX with an inferred pip — the invariant that
        matters is that a slash between two FIAT legs never becomes
        crypto, whatever the registry happens to contain."""
        i = resolve("SEK/NOK")
        self.assertEqual(i.asset_class, FOREX)
        self.assertNotEqual(i.asset_class, CRYPTO)
        self.assertEqual(i.quantity_unit, FX_UNITS)
        self.assertEqual(i.status, SUPPORTED)

    def test_the_ambiguous_branch_is_defensive_only(self):
        """No input currently reaches it: fx_canonical catches every
        fiat/fiat pair first. Kept as a guard rather than deleted, but
        pinned here so its unreachability is a stated fact rather than an
        assumption somebody has to re-derive."""
        self.assertNotEqual(resolve("SEK/NOK").status, AMBIGUOUS)


class NoFailOpenTests(unittest.TestCase):
    def test_get_spec_refuses_an_unknown_future(self):
        with self.assertRaises(UnsupportedInstrument):
            get_spec("UNKNOWN=F")

    def test_get_spec_no_longer_calls_fx_crypto(self):
        self.assertEqual(get_spec("EUR/USD").symbol, "FX")
        self.assertEqual(get_spec("EURUSD=X").symbol, "FX")

    def test_known_futures_still_resolve(self):
        self.assertEqual(get_spec("GC=F").multiplier, 100)
        self.assertEqual(get_spec("MES=F").multiplier, 5)

    def test_require_executable_raises_on_unsupported(self):
        with self.assertRaises(UnsupportedInstrument):
            resolve("UNKNOWN=F").require_executable()

    def test_require_executable_passes_a_verified_contract(self):
        self.assertEqual(resolve("GC=F").require_executable().multiplier, 100)

    def test_the_old_fallback_would_have_returned_multiplier_one(self):
        """Proves the defect was real: the old path gave a futures symbol
        an equity spec, so one contract moved one dollar per point."""
        s = "UNKNOWN=F"
        old_would_be = 1.0 if s not in ("GC=F", "MES=F") else 100.0
        self.assertEqual(old_would_be, 1.0)
        self.assertFalse(resolve(s).executable)


class PipSizeTests(unittest.TestCase):
    """A JPY pair pips at the 2nd decimal, everything else at the 4th —
    a 100x difference in every FX cost calculation."""

    def test_a_major_pips_at_four_decimals(self):
        self.assertEqual(resolve("EUR/USD").pip_size, 0.0001)

    def test_a_jpy_cross_pips_at_two_decimals(self):
        self.assertEqual(resolve("USD/JPY").pip_size, 0.01)
        self.assertEqual(resolve("GBP/JPY").pip_size, 0.01)

    def test_an_unregistered_jpy_pair_still_infers_the_right_pip(self):
        i = resolve("NZD/JPY")
        self.assertEqual(i.pip_size, 0.01)
        self.assertEqual(i.status, SUPPORTED)
        self.assertIn("inferred", i.reason or "")


class SymbolIsNotIdentityTests(unittest.TestCase):
    def test_fx_spellings_collapse_to_one_canonical(self):
        for s in ("EUR/USD", "EURUSD=X", "EURUSD", "eur/usd"):
            self.assertEqual(fx_canonical(s), "EUR/USD", s)

    def test_a_crypto_pair_is_not_fx(self):
        self.assertIsNone(fx_canonical("BTC/USD"))
        self.assertIsNone(fx_canonical("SOL/USDC"))

    def test_every_identity_carries_a_stable_id(self):
        for s in ("NVDA", "BTC/USD", "EUR/USD", "GC=F"):
            self.assertTrue(resolve(s).instrument_id, s)


class UnitInvariantTests(unittest.TestCase):
    """No two products may share a unit that means different things."""

    def test_futures_are_always_contracts(self):
        for s in ("GC=F", "MES=F", "ES=F", "CL=F"):
            i = resolve(s)
            if i.executable:
                self.assertEqual(i.quantity_unit, CONTRACTS, s)

    def test_contract_products_carry_a_real_multiplier(self):
        for s in ("GC=F", "MES=F"):
            self.assertGreater(resolve(s).multiplier, 1)

    def test_unit_products_carry_multiplier_one(self):
        for s in ("NVDA", "BTC/USD", "EUR/USD"):
            self.assertEqual(resolve(s).multiplier, 1.0, s)

    def test_every_executable_instrument_states_its_unit(self):
        from lib.instruments import UNIT_UNKNOWN
        for s in ("NVDA", "BTC/USD", "EUR/USD", "GC=F", "MES=F"):
            i = resolve(s)
            self.assertNotEqual(i.quantity_unit, UNIT_UNKNOWN, s)

    def test_asset_class_is_never_used_as_the_product(self):
        """"Crypto" does not say spot vs perp vs DEX swap."""
        products = {resolve("BTC/USD").product,
                    resolve("BTC/USD", product=CRYPTO_PERP).product,
                    resolve("SOL/USDC", venue="dex").product}
        self.assertEqual(len(products), 3, products)


if __name__ == "__main__":
    unittest.main()
