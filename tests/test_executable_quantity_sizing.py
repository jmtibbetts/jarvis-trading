"""B0.2 tripwire — executable quantity must stay executable.

WRITTEN BEFORE THE IMPLEMENTATION, ON PURPOSE. These fail against the HEAD
that introduced them, which is the point: they are the development tripwire
for the sizing chain, not a confirmation written afterwards.

THE DEFECT THEY GUARD. Execution now speaks PBTCUCZ50 / CONTRACTS /
multiplier 0.01, while sizing still derives semantics from the bare symbol and
so speaks generic BTC coins at multiplier 1.0. One quantity, two meanings,
differing by 100x.

THE HAZARD THAT MAKES THEM NECESSARY. Every shrinking constraint — notional
cap, cash cap, manual margin — currently scales quantity continuously
(`qty *= scale`). Threading an exact multiplier through without re-normalising
after EACH of those produces 2.73 contracts: a number every downstream stage
agrees on, and which no stage can flag, because they all agree. A visible
seam is safer than a silent one, so these tests exist to make the silent case
impossible to ship.

CALIBRATION. A red test earns its place only when it is red for the invariant
it names. The first draft failed for four reasons that were not the sizing
defect at all — it read `d.quantity` where the canonical field is `qty`,
expected a stored `d.loss_at_stop` that RiskDecision does not carry, omitted
the `free_cash` argument `solve_position` already requires, and invented a
`cash_cap_usd` parameter where the production API expresses the ceiling as
`free_cash * max_margin_frac_of_cash`. Those were defects in the tripwire,
not in the code under test. They are removed here: what remains fails only
because the normaliser and the exact-instrument seam do not exist yet.

The cash ceiling is therefore expressed the way production expresses it. For
PBTC at 64000 with multiplier 0.01, one contract is 640 USD of notional, so a
ceiling worth 2.73 contracts is `free_cash = 2.73 * 640 = 1747.20` at
`max_margin_frac_of_cash = 1.0` and 1x. Risk permits 4 contracts there; cash
supports 2.73 continuously; the only executable answer is 2, and the margin
that results (1280.00) is strictly BELOW the 1747.20 ceiling — because a cap
is a ceiling, not an instruction to spend it.
"""
import unittest

from lib.instruments import resolve_for_execution

PERP, VENUE, CONTRACT = "CRYPTO_PERP", "kraken_derivatives_us", "PBTCUCZ50"

# One PBTC contract at 64000 is 640 USD of notional (64000 * 0.01).
UNIT_NOTIONAL = 640.0

# Financing that cannot bind. These tests are about quantity semantics, so
# leverage selection and free cash are pinned out of the way deliberately —
# a test that fails because of an unrelated financing rule proves nothing
# about the multiplier.
UNBOUNDED = dict(free_cash=1_000_000.0, max_margin_frac_of_cash=1.0,
                 requested_leverage=1.0)


def _pbtc():
    return resolve_for_execution("BTC/USD", product=PERP, venue=VENUE,
                                 instrument_id=CONTRACT)


def actual_loss_at_stop(d):
    """One arithmetic truth, derived — not a second stored field.

    RiskDecision carries the quantity, the stop distance and the unit basis;
    the loss at the stop follows from those three. Storing it as well would
    create a second place for it to be wrong.
    """
    return d.qty * d.stop_distance * d.multiplier


class NormalizerContractTests(unittest.TestCase):
    """One shrink-only authority, owned by instruments."""

    def _norm(self, qty, inst=None):
        from lib.instruments import normalize_quantity_down
        return normalize_quantity_down(qty, inst or _pbtc())

    def test_below_one_contract_floors_to_zero_not_up_to_one(self):
        """A minimum is an eligibility floor, never permission to enlarge."""
        self.assertEqual(self._norm(0.94), 0)

    def test_two_point_nine_four_floors_to_two(self):
        self.assertEqual(self._norm(2.94), 2)

    def test_an_exact_multiple_survives(self):
        self.assertEqual(self._norm(3.0), 3)

    def test_it_never_enlarges(self):
        for q in (0.0, 0.5, 0.94, 1.0, 1.999, 2.94, 7.0, 7.0000001):
            self.assertLessEqual(self._norm(q), q + 1e-9, f"enlarged {q}")

    def test_continuous_instruments_keep_fractional_quantity(self):
        spot = resolve_for_execution("BTC/USD", product="CRYPTO_SPOT")
        self.assertAlmostEqual(self._norm(1.2349, spot), 1.2349)


class ContractNativeRiskTests(unittest.TestCase):
    """Sizing must use the exact contract, not the generic symbol."""

    def _solve(self, *, budget, entry, stop, **kw):
        from lib import risk_engine
        base = dict(symbol="BTC/USD", entry=entry, stop=stop,
                    risk_budget_usd=budget, execution_instrument=_pbtc())
        base.update(UNBOUNDED)
        base.update(kw)
        return risk_engine.solve_position(**base)

    def test_the_multiplier_is_the_contract_size(self):
        # budget/(stop_distance*0.01) = 2.94 exactly
        d = self._solve(budget=2.94, entry=64000.0, stop=63900.0)
        self.assertAlmostEqual(d.multiplier, 0.01)
        self.assertEqual(d.quantity_unit, "CONTRACTS")

    def test_a_sub_contract_budget_is_rejected_not_rounded(self):
        """0.94 contracts affordable -> no executable quantity at all."""
        d = self._solve(budget=0.94, entry=64000.0, stop=63900.0)
        self.assertTrue(d.rejected)

    def test_two_point_nine_four_contracts_becomes_two(self):
        d = self._solve(budget=2.94, entry=64000.0, stop=63900.0)
        self.assertFalse(d.rejected)
        self.assertEqual(d.qty, 2)

    def test_the_economics_are_recomputed_from_the_executable_quantity(self):
        d = self._solve(budget=2.94, entry=64000.0, stop=63900.0)
        self.assertAlmostEqual(d.notional, 2 * 64000.0 * 0.01, places=6)
        self.assertAlmostEqual(actual_loss_at_stop(d), 2 * 100.0 * 0.01,
                               places=6)


class ConstraintsOnlyShrinkTests(unittest.TestCase):
    """Every cap must re-normalise; none may create a fractional contract."""

    def _solve(self, **kw):
        from lib import risk_engine
        base = dict(symbol="BTC/USD", entry=64000.0, stop=63900.0,
                    execution_instrument=_pbtc())
        base.update(UNBOUNDED)
        base.update(kw)
        return risk_engine.solve_position(**base)

    def test_a_notional_cap_cannot_produce_a_fractional_contract(self):
        """risk allows 5, notional cap allows 3.7 -> 3, never 3.7."""
        d = self._solve(risk_budget_usd=5.0,
                        notional_cap_usd=3.7 * UNIT_NOTIONAL)
        self.assertEqual(d.qty, 3)
        self.assertLessEqual(d.notional, 3.7 * UNIT_NOTIONAL + 1e-6)

    def test_a_cash_cap_cannot_produce_a_fractional_contract(self):
        """The regression that matters: qty *= scale would give 2.73.

        The ceiling is expressed the way production expresses it — free cash
        times the margin fraction — not through an invented parameter.
        """
        d = self._solve(risk_budget_usd=4.0, free_cash=2.73 * UNIT_NOTIONAL)
        self.assertEqual(d.qty, 2)

    def test_a_cash_cap_is_a_ceiling_not_an_obligation_to_spend(self):
        cap = 2.73 * UNIT_NOTIONAL          # 1747.20 at max_margin_frac 1.0
        d = self._solve(risk_budget_usd=4.0, free_cash=cap)
        self.assertLess(d.margin, cap)
        self.assertAlmostEqual(d.margin, 2 * UNIT_NOTIONAL, places=6)
        self.assertAlmostEqual(d.notional, 2 * 64000.0 * 0.01, places=6)

    def test_no_constraint_enlarges_quantity(self):
        unconstrained = self._solve(risk_budget_usd=5.0)
        for kw in ({"notional_cap_usd": 3.7 * UNIT_NOTIONAL},
                   {"free_cash": 2.73 * UNIT_NOTIONAL}):
            d = self._solve(risk_budget_usd=5.0, **kw)
            self.assertLessEqual(d.qty, unconstrained.qty)


class BothSizingPassesUseTheSameInstrumentTests(unittest.TestCase):
    """canonical_entry sizes twice — mid, then actual fill. If the second
    pass reverts to generic units it resizes the contract order wrongly."""

    def test_generic_multiplier_lookup_is_never_consulted(self):
        """Stronger than reading source: make the old path explode."""
        from lib import instruments as INST
        real = INST.get_spec

        def explode(*a, **k):
            raise AssertionError(
                "generic get_spec() consulted while an exact execution "
                "instrument was supplied")
        INST.get_spec = explode
        try:
            from lib import risk_engine
            d = risk_engine.solve_position(
                symbol="BTC/USD", entry=64000.0, stop=63900.0,
                risk_budget_usd=2.94, execution_instrument=_pbtc(),
                **UNBOUNDED)
        finally:
            INST.get_spec = real
        self.assertEqual(d.qty, 2)


if __name__ == "__main__":
    unittest.main()
