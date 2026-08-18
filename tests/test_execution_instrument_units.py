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


class ExecutableStatusAndAgreementTests(unittest.TestCase):
    """A1/A2/A3 — the identity must be truthful, and agreement is required on
    every axis the caller already froze."""

    def test_a_verified_contract_is_actually_executable(self):
        """An object described as the exact executable identity must not be
        unexecutable merely because status defaulted."""
        i = resolve_for_execution("BTC/USD", product=PERP, venue=VENUE,
                                  instrument_id=CONTRACT)
        self.assertEqual(i.status, instruments.VERIFIED)
        self.assertTrue(i.executable)
        i.require_executable()

    def test_a_frozen_venue_is_authority_not_a_suggestion(self):
        """Preferring the spec's venue would silently relocate an execution
        the desk had already committed elsewhere."""
        with self.assertRaises(ExecutionIdentityRefused):
            resolve_for_execution("BTC/USD", product=PERP, venue="kraken",
                                  instrument_id=CONTRACT)

    def test_equity_short_is_not_flattened_to_spot(self):
        """resolve(symbol) alone answers what a symbol USUALLY means, losing
        the distinction the caller had already made."""
        self.assertEqual(
            resolve_for_execution("AMD", product="EQUITY_SHORT").product,
            "EQUITY_SHORT")

    def test_crypto_spot_stays_crypto_spot(self):
        self.assertEqual(
            resolve_for_execution("BTC/USD", product="CRYPTO_SPOT").product,
            "CRYPTO_SPOT")


if __name__ == "__main__":
    unittest.main()


class CanonicalEntryActuallyCarriesItTests(unittest.TestCase):
    """A parameter existing proves nothing. The CALL GRAPH must carry it.

    This project has now shipped four defects of the shape "signature exists,
    body exists, caller never passes the value": the conflict guard reading a
    field that was never populated, the funnel signature missing its
    parameter, snapshot.instrument_id assigned by no reader, and this one —
    canonical_entry never calling resolve_for_execution at all.
    """

    def test_canonical_entry_resolves_the_exact_instrument(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("lib/canonical_entry.py").read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        self.assertIn("resolve_for_execution", called)

    def test_it_passes_a_non_null_instrument_to_the_venue(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("lib/canonical_entry.py").read_text())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "submit"):
                kws = {k.arg: k.value for k in n.keywords}
                self.assertIn("instrument", kws,
                              "EV.submit called without the exact instrument")
                # and it must be the resolved object, not a literal None
                self.assertNotIsInstance(kws["instrument"], ast.Constant)
                return
        self.fail("no EV.submit call found in canonical_entry")

    def test_the_fill_model_does_not_re_resolve_when_given_an_instrument(self):
        """The behavioural half: with an exact instrument supplied,
        virtual_orders must not consult the generic resolver at all."""
        from lib import instruments as INST
        from lib import virtual_orders as VO

        exact = INST.resolve_for_execution(
            "BTC/USD", product=PERP, venue=VENUE, instrument_id=CONTRACT)

        real = INST.resolve
        def explode(*a, **k):
            raise AssertionError(
                "generic instruments.resolve() was called while an exact "
                "execution instrument was supplied")
        INST.resolve = explode
        try:
            order = VO.VirtualOrder(symbol="BTC/USD", side="long",
                                    quantity=3.0, order_type="market")
            quote = VO.Quote(bid=64000.0, ask=64010.0, as_of=None,
                             source="test")
            res = VO.execute_market(order, quote, instrument=exact)
        finally:
            INST.resolve = real

        # ExecutionResult carries the UNITS, which is what settlement needs.
        self.assertEqual(res.quantity_unit, "CONTRACTS")
        self.assertAlmostEqual(res.multiplier, 0.01)
