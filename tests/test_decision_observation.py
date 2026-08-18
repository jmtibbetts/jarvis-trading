"""A refused opportunity must leave evidence behind.

THE MEASURED REASON THIS EXISTS. 11,952 historical cost-gate rejections
were found in candidate_signals; 11,775 of them have no usable forward
evidence at all. "How much opportunity did the broken cost model suppress?"
came back UNKNOWN — not because the analysis was hard, but because the
evidence was discarded the moment the answer was NO_TRADE.

    candidate -> NO_TRADE -> forgotten

A market JARVIS declines to trade keeps moving, and that movement is free
information about the quality of the refusal. These tests pin that the
record is now kept, that it is an AUDIT TRAIL rather than a second opinion,
and — most importantly — that keeping it changes nothing about the book.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import decision_observation as DO
from lib import product_router as PR

PERP_SYM = "PBTCUCZ50"
TICK = 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
PERP_BID, PERP_ASK = 64_500.0, 64_600.0


def _at(s=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=s)


def _spot_feed(bid=SPOT_BID, ask=SPOT_ASK, age_s=0.2):
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": bid, "ask": ask, "at": _at(age_s)},
        trade_flow=lambda symbol, window=200: None)


def _seed_perp(bid=PERP_BID, ask=PERP_ASK):
    MD.reset_books()
    MD.book_for(PERP_SYM, create=True).apply(
        {"type": "book", "ack_id": "9001", "symbol": PERP_SYM,
         "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
         "bids": [[int(bid / TICK), 50]], "asks": [[int(ask / TICK), 50]]})


BASE = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
        "paper_direction": "Long", "entry_price": 64_400.0,
        "stop_loss": 61_000.0, "target_price": 70_000.0,
        "timeframe": "4H", "id": "sig-obs", "strategy": "breakout"}
PERP = dict(BASE, product=PR.CRYPTO_PERP)
SPOT = dict(BASE, product=PR.CRYPTO_SPOT)


def _rows(symbol=None):
    from app.database import DecisionObservation, get_db
    with get_db() as db:
        q = db.query(DecisionObservation)
        if symbol:
            q = q.filter(DecisionObservation.symbol == symbol)
        return [{c.name: getattr(r, c.name)
                 for c in DecisionObservation.__table__.columns}
                for r in q.all()]


def _clear():
    from app.database import DecisionObservation, PaperPosition, get_db
    with get_db() as db:
        db.query(DecisionObservation).delete()
        db.query(PaperPosition).filter(PaperPosition.symbol == "BTC/USD").delete()
        db.commit()


class ARefusalIsRecordedTests(unittest.TestCase):

    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        MD.reset_books()
        self.addCleanup(MD.reset_books)

    def _attempt(self, signal):
        from lib.canonical_entry import open_canonical_position
        with _spot_feed():
            return open_canonical_position(signal, decision_price=64_400.0)

    def test_a_perp_with_no_book_still_leaves_an_observation(self):
        """THE WHOLE POINT. This is the case that used to vanish."""
        res = self._attempt(PERP)
        self.assertFalse(res.get("ok"))
        rows = _rows("BTC/USD")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_decision"], DO.NO_TRADE)
        self.assertTrue(rows[0]["binding_reason"])

    def test_the_refusal_records_which_KIND_of_constraint_bound(self):
        """"eligible = false" is not diagnostically useful years later.

        A perpetual with no book is a DATA gap, not a CAPABILITY gap: the
        product exists and is executable, we simply could not see it. Those
        two demand completely different remedies — wire a feed, versus stop
        routing to a product that does not exist — and collapsing them is
        how the historical rejections became unreadable.
        """
        self._attempt(PERP)
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["binding_constraint"], DO.DATA)
        self.assertTrue(row["venue_data_failure"])

    def test_a_missing_product_is_a_capability_gap_not_a_data_gap(self):
        """The other side of the same distinction, on an asset with no US
        perpetual at all."""
        res = self._attempt(dict(PERP, asset_symbol="BANK/USD",
                                 entry_price=1.0, stop_loss=0.9,
                                 target_price=1.3))
        self.assertFalse(res.get("ok"))
        row = [r for r in _rows() if r["symbol"] == "BANK/USD"][0]
        self.assertEqual(row["binding_constraint"], DO.CAPABILITY)

    def test_a_venue_gap_is_never_recorded_as_a_thesis_failure(self):
        self._attempt(PERP)
        row = _rows("BTC/USD")[0]
        self.assertNotEqual(row["binding_constraint"], DO.EDGE)
        self.assertNotEqual(row["binding_constraint"], DO.COST)

    def test_gates_reached_before_the_refusal_are_recorded_individually(self):
        self._attempt(PERP)
        gates = json.loads(_rows("BTC/USD")[0]["gate_results"])
        self.assertEqual(gates["side_parse"], "PASS")
        self.assertEqual(gates["executable_quote"], "FAIL")

    def test_an_unreadable_side_is_observed_too(self):
        res = self._attempt(dict(PERP, paper_direction="sideways"))
        self.assertIn("unparseable", res["error"])
        self.assertEqual(_rows("BTC/USD")[0]["binding_reason"],
                         "UNPARSEABLE_SIDE")

    def test_a_refusal_opens_nothing_and_moves_no_cash(self):
        """An observation is evidence, not a trade."""
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            before = float(db.query(PaperPortfolio).first().cash)
            n_before = db.query(PaperPosition).count()
        self._attempt(PERP)
        with get_db() as db:
            after = float(db.query(PaperPortfolio).first().cash)
            n_after = db.query(PaperPosition).count()
        self.assertEqual(before, after)
        self.assertEqual(n_before, n_after)
        self.assertIsNone(_rows("BTC/USD")[0]["position_id"])
        self.assertIsNone(_rows("BTC/USD")[0]["execution_id"])


class AnAcceptedTradeIsObservedTheSameWayTests(unittest.TestCase):

    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        _seed_perp()
        self.addCleanup(MD.reset_books)

    def _open(self, signal=PERP):
        from lib.canonical_entry import open_canonical_position
        with _spot_feed():
            return open_canonical_position(signal, decision_price=64_400.0)

    def test_an_accepted_trade_writes_one_observation(self):
        res = self._open()
        self.assertTrue(res.get("ok"), res)
        rows = _rows("BTC/USD")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_decision"], DO.TRADE)
        self.assertEqual(rows[0]["binding_constraint"], DO.NONE_BINDING)

    def test_it_links_to_the_position_it_produced(self):
        res = self._open()
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["position_id"], res["position"]["id"])
        self.assertEqual(res["observation_id"], row["observation_id"])

    def test_the_perp_observation_names_bitnomial_as_the_data_source(self):
        self._open()
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["product"], PR.CRYPTO_PERP)
        self.assertEqual(row["market_data_source"], "bitnomial_public_book")
        self.assertEqual(row["provider_product_code"], "PBTCUC")
        self.assertEqual(row["instrument_id"], PERP_SYM)

    def test_a_spot_observation_names_the_spot_book_instead(self):
        _clear()
        self._open(SPOT)
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["product"], PR.CRYPTO_SPOT)
        self.assertEqual(row["market_data_source"], "kraken_stream.latest_quote")

    def test_the_market_state_actually_used_is_persisted(self):
        self._open()
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["bid"], PERP_BID)
        self.assertEqual(row["ask"], PERP_ASK)
        self.assertIsNotNone(row["quote_age_ms"])
        self.assertEqual(row["quote_status"], "AVAILABLE")
        self.assertEqual(row["book_ack_id"], "9001")

    def test_depth_is_summarised_not_dumped(self):
        """A full book per decision would be the largest table here within
        a month, for data almost none of which is ever read."""
        self._open()
        depth = json.loads(_rows("BTC/USD")[0]["depth_summary"])
        self.assertIn("top_bids", depth)
        self.assertLessEqual(len(depth["top_bids"]), 3)

    def test_risk_comes_from_the_real_authorization(self):
        res = self._open()
        row = _rows("BTC/USD")[0]
        self.assertAlmostEqual(row["authorized_qty"],
                               res["execution"]["filled_quantity"], places=9)
        self.assertGreater(row["committed_margin_usd"], 0)
        self.assertGreater(row["authorized_risk_usd"], 0)

    def test_the_fee_and_its_quality_are_persisted(self):
        self._open()
        row = _rows("BTC/USD")[0]
        self.assertGreater(row["entry_fee_usd"], 0)
        self.assertEqual(row["fee_basis"], "PER_CONTRACT")
        prov = json.loads(row["cost_provenance"])
        self.assertEqual(prov["fee_quality"], "EXCHANGE_SCHEDULE")
        self.assertTrue(prov["fee_is_measured"])

    def test_a_fallback_cost_could_never_pass_as_measured(self):
        """`fee_is_measured` is the field that keeps an estimate out of
        calibration; it must be a real boolean, not absent."""
        self._open()
        prov = json.loads(_rows("BTC/USD")[0]["cost_provenance"])
        self.assertIn("fee_is_measured", prov)
        self.assertIsInstance(prov["fee_is_measured"], bool)


class OneMarketEventIsOneObservationTests(unittest.TestCase):
    """Arms of one thesis must not each look like an independent sample."""

    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        MD.reset_books()
        self.addCleanup(MD.reset_books)

    EVENT = dict(PERP, event_at="2026-08-18T14:00:00Z")

    def _attempt(self, signal):
        from lib.canonical_entry import open_canonical_position
        with _spot_feed():
            return open_canonical_position(signal, decision_price=64_400.0)

    def test_a_retry_at_a_different_wall_clock_writes_ONE_row(self):
        """THE DEFECT. The anchor used to be `_now()`, so a cycle at
        12:00:00 and its retry at 12:00:01 hashed differently and the retry
        wrote a SECOND observation of the same market event.

        This goes through the real canonical path twice rather than handing
        the helper a fixed timestamp, which would prove only that a constant
        hashes to a constant.
        """
        self._attempt(self.EVENT)
        self._attempt(self.EVENT)          # genuinely later wall-clock
        rows = _rows("BTC/USD")
        self.assertEqual(len(rows), 1, "a retry created a second observation")
        self.assertEqual(rows[0]["identity_quality"],
                         DO.IDENTITY_SIGNAL_EVENT_TIME)

    def test_a_genuinely_new_market_event_gets_its_own_observation(self):
        """Re-evaluation is a new observation; a retry is not."""
        self._attempt(self.EVENT)
        self._attempt(dict(self.EVENT, event_at="2026-08-18T14:05:00Z"))
        self.assertEqual(len(_rows("BTC/USD")), 2)

    def test_arms_of_one_event_share_its_identity(self):
        """Agent and control arms of the same observation must not each
        become an independent market sample."""
        agent = DO.build(signal=self.EVENT, decision=DO.TRADE)
        control = DO.build(signal=self.EVENT, decision=DO.NO_TRADE,
                           binding_reason="CONTROL_ARM")
        self.assertEqual(agent["observation_id"], control["observation_id"])

    def test_an_explicit_event_id_outranks_everything(self):
        row = DO.build(signal=dict(self.EVENT, market_event_id="evt-9"),
                       decision=DO.NO_TRADE, binding_reason="X")
        self.assertEqual(row["identity_quality"], DO.IDENTITY_EXPLICIT)

    def test_a_wall_clock_anchor_is_labelled_unstable_rather_than_hidden(self):
        """When nothing upstream offers a stable anchor the row says so, so
        a duplicate is diagnosable instead of mysterious."""
        bare = {k: v for k, v in BASE.items() if k != "id"}
        row = DO.build(signal=bare, decision=DO.NO_TRADE, binding_reason="X")
        self.assertEqual(row["identity_quality"], DO.IDENTITY_UNSTABLE)

    def test_recording_the_same_event_twice_writes_one_row(self):
        row = DO.build(signal=self.EVENT, decision=DO.NO_TRADE,
                       binding_reason="STALE_EXECUTION_DATA")
        self.assertEqual(DO.record(row), DO.record(dict(row)))
        self.assertEqual(len(_rows("BTC/USD")), 1)

    def test_a_replayed_write_never_rewrites_the_judgment(self):
        """Hindsight editing its own paper trail is how a learning system
        lies to itself."""
        row = DO.build(signal=self.EVENT, decision=DO.NO_TRADE,
                       binding_reason="STALE_EXECUTION_DATA")
        DO.record(row)
        DO.record(dict(row, final_decision=DO.TRADE, binding_reason="CHANGED",
                       bid=1.0, edge_threshold_r=99.0))
        stored = _rows("BTC/USD")[0]
        self.assertEqual(stored["final_decision"], DO.NO_TRADE)
        self.assertEqual(stored["binding_reason"], "STALE_EXECUTION_DATA")
        self.assertNotEqual(stored["edge_threshold_r"], 99.0)

    def test_only_append_only_lifecycle_fields_may_complete(self):
        row = DO.build(signal=self.EVENT, decision=DO.TRADE)
        DO.record(row)
        DO.record(dict(row, position_id="pos-123", execution_id="exec-1",
                       execution_state=DO.EXEC_SETTLED))
        stored = _rows("BTC/USD")[0]
        self.assertEqual(stored["position_id"], "pos-123")
        self.assertEqual(stored["execution_id"], "exec-1")
        self.assertEqual(stored["execution_state"], DO.EXEC_SETTLED)


class AnAcceptedTradeFailsClosedWithoutItsRecordTests(unittest.TestCase):
    """A CANONICAL POSITION MUST NOT EXIST WITHOUT THE DECISION RECORD THAT
    AUTHORIZED IT.

    For a refusal, losing the observation costs evidence and nothing else.
    For an accepted trade it would create the exact state this subsystem
    exists to prevent: a position in the book that nobody can explain —
    indistinguishable, later, from the 11,775 historical decisions that
    cannot be judged.
    """

    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        _seed_perp()
        self.addCleanup(MD.reset_books)

    def _book(self):
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            return (float(db.query(PaperPortfolio).first().cash),
                    db.query(PaperPosition).count())

    def _open_with(self, patch_target, **kw):
        from lib.canonical_entry import open_canonical_position
        with _spot_feed(), patch(patch_target, **kw):
            return open_canonical_position(PERP, decision_price=64_400.0)

    def test_persistence_failure_settles_nothing(self):
        before = self._book()
        res = self._open_with("lib.decision_observation.record",
                              return_value=None)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], "DECISION_OBSERVATION_PERSIST_FAILED")
        self.assertEqual(self._book(), before,
                         "cash or positions moved despite a lost audit record")

    def test_a_raising_build_also_fails_closed(self):
        before = self._book()
        res = self._open_with("lib.decision_observation.build",
                              side_effect=RuntimeError("injected"))
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], "DECISION_OBSERVATION_PERSIST_FAILED")
        self.assertEqual(self._book(), before)

    def test_no_fee_is_debited_when_the_record_cannot_be_written(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            before = float(db.query(PaperPortfolio).first().cash)
        self._open_with("lib.decision_observation.record", return_value=None)
        with get_db() as db:
            self.assertEqual(float(db.query(PaperPortfolio).first().cash),
                             before)

    def test_a_settlement_failure_leaves_the_decision_as_TRADE(self):
        """JARVIS did decide to trade, and the venue did produce a fill.
        Rewriting the judgment to NO_TRADE would be a lie about what
        happened; the LIFECYCLE carries the truth instead."""
        res = self._open_with("lib.paper_engine.settle_position_entry",
                              return_value={"ok": False, "error": "injected"})
        self.assertFalse(res.get("ok"))
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["final_decision"], DO.TRADE)
        self.assertEqual(row["execution_state"], DO.EXEC_SETTLEMENT_FAILED)
        self.assertIsNotNone(row["execution_id"])
        self.assertIsNone(row["position_id"])

    def test_a_failed_settlement_is_never_calibration_evidence(self):
        self._open_with("lib.paper_engine.settle_position_entry",
                        return_value={"ok": False, "error": "injected"})
        self.assertFalse(
            DO.is_execution_calibration_eligible(_rows("BTC/USD")[0]))


class TheCausalChainIsCompleteTests(unittest.TestCase):

    def setUp(self):
        _clear()
        self.addCleanup(_clear)
        _seed_perp()
        self.addCleanup(MD.reset_books)

    def _open(self):
        from lib.canonical_entry import open_canonical_position
        with _spot_feed():
            return open_canonical_position(PERP, decision_price=64_400.0)

    def test_the_observation_links_the_real_execution_identity(self):
        """`ExecutionResult` carries no id of its own, so the entry
        execution identity is minted once and used for BOTH the provenance
        stamp and this link. It used to be generated inside
        build_provenance where nothing else could see it, leaving
        execution_id permanently NULL."""
        res = self._open()
        row = _rows("BTC/USD")[0]
        from app.database import PaperPosition, get_db
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            stamped = json.loads(pos.execution_provenance)["entry_execution_id"]
        self.assertIsNotNone(row["execution_id"])
        self.assertEqual(row["execution_id"], stamped)

    def test_a_settled_trade_reaches_the_settled_state(self):
        res = self._open()
        row = _rows("BTC/USD")[0]
        self.assertEqual(row["execution_state"], DO.EXEC_SETTLED)
        self.assertEqual(row["position_id"], res["position"]["id"])
        self.assertIsNotNone(row["settlement_at"] or row["execution_state"])

    def test_a_settled_canonical_trade_is_calibration_eligible(self):
        self._open()
        self.assertTrue(
            DO.is_execution_calibration_eligible(_rows("BTC/USD")[0]))


class CalibrationEligibilityIsAPredicateTests(unittest.TestCase):
    """`source == FORWARD_CANONICAL` was never sufficient: the source is
    written at T0, before settlement is known."""

    def _row(self, **kw):
        base = {"source": DO.FORWARD_CANONICAL, "final_decision": DO.TRADE,
                "execution_id": "e1", "execution_state": DO.EXEC_SETTLED,
                "position_id": "p1"}
        base.update(kw)
        return base

    def test_a_fully_settled_canonical_trade_qualifies(self):
        self.assertTrue(DO.is_execution_calibration_eligible(self._row()))

    def test_a_decision_to_trade_with_no_execution_does_not(self):
        self.assertFalse(DO.is_execution_calibration_eligible(
            self._row(execution_id=None, execution_state=None)))

    def test_a_simulated_fill_that_never_settled_does_not(self):
        self.assertFalse(DO.is_execution_calibration_eligible(
            self._row(execution_state=DO.EXEC_SIMULATED_FILLED,
                      position_id=None)))

    def test_a_rejected_observation_never_qualifies(self):
        self.assertFalse(DO.is_execution_calibration_eligible(
            self._row(source=DO.FORWARD_REJECTED_OBSERVATION,
                      final_decision=DO.NO_TRADE)))

    def test_counterfactual_and_backtest_never_qualify(self):
        for src in (DO.COUNTERFACTUAL_REPLAY, DO.HISTORICAL_BACKTEST):
            with self.subTest(source=src):
                self.assertFalse(
                    DO.is_execution_calibration_eligible(self._row(source=src)))


class EvidenceProvenanceIsExplicitTests(unittest.TestCase):

    def test_the_source_taxonomy_exists_before_it_is_needed(self):
        for s in (DO.FORWARD_CANONICAL, DO.FORWARD_REJECTED_OBSERVATION,
                  DO.LEGACY_FORWARD_VIRTUAL, DO.HISTORICAL_BACKTEST,
                  DO.COUNTERFACTUAL_REPLAY):
            with self.subTest(source=s):
                self.assertIn(s, DO.ALL_SOURCES)

    def test_only_executed_forward_sources_may_inform_calibration(self):
        """A refused opportunity was never filled, so it carries no evidence
        about what filling costs."""
        self.assertNotIn(DO.FORWARD_REJECTED_OBSERVATION,
                         DO.FORWARD_EXECUTED_SOURCES)
        self.assertNotIn(DO.COUNTERFACTUAL_REPLAY, DO.FORWARD_EXECUTED_SOURCES)
        self.assertIn(DO.FORWARD_CANONICAL, DO.FORWARD_EXECUTED_SOURCES)

    def test_a_rejected_observation_is_labelled_as_such(self):
        row = DO.build(signal=BASE, decision=DO.NO_TRADE, binding_reason="X")
        self.assertEqual(row["source"], DO.FORWARD_REJECTED_OBSERVATION)

    def test_an_unmapped_reason_is_flagged_rather_than_bucketed(self):
        """Guessing a category would reproduce exactly the ambiguity that
        made the historical data unusable."""
        self.assertEqual(DO.constraint_for("SOMETHING_NEW"), "UNCLASSIFIED")

    def test_known_reasons_map_to_the_right_kind_of_constraint(self):
        self.assertEqual(DO.constraint_for("STALE_EXECUTION_DATA"), DO.DATA)
        self.assertEqual(DO.constraint_for("NO_EXECUTABLE_PERP_QUOTE"),
                         DO.CAPABILITY)
        self.assertEqual(DO.constraint_for("FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL"),
                         DO.COST)
        self.assertEqual(DO.constraint_for("REFUSED_EXCEEDS_RISK"), DO.RISK)


class TheThresholdIsRecordedNotChangedTests(unittest.TestCase):

    def test_distance_to_threshold_is_derived_when_all_three_are_known(self):
        row = DO.build(signal=BASE, decision=DO.NO_TRADE,
                       binding_reason="EDGE_BELOW_THRESHOLD",
                       gross_expected_r=0.42, estimated_cost_r=0.20,
                       edge_threshold_r=0.50)
        self.assertAlmostEqual(row["expected_net_r"], 0.22, places=9)
        self.assertAlmostEqual(row["distance_to_threshold_r"], -0.28, places=9)

    def test_nothing_is_invented_when_the_inputs_are_absent(self):
        """A decision that never computed an edge must not acquire one."""
        row = DO.build(signal=BASE, decision=DO.NO_TRADE, binding_reason="X")
        self.assertIsNone(row.get("expected_net_r"))
        self.assertIsNone(row.get("distance_to_threshold_r"))


if __name__ == "__main__":
    unittest.main()
