"""Collect the execution data now, because it cannot be recovered later.

Phase 4 failed for want of data, not want of model. 4 of 39,821 signals
carried a measured slippage and nothing persisted the order book at all.
No later cleverness reconstructs a measurement nobody took.

Three properties these tests defend:

  The snapshot is taken BEFORE submission. Spread and imbalance read after
  the fill are contaminated by the fill — the order moved the book it would
  be measured against.

  Slippage is SIGNED so positive always means worse than intended. Unsigned,
  a buy filling high and a sell filling low cancel into a comfortable zero
  that says execution is free.

  Unfilled orders are kept. An order that never filled is a real
  observation about liquidity; dropping them biases the dataset toward
  moments when trading happened to be easy.
"""
import unittest

from lib import execution_recorder as rec

TEST_VENUE = "test"


def tearDownModule():
    """These write to the live DB, as the rest of this suite does. Remove
    the synthetic rows so they cannot be mistaken for measured fills."""
    try:
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            db.query(ExecutionSample).filter(
                ExecutionSample.venue == TEST_VENUE).delete(synchronize_session=False)
            db.commit()
    except Exception:
        pass


class SlippageIsSignedAgainstYouTests(unittest.TestCase):
    """The direction bug that makes execution look free."""

    def _slip(self, side, intended, fill):
        row_id = rec.record_intent(signal_id=None, symbol="TEST/USD", side=side,
                                   order_type="market", intended_price=intended,
                                   qty=1.0, venue="test")
        self.assertIsNotNone(row_id)
        self.assertTrue(rec.record_fill(row_id, fill_price=fill))
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            return row.slippage_bps

    def test_a_buy_filled_high_is_positive_slippage(self):
        self.assertGreater(self._slip("buy", 100.0, 100.5), 0)

    def test_a_sell_filled_low_is_ALSO_positive_slippage(self):
        """Both are worse than intended. If one were negative they would
        average out and the book would look free to trade."""
        self.assertGreater(self._slip("sell", 100.0, 99.5), 0)

    def test_a_favourable_fill_is_negative(self):
        self.assertLess(self._slip("buy", 100.0, 99.5), 0)

    def test_buy_and_sell_adversity_do_not_cancel(self):
        buy = self._slip("buy", 100.0, 100.5)
        sell = self._slip("sell", 100.0, 99.5)
        self.assertGreater((buy + sell) / 2, 0, "adverse fills averaged to zero")

    def test_the_magnitudes_match_for_equal_adversity(self):
        self.assertAlmostEqual(self._slip("buy", 100.0, 100.5),
                               self._slip("sell", 100.0, 99.5), places=4)


class SnapshotPrecedesSubmissionTests(unittest.TestCase):

    def test_the_row_exists_before_any_fill(self):
        row_id = rec.record_intent(signal_id=None, symbol="TEST/USD", side="buy",
                                   order_type="limit", intended_price=100.0,
                                   qty=2.0, venue="test")
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.status, rec.PENDING)
            self.assertIsNotNone(row.submitted_at)
            self.assertIsNone(row.fill_price)

    def test_an_unfilled_order_is_retained_as_evidence(self):
        row_id = rec.record_intent(signal_id=None, symbol="TEST/USD", side="buy",
                                   order_type="limit", intended_price=100.0,
                                   qty=1.0, venue="test")
        rec.record_fill(row_id, fill_price=None, status=rec.CANCELLED)
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            self.assertEqual(row.status, rec.CANCELLED)
            self.assertIsNone(row.slippage_bps)

    def test_partial_fills_record_their_ratio(self):
        row_id = rec.record_intent(signal_id=None, symbol="TEST/USD", side="buy",
                                   order_type="market", intended_price=100.0,
                                   qty=10.0, venue="test")
        rec.record_fill(row_id, fill_price=100.1, filled_qty=4.0, status=rec.PARTIAL)
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            self.assertAlmostEqual(row.fill_ratio, 0.4, places=4)

    def test_fill_delay_is_measured(self):
        row_id = rec.record_intent(signal_id=None, symbol="TEST/USD", side="buy",
                                   order_type="market", intended_price=100.0,
                                   qty=1.0, venue="test")
        rec.record_fill(row_id, fill_price=100.0)
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            self.assertIsNotNone(row.fill_delay_ms)
            self.assertGreaterEqual(row.fill_delay_ms, 0.0)


class NeverBlocksTheTradeTests(unittest.TestCase):
    """A recorder that can raise into the execution path is worse than no
    recorder."""

    def test_junk_input_does_not_raise_into_the_execution_path(self):
        """It may still write a row — nulls are excluded by readiness(),
        which requires slippage_bps NOT NULL. What must never happen is an
        exception propagating into the order loop."""
        try:
            rec.record_intent(signal_id=None, symbol=None, side=None,
                              order_type=None, intended_price="not a number",
                              qty=None, venue="test")
        except Exception as e:
            self.fail(f"recorder raised into the execution path: {e}")

    def test_recording_a_fill_for_an_unknown_row_is_false_not_an_exception(self):
        self.assertFalse(rec.record_fill("no-such-row", fill_price=100.0))
        self.assertFalse(rec.record_fill(None, fill_price=100.0))

    def test_microstructure_capture_survives_missing_feeds(self):
        snap = rec.capture_microstructure("NOSUCH/PAIR", venue="test")
        self.assertIn("captured_at", snap)
        # Absence must stay absent — a missing spread must not become 0.0,
        # which would read as a perfectly tight market.
        self.assertNotEqual(snap.get("spread_pct"), 0.0)


class ReadinessIsBluntTests(unittest.TestCase):
    """The failure this guards is training on 4 samples and believing it."""

    def test_it_reports_a_count_and_a_verdict(self):
        r = rec.readiness()
        self.assertIn("samples", r)
        self.assertIn("verdict", r)
        self.assertIn("ready", r)

    def test_the_bar_is_high_because_slippage_is_heavy_tailed(self):
        self.assertGreaterEqual(rec.MIN_SAMPLES_TO_TRAIN, 500)

    def test_it_is_not_ready_on_a_handful_of_fills(self):
        r = rec.readiness()
        if r.get("with_slippage", 0) < rec.MIN_SAMPLES_TO_TRAIN:
            self.assertFalse(r["ready"])


class WiredIntoTheExecutionPathTests(unittest.TestCase):
    """A recorder nobody calls collects nothing."""

    def test_execute_signals_records_intent_before_submitting(self):
        import inspect
        from jobs import execute_signals
        src = inspect.getsource(execute_signals.run)
        self.assertIn("record_intent", src)
        self.assertLess(src.index("record_intent"), src.index("submit_bracket_order("),
                        "the snapshot must be taken BEFORE submission")

    def test_manage_positions_closes_the_loop_on_fills(self):
        import inspect
        from jobs import manage_positions
        self.assertIn("record_fill", inspect.getsource(manage_positions._record_slippage))


if __name__ == "__main__":
    unittest.main()


class BrokerOrderIdIsCapturedTests(unittest.TestCase):
    """The bug that silently disabled slippage measurement entirely.

    submit_bracket_order RETURNS the broker order id. execute_signals.py
    discarded the return value, so alpaca_order_id was only ever written by
    the manual /signals/{id}/execute route. Measured on the live table: 643
    signals marked Executed, 7 with an order id, 4 with a fill price.

    _record_slippage gates on `if not sig.alpaca_order_id: return`, so every
    automatically-executed fill was skipped — which is why slippage sat at
    4 of 39,821 signals, and why the execution recorder would have collected
    intents forever and never one fill.
    """

    def test_the_execution_job_keeps_the_return_value(self):
        import inspect
        from jobs import execute_signals
        src = inspect.getsource(execute_signals.run)
        self.assertNotIn("\n                submit_bracket_order(", src,
                         "the return value of submit_bracket_order is discarded")
        self.assertIn("= submit_bracket_order(", src)

    def test_it_writes_the_id_onto_the_signal(self):
        import inspect
        from jobs import execute_signals
        src = inspect.getsource(execute_signals.run)
        self.assertIn("rec.alpaca_order_id", src)

    def test_submit_bracket_order_actually_returns_one(self):
        """If the client stopped returning an id, the capture above would
        silently write None and we would be back where we started."""
        import inspect
        from lib import alpaca_client
        src = inspect.getsource(alpaca_client.submit_bracket_order)
        self.assertIn("'id'", src)

    def test_the_snapshot_is_tagged_with_the_broker_id(self):
        import inspect
        from jobs import execute_signals
        src = inspect.getsource(execute_signals.run)
        self.assertIn("broker_order_id = broker_id", src.replace("es.", ""))
