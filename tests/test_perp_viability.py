"""A product existing is not the same as it being tradeable.

Kraken US lists a SHIB perpetual. It is still NO_TRADE, because a fixed
per-contract fee does not scale away:

    contract_notional  = 100,000 SHIB x $0.00000447 = $0.447
    round_trip_fee     = $0.15 x 2                  = $0.30
    effective fee      = 67.11% of notional

Unlike a percentage schedule, there is no position size at which this
improves -- buying more buys more contracts, each carrying the same fixed
charge. And unlike a spread cost, no stop distance dilutes it either. So
the rejection belongs BEFORE the signal is built, not in sizing.
"""
import os
import unittest
from unittest import mock

from lib.venues import (us_perp_viability, us_perp_spec, us_perp_fee,
                        us_perp_contracts, US_PERP_CONTRACTS,
                        MAX_VIABLE_FEE_PCT_OF_NOTIONAL)


# Every test here exercises Kraken Pro US pricing, which is scoped to that
# venue by design — the per-contract schedule describes no other venue. The
# environment is therefore stated rather than inherited from a developer's
# .env, so these do not silently pass or fail on local configuration.
_KRAKEN_US = {"VENUE_REGION": "us", "PAPER_VENUE": "kraken"}


def _kraken_us(testcase):
    patch = mock.patch.dict(os.environ, _KRAKEN_US)
    patch.start()
    testcase.addCleanup(patch.stop)


class ViabilityTests(unittest.TestCase):

    def setUp(self):
        _kraken_us(self)

    def test_shib_is_no_trade(self):
        v = us_perp_viability("SHIB/USD", 0.00000447)
        self.assertEqual(v["decision"], "NO_TRADE")
        self.assertFalse(v["tradeable"])
        self.assertAlmostEqual(v["contract_notional"], 0.447, places=3)
        self.assertAlmostEqual(v["round_trip_fee_pct"], 67.114, places=2)

    def test_the_reason_names_the_cause(self):
        v = us_perp_viability("SHIB/USD", 0.00000447)
        self.assertIn("unacceptable percentage", v["reason"])

    def test_every_other_listed_perpetual_is_tradeable(self):
        prices = {"BTC": 95_000.0, "ETH": 3_200.0, "SOL": 180.0, "XRP": 2.1,
                  "AAVE": 260.0, "AVAX": 22.0, "BCH": 520.0, "ADA": 0.62,
                  "LINK": 18.0, "DOGE": 0.16, "HBAR": 0.19, "LTC": 95.0,
                  "DOT": 4.1, "XLM": 0.31, "XTZ": 0.75, "TRX": 0.29}
        for asset, px in prices.items():
            v = us_perp_viability(f"{asset}/USD", px)
            self.assertEqual(v["decision"], "TRADEABLE",
                             f"{asset}: {v['reason']}")

    def test_viability_does_not_depend_on_position_size(self):
        """That is the whole point -- a fixed per-contract cost is
        scale-invariant, so the answer cannot be sized around."""
        import inspect
        params = inspect.signature(us_perp_viability).parameters
        self.assertNotIn("notional", params)
        self.assertNotIn("quantity", params)

    def test_the_limit_is_expressed_in_percent(self):
        v = us_perp_viability("SHIB/USD", 0.00000447)
        self.assertEqual(v["limit_pct"], MAX_VIABLE_FEE_PCT_OF_NOTIONAL)


class ScaleInvarianceTests(unittest.TestCase):
    """Why NO_TRADE rather than "trade it bigger"."""

    def setUp(self):
        _kraken_us(self)

    def test_the_fee_percentage_is_identical_at_every_size(self):
        rates = []
        for tokens in (100_000, 1_000_000, 100_000_000, 10_000_000_000):
            notional = tokens * 0.00000447
            fee, _ = us_perp_fee("SHIB/USD", notional, 0.00000447)
            rates.append(round(fee / notional, 6))
        self.assertEqual(len(set(rates)), 1, rates)

    def test_a_bigger_position_costs_proportionally_more(self):
        small, _ = us_perp_fee("SHIB/USD", 447.0, 0.00000447)
        large, _ = us_perp_fee("SHIB/USD", 44_700.0, 0.00000447)
        self.assertAlmostEqual(large, small * 100, places=4)


class ProductCodeTests(unittest.TestCase):
    """Fixed futures sit beside perpetuals with multipliers differing 5x-20x:

        SOL  perp 5      fixed 100
        ETH  perp 0.5    fixed 0.1
        XRP  perp 500    fixed 100
        DOGE perp 5,000  fixed 100,000

    A bare {"SOL": 5.0} cannot be audited against the rulebook. The product
    code is what proves the row came from the perpetual table.
    """

    def setUp(self):
        _kraken_us(self)

    def test_every_entry_carries_its_product_code(self):
        for asset, spec in US_PERP_CONTRACTS.items():
            self.assertIn("product_code", spec, asset)
            self.assertTrue(spec["product_code"].startswith("P"), asset)

    def test_every_entry_declares_whether_it_is_verified(self):
        for asset, spec in US_PERP_CONTRACTS.items():
            self.assertIn("verified", spec, asset)

    def test_an_unverified_spec_is_not_priced_exactly(self):
        spec = dict(US_PERP_CONTRACTS["SOL"], verified=False)
        with mock.patch.dict(US_PERP_CONTRACTS, {"SOL": spec}):
            n, why = us_perp_contracts("SOL/USD", 8_900.0, 180.0)
            self.assertIsNone(n)
            self.assertIn("unverified", why)

    def test_the_perpetual_multipliers_are_not_the_fixed_future_ones(self):
        """The specific rows most easily confused."""
        self.assertEqual(US_PERP_CONTRACTS["SOL"]["contract_size"], 5.0)
        self.assertEqual(US_PERP_CONTRACTS["ETH"]["contract_size"], 0.5)
        self.assertEqual(US_PERP_CONTRACTS["XRP"]["contract_size"], 500.0)
        self.assertEqual(US_PERP_CONTRACTS["DOGE"]["contract_size"], 5_000.0)

    def test_the_fee_rate_travels_with_the_contract(self):
        for asset, spec in US_PERP_CONTRACTS.items():
            self.assertEqual(spec["fee_per_contract_per_side"], 0.15, asset)


class ContractCountTests(unittest.TestCase):
    """contracts = ceil(requested_underlying / contract_size)"""

    def setUp(self):
        _kraken_us(self)

    def test_the_count_is_the_formula(self):
        import math
        for notional, px, asset, size in (
            (9_500.0, 95_000.0, "BTC", 0.01),
            (8_900.0, 180.0, "SOL", 5.0),
            (1_000.0, 0.62, "ADA", 5_000.0),
        ):
            n, _ = us_perp_contracts(f"{asset}/USD", notional, px)
            self.assertEqual(n, max(1.0, math.ceil((notional / px) / size)), asset)

    def test_fractional_contracts_are_never_returned(self):
        for asset, px in (("BTC", 95_000.0), ("SOL", 180.0), ("ADA", 0.62)):
            n, _ = us_perp_contracts(f"{asset}/USD", 137.0, px)
            self.assertEqual(n, int(n), asset)


class SignalGateTests(unittest.TestCase):
    """The rejection has to happen before a signal exists."""

    def setUp(self):
        _kraken_us(self)

    def test_an_unviable_instrument_is_dropped_at_construction(self):
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "SHIB/USD", "direction": "Long",
               "entry_price": 0.00000447, "stop_loss": 0.00000430,
               "target_price": 0.00000500}
        with mock.patch.dict(os.environ, _KRAKEN_US):
            out, ok, reason = clamp_stop_to_atr(sig, atr_pct=3.0)
        self.assertFalse(ok)
        self.assertIn("NO_TRADE", reason)

    def test_a_viable_instrument_is_not_dropped_by_this_gate(self):
        from lib.signal_levels import clamp_stop_to_atr
        sig = {"asset_symbol": "BTC/USD", "direction": "Long",
               "entry_price": 95_000.0, "stop_loss": 92_000.0,
               "target_price": 101_000.0}
        with mock.patch.dict(os.environ, _KRAKEN_US):
            out, ok, reason = clamp_stop_to_atr(sig, atr_pct=3.0)
        self.assertNotIn("NO_TRADE", str(reason))


if __name__ == "__main__":
    unittest.main()
