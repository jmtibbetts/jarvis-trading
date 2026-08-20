"""The consumer-level half of the bridge: eligible manual evidence MOVES something.

WHAT THE PREVIOUS PASS GOT WRONG. An eligible manual outcome reached
`trade_outcomes` and every existing consumer excluded it — a safe canonical
evidence repository, not a completed learning bridge. Storing a row is not
learning from it.

WHAT MOVES NOW, AND WHY IT IS THE HONEST THING TO MOVE. A manually executed
trade separates prediction from execution BY CONSTRUCTION: JARVIS made the
claim and a PERSON produced the fills. So the learnable quantity is
PREDICTION ERROR — recommended vs paid entry, expected vs charged fee,
expected vs realized R — every term measured rather than inferred, and none
of it requiring the market path this system does not have for a manual trade.

`lib/recommendation_calibration` is that consumer. These tests prove it
actually changes, changes exactly once, and stays empty for every kind of
evidence that must not reach it.
"""
import ast
import copy
import pathlib
import unittest

from sqlalchemy import text

from lib import learning_population as LP
from lib import manual_execution as mx
from lib import manual_learning as ML
from lib import manual_trade_store as store
from lib import recommendation_calibration as RC
from lib import trade_thesis as tt

ENTRY, PARTIAL, FINAL = mx.LEG_ENTRY, mx.LEG_PARTIAL_EXIT, mx.LEG_FINAL_EXIT

T0 = "2026-08-19T14:00:00+00:00"
T1 = "2026-08-19T14:05:00+00:00"
T2 = "2026-08-19T16:00:00+00:00"
T3 = "2026-08-19T18:00:00+00:00"
T4 = "2026-08-20T02:00:00+00:00"

ROOT = pathlib.Path(__file__).parent.parent
VENUE, PRODUCT = "BTCC", "CRYPTO_PERP"


def _rec(**kw):
    base = dict(thesis_id="rc-thesis-1", signal_id="rc-sig-1",
                decision_id="rc-dec-1", recommended_at=T0, direction="long",
                venue=VENUE, product=PRODUCT, symbol="LINK/USDT",
                entry=10.00, stop=9.50, targets=(11.0,), leverage=10.0,
                expected_fee_usd=0.40, expected_funding_usd=0.10,
                expected_cost_usd=0.50, expected_r=1.8, confidence=0.62)
    base.update(kw)
    return mx.RecommendationSnapshot(**base)


def _trade(**kw):
    base = dict(venue=VENUE, product=PRODUCT, symbol="LINK/USDT",
                direction="long", quantity_unit="COINS", multiplier=1.0,
                account_label="btcc-main", leverage=10.0, margin_mode="CROSS",
                collateral_usd=100.0, collateral_capital_kind="OWN_CAPITAL",
                initial_risk_usd=50.0, opened_at=T1,
                evidence_type="OFFICIAL_STATEMENT")
    base.update(kw)
    return store.create(**base)


def _round_trip(tid, *, entry_px=10.35, exit_px=10.90, fee=3.0, funding=1.0,
                qty=100):
    store.append_leg(tid, kind=ENTRY, quantity=qty, fill_price=entry_px,
                     at=T1, fee_usd=fee)
    if funding is not None:
        store.append_cost_event(tid, kind=mx.FUNDING_PAID,
                                amount_usd=funding, at=T2)
    store.append_leg(tid, kind=FINAL, quantity=qty, fill_price=exit_px,
                     at=T4, fee_usd=fee, exit_reason="TARGET_EXIT")


def _cal(**kw):
    return RC.lookup(venue=VENUE, product=PRODUCT, **kw)


def _jarvis_metrics() -> dict:
    """The statistics that must NEVER move for a manual trade."""
    from app.database import engine
    from jobs.paper_trading import _observed_outcome_count
    from lib import calibration, expectancy

    calibration._CACHE["table"] = None
    expectancy._CACHE["table"] = None
    cal = calibration.build_table(force=True)
    with engine.connect() as conn:
        sa = conn.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(total_trades),0), "
            "COALESCE(SUM(wins),0) FROM signal_accuracy")).fetchone()
    return {
        "calibration_total": cal["overall"]["total"],
        "calibration_wins": cal["overall"]["wins"],
        "expectancy_buckets": len(expectancy.build_table()),
        "signal_accuracy": tuple(sa),
        "bootstrap_observed": _observed_outcome_count(),
    }


def _economy() -> dict:
    from app.database import (DexBalance, DexFundingEvent, DexPortfolio,
                              DexPosition, DexTrade, PaperPortfolio,
                              PaperPosition, PaperTrade, get_db)

    with get_db() as db:
        pf = db.query(PaperPortfolio).first()
        return {"cash": pf.cash if pf else None,
                "realized_pnl": pf.realized_pnl if pf else None,
                "paper_positions": db.query(PaperPosition).count(),
                "paper_trades": db.query(PaperTrade).count(),
                "dex_balances": db.query(DexBalance).count(),
                "dex_funding_events": db.query(DexFundingEvent).count(),
                "dex_portfolio": db.query(DexPortfolio).count(),
                "dex_positions": db.query(DexPosition).count(),
                "dex_trades": db.query(DexTrade).count()}


class BridgeCase(unittest.TestCase):
    """Each test starts from an empty MANUAL population and calibration.

    Only manual rows are cleared. Other populations belong to other test
    modules sharing this database.
    """

    def setUp(self):
        from app.database import engine

        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM trade_outcomes WHERE outcome_source = :s"),
                {"s": LP.MANUAL_OPERATOR})
            for t in ("recommendation_calibration_samples",
                      "manual_trade_corrections", "manual_trade_cost_events",
                      "manual_trade_legs", "manual_trades"):
                conn.execute(text(f"DELETE FROM {t}"))


# ── A/B/E/J. The consumer actually moves ─────────────────────────────────
class EligibleEvidenceMovesTheConsumerTests(BridgeCase):

    def test_an_eligible_linked_outcome_changes_the_consumer(self):
        """A — the whole point of the phase."""
        before = _cal()
        self.assertEqual(before["theses"], 0)
        self.assertIsNone(before["direction"]["followed_win_rate"])
        self.assertIsNone(before["cost_accuracy"]["fee_ratio"]["median"])

        tid = _trade(recommendation=_rec())
        _round_trip(tid)
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["calibration"]["contributed"])

        after = _cal()
        self.assertNotEqual(after, before, "the consumer did not change")
        self.assertEqual(after["theses"], 1)
        self.assertEqual(after["direction"]["followed"], 1)
        self.assertEqual(after["direction"]["followed_win_rate"], 1.0)

    def test_the_contributed_fields_are_measured_prediction_error(self):
        """Every figure is a frozen prediction against a measured actual."""
        tid = _trade(recommendation=_rec(entry=10.00, expected_fee_usd=0.40,
                                         expected_r=1.8))
        _round_trip(tid, entry_px=10.35, exit_px=10.90, fee=3.0, funding=1.0)
        ML.apply_manual_outcome(tid)
        c = _cal(account_label="btcc-main")

        # Entry: paid 10.35 against a recommended 10.00 = 350bp WORSE.
        self.assertAlmostEqual(c["entry_deviation_bps"]["median"], 350.0,
                               places=6)
        # Cost model: predicted $0.40, charged $6.00 — wrong by 15x.
        self.assertAlmostEqual(c["cost_accuracy"]["fee_ratio"]["median"],
                               15.0, places=6)
        self.assertAlmostEqual(
            c["cost_accuracy"]["fee_deviation_usd"]["median"], 5.6, places=6)
        # R: expected 1.8, realized (55 gross - 7 costs) / 50 risk = 0.96.
        self.assertAlmostEqual(c["r_deviation"]["median"], -0.84, places=6)

    def test_recommendation_quality_and_execution_quality_stay_separable(self):
        """J — a PERFECT entry with a WRONG cost prediction must show as
        zero execution deviation and large prediction error, not as one
        blended verdict."""
        tid = _trade(recommendation=_rec(entry=10.00, expected_fee_usd=0.40))
        _round_trip(tid, entry_px=10.00, fee=3.0)     # entry exactly as told
        ML.apply_manual_outcome(tid)
        c = _cal(account_label="btcc-main")
        self.assertAlmostEqual(c["entry_deviation_bps"]["median"], 0.0,
                               places=6)
        self.assertAlmostEqual(
            c["cost_accuracy"]["fee_deviation_usd"]["median"], 5.6, places=6)

    def test_entry_deviation_is_signed_so_worse_is_always_positive(self):
        """A long that paid MORE and a short that sold LOWER are the same
        error; a naive sign would cancel them against each other."""
        long_id = _trade(recommendation=_rec(entry=10.00))
        _round_trip(long_id, entry_px=10.10)
        ML.apply_manual_outcome(long_id)
        short_id = _trade(direction="short",
                          recommendation=_rec(thesis_id="rc-thesis-2",
                                              direction="short", entry=10.00))
        _round_trip(short_id, entry_px=9.90, exit_px=9.50)
        ML.apply_manual_outcome(short_id)
        c = _cal(account_label="btcc-main")
        self.assertEqual(c["entry_deviation_bps"]["n"], 2)
        # Both are +100bp WORSE, so the mean is +100 and not 0.
        self.assertAlmostEqual(c["entry_deviation_bps"]["mean"], 100.0,
                               places=6)

    def test_only_the_appropriate_consumer_moves(self):
        """E — calibration changes; every JARVIS statistic does not."""
        before_j = _jarvis_metrics()
        tid = _trade(recommendation=_rec())
        _round_trip(tid)
        ML.apply_manual_outcome(tid)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(_jarvis_metrics(), before_j)


# ── C/D. The poison, measured against the ADMITTED consumer ──────────────
class PoisonAgainstTheAdmittedConsumerTests(BridgeCase):
    """Blocked evidence must move the consumer that DOES admit manual
    evidence — not merely fail to write a trade_outcomes row."""

    def _poison(self):
        tid = _trade(recommendation=_rec())
        # +$60 gross and unevidenced fees. Real costs were $62: a LOSS.
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=None)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.6,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        return tid

    def test_the_poison_moves_the_admitted_consumer_not_at_all(self):
        before_cal, before_j = _cal(), _jarvis_metrics()
        tid = self._poison()
        res = ML.apply_manual_outcome(tid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["verdict"], ML.BLOCKED_INCOMPLETE_COSTS)
        self.assertEqual(_cal(), before_cal,
                         "an incomplete outcome moved the calibration")
        self.assertEqual(_jarvis_metrics(), before_j)
        self.assertEqual(_cal()["theses"], 0)

    def test_an_open_trade_moves_the_admitted_consumer_not_at_all(self):
        before = _cal()
        tid = _trade(recommendation=_rec())
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.0,
                         at=T1, fee_usd=3.0)
        self.assertFalse(ML.apply_manual_outcome(tid)["ok"])
        self.assertEqual(_cal(), before)

    def test_a_materially_unreconciled_trade_moves_it_not_at_all(self):
        before = _cal()
        tid = _trade(recommendation=_rec(),
                     operator_reported_realized_pnl_usd=10.0)
        _round_trip(tid)     # component net is ~48, reported 10
        res = ML.apply_manual_outcome(tid)
        self.assertEqual(res["verdict"],
                         ML.BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS)
        self.assertEqual(_cal(), before)

    def test_supplying_the_fees_then_moves_it_and_only_it(self):
        """The full poison arc, ending at the admitted consumer."""
        before_j = _jarvis_metrics()
        tid = self._poison()
        ML.apply_manual_outcome(tid)
        self.assertEqual(_cal()["theses"], 0)

        for leg in store.get_record(tid)["legs"]:
            store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                          target_id=leg["leg_id"], field="fee_usd",
                          new_value=28.0, reason="BTCC monthly statement",
                          corrected_by="operator",
                          evidence_type="OFFICIAL_STATEMENT")
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=6.0,
                                at=T2)

        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)
        c = _cal(account_label="btcc-main")
        self.assertEqual(c["theses"], 1)
        # The trade that "made $60" contributed a LOSS and a cost model
        # wrong by $61.60.
        self.assertEqual(c["direction"]["followed_wins"], 0)
        self.assertAlmostEqual(
            c["cost_accuracy"]["fee_deviation_usd"]["median"], 55.6, places=6)
        self.assertEqual(_jarvis_metrics(), before_j)

    def test_losing_evidence_after_contributing_withdraws_the_measurement(self):
        """The event happened and is kept; the MEASUREMENT's inputs are no
        longer valid, so it stops counting."""
        tid = _trade(recommendation=_rec())
        _round_trip(tid)
        ML.apply_manual_outcome(tid)
        self.assertEqual(_cal()["theses"], 1)

        leg = store.get_record(tid)["legs"][0]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg, field="fee_usd", new_value=None,
                      reason="the statement did not itemise this leg",
                      corrected_by="operator")
        res = ML.apply_manual_outcome(tid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["calibration_withdrawn"], 1)
        self.assertEqual(_cal()["theses"], 0,
                         "a measurement kept counting after its inputs were "
                         "withdrawn")
        # The EVENT record stands — a fee becoming unknown does not un-know
        # that the trade happened.
        from app.database import engine
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM trade_outcomes WHERE "
                "canonical_outcome_id = :i"), {"i": tid}).fetchone()[0]
        self.assertEqual(n, 1)


# ── B/F/G/L. One thesis, one contribution ────────────────────────────────
class OneThesisOneContributionTests(BridgeCase):

    def _scaled(self, thesis="rc-thesis-1"):
        tid = _trade(recommendation=_rec(thesis_id=thesis))
        store.append_leg(tid, kind=ENTRY, quantity=60, fill_price=10.0,
                         at=T1, fee_usd=2.0)
        store.append_leg(tid, kind=ENTRY, quantity=40, fill_price=10.2,
                         at=T2, fee_usd=1.0)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=1.5,
                                at=T2)
        store.append_cost_event(tid, kind=mx.FUNDING_RECEIVED,
                                amount_usd=0.5, at=T3)
        store.append_leg(tid, kind=PARTIAL, quantity=40, fill_price=10.6,
                         at=T3, fee_usd=1.0, exit_reason="TARGET_EXIT")
        store.append_leg(tid, kind=FINAL, quantity=60, fill_price=10.9,
                         at=T4, fee_usd=2.0, exit_reason="TARGET_EXIT")
        return tid

    def test_many_legs_and_funding_events_contribute_once(self):
        tid = self._scaled()
        ML.apply_manual_outcome(tid)
        self.assertEqual(len(store.get_record(tid)["legs"]), 4)
        self.assertEqual(len(store.get_record(tid)["cost_events"]), 2)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(self._sample_count(), 1)

    def test_repeated_projection_does_not_increase_the_sample(self):
        tid = self._scaled()
        for _ in range(5):
            ML.apply_manual_outcome(tid)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(self._sample_count(), 1)

    def test_corrections_do_not_become_extra_samples(self):
        tid = self._scaled()
        ML.apply_manual_outcome(tid)
        leg = store.get_record(tid)["legs"][0]["leg_id"]
        for i, fee in enumerate((2.1, 2.2, 2.3)):
            store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                          target_id=leg, field="fee_usd", new_value=fee,
                          reason=f"restatement {i}", corrected_by="operator",
                          evidence_type="OFFICIAL_STATEMENT")
            ML.apply_manual_outcome(tid)
        self.assertEqual(len(store.corrections(tid)), 3)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(self._sample_count(), 1)

    def test_a_second_trade_on_the_same_thesis_is_refused_by_name(self):
        """ONE THESIS IS ONE MARKET OBSERVATION, however many times the
        operator acted on it. Refused visibly, not dropped silently."""
        first = self._scaled(thesis="shared-thesis")
        ML.apply_manual_outcome(first)
        second = _trade(recommendation=_rec(thesis_id="shared-thesis"))
        _round_trip(second)
        res = ML.apply_manual_outcome(second)
        # The learning ROW still exists — the trade happened.
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["calibration"]["ok"])
        self.assertEqual(res["calibration"]["result"],
                         RC.REFUSED_THESIS_ALREADY_CONTRIBUTED)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(self._sample_count(), 1)

    def test_the_thesis_arm_count_is_still_one(self):
        tid = self._scaled()
        ML.apply_manual_outcome(tid)
        arm = store.arm_result(tid)
        agent = tt.ArmResult("rc-thesis-1", tt.AGENT, traded=True, net_r=0.3)
        self.assertEqual(tt.sample_count([agent, arm]), 1)

    def test_a_correction_supersedes_the_contribution_in_place(self):
        """L — the database must never hold both the old and the corrected
        fact contributing."""
        tid = self._scaled()
        ML.apply_manual_outcome(tid)
        before = _cal(account_label="btcc-main")
        before_dev = before["cost_accuracy"]["fee_deviation_usd"]["median"]

        leg = store.get_record(tid)["legs"][3]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg, field="fee_usd", new_value=8.0,
                      reason="official statement restated the exit fee",
                      corrected_by="operator",
                      evidence_type="OFFICIAL_STATEMENT")
        self.assertEqual(store.get_record(tid)["learning_state"],
                         store.PENDING_REPROJECTION)

        res = ML.apply_manual_outcome(tid)
        self.assertEqual(res["calibration"]["result"], "SUPERSEDED")
        self.assertEqual(res["calibration"]["revision"], 1)
        after = _cal(account_label="btcc-main")
        self.assertEqual(after["theses"], 1, "the correction double-voted")
        self.assertEqual(self._sample_count(), 1)
        self.assertAlmostEqual(
            after["cost_accuracy"]["fee_deviation_usd"]["median"],
            before_dev + 6.0, places=6)

    def test_the_superseded_row_keeps_the_previous_learned_value(self):
        """Provenance: what it used to say, and that it was replaced."""
        from app.database import engine

        tid = self._scaled()
        ML.apply_manual_outcome(tid)
        leg = store.get_record(tid)["legs"][3]["leg_id"]
        store.correct(tid, target_kind=store.CORRECTION_TARGET_LEG,
                      target_id=leg, field="fee_usd", new_value=8.0,
                      reason="restated", corrected_by="operator",
                      evidence_type="OFFICIAL_STATEMENT")
        ML.apply_manual_outcome(tid)

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT revision, previous_values_json, superseded_at, "
                "actual_fee_usd FROM recommendation_calibration_samples "
                "WHERE manual_trade_id = :m"), {"m": tid}).fetchone()
        import json
        self.assertEqual(row[0], 1)
        prev = json.loads(row[1])
        self.assertAlmostEqual(prev["actual_fee_usd"], 6.0, places=6)
        self.assertAlmostEqual(row[3], 12.0, places=6)
        self.assertTrue(row[2])
        # The correction's own provenance survives alongside it.
        self.assertEqual(store.corrections(tid)[0]["evidence_type"],
                         "OFFICIAL_STATEMENT")

    def _sample_count(self) -> int:
        from app.database import engine

        with engine.connect() as conn:
            return conn.execute(text(
                "SELECT COUNT(*) FROM recommendation_calibration_samples"
            )).fetchone()[0]


# ── H/I. Unlinked and disagreement ───────────────────────────────────────
class LinkageAndDisagreementTests(BridgeCase):

    def test_an_unlinked_trade_contributes_nothing_and_says_why(self):
        """H — no prediction exists, so nothing is scored and no thesis,
        signal or confidence is invented to supply one."""
        before = _cal()
        tid = _trade()
        _round_trip(tid)
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], "the learning row must still be written")
        self.assertFalse(res["calibration"]["contributed"])
        self.assertEqual(res["calibration"]["reason"],
                         RC.REFUSED_NO_RECOMMENDATION)
        self.assertEqual(_cal(), before)
        # It remains visible in the operator-only population.
        self.assertEqual(ML.operator_population()["independent"]["trades"], 1)

    def test_an_opposed_trade_is_never_scored_as_a_followed_win(self):
        """I — JARVIS said LONG, the operator went SHORT and MADE MONEY.
        That must not become evidence that the LONG recommendation won."""
        tid = _trade(direction="short",
                     recommendation=_rec(direction="long"))
        _round_trip(tid, entry_px=10.90, exit_px=10.00)   # a profitable short
        res = ML.apply_manual_outcome(tid)
        self.assertTrue(res["ok"], res)

        c = _cal()
        self.assertEqual(c["direction"]["opposed"], 1)
        self.assertEqual(c["direction"]["followed"], 0)
        self.assertEqual(c["direction"]["followed_wins"], 0)
        self.assertIsNone(c["direction"]["followed_win_rate"],
                          "an opposed trade produced a followed win rate")

    def test_an_opposed_loss_is_not_scored_against_the_recommendation_either(self):
        """The inverse temptation: a LOSING opposed trade is not evidence
        that the recommendation was RIGHT. The operator's window is not
        JARVIS's horizon."""
        tid = _trade(direction="short",
                     recommendation=_rec(direction="long"))
        _round_trip(tid, entry_px=10.00, exit_px=10.90)   # a losing short
        ML.apply_manual_outcome(tid)
        c = _cal()
        self.assertEqual(c["direction"]["opposed"], 1)
        self.assertEqual(c["direction"]["followed"], 0)
        self.assertIsNone(c["direction"]["followed_win_rate"])

    def test_a_different_venue_is_still_a_followed_direction(self):
        tid = _trade(venue="KRAKEN", recommendation=_rec(venue="BTCC"))
        _round_trip(tid)
        ML.apply_manual_outcome(tid)
        c = RC.lookup(venue="KRAKEN", product=PRODUCT)
        self.assertEqual(c["direction"]["followed"], 1)
        from app.database import engine
        with engine.connect() as conn:
            klass, followed = conn.execute(text(
                "SELECT deviation_class, venue_followed FROM "
                "recommendation_calibration_samples WHERE manual_trade_id=:m"),
                {"m": tid}).fetchone()
        self.assertEqual(klass, RC.FOLLOWED_DIFFERENT_VENUE)
        self.assertFalse(followed)

    def test_recommended_and_actual_stay_side_by_side_on_the_sample(self):
        from app.database import engine

        tid = _trade(venue="KRAKEN", direction="short",
                     recommendation=_rec(venue="BTCC", direction="long",
                                         entry=10.00))
        _round_trip(tid, entry_px=10.90, exit_px=10.00)
        ML.apply_manual_outcome(tid)
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT venue_recommended, venue_actual, "
                "direction_recommended, direction_actual, entry_recommended, "
                "entry_actual FROM recommendation_calibration_samples "
                "WHERE manual_trade_id = :m"), {"m": tid}).fetchone()
        self.assertEqual(row[0], "BTCC")
        self.assertEqual(row[1], "KRAKEN")
        self.assertEqual(row[2], "long")
        self.assertEqual(row[3], "short")
        self.assertAlmostEqual(row[4], 10.00)
        self.assertAlmostEqual(row[5], 10.90)


# ── K. Promotions stay account-scoped ────────────────────────────────────
class PromotionScopingTests(BridgeCase):

    def _promotional(self, thesis="rc-thesis-1"):
        tid = _trade(declared_absent_costs=("commission_usd",),
                     recommendation=_rec(thesis_id=thesis))
        store.append_leg(tid, kind=ENTRY, quantity=100, fill_price=10.35,
                         at=T1, fee_usd=None)
        store.append_cost_event(tid, kind=mx.FUNDING_PAID, amount_usd=1.0,
                                at=T2)
        store.append_leg(tid, kind=FINAL, quantity=100, fill_price=10.90,
                         at=T4, fee_usd=None, exit_reason="TARGET_EXIT")
        return tid

    def test_a_promotional_zero_fee_never_enters_venue_cost_accuracy(self):
        """K — it may teach "this account paid zero". It may never teach
        "this venue is free"."""
        tid = self._promotional()
        self.assertTrue(ML.apply_manual_outcome(tid)["ok"])

        venue_scope = _cal()
        self.assertEqual(venue_scope["cost_accuracy"]["samples"], 0)
        self.assertEqual(
            venue_scope["cost_accuracy"]["promotional_samples_excluded"], 1)
        self.assertIsNone(
            venue_scope["cost_accuracy"]["fee_deviation_usd"]["median"])
        # The DIRECTIONAL evidence still counts — the promotion changed the
        # fee, not whether the recommendation's side worked.
        self.assertEqual(venue_scope["direction"]["followed"], 1)

        account_scope = _cal(account_label="btcc-main")
        self.assertEqual(account_scope["cost_accuracy"]["samples"], 1)
        self.assertAlmostEqual(
            account_scope["cost_accuracy"]["fee_deviation_usd"]["median"],
            -0.40, places=6)

    def test_the_public_venue_schedule_is_untouched(self):
        from lib import venues

        before = copy.deepcopy(venues.VENUE_FEES)
        tid = self._promotional()
        ML.apply_manual_outcome(tid)
        self.assertEqual(before, venues.VENUE_FEES)

    def test_a_normal_trade_beside_a_promotional_one_is_not_diluted(self):
        """The venue figure must reflect ONLY the venue-baseline trade."""
        promo = self._promotional(thesis="promo-thesis")
        ML.apply_manual_outcome(promo)
        normal = _trade(recommendation=_rec(thesis_id="normal-thesis"))
        _round_trip(normal, fee=3.0)
        ML.apply_manual_outcome(normal)

        venue_scope = _cal()
        self.assertEqual(venue_scope["theses"], 2)
        self.assertEqual(venue_scope["cost_accuracy"]["samples"], 1)
        self.assertAlmostEqual(
            venue_scope["cost_accuracy"]["fee_deviation_usd"]["median"],
            5.6, places=6)

    def test_the_scope_is_recorded_on_the_row_not_inferred_at_read_time(self):
        from app.database import engine

        tid = self._promotional()
        ML.apply_manual_outcome(tid)
        with engine.connect() as conn:
            scope = conn.execute(text(
                "SELECT cost_evidence_scope FROM "
                "recommendation_calibration_samples WHERE manual_trade_id=:m"),
                {"m": tid}).fetchone()[0]
        self.assertEqual(scope, RC.ACCOUNT_PROMOTIONAL)


# ── M/N/O. Isolation, autonomy, no regression ────────────────────────────
class IsolationAndNoRegressionTests(BridgeCase):

    def test_calibration_contribution_moves_no_virtual_money(self):
        before = _economy()
        tid = _trade(recommendation=_rec())
        _round_trip(tid)
        ML.apply_manual_outcome(tid)
        RC.summary()
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(_economy(), before)

    def test_manual_evidence_never_certifies_autonomy_readiness(self):
        """N — a person's trades cannot certify the PROGRAM is ready."""
        from jobs.paper_trading import _observed_outcome_count

        before = _observed_outcome_count()
        tid = _trade(recommendation=_rec())
        _round_trip(tid)
        ML.apply_manual_outcome(tid)
        self.assertEqual(_cal()["theses"], 1)
        self.assertEqual(_observed_outcome_count(), before)

    def test_admission_still_excludes_manual_from_jarvis_profiles(self):
        """O — the allowlists were the correct change and stay."""
        self.assertIsNone(LP.weight(LP.MANUAL_OPERATOR,
                                    profile=LP.JARVIS_EXECUTION))
        self.assertIsNone(LP.weight(LP.MANUAL_OPERATOR,
                                    profile=LP.FORWARD_OBSERVED_CERTIFICATION))
        self.assertEqual(LP.weight(LP.LIVE, profile=LP.JARVIS_EXECUTION), 1.0)
        self.assertEqual(LP.weight(LP.REPLAY, profile=LP.JARVIS_EXECUTION),
                         LP.REPLAY_WEIGHT)
        self.assertEqual(LP.weight(None, profile=LP.JARVIS_EXECUTION), 1.0)

    def test_the_consumer_reads_no_virtual_book_table(self):
        """Structural: calibration is built from its own samples."""
        tree = ast.parse((ROOT / "lib" / "recommendation_calibration.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name)
        for mod in ("lib.paper_engine", "lib.dex_paper", "lib.dex_wallet",
                    "lib.canonical_settlement", "lib.settlement_ledger"):
            self.assertNotIn(mod, imported)

    def test_the_new_table_is_classified_for_a_cutover(self):
        from lib.cutover_classification import CLASSIFICATION, UNKNOWN_REFUSE

        t = "recommendation_calibration_samples"
        self.assertIn(t, CLASSIFICATION)
        self.assertNotEqual(CLASSIFICATION[t][0], UNKNOWN_REFUSE)

    def test_an_empty_scope_reports_none_rather_than_zero(self):
        c = RC.lookup(venue="NOWHERE", product=PRODUCT)
        self.assertEqual(c["theses"], 0)
        self.assertIsNone(c["direction"]["followed_win_rate"])
        self.assertIsNone(c["entry_deviation_bps"]["median"])
        self.assertIsNone(c["cost_accuracy"]["fee_ratio"]["median"])

    def test_a_ratio_against_a_zero_expectation_is_none_not_infinity(self):
        """"We predicted nothing and were charged $6" is a real finding, and
        an infinite ratio would poison every average it entered."""
        self.assertIsNone(RC._ratio(6.0, 0.0))
        self.assertIsNone(RC._ratio(6.0, None))
        self.assertIsNone(RC._ratio(None, 0.4))
        self.assertAlmostEqual(RC._ratio(6.0, 0.4), 15.0)

    def test_nan_and_infinity_never_enter_a_sample(self):
        self.assertIsNone(RC._f(float("nan")))
        self.assertIsNone(RC._f(float("inf")))
        self.assertIsNone(RC._f(float("-inf")))
        self.assertIsNone(RC._f("not a number"))


if __name__ == "__main__":
    unittest.main()
