"""The pegged-asset registry, and the two ways `stablecoin = true` fails.

Pinned here because both failure directions were live defects, not
hypotheticals: the flat boolean would have deleted XAUT (a position class
this desk actually trades) and would have modelled sUSDe as if it were a
cash claim.
"""
import unittest

from lib.pegged_assets import (
    REGISTRY, BY_MINT, depeg_profile, is_non_directional, is_pegged,
    is_usd_pegged, lookup, peg_currency_of,
    TIER_FIAT_RESERVE, TIER_SYNTHETIC, TIER_YIELD_WRAPPER,
)


class DirectionalityTests(unittest.TestCase):
    """The scanner exclusion must be USD-peg-shaped, not stablecoin-shaped."""

    def test_usd_pegged_assets_are_not_directional(self):
        for s in ("USDT/USD", "USDC/USD", "DAI/USD", "FRAX/USD", "USDe"):
            self.assertTrue(is_non_directional(s), s)

    def test_gold_tokens_stay_directional(self):
        """XAUT and PAXG are pegged — to GOLD, which moves in USD.

        This desk's paper book has been 76% XAUT. Sweeping it into
        'not a directional instrument' would delete a live position class.
        """
        for s in ("XAUT/USD", "PAXG/USD"):
            self.assertTrue(is_pegged(s), f"{s} belongs in the registry")
            self.assertFalse(is_non_directional(s), f"{s} must stay tradeable")
            self.assertEqual(peg_currency_of(s), "XAU")
            self.assertFalse(is_usd_pegged(s))

    def test_non_usd_stablecoins_stay_directional(self):
        """EURC is stable in EUR and carries real FX risk against USD."""
        self.assertTrue(is_pegged("EURC"))
        self.assertFalse(is_non_directional("EURC"))
        self.assertEqual(peg_currency_of("EURC"), "EUR")

    def test_only_the_base_decides(self):
        """BTC/USDT is a bet on BTC; USDT/USD is a bet on a peg."""
        self.assertFalse(is_non_directional("BTC/USDT"))
        self.assertFalse(is_non_directional("ETH/USDC"))
        self.assertTrue(is_non_directional("USDT/USD"))

    def test_wrapped_prefix_resolves_to_the_underlying(self):
        self.assertTrue(is_non_directional("wUSDC"))

    def test_unknown_symbols_are_not_assumed_pegged(self):
        for s in ("BTC/USD", "AAPL", "NZDUSD=X", "ES=F", ""):
            self.assertFalse(is_pegged(s), s)


class TierTests(unittest.TestCase):
    def test_tiers_separate_mechanisms_not_just_names(self):
        self.assertEqual(lookup("USDC").tier, TIER_FIAT_RESERVE)
        self.assertEqual(lookup("USDe").tier, TIER_SYNTHETIC)
        self.assertEqual(lookup("sUSDe").tier, TIER_YIELD_WRAPPER)

    def test_a_synthetic_dollar_is_not_modelled_like_a_cash_claim(self):
        """USDe and USDC both answer True to 'is it a stablecoin'."""
        usdc = depeg_profile("USDC")
        usde = depeg_profile("USDe")
        self.assertGreater(usde["severe_depeg_pct"], usdc["severe_depeg_pct"])

    def test_yield_wrappers_do_not_target_par(self):
        """sDAI is worth more than a dollar BY DESIGN; scoring the excess as
        a depeg would have the sign backwards."""
        for s in ("sDAI", "sUSDe", "sUSDS", "USD0++"):
            p = depeg_profile(s)
            self.assertFalse(p["targets_par"], s)
            self.assertTrue(p["yield_bearing"], s)
        self.assertTrue(depeg_profile("USDC")["targets_par"])

    def test_profiles_declare_themselves_assumptions(self):
        p = depeg_profile("USDC")
        self.assertIn("ASSUMED", p["basis"])
        self.assertTrue(p["failure_mode"])

    def test_unknown_asset_returns_none_rather_than_a_default(self):
        """Silently assuming a peg for an unknown token is the failure this
        module exists to prevent."""
        self.assertIsNone(depeg_profile("BTC"))
        self.assertIsNone(lookup("SOMETHINGNEW"))


class IdentityTests(unittest.TestCase):
    def test_mint_beats_ticker(self):
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertIn(usdc_mint, BY_MINT)
        self.assertEqual(lookup("WHATEVER", mint=usdc_mint).symbol, "USDC")

    def test_every_registered_mint_resolves_back(self):
        for mint, asset in BY_MINT.items():
            self.assertIs(lookup(None, mint=mint), asset)

    def test_every_entry_has_a_peg_currency_and_tier(self):
        for sym, a in REGISTRY.items():
            self.assertTrue(a.peg_currency, sym)
            self.assertIsNotNone(depeg_profile(sym), sym)


if __name__ == "__main__":
    unittest.main()
