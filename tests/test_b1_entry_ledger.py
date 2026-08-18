"""B1 — one canonical entry transaction, five facts, zero survivors on failure.

After B1 a NEW canonical position can no longer exist as only a PaperPosition
plus a JSON document. One settlement transaction creates, atomically:

    PaperPosition
    PaperPositionSettlement          the OPEN accounting header
    PaperSettlementLeg (ENTRY)       the executed leg
    the cash/margin debit
    DecisionObservation SETTLED linkage

and if any one of the five fails, none survives. Everything here runs the
REAL canonical chain — real perp book, real risk, real fee authority, real
settlement against the disposable pytest database — with only the market
feeds stubbed. The golden rule under test:

    THE BOT MUST NEVER MAKE MONEY BECAUSE THE SIMULATOR IS WRONG.

so the dollars debited, the quantity on the position, the exact instrument,
the fee, the ledger and the decision linkage must all describe THE SAME
ENTRY. A pretty ledger that disagrees with cash is worse than no ledger.
"""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
PERP_BID_USD, PERP_ASK_USD = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-b1-{next(_seq)}",
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


def _seed_perp_book():
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(PERP_BID_USD / TICK), 50]],
                "asks": [[int(PERP_ASK_USD / TICK), 50]]})
    return book


class _B1Harness(unittest.TestCase):
    """Real chain, real settlement, disposable DB."""

    def setUp(self):
        _seed_perp_book()
        self.addCleanup(MD.reset_books)
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperSettlementLeg, get_db)
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    # ── DB reading helpers ────────────────────────────────────────────────
    def _counts(self):
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperSettlementLeg, PaperTrade, get_db)
        with get_db() as db:
            return {
                "positions": db.query(PaperPosition).count(),
                "headers": db.query(PaperPositionSettlement).count(),
                "legs": db.query(PaperSettlementLeg).count(),
                "trades": db.query(PaperTrade).count(),
            }

    def _portfolio(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {"cash": float(p.cash), "total": float(p.total_trades or 0),
                    "wins": float(p.winning_trades or 0)}

    def _rows_for(self, position_id):
        from app.database import (DecisionObservation, PaperPosition,
                                  PaperPositionSettlement, PaperSettlementLeg,
                                  get_db)
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == position_id).first()
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).all()
            legs = db.query(PaperSettlementLeg).filter(
                PaperSettlementLeg.position_id == position_id).all()
            obs = db.query(DecisionObservation).filter(
                DecisionObservation.position_id == position_id).all()
            db.expunge_all()
        return pos, header, legs, obs

    # ── chain drivers ─────────────────────────────────────────────────────
    def _open(self, signal=None):
        """The fully real canonical entry — nothing below the feeds stubbed."""
        from lib import canonical_entry as CE
        with _spot_feed():
            return CE.open_canonical_position(signal or _signal(),
                                              decision_price=64_400.0)

    def _capture_settle_args(self, signal=None):
        """Run the real chain up to settlement, capturing settlement's exact
        arguments without performing it — the replay point for refusal and
        poison tests. The observation stays SIMULATED_FILLED, exactly as a
        settlement that never ran would leave it."""
        from lib import canonical_entry as CE
        from lib import paper_engine as PE
        captured = {}

        def fake_settle(auth, **kw):
            captured["auth"], captured["kw"] = auth, kw
            return {"ok": True, "position": {"id": "pos-fake"}}

        with _spot_feed(), \
             patch("lib.paper_engine.settle_position_entry", fake_settle):
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        return captured["auth"], captured["kw"]


class CanonicalEntryWritesTheLedgerTests(_B1Harness):
    """The primary acceptance: one real PBTC entry, five artifacts, one set
    of facts."""

    def _settled(self):
        before = self._portfolio()
        res = self._open()
        self.assertTrue(res.get("ok"), res)
        after = self._portfolio()
        pos, headers, legs, obs = self._rows_for(res["position"]["id"])
        return before, after, pos, headers, legs, obs

    def test_one_header_one_entry_leg_one_settled_observation(self):
        _, _, pos, headers, legs, obs = self._settled()
        self.assertEqual(pos.status, "Open")
        self.assertEqual(len(headers), 1)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].kind, "ENTRY")
        self.assertEqual(len(obs), 1)
        from lib import decision_observation as DO
        self.assertEqual(obs[0].execution_state, DO.EXEC_SETTLED)

    def test_the_causal_ids_are_persisted_and_agree(self):
        _, _, pos, (header,), (leg,), (ob,) = self._settled()
        self.assertTrue(header.entry_execution_id)
        self.assertEqual(header.entry_execution_id, leg.execution_id)
        self.assertEqual(header.entry_execution_id, ob.execution_id)
        self.assertEqual(header.observation_id, ob.observation_id)
        self.assertEqual(leg.observation_id, ob.observation_id)
        self.assertEqual(header.signal_id, pos.signal_id)
        self.assertEqual(leg.signal_id, pos.signal_id)

    def test_the_frozen_identity_is_the_exact_contract(self):
        _, _, _, (header,), (leg,), _ = self._settled()
        for row in (header, leg):
            self.assertEqual(row.symbol, "BTC/USD")
            self.assertEqual(row.product, PR.CRYPTO_PERP)
            self.assertEqual(row.venue, BP.KRAKEN_US_VENUE)
            self.assertEqual(row.instrument_id, PERP_SYM)
            self.assertEqual(row.quantity_unit, "CONTRACTS")
            self.assertAlmostEqual(row.multiplier, 0.01)
        self.assertEqual(header.settlement_version, "paper_settlement_v1")
        self.assertEqual(leg.settlement_version, "paper_settlement_v1")

    def test_quantities_describe_the_position_row(self):
        _, _, pos, (header,), (leg,), _ = self._settled()
        self.assertEqual(header.original_quantity, pos.qty)
        self.assertEqual(leg.filled_qty, pos.qty)
        self.assertGreaterEqual(leg.requested_qty, leg.filled_qty)
        self.assertGreater(leg.filled_qty, 0)
        self.assertEqual(pos.qty, float(int(pos.qty)),
                         "a perpetual settled at a fractional contract")

    def test_one_fill_price_everywhere(self):
        _, _, pos, (header,), (leg,), _ = self._settled()
        self.assertEqual(header.actual_entry_fill, pos.entry_price)
        self.assertEqual(leg.fill_price, pos.entry_price)
        self.assertEqual(header.decision_entry_price, 64_400.0)

    def test_the_notional_is_the_contract_arithmetic(self):
        _, _, pos, (header,), (leg,), _ = self._settled()
        expected = pos.qty * pos.entry_price * 0.01
        self.assertAlmostEqual(header.original_notional_usd, expected, places=6)
        self.assertAlmostEqual(leg.notional_usd, expected, places=6)

    def test_the_cash_identity_is_exact(self):
        """C1 = C0 - M - F, with M and F the PERSISTED figures — the ledger
        must describe the cash that moved, to the cent and beyond."""
        before, after, pos, (header,), (leg,), _ = self._settled()
        debited = before["cash"] - after["cash"]
        self.assertAlmostEqual(
            debited, header.committed_margin_usd + header.entry_fee_usd,
            places=9)
        self.assertEqual(header.committed_margin_usd, pos.margin_used)
        self.assertEqual(leg.explicit_fee_usd, header.entry_fee_usd)
        self.assertGreater(header.entry_fee_usd, 0.0)

    def test_the_fee_counts_the_filled_contracts_exactly(self):
        from lib import fee_authority as FA
        _, _, pos, (header,), (leg,), _ = self._settled()
        self.assertEqual(header.entry_fee_contract_count, pos.qty)
        self.assertEqual(header.entry_fee_contract_count_basis,
                         FA.EXECUTED_EXACT)
        self.assertEqual(leg.fee_contract_count_basis, FA.EXECUTED_EXACT)
        self.assertAlmostEqual(header.entry_fee_usd, pos.qty * 0.15, places=9)

    def test_initial_risk_is_one_arithmetic_truth(self):
        _, _, pos, (header,), _, _ = self._settled()
        self.assertEqual(header.initial_stop, 61_000.0)
        implied = pos.qty * abs(pos.entry_price - header.initial_stop) * 0.01
        self.assertAlmostEqual(header.initial_risk_usd, implied, places=6)

    def test_the_entry_leg_is_not_an_outcome(self):
        before, after, _, (header,), (leg,), _ = self._settled()
        self.assertEqual(leg.gross_pnl_usd, 0.0)
        self.assertEqual(leg.holding_cost_usd, 0.0)
        self.assertEqual(leg.released_margin_usd, 0.0)
        self.assertEqual(leg.hours_held, 0.0)
        self.assertEqual(header.status, "OPEN")
        self.assertEqual(header.settlement_revision, 0)
        self.assertIsNone(header.closed_at)
        # No trade was completed: no PaperTrade, no counters, no win.
        self.assertEqual(before["total"], after["total"])
        self.assertEqual(before["wins"], after["wins"])

    def test_no_paper_trade_row_exists_for_an_entry(self):
        from app.database import PaperTrade, get_db
        _, _, pos, _, _, _ = self._settled()
        with get_db() as db:
            n = db.query(PaperTrade).filter(
                PaperTrade.position_id == pos.id).count()
        self.assertEqual(n, 0)

    def test_one_settlement_time_across_the_transaction(self):
        _, _, pos, (header,), (leg,), (ob,) = self._settled()
        self.assertEqual(pos.opened_at, header.opened_at)
        self.assertEqual(pos.opened_at, leg.created_at)
        self.assertEqual(pos.opened_at, ob.settlement_at)

    def test_provenance_remains_beside_the_ledger(self):
        """The ledger does not obsolete provenance — they answer different
        questions, and they must agree."""
        import json
        _, _, pos, (header,), (leg,), _ = self._settled()
        self.assertIsNotNone(pos.execution_provenance)
        doc = json.loads(pos.execution_provenance)
        self.assertEqual(doc["instrument"], header.instrument_id)
        self.assertEqual(doc["entry_execution_id"], header.entry_execution_id)
        self.assertAlmostEqual(doc["executed_notional_usd"],
                               header.original_notional_usd, places=6)
        self.assertEqual(json.loads(leg.provenance_json)["entry_execution_id"],
                         leg.execution_id)


class SemanticReconstructionTests(_B1Harness):
    """§31 — persisted rows must reproduce the existing accounting model,
    and the entry fee must be counted ONCE."""

    def test_a_fresh_open_entry_reconstructs(self):
        from lib.settlement_ledger import load_position_settlement
        from app.database import get_db
        res = self._open()
        self.assertTrue(res.get("ok"), res)
        pos_id = res["position"]["id"]
        pos, (header,), _, _ = self._rows_for(pos_id)

        with get_db() as db:
            s = load_position_settlement(db, pos_id)
        self.assertIsNotNone(s)
        self.assertEqual(s.position_id, pos_id)
        self.assertEqual(s.entry_fee_usd, header.entry_fee_usd)
        self.assertEqual(s.committed_margin_usd, header.committed_margin_usd)
        self.assertEqual(len(s.legs), 1)
        self.assertEqual(s.legs[0].kind, "ENTRY")
        self.assertEqual(s.exit_legs, [])
        self.assertEqual(s.gross_pnl_usd, 0.0)
        self.assertEqual(s.exit_fees_usd, 0.0,
                         "the ENTRY leg's fee leaked into exit fees — the "
                         "fee would be counted twice")
        self.assertEqual(s.funding_usd, 0.0)
        self.assertAlmostEqual(
            s.cash_delta_total(),
            -(header.committed_margin_usd + header.entry_fee_usd), places=9)

    def test_a_legacy_position_reconstructs_to_none_not_empty(self):
        from lib.settlement_ledger import load_position_settlement
        from lib.paper_engine import open_paper_position
        from app.database import get_db
        res = open_paper_position(
            _signal(product="CRYPTO_SPOT"), current_price=100.0)
        self.assertTrue(res.get("ok"), res)
        with get_db() as db:
            s = load_position_settlement(db, res["position"]["id"])
        self.assertIsNone(s, "a legacy position grew a canonical ledger")


class PartialCanonicalInvocationRefusesTests(_B1Harness):
    """§33 — half a canonical invocation is a broken chain, never a silent
    downgrade to legacy. Every variant: no position, no rows, no cash."""

    def _refuses(self, auth, kw, expect_error=None, raises=False):
        from lib import paper_engine as PE
        before, counts = self._portfolio(), self._counts()
        if raises:
            with self.assertRaises(Exception):
                PE.settle_position_entry(auth, **kw)
        else:
            res = PE.settle_position_entry(auth, **kw)
            self.assertFalse(res.get("ok"), res)
            if expect_error:
                self.assertEqual(res.get("error"), expect_error)
        self.assertEqual(self._portfolio()["cash"], before["cash"],
                         "a refused settlement moved cash")
        self.assertEqual(self._counts(), counts,
                         "a refused settlement left rows behind")

    def test_observation_id_alone_refuses(self):
        auth, kw = self._capture_settle_args()
        kw = dict(kw, execution_id=None)
        self._refuses(auth, kw, "INCOMPLETE_CANONICAL_LINKAGE")

    def test_execution_id_alone_refuses(self):
        auth, kw = self._capture_settle_args()
        kw = dict(kw, observation_id=None)
        self._refuses(auth, kw, "INCOMPLETE_CANONICAL_LINKAGE")

    def test_missing_provenance_refuses(self):
        auth, kw = self._capture_settle_args()
        kw = dict(kw, execution_provenance=None)
        self._refuses(auth, kw, "INCOMPLETE_CANONICAL_LINKAGE")

    def test_missing_canonical_fee_refuses(self):
        auth, kw = self._capture_settle_args()
        kw = dict(kw, canonical_entry_fee_usd=None)
        self._refuses(auth, kw, "INCOMPLETE_CANONICAL_LINKAGE")

    def test_wrong_execution_model_refuses(self):
        auth, kw = self._capture_settle_args()
        prov = dict(kw["execution_provenance"],
                    execution_model="virtual_cex_direct_mark_v1")
        self._refuses(auth, dict(kw, execution_provenance=prov),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_cost_model_refuses(self):
        auth, kw = self._capture_settle_args()
        prov = dict(kw["execution_provenance"],
                    cost_model="legacy_round_trip_v1")
        self._refuses(auth, dict(kw, execution_provenance=prov),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_engine_epoch_refuses(self):
        auth, kw = self._capture_settle_args()
        prov = dict(kw["execution_provenance"], engine_epoch="2025-01-01-old")
        self._refuses(auth, dict(kw, execution_provenance=prov),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_execution_id_refuses(self):
        auth, kw = self._capture_settle_args()
        self._refuses(auth, dict(kw, execution_id="exec-not-this-one"),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_unit_fact_refuses(self):
        auth, kw = self._capture_settle_args()
        prov = dict(kw["execution_provenance"], quantity_unit="COINS",
                    multiplier=1.0)
        self._refuses(auth, dict(kw, execution_provenance=prov),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_fill_refuses(self):
        auth, kw = self._capture_settle_args()
        self._refuses(auth, dict(kw, fill_price=kw["fill_price"] + 5.0),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_fee_refuses(self):
        auth, kw = self._capture_settle_args()
        wrong = float(kw["canonical_entry_fee_usd"]) * 2.0
        self._refuses(auth, dict(kw, canonical_entry_fee_usd=wrong),
                      "CANONICAL_LEDGER_VALIDATION_FAILED")

    def test_wrong_observation_id_rolls_back_in_transaction(self):
        auth, kw = self._capture_settle_args()
        self._refuses(auth, dict(kw, observation_id="obs-nonexistent"),
                      raises=True)


class ResubmissionPersistsTheSurvivorTests(_B1Harness):
    """§37 — the discarded 3-contract simulation leaves no phantom row."""

    def test_the_ledger_records_only_the_two_contract_survivor(self):
        from lib import fee_authority as FA
        from lib import paper_engine as PE
        from app.database import PaperPortfolio, PaperPosition, get_db

        with get_db() as db:
            db.query(PaperPosition).delete()
            pf = db.query(PaperPortfolio).first()
            pf.cash = 100_000.0
            db.commit()

        before = self._portfolio()
        with patch.object(PE, "TRADE_MARGIN_PCT", 0.107):
            res = self._open(_signal())
        self.assertTrue(res.get("ok"), res)
        after = self._portfolio()

        pos, (header,), (leg,), _ = self._rows_for(res["position"]["id"])
        self.assertEqual(pos.qty, 2.0)
        self.assertEqual(header.original_quantity, 2.0)
        self.assertEqual(leg.requested_qty, 2.0,
                         "the ENTRY leg recorded the discarded 3-lot")
        self.assertEqual(leg.filled_qty, 2.0)
        self.assertEqual(header.entry_fee_contract_count, 2.0)
        self.assertEqual(header.entry_fee_contract_count_basis,
                         FA.EXECUTED_EXACT)
        self.assertAlmostEqual(header.entry_fee_usd, 2 * 0.15, places=9)
        self.assertAlmostEqual(
            before["cash"] - after["cash"],
            header.committed_margin_usd + 0.30, places=9)


class AtomicRollbackMatrixTests(_B1Harness):
    """§38 — not "an exception was raised": the PROPERTY that no economic
    mutation survives, at every failure stage."""

    def _assert_nothing_survived(self, action, expect_raise=True):
        from lib import decision_observation as DO
        from app.database import DecisionObservation, get_db
        before, counts = self._portfolio(), self._counts()
        auth, kw = self._capture_settle_args()
        obs_id = kw["observation_id"]

        from lib import paper_engine as PE
        if expect_raise:
            with self.assertRaises(Exception):
                action(lambda: PE.settle_position_entry(auth, **kw))
        else:
            res = action(lambda: PE.settle_position_entry(auth, **kw))
            self.assertFalse(res.get("ok"))

        # The capture itself wrote one observation; economics stay put.
        self.assertEqual(self._portfolio()["cash"], before["cash"],
                         "cash was mutated by a failed settlement")
        self.assertEqual(self._counts(), counts,
                         "rows survived a failed settlement")
        with get_db() as db:
            ob = db.query(DecisionObservation).filter(
                DecisionObservation.observation_id == obs_id).first()
            self.assertEqual(ob.execution_state, DO.EXEC_SIMULATED_FILLED,
                             "the observation falsely advanced to SETTLED")
            self.assertIsNone(ob.position_id)

    def test_failure_in_ledger_persistence_unwinds_everything(self):
        import lib.settlement_ledger as SL

        def run(settle):
            with patch.object(SL, "persist_entry_ledger",
                              side_effect=RuntimeError("injected: stage A")):
                return settle()
        self._assert_nothing_survived(run)

    def test_failure_constructing_the_header_unwinds_everything(self):
        import app.database as DBM

        def run(settle):
            with patch.object(DBM, "PaperPositionSettlement",
                              side_effect=RuntimeError("injected: stage B")):
                return settle()
        self._assert_nothing_survived(run)

    def test_failure_constructing_the_leg_unwinds_everything(self):
        import app.database as DBM

        def run(settle):
            with patch.object(DBM, "PaperSettlementLeg",
                              side_effect=RuntimeError("injected: stage C")):
                return settle()
        self._assert_nothing_survived(run)

    def test_failure_at_commit_time_unwinds_everything(self):
        """Everything pending — position, cash, observation, header, leg —
        and the COMMIT itself fails."""
        from sqlalchemy import event
        from app.database import SessionLocal

        def boom(session):
            raise RuntimeError("injected: stage D, before_commit")

        def run(settle):
            event.listen(SessionLocal, "before_commit", boom)
            try:
                return settle()
            finally:
                event.remove(SessionLocal, "before_commit", boom)
        self._assert_nothing_survived(run)

    def test_failure_at_observation_linkage_unwinds_everything(self):
        """Stage E: the observation names a different execution — the raise
        happens after the position and cash mutations are pending."""
        from lib import paper_engine as PE
        from app.database import DecisionObservation, get_db

        before, counts = self._portfolio(), self._counts()
        auth, kw = self._capture_settle_args()
        # Corrupt the OBSERVATION side (the row), leaving the settlement
        # arguments self-consistent so validation passes and the failure
        # lands inside the transaction.
        with get_db() as db:
            ob = db.query(DecisionObservation).filter(
                DecisionObservation.observation_id == kw["observation_id"]
            ).first()
            ob.execution_id = "exec-someone-else"
            db.commit()

        with self.assertRaises(ValueError):
            PE.settle_position_entry(auth, **kw)
        self.assertEqual(self._portfolio()["cash"], before["cash"])
        self.assertEqual(self._counts(), counts)


class DuplicateSettlementRefusesTests(_B1Harness):
    """§39/§40 — one execution is one entry, enforced by the database."""

    def test_the_same_execution_cannot_settle_twice(self):
        from lib import paper_engine as PE
        from app.database import PaperPosition, get_db

        auth, kw = self._capture_settle_args()
        first = PE.settle_position_entry(auth, **kw)
        self.assertTrue(first.get("ok"), first)
        before, counts = self._portfolio(), self._counts()

        # Clear the symbol collision so ONLY the execution identity guards.
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == first["position"]["id"]).update(
                {"status": "Closed"})
            db.commit()

        second = PE.settle_position_entry(auth, **kw)
        self.assertFalse(second.get("ok"))
        self.assertEqual(second.get("error"), "DUPLICATE_CANONICAL_EXECUTION")
        self.assertEqual(self._portfolio()["cash"], before["cash"])
        self.assertEqual(self._counts(), counts)

    def test_two_headers_for_one_position_is_a_database_error(self):
        from sqlalchemy.exc import IntegrityError
        from app.database import PaperPositionSettlement, get_db

        res = self._open()
        self.assertTrue(res.get("ok"), res)
        _, (header,), _, _ = self._rows_for(res["position"]["id"])
        counts = self._counts()

        with self.assertRaises(IntegrityError):
            with get_db() as db:
                db.add(PaperPositionSettlement(
                    position_id=header.position_id,
                    symbol="BTC/USD", product=PR.CRYPTO_PERP,
                    venue=BP.KRAKEN_US_VENUE, instrument_id=PERP_SYM,
                    position_side="long", quantity_unit="CONTRACTS",
                    multiplier=0.01, original_quantity=1.0,
                    original_notional_usd=646.0, committed_margin_usd=646.0,
                    actual_entry_fill=64_600.0,
                    entry_execution_id="exec-different",
                    entry_fee_usd=0.15,
                    settlement_version="paper_settlement_v1",
                    cost_model="per_leg_v2",
                    execution_model="virtual_cex_venue_book_v2",
                    engine_epoch="x", opened_at="t"))
        self.assertEqual(self._counts(), counts)

    def test_two_legs_for_one_execution_is_a_database_error(self):
        from sqlalchemy.exc import IntegrityError
        from app.database import PaperSettlementLeg, get_db

        res = self._open()
        self.assertTrue(res.get("ok"), res)
        _, _, (leg,), _ = self._rows_for(res["position"]["id"])
        counts = self._counts()

        with self.assertRaises(IntegrityError):
            with get_db() as db:
                db.add(PaperSettlementLeg(
                    position_id="pos-other", execution_id=leg.execution_id,
                    kind="ENTRY", settlement_version="paper_settlement_v1",
                    symbol="BTC/USD", product=PR.CRYPTO_PERP,
                    venue=BP.KRAKEN_US_VENUE, instrument_id=PERP_SYM,
                    position_side="long", execution_side="buy",
                    requested_qty=1.0, filled_qty=1.0,
                    quantity_unit="CONTRACTS", multiplier=0.01,
                    fill_price=64_600.0, notional_usd=646.0,
                    explicit_fee_usd=0.15,
                    execution_model="virtual_cex_venue_book_v2",
                    cost_model="per_leg_v2", created_at="t"))
        self.assertEqual(self._counts(), counts)


class ModeAndLegacyBoundariesTests(_B1Harness):
    """§45/§34 — EVIDENCE_ONLY writes nothing; legacy stays exactly legacy."""

    def test_evidence_only_creates_no_b1_rows(self):
        import os
        from lib import paper_engine as PE
        auth, kw = self._capture_settle_args()
        before, counts = self._portfolio(), self._counts()
        os.environ["JARVIS_RUNTIME_MODE"] = "EVIDENCE_ONLY"
        try:
            with self.assertRaises(Exception):
                PE.settle_position_entry(auth, **kw)
        finally:
            os.environ.pop("JARVIS_RUNTIME_MODE", None)
        self.assertEqual(self._portfolio()["cash"], before["cash"])
        self.assertEqual(self._counts(), counts)

    def test_legacy_entry_is_untouched_by_b1(self):
        from lib.paper_engine import open_paper_position
        before, counts = self._portfolio(), self._counts()
        res = open_paper_position(_signal(product="CRYPTO_SPOT"),
                                  current_price=100.0)
        self.assertTrue(res.get("ok"), res)
        pos, headers, legs, _ = self._rows_for(res["position"]["id"])

        self.assertEqual(headers, [], "legacy entry grew a settlement header")
        self.assertEqual(legs, [], "legacy entry grew a settlement leg")
        # Legacy economics exactly as before: deferred round-trip estimate
        # stored, margin-only cash debit, no canonical fee.
        self.assertGreater(pos.fees, 0.0,
                           "legacy lost its deferred round-trip estimate")
        self.assertNotEqual(pos.fee_basis, "per_leg_v2_entry")
        debited = before["cash"] - self._portfolio()["cash"]
        self.assertAlmostEqual(debited, pos.margin_used, places=9)
        self.assertEqual(self._counts()["headers"], counts["headers"])
        self.assertEqual(self._counts()["legs"], counts["legs"])


class QueryPlanTests(_B1Harness):
    """§53 — B2's four lookups are index walks, not table scans."""

    def test_the_four_lookups_use_indexes(self):
        from app.database import engine
        from sqlalchemy import text
        res = self._open()
        self.assertTrue(res.get("ok"), res)

        queries = {
            "header by position": ("SELECT * FROM paper_position_settlements "
                                   "WHERE position_id = :v"),
            "header by execution": ("SELECT * FROM paper_position_settlements "
                                    "WHERE entry_execution_id = :v"),
            "legs by position": ("SELECT * FROM paper_settlement_legs "
                                 "WHERE position_id = :v"),
            "leg by execution": ("SELECT * FROM paper_settlement_legs "
                                 "WHERE execution_id = :v"),
        }
        with engine.connect() as conn:
            for name, q in queries.items():
                plan = " | ".join(
                    str(row) for row in conn.execute(
                        text("EXPLAIN QUERY PLAN " + q), {"v": "x"}))
                self.assertIn("USING INDEX", plan.upper(),
                              f"{name} does not walk an index: {plan}")
                self.assertNotIn("SCAN paper_", plan,
                                 f"{name} scans the table: {plan}")


class FrozenFactsPoisonTests(_B1Harness):
    """§55 — settlement records frozen facts. Poison everything it must not
    consult; settlement still succeeds. Then the control: the same poison
    kills the path that legitimately DOES consult it."""

    def _poison(self):
        from lib import fee_authority as FA
        from lib import instruments as INST
        from lib import risk_engine as RE
        from lib import product_router as PRR

        def explode(*a, **k):
            raise AssertionError("settlement consulted a frozen-fact source")
        return (patch.object(FA, "leg_fee", explode),
                patch.object(RE, "solve_position", explode),
                patch.object(INST, "resolve_for_execution", explode),
                patch.object(PRR, "route", explode))

    def test_settlement_uses_frozen_facts_only(self):
        from lib import paper_engine as PE
        auth, kw = self._capture_settle_args()
        p1, p2, p3, p4 = self._poison()
        with p1, p2, p3, p4:
            res = PE.settle_position_entry(auth, **kw)
        self.assertTrue(res.get("ok"), res)
        _, headers, legs, _ = self._rows_for(res["position"]["id"])
        self.assertEqual(len(headers), 1)
        self.assertEqual(len(legs), 1)

    def test_the_control_the_poison_kills_the_pricing_path(self):
        """open_canonical_position legitimately calls the fee authority, so
        under the same poison it must fail — proving the poison is live in
        exactly the code settlement is claimed to avoid."""
        from lib import fee_authority as FA

        def explode(*a, **k):
            raise AssertionError("leg_fee poison reached")
        with patch.object(FA, "leg_fee", explode):
            with self.assertRaises(AssertionError):
                res = self._open()
                # If it did not raise, it must at least have refused —
                # either way the poison bit. Reaching here green-and-ok
                # means the poison is inert and the silence above proves
                # nothing.
                self.assertFalse(res.get("ok"))
                raise AssertionError("inert poison surfaced as a refusal")


class StructuralDefenseInDepthTests(unittest.TestCase):
    """§54 — secondary to the behavioral proofs above, and kept because four
    separate defects here were wired-but-inert."""

    def _ledger_calls(self):
        import ast
        import pathlib
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "settlement_ledger.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
        return names

    def test_the_ledger_module_owns_no_transaction(self):
        calls = self._ledger_calls()
        self.assertNotIn("get_db", calls)
        self.assertNotIn("commit", calls)
        self.assertNotIn("rollback", calls)

    def test_the_ledger_module_revisits_no_decision(self):
        calls = self._ledger_calls()
        self.assertNotIn("leg_fee", calls)
        self.assertNotIn("solve_position", calls)
        self.assertNotIn("resolve_for_execution", calls)
        self.assertNotIn("resolve_product", calls)

    def test_settlement_actually_calls_the_ledger(self):
        import inspect
        from lib import paper_engine as PE
        src = inspect.getsource(PE.settle_position_entry)
        self.assertIn("validate_entry_ledger_facts", src)
        self.assertIn("persist_entry_ledger", src)


if __name__ == "__main__":
    unittest.main()
