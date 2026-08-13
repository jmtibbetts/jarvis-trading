"""One asset, a different ticker at every venue.

The operator sees SpaceX on BTCC as SPCX/USD. The tracked symbol is
XSPCX/USD — the OKX xStock issue, live at $147.06, and the price BTCC
quotes. Typing the ticker on your own screen should not fail.

But an alias asserts that two names are the same tradeable thing, and that
assertion can be wrong: SPCXB/USD is the bStocks issue of the same
underlying and is a DIFFERENT instrument with its own liquidity — it had no
live quote at all when this was written. So aliases are explicit, never
inferred from a name match.
"""
import unittest

from lib.symbol_aliases import ALIASES, aliases_for, resolve


class ResolutionTests(unittest.TestCase):
    def test_the_venue_ticker_resolves(self):
        sym, note = resolve("SPCX/USD")
        self.assertEqual(sym, "XSPCX/USD")
        self.assertIsNotNone(note)

    def test_an_unaliased_symbol_passes_through_untouched(self):
        for s in ("BTC/USD", "NVDA", "ETH/USD"):
            sym, note = resolve(s)
            self.assertEqual(sym, s)
            self.assertIsNone(note)

    def test_case_and_whitespace_do_not_matter(self):
        self.assertEqual(resolve("  spcx/usd  ")[0], "XSPCX/USD")

    def test_an_alias_never_shadows_an_existing_symbol(self):
        """Bare SPCX is a separately tracked equity at $146.15. Aliasing it
        would mean asking for the equity and silently getting the token —
        an alias may resolve an ambiguity, never overwrite a real symbol."""
        sym, note = resolve("SPCX")
        self.assertEqual(sym, "SPCX")
        self.assertIsNone(note)

    def test_empty_input_does_not_explode(self):
        self.assertEqual(resolve(None), ("", None))
        self.assertEqual(resolve("")[0], "")


class SubstitutionIsVisibleTests(unittest.TestCase):
    """A silent rename means asking for one instrument and holding another."""

    def test_a_substitution_always_carries_an_explanation(self):
        for venue_ticker in ALIASES:
            _, note = resolve(venue_ticker)
            self.assertTrue(note, f"{venue_ticker} substituted silently")

    def test_the_note_names_both_tickers(self):
        _, note = resolve("SPCX/USD")
        self.assertIn("SPCX/USD", note)
        self.assertIn("XSPCX/USD", note)


class NarrownessTests(unittest.TestCase):
    """The claim being made is 'same tradeable thing', so it stays narrow."""

    def test_a_different_issue_of_the_same_underlying_is_not_an_alias(self):
        """SPCXB/USD is the bStocks issue — same company, different
        instrument, different liquidity, no live quote."""
        self.assertNotIn("SPCXB/USD", ALIASES)
        self.assertNotIn("SPCXB/USD", ALIASES.values())

    def test_every_alias_points_at_a_pair_not_a_bare_base(self):
        for target in ALIASES.values():
            self.assertIn("/", target, f"{target} is not a tradeable pair")

    def test_reverse_lookup_lists_the_venue_tickers(self):
        self.assertIn("SPCX/USD", aliases_for("XSPCX/USD"))


if __name__ == "__main__":
    unittest.main()


class AppliedEverywhereTests(unittest.TestCase):
    """A symbol must not mean different things at different endpoints.

    SPCX/USD added fine and focused fine, then analyzed as "insufficient
    data" on every timeframe — because two of the three paths resolved the
    venue ticker and the third did not. The same input behaving three ways
    is worse than it failing consistently: it looks like missing data
    rather than a missing lookup.
    """

    ENTRY_POINTS = ("add_watchlist_symbol", "set_focus", "analyze")

    def test_every_symbol_entry_point_resolves_aliases(self):
        import inspect
        from app import routes
        for name in self.ENTRY_POINTS:
            fn = getattr(routes, name, None)
            self.assertIsNotNone(fn, f"{name} not found — did it get renamed?")
            src = inspect.getsource(fn)
            self.assertIn("symbol_aliases", src,
                          f"{name} accepts a symbol but never resolves venue tickers")
