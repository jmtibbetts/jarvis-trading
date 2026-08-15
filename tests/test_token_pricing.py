"""Pricing a token amount — by precedence, or abstaining.

The gap these close: /v1/transfers carries no USD, so the whale floor had
nothing to measure against and scored a silent zero. The fix is NOT one
price source — it is an ordered set of them with the weakest clearly
marked, and a real None when none of them answer.
"""
import unittest

from lib.token_pricing import (
    STABLECOIN_MINTS,
    coverage,
    price_map,
    usd_value,
    value_transfers,
)

USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
JUNK = "JunkMintAddressThatNobodyHasEverPriced111111"


class PegTests(unittest.TestCase):
    def test_the_verified_stablecoins_are_the_ones_helius_confirmed(self):
        """Both mints were checked with getAsset rather than recalled:
        symbol USDT / USD Coin, 6 decimals each."""
        self.assertEqual(set(STABLECOIN_MINTS), {USDT, USDC})

    def test_a_stablecoin_needs_no_lookup_at_all(self):
        p = price_map([USDT, USDC])
        for mint in (USDT, USDC):
            self.assertEqual(p[mint]["price"], 1.0)
            self.assertEqual(p[mint]["source"], "peg")

    def test_wrapped_sol_is_not_treated_as_a_stablecoin(self):
        """It starts with 'So1111...' and is the most common mint in the
        flow; mistaking it for a dollar would misprice everything."""
        self.assertNotIn(WSOL, STABLECOIN_MINTS)
        self.assertIsNone(price_map([WSOL])[WSOL]["price"])

    def test_a_stablecoin_amount_is_its_dollar_value(self):
        self.assertEqual(usd_value(49.7, USDT, price_map([USDT])), 49.7)


class PrecedenceTests(unittest.TestCase):
    def test_helius_price_is_used_when_present(self):
        p = price_map([WSOL], helius_prices={WSOL: 75.22})
        self.assertEqual(p[WSOL]["source"], "helius")
        self.assertAlmostEqual(usd_value(2, WSOL, p), 150.44, places=2)

    def test_peg_wins_over_a_contradicting_helius_price(self):
        """A stablecoin quoted at 0.98 is a depeg or a bad tick; the peg
        is the assumption this module documents and stands behind."""
        p = price_map([USDT], helius_prices={USDT: 0.98})
        self.assertEqual(p[USDT]["price"], 1.0)
        self.assertEqual(p[USDT]["source"], "peg")

    def test_market_fallback_is_marked_lowest_confidence(self):
        """Joining by SYMBOL is not an identity match — ticker collisions
        are routine, so this source is used last and flagged."""
        p = price_map([JUNK], market_prices={"FOO": 3.0}, symbols={JUNK: "FOO"})
        self.assertEqual(p[JUNK]["source"], "market")
        self.assertLess(p[JUNK]["confidence"], 0.9)

    def test_helius_beats_the_market_table(self):
        p = price_map([WSOL], helius_prices={WSOL: 75.0},
                      market_prices={"SOL": 999.0}, symbols={WSOL: "SOL"})
        self.assertEqual(p[WSOL]["price"], 75.0)

    def test_a_market_price_without_a_symbol_is_not_guessed(self):
        """No symbol means no join. Inferring one from the mint would be
        inventing an identity."""
        p = price_map([JUNK], market_prices={"FOO": 3.0})
        self.assertIsNone(p[JUNK]["price"])


class AbstentionTests(unittest.TestCase):
    def test_an_unpriced_mint_returns_none_not_zero(self):
        """Zero would read as a $0 transfer and silently clear no whale
        threshold; None is a branch the caller must handle."""
        self.assertIsNone(usd_value(1_000_000, JUNK, price_map([JUNK])))

    def test_a_zero_or_negative_price_is_not_accepted(self):
        p = price_map([WSOL], helius_prices={WSOL: 0})
        self.assertIsNone(p[WSOL]["price"])

    def test_unknown_mint_is_absent_not_an_error(self):
        self.assertIsNone(usd_value(5, "NEVERSEEN", {}))


class ValuationTests(unittest.TestCase):
    TRANSFERS = [
        {"mint": USDT, "amount": 49.7, "direction": "out"},
        {"mint": WSOL, "amount": 2.0, "direction": "out"},
        {"mint": JUNK, "amount": 5000.0, "direction": "in"},
    ]

    def _priced(self):
        p = price_map([USDT, WSOL, JUNK], helius_prices={WSOL: 75.0})
        return value_transfers(self.TRANSFERS, p)

    def test_each_transfer_gains_a_value_or_an_explicit_none(self):
        v = self._priced()
        self.assertEqual(v[0]["usd_value"], 49.7)
        self.assertEqual(v[1]["usd_value"], 150.0)
        self.assertIsNone(v[2]["usd_value"])
        self.assertIn("usd_value", v[2], "the key must exist even when None")

    def test_original_fields_survive(self):
        self.assertEqual(self._priced()[0]["direction"], "out")

    def test_coverage_reports_the_blind_spot(self):
        """A whale scan that could price a third of its input has a
        two-thirds blind spot, and that belongs on screen beside it."""
        c = coverage(self._priced())
        self.assertEqual(c["total"], 3)
        self.assertEqual(c["priced"], 2)
        self.assertEqual(c["unpriced"], 1)
        self.assertEqual(c["by_source"], {"peg": 1, "helius": 1})

    def test_coverage_of_nothing_does_not_divide_by_zero(self):
        self.assertIsNone(coverage([])["priced_pct"])


class WhaleIntegrationTests(unittest.TestCase):
    """The point of the whole module: absolute size can now fire."""

    def test_a_priced_stablecoin_transfer_reaches_the_whale_floor(self):
        from lib.wallet_intel import whale_score
        p = price_map([USDT])
        valued = value_transfers([{"mint": USDT, "amount": 250_000}], p)
        v = whale_score(valued[0])
        self.assertTrue(v["is_whale"])
        self.assertFalse(any("no USD value" in r for r in v["reasons"]))

    def test_an_unpriced_transfer_still_says_why_it_cannot_judge_size(self):
        from lib.wallet_intel import whale_score
        valued = value_transfers([{"mint": JUNK, "amount": 10**9}],
                                 price_map([JUNK]))
        v = whale_score(valued[0])
        self.assertTrue(any("no USD value" in r for r in v["reasons"]))


if __name__ == "__main__":
    unittest.main()
