"""A fee must match the instrument, not the default venue.

`venue_round_trip_fee` priced EVERYTHING at the Kraken crypto taker rate.
That is wrong in both directions and by large factors:

  GOOGL 1x   charged 0.8% round trip at a venue that does not list it.
             The real cost is SEC + FINRA on the sell side: ~0.003%.
  ANET 3.2x  charged a perpetual fee for a product that does not exist.
             Leverage on a stock is a margin LOAN — interest per day held,
             not a per-trade fee.
  MES=F      charged a percentage. CME products are priced per contract,
             which is a different SHAPE, not a different number.

A 60-position Auto Sim book was carrying ~$1,200 of fees, most of it billed
to equities that would have cost pennies.
"""
import os
import unittest
from unittest import mock

from lib.paper_engine import venue_round_trip_fee


def pct(symbol, notional, leverage, price):
    fee, why = venue_round_trip_fee(symbol, notional, leverage, price)
    return fee / notional * 100, why


class EquityRoutingTests(unittest.TestCase):
    def test_a_stock_is_not_charged_a_crypto_taker_fee(self):
        rate, why = pct("GOOGL", 8_000.0, 1.0, 342.0)
        self.assertLess(rate, 0.01, f"stock charged {rate:.3f}% — {why}")
        self.assertIn("commission", why)

    def test_leverage_on_a_stock_does_not_become_a_perpetual(self):
        """3.2x ANET is margin, and margin has no per-trade fee."""
        unlevered, _ = pct("ANET", 25_600.0, 1.0, 206.5)
        levered, why = pct("ANET", 25_600.0, 3.2, 206.5)
        self.assertAlmostEqual(unlevered, levered, places=6)
        self.assertIn("margin loan", why)

    def test_the_cost_is_not_zero_either(self):
        """Commission-free is not free — the sell side pays SEC and FINRA."""
        fee, _ = venue_round_trip_fee("GOOGL", 8_000.0, 1.0, 342.0)
        self.assertGreater(fee, 0.0)

    def test_taf_is_capped(self):
        """FINRA caps the TAF per trade; an enormous share count must not
        produce an unbounded fee."""
        from lib.venues import equity_regulatory_fee, FINRA_TAF_CAP
        _, why = equity_regulatory_fee(1_000.0, 10_000_000)
        self.assertIn(f"{FINRA_TAF_CAP:,.2f}", why)


class FuturesRoutingTests(unittest.TestCase):
    def test_cme_products_are_priced_per_contract(self):
        """Doubling notional at the same price doubles the contract count,
        so the fee scales with CONTRACTS — and the percentage stays flat."""
        one, _ = venue_round_trip_fee("MES=F", 5_000.0, 10.0, 5_000.0)
        ten, _ = venue_round_trip_fee("MES=F", 50_000.0, 10.0, 5_000.0)
        self.assertAlmostEqual(ten, one * 10, places=6)

    def test_a_future_never_falls_through_to_the_crypto_rate(self):
        for sym in ("MES=F", "ES=F", "MNQ=F", "NQ=F"):
            _, why = venue_round_trip_fee(sym, 25_000.0, 10.0, 5_000.0)
            self.assertNotIn("kraken taker", why.lower(), f"{sym} priced as crypto spot")

    def test_micros_stay_cheaper_than_e_minis_per_contract(self):
        micro, _ = venue_round_trip_fee("MES=F", 5_000.0, 10.0, 5_000.0)
        emini, _ = venue_round_trip_fee("ES=F", 5_000.0, 10.0, 5_000.0)
        self.assertLess(micro, emini)


class CryptoStillWorksTests(unittest.TestCase):
    def test_crypto_spot_still_pays_the_venue_rate(self):
        rate, _ = pct("BTC/USD", 2_000.0, 1.0, 95_000.0)
        self.assertGreater(rate, 0.1, "crypto spot should be expensive")


class PerContractSanityTests(unittest.TestCase):
    """Kraken flexible futures use contract_size 1.0 — one contract IS one
    token. A $0.089 coin therefore needs ~20,000 contracts to build a $1,800
    position, and a flat $0.15/contract/side bills $6,047 to trade $1,800.

    That is a misapplied model, not an expensive venue. Being wrong by 3400%
    in the EXPENSIVE direction vetoes every sound low-priced trade, which is
    the same class of failure as charging nothing at all.
    """

    # The per-contract schedule only applies to a US account, so these tests
    # state the region instead of inheriting whatever .env happens to hold —
    # a test that passes only when the developer's environment is configured
    # is not testing the guard.
    def setUp(self):
        self._patch = mock.patch.dict(os.environ, {"VENUE_REGION": "us"})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_a_cheap_coin_is_not_charged_more_than_the_position(self):
        fee, why = venue_round_trip_fee("OP/USD", 1_800.0, 5.0, 0.0893)
        self.assertLess(fee, 1_800.0, f"fee exceeded notional — {why}")

    def test_the_fallback_is_labelled_not_silent(self):
        _, why = venue_round_trip_fee("OP/USD", 1_800.0, 5.0, 0.0893)
        self.assertIn("not applicable", why)

    def test_cost_stays_bounded_across_a_million_fold_price_range(self):
        """BTC at $95,000 and OP at $0.089 must both land on a plausible
        round-trip cost. The old path spanned 0.017% to 336%."""
        for sym, px in (("BTC/USD", 95_000.0), ("ETH/USD", 3_200.0),
                        ("SOL/USD", 180.0), ("SUI/USD", 0.686), ("OP/USD", 0.0893)):
            fee, why = venue_round_trip_fee(sym, 1_800.0, 5.0, px)
            rate = fee / 1_800.0
            self.assertGreater(rate, 0.0, f"{sym} traded free")
            self.assertLess(rate, 0.05, f"{sym} charged {rate * 100:.1f}% — {why}")

    def test_expensive_instruments_still_price_per_contract(self):
        """The guard must not swallow the per-contract model where it DOES
        apply — BTC and ETH stay on it."""
        for sym, px in (("BTC/USD", 95_000.0), ("ETH/USD", 3_200.0)):
            _, why = venue_round_trip_fee(sym, 1_800.0, 5.0, px)
            self.assertIn("/contract/side", why.lower(), sym)

    def test_a_symbol_with_no_perp_schedule_is_still_bounded(self):
        """The guard used to fall through to the very number it rejected when
        no percentage schedule existed for the symbol. ISEK/USD reached
        $2,037,632 of 'fees' on $1,000 of margin that way."""
        fee, why = venue_round_trip_fee("ISEK/USD", 8_900.0, 8.9, 0.0001)
        self.assertLess(fee, 8_900.0 * 0.05, f"guard returned its own reject — {why}")
        self.assertGreater(fee, 0.0)

    def test_no_position_in_a_broad_symbol_sweep_exceeds_the_ceiling(self):
        """Whatever the symbol, whatever the unit price."""
        for sym in ("ISEK/USD", "ALT/USD", "DOGE/USD", "OP/USD", "ARC/USD",
                    "VIRTUAL/USD", "NOTAREALCOIN/USD"):
            for px in (0.00001, 0.0893, 1.0, 180.0, 95_000.0):
                fee, why = venue_round_trip_fee(sym, 5_000.0, 8.0, px)
                self.assertLess(fee / 5_000.0, 0.05,
                                f"{sym} @ {px}: {fee / 50:.1f}% — {why}")

    def test_the_international_schedule_is_bounded_too(self):
        """Outside the US there is no per-contract path at all — the
        percentage schedule must still produce a sane cost everywhere."""
        with mock.patch.dict(os.environ, {"VENUE_REGION": "international"}):
            for sym, px in (("BTC/USD", 95_000.0), ("OP/USD", 0.0893)):
                fee, why = venue_round_trip_fee(sym, 1_800.0, 5.0, px)
                self.assertGreater(fee, 0.0, sym)
                self.assertLess(fee / 1_800.0, 0.05, f"{sym} — {why}")


class NoFreeLunchTests(unittest.TestCase):
    """The most dangerous possible return value is 0.0 — the one path where
    the cost model breaks is the one path that reports no cost at all. It
    used to return exactly that."""

    def test_a_broken_lookup_charges_rather_than_zeroes(self):
        fee, why = venue_round_trip_fee("!!!GARBAGE!!!", 10_000.0, 3.0, 0.0)
        self.assertGreater(fee, 0.0, f"failure path made trading free — {why}")


if __name__ == "__main__":
    unittest.main()
