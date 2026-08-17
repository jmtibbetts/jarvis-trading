"""The two confirmed accounting defects, and the identity that replaces them.

Both were verified in the live code, not inferred:

  PARTIAL EXITS PAY NO FUNDING. `_funding_cost_usd()` derives its interval
  from `age_minutes(opened_at)` — always to NOW — and the partial-close path
  never calls it. Final close then charges funding on the REMAINING quantity
  only, so anything scaled out holds for days and pays nothing.

  A SCALE-OUT VOTES TWICE. Partial close increments total_trades and
  winning_trades and records a learning outcome; final close does it all
  again. ONE THESIS DOES NOT VOTE TWICE.

The canonical model separates an estimate from a charge, and the tests below
assert cash conservation directly rather than arguing for it.
"""
import unittest
from unittest.mock import patch

from lib import paper_settlement as S


class TheTwoCostModelsAreNotVersionsOfOneIdeaTests(unittest.TestCase):

    def test_legacy_charges_no_fee_at_entry(self):
        """Its stored `fees` is a DEFERRED round-trip charge, and no fee
        dollars leave cash at entry. Only margin moves."""
        delta = S.settle_entry(committed_margin_usd=1000.0, entry_fee_usd=0.0,
                               cost_model=S.COST_MODEL_LEGACY)
        self.assertEqual(delta, -1000.0)

    def test_legacy_refuses_an_entry_fee_rather_than_quietly_taking_one(self):
        """Charging one here plus the stored round-trip at close is exactly
        the double-charge this separation exists to prevent."""
        with self.assertRaises(ValueError) as ctx:
            S.settle_entry(committed_margin_usd=1000.0, entry_fee_usd=8.0,
                           cost_model=S.COST_MODEL_LEGACY)
        self.assertIn("double-charge", str(ctx.exception))

    def test_canonical_charges_the_entry_fee_at_entry(self):
        delta = S.settle_entry(committed_margin_usd=1000.0, entry_fee_usd=8.0,
                               cost_model=S.COST_MODEL_CANONICAL)
        self.assertEqual(delta, -1008.0)

    def test_the_legacy_prorata_share_is_preserved_exactly(self):
        """`fees` cannot be decomposed into entry and exit halves after the
        fact; inventing that split would be fabrication."""
        self.assertAlmostEqual(S.legacy_close_fee(20.0, 3.0, 10.0), 6.0)
        self.assertAlmostEqual(S.legacy_close_fee(20.0, 10.0, 10.0), 20.0)

    def test_the_execution_models_are_distinct_identifiers(self):
        """Pooling outcomes across the boundary would compare two different
        simulators."""
        self.assertNotEqual(S.EXECUTION_MODEL_LEGACY, S.EXECUTION_MODEL_CANONICAL)
        self.assertNotEqual(S.COST_MODEL_LEGACY, S.COST_MODEL_CANONICAL)


class FundingIsChargedForTheIntervalActuallyHeldTests(unittest.TestCase):
    """THE DEFECT: a scale-out held for days and paid nothing."""

    def _funding(self, hours, notional=10_000.0, is_short=False):
        # 0.01% per 8h, the documented standard baseline.
        with patch("lib.transaction_costs.funding_cost_pct",
                   lambda sym, hrs, is_short=False: (0.0001 * (hrs / 8.0), "test")):
            return S.funding_for_interval("BTC/USD", notional, is_short, hours)

    def test_funding_scales_with_the_interval(self):
        self.assertAlmostEqual(self._funding(8.0), 1.0, places=6)
        self.assertAlmostEqual(self._funding(24.0), 3.0, places=6)

    def test_a_zero_or_negative_interval_costs_nothing(self):
        self.assertEqual(self._funding(0.0), 0.0)
        self.assertEqual(self._funding(-5.0), 0.0)

    def test_the_scaled_out_quantity_pays_for_its_own_holding_period(self):
        """The whole point. 4 units leave at 24h; 6 remain to 72h. Each
        pays for the time IT was on, and neither pays for the other's."""
        early = self._funding(24.0, notional=4_000.0)
        late = self._funding(72.0, notional=6_000.0)
        self.assertGreater(early, 0.0, "the scaled-out quantity must pay")
        self.assertGreater(late, early)
        # Under the old model `early` was simply 0.0.
        self.assertNotEqual(early, 0.0)

    def test_funding_may_be_negative_and_improve_a_short(self):
        """It is a TRANSFER. A model that always charges it as a cost
        systematically understates every short."""
        with patch("lib.transaction_costs.funding_cost_pct",
                   lambda sym, hrs, is_short=False: (-0.0002, "test")):
            got = S.funding_for_interval("BTC/USD", 10_000.0, True, 8.0)
        self.assertLess(got, 0.0)

    def test_the_interval_is_explicit_rather_than_read_from_the_clock(self):
        """A wall-clock interval cannot be replayed in a test, which is part
        of why the defect survived."""
        import inspect
        params = inspect.signature(S.funding_for_interval).parameters
        self.assertIn("hours_held", params)


class CashConservationTests(unittest.TestCase):
    """final_cash = starting_cash + net_pnl. Asserted, not argued."""

    def _position(self):
        """10 units long, $1,000 margin, $8 entry fee.
        Scale out 4 at +$400 gross after 24h; close 6 at +$300 after 72h."""
        s = S.PositionSettlement(position_id="p1", committed_margin_usd=1000.0,
                                 entry_fee_usd=8.0)
        s.add(S.SettlementLeg(kind=S.LEG_PARTIAL_EXIT, quantity=4.0,
                              fill_price=110.0, gross_pnl_usd=400.0,
                              explicit_fee_usd=4.0, funding_usd=1.0,
                              released_margin_usd=400.0, hours_held=24.0))
        s.add(S.SettlementLeg(kind=S.LEG_FINAL_EXIT, quantity=6.0,
                              fill_price=105.0, gross_pnl_usd=300.0,
                              explicit_fee_usd=6.0, funding_usd=4.5,
                              released_margin_usd=600.0, hours_held=72.0))
        return s

    def test_every_explicit_cost_is_counted_exactly_once(self):
        s = self._position()
        self.assertEqual(s.entry_fee_usd, 8.0)
        self.assertEqual(s.exit_fees_usd, 10.0)      # 4 + 6
        self.assertEqual(s.funding_usd, 5.5)         # 1.0 + 4.5
        self.assertEqual(s.total_explicit_cost_usd, 23.5)

    def test_net_is_gross_minus_every_explicit_cost(self):
        s = self._position()
        self.assertEqual(s.gross_pnl_usd, 700.0)
        self.assertEqual(s.net_pnl_usd, 700.0 - 23.5)

    def test_the_account_moves_by_exactly_the_net(self):
        """The conservation identity. Margin is returned, so it must cancel
        out entirely and leave only the economics behind."""
        s = self._position()
        self.assertAlmostEqual(s.cash_delta_total(), s.net_pnl_usd, places=9)

    def test_margin_is_fully_released_across_the_legs(self):
        s = self._position()
        released = sum(l.released_margin_usd for l in s.exit_legs)
        self.assertAlmostEqual(released, s.committed_margin_usd)

    def test_spread_and_slippage_are_not_charged_again(self):
        """They are already inside the fill prices, so gross carries them.
        Only commissions and funding appear as explicit costs."""
        s = self._position()
        self.assertEqual(s.total_explicit_cost_usd,
                         s.entry_fee_usd + s.exit_fees_usd + s.funding_usd)

    def test_return_percent_uses_committed_margin_not_r_and_not_notional(self):
        """$50 net on $1,000 committed margin is 5%. R IS NOT PERCENT."""
        s = S.PositionSettlement(committed_margin_usd=1000.0, entry_fee_usd=0.0)
        s.add(S.SettlementLeg(kind=S.LEG_FINAL_EXIT, quantity=10.0,
                              gross_pnl_usd=50.0, released_margin_usd=1000.0))
        self.assertAlmostEqual(s.return_pct(), 5.0)
        self.assertNotAlmostEqual(s.return_pct(), 50.0)

    def test_an_unknown_margin_yields_no_percentage_rather_than_zero(self):
        s = S.PositionSettlement(committed_margin_usd=0.0)
        self.assertIsNone(s.return_pct())


class LegsAreLedgerRowsNotStrategyVotesTests(unittest.TestCase):
    """A scale-out became trade #1 and trade #2, and two learning
    observations, purely because it scaled out."""

    def test_a_position_with_three_legs_is_still_one_position(self):
        s = S.PositionSettlement(committed_margin_usd=1000.0, entry_fee_usd=5.0)
        for kind in (S.LEG_PARTIAL_EXIT, S.LEG_PARTIAL_EXIT, S.LEG_FINAL_EXIT):
            s.add(S.SettlementLeg(kind=kind, quantity=2.0, gross_pnl_usd=10.0,
                                  released_margin_usd=333.3333333))
        self.assertEqual(len(s.exit_legs), 3)
        # One aggregate, from which learning votes once.
        self.assertAlmostEqual(s.gross_pnl_usd, 30.0)
        self.assertAlmostEqual(s.closed_quantity, 6.0)

    def test_the_entry_leg_is_never_counted_as_a_realisation(self):
        s = S.PositionSettlement(committed_margin_usd=100.0)
        s.add(S.SettlementLeg(kind=S.LEG_ENTRY, quantity=10.0, fill_price=10.0))
        s.add(S.SettlementLeg(kind=S.LEG_FINAL_EXIT, quantity=10.0,
                              gross_pnl_usd=25.0, released_margin_usd=100.0))
        self.assertEqual(len(s.exit_legs), 1)
        self.assertEqual(s.gross_pnl_usd, 25.0)

    def test_incremental_cash_and_the_aggregate_agree(self):
        """ACCOUNTING realises leg by leg; LEARNING votes once. The two
        views must reconcile, which is why they may not share a counter."""
        s = S.PositionSettlement(committed_margin_usd=1000.0, entry_fee_usd=8.0)
        s.add(S.SettlementLeg(kind=S.LEG_PARTIAL_EXIT, quantity=5.0,
                              gross_pnl_usd=200.0, explicit_fee_usd=3.0,
                              funding_usd=1.0, released_margin_usd=500.0))
        s.add(S.SettlementLeg(kind=S.LEG_FINAL_EXIT, quantity=5.0,
                              gross_pnl_usd=-50.0, explicit_fee_usd=3.0,
                              funding_usd=2.0, released_margin_usd=500.0))
        incremental = sum(l.cash_delta for l in s.exit_legs)
        entry_move = -(s.committed_margin_usd + s.entry_fee_usd)
        self.assertAlmostEqual(incremental + entry_move, s.net_pnl_usd, places=9)

    def test_a_losing_final_leg_after_a_winning_partial_nets_correctly(self):
        s = S.PositionSettlement(committed_margin_usd=1000.0, entry_fee_usd=0.0)
        s.add(S.SettlementLeg(kind=S.LEG_PARTIAL_EXIT, quantity=5.0,
                              gross_pnl_usd=200.0, released_margin_usd=500.0))
        s.add(S.SettlementLeg(kind=S.LEG_FINAL_EXIT, quantity=5.0,
                              gross_pnl_usd=-260.0, released_margin_usd=500.0))
        self.assertAlmostEqual(s.net_pnl_usd, -60.0)
        # The position LOST, even though its first leg won — which is the
        # judgement a per-leg vote would have got backwards.
        self.assertLess(s.net_pnl_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
