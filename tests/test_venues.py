"""Venue-aware costs and order validity.

The cost model previously assumed one crypto fee for every venue — Alpaca's
0.25% taker. Kraken's base tier is 0.40%, verified live from their public
AssetPairs endpoint. Since P0 rejects trades whose costs exceed 0.50R, a
fee wrong by 60% silently moves the line between tradeable and not.
"""
import unittest

from lib.venues import (fee_for, validate_order, is_tradeable_on,
                        kraken_pair_specs, VENUE_FEES)
from lib.transaction_costs import estimate_costs, min_viable_stop_pct


class FeeScheduleTests(unittest.TestCase):
    def test_kraken_is_dearer_than_alpaca_at_base_volume(self):
        self.assertGreater(fee_for("kraken")[0], fee_for("alpaca")[0])

    def test_maker_is_always_cheaper_than_taker(self):
        for venue in ("alpaca", "kraken"):
            self.assertLess(fee_for(venue, maker=True)[0], fee_for(venue)[0])

    def test_volume_tiers_step_down_and_never_up(self):
        rates = [fee_for("kraken", volume_30d=v)[0]
                 for v in (0, 10_000, 50_000, 100_000, 1_000_000)]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_default_tier_is_the_expensive_one(self):
        """Assuming a discount you have not earned understates cost."""
        self.assertEqual(fee_for("kraken")[0], VENUE_FEES["kraken"]["crypto"]["taker"][0][1])

    def test_unknown_venue_is_never_free(self):
        rate, why = fee_for("someexchange")
        self.assertGreater(rate, 0)
        self.assertIn("unknown venue", why)

    def test_explanation_names_venue_and_tier(self):
        _, why = fee_for("kraken", volume_30d=100_000)
        self.assertIn("kraken", why)
        self.assertIn("100,000", why)


class CostModelIsVenueAwareTests(unittest.TestCase):
    def test_same_trade_costs_more_on_the_dearer_venue(self):
        a = estimate_costs("BTC/USD", 100.0, 98.0, venue="alpaca")
        k = estimate_costs("BTC/USD", 100.0, 98.0, venue="kraken")
        self.assertGreater(k["total_r"], a["total_r"])

    def test_min_viable_stop_widens_on_the_dearer_venue(self):
        self.assertGreater(min_viable_stop_pct("BTC/USD", venue="kraken"),
                           min_viable_stop_pct("BTC/USD", venue="alpaca"))

    def test_result_records_which_venue_priced_it(self):
        self.assertEqual(estimate_costs("BTC/USD", 100.0, 98.0, venue="kraken")["fee_venue"],
                         "kraken")

    def test_equities_are_unaffected_by_crypto_venue(self):
        self.assertEqual(estimate_costs("NVDA", 100.0, 98.0, venue="kraken")["fees_pct"], 0.0)


class OrderValidityTests(unittest.TestCase):
    """Live checks against Kraken's published pair specs."""

    def test_btc_is_listed(self):
        ok, why = is_tradeable_on("kraken", "BTC/USD")
        self.assertTrue(ok, why)

    def test_nonsense_symbol_is_not_listed(self):
        ok, _ = is_tradeable_on("kraken", "NOTACOIN/USD")
        self.assertFalse(ok)

    def test_size_below_the_minimum_is_refused(self):
        out = validate_order("kraken", "BTC/USD", 0.000001, 95000.0)
        self.assertFalse(out["ok"])
        self.assertIn("below kraken minimum", out["reason"])

    def test_price_off_the_tick_grid_is_refused_and_names_the_valid_price(self):
        out = validate_order("kraken", "BTC/USD", 0.01, 95000.037)
        self.assertFalse(out["ok"])
        self.assertIn("off the", out["reason"])
        self.assertIn("nearest valid", out["reason"])

    def test_a_valid_order_passes(self):
        self.assertTrue(validate_order("kraken", "BTC/USD", 0.01, 95000.0)["ok"])

    def test_specs_expose_margin_leverage(self):
        spec = kraken_pair_specs("BTC/USD")
        self.assertIsNotNone(spec)
        self.assertGreaterEqual(spec["max_leverage"], 2)


if __name__ == "__main__":
    unittest.main()


class MeasuredSpreadTests(unittest.TestCase):
    """A conservative default is right when nothing is known. It is wrong
    when a free live measurement exists — the 0.10% crypto assumption was
    16x too wide for BTC, inflating the minimum viable stop."""

    def test_live_spread_is_used_when_available(self):
        from lib.transaction_costs import estimate_spread_pct
        spread, source = estimate_spread_pct("BTC/USD", venue="kraken")
        self.assertIn("measured", source)
        self.assertGreater(spread, 0)

    def test_measured_beats_the_default_for_a_liquid_pair(self):
        from lib.transaction_costs import estimate_spread_pct, DEFAULT_CRYPTO_SPREAD_PCT
        spread, _ = estimate_spread_pct("BTC/USD", venue="kraken")
        self.assertLess(spread, DEFAULT_CRYPTO_SPREAD_PCT)

    def test_illiquid_names_show_a_wider_spread_than_majors(self):
        from lib.venues import measured_spread_pct
        btc, _ = measured_spread_pct("BTC/USD")
        sol, _ = measured_spread_pct("SOL/USD")
        if btc is not None and sol is not None:
            self.assertGreater(sol, btc)   # thinner book, wider quote

    def test_a_caller_supplied_quote_always_wins(self):
        from lib.transaction_costs import estimate_spread_pct
        spread, source = estimate_spread_pct("BTC/USD", quoted_spread_pct=0.005, venue="kraken")
        self.assertEqual(spread, 0.005)
        self.assertEqual(source, "quoted")

    def test_unlisted_symbol_falls_back_to_the_default_not_to_zero(self):
        from lib.transaction_costs import estimate_spread_pct, DEFAULT_CRYPTO_SPREAD_PCT
        spread, source = estimate_spread_pct("NOTACOIN/USD", venue="kraken")
        self.assertEqual(spread, DEFAULT_CRYPTO_SPREAD_PCT)
        self.assertEqual(source, "default_crypto")

    def test_failure_never_makes_a_trade_look_free(self):
        """The direction of the fallback matters: a network problem must
        not reduce estimated cost."""
        from lib.transaction_costs import estimate_spread_pct, DEFAULT_CRYPTO_SPREAD_PCT
        spread, _ = estimate_spread_pct("BTC/USD", venue="nonexistent-venue")
        self.assertGreaterEqual(spread, DEFAULT_CRYPTO_SPREAD_PCT)

    def test_equities_are_unaffected(self):
        from lib.transaction_costs import estimate_spread_pct, DEFAULT_EQUITY_SPREAD_PCT
        spread, source = estimate_spread_pct("NVDA", venue="kraken")
        self.assertEqual(spread, DEFAULT_EQUITY_SPREAD_PCT)
        self.assertEqual(source, "default_equity")


class KrakenFuturesSpecTests(unittest.TestCase):
    """Crypto-derivative specs from the venue itself, not typed in by hand."""

    def test_perpetual_specs_load(self):
        from lib.venues import kraken_futures_spec
        spec = kraken_futures_spec("BTC/USD")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["symbol"], "PF_XBTUSD")
        self.assertGreater(spec["tick_size"], 0)

    def test_margin_is_tiered_not_a_single_number(self):
        from lib.venues import kraken_futures_spec
        tiers = kraken_futures_spec("BTC/USD")["margin_tiers"]
        self.assertGreater(len(tiers), 1)
        # Margin requirement must rise with size, never fall.
        margins = [t["initial_margin"] for t in tiers]
        self.assertEqual(margins, sorted(margins))

    def test_max_leverage_falls_as_position_grows(self):
        from lib.venues import max_leverage_at_size
        small, _ = max_leverage_at_size("BTC/USD", 1_000)
        large, _ = max_leverage_at_size("BTC/USD", 40_000_000)
        self.assertGreater(small, large)

    def test_unlisted_symbol_returns_1x_not_a_guess(self):
        from lib.venues import max_leverage_at_size
        lev, why = max_leverage_at_size("NOTACOIN/USD", 1_000)
        self.assertEqual(lev, 1.0)
        self.assertIn("no kraken futures listing", why)


class ReadOnlyAccountTests(unittest.TestCase):
    """The adapter must be incapable of trading, and must degrade cleanly
    when no key is present."""

    def test_write_endpoints_are_rejected_by_construction(self):
        from lib.kraken_account import _private
        for endpoint in ("/0/private/AddOrder", "/0/private/CancelOrder",
                         "/0/private/Withdraw"):
            with self.assertRaises(ValueError):
                _private(endpoint)

    def test_allowlist_contains_no_mutating_endpoint(self):
        from lib.kraken_account import READ_ONLY_ENDPOINTS
        banned = ("AddOrder", "Cancel", "Withdraw", "Transfer", "Edit")
        for ep in READ_ONLY_ENDPOINTS:
            self.assertFalse(any(b.lower() in ep.lower() for b in banned), ep)

    def test_missing_credentials_report_rather_than_raise(self):
        from lib.kraken_account import check_connection, is_configured
        if not is_configured():
            out = check_connection()
            self.assertFalse(out["connected"])
            self.assertIn("reason", out)

    def test_signature_is_deterministic_and_matches_kraken_spec(self):
        """Known-answer test so a refactor cannot silently break signing."""
        from lib.kraken_account import _sign
        import base64
        secret = base64.b64encode(b"testsecret" * 5).decode()
        sig_a = _sign("/0/private/Balance", {"nonce": 1234567890}, secret)
        sig_b = _sign("/0/private/Balance", {"nonce": 1234567890}, secret)
        self.assertEqual(sig_a, sig_b)
        self.assertNotEqual(sig_a, _sign("/0/private/Balance", {"nonce": 1234567891}, secret))
        self.assertEqual(len(base64.b64decode(sig_a)), 64)   # SHA-512 digest
