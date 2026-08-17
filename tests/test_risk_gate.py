"""P0 — the last gate before submission.

`OrderPlan.within(RiskDecision)` compared quantity and notional and nothing
else. Measured against the real types before this change, all four of these
were AUTHORIZED:

    NaN quantity                    every comparison against NaN is False,
                                    so a non-number satisfies every ceiling
    NaN qty on a REJECTED decision  a refusal that authorized an order
    50x leverage vs approved 2x     leverage was never checked
    stop widened 1.0 -> 50.0        same size, fifty times the money at risk

Quantity alone does not bound loss. Loss is quantity TIMES stop distance
TIMES the contract multiplier, and the old gate checked one of the three.
"""
import math
import unittest

from lib.decision_types import (OrderPlan, RiskDecision,
                                normalized_stays_within)

APPROVED = RiskDecision(allowed_risk_usd=100.0, stop_distance=1.0, qty=10.0,
                        notional=1000.0, margin=500.0, leverage=2.0)


def plan(**over):
    base = dict(symbol="AAPL", venue="virtual_cex", side="long",
                order_type="market", qty=10.0, entry=100.0,
                initial_stop=99.0, notional=1000.0, leverage=1.0)
    base.update(over)
    return OrderPlan(**base)


class ARefusalCanNeverAuthorize(unittest.TestCase):
    def test_a_rejected_decision_blocks_a_perfectly_ordinary_order(self):
        v = plan(qty=1.0, notional=100.0).check(RiskDecision.rejection("no edge"))
        self.assertFalse(v.ok)
        self.assertIn("REJECTED", v.reason)

    def test_the_rejection_reason_travels_to_the_gate(self):
        v = plan().check(RiskDecision.rejection("insufficient cash"))
        self.assertIn("insufficient cash", v.reason)

    def test_it_does_not_rely_on_a_rejected_decision_carrying_zero_qty(self):
        """The old guarantee was an accident of the constructor. A rejected
        decision with a non-zero qty must still refuse."""
        odd = RiskDecision(allowed_risk_usd=1e9, stop_distance=100.0,
                           qty=1e9, notional=1e9, margin=1e9, leverage=100.0,
                           rejected=True, rejection_reason="refused")
        self.assertFalse(plan().check(odd).ok)


class NotANumberIsNotASize(unittest.TestCase):
    def test_nan_quantity_is_refused(self):
        v = plan(qty=float("nan")).check(APPROVED)
        self.assertFalse(v.ok)
        self.assertIn("finite", v.reason)

    def test_infinite_quantity_is_refused(self):
        self.assertFalse(plan(qty=math.inf).check(APPROVED).ok)

    def test_nan_notional_is_refused(self):
        self.assertFalse(plan(notional=float("nan")).check(APPROVED).ok)

    def test_nan_leverage_is_refused(self):
        self.assertFalse(plan(leverage=float("nan")).check(APPROVED).ok)

    def test_nan_stop_is_refused(self):
        self.assertFalse(plan(initial_stop=float("nan")).check(APPROVED).ok)

    def test_zero_and_negative_quantities_are_refused(self):
        self.assertFalse(plan(qty=0.0).check(APPROVED).ok)
        self.assertFalse(plan(qty=-1.0).check(APPROVED).ok)


class TheApprovedCeilings(unittest.TestCase):
    def test_an_order_inside_every_ceiling_passes(self):
        self.assertTrue(plan(qty=7.0, notional=700.0).check(APPROVED).ok)

    def test_quantity_enlargement_is_refused(self):
        self.assertFalse(plan(qty=10.5, notional=1050.0).check(APPROVED).ok)

    def test_notional_enlargement_is_refused(self):
        self.assertFalse(plan(qty=10.0, notional=1100.0).check(APPROVED).ok)

    def test_leverage_enlargement_is_refused(self):
        v = plan(leverage=50.0).check(APPROVED)
        self.assertFalse(v.ok)
        self.assertIn("leverage", v.reason)

    def test_a_widened_stop_is_refused_even_at_the_approved_quantity(self):
        """The subtlest hole. Quantity is untouched and within its ceiling;
        the money at risk is fifty times what was approved."""
        v = plan(initial_stop=50.0).check(APPROVED)
        self.assertFalse(v.ok)
        self.assertIn("WIDER", v.reason)

    def test_a_tightened_stop_is_allowed(self):
        self.assertTrue(plan(initial_stop=99.5).check(APPROVED).ok)


class RiskIsPricedThroughTheMultiplier(unittest.TestCase):
    def test_a_futures_stop_is_priced_per_contract_not_per_point(self):
        """A 10-point MES stop is $50 of risk per contract, not $10. A gate
        that priced it as $10 would authorize five times the risk."""
        approved = RiskDecision(allowed_risk_usd=100.0, stop_distance=10.0,
                                qty=2.0, notional=100_000.0, margin=2640.0,
                                leverage=1.0)
        # 2 contracts x 10 points x $5/point = $100 — exactly at the limit.
        self.assertTrue(plan(symbol="MES=F", product="INDEX_FUTURE", qty=2.0,
                             entry=5000.0, initial_stop=4990.0,
                             notional=50_000.0).check(approved).ok)
        # 3 contracts is $150 against a $100 budget.
        v = plan(symbol="MES=F", product="INDEX_FUTURE", qty=3.0,
                 entry=5000.0, initial_stop=4990.0,
                 notional=75_000.0).check(
            RiskDecision(allowed_risk_usd=100.0, stop_distance=10.0, qty=5.0,
                         notional=1e9, margin=1e9, leverage=1.0))
        self.assertFalse(v.ok)
        self.assertIn("risk at stop", v.reason)

    def test_an_unresolvable_instrument_is_refused_not_assumed(self):
        """6J=F has no verified contract spec. An instrument whose units
        are unknown cannot be risk-checked, and refusing is the point."""
        v = plan(symbol="6J=F", product="UNKNOWN", qty=1.0,
                 entry=100.0, initial_stop=99.0).check(APPROVED)
        self.assertFalse(v.ok)


class IdentityMustBeKnown(unittest.TestCase):
    def test_an_unparseable_side_is_refused(self):
        for bad in ("sideways", "", "?", "flat"):
            self.assertFalse(plan(side=bad).check(APPROVED).ok, bad)

    def test_canonical_sides_are_accepted(self):
        # Prefix forms are deliberate: real broker strings are "Long_5x",
        # "Short_Leveraged", "buy". The strict parser handles those, and
        # "Short_10x" silently keeping a LONG sign is the bug it exists for.
        for good in ("long", "short", "Long", "SHORT", "buy", "sell",
                     "Long_Leveraged", "Short_5x"):
            self.assertTrue(plan(side=good, qty=1.0, notional=100.0)
                            .check(APPROVED).ok, good)


class AVenueMayShrinkRiskNeverEnlargeIt(unittest.TestCase):
    TIGHT = RiskDecision(allowed_risk_usd=100.0, stop_distance=1.0, qty=0.010,
                         notional=1000.0, margin=500.0, leverage=2.0)

    def test_rounding_up_to_a_venue_minimum_is_refused(self):
        """approved 0.006, venue minimum 0.010 — submitting the minimum
        takes 67% more risk than was authorized."""
        v = normalized_stays_within(plan(qty=0.006, notional=600.0),
                                    plan(qty=0.010, notional=1000.0),
                                    self.TIGHT)
        self.assertFalse(v.ok)
        self.assertIn("ENLARGED quantity", v.reason)

    def test_rounding_down_is_allowed(self):
        v = normalized_stays_within(plan(qty=0.010, notional=1000.0),
                                    plan(qty=0.006, notional=600.0),
                                    self.TIGHT)
        self.assertTrue(v.ok)

    def test_contract_rounding_up_is_refused(self):
        v = normalized_stays_within(plan(qty=1.4, notional=140.0),
                                    plan(qty=2.0, notional=200.0),
                                    RiskDecision(allowed_risk_usd=1e6,
                                                 stop_distance=1.0, qty=2.0,
                                                 notional=1e6, margin=1e6,
                                                 leverage=2.0))
        self.assertFalse(v.ok)

    def test_normalization_that_widens_the_stop_is_refused(self):
        v = normalized_stays_within(plan(initial_stop=99.0),
                                    plan(initial_stop=95.0),
                                    RiskDecision(allowed_risk_usd=1e6,
                                                 stop_distance=100.0, qty=10.0,
                                                 notional=1e6, margin=1e6,
                                                 leverage=2.0))
        self.assertFalse(v.ok)
        self.assertIn("WIDENED the stop", v.reason)

    def test_normalization_that_enlarges_leverage_is_refused(self):
        v = normalized_stays_within(plan(leverage=1.0), plan(leverage=2.0),
                                    APPROVED)
        self.assertFalse(v.ok)

    def test_the_normalized_order_is_re_checked_against_risk_itself(self):
        """Not merely compared to the original: a venue could shrink
        against the plan and still breach the approved ceiling."""
        v = normalized_stays_within(plan(qty=50.0, notional=5000.0),
                                    plan(qty=20.0, notional=2000.0),
                                    APPROVED)
        self.assertFalse(v.ok)


class TheVerdictSaysWhy(unittest.TestCase):
    def test_a_refusal_names_the_ceiling_it_breached(self):
        v = plan(qty=99.0, leverage=99.0, initial_stop=1.0).check(APPROVED)
        self.assertFalse(v.ok)
        self.assertGreaterEqual(len(v.failures), 2)

    def test_a_pass_carries_no_failures(self):
        v = plan(qty=1.0, notional=100.0).check(APPROVED)
        self.assertTrue(v.ok)
        self.assertIsNone(v.reason)

    def test_within_still_returns_a_plain_bool_for_existing_callers(self):
        self.assertIs(plan(qty=1.0, notional=100.0).within(APPROVED), True)
        self.assertIs(plan(qty=99.0).within(APPROVED), False)


if __name__ == "__main__":
    unittest.main()
