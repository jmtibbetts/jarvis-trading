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
        MD.reset_books()
        self.addCleanup(MD.reset_books)

    def test_the_identity_is_deterministic_for_the_same_event(self):
        a = DO.observation_id_for(signal_id="s1", symbol="BTC/USD",
                                  decision_at="2026-08-18T00:00:00Z")
        b = DO.observation_id_for(signal_id="s1", symbol="BTC/USD",
                                  decision_at="2026-08-18T00:00:00Z")
        self.assertEqual(a, b)

    def test_a_different_event_gets_a_different_identity(self):
        a = DO.observation_id_for(signal_id="s1", symbol="BTC/USD",
                                  decision_at="2026-08-18T00:00:00Z")
        b = DO.observation_id_for(signal_id="s2", symbol="BTC/USD",
                                  decision_at="2026-08-18T00:00:00Z")
        self.assertNotEqual(a, b)

    def test_recording_the_same_event_twice_writes_one_row(self):
        row = DO.build(signal=BASE, decision=DO.NO_TRADE,
                       binding_reason="STALE_EXECUTION_DATA",
                       decision_at="2026-08-18T00:00:00Z")
        first = DO.record(row)
        second = DO.record(dict(row))
        self.assertEqual(first, second)
        self.assertEqual(len(_rows("BTC/USD")), 1)

    def test_a_replayed_write_never_rewrites_the_judgment(self):
        """Hindsight editing its own paper trail is how a learning system
        lies to itself."""
        row = DO.build(signal=BASE, decision=DO.NO_TRADE,
                       binding_reason="STALE_EXECUTION_DATA",
                       decision_at="2026-08-18T00:00:00Z")
        DO.record(row)
        DO.record(dict(row, final_decision=DO.TRADE, binding_reason="CHANGED"))
        stored = _rows("BTC/USD")[0]
        self.assertEqual(stored["final_decision"], DO.NO_TRADE)
        self.assertEqual(stored["binding_reason"], "STALE_EXECUTION_DATA")

    def test_late_linkage_is_the_only_thing_that_may_be_filled_in(self):
        row = DO.build(signal=BASE, decision=DO.TRADE,
                       decision_at="2026-08-18T00:00:00Z")
        DO.record(row)
        DO.record(dict(row, position_id="pos-123"))
        self.assertEqual(_rows("BTC/USD")[0]["position_id"], "pos-123")


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
