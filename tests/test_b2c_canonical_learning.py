"""B2C — one settled truth, one learning row, exactly once.

Every outcome here is produced by the REAL chain — canonical entry, B2B
exit, B2A settlement — and then projected by `apply_realized_outcome`. The
golden rule, learning edition: THE BOT MUST NEVER LEARN A RESULT DIFFERENT
FROM THE ONE IT ACTUALLY SETTLED. So the projection copies persisted truth
under validation and these tests attack every way it could stop doing that:
recomputation, hindsight, epoch drift, clock drift, an unlabelled return,
an unlabelled quantity, a double vote.
"""
import hashlib
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import canonical_exit as CX
from lib import canonical_learning as CL
from lib import canonical_settlement as CS
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
ENTRY_BID, ENTRY_ASK = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-b2c-{next(_seq)}",
            "product": PR.CRYPTO_PERP}
    base.update(over)
    return base


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _spot_feed():
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": SPOT_BID, "ask": SPOT_ASK,
                                     "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


def _seed_book(bid=ENTRY_BID, ask=ENTRY_ASK):
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid / TICK), 50]],
                "asks": [[int(ask / TICK), 50]]})
    return book


class _B2CHarness(unittest.TestCase):

    def setUp(self):
        _seed_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

        def _close_leftovers():
            with get_db() as db:
                db.query(PaperPosition).filter(
                    PaperPosition.symbol == "BTC/USD",
                    PaperPosition.status == "Open").update(
                    {"status": "Closed"})
                db.commit()
        self.addCleanup(_close_leftovers)

    def _settled_outcome(self, signal=None, exit_bid=64_900.0,
                         exit_ask=65_000.0):
        """Real entry -> real B2B final close -> PENDING outcome row."""
        from lib import canonical_entry as CE
        from app.database import PaperRealizedOutcome, get_db
        with _spot_feed():
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        pos_id = res["position"]["id"]
        _seed_book(bid=exit_bid, ask=exit_ask)
        out = CX.close_canonical_position(pos_id,
                                          exit_reason=CS.VOLUNTARY_EXIT)
        self.assertTrue(out.get("ok"), out)
        with get_db() as db:
            o = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.position_id == pos_id).one()
            db.expunge_all()
        self.assertEqual(o.learning_state, "PENDING")
        return o

    def _outcome_row(self, outcome_id):
        from sqlalchemy import text
        from app.database import engine
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT * FROM trade_outcomes WHERE canonical_outcome_id=:c"),
                {"c": outcome_id}).mappings().fetchall()
        return row

    def _fresh(self, outcome_id):
        from app.database import PaperRealizedOutcome, get_db
        with get_db() as db:
            o = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.id == outcome_id).one()
            db.expunge_all()
        return o

    def _corrupt_outcome(self, outcome_id, **fields):
        from app.database import PaperRealizedOutcome, get_db
        with get_db() as db:
            db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.id == outcome_id).update(fields)
            db.commit()

    def _financial_hash(self):
        """Everything money-shaped, hashed. Learning may change ONLY the
        outcome's learning metadata."""
        from sqlalchemy import text
        from app.database import engine
        h = hashlib.sha256()
        with engine.connect() as conn:
            for table, cols in (
                    ("paper_positions", "id,qty,margin_used,notional,status,"
                                        "entry_price"),
                    ("paper_position_settlements",
                     "id,original_quantity,committed_margin_usd,"
                     "entry_fee_usd,status,settlement_revision"),
                    ("paper_settlement_legs",
                     "id,filled_qty,gross_pnl_usd,explicit_fee_usd,"
                     "holding_cost_usd,released_margin_usd"),
                    ("paper_portfolio", "id,cash,total_trades,"
                                        "winning_trades,realized_pnl"),
                    ("paper_trades", "id,gross_pnl,fees,realized_pnl"),
                    ("paper_realized_outcomes",
                     "id,gross_pnl_usd,net_pnl_usd,commission_usd,"
                     "funding_usd,net_return_pct,outcome,quantity,"
                     "actual_entry_fill,actual_exit_fill")):
                for row in conn.execute(text(
                        f"SELECT {cols} FROM {table} ORDER BY id")):
                    h.update(repr(tuple(row)).encode())
        return h.hexdigest()


class PbtcLongProjectionTests(_B2CHarness):
    """§46 — the acceptance: every learning field is the persisted truth."""

    def test_the_projection_copies_settlement_truth_exactly(self):
        o = self._settled_outcome()
        res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)

        rows = self._outcome_row(o.id)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["id"], o.id, "one truth, one id")
        self.assertEqual(r["canonical_outcome_id"], o.id)
        self.assertEqual(r["position_id"], o.position_id)
        self.assertEqual(r["symbol"], "BTC/USD")
        self.assertEqual(r["product"], PR.CRYPTO_PERP)
        self.assertEqual(r["instrument_id"], PERP_SYM)
        self.assertEqual(r["qty"], o.quantity)
        self.assertEqual(r["quantity_unit"], "CONTRACTS")
        self.assertAlmostEqual(r["multiplier"], 0.01)
        self.assertEqual(r["direction"], "long")
        self.assertEqual(r["entry_price"], o.actual_entry_fill)
        self.assertEqual(r["exit_price"], o.actual_exit_fill)
        self.assertAlmostEqual(r["pnl_usd"], o.net_pnl_usd, places=4)
        self.assertAlmostEqual(r["pnl_pct"], o.net_return_pct, places=4)
        self.assertEqual(r["return_pct_basis"], "MARGIN")
        self.assertEqual(r["outcome"], o.outcome)
        self.assertEqual(r["entered_at"], o.opened_at)
        self.assertEqual(r["exited_at"], o.closed_at)
        self.assertAlmostEqual(r["hold_duration_m"], o.hold_minutes,
                               places=6)
        self.assertEqual(r["engine_epoch"], o.engine_epoch)
        self.assertEqual(r["outcome_version"], "outcome_v2_settlement")
        self.assertEqual(r["settlement_version"], "paper_settlement_v1")
        self.assertEqual(r["paper_mode"], 1)
        self.assertIsNotNone(r["projected_at"])
        # Decision-frozen timeframe reached the row via the observation.
        self.assertEqual(r["timeframe"], "4H")

        o2 = self._fresh(o.id)
        self.assertEqual(o2.learning_state, "APPLIED")
        self.assertEqual(o2.trade_outcome_id, o.id)
        self.assertIsNotNone(o2.learning_applied_at)
        self.assertIsNone(o2.learning_error)

    def test_signal_accuracy_reflects_the_projection(self):
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        self.assertTrue(CL.apply_realized_outcome(o.id).get("ok"))
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT total_trades FROM signal_accuracy "
                "WHERE symbol='BTC/USD'")).fetchone()
        self.assertIsNotNone(n)
        self.assertGreaterEqual(n[0], 1)


class PbtcShortProjectionTests(_B2CHarness):
    """§47 — the learner records the settled short, never re-derives it."""

    def test_a_short_projects_its_settled_sign(self):
        o = self._settled_outcome(
            _signal(paper_direction="Short", stop_loss=68_000.0,
                    target_price=58_000.0),
            exit_bid=63_000.0, exit_ask=63_100.0)
        self.assertEqual(o.side, "short")
        self.assertGreater(o.gross_pnl_usd, 0)
        res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        (r,) = self._outcome_row(o.id)
        self.assertEqual(r["direction"], "short")
        self.assertAlmostEqual(r["pnl_usd"], o.net_pnl_usd, places=4)
        self.assertAlmostEqual(r["gross_pnl_usd"], o.gross_pnl_usd, places=4)


class PartialDoesNotLearnTests(_B2CHarness):
    """§48 — one position, one final learning event."""

    def test_a_partial_produces_nothing_to_apply(self):
        from lib import canonical_entry as CE
        from app.database import PaperRealizedOutcome, get_db
        with _spot_feed():
            res = CE.open_canonical_position(_signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        _seed_book(bid=64_700.0, ask=64_800.0)
        p = CX.close_canonical_position(res["position"]["id"],
                                        requested_qty=2.0,
                                        exit_reason=CS.SCALE_OUT)
        self.assertTrue(p.get("ok"), p)
        with get_db() as db:
            n = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.position_id == res["position"]["id"]
            ).count()
        self.assertEqual(n, 0, "a partial exit created a learning source")


class IdempotencyTests(_B2CHarness):

    def test_apply_twice_teaches_once(self):
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        first = CL.apply_realized_outcome(o.id)
        self.assertTrue(first.get("ok"), first)

        with engine.connect() as conn:
            acc_before = conn.execute(text(
                "SELECT total_trades FROM signal_accuracy "
                "WHERE symbol='BTC/USD'")).fetchone()[0]

        second = CL.apply_realized_outcome(o.id)
        self.assertTrue(second.get("ok"))
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(second["result"], CL.LEARNING_ALREADY_APPLIED)

        self.assertEqual(len(self._outcome_row(o.id)), 1)
        with engine.connect() as conn:
            acc_after = conn.execute(text(
                "SELECT total_trades FROM signal_accuracy "
                "WHERE symbol='BTC/USD'")).fetchone()[0]
        self.assertEqual(acc_before, acc_after,
                         "a retry moved an aggregate")

    def test_a_concurrent_rival_is_detected_and_verified(self):
        """§50 — the rival commits between the §29 pre-check and the
        transaction; the unique index catches it; the loser VERIFIES rather
        than assumes."""
        o = self._settled_outcome()
        state = {"raced": False}
        real_meta = CL._entry_metadata

        def racing_meta(conn, signal_id, position_id):
            if not state["raced"]:
                state["raced"] = True
                rival = CL.apply_realized_outcome(o.id)
                assert rival.get("ok"), rival
            return real_meta(conn, signal_id, position_id)

        with patch.object(CL, "_entry_metadata", racing_meta):
            res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("idempotent"))
        self.assertEqual(len(self._outcome_row(o.id)), 1)
        self.assertEqual(self._fresh(o.id).learning_state, "APPLIED")

    def test_applied_with_no_projection_is_corrupt_not_idempotent(self):
        """§28."""
        o = self._settled_outcome()
        self._corrupt_outcome(o.id, learning_state="APPLIED",
                              trade_outcome_id=None)
        res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CL.LEARNING_STATE_CORRUPT)
        self.assertEqual(self._outcome_row(o.id), [])

    def test_pending_with_an_agreeing_row_heals_without_reapplying(self):
        """§29 — recovery: verify in full, heal the link, touch no
        aggregate."""
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        self.assertTrue(CL.apply_realized_outcome(o.id).get("ok"))
        # Tamper the STATE back (the row and aggregates remain).
        self._corrupt_outcome(o.id, learning_state="PENDING",
                              trade_outcome_id=None)
        with engine.connect() as conn:
            acc_before = conn.execute(text(
                "SELECT total_trades FROM signal_accuracy "
                "WHERE symbol='BTC/USD'")).fetchone()[0]
        res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("healed"))
        with engine.connect() as conn:
            acc_after = conn.execute(text(
                "SELECT total_trades FROM signal_accuracy "
                "WHERE symbol='BTC/USD'")).fetchone()[0]
        self.assertEqual(acc_before, acc_after)
        self.assertEqual(self._fresh(o.id).learning_state, "APPLIED")

    def test_pending_with_a_disagreeing_row_is_ambiguous(self):
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        self.assertTrue(CL.apply_realized_outcome(o.id).get("ok"))
        self._corrupt_outcome(o.id, learning_state="PENDING",
                              trade_outcome_id=None)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE trade_outcomes SET pnl_usd = pnl_usd + 100 "
                "WHERE canonical_outcome_id=:c"), {"c": o.id})
        res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CL.LEARNING_RECOVERY_AMBIGUOUS)


class TruthPreservationTests(_B2CHarness):
    """§51-§55 — the drifts the old recorder would have accepted."""

    def test_engine_epoch_is_the_persisted_one_not_todays(self):
        """§53 — must fail against record_trade_outcome, which stamps
        _CURRENT_EPOCH() at projection time."""
        from lib import learning_engine as LE
        o = self._settled_outcome()
        with patch.object(LE, "_CURRENT_EPOCH", lambda: "EPOCH_B_TODAY"):
            res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        (r,) = self._outcome_row(o.id)
        self.assertEqual(r["engine_epoch"], o.engine_epoch)
        self.assertNotEqual(r["engine_epoch"], "EPOCH_B_TODAY")

    def test_trade_time_is_the_persisted_one_not_projection_time(self):
        """§54 — project 'days later'; the row keeps T0/T1/60."""
        o = self._settled_outcome()
        t0 = "2026-08-10T10:00:00+00:00"
        t1 = "2026-08-10T11:00:00+00:00"
        self._corrupt_outcome(o.id, opened_at=t0, closed_at=t1,
                              hold_minutes=60.0)
        res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        (r,) = self._outcome_row(o.id)
        self.assertEqual(r["entered_at"], t0)
        self.assertEqual(r["exited_at"], t1)
        self.assertEqual(r["hold_duration_m"], 60.0)
        self.assertNotEqual(r["exited_at"][:10],
                            datetime.now(timezone.utc).date().isoformat(),
                            "projection time leaked into the exit time")

    def test_the_return_basis_travels_and_r_never_masquerades(self):
        """§55."""
        o = self._settled_outcome()
        res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        (r,) = self._outcome_row(o.id)
        self.assertEqual(r["return_pct_basis"], "MARGIN")
        self.assertAlmostEqual(r["pnl_pct"], o.net_return_pct, places=4)
        if o.net_r is not None:
            self.assertNotAlmostEqual(r["pnl_pct"], o.net_r * 100.0,
                                      places=2)

    def test_a_missing_return_basis_refuses_permanently(self):
        """§51."""
        o = self._settled_outcome()
        self._corrupt_outcome(o.id, return_pct_basis=None)
        res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CL.LEARNING_OUTCOME_INVALID)
        self.assertEqual(self._outcome_row(o.id), [])
        self.assertEqual(self._fresh(o.id).learning_state,
                         "FAILED_PERMANENT")

    def test_a_notional_basis_on_a_margin_book_refuses(self):
        o = self._settled_outcome()
        self._corrupt_outcome(o.id, return_pct_basis="NOTIONAL")
        res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(self._outcome_row(o.id), [])

    def test_a_coins_unit_against_a_contracts_header_refuses(self):
        """§52 — no laundering an unlabelled quantity into learning."""
        o = self._settled_outcome()
        self._corrupt_outcome(o.id, quantity_unit="COINS", multiplier=1.0)
        res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CL.LEARNING_OUTCOME_INVALID)
        self.assertEqual(self._outcome_row(o.id), [])
        self.assertEqual(self._fresh(o.id).learning_state,
                         "FAILED_PERMANENT")


class NoHindsightTests(_B2CHarness):
    """§56/§65 — the current world is poisoned; persisted truth suffices."""

    def test_projection_survives_a_poisoned_present(self):
        from lib import execution_snapshot as ES
        from lib import fee_authority as FA
        from lib import holding_cost_authority as HC
        from lib import instruments as INST
        from lib import learning_engine as LE
        from lib import realized_outcome as RO

        o = self._settled_outcome()

        def explode(*a, **k):
            raise AssertionError("learning consulted the current world")
        with patch.object(INST, "resolve", explode), \
             patch.object(INST, "resolve_for_execution", explode), \
             patch.object(RO, "build", explode), \
             patch.object(RO, "build_from_settlement", explode), \
             patch.object(FA, "leg_fee", explode), \
             patch.object(HC, "holding_cost", explode), \
             patch.object(ES, "execution_market_snapshot", explode), \
             patch.object(LE, "record_trade_outcome", explode), \
             patch.object(LE, "_run_reasoning_audit", explode):
            res = CL.apply_realized_outcome(o.id)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(self._outcome_row(o.id)), 1)

    def test_the_control_the_poison_is_live(self):
        from lib import instruments as INST

        def explode(*a, **k):
            raise AssertionError("resolve poison reached")
        with patch.object(INST, "resolve", explode):
            with self.assertRaises(AssertionError):
                INST.resolve("BTC/USD")

    def test_no_persisted_entry_ta_means_no_pattern_update(self):
        """§25/§56 — pattern memory is skipped, never fed exit-time TA."""
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        with engine.connect() as conn:
            before = conn.execute(text(
                "SELECT COUNT(*) FROM pattern_memory")).fetchone()[0]
        self.assertTrue(CL.apply_realized_outcome(o.id).get("ok"))
        with engine.connect() as conn:
            after = conn.execute(text(
                "SELECT COUNT(*) FROM pattern_memory")).fetchone()[0]
        self.assertEqual(before, after,
                         "pattern memory grew without persisted entry TA")

    def test_persisted_entry_ta_updates_pattern_memory_exactly_once(self):
        """The capability, exercised via injected persisted metadata."""
        from sqlalchemy import text
        from app.database import engine
        o = self._settled_outcome()
        fake_profile = {"4H": {"rsi": 25, "bias": "bullish",
                               "macd": {"trend": "bullish"}}}

        def with_ta(conn, signal_id, position_id):
            return {"timeframe": "4H", "confidence": 70.0, "score": 80.0,
                    "reasoning": "persisted", "ta_profile": fake_profile,
                    "ta_summary": "persisted summary",
                    "market_regime": "Risk-On Bull"}
        with patch.object(CL, "_entry_metadata", with_ta):
            first = CL.apply_realized_outcome(o.id)
            self.assertTrue(first.get("ok"), first)
            second = CL.apply_realized_outcome(o.id)
            self.assertTrue(second.get("idempotent"))
        with engine.connect() as conn:
            pm = conn.execute(text(
                "SELECT total FROM pattern_memory")).fetchall()
            rp = conn.execute(text(
                "SELECT total FROM regime_performance "
                "WHERE regime='Risk-On Bull'")).fetchone()
        self.assertEqual(sum(r[0] for r in pm), 1,
                         "pattern memory voted more than once")
        self.assertEqual(rp[0], 1, "regime performance voted more than once")


class FinancialImmutabilityTests(_B2CHarness):
    """§36/§57 — learning cannot move money, full stop."""

    def test_the_financial_hash_is_identical_across_apply(self):
        o = self._settled_outcome()
        before = self._financial_hash()
        self.assertTrue(CL.apply_realized_outcome(o.id).get("ok"))
        after = self._financial_hash()
        self.assertEqual(before, after,
                         "a learning projection changed financial state")

    def test_a_failed_projection_damages_nothing_and_is_retryable(self):
        """§58."""
        from lib import learning_engine as LE
        o = self._settled_outcome()
        before = self._financial_hash()

        with patch.object(LE, "_refresh_signal_accuracy_conn",
                          side_effect=RuntimeError("injected")):
            res = CL.apply_realized_outcome(o.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CL.LEARNING_PROJECTION_FAILED)
        self.assertEqual(self._financial_hash(), before)
        self.assertEqual(self._outcome_row(o.id), [],
                         "a rolled-back projection left its row")
        o2 = self._fresh(o.id)
        self.assertEqual(o2.learning_state, "FAILED_RETRYABLE")
        self.assertIn("injected", o2.learning_error)

        # The retry succeeds once the fault clears.
        res2 = CL.apply_realized_outcome(o.id)
        self.assertTrue(res2.get("ok"), res2)
        self.assertEqual(self._fresh(o.id).learning_state, "APPLIED")

    def test_failed_permanent_is_not_auto_retried(self):
        """§32/§59."""
        o = self._settled_outcome()
        self._corrupt_outcome(o.id, return_pct_basis=None)
        first = CL.apply_realized_outcome(o.id)
        self.assertFalse(first.get("ok"))
        # Even after repairing the field, a FAILED_PERMANENT stays parked
        # until an operator intervenes.
        self._corrupt_outcome(o.id, return_pct_basis="MARGIN")
        second = CL.apply_realized_outcome(o.id)
        self.assertFalse(second.get("ok"))
        self.assertEqual(second["error"], CL.LEARNING_FAILED_PERMANENT)


class BatchHelperTests(_B2CHarness):
    """§40/§41 — bounded, oldest-first, no durable claim."""

    def test_the_batch_applies_pending_and_reports(self):
        o = self._settled_outcome()
        # The shared test book accumulates PENDING outcomes from every B2B
        # final close that ran before this test; the sweep bound must cover
        # them all or the oldest-first ordering never reaches ours.
        summary = CL.apply_pending_realized_outcomes(limit=500)
        self.assertGreaterEqual(summary["scanned"], 1)
        self.assertGreaterEqual(summary["applied"], 1)
        self.assertEqual(self._fresh(o.id).learning_state, "APPLIED")
        # A second sweep finds nothing new to do.
        again = CL.apply_pending_realized_outcomes(limit=500)
        self.assertEqual(again["applied"], 0)


class StructuralGuardTests(unittest.TestCase):
    """§64 — secondary to the poison proof."""

    def _calls(self):
        import ast
        import pathlib
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "canonical_learning.py").read_text(
            encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
        return names

    def test_learning_projects_and_never_recomputes(self):
        calls = self._calls()
        for forbidden in ("build", "build_from_settlement", "resolve",
                          "resolve_for_execution", "leg_fee", "holding_cost",
                          "execution_market_snapshot", "record_trade_outcome",
                          "_run_reasoning_audit", "settle_prepared_exit"):
            self.assertNotIn(forbidden, calls,
                             f"canonical_learning calls {forbidden}")


if __name__ == "__main__":
    unittest.main()
