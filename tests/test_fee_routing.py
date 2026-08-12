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
        """Spot must stay expensive — the measured account rate is 0.8%/side.
        Requires product="spot", since the desk's default product is perp."""
        fee, _ = venue_round_trip_fee("BTC/USD", 2_000.0, 1.0, 95_000.0,
                                      product="spot")
        self.assertGreater(fee / 2_000.0 * 100, 0.1, "crypto spot should be expensive")


class PerpetualPricingTests(unittest.TestCase):
    """Perps are priced as a PERCENTAGE of notional, not per contract.

    The US per-contract figure ($0.15/side) was quoted for standardised
    contracts. Kraken's flexible futures use contract_size 1.0 -- one
    contract IS one token -- so carrying that rate over lands at opposite
    absurdities depending only on unit price:

        OP/USD   $0.089    20,157 contracts -> $6,047 on $1,800  (336%)
        BTC/USD  $95,000    0.0937 contracts ->   $0.03 on $8,900 (0.0003%)

    No venue charges either. The percentage schedule gives $8.90 on $8,900
    at every unit price -- what Kraken publishes for perps (0.05%/side) and
    what the operator observes paying. Per-contract pricing is kept only for
    CME products, where the contract is genuinely standardised.
    """

    def setUp(self):
        self._patch = mock.patch.dict(os.environ, {"VENUE_REGION": "us"})
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_the_rate_is_flat_across_a_million_fold_price_range(self):
        """A perp round trip costs the same fraction whether the token is
        worth $95,000 or a hundredth of a cent."""
        rates = []
        for sym, px in (("BTC/USD", 95_000.0), ("ETH/USD", 3_200.0),
                        ("SOL/USD", 180.0), ("OP/USD", 0.0893),
                        ("ISEK/USD", 0.0001)):
            fee, why = venue_round_trip_fee(sym, 8_900.0, 8.9, px)
            rates.append(round(fee / 8_900.0, 6))
        self.assertEqual(len(set(rates)), 1, f"rate varied by unit price: {rates}")

    def test_a_leveraged_round_trip_costs_a_few_dollars_not_thousands(self):
        fee, why = venue_round_trip_fee("OP/USD", 8_900.0, 8.9, 0.0893)
        self.assertLess(fee, 25.0, f"${fee:,.2f} to trade $8,900 -- {why}")
        self.assertGreater(fee, 0.0)

    def test_a_symbol_with_no_listed_schedule_still_pays_the_perp_rate(self):
        """Not the SPOT rate. That fallback billed 16x."""
        fee, why = venue_round_trip_fee("ISEK/USD", 8_900.0, 8.9, 0.0001)
        self.assertNotIn("kraken taker", why.lower())
        self.assertIn("perpetual", why.lower())
        self.assertLess(fee / 8_900.0, 0.01)


class ProductNotLeverageTests(unittest.TestCase):
    """Spot and perpetuals are different PRODUCTS -- you trade one or the
    other. Selecting the schedule with `leverage > 1` meant that whenever
    the conviction ladder bottomed out at 1x, a perp was billed as a spot
    trade: 1.6% round trip instead of 0.10%, on a book whose whole premise
    is leveraged perps.
    """

    def test_a_perp_at_1x_is_still_a_perp(self):
        fee, why = venue_round_trip_fee("BTC/USD", 1_000.0, 1.0, 95_000.0)
        self.assertLess(fee / 1_000.0, 0.005, f"1x perp billed as spot -- {why}")

    def test_an_explicit_spot_trade_still_pays_the_spot_schedule(self):
        """The fix must not make real spot trading look cheap."""
        fee, _ = venue_round_trip_fee("BTC/USD", 1_000.0, 1.0, 95_000.0,
                                      product="spot")
        self.assertGreater(fee / 1_000.0, 0.005)

    def test_spot_costs_much_more_than_a_perp(self):
        spot, _ = venue_round_trip_fee("BTC/USD", 1_000.0, 1.0, 95_000.0, product="spot")
        perp, _ = venue_round_trip_fee("BTC/USD", 1_000.0, 1.0, 95_000.0, product="perp")
        self.assertGreater(spot, perp * 5)


class CeilingBackstopTests(unittest.TestCase):
    """Every schedule here has now been wrong in BOTH directions at least
    once, so the ceiling is enforced over all of them rather than inside
    whichever branch failed last."""

    def test_no_symbol_at_any_price_exceeds_the_ceiling(self):
        for sym in ("ISEK/USD", "ALT/USD", "DOGE/USD", "OP/USD",
                    "NOTAREALCOIN/USD", "GOOGL", "MES=F"):
            for px in (0.00001, 0.0893, 1.0, 180.0, 95_000.0):
                fee, why = venue_round_trip_fee(sym, 5_000.0, 8.0, px)
                self.assertLessEqual(fee / 5_000.0, 0.05,
                                     f"{sym} @ {px}: {fee / 50:.1f}% -- {why}")


class NoFreeLunchTests(unittest.TestCase):
    """The most dangerous possible return value is 0.0 — the one path where
    the cost model breaks is the one path that reports no cost at all. It
    used to return exactly that."""

    def test_a_broken_lookup_charges_rather_than_zeroes(self):
        fee, why = venue_round_trip_fee("!!!GARBAGE!!!", 10_000.0, 3.0, 0.0)
        self.assertGreater(fee, 0.0, f"failure path made trading free — {why}")


if __name__ == "__main__":
    unittest.main()
