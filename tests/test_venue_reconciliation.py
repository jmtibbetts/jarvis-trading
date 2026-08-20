"""An incomplete observation cannot be made to confess a realized number.

The first real-world fixture is a BTCC LINKUSDT perpetual observed
MID-FLIGHT: entry 10.727, observed ~10.692, displayed P&L ~-1.99 against a
raw price P&L of ~-1.75. The -0.24 gap has a suspicious twin — the dialog
separately displayed an estimated closing fee of ~0.24 — and the entire
point of these tests is that similarity of magnitude is NOT identity of
cause. Until realized-history evidence proves the venue's accounting, the
gap stays UNEXPLAINED_VENUE_COST, funding stays UNKNOWN, and every attempt
to extract a realized quantity raises instead of guessing.
"""
import unittest

from lib import venue_reconciliation as VR


class TheFixtureIsWhatItClaimsToBeTests(unittest.TestCase):

    def setUp(self):
        self.obs = VR.btcc_linkusdt_observation()

    def test_it_is_ui_evidence_of_an_incomplete_lifecycle(self):
        self.assertEqual(self.obs.evidence_type, VR.OBSERVED_UI)
        self.assertEqual(self.obs.lifecycle, VR.INCOMPLETE_LIFECYCLE)

    def test_it_carries_no_account_identifiers(self):
        d = str(self.obs.as_dict()).lower()
        for banned in ("api_key", "account_id", "token", "secret"):
            self.assertNotIn(banned, d)

    def test_the_timezone_is_not_assumed(self):
        self.assertIsNone(self.obs.venue_timezone_if_known)
        self.assertIn("UNPROVEN", self.obs.opened_at)

    def test_the_hold_duration_is_bounded_not_exact(self):
        self.assertIsInstance(self.obs.hold_duration_hours, str)
        self.assertIn("6-8", self.obs.hold_duration_hours)


class RawArithmeticIsPermittedTests(unittest.TestCase):
    """Computing on observed facts is fine; claiming accounting is not."""

    def setUp(self):
        self.obs = VR.btcc_linkusdt_observation()

    def test_raw_price_pnl_matches_the_hand_calculation(self):
        raw = self.obs.raw_price_pnl_at(10.692)
        self.assertAlmostEqual(raw, 50 * (10.692 - 10.727), places=6)
        self.assertAlmostEqual(raw, -1.75, places=2)

    def test_the_display_is_consistent_with_return_on_margin(self):
        """-1.99 / 10.73 is about -18.55%. That verifies the DISPLAY
        CONVENTION (return on margin), not which costs are inside it."""
        c = self.obs.display_consistency()
        self.assertTrue(c["consistent_with_return_on_margin"])
        self.assertAlmostEqual(c["implied_return_on_margin_pct"], -18.55,
                               delta=0.2)

    def test_the_gap_is_measured_but_not_attributed(self):
        c = self.obs.display_consistency()
        self.assertAlmostEqual(c["displayed_minus_raw"], -0.24, places=2)
        status = self.obs.unexplained_cost_status()
        self.assertEqual(status["status"], "UNEXPLAINED_VENUE_COST")
        # Every candidate cause is enumerated; none is selected.
        self.assertIn("entry fee", status["candidate_causes"])
        self.assertIn("mark vs last price", status["candidate_causes"])
        self.assertIn("similarity of magnitude is not identity of cause",
                      status["note"])

    def test_leveraged_return_is_not_move_times_leverage(self):
        """The displayed -18.55%% is return ON MARGIN. The naive
        move-x-leverage figure (-0.326%% x 50 = -16.3%%) is a DIFFERENT
        number, and the fixture proves the venue does not display it."""
        move_pct = 100.0 * (10.692 - 10.727) / 10.727
        naive = move_pct * 50
        self.assertNotAlmostEqual(naive, -18.55, delta=1.0)


class IncompletenessIsEnforcedTests(unittest.TestCase):
    """JARVIS cannot invent what the evidence never showed."""

    def setUp(self):
        self.obs = VR.btcc_linkusdt_observation()

    def test_realized_pnl_raises(self):
        with self.assertRaises(VR.IncompleteLifecycle):
            self.obs.realized_pnl()

    def test_realized_funding_raises(self):
        with self.assertRaises(VR.IncompleteLifecycle):
            self.obs.realized_funding()

    def test_funding_is_unknown_not_zero(self):
        self.assertEqual(self.obs.funding.paid_usd, VR.UNKNOWN)
        self.assertEqual(self.obs.funding.net_usd, VR.UNKNOWN)

    def test_reconciliation_refuses_an_incomplete_observation(self):
        model = VR.ModelCosts(raw_price_pnl=-1.75, entry_fee=0.12,
                              exit_fee=0.12, spread_cost=0.01, slippage=0.02,
                              funding=0.0, other_carry=0.0)
        with self.assertRaises(VR.IncompleteLifecycle):
            VR.reconcile(self.obs, model)

    def test_no_field_smuggles_a_realized_value(self):
        for f in ("actual_realized_pnl", "actual_funding",
                  "actual_opening_fee", "actual_closing_fee",
                  "actual_exit_price", "actual_total_cost", "closed_at"):
            self.assertIsNone(getattr(self.obs, f), f)


class CrossMarginHonestyTests(unittest.TestCase):

    def test_liquidation_without_account_state_is_unknown(self):
        out = VR.estimate_cross_liquidation(entry_price=10.727, leverage=50,
                                            side="LONG")
        self.assertEqual(out["liquidation_price"], VR.UNKNOWN)
        self.assertIn("account", out["reason"])

    def test_even_with_collateral_missing_rules_stay_unknown(self):
        out = VR.estimate_cross_liquidation(
            entry_price=10.727, leverage=50, side="LONG",
            account_collateral_usd=100.0, venue_rules_known=False)
        self.assertEqual(out["liquidation_price"], VR.UNKNOWN)

    def test_the_fixture_records_the_displayed_estimate_as_display_only(self):
        obs = VR.btcc_linkusdt_observation()
        self.assertAlmostEqual(obs.estimated_liquidation_price, 9.801)
        # Sanity: the naive isolated formula does NOT reproduce the venue's
        # number, which is exactly why fabricating one is forbidden.
        naive_isolated = 10.727 * (1 - 1 / 50)
        self.assertNotAlmostEqual(naive_isolated,
                                  obs.estimated_liquidation_price, delta=0.05)


class CompletedLifecycleReconciliationTests(unittest.TestCase):
    """The machinery works when evidence is real — proven with a synthetic
    COMPLETE observation, since no real one exists yet."""

    def _completed(self, realized=-2.10):
        return VR.VenueExecutionObservation(
            observation_id="synthetic-complete", venue="TEST",
            account_type="test", instrument="LINKUSDT", product="perpetual",
            side="LONG", leverage=50.0, margin_mode="CROSS", quantity=50.0,
            quantity_unit="LINK", entry_price=10.727,
            actual_exit_price=10.690, actual_opening_fee=0.12,
            actual_closing_fee=0.12, actual_funding=0.01,
            actual_realized_pnl=realized, actual_total_cost=0.25,
            closed_at="synthetic",
            evidence_type=VR.REALIZED_HISTORY,
            evidence_source="synthetic fixture",
            lifecycle=VR.COMPLETE_LIFECYCLE)

    def test_a_complete_model_reconciles_and_preserves_the_gap(self):
        obs = self._completed(realized=-2.10)
        model = VR.ModelCosts(raw_price_pnl=50 * (10.690 - 10.727),
                              entry_fee=0.12, exit_fee=0.12,
                              spread_cost=0.01, slippage=0.02, funding=0.01,
                              other_carry=0.0,
                              execution_model_version="fill_v1",
                              venue_cost_model_version="test_v1")
        out = VR.reconcile(obs, model)
        self.assertEqual(out["status"], "RECONCILED")
        self.assertAlmostEqual(out["reconciliation_delta_usd"],
                               out["unexplained_delta_usd"])
        self.assertIn("never absorbed into a fitted parameter",
                      out["unexplained_note"])

    def test_an_unknown_model_component_blocks_attribution(self):
        """A reconciliation against a model with UNKNOWN funding would
        attribute the venue's funding to the hole. Refused."""
        obs = self._completed()
        model = VR.ModelCosts(raw_price_pnl=-1.85, entry_fee=0.12,
                              exit_fee=0.12, spread_cost=0.01,
                              slippage=0.02)          # funding UNKNOWN
        out = VR.reconcile(obs, model)
        self.assertEqual(out["status"], "MODEL_INCOMPLETE")
        self.assertIn("funding", out["model_components_unknown"])

    def test_the_delta_is_in_both_usd_and_bps(self):
        obs = self._completed(realized=-2.00)
        model = VR.ModelCosts(raw_price_pnl=50 * (10.690 - 10.727),
                              entry_fee=0.12, exit_fee=0.12,
                              spread_cost=0.0, slippage=0.0, funding=0.0,
                              other_carry=0.0)
        out = VR.reconcile(obs, model)
        self.assertIsInstance(out["reconciliation_delta_bps"], float)


class EvidenceRankTests(unittest.TestCase):

    def test_realized_history_outranks_ui(self):
        rank = VR.EVIDENCE_RANK
        self.assertLess(rank.index(VR.REALIZED_HISTORY),
                        rank.index(VR.OBSERVED_UI))

    def test_model_derived_is_the_weakest(self):
        self.assertEqual(VR.EVIDENCE_RANK[-1], VR.MODEL_DERIVED)


if __name__ == "__main__":
    unittest.main()
