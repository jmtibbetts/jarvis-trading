"""MANUAL_OPERATOR — a real trade this program did not place.

WHAT THESE TESTS PROTECT, in one line each:

  the taxonomy is EXTENDED, not duplicated
  a manual trade may or may not answer a JARVIS thesis, and never invents one
  what was RECOMMENDED and what actually HAPPENED stay separate facts
  an unevidenced cost stays UNKNOWN and never becomes zero
  a scale-out does not let one thesis vote twice
  recording external evidence moves NO virtual money, on either book
  promotional credit does not become owned capital by being written down

The isolation tests are the load-bearing ones. Everything else here is
arithmetic that can be re-derived; a training economy silently funded by
real-world evidence cannot be un-funded, because afterwards nobody can tell
which part of the balance the simulator produced.
"""
import ast
import copy
import math
import pathlib
import unittest

from lib import account_economics as ae
from lib import execution_mode as em
from lib import manual_execution as mx
from lib import manual_trade_store as store
from lib import realized_outcome as ro
from lib import trade_thesis as tt

ENTRY, PARTIAL, FINAL = mx.LEG_ENTRY, mx.LEG_PARTIAL_EXIT, mx.LEG_FINAL_EXIT

T0 = "2026-08-19T14:00:00+00:00"
T1 = "2026-08-19T14:05:00+00:00"
T2 = "2026-08-19T15:00:00+00:00"
T3 = "2026-08-19T18:00:00+00:00"
T4 = "2026-08-20T02:00:00+00:00"


def _recommendation(**kw):
    base = dict(
        thesis_id="thesis-abc", signal_id="sig-1", decision_id="dec-1",
        recommended_at=T0, direction="long", venue="BTCC",
        product="CRYPTO_PERP", symbol="LINK/USDT", entry=10.700, stop=9.90,
        targets=(11.5,), leverage=50.0, expected_fee_usd=0.50,
        expected_funding_usd=0.10, expected_r=1.8, confidence=0.62)
    base.update(kw)
    return mx.RecommendationSnapshot(**base)


def _perp(**kw):
    """A BTCC-style perpetual, the operator's real venue shape."""
    base = dict(
        venue="BTCC", product="CRYPTO_PERP", symbol="LINK/USDT",
        direction="long", quantity_unit="COINS", multiplier=1.0,
        account_label="btcc-main", leverage=50.0, margin_mode="CROSS",
        collateral_usd=10.73, collateral_capital_kind=ae.OWN_CAPITAL,
        initial_risk_usd=10.73, evidence_type="OBSERVED_UI",
        evidence_source="operator screenshot", opened_at=T1)
    base.update(kw)
    return store.create(**base)


def _economy_snapshot() -> dict:
    """Every table that holds virtual money, before and after.

    Deliberately reads the tables directly rather than an aggregate view:
    a summary that filters out manual rows would prove nothing about
    whether the rows were written.
    """
    from app.database import (DexBalance, DexFundingEvent, DexPortfolio,
                              DexPosition, DexTrade, PaperPortfolio,
                              PaperPosition, PaperPositionSettlement,
                              PaperRealizedOutcome, PaperSettlementLeg,
                              PaperTrade, get_db)

    with get_db() as db:
        pf = db.query(PaperPortfolio).first()
        return {
            "cash": (pf.cash if pf else None),
            "realized_pnl": (pf.realized_pnl if pf else None),
            "total_trades": (pf.total_trades if pf else None),
            "paper_positions": db.query(PaperPosition).count(),
            "paper_trades": db.query(PaperTrade).count(),
            "paper_settlements": db.query(PaperPositionSettlement).count(),
            "paper_settlement_legs": db.query(PaperSettlementLeg).count(),
            "paper_realized_outcomes": db.query(PaperRealizedOutcome).count(),
            "dex_balances": db.query(DexBalance).count(),
            "dex_funding_events": db.query(DexFundingEvent).count(),
            "dex_portfolio": db.query(DexPortfolio).count(),
            "dex_positions": db.query(DexPosition).count(),
            "dex_trades": db.query(DexTrade).count(),
        }


# ── A. The taxonomy is extended, not duplicated ──────────────────────────
class ExecutionModeTaxonomyTests(unittest.TestCase):

    def test_manual_operator_is_a_canonical_mode(self):
        self.assertIn(em.MANUAL_OPERATOR, em.MODES)
        self.assertEqual(em.spec(em.MANUAL_OPERATOR).mode, "MANUAL_OPERATOR")

    def test_no_parallel_vocabulary_for_the_existing_families(self):
        """The venue families are IMPORTED from execution_venue, not retyped.

        Two modules each spelling "VIRTUAL_CEX" is how a taxonomy forks:
        one of them gets renamed, both keep working, and the mismatch is
        invisible until a filter silently matches nothing.
        """
        from lib import execution_venue as ev

        self.assertIs(em.VIRTUAL_CEX, ev.VIRTUAL_CEX)
        self.assertIs(em.VIRTUAL_DEX, ev.VIRTUAL_DEX)
        self.assertIs(em.SHADOW, ev.SHADOW)

        # And the module does not define its own copies.
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "execution_mode.py").read_text(encoding="utf-8"))
        assigned = {t.id for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)}
        for name in ("VIRTUAL_CEX", "VIRTUAL_DEX", "SHADOW"):
            self.assertNotIn(name, assigned,
                             f"{name} is redefined instead of imported")

    def test_manual_operator_is_a_learning_source(self):
        self.assertIn(ro.MANUAL_OPERATOR, ro.SOURCES)
        self.assertEqual(em.outcome_source(em.MANUAL_OPERATOR),
                         ro.MANUAL_OPERATOR)

    def test_manual_operator_is_a_thesis_arm(self):
        self.assertIn(tt.OPERATOR, tt.ARMS)

    def test_live_autonomous_exists_and_is_refused(self):
        """Present so nothing needs renaming later; refused so its presence
        authorises nothing."""
        self.assertIn(em.LIVE_AUTONOMOUS, em.MODES)
        self.assertFalse(em.spec(em.LIVE_AUTONOMOUS).executable_today)
        with self.assertRaises(em.ExecutionModeError):
            em.assert_executable(em.LIVE_AUTONOMOUS)

    def test_unknown_mode_raises_rather_than_defaulting(self):
        """A permissive default for an unknown mode is how a typo acquires
        execution rights. Surrounding whitespace IS forgiven — it cannot
        make one mode mean another — but a different string cannot."""
        for bad in ("MANUAL", "manual_operator", "LIVE", "", None,
                    "VIRTUAL", "LIVE_CEX"):
            with self.assertRaises(em.ExecutionModeError):
                em.spec(bad)
        self.assertEqual(em.spec("  VIRTUAL_CEX  ").mode, em.VIRTUAL_CEX)

    def test_shadow_will_not_guess_its_venue_type(self):
        with self.assertRaises(em.ExecutionModeError):
            em.outcome_source(em.SHADOW)
        self.assertEqual(em.outcome_source(em.SHADOW, venue_type="CEX"),
                         ro.SHADOW_CEX)


# ── P. JARVIS never claims it submitted a manual order ───────────────────
class SubmissionAuthorshipTests(unittest.TestCase):

    def test_manual_mode_is_not_submitted_by_jarvis(self):
        self.assertFalse(em.submitted_by_jarvis(em.MANUAL_OPERATOR))
        self.assertTrue(em.submitted_by_jarvis(em.VIRTUAL_CEX))

    def test_manual_trade_dict_says_so_explicitly(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.727,
                         at=T1, fee_usd=0.27)
        rec = store.get_record(tid)
        self.assertEqual(rec["execution_mode"], "MANUAL_OPERATOR")
        self.assertFalse(rec["submitted_by_jarvis"])

    def test_the_outcome_carries_the_same_bit(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        res = store.append_leg(tid, kind=FINAL, quantity=50, fill_price=11.0,
                               at=T4, fee_usd=0.27, exit_reason="TARGET_EXIT")
        prov = res["realized_outcome"]["provenance"]
        self.assertIs(prov["submitted_by_jarvis"], False)
        self.assertEqual(prov["execution_mode"], "MANUAL_OPERATOR")

    def test_no_submit_path_exists_on_the_manual_surface(self):
        """There is no adapter and no submit route, and that is the design.

        Asserted on the AST rather than on prose, because prose about
        refusing to execute is exactly the kind of text a search for
        'submit' matches.
        """
        for name in ("lib/manual_execution.py", "lib/manual_trade_store.py"):
            tree = ast.parse((pathlib.Path(__file__).parent.parent
                              / name).read_text(encoding="utf-8"))
            funcs = {n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertNotIn("submit", funcs, name)


# ── B/C/Q. Thesis linkage, and the refusal to invent one ─────────────────
class ThesisLinkageTests(unittest.TestCase):

    def test_a_manual_trade_may_link_to_a_thesis(self):
        tid = _perp(recommendation=_recommendation())
        trade = store.get(tid)
        self.assertEqual(trade.thesis_id, "thesis-abc")
        self.assertEqual(trade.recommendation.signal_id, "sig-1")

    def test_a_manual_trade_may_exist_without_one(self):
        tid = _perp()
        trade = store.get(tid)
        self.assertIsNone(trade.thesis_id)
        self.assertIsNone(trade.recommendation)
        self.assertEqual(
            store.get_record(tid)["recommendation_vs_actual"]["linked"],
            False)

    def test_an_independent_trade_fabricates_no_thesis(self):
        """No derived id, no synthesised recommendation, nothing back-filled."""
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        rec = store.get_record(tid)
        self.assertIsNone(rec["thesis_id"])
        self.assertIsNone(rec["recommendation"])
        self.assertIsNone(store.arm_result(tid))

        from app.database import ManualTradeRecord, get_db
        with get_db() as db:
            row = db.query(ManualTradeRecord).filter(
                ManualTradeRecord.id == tid).first()
            self.assertIsNone(row.thesis_id)
            self.assertIsNone(row.recommendation_json)

    def test_a_disagreement_keeps_the_link_and_records_both_sides(self):
        """JARVIS said short, the operator went long. The most informative
        case there is, and the schema must not require agreement."""
        tid = _perp(direction="long",
                    recommendation=_recommendation(direction="short",
                                                   venue="KRAKEN"))
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertTrue(cmp["linked"])
        self.assertEqual(cmp["direction"]["recommended"], "short")
        self.assertEqual(cmp["direction"]["actual"], "long")
        self.assertFalse(cmp["direction"]["followed"])
        self.assertEqual(cmp["venue"], {"recommended": "KRAKEN",
                                        "actual": "BTCC"})


# ── D/V. Recommended and actual are different facts ──────────────────────
class RecommendedVersusActualTests(unittest.TestCase):

    def test_recommended_entry_survives_a_worse_actual_entry(self):
        tid = _perp(recommendation=_recommendation(entry=10.700))
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.900,
                         at=T1, fee_usd=0.27)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertAlmostEqual(cmp["entry"]["recommended"], 10.700)
        self.assertAlmostEqual(cmp["entry"]["actual"], 10.900)
        self.assertAlmostEqual(cmp["entry"]["gap"], 0.200, places=9)

    def test_expected_fee_and_actual_fee_are_separate(self):
        tid = _perp(recommendation=_recommendation(expected_fee_usd=0.50))
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.31)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertAlmostEqual(cmp["fee_usd"]["expected"], 0.50)
        self.assertAlmostEqual(cmp["fee_usd"]["actual"], 0.31)

    def test_expected_funding_and_actual_funding_are_separate(self):
        tid = _perp(recommendation=_recommendation(expected_funding_usd=0.10))
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.31,
                                at=T2)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertAlmostEqual(cmp["funding_usd"]["expected"], 0.10)
        self.assertAlmostEqual(cmp["funding_usd"]["actual"], 0.31)

    def test_the_recommendation_is_not_correctable(self):
        """Bending the claim to fit the outcome deletes the baseline."""
        tid = _perp(recommendation=_recommendation())
        for field in ("thesis_id", "recommendation_json", "signal_id"):
            with self.assertRaises(mx.ManualExecutionError) as ctx:
                store.correct(tid, target_kind=store.CORRECTION_TARGET_TRADE,
                              target_id=tid, field=field, new_value="x",
                              reason="tidying", corrected_by="operator")
            self.assertIn("not correctable", str(ctx.exception))


# ── E/F/G. CEX, DEX and derivatives all record ───────────────────────────
class ProductCoverageTests(unittest.TestCase):

    def test_cex_spot_trade_is_accepted(self):
        tid = store.create(venue="KRAKEN", product="CRYPTO_SPOT",
                           symbol="BTC/USD", direction="long",
                           quantity_unit="COINS", opened_at=T1)
        store.append_leg(tid, kind=ENTRY, quantity=0.5, fill_price=60000.0,
                         at=T1, fee_usd=15.0)
        rec = store.get_record(tid)
        self.assertEqual(rec["state"], mx.OPEN)
        self.assertEqual(rec["product"], "CRYPTO_SPOT")
        # Spot borrows nothing and pays no funding — forbidden inferences.
        self.assertNotIn("funding_usd", rec["applicable_cost_categories"])
        self.assertNotIn("borrow_cost_usd", rec["applicable_cost_categories"])

    def test_dex_trade_is_accepted_and_owes_gas(self):
        tid = store.create(venue="JUPITER", product="DEX_SPOT",
                           symbol="SOL/USDC", direction="long",
                           quantity_unit="TOKEN_UNITS", opened_at=T1)
        rec = store.get_record(tid)
        self.assertIn("network_fees_usd", rec["applicable_cost_categories"])
        store.append_leg(tid, kind=ENTRY, quantity=100.0, fill_price=1.02,
                         at=T1, fee_usd=0.31)
        store.append_cost_event(tid, kind=mx.NETWORK_FEE, amount_usd=0.02,
                                at=T1)
        self.assertAlmostEqual(
            store.get_record(tid)["costs_usd"]["network_fees_usd"], 0.02)

    def test_perpetual_preserves_leverage_and_margin_facts(self):
        tid = _perp(leverage=50.0, margin_mode="CROSS", collateral_usd=10.73)
        rec = store.get_record(tid)
        self.assertEqual(rec["leverage"], 50.0)
        self.assertEqual(rec["margin_mode"], "CROSS")
        self.assertEqual(rec["collateral_usd"], 10.73)
        # PRODUCT IS NEVER INFERRED FROM LEVERAGE, and 50x borrows nothing.
        self.assertIn("funding_usd", rec["applicable_cost_categories"])
        self.assertNotIn("borrow_cost_usd", rec["applicable_cost_categories"])

    def test_futures_contracts_require_an_explicit_multiplier(self):
        """26 contracts at 0.01 are not 26 coins. That confusion once priced
        a position 100x high and fed it to the risk backstop."""
        with self.assertRaises(mx.ManualExecutionError) as ctx:
            store.create(venue="CME", product="INDEX_FUTURE", symbol="MES=F",
                         direction="long", quantity_unit="CONTRACTS")
        self.assertIn("multiplier", str(ctx.exception))

        tid = store.create(venue="CME", product="INDEX_FUTURE",
                           symbol="MES=F", direction="long",
                           quantity_unit="CONTRACTS", multiplier=5.0,
                           opened_at=T1)
        store.append_leg(tid, kind=ENTRY, quantity=2, fill_price=5000.0,
                         at=T1, fee_usd=1.0)
        store.append_leg(tid, kind=FINAL, quantity=2, fill_price=5010.0,
                         at=T4, fee_usd=1.0, exit_reason="TARGET_EXIT")
        # 10 points x 2 contracts x $5 = $100, not $20.
        self.assertAlmostEqual(store.get_record(tid)["gross_pnl_usd"], 100.0)

    def test_options_are_refused_rather_than_approximated(self):
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="DERIBIT", product="OPTION", symbol="BTC-C",
                         direction="long", quantity_unit="CONTRACTS",
                         multiplier=1.0)


# ── H/I/J. Costs: unknown, known, and signed ─────────────────────────────
class CostSemanticsTests(unittest.TestCase):

    def test_an_unevidenced_fee_stays_none_and_blocks_net(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=None)
        rec = store.get_record(tid)
        self.assertIsNone(rec["costs_usd"]["commission_usd"])
        self.assertIn("commission_usd", rec["unknown_cost_categories"])
        self.assertIsNone(rec["net_pnl_usd"])

    def test_a_known_fee_is_deterministic(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        store.append_leg(tid, kind=FINAL, quantity=50, fill_price=11.0,
                         at=T4, fee_usd=0.27, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.30,
                                at=T2)
        rec = store.get_record(tid)
        self.assertAlmostEqual(rec["costs_usd"]["commission_usd"], 0.52)
        self.assertAlmostEqual(rec["gross_pnl_usd"], 50.0)
        self.assertAlmostEqual(rec["net_pnl_usd"], 50.0 - 0.52 - 0.30)

    def test_one_missing_leg_fee_makes_the_whole_commission_unknown(self):
        """A partial sum of a total is not the total."""
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        store.append_leg(tid, kind=FINAL, quantity=50, fill_price=11.0,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        self.assertIsNone(
            store.get_record(tid)["costs_usd"]["commission_usd"])

    def test_funding_received_improves_the_result(self):
        """Funding is a TRANSFER. A model that always charges it as a cost
        understates every short that was PAID to hold its position."""
        tid = _perp(direction="short")
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=11.0,
                         at=T1, fee_usd=0.25)
        store.append_leg(tid, kind=FINAL, quantity=50, fill_price=10.0,
                         at=T4, fee_usd=0.27, exit_reason="TARGET_EXIT")
        before = store.get_record(tid)
        store.append_cost_event(tid, kind=mx.FUNDING_RECEIVED,
                                amount_usd=0.40, at=T2)
        after = store.get_record(tid)
        self.assertAlmostEqual(after["costs_usd"]["funding_usd"], -0.40)
        self.assertGreater(after["net_pnl_usd"], before["net_pnl_usd"] or 0)
        # A short that fell from 11 to 10 made money.
        self.assertAlmostEqual(after["gross_pnl_usd"], 50.0)

    def test_paid_and_received_funding_net_against_each_other(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.31,
                                at=T2)
        store.append_cost_event(tid, kind=mx.FUNDING_RECEIVED,
                                amount_usd=0.11, at=T3)
        self.assertAlmostEqual(
            store.get_record(tid)["costs_usd"]["funding_usd"], 0.20)

    def test_a_cost_event_amount_is_a_magnitude_not_a_sign(self):
        with self.assertRaises(mx.ManualExecutionError):
            mx.ManualCostEvent(mx.FUNDING_PAID, -0.31, T2)

    def test_zero_fee_does_not_mean_zero_cost(self):
        """A promotional zero-commission window leaves funding standing."""
        tid = _perp(declared_absent_costs=("commission_usd",))
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=50, fill_price=11.0,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        rec = store.get_record(tid)
        self.assertEqual(rec["costs_usd"]["commission_usd"], 0.0)
        # Funding is still owed and still unevidenced.
        self.assertIn("funding_usd", rec["unknown_cost_categories"])
        self.assertIsNone(rec["net_pnl_usd"])

    def test_declaring_a_cost_absent_that_was_also_charged_refuses(self):
        tid = _perp(declared_absent_costs=("commission_usd",))
        with self.assertRaises(mx.ManualExecutionError) as ctx:
            store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                             at=T1, fee_usd=0.25)
        self.assertIn("declares", str(ctx.exception))

    def test_cost_categories_track_the_realized_outcome_dataclass(self):
        """The entitlement vocabulary may only waive a cost that exists.

        Pinned against the dataclass so the two cannot drift: a category
        renamed on one side and not the other would make a promotion
        silently cover nothing.
        """
        fields = ro.RealizedOutcome.__dataclass_fields__
        for cat in ae.COST_CATEGORIES:
            self.assertIn(cat, fields)


# ── K/L/M/N/Z. Legs, closure, and the one-vote rule ──────────────────────
class LifecycleAndOutcomeTests(unittest.TestCase):

    def _scaled_trade(self):
        tid = _perp(recommendation=_recommendation())
        store.append_leg(tid, kind=ENTRY, quantity=30, fill_price=10.727,
                         at=T1, fee_usd=0.16)
        store.append_leg(tid, kind=ENTRY, quantity=20, fill_price=10.800,
                         at=T2, fee_usd=0.11)
        store.append_leg(tid, kind=PARTIAL, quantity=20, fill_price=11.000,
                         at=T3, fee_usd=0.11, exit_reason="TARGET_EXIT")
        return tid

    def test_many_legs_remain_one_trade(self):
        tid = self._scaled_trade()
        rec = store.get_record(tid)
        self.assertEqual(len(rec["legs"]), 3)
        self.assertEqual(rec["state"], mx.PARTIALLY_CLOSED)
        self.assertAlmostEqual(rec["open_quantity"], 30.0)
        self.assertAlmostEqual(rec["closed_quantity"], 20.0)
        # Weighted average of 30@10.727 and 20@10.800.
        self.assertAlmostEqual(rec["entry_vwap"], 10.7562, places=6)

    def test_a_partial_close_produces_no_final_outcome(self):
        tid = self._scaled_trade()
        with self.assertRaises(mx.IncompleteManualTrade):
            store.realized_outcome(tid)
        with self.assertRaises(mx.IncompleteManualTrade):
            mx.realized_outcome(store.get(tid))

    def test_a_partial_exit_may_not_flatten_the_book(self):
        tid = self._scaled_trade()
        with self.assertRaises(mx.ManualExecutionError) as ctx:
            store.append_leg(tid, kind=PARTIAL, quantity=30,
                             fill_price=10.5, at=T4)
        self.assertIn("FINAL_EXIT", str(ctx.exception))

    def test_a_final_exit_may_not_leave_quantity_on(self):
        tid = self._scaled_trade()
        with self.assertRaises(mx.ManualExecutionError):
            store.append_leg(tid, kind=FINAL, quantity=10, fill_price=10.5,
                             at=T4)

    def test_closing_more_than_is_open_refuses(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        with self.assertRaises(mx.ManualExecutionError) as ctx:
            store.append_leg(tid, kind=FINAL, quantity=25, fill_price=11.0,
                             at=T4)
        self.assertIn("only", str(ctx.exception))

    def test_the_final_exit_produces_the_canonical_outcome(self):
        tid = self._scaled_trade()
        res = store.append_leg(tid, kind=FINAL, quantity=30,
                               fill_price=10.500, at=T4, fee_usd=0.16,
                               exit_reason="STOP_EXIT")
        self.assertEqual(res["state"], mx.CLOSED)
        outcome = res["realized_outcome"]

        # Built from the LEGS, on the stated basis — never from one blended
        # entry and one blended exit.
        self.assertEqual(outcome["source"], ro.MANUAL_OPERATOR)
        self.assertEqual(outcome["provenance"]["pnl_basis"],
                         mx.PNL_BASIS)
        self.assertEqual(outcome["thesis_id"], "thesis-abc")
        # 20 @ (11.000-10.7562) + 30 @ (10.500-10.7562)
        self.assertAlmostEqual(outcome["gross_pnl_usd"], -2.81, places=6)
        self.assertAlmostEqual(outcome["commission_usd"], 0.54, places=6)
        self.assertAlmostEqual(outcome["net_pnl_usd"], -3.35, places=6)
        self.assertEqual(outcome["outcome"], ro.LOSS)

        stored = store.realized_outcome(tid)
        self.assertEqual(stored["outcome"]["net_pnl_usd"],
                         outcome["net_pnl_usd"])

    def test_the_outcome_uses_actual_economics_not_recommended(self):
        tid = _perp(recommendation=_recommendation(entry=10.000,
                                                   expected_fee_usd=0.05))
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.500,
                         at=T1, fee_usd=0.40)
        res = store.append_leg(tid, kind=FINAL, quantity=10,
                               fill_price=11.000, at=T4, fee_usd=0.42,
                               exit_reason="TARGET_EXIT")
        o = res["realized_outcome"]
        # Gross is measured from the ACTUAL 10.500 fill, not the 10.000
        # that was recommended.
        self.assertAlmostEqual(o["actual_entry_fill"], 10.500)
        self.assertAlmostEqual(o["decision_entry_price"], 10.000)
        self.assertAlmostEqual(o["gross_pnl_usd"], 5.0)
        self.assertAlmostEqual(o["commission_usd"], 0.82)
        # The recommended-vs-actual gap is ATTRIBUTED, never charged.
        self.assertAlmostEqual(o["net_pnl_usd"], 5.0 - 0.82)
        self.assertNotEqual(o["slippage_attribution_usd"], 0.0)

    def test_one_thesis_does_not_vote_twice_because_it_scaled_out(self):
        tid = self._scaled_trade()
        store.append_leg(tid, kind=FINAL, quantity=30, fill_price=10.500,
                         at=T4, fee_usd=0.16, exit_reason="STOP_EXIT")

        arm = store.arm_result(tid)
        self.assertEqual(arm.arm, tt.OPERATOR)
        self.assertEqual(arm.thesis_id, "thesis-abc")

        # Four legs, one arm result, and one sample alongside the agent's.
        agent = tt.ArmResult("thesis-abc", tt.AGENT, traded=True, net_r=0.4)
        self.assertEqual(tt.sample_count([agent, arm]), 1)
        self.assertEqual(len(store.get_record(tid)["legs"]), 4)

    def test_a_blocked_outcome_does_not_reach_learning(self):
        """THE BOT MUST NEVER LEARN THAT IT MADE MONEY BECAUSE A COST WAS
        NEVER ENTERED."""
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        res = store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                               at=T4, fee_usd=0.05, exit_reason="TARGET_EXIT")
        # Funding was never evidenced on a perpetual, which really does
        # charge it.
        self.assertEqual(res["learning_state"],
                         store.BLOCKED_INCOMPLETE_COSTS)
        self.assertFalse(
            res["realized_outcome"]["provenance"]["net_pnl_is_complete"])

        # Supplying it later unblocks the outcome without rewriting history.
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.12,
                                at=T2)
        after = store.realized_outcome(tid)
        self.assertEqual(after["learning_state"], store.PENDING)
        self.assertAlmostEqual(after["outcome"]["funding_usd"], 0.12)

    def test_a_terminated_trade_produces_no_outcome(self):
        tid = _perp()
        store.terminate(tid, state=mx.CANCELLED,
                        reason="the operator never placed it")
        self.assertEqual(store.get_record(tid)["state"], mx.CANCELLED)
        with self.assertRaises(mx.IncompleteManualTrade):
            store.realized_outcome(tid)

    def test_cancelled_and_abandoned_stay_different_facts(self):
        opened = _perp()
        store.append_leg(opened, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        # An OPEN trade was executed, so it cannot be CANCELLED.
        with self.assertRaises(mx.ManualExecutionError):
            store.terminate(opened, state=mx.CANCELLED, reason="changed mind")
        store.terminate(opened, state=mx.ABANDONED,
                        reason="venue statement never arrived")
        self.assertEqual(store.get_record(opened)["state"], mx.ABANDONED)


# ── X/Y. Refusals ────────────────────────────────────────────────────────
class RefusalTests(unittest.TestCase):

    def test_a_closed_trade_cannot_reopen(self):
        with self.assertRaises(mx.ManualExecutionError):
            mx.parse_state_transition(mx.CLOSED, mx.OPEN)
        for terminal in (mx.CLOSED, mx.CANCELLED, mx.ABANDONED):
            for target in mx.STATES:
                with self.assertRaises(mx.ManualExecutionError):
                    mx.parse_state_transition(terminal, target)

    def test_a_settled_trade_takes_no_further_legs(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                         at=T4, fee_usd=0.05, exit_reason="TARGET_EXIT")
        with self.assertRaises(mx.ManualExecutionError) as ctx:
            store.append_leg(tid, kind=ENTRY, quantity=5, fill_price=10.0,
                             at=T4)
        self.assertIn("CORRECTION", str(ctx.exception))

    def test_nan_and_infinity_refuse(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(mx.ManualExecutionError):
                mx.ManualExecutionLeg(ENTRY, bad, 10.0, T1)
            with self.assertRaises(mx.ManualExecutionError):
                mx.ManualExecutionLeg(ENTRY, 1.0, bad, T1)
            with self.assertRaises(mx.ManualExecutionError):
                mx.ManualExecutionLeg(ENTRY, 1.0, 10.0, T1, fee_usd=bad)

    def test_impossible_quantities_and_prices_refuse(self):
        for bad in (0, -1, -0.0001):
            with self.assertRaises(mx.ManualExecutionError):
                mx.ManualExecutionLeg(ENTRY, bad, 10.0, T1)
            with self.assertRaises(mx.ManualExecutionError):
                mx.ManualExecutionLeg(ENTRY, 1.0, bad, T1)
        with self.assertRaises(mx.ManualExecutionError):
            mx.ManualExecutionLeg(ENTRY, 1.0, 10.0, T1, fee_usd=-0.01)

    def test_unreadable_units_products_sides_and_kinds_refuse(self):
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="X", product="CRYPTO_SPOT", symbol="BTC/USD",
                         direction="long", quantity_unit="LOTS")
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="X", product="PERPETUAL", symbol="BTC/USD",
                         direction="long", quantity_unit="COINS")
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="X", product="CRYPTO_SPOT", symbol="BTC/USD",
                         direction="LONGSHORT", quantity_unit="COINS")
        with self.assertRaises(mx.ManualExecutionError):
            mx.ManualExecutionLeg("ROLL", 1.0, 10.0, T1)
        with self.assertRaises(mx.ManualExecutionError):
            mx.ManualCostEvent("MYSTERY_FEE", 1.0, T1)

    def test_nonsensical_leverage_refuses(self):
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="X", product="CRYPTO_PERP", symbol="BTC/USD",
                         direction="long", quantity_unit="COINS",
                         leverage=5000.0)
        with self.assertRaises(mx.ManualExecutionError):
            store.create(venue="X", product="CRYPTO_PERP", symbol="BTC/USD",
                         direction="long", quantity_unit="COINS",
                         leverage=0.0)

    def test_unreadable_timestamps_refuse(self):
        with self.assertRaises(mx.ManualExecutionError):
            mx.ManualExecutionLeg(ENTRY, 1.0, 10.0, "yesterday afternoon")

    def test_an_exit_before_its_entry_is_refused_not_reordered(self):
        """The walk runs in TIME order, so an exit stamped before the entry
        closes a position that does not yet exist. Refused rather than
        silently re-sorted into something that balances."""
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T3, fee_usd=0.05)
        with self.assertRaises(mx.ManualExecutionError):
            store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                             at=T1, fee_usd=0.05)


# ── R/S. The virtual economy is untouched ────────────────────────────────
class VirtualEconomyIsolationTests(unittest.TestCase):

    def test_a_full_manual_lifecycle_moves_no_virtual_money(self):
        before = _economy_snapshot()

        tid = _perp(recommendation=_recommendation())
        store.append_leg(tid, kind=ENTRY, quantity=30, fill_price=10.727,
                         at=T1, fee_usd=0.16)
        store.append_leg(tid, kind=ENTRY, quantity=20, fill_price=10.800,
                         at=T2, fee_usd=0.11)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.21,
                                at=T2)
        store.append_leg(tid, kind=PARTIAL, quantity=20, fill_price=11.000,
                         at=T3, fee_usd=0.11, exit_reason="TARGET_EXIT")
        store.append_leg(tid, kind=FINAL, quantity=30, fill_price=10.500,
                         at=T4, fee_usd=0.16, exit_reason="STOP_EXIT")
        store.correct(tid, target_kind=store.CORRECTION_TARGET_TRADE,
                      target_id=tid, field="notes", new_value="reconciled",
                      reason="statement arrived", corrected_by="operator")

        after = _economy_snapshot()
        self.assertEqual(before, after,
                         "recording manual evidence changed the virtual "
                         "economy; a training book funded by external "
                         "evidence can no longer measure the simulator")

    def test_a_manual_dex_trade_moves_no_dex_money(self):
        before = _economy_snapshot()
        tid = store.create(venue="JUPITER", product="DEX_SPOT",
                           symbol="SOL/USDC", direction="long",
                           quantity_unit="TOKEN_UNITS", opened_at=T1)
        store.append_leg(tid, kind=ENTRY, quantity=100.0, fill_price=1.02,
                         at=T1, fee_usd=0.31)
        store.append_cost_event(tid, kind=mx.NETWORK_FEE, amount_usd=0.02,
                                at=T1)
        store.append_leg(tid, kind=FINAL, quantity=100.0, fill_price=1.10,
                         at=T4, fee_usd=0.33, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.NETWORK_FEE, amount_usd=0.02,
                                at=T4)
        after = _economy_snapshot()
        self.assertEqual(before["dex_balances"], after["dex_balances"])
        self.assertEqual(before["dex_funding_events"],
                         after["dex_funding_events"])
        self.assertEqual(before["dex_positions"], after["dex_positions"])
        self.assertEqual(before["dex_trades"], after["dex_trades"])
        self.assertEqual(before["cash"], after["cash"])

    def test_the_store_imports_no_virtual_book_writer(self):
        """Structural, so the boundary cannot be crossed by a future edit
        that merely looks harmless at the call site."""
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "manual_trade_store.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                for a in node.names:
                    imported.add(f"{node.module}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        forbidden = ("lib.paper_engine", "lib.dex_paper", "lib.dex_wallet",
                     "lib.canonical_entry", "lib.canonical_exit",
                     "lib.canonical_settlement", "lib.settlement_ledger",
                     "lib.virtual_orders", "lib.execution_venue")
        for mod in forbidden:
            self.assertNotIn(mod, imported,
                             f"manual_trade_store imports {mod}")
        for name in ("PaperPortfolio", "PaperPosition", "PaperTrade",
                     "DexBalance", "DexPortfolio", "DexPosition"):
            self.assertNotIn(f"app.database.{name}", imported,
                             f"manual_trade_store imports {name}")


# ── T/U. Account entitlements and unowned capital ────────────────────────
class AccountEconomicsTests(unittest.TestCase):

    def test_promotional_credit_is_not_owned_capital(self):
        self.assertFalse(ae.is_owned_capital(ae.PROMOTIONAL_CREDIT))
        self.assertFalse(ae.is_owned_capital(ae.TRADING_BONUS))
        self.assertFalse(ae.is_owned_capital(ae.BORROWED_CAPITAL))
        self.assertTrue(ae.is_owned_capital(ae.OWN_CAPITAL))

    def test_unknown_capital_fails_closed(self):
        """Unproven capital counted as equity inflates the book and every
        return computed against it."""
        self.assertFalse(ae.is_owned_capital(ae.CAPITAL_UNKNOWN))
        self.assertEqual(ae.capital_spec(ae.CAPITAL_UNKNOWN).owned,
                         ae.UNKNOWN)

    def test_recording_a_promotional_trade_creates_no_owned_cash(self):
        before = _economy_snapshot()
        tid = _perp(collateral_usd=1000.0,
                    collateral_capital_kind=ae.PROMOTIONAL_CREDIT)
        store.append_leg(tid, kind=ENTRY, quantity=50, fill_price=10.0,
                         at=T1, fee_usd=0.25)
        rec = store.get_record(tid)
        self.assertEqual(rec["collateral_usd"], 1000.0)
        self.assertEqual(rec["owned_collateral_usd"], 0.0)
        self.assertEqual(rec["collateral_capital_kind"],
                         ae.PROMOTIONAL_CREDIT)
        self.assertEqual(before["cash"], _economy_snapshot()["cash"])

    def test_an_unmeasured_amount_does_not_become_zero_owned(self):
        self.assertIsNone(ae.owned_amount_usd(None, ae.OWN_CAPITAL))

    def test_an_account_promotion_does_not_rewrite_the_public_schedule(self):
        """A schedule edited to match one account stops describing the venue."""
        from lib import venues

        before = copy.deepcopy(venues.VENUE_FEES)
        promo = ae.AccountEntitlement(
            entitlement_id="btcc-zero-fee-btc-eth",
            account_label="btcc-main", venue="BTCC", kind=ae.PROMOTION,
            covers=("commission_usd",), waive=True,
            effective_from="2026-08-01T00:00:00+00:00",
            effective_until="2026-08-31T23:59:59+00:00",
            evidence_type="OFFICIAL_STATEMENT",
            evidence_source="venue promotion page")
        view = ae.apply({"commission_usd": 4.20, "funding_usd": 0.31},
                        [promo], account_label="btcc-main", venue="BTCC",
                        at=T1)
        self.assertEqual(view.effective_usd["commission_usd"], 0.0)
        # NOTHING ELSE MOVED.
        self.assertEqual(view.effective_usd["funding_usd"], 0.31)
        self.assertEqual(view.public_usd["commission_usd"], 4.20)
        self.assertAlmostEqual(view.entitlement_value_usd, 4.20)
        self.assertIn("funding_usd", view.untouched_categories)
        self.assertEqual(before, venues.VENUE_FEES)

    def test_a_promotion_must_have_an_end(self):
        """An unbounded 'temporary' waiver keeps discounting after it ends."""
        with self.assertRaises(ae.AccountEconomicsError):
            ae.AccountEntitlement(
                entitlement_id="forever", account_label="a", venue="BTCC",
                kind=ae.PROMOTION, covers=("commission_usd",), waive=True)

    def test_an_expired_promotion_does_not_reach_a_later_trade(self):
        promo = ae.AccountEntitlement(
            entitlement_id="expired", account_label="a", venue="BTCC",
            kind=ae.PROMOTION, covers=("commission_usd",), waive=True,
            effective_until="2026-08-18T00:00:00+00:00")
        view = ae.apply({"commission_usd": 4.20}, [promo],
                        account_label="a", venue="BTCC", at=T1)
        self.assertEqual(view.effective_usd["commission_usd"], 4.20)
        self.assertEqual(view.applied, [])

    def test_an_entitlement_may_not_waive_a_cost_that_does_not_exist(self):
        with self.assertRaises(ae.AccountEconomicsError):
            ae.AccountEntitlement(
                entitlement_id="bogus", account_label="a", venue="BTCC",
                kind=ae.FEE_WAIVER, covers=("vibes_usd",), waive=True)

    def test_an_entitlement_leaves_an_unknown_cost_unknown(self):
        promo = ae.AccountEntitlement(
            entitlement_id="w", account_label="a", venue="BTCC",
            kind=ae.FEE_WAIVER, covers=("commission_usd",), waive=True)
        view = ae.apply({"commission_usd": None}, [promo],
                        account_label="a", venue="BTCC", at=T1)
        self.assertIsNone(view.effective_usd["commission_usd"])
        self.assertIsNone(view.entitlement_value_usd)


# ── W. The operator's own number is preserved, not forced ────────────────
class ReconciliationTests(unittest.TestCase):

    def _closed(self, reported=None, exit_fee=0.05):
        tid = _perp(operator_reported_realized_pnl_usd=reported,
                    operator_reported_evidence_type="OFFICIAL_STATEMENT")
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                         at=T4, fee_usd=exit_fee, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.10,
                                at=T2)
        return tid

    def test_a_matching_report_reconciles(self):
        # 10 x (11-10) = 10.00 gross, minus 0.10 fees and 0.10 funding.
        tid = self._closed(reported=9.80)
        r = store.get_record(tid)["reconciliation"]
        self.assertEqual(r["status"], "RECONCILED")
        self.assertAlmostEqual(r["delta_usd"], 0.0, places=6)

    def test_a_disagreeing_report_stays_unexplained_not_absorbed(self):
        """An unexplained delta measures the model's ignorance. Fitting it
        away deletes the only number that says how wrong the model is."""
        tid = self._closed(reported=9.20)
        r = store.get_record(tid)["reconciliation"]
        self.assertEqual(r["status"], "UNEXPLAINED_VENUE_COST")
        self.assertAlmostEqual(r["delta_usd"], -0.60, places=6)
        # Both numbers survive, separately.
        self.assertAlmostEqual(r["operator_reported_realized_pnl_usd"], 9.20)
        self.assertAlmostEqual(r["component_derived_net_pnl_usd"], 9.80)

    def test_a_report_is_never_back_solved_into_a_missing_cost(self):
        tid = _perp(operator_reported_realized_pnl_usd=9.20)
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=None)
        r = store.get_record(tid)["reconciliation"]
        self.assertEqual(r["status"], "MODEL_INCOMPLETE")
        self.assertIn("commission_usd", r["unknown_cost_categories"])
        self.assertAlmostEqual(r["operator_reported_realized_pnl_usd"], 9.20)
        self.assertIsNone(r["component_derived_net_pnl_usd"])
        self.assertIsNone(store.get_record(tid)["costs_usd"]["commission_usd"])

    def test_components_alone_report_as_such(self):
        tid = self._closed(reported=None)
        self.assertEqual(store.get_record(tid)["reconciliation"]["status"],
                         "COMPONENT_ONLY")

    def test_the_vocabulary_is_the_existing_one(self):
        from lib import venue_reconciliation as vr

        self.assertEqual(
            store.get_record(self._closed(9.80))["reconciliation"][
                "reconciliation_model_version"],
            vr.RECONCILIATION_MODEL_VERSION)


# ── AA. Corrections keep what they replaced ──────────────────────────────
class CorrectionTests(unittest.TestCase):

    def test_a_correction_preserves_the_previous_value_and_its_provenance(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.16)
        leg_id = store.get_record(tid)["legs"][0]["leg_id"]

        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg_id, field="fee_usd", new_value=0.20,
                      reason="venue statement restated the fee",
                      corrected_by="operator",
                      evidence_type="OFFICIAL_STATEMENT",
                      evidence_source="BTCC monthly statement")

        self.assertAlmostEqual(
            store.get_record(tid)["costs_usd"]["commission_usd"], 0.20)

        hist = store.corrections(tid)
        self.assertEqual(len(hist), 1)
        self.assertAlmostEqual(hist[0]["previous_value"], 0.16)
        self.assertAlmostEqual(hist[0]["new_value"], 0.20)
        self.assertEqual(hist[0]["corrected_by"], "operator")
        self.assertEqual(hist[0]["evidence_type"], "OFFICIAL_STATEMENT")
        self.assertEqual(hist[0]["field"], "fee_usd")
        self.assertTrue(hist[0]["corrected_at"])

    def test_a_correction_needs_a_reason_and_an_author(self):
        tid = _perp()
        for kw in ({"reason": "", "corrected_by": "op"},
                   {"reason": "typo", "corrected_by": ""}):
            with self.assertRaises(mx.ManualExecutionError):
                store.correct(tid,
                              target_kind=store.CORRECTION_TARGET_TRADE,
                              target_id=tid, field="notes",
                              new_value="x", **kw)

    def test_a_correction_that_breaks_the_book_is_refused(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                         at=T4, fee_usd=0.05, exit_reason="TARGET_EXIT")
        entry_leg = store.get_record(tid)["legs"][0]["leg_id"]
        with self.assertRaises(mx.ManualExecutionError):
            # Shrinking the entry below what was already closed.
            store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                          target_id=entry_leg, field="quantity",
                          new_value=2.0, reason="misread",
                          corrected_by="operator")
        self.assertAlmostEqual(
            store.get_record(tid)["legs"][0]["quantity"], 10.0,
            msg="a refused correction must leave the original intact")

    def test_a_late_correction_re_derives_the_closed_outcome(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=10, fill_price=10.0,
                         at=T1, fee_usd=0.05)
        store.append_leg(tid, kind=FINAL, quantity=10, fill_price=11.0,
                         at=T4, fee_usd=0.05, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=0.10,
                                at=T2)
        first = store.realized_outcome(tid)["outcome"]["net_pnl_usd"]
        leg_id = store.get_record(tid)["legs"][1]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg_id, field="fee_usd", new_value=0.55,
                      reason="statement restated the exit fee",
                      corrected_by="operator",
                      evidence_type="OFFICIAL_STATEMENT")
        second = store.realized_outcome(tid)["outcome"]["net_pnl_usd"]
        self.assertAlmostEqual(second, first - 0.50, places=6)


# ── AB. The existing virtual behaviour does not regress ──────────────────
class NoRegressionTests(unittest.TestCase):

    def test_the_default_outcome_source_is_still_virtual(self):
        o = ro.build(symbol="BTC/USD", direction="long", entry_fill=100.0,
                     exit_fill=110.0, quantity=1.0)
        self.assertEqual(o.source, ro.VIRTUAL_CEX_AGENT)
        self.assertAlmostEqual(o.gross_pnl_usd, 10.0)
        self.assertAlmostEqual(o.net_pnl_usd, 10.0)
        self.assertEqual(o.outcome, ro.WIN)

    def test_existing_sources_are_untouched(self):
        for s in (ro.VIRTUAL_CEX_AGENT, ro.VIRTUAL_DEX_AGENT, ro.SHADOW_CEX,
                  ro.SHADOW_DEX, ro.REPLAY, ro.COUNTERFACTUAL, ro.BACKTEST,
                  ro.LIVE_CEX, ro.LIVE_DEX):
            self.assertIn(s, ro.SOURCES)

    def test_an_added_arm_cannot_inflate_the_sample(self):
        t = "one-thesis"
        rows = [tt.ArmResult(t, a, traded=True) for a in tt.ARMS]
        self.assertEqual(tt.sample_count(rows), 1)

    def test_the_agent_shadow_decomposition_still_pairs(self):
        t = "paired"
        rows = [tt.ArmResult(t, tt.AGENT, traded=True, net_r=1.0),
                tt.ArmResult(t, tt.SHADOW, traded=True, net_r=0.5),
                tt.ArmResult(t, tt.OPERATOR, traded=True, net_r=0.2)]
        d = tt.decompose(rows)
        self.assertEqual(d["market_samples"], 1)
        self.assertAlmostEqual(d["management_delta_r_mean"], 0.5)

    def test_the_new_tables_are_classified_for_a_cutover(self):
        """An unclassified table FAILS the dry run — by design."""
        from lib.cutover_classification import CLASSIFICATION, UNKNOWN_REFUSE

        for t in ("manual_trades", "manual_trade_legs",
                  "manual_trade_cost_events", "manual_trade_corrections"):
            self.assertIn(t, CLASSIFICATION)
            self.assertNotEqual(CLASSIFICATION[t][0], UNKNOWN_REFUSE)


if __name__ == "__main__":
    unittest.main()
