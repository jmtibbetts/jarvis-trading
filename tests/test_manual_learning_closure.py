"""MANUAL_OPERATOR outcomes reaching learning — and what must never reach it.

THE POISON TEST IS THE POINT OF THIS FILE. `RealizedOutcome` stores costs as
floats, so a fee nobody evidenced is indistinguishable IN THE ROW from a fee
that was genuinely zero — and the bigger the missing fee, the better the trade
looks. `TheUnknownCostPoisonTests` builds exactly that trade: apparent +$60
gross, real costs $62, so the truth is a LOSS. It proves the trade moves
NOTHING while its costs are unknown, then supplies the authoritative fees and
proves it becomes eligible and lands as the loss it always was.

The second half is quieter and matters as much. `trade_outcomes.outcome_source`
had two values and every consumer expressed its policy as `!= "replay"`. A
third population would have been admitted at FULL WEIGHT by six of them,
silently, so JARVIS's measured win rate would have started including trades a
person picked. Those sites now consult `lib/learning_population`, and
`AdmissionIsAnAllowlistTests` pins that they cannot drift back.
"""
import ast
import pathlib
import unittest

from sqlalchemy import text

from lib import learning_population as LP
from lib import manual_execution as mx
from lib import manual_learning as ML
from lib import manual_trade_store as store
from lib import realized_outcome as ro
from lib import trade_thesis as tt

ENTRY, PARTIAL, FINAL = mx.LEG_ENTRY, mx.LEG_PARTIAL_EXIT, mx.LEG_FINAL_EXIT

T0 = "2026-08-19T14:00:00+00:00"
T1 = "2026-08-19T14:05:00+00:00"
T2 = "2026-08-19T16:00:00+00:00"
T3 = "2026-08-19T18:00:00+00:00"
T4 = "2026-08-20T02:00:00+00:00"

ROOT = pathlib.Path(__file__).parent.parent


def _recommendation(**kw):
    base = dict(thesis_id="ml-thesis-1", signal_id="ml-sig-1",
                decision_id="ml-dec-1", recommended_at=T0, direction="long",
                venue="BTCC", product="CRYPTO_PERP", symbol="LINK/USDT",
                entry=10.00, stop=9.50, targets=(11.0,), leverage=10.0,
                expected_fee_usd=0.40, expected_funding_usd=0.10,
                expected_r=1.8, confidence=0.62)
    base.update(kw)
    return mx.RecommendationSnapshot(**base)


def _perp(**kw):
    base = dict(venue="BTCC", product="CRYPTO_PERP", symbol="LINK/USDT",
                direction="long", quantity_unit="COINS", multiplier=1.0,
                account_label="btcc-main", leverage=10.0, margin_mode="CROSS",
                collateral_usd=100.0, collateral_capital_kind="OWN_CAPITAL",
                initial_risk_usd=50.0, evidence_type="OFFICIAL_STATEMENT",
                evidence_source="BTCC statement", opened_at=T1)
    base.update(kw)
    return store.create(**base)


def _round_trip(tid, *, entry_fee, exit_fee, entry_px=10.0, exit_px=10.5,
                 qty=100, funding=None):
    store.append_leg(tid, kind=ENTRY, quantity=qty, fill_price=entry_px,
                     at=T1, fee_usd=entry_fee)
    if funding is not None:
        store.append_cost_event(tid, kind=mx.FUNDING_PAID,
                                amount_usd=funding, at=T2)
    return store.append_leg(tid, kind=FINAL, quantity=qty, fill_price=exit_px,
                            at=T4, fee_usd=exit_fee, exit_reason="TARGET_EXIT")


def _learning_rows(source=LP.MANUAL_OPERATOR):
    """The MANUAL population by default.

    Scoped deliberately: the test database is shared across the whole
    session, so an absolute count over every population would measure other
    test modules rather than this one. Pass source=None to see everything.
    """
    from app.database import engine

    sql = ("SELECT id, canonical_outcome_id, outcome_source, pnl_usd, "
           "pnl_pct, return_pct_basis, outcome, signal_id, symbol, "
           "engine_epoch, execution_model FROM trade_outcomes")
    params = {}
    if source is not None:
        sql += " WHERE outcome_source = :s"
        params["s"] = source
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


class ManualLearningCase(unittest.TestCase):
    """Every test starts from an empty MANUAL population.

    Only manual rows are removed. Other populations belong to other test
    modules sharing this database, and deleting them would make this file's
    isolation someone else's flaky failure.
    """

    def setUp(self):
        from app.database import engine

        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM trade_outcomes WHERE outcome_source = :s"),
                {"s": LP.MANUAL_OPERATOR})
            for t in ("manual_trade_corrections", "manual_trade_cost_events",
                      "manual_trade_legs", "manual_trades"):
                conn.execute(text(f"DELETE FROM {t}"))


def _metrics_snapshot() -> dict:
    """Every statistic that is supposed to describe JARVIS, not a person."""
    from app.database import engine
    from lib import calibration, edge_cost_matrix, expectancy

    calibration._CACHE["table"] = None
    expectancy._CACHE["table"] = None
    cal = calibration.build_table(force=True)
    exp = expectancy.build_table()
    with engine.connect() as conn:
        sa = conn.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(total_trades),0), "
            "COALESCE(SUM(wins),0) FROM signal_accuracy")).fetchone()
    try:
        matrix = edge_cost_matrix.build()
        cells = len(matrix.get("cells") or [])
    except Exception:
        cells = 0
    from jobs.paper_trading import _observed_outcome_count
    return {
        "calibration_total": cal["overall"]["total"],
        "calibration_wins": cal["overall"]["wins"],
        "expectancy_buckets": len(exp),
        "signal_accuracy_rows": sa[0],
        "signal_accuracy_trades": sa[1],
        "signal_accuracy_wins": sa[2],
        "edge_cost_cells": cells,
        "bootstrap_observed_count": _observed_outcome_count(),
    }


def _economy_snapshot() -> dict:
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


# ── F/E/G. THE POISON TEST ───────────────────────────────────────────────
class TheUnknownCostPoisonTests(ManualLearningCase):
    """A trade that looks profitable ONLY because its fees are unknown."""

    def _poison(self):
        tid = _perp(recommendation=_recommendation())
        # +$60 of gross. The real costs are $62, so this is a LOSS — and
        # nothing in the row can say so while the fees are unevidenced.
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        return tid

    def test_the_trade_looks_profitable_and_its_net_is_unknown(self):
        rec = store.get_record(self._poison())
        self.assertAlmostEqual(rec["gross_pnl_usd"], 60.0, places=6)
        self.assertIsNone(rec["net_pnl_usd"],
                          "net must be UNKNOWN, not gross-minus-what-we-have")
        self.assertIn("commission_usd", rec["unknown_cost_categories"])

    def test_the_float_field_would_read_zero_but_the_gate_reads_evidence(self):
        """THE DISCRIMINATION TEST. The RealizedOutcome cannot express
        UNKNOWN — it stores 0.0 — so eligibility must not consult it."""
        tid = self._poison()
        trade = store.get(tid)
        outcome = mx.realized_outcome(trade)
        # The float says zero...
        self.assertEqual(outcome.commission_usd, 0.0)
        self.assertEqual(outcome.explicit_fees_usd, 0.0)
        # ...and the outcome therefore claims a $60 profit.
        self.assertAlmostEqual(outcome.net_pnl_usd, 60.0, places=6)
        self.assertEqual(outcome.outcome, ro.WIN)
        # The gate is not fooled, because it reads the EVIDENCE.
        v = ML.eligibility(trade)
        self.assertFalse(v.eligible)
        self.assertEqual(v.verdict, ML.BLOCKED_INCOMPLETE_COSTS)
        self.assertIn("commission_usd", v.unknown_cost_categories)
        # And the outcome itself says it is not complete.
        self.assertFalse(outcome.provenance["net_pnl_is_complete"])

    def test_it_contributes_nothing_to_any_learning_metric(self):
        before = _metrics_snapshot()
        tid = self._poison()
        res = ML.apply_manual_outcome(tid)

        self.assertFalse(res["ok"])
        self.assertEqual(res["verdict"], ML.BLOCKED_INCOMPLETE_COSTS)
        self.assertEqual(len(_learning_rows()), 0,
                         "a blocked outcome wrote a learning row")
        self.assertEqual(_metrics_snapshot(), before,
                         "a blocked outcome moved a learning metric")

    def test_supplying_the_authoritative_fees_makes_it_eligible(self):
        tid = self._poison()
        self.assertFalse(ML.apply_manual_outcome(tid)["ok"])

        legs = store.get_record(tid)["legs"]
        for leg in legs:
            store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                          target_id=leg["leg_id"], field="fee_usd",
                          new_value=28.0,
                          reason="BTCC monthly statement",
                          corrected_by="operator",
                          evidence_type="OFFICIAL_STATEMENT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=6.0,
                                at=T2)

        v = ML.eligibility(store.get(tid))
        self.assertTrue(v.eligible, v.detail)
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)

        # 60 gross - 56 commission - 6 funding = -2. The apparently
        # profitable trade was a LOSS all along.
        rows = _learning_rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][3], -2.0, places=6)
        self.assertEqual(rows[0][6], ro.LOSS)

    def test_a_partially_evidenced_cost_set_is_still_blocked(self):
        """One known fee and one unknown is not "mostly known"."""
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=28.0)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=6.0,
                                at=T2)
        v = ML.eligibility(store.get(tid))
        self.assertEqual(v.verdict, ML.BLOCKED_INCOMPLETE_COSTS)
        self.assertIn("commission_usd", v.unknown_cost_categories)

    def test_a_declared_zero_fee_is_evidence_and_passes(self):
        """A promotional zero-fee window is a FACT, not a missing value —
        and the two must not be treated alike in either direction."""
        tid = _perp(declared_absent_costs=("commission_usd", "funding_usd"))
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        v = ML.eligibility(store.get(tid))
        self.assertTrue(v.eligible, v.detail)
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        self.assertAlmostEqual(_learning_rows()[0][3], 60.0, places=6)


# ── B/C. Unfinished trades cannot teach ──────────────────────────────────
class UnfinishedTradesTests(ManualLearningCase):

    def test_an_open_trade_is_blocked(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=1.0)
        v = ML.eligibility(store.get(tid))
        self.assertEqual(v.verdict, ML.BLOCKED_OPEN_TRADE)
        self.assertFalse(ML.apply_manual_outcome(tid)["ok"])
        self.assertEqual(len(_learning_rows()), 0)

    def test_a_partially_closed_trade_is_blocked(self):
        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=1.0)
        store.append_leg(tid, kind=PARTIAL, quantity=40, fill_price=10.5,
                         at=T3, fee_usd=0.5, exit_reason="TARGET_EXIT")
        v = ML.eligibility(store.get(tid))
        self.assertEqual(v.verdict, ML.BLOCKED_OPEN_TRADE)
        self.assertEqual(len(_learning_rows()), 0)

    def test_a_terminated_trade_is_blocked_as_an_invalid_state(self):
        for state in (mx.CANCELLED, mx.ABANDONED):
            tid = _perp()
            if state == mx.ABANDONED:
                store.append_leg(tid, kind=ENTRY, quantity=10,
                                 fill_price=10.0, at=T1, fee_usd=1.0)
            store.terminate(tid, state=state, reason="test")
            v = ML.eligibility(store.get(tid))
            self.assertEqual(v.verdict, ML.BLOCKED_INVALID_STATE, state)
        self.assertEqual(len(_learning_rows()), 0)

    def test_the_gate_is_re_derived_not_read_from_the_stored_status(self):
        """DO NOT TRUST THE PRODUCER. A tampered status must not admit an
        incomplete trade — the consumer recomputes from the legs."""
        from app.database import engine

        tid = _perp()
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        # Forge a clean status directly in the database.
        with engine.begin() as conn:
            conn.execute(text("UPDATE manual_trades SET learning_state='PENDING', "
                              "learning_error=NULL WHERE id=:i"), {"i": tid})
        self.assertEqual(store.get_record(tid)["learning_state"], "PENDING")

        res = ML.apply_manual_outcome(tid)
        self.assertFalse(res["ok"], "a forged status admitted an incomplete trade")
        self.assertEqual(res["verdict"], ML.BLOCKED_INCOMPLETE_COSTS)
        self.assertEqual(len(_learning_rows()), 0)


# ── A/M/H/I/J/K/L. One trade, one outcome, one vote ──────────────────────
class OneTradeOneVoteTests(ManualLearningCase):

    def _scaled_and_closed(self):
        tid = _perp(recommendation=_recommendation())
        store.append_leg(tid, kind=ENTRY, quantity=60, fill_price=10.0,
                         at=T1, fee_usd=3.0)
        store.append_leg(tid, kind=ENTRY, quantity=40, fill_price=10.2,
                         at=T2, fee_usd=2.0)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=1.5,
                                at=T2)
        store.append_cost_event(tid, kind=mx.FUNDING_RECEIVED,
                                amount_usd=0.5, at=T3)
        store.append_leg(tid, kind=PARTIAL, quantity=40, fill_price=10.6,
                         at=T3, fee_usd=2.0, exit_reason="TARGET_EXIT")
        store.append_leg(tid, kind=FINAL, quantity=60, fill_price=10.9,
                         at=T4, fee_usd=3.0, exit_reason="TARGET_EXIT")
        return tid

    def test_a_complete_closed_trade_reaches_learning(self):
        tid = self._scaled_and_closed()
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"], ML.MANUAL_LEARNING_APPLIED)
        rows = _learning_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], LP.MANUAL_OPERATOR)
        self.assertEqual(store.get_record(tid)["learning_state"], "APPLIED")

    def test_five_legs_and_two_funding_events_make_exactly_one_row(self):
        tid = self._scaled_and_closed()
        rec = store.get_record(tid)
        self.assertEqual(len(rec["legs"]), 4)
        self.assertEqual(len(rec["cost_events"]), 2)
        ML.apply_manual_outcome(tid)
        self.assertEqual(len(_learning_rows()), 1,
                         "execution legs became independent observations")

    def test_the_learning_row_id_is_the_trade_id(self):
        """One trade, one row, forever — and the database enforces it via
        the partial unique index on canonical_outcome_id."""
        tid = self._scaled_and_closed()
        ML.apply_manual_outcome(tid)
        row = _learning_rows()[0]
        self.assertEqual(row[0], tid)
        self.assertEqual(row[1], tid)

    def test_one_thesis_does_not_vote_twice_across_arms(self):
        tid = self._scaled_and_closed()
        ML.apply_manual_outcome(tid)
        arm = store.arm_result(tid)
        agent = tt.ArmResult("ml-thesis-1", tt.AGENT, traded=True, net_r=0.3)
        self.assertEqual(tt.sample_count([agent, arm]), 1)

    def test_corrections_do_not_become_observations(self):
        tid = self._scaled_and_closed()
        ML.apply_manual_outcome(tid)
        leg = store.get_record(tid)["legs"][0]["leg_id"]
        for i, fee in enumerate((3.1, 3.2, 3.3)):
            store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                          target_id=leg, field="fee_usd", new_value=fee,
                          reason=f"restatement {i}", corrected_by="operator",
                          evidence_type="OFFICIAL_STATEMENT")
            ML.apply_manual_outcome(tid)
        self.assertEqual(len(store.corrections(tid)), 3)
        self.assertEqual(len(_learning_rows()), 1,
                         "corrections became independent observations")


# ── W/X. Idempotence and correction after projection ─────────────────────
class IdempotenceTests(ManualLearningCase):

    def _closed(self):
        tid = _perp(recommendation=_recommendation())
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        return tid

    def test_repeated_projection_is_idempotent(self):
        tid = self._closed()
        first = ML.apply_manual_outcome(tid)
        self.assertTrue(first["ok"])
        self.assertNotIn("idempotent", first)
        pnl = _learning_rows()[0][3]
        for _ in range(4):
            res = ML.apply_manual_outcome(tid)
            self.assertTrue(res["ok"])
            self.assertTrue(res["idempotent"])
            self.assertEqual(len(_learning_rows()), 1)
            self.assertEqual(_learning_rows()[0][3], pnl)

    def test_the_pending_sweep_is_idempotent_too(self):
        tid = self._closed()
        first = ML.apply_pending_manual_outcomes()
        self.assertEqual(first["applied"], 1)
        second = ML.apply_pending_manual_outcomes()
        self.assertEqual(second["scanned"], 0,
                         "an applied trade stayed in the pending sweep")
        self.assertEqual(len(_learning_rows()), 1)
        self.assertEqual(store.get_record(tid)["learning_state"], "APPLIED")

    def test_a_correction_after_projection_marks_reprojection_not_a_second_vote(self):
        tid = self._closed()
        ML.apply_manual_outcome(tid)
        before_pnl = _learning_rows()[0][3]

        leg = store.get_record(tid)["legs"][1]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg, field="fee_usd", new_value=9.0,
                      reason="official statement restated the exit fee",
                      corrected_by="operator",
                      evidence_type="OFFICIAL_STATEMENT")

        # FAIL CLOSED: flagged, not silently revised and not duplicated.
        self.assertEqual(store.get_record(tid)["learning_state"],
                         store.PENDING_REPROJECTION)
        self.assertEqual(len(_learning_rows()), 1)
        self.assertEqual(_learning_rows()[0][3], before_pnl,
                         "the stale row changed without a re-projection")

        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], ML.MANUAL_LEARNING_REPROJECTED)
        self.assertEqual(len(_learning_rows()), 1,
                         "re-projection cast a SECOND vote")
        self.assertAlmostEqual(_learning_rows()[0][3], before_pnl - 6.0,
                               places=6)
        self.assertEqual(store.get_record(tid)["learning_state"], "APPLIED")

    def test_a_correction_that_breaks_completeness_reblocks_the_row(self):
        tid = self._closed()
        ML.apply_manual_outcome(tid)
        leg = store.get_record(tid)["legs"][0]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg, field="fee_usd", new_value=None,
                      reason="the statement did not itemise this leg",
                      corrected_by="operator")
        res = ML.apply_manual_outcome(tid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["verdict"], ML.BLOCKED_INCOMPLETE_COSTS)
        # The already-cast vote is NOT deleted by a later loss of evidence;
        # it is flagged. Removing it would rewrite history from silence.
        self.assertEqual(len(_learning_rows()), 1)


# ── M/N/O/P/Q/R. Attribution, linkage and separation ─────────────────────
class AttributionTests(ManualLearningCase):

    def test_a_manual_row_is_labelled_manual_operator(self):
        tid = _perp(recommendation=_recommendation())
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        row = _learning_rows()[0]
        self.assertEqual(row[2], LP.MANUAL_OPERATOR)
        self.assertNotEqual(row[2], LP.LIVE)
        self.assertEqual(row[10], mx.MANUAL_EXECUTION_VERSION)

    def test_an_unlinked_trade_fabricates_no_thesis_or_signal(self):
        tid = _perp()
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        row = _learning_rows()[0]
        self.assertIsNone(row[7], "an unlinked trade invented a signal_id")
        pop = ML.operator_population()
        self.assertEqual(pop["independent"]["trades"], 1)
        self.assertEqual(pop["thesis_linked"]["trades"], 0)

    def test_a_linked_trade_is_tallied_apart_from_an_independent_one(self):
        linked = _perp(recommendation=_recommendation())
        _round_trip(linked, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(linked)
        solo = _perp()
        _round_trip(solo, entry_fee=3.0, exit_fee=3.0, funding=1.0,
                    exit_px=9.5)
        ML.apply_manual_outcome(solo)

        pop = ML.operator_population()
        self.assertEqual(pop["all"]["trades"], 2)
        self.assertEqual(pop["thesis_linked"]["trades"], 1)
        self.assertEqual(pop["independent"]["trades"], 1)
        self.assertEqual(pop["thesis_linked"]["wins"], 1)
        self.assertEqual(pop["independent"]["losses"], 1)

    def test_recommendation_and_actual_execution_stay_separate(self):
        tid = _perp(recommendation=_recommendation(entry=10.00,
                                                   expected_fee_usd=0.40))
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, entry_px=10.35,
                    funding=1.0)
        ML.apply_manual_outcome(tid)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertAlmostEqual(cmp["entry"]["recommended"], 10.00)
        self.assertAlmostEqual(cmp["entry"]["actual"], 10.35)
        self.assertAlmostEqual(cmp["fee_usd"]["expected"], 0.40)
        self.assertAlmostEqual(cmp["fee_usd"]["actual"], 6.0)
        self.assertAlmostEqual(cmp["funding_usd"]["expected"], 0.10)
        self.assertAlmostEqual(cmp["funding_usd"]["actual"], 1.0)

    def test_directional_disagreement_survives_projection(self):
        tid = _perp(direction="short",
                    recommendation=_recommendation(direction="long"))
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, entry_px=10.5,
                    exit_px=10.0, funding=1.0)
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertEqual(cmp["direction"]["recommended"], "long")
        self.assertEqual(cmp["direction"]["actual"], "short")
        self.assertFalse(cmp["direction"]["followed"])
        # The learning row records what the OPERATOR did, not the advice.
        from app.database import engine
        with engine.connect() as conn:
            d = conn.execute(text(
                "SELECT direction FROM trade_outcomes WHERE id=:i"),
                {"i": tid}).fetchone()[0]
        self.assertEqual(d, "short")

    def test_recommended_venue_and_actual_venue_stay_separate(self):
        tid = _perp(venue="KRAKEN",
                    recommendation=_recommendation(venue="BTCC"))
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        cmp = store.get_record(tid)["recommendation_vs_actual"]
        self.assertEqual(cmp["venue"], {"recommended": "BTCC",
                                        "actual": "KRAKEN"})


# ── S/T. Account economics survive without becoming global rules ─────────
class AccountEconomicsSurvivalTests(ManualLearningCase):

    def test_a_promotion_does_not_rewrite_the_public_venue_schedule(self):
        import copy

        from lib import venues

        before = copy.deepcopy(venues.VENUE_FEES)
        tid = _perp(declared_absent_costs=("commission_usd",))
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=1.0,
                                at=T2)
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        # The trade learned its OWN zero-fee economics...
        self.assertAlmostEqual(_learning_rows()[0][3], 59.0, places=6)
        # ...and the venue's published schedule is untouched.
        self.assertEqual(before, venues.VENUE_FEES)

    def test_promotional_collateral_does_not_become_owned_capital(self):
        from lib import account_economics as ae

        tid = _perp(collateral_usd=1000.0,
                    collateral_capital_kind=ae.PROMOTIONAL_CREDIT)
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        rec = store.get_record(tid)
        self.assertEqual(rec["owned_collateral_usd"], 0.0)
        # THE DENOMINATOR IS LABELLED. A percentage against margin that is
        # not the operator's money must not read as a return on equity.
        row = _learning_rows()[0]
        self.assertEqual(row[5], ML.BASIS_MARGIN_MIXED_CAPITAL)
        self.assertNotEqual(row[5], ML.BASIS_MARGIN)

    def test_owned_collateral_keeps_the_plain_margin_basis(self):
        tid = _perp(collateral_usd=1000.0,
                    collateral_capital_kind="OWN_CAPITAL")
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        self.assertEqual(_learning_rows()[0][5], ML.BASIS_MARGIN)

    def test_an_unstated_denominator_yields_no_percentage_at_all(self):
        tid = _perp(collateral_usd=None)
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        row = _learning_rows()[0]
        self.assertIsNone(row[4])
        self.assertIsNone(row[5])


# ── U/V. Reconciliation gating ───────────────────────────────────────────
class ReconciliationGateTests(ManualLearningCase):

    def _with_report(self, reported):
        tid = _perp(operator_reported_realized_pnl_usd=reported,
                    operator_reported_evidence_type="OFFICIAL_STATEMENT")
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        return tid

    def test_a_reconciled_report_is_eligible(self):
        # 100 x (10.5-10.0) = 50 gross, -6 fees, -1 funding = 43.
        tid = self._with_report(43.0)
        v = ML.eligibility(store.get(tid))
        self.assertTrue(v.eligible, v.detail)
        self.assertEqual(v.reconciliation_status, "RECONCILED")

    def test_a_rounding_scale_gap_is_tolerated_and_preserved(self):
        tid = self._with_report(43.02)
        v = ML.eligibility(store.get(tid))
        self.assertTrue(v.eligible, v.detail)
        self.assertEqual(v.reconciliation_status, "UNEXPLAINED_VENUE_COST")
        self.assertAlmostEqual(v.unexplained_delta_usd, 0.02, places=6)
        # PRESERVED, not absorbed.
        rec = store.get_record(tid)["reconciliation"]
        self.assertAlmostEqual(rec["delta_usd"], 0.02, places=6)

    def test_a_material_unexplained_gap_blocks_calibration(self):
        tid = self._with_report(30.0)     # $13 unexplained on a $43 result
        v = ML.eligibility(store.get(tid))
        self.assertFalse(v.eligible)
        self.assertEqual(v.verdict,
                         ML.BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS)
        self.assertAlmostEqual(v.unexplained_delta_usd, -13.0, places=6)
        res = ML.apply_manual_outcome(tid)
        self.assertFalse(res["ok"])
        self.assertEqual(len(_learning_rows()), 0)

    def test_the_report_is_never_back_solved_into_the_components(self):
        tid = self._with_report(30.0)
        rec = store.get_record(tid)
        self.assertAlmostEqual(rec["net_pnl_usd"], 43.0, places=6)
        self.assertAlmostEqual(
            rec["reconciliation"]["operator_reported_realized_pnl_usd"], 30.0)
        self.assertNotEqual(rec["costs_usd"]["commission_usd"], 19.0)


# ── AdmissionIsAnAllowlist: the six sites that would have leaked ─────────
class AdmissionIsAnAllowlistTests(unittest.TestCase):

    def test_manual_is_excluded_from_every_jarvis_execution_profile(self):
        self.assertIsNone(LP.weight(LP.MANUAL_OPERATOR,
                                    profile=LP.JARVIS_EXECUTION))
        self.assertIsNone(LP.weight(LP.MANUAL_OPERATOR,
                                    profile=LP.FORWARD_OBSERVED_CERTIFICATION))
        self.assertEqual(LP.weight(LP.MANUAL_OPERATOR,
                                   profile=LP.OPERATOR_EXECUTION), 1.0)

    def test_existing_populations_keep_their_existing_weights(self):
        """The conversion from denylist to allowlist must not have changed
        what `live`, `replay` or a legacy NULL were already worth."""
        self.assertEqual(LP.weight(LP.LIVE, profile=LP.JARVIS_EXECUTION), 1.0)
        self.assertEqual(LP.weight(None, profile=LP.JARVIS_EXECUTION), 1.0)
        self.assertEqual(LP.weight(LP.REPLAY, profile=LP.JARVIS_EXECUTION),
                         LP.REPLAY_WEIGHT)
        self.assertEqual(LP.weight(LP.LIVE,
                                   profile=LP.FORWARD_OBSERVED_CERTIFICATION),
                         1.0)
        self.assertIsNone(LP.weight(LP.REPLAY,
                                    profile=LP.FORWARD_OBSERVED_CERTIFICATION))

    def test_an_uncharacterised_source_fails_closed(self):
        """The inversion. A denylist gave full weight to anything it had not
        been told to distrust — the wrong direction for a number that sizes
        positions."""
        for unknown in ("live_v2", "manual", "MANUAL_OPERATOR", "backtest"):
            for profile in LP.PROFILES:
                self.assertIsNone(LP.weight(unknown, profile=profile),
                                  f"{unknown} was admitted to {profile}")

    def test_an_unknown_profile_raises(self):
        with self.assertRaises(LP.LearningPopulationError):
            LP.weight(LP.LIVE, profile="EVERYTHING")

    def test_the_sql_filter_keeps_legacy_null_rows(self):
        """`outcome_source IN ('live')` does NOT match NULL, and dropping
        every pre-source outcome from calibration would be a large, quiet
        behaviour change."""
        where, params = LP.sql_filter("outcome_source", LP.JARVIS_EXECUTION)
        self.assertIn("IS NULL", where)
        self.assertIn(LP.LIVE, params.values())
        self.assertIn(LP.REPLAY, params.values())
        self.assertNotIn(LP.MANUAL_OPERATOR, params.values())

    def test_no_consumer_still_uses_a_bare_not_replay_denylist(self):
        """STRUCTURAL. A future edit reintroducing `!= "replay"` would
        silently re-admit every population nobody had thought about."""
        offenders = []
        for rel in ("lib/calibration.py", "lib/expectancy.py",
                    "lib/edge_cost_matrix.py", "lib/strategy_lifecycle.py",
                    "jobs/paper_trading.py", "lib/learning_engine.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # `src != "replay"` / `src == "replay"` used as ADMISSION.
                if not isinstance(node, ast.Compare):
                    continue
                if not any(isinstance(o, ast.NotEq) for o in node.ops):
                    continue
                consts = [c.value for c in node.comparators
                          if isinstance(c, ast.Constant)]
                if "replay" in consts:
                    offenders.append(rel)
        self.assertEqual(offenders, [],
                         f"a `!= 'replay'` admission denylist returned in "
                         f"{offenders}")

    def test_the_shared_writer_refuses_an_unlabelled_population(self):
        from lib.canonical_learning import (LearningValidationError,
                                            insert_learning_row)

        with self.assertRaises(LearningValidationError):
            insert_learning_row(None, None, outcome_source="whatever",
                                projected_at="2026-08-20T00:00:00+00:00")


# ── Y/Z/AA. The virtual economy is untouched by LEARNING ─────────────────
class VirtualEconomyIsolationTests(ManualLearningCase):

    def test_projecting_manual_learning_moves_no_virtual_money(self):
        before = _economy_snapshot()
        tid = _perp(recommendation=_recommendation())
        store.append_leg(tid, kind=ENTRY, quantity=60, fill_price=10.0,
                         at=T1, fee_usd=3.0)
        store.append_leg(tid, kind=ENTRY, quantity=40, fill_price=10.2,
                         at=T2, fee_usd=2.0)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=1.0,
                                at=T2)
        store.append_leg(tid, kind=PARTIAL, quantity=40, fill_price=10.6,
                         at=T3, fee_usd=2.0, exit_reason="TARGET_EXIT")
        store.append_leg(tid, kind=FINAL, quantity=60, fill_price=10.9,
                         at=T4, fee_usd=3.0, exit_reason="TARGET_EXIT")
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        ML.apply_pending_manual_outcomes()
        ML.operator_population()
        self.assertEqual(before, _economy_snapshot(),
                         "manual LEARNING moved the virtual economy")

    def test_the_manual_projector_imports_no_book_writer(self):
        tree = ast.parse((ROOT / "lib" / "manual_learning.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                for a in node.names:
                    imported.add(f"{node.module}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        for mod in ("lib.paper_engine", "lib.dex_paper", "lib.dex_wallet",
                    "lib.canonical_entry", "lib.canonical_exit",
                    "lib.canonical_settlement", "lib.settlement_ledger"):
            self.assertNotIn(mod, imported)

    def test_manual_learning_writes_no_paper_realized_outcome(self):
        """A DELTA, not an absolute count: this database is shared with
        every other test module, and some of them legitimately settle
        canonical positions. What must hold is that MANUAL projection adds
        nothing to the virtual book's final-truth table."""
        from app.database import engine

        def _count():
            with engine.connect() as conn:
                return conn.execute(text(
                    "SELECT COUNT(*) FROM paper_realized_outcomes"
                )).fetchone()[0]

        before = _count()
        tid = _perp()
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        self.assertEqual(len(_learning_rows()), 1,
                         "the manual row must exist — otherwise this proves "
                         "nothing")
        self.assertEqual(_count(), before)


# ── AB/AC/AD. No regression, and autonomy stays refused ──────────────────
class NoRegressionTests(ManualLearningCase):

    def test_live_autonomous_remains_unavailable(self):
        from lib import execution_mode as em

        self.assertFalse(em.spec(em.LIVE_AUTONOMOUS).executable_today)
        with self.assertRaises(em.ExecutionModeError):
            em.assert_executable(em.LIVE_AUTONOMOUS)
        # And it is not a learning population either.
        self.assertNotIn("LIVE_AUTONOMOUS", LP.POPULATIONS)

    def test_the_canonical_projector_still_stamps_live(self):
        """AC — the virtual path's population label must not have moved when
        the writer was shared."""
        src = (ROOT / "lib" / "canonical_learning.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "insert_learning_row"]
        self.assertEqual(len(calls), 1)
        kw = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("outcome_source", kw)
        # LP.LIVE, resolved — not a literal that could drift.
        self.assertIsInstance(kw["outcome_source"], ast.Attribute)
        self.assertEqual(kw["outcome_source"].attr, "LIVE")

    def test_manual_rows_never_enter_the_bootstrap_certification(self):
        """A person's trades cannot certify that the PROGRAM is ready."""
        from jobs.paper_trading import _observed_outcome_count

        before = _observed_outcome_count()
        tid = _perp()
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        self.assertEqual(len(_learning_rows(LP.MANUAL_OPERATOR)), 1)
        self.assertEqual(_observed_outcome_count(), before,
                         "an operator trade counted toward the bootstrap gate")

    def test_a_manual_row_moves_no_jarvis_statistic(self):
        before = _metrics_snapshot()
        tid = _perp(recommendation=_recommendation())
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])
        self.assertEqual(len(_learning_rows(LP.MANUAL_OPERATOR)), 1,
                         "the row must exist — this is not a no-op test")
        self.assertEqual(_metrics_snapshot(), before,
                         "an operator trade moved a JARVIS statistic")

    def test_the_operator_population_is_not_an_empty_promise(self):
        """The row must be READ by something, or it is a log, not learning."""
        tid = _perp(recommendation=_recommendation())
        _round_trip(tid, entry_fee=3.0, exit_fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        pop = ML.operator_population()
        self.assertEqual(pop["all"]["trades"], 1)
        self.assertEqual(pop["all"]["wins"], 1)
        self.assertAlmostEqual(pop["all"]["net_pnl_usd"], 43.0, places=6)
        self.assertAlmostEqual(pop["all"]["explicit_fees_usd"], 7.0, places=6)

    def test_an_empty_population_has_no_win_rate_rather_than_zero_percent(self):
        pop = ML.operator_population()
        self.assertEqual(pop["all"]["trades"], 0)
        self.assertIsNone(pop["all"]["win_rate"])
        self.assertIsNone(pop["all"]["net_pnl_usd"])


if __name__ == "__main__":
    unittest.main()
