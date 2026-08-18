"""A frozen perpetual must not execute in spot units.

THE DEFECT. A canonical plan frozen as BTC/USD / CRYPTO_PERP /
kraken_derivatives_us / PBTCUCZ50 reached virtual_orders with no instrument,
so execution re-resolved the bare symbol to crypto:BTC/USD, CRYPTO_SPOT,
COINS, multiplier 1.0 — while the position's provenance recorded PBTCUCZ50.

It hides well: BTC's generic multiplier is 1.0 and the fill price still looks
sane. It is not harmless. PBTCUCZ50 has a contract size of 0.01 BTC, so one
unit of quantity means 1 BTC under one reading and 0.01 BTC under the other —
a hundredfold difference in exposure, fees and P&L, about to be frozen into a
settlement ledger.
"""
import inspect
import unittest

from lib import instruments
from lib.instruments import ExecutionIdentityRefused, resolve_for_execution

PERP, VENUE, CONTRACT = "CRYPTO_PERP", "kraken_derivatives_us", "PBTCUCZ50"


class ExactPerpUnitsTests(unittest.TestCase):
    def setUp(self):
        self.i = resolve_for_execution("BTC/USD", product=PERP, venue=VENUE,
                                       instrument_id=CONTRACT)

    def test_the_contract_is_the_instrument(self):
        self.assertEqual(self.i.instrument_id, CONTRACT)
        self.assertEqual(self.i.product, PERP)

    def test_quantity_is_contracts_not_coins(self):
        self.assertEqual(self.i.quantity_unit, "CONTRACTS")

    def test_the_multiplier_is_the_contract_size_not_one(self):
        """1.0 would overstate exposure by 100x for this contract."""
        self.assertAlmostEqual(self.i.multiplier, 0.01)
        self.assertAlmostEqual(self.i.contract_size, 0.01)
        self.assertNotEqual(self.i.multiplier, 1.0)

    def test_contracts_are_indivisible(self):
        self.assertEqual(self.i.quantity_step, 1.0)
        self.assertEqual(self.i.minimum_quantity, 1.0)

    def test_the_tick_is_the_verified_price_increment(self):
        self.assertAlmostEqual(self.i.tick_size, 5.0)

    def test_the_venue_is_the_derivative_venue(self):
        self.assertEqual(self.i.venue_family, VENUE)

    def test_a_1x_perpetual_is_still_a_perpetual_contract(self):
        """Product is never derived from leverage."""
        self.assertEqual(self.i.product, PERP)
        self.assertEqual(self.i.quantity_unit, "CONTRACTS")


class RefusalTests(unittest.TestCase):
    def test_a_mismatched_frozen_contract_is_refused(self):
        with self.assertRaises(ExecutionIdentityRefused):
            resolve_for_execution("BTC/USD", product=PERP, venue=VENUE,
                                  instrument_id="WRONG_CONTRACT")

    def test_an_unverified_price_scale_cannot_execute(self):
        """SHIB stays fail-closed; no guessed 1000x."""
        with self.assertRaises(ExecutionIdentityRefused):
            resolve_for_execution("SHIB/USD", product=PERP, venue=VENUE)

    def test_an_unlisted_perp_is_refused_not_downgraded_to_spot(self):
        with self.assertRaises(ExecutionIdentityRefused):
            resolve_for_execution("NEAR/USD", product=PERP, venue=VENUE)


class GenericBehaviourUnchangedTests(unittest.TestCase):
    def test_the_bare_symbol_still_resolves_to_spot(self):
        """resolve() keeps its watchlist meaning; only execution is exact."""
        g = instruments.resolve("BTC/USD")
        self.assertEqual(g.product, "CRYPTO_SPOT")
        self.assertEqual(g.quantity_unit, "COINS")

    def test_spot_execution_is_unchanged(self):
        e = resolve_for_execution("BTC/USD", product="CRYPTO_SPOT")
        self.assertEqual(e.quantity_unit, "COINS")
        self.assertAlmostEqual(e.multiplier, 1.0)

    def test_the_two_answers_genuinely_differ(self):
        """The contradiction this exists to remove."""
        spot = instruments.resolve("BTC/USD")
        perp = resolve_for_execution("BTC/USD", product=PERP,
                                     instrument_id=CONTRACT)
        self.assertNotEqual(spot.quantity_unit, perp.quantity_unit)
        self.assertNotEqual(spot.multiplier, perp.multiplier)
        self.assertNotEqual(spot.instrument_id, perp.instrument_id)


class VenueBoundaryTests(unittest.TestCase):
    def test_submit_accepts_an_exact_instrument(self):
        from lib.execution_venue import VirtualCexAdapter
        self.assertIn("instrument",
                      inspect.signature(VirtualCexAdapter.submit).parameters)

    def test_the_instrument_reaches_the_fill_model(self):
        """Without this, virtual_orders re-resolves and spot semantics win."""
        import pathlib
        src = pathlib.Path("lib/execution_venue.py").read_text()
        self.assertIn("execute_market(order, quote, instrument=instrument)",
                      src)


if __name__ == "__main__":
    unittest.main()
