"""Event-driven evidence capture and the runtime that owns outcome resolution.

Hermetic: the ingest is driven by handing decoded messages to the existing
`apply_message`, exactly as the reader loop would. No socket is opened.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.database import (DecisionObservation, DecisionOutcome,
                          InstrumentQuoteSample, get_db)
from lib import decision_outcome as DO
from lib import evidence_runtime as ER
from lib import range_collector as RC

PERP, VENUE = "CRYPTO_PERP", "kraken_derivatives_us"


def _clear():
    RC.reset_stream_state()
    with get_db() as db:
        db.query(InstrumentQuoteSample).delete()
        db.query(DecisionOutcome).delete()
        db.query(DecisionObservation).delete()


class EventDrivenSamplingTests(unittest.TestCase):
    """MEASURED: a 1Hz poll captured ~13% of real top-of-book changes."""

    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=10)

    def tearDown(self):
        RC.reset_stream_state()

    def test_a_moved_book_is_always_recorded(self):
        for i in range(5):
            r = RC.note_quote(symbol="BTC/USD", product=PERP, venue=VENUE,
                              bid=100.0 + i, ask=101.0 + i,
                              at=self.t0 + timedelta(milliseconds=i * 50))
            self.assertEqual(r, RC.CHANGE)
        self.assertEqual(RC.buffered_count(), 5)

    def test_an_unchanged_book_is_deduped_until_the_heartbeat(self):
        RC.note_quote(symbol="BTC/USD", product=PERP, venue=VENUE,
                      bid=100.0, ask=101.0, at=self.t0)
        # identical, well inside the heartbeat: nothing new to say
        self.assertIsNone(RC.note_quote(
            symbol="BTC/USD", product=PERP, venue=VENUE, bid=100.0, ask=101.0,
            at=self.t0 + timedelta(seconds=1)))
        # identical, past the heartbeat: say the feed is still alive
        self.assertEqual(RC.note_quote(
            symbol="BTC/USD", product=PERP, venue=VENUE, bid=100.0, ask=101.0,
            at=self.t0 + timedelta(seconds=RC.HEARTBEAT_S + 1)), RC.HEARTBEAT)

    def test_sub_second_changes_are_all_kept(self):
        """The fidelity a clock-driven sampler was throwing away."""
        for i in range(40):
            RC.note_quote(symbol="ETH/USD", product=PERP, venue=VENUE,
                          bid=100.0 + i * 0.01, ask=101.0 + i * 0.01,
                          at=self.t0 + timedelta(milliseconds=i * 25))
        self.assertEqual(RC.buffered_count(), 40)   # 40 in one second

    def test_flush_persists_and_empties_the_buffer(self):
        for i in range(10):
            RC.note_quote(symbol="SOL/USD", product=PERP, venue=VENUE,
                          bid=100.0 + i, ask=101.0 + i,
                          at=self.t0 + timedelta(seconds=i))
        self.assertEqual(RC.flush_samples(), 10)
        self.assertEqual(RC.buffered_count(), 0)
        with get_db() as db:
            self.assertEqual(db.query(InstrumentQuoteSample).filter(
                InstrumentQuoteSample.symbol == "SOL/USD").count(), 10)
        self.assertEqual(RC.flush_samples(), 0)      # idempotent

    def test_reconnect_forgets_the_pre_gap_book(self):
        """An identical price after an outage is NEW evidence, not a dupe."""
        RC.note_quote(symbol="BTC/USD", product=PERP, venue=VENUE,
                      bid=100.0, ask=101.0, at=self.t0)
        RC.reset_stream_state()
        r = RC.note_quote(symbol="BTC/USD", product=PERP, venue=VENUE,
                          bid=100.0, ask=101.0,
                          at=self.t0 + timedelta(seconds=1))
        self.assertEqual(r, RC.CHANGE)

    def test_a_one_sided_book_is_not_evidence(self):
        self.assertIsNone(RC.note_quote(symbol="BTC/USD", product=PERP,
                                        venue=VENUE, bid=100.0, ask=None,
                                        at=self.t0))


class IngestBridgeTests(unittest.TestCase):
    """ONE ingest, many consumers — the collector opens no second client."""

    def tearDown(self):
        from lib import bitnomial_market_data as MD
        MD.clear_book_listeners()
        RC.reset_stream_state()

    def test_listeners_are_notified_by_the_existing_ingest(self):
        from lib import bitnomial_market_data as MD
        MD.clear_book_listeners()
        seen = []
        MD.add_book_listener(lambda sym: seen.append(sym))
        MD.apply_message({"type": "level", "symbol": "PBTCUCZ50",
                          "side": "bid", "price": 1, "quantity": 1})
        self.assertEqual(seen, ["PBTCUCZ50"])

    def test_a_failing_listener_never_stops_ingest(self):
        from lib import bitnomial_market_data as MD
        MD.clear_book_listeners()
        ok = []
        MD.add_book_listener(lambda sym: (_ for _ in ()).throw(RuntimeError("x")))
        MD.add_book_listener(lambda sym: ok.append(sym))
        MD.apply_message({"type": "level", "symbol": "PBTCUCZ50",
                          "side": "bid", "price": 1, "quantity": 1})
        self.assertEqual(ok, ["PBTCUCZ50"])

    def test_listener_registration_is_idempotent(self):
        from lib import bitnomial_market_data as MD
        MD.clear_book_listeners()

        def fn(sym):
            pass
        MD.add_book_listener(fn)
        MD.add_book_listener(fn)
        self.assertEqual(len(MD._LISTENERS), 1)

    def test_ws_code_maps_to_the_desk_symbol(self):
        """Passing a WS code where a desk symbol belongs refused all 16."""
        from lib import market_data_runtime as MDR
        self.assertEqual(MDR._desk_symbol("PBTCUCZ50"), "BTC/USD")
        self.assertEqual(MDR._desk_symbol("PETHUIZ50"), "ETH/USD")
        self.assertIsNone(MDR._desk_symbol("NOT_A_PRODUCT"))


class EvidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        ER.stop()

    def test_it_is_not_gated_on_the_trading_scheduler(self):
        import os
        self.assertEqual(os.environ.get("JARVIS_DISABLE_SCHEDULER"), "1")
        self.assertEqual(ER.DISABLE_ENV, "JARVIS_DISABLE_EVIDENCE_RUNTIME")
        prior = os.environ.pop("JARVIS_DISABLE_EVIDENCE_RUNTIME", None)
        try:
            # scheduler still off, and yet evidence work is permitted
            self.assertTrue(ER.evidence_runtime_enabled())
        finally:
            if prior is not None:
                os.environ["JARVIS_DISABLE_EVIDENCE_RUNTIME"] = prior

    def test_disabled_runtime_starts_nothing(self):
        self.assertFalse(ER.start()["started"])

    def test_start_is_idempotent_and_stop_is_clean(self):
        import os
        os.environ.pop("JARVIS_DISABLE_EVIDENCE_RUNTIME", None)
        try:
            self.assertTrue(ER.start()["started"])
            self.assertFalse(ER.start()["started"])   # never a second worker
            self.assertTrue(ER.stop()["stopped"])
            self.assertFalse(ER.stop()["stopped"])    # idempotent
        finally:
            os.environ["JARVIS_DISABLE_EVIDENCE_RUNTIME"] = "1"

    def test_a_cycle_resolves_due_horizons(self):
        t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
        for i in range(16):
            RC.note_quote(symbol="BTC/USD", product=PERP, venue=VENUE,
                          bid=99.5 + i, ask=100.5 + i,
                          at=t0 + timedelta(minutes=i))
        RC.flush_samples()
        with get_db() as db:
            db.add(DecisionObservation(
                observation_id="er-1", symbol="BTC/USD", asset_class="crypto",
                product=PERP, venue=VENUE, side="long", timeframe="15m",
                decision_at=t0.isoformat(), decision_price=100.0,
                bid=99.5, ask=100.5, intended_stop=95.0, intended_target=110.0,
                final_decision="NO_TRADE", binding_constraint="EDGE"))
        r = ER.run_cycle()
        self.assertGreater(r["scheduled"]["scheduled"], 0)
        self.assertGreater(r["resolved"]["resolved"], 0)

    def test_health_reports_the_backlog_honestly(self):
        h = ER.health()
        for k in ("service_running", "cycles", "last_cycle_at",
                  "last_success_at", "last_error", "pending",
                  "overdue", "oldest_overdue_at", "enabled"):
            self.assertIn(k, h)

    def test_a_failing_cycle_is_contained_and_recorded(self):
        import lib.decision_outcome as target
        real = target.schedule_pending_observations
        target.schedule_pending_observations = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            r = ER.run_cycle()
            self.assertIn("error", r)
            self.assertIn("boom", ER.health()["last_error"])
        finally:
            target.schedule_pending_observations = real

    def test_runtime_cannot_reach_the_execution_surface(self):
        """AST, not promise: no trading import exists in this module."""
        import ast
        import pathlib
        src = pathlib.Path("lib/evidence_runtime.py").read_text(encoding="utf-8")
        banned = {"paper_engine", "virtual_orders", "execution_venue",
                  "canonical_entry", "paper_settlement", "learning_engine",
                  "alpaca_client", "kraken_account"}
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                found |= {b for b in banned if b in node.module}
            elif isinstance(node, ast.Import):
                for a in node.names:
                    found |= {b for b in banned if b in a.name}
        self.assertEqual(found, set())

    def test_batch_is_bounded(self):
        """A backlog must not become one unbounded transaction."""
        self.assertLessEqual(ER.BATCH, 1000)
        self.assertGreater(ER.BATCH, 0)


if __name__ == "__main__":
    unittest.main()


class RetentionPolicyTests(unittest.TestCase):
    """Storage is plentiful; the evidence is not reproducible. Keep it."""

    def test_there_is_no_deletion_path(self):
        from lib import evidence_retention as RET
        self.assertTrue(RET.assert_no_delete_path())
        self.assertFalse(RET.DELETE_ENABLED)
        self.assertIsNone(RET.RAW_RETENTION_DAYS)

    def test_projection_uses_measured_rates_not_assumptions(self):
        from lib import evidence_retention as RET
        p = RET.projection()
        # The first design assumed 6 samples/product/min; measured over 900s
        # the venue moves the book ~144 times and we persist ~110.
        self.assertAlmostEqual(p["tob_changes_per_s"], 29.3, places=1)
        self.assertAlmostEqual(p["bytes_per_row"], 353.3, places=1)
        self.assertEqual(p["storage_case"], "CASE_1")
        self.assertLess(p["gb_per_year"], 1000)

    def test_case_escalates_with_volume(self):
        from lib import evidence_retention as RET
        big = RET.projection(tob_changes_per_s=2000.0)
        self.assertIn(big["storage_case"], ("CASE_2", "CASE_3"))

    def test_no_pruning_or_compaction_job_exists(self):
        """A job that deletes evidence must not appear by accident."""
        import pathlib
        import re
        for p in pathlib.Path("jobs").glob("*.py"):
            src = p.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"delete\(\).*InstrumentQuoteSample|"
                          r"InstrumentQuoteSample.*\.delete\(\)", src),
                f"{p} deletes raw evidence")
