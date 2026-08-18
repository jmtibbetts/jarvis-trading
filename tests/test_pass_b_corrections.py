"""Pass B correction gate — the four behavioural seams behind the wiring.

Routing was structurally complete and behaviourally incomplete in four ways,
each of which this file pins with the REAL production shapes rather than
idealized ones:

    A  the dispatcher queried the B1 settlement table unconditionally, so on
       the still-unmigrated operator schema — where that table does not
       exist — a LEGACY exit could fail before it was even classified

    B  real callers pass human prose ("<=-4% — cut loss", a catastrophic
       backstop sentence) that no reason table should be expected to parse,
       and some callers counted a close without reading the result

    C  canonical mark economics were unit-blind: 26 PBTC CONTRACTS at a 0.01
       multiplier were priced as 26 BTC, inflating dollar P&L by 100x. This
       was NOT display-only — jobs/paper_trading feeds that number to the
       catastrophic backstop, so an exact fill could still be produced by a
       wrong DECISION

    D  the legacy hard reset deletes positions/trades/portfolio and knows
       nothing about canonical ledgers, so it would shred half an economic
       record
"""
import itertools
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
ENTRY_BID, ENTRY_ASK = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _seed_book(bid=ENTRY_BID, ask=ENTRY_ASK):
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid / TICK), 50]],
                "asks": [[int(ask / TICK), 50]]})
    return book


def _spot_feed():
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": SPOT_BID, "ask": SPOT_ASK,
                                     "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-corr-{next(_seq)}",
            "product": PR.CRYPTO_PERP}
    base.update(over)
    return base


# ── A ────────────────────────────────────────────────────────────────────
# The operator database TODAY has no canonical ledger tables. A dispatcher
# that cannot exit a legacy trade there is not a dispatcher the operator
# book can adopt.

PRE_B1_SCHEMA = """
CREATE TABLE paper_positions (
    id TEXT PRIMARY KEY, user_id TEXT, symbol TEXT, asset_class TEXT,
    direction TEXT, side TEXT, leverage REAL, qty REAL, entry_price REAL,
    current_price REAL, target_price REAL, stop_loss REAL,
    initial_stop_loss REAL, notional REAL, margin_used REAL, fees REAL,
    fee_basis TEXT, execution_provenance TEXT, unrealized_pnl REAL,
    unrealized_pct REAL, signal_id TEXT, status TEXT, scaled_out INTEGER,
    scaled_out_qty REAL, opened_at TEXT, updated_at TEXT
);
CREATE TABLE paper_trades (
    id TEXT PRIMARY KEY, user_id TEXT, position_id TEXT, symbol TEXT,
    asset_class TEXT, direction TEXT, side TEXT, leverage REAL, qty REAL,
    entry_price REAL, exit_price REAL, notional REAL, gross_pnl REAL,
    fees REAL, fee_basis TEXT, realized_pnl REAL, pnl_pct REAL,
    close_reason TEXT, signal_id TEXT, opened_at TEXT, closed_at TEXT
);
CREATE TABLE paper_portfolio (
    id TEXT PRIMARY KEY, user_id TEXT, cash REAL, total_trades REAL,
    winning_trades REAL, realized_pnl REAL, updated_at TEXT, reset_at TEXT
);
CREATE TABLE trading_signals (id TEXT PRIMARY KEY, timeframe TEXT,
    confidence REAL, composite_score REAL, reasoning TEXT);
"""


class _PreB1Schema:
    """A disposable database in the REAL shape the operator book has now:
    paper tables, and no canonical ledger anywhere."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="jarvis-pre-b1-")
        self.path = Path(self._dir.name) / "legacy.db"
        raw = sqlite3.connect(self.path)
        raw.executescript(PRE_B1_SCHEMA)
        raw.execute(
            "INSERT INTO paper_portfolio (id, user_id, cash, total_trades, "
            "winning_trades, realized_pnl) VALUES "
            "('pf-legacy', 'local', 100000.0, 0, 0, 0.0)")
        raw.execute(
            "INSERT INTO paper_positions (id, user_id, symbol, asset_class, "
            "direction, side, leverage, qty, entry_price, current_price, "
            "target_price, stop_loss, initial_stop_loss, notional, "
            "margin_used, fees, status, scaled_out, opened_at, updated_at) "
            "VALUES ('pos-legacy', 'local', 'AAPL', 'Equity', 'Long', "
            "'long', 1.0, 10.0, 100.0, 100.0, 115.0, 95.0, 95.0, 1000.0, "
            "1000.0, 2.0, 'Open', 0, '2026-08-01T00:00:00+00:00', "
            "'2026-08-01T00:00:00+00:00')")
        raw.commit()
        raw.close()

        self.engine = create_engine(f"sqlite:///{self.path}")
        Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = Session()

        @contextmanager
        def _get_db():
            try:
                yield self.session
                self.session.commit()
            except Exception:
                self.session.rollback()
                raise

        self._patches = []
        import app.database as dbmod
        from lib import paper_engine
        for target in (dbmod, paper_engine):
            p = patch.object(target, "get_db", _get_db)
            p.start()
            self._patches.append(p)
        p = patch.object(dbmod, "engine", self.engine)
        p.start()
        self._patches.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self.session.close()
        self.engine.dispose()
        self._dir.cleanup()
        return False

    def tables(self):
        raw = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            raw.close()


class PreMigrationLegacyRoutingTests(unittest.TestCase):
    """A — the dispatcher must work against the operator schema as it is
    TODAY, before any canonical migration."""

    def test_the_fixture_really_has_no_canonical_tables(self):
        with _PreB1Schema() as db:
            tables = db.tables()
            self.assertIn("paper_positions", tables)
            for canonical in ("paper_position_settlements",
                              "paper_settlement_legs",
                              "paper_realized_outcomes"):
                self.assertNotIn(canonical, tables)

    def test_a_legacy_full_exit_works_with_no_ledger_table(self):
        from lib import exit_dispatch as ED
        with _PreB1Schema() as db:
            res = ED.request_position_exit(
                "pos-legacy", caller_price=110.0, caller_reason="manual",
                caller_source="API_MANUAL")
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res["route"], ED.LEGACY)
            # Legacy economics: the caller's price IS the fill.
            self.assertAlmostEqual(res["close_price"], 110.0)
            row = db.session.execute(text(
                "SELECT status FROM paper_positions WHERE id='pos-legacy'"
            )).fetchone()
            self.assertEqual(row[0], "Closed")
            # And nothing created a canonical table on the way through.
            self.assertNotIn("paper_position_settlements", db.tables())

    def test_a_legacy_partial_exit_works_with_no_ledger_table(self):
        from lib import exit_dispatch as ED
        with _PreB1Schema() as db:
            res = ED.request_position_partial_exit(
                "pos-legacy", fraction=0.5, caller_price=110.0,
                caller_reason="scale_out_tp1", caller_source="PAPER_TP1")
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res["route"], ED.LEGACY)
            self.assertNotIn("paper_settlement_legs", db.tables())

    def test_a_canonical_fill_without_a_ledger_table_is_hybrid(self):
        """A venue-book fill on a schema that cannot hold its ledger is not
        legacy — it is unsettleable, and it fails closed."""
        import json
        from lib import exit_dispatch as ED
        from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                          EXECUTION_MODEL_CANONICAL)
        from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
        with _PreB1Schema() as db:
            doc = {"execution_model": EXECUTION_MODEL_CANONICAL,
                   "cost_model": COST_MODEL_CANONICAL,
                   "engine_epoch": CANONICAL_ENGINE_EPOCH,
                   "entry_execution_id": "exec-x"}
            db.session.execute(text(
                "UPDATE paper_positions SET execution_provenance=:p "
                "WHERE id='pos-legacy'"), {"p": json.dumps(doc)})
            db.session.commit()

            res = ED.request_position_exit(
                "pos-legacy", caller_price=110.0, caller_reason="manual",
                caller_source="API_MANUAL")
            self.assertFalse(res.get("ok"), res)
            self.assertEqual(res["route"], ED.HYBRID)
            row = db.session.execute(text(
                "SELECT status FROM paper_positions WHERE id='pos-legacy'"
            )).fetchone()
            self.assertEqual(row[0], "Open")

    def test_a_broken_database_is_not_read_as_legacy(self):
        """A3 — table ABSENT and database BROKEN are different facts. A
        connection failure must not be laundered into 'this is legacy'."""
        from lib import exit_dispatch as ED
        with self.assertRaises(Exception):
            with _PreB1Schema() as db:
                with patch.object(ED, "_canonical_ledger_available",
                                  side_effect=RuntimeError("db is down")):
                    ED.request_position_exit(
                        "pos-legacy", caller_price=110.0,
                        caller_reason="manual", caller_source="API_MANUAL")


class _CanonicalHarness(unittest.TestCase):
    """A real canonical PBTC position on the normal pytest database."""

    def setUp(self):
        _seed_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).delete()
            pf = db.query(PaperPortfolio).first()
            if pf is not None:
                pf.cash = 100_000.0
            db.commit()

        def _close_leftovers():
            with get_db() as db:
                db.query(PaperPosition).filter(
                    PaperPosition.status == "Open").update(
                    {"status": "Closed"})
                db.commit()
        self.addCleanup(_close_leftovers)

    def _portfolio(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {"cash": float(p.cash)}

    def _state(self, position_id):
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperRealizedOutcome, PaperSettlementLeg,
                                  get_db)
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == position_id).first()
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).first()
            legs = (db.query(PaperSettlementLeg)
                    .filter(PaperSettlementLeg.position_id == position_id)
                    .order_by(PaperSettlementLeg.settlement_revision).all())
            outcomes = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.position_id == position_id).all()
            db.expunge_all()
        return pos, header, legs, outcomes

    def _set_position(self, position_id, **fields):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == position_id).update(fields)
            db.commit()

    def _enter(self, signal=None):
        from lib import canonical_entry as CE
        with _spot_feed():
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        pos, header, *_ = self._state(res["position"]["id"])
        return pos, header


# ── B ────────────────────────────────────────────────────────────────────
# Real callers pass human prose. The STRUCTURED caller_source is what has
# canonical meaning; the reason text is provenance.

class RealCallerReasonsAreRoutableTests(unittest.TestCase):
    """B1/B2 — the exact strings production emits, not idealized tokens."""

    # Verbatim from jobs/paper_trading: _PAPER_TIERS labels, and a
    # catastrophic-backstop sentence built by _paper_exit_plan.
    REAL = [
        ("RISK_GUARD",
         "catastrophic backstop — lost $350.00 of $1,000.00 margin (35% cap)",
         "VOLUNTARY_EXIT"),
        ("TIER_EXIT", ">=10% — take profit", "VOLUNTARY_EXIT"),
        ("TIER_EXIT", "<=-4% — cut loss", "VOLUNTARY_EXIT"),
        ("TIER_EXIT", ">=15% — take profit", "VOLUNTARY_EXIT"),
        ("TIER_EXIT", "<=-5% — cut loss", "VOLUNTARY_EXIT"),
        ("AI_EXIT", "AI EXIT: thesis broke after support failed",
         "VOLUNTARY_EXIT"),
        ("PAPER_TP1", "scale_out_tp1", "SCALE_OUT"),
        ("SOFT_RESET", "reset", "ADMINISTRATIVE_RESET"),
        ("API_MANUAL", "api_manual", "VOLUNTARY_EXIT"),
        ("TELEGRAM_MANUAL", "telegram_manual", "VOLUNTARY_EXIT"),
        ("API_FLATTEN", "flatten", "VOLUNTARY_EXIT"),
        ("MARK_TO_MARKET", "stop_loss", "STOP_EXIT"),
        ("MARK_TO_MARKET", "take_profit", "TARGET_EXIT"),
        ("MARK_TO_MARKET", "margin_call", "MARGIN_CALL"),
    ]

    def test_every_real_production_pair_has_a_canonical_meaning(self):
        from lib import exit_dispatch as ED
        for source, reason, expected in self.REAL:
            self.assertEqual(
                ED.canonical_reason_for(reason, caller_source=source),
                expected,
                f"{source} / {reason!r} did not route")

    def test_prose_alone_without_a_known_source_still_refuses(self):
        """B3 — the fix is the structured SOURCE, not a looser parser. Free
        text with no recognised source is still unmappable."""
        from lib import exit_dispatch as ED
        self.assertIsNone(ED.canonical_reason_for("<=-4% — cut loss"))
        self.assertIsNone(
            ED.canonical_reason_for("<=-4% — cut loss",
                                    caller_source="SOMETHING_NEW"))

    def test_an_unknown_source_and_reason_fails_closed(self):
        from lib import exit_dispatch as ED
        self.assertIsNone(ED.canonical_reason_for("emergency_unwind",
                                                  caller_source="WAT"))

    def test_mark_to_market_still_needs_a_known_trigger_reason(self):
        """A price-threshold source must say WHICH threshold; it does not
        get a blanket VOLUNTARY_EXIT."""
        from lib import exit_dispatch as ED
        self.assertIsNone(
            ED.canonical_reason_for("something odd",
                                    caller_source="MARK_TO_MARKET"))

    def test_every_structured_production_source_is_enumerated(self):
        """F2 — the audited caller_source values all have explicit
        semantics; a new source must be added deliberately."""
        from lib import exit_dispatch as ED
        for source in ("MARK_TO_MARKET", "SOFT_RESET", "PAPER_TP1",
                       "RISK_GUARD", "TIER_EXIT", "AI_EXIT",
                       "TELEGRAM_MANUAL", "TELEGRAM_TAKE_PROFIT",
                       "API_MANUAL", "API_FLATTEN"):
            self.assertIn(source, ED.KNOWN_CALLER_SOURCES, source)


class RealCallerProseRoutesEndToEndTests(_CanonicalHarness):
    """B6 — proved through the REAL management seam, not through a unit
    test of the mapper. The wired-but-inert lesson applies here too."""

    def test_a_risk_guard_exit_with_prose_settles_canonically(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        prose = ("catastrophic backstop — lost $350.00 of $1,000.00 margin "
                 "(35% cap)")
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason=prose,
                                       caller_source="RISK_GUARD")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["reason"], "VOLUNTARY_EXIT")
        _, _, legs, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        import json
        prov = json.loads(leg.provenance_json)
        # The canonical vocabulary stays clean; the human sentence survives
        # verbatim beside it.
        self.assertEqual(prov["caller_reason"], prose)
        self.assertEqual(prov["caller_source"], "RISK_GUARD")

    def test_a_profit_tier_label_settles_canonically(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason=">=10% — take profit",
                                       caller_source="TIER_EXIT")
        self.assertTrue(res.get("ok"), res)
        self.assertNotEqual(res.get("error"), ED.UNKNOWN_EXIT_REASON)
        self.assertEqual(res["reason"], "VOLUNTARY_EXIT")

    def test_a_loss_tier_label_settles_canonically(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="<=-4% — cut loss",
                                       caller_source="TIER_EXIT")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["reason"], "VOLUNTARY_EXIT")

    def test_arbitrary_ai_reasoning_settles_canonically(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(
            pos.id, caller_price=64_700.0,
            caller_reason="AI EXIT: momentum died and the 4H bias flipped; "
                          "no reason to hold into the weekend",
            caller_source="AI_EXIT")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["reason"], "VOLUNTARY_EXIT")


class CallerSuccessMustMeanSuccessTests(_CanonicalHarness):
    """B4/B5/E5 — a refusal is never counted as a close."""

    def _manage_once(self, price):
        from jobs import paper_trading as PT
        return PT._manage_open_positions({"BTC/USD": price})

    def test_a_refused_risk_exit_is_not_counted_closed(self):
        from jobs import paper_trading as PT
        pos, header = self._enter()
        cash = self._portfolio()["cash"]
        _seed_book(bid=64_700.0, ask=64_800.0)
        # The risk guard demands an exit and the book is fine — but
        # settlement refuses (here: a concurrent revision won the race).
        # The caller must report that honestly.
        from lib import canonical_settlement as CS
        plan = {"ok": True, "action": "EXIT",
                "reason": "catastrophic backstop — lost $350.00 of "
                          "$1,000.00 margin (35% cap)"}
        refusal = {"ok": False, "error": CS.STALE_SETTLEMENT_REVISION,
                   "detail": "injected: another settlement won"}

        with patch.object(PT, "_paper_exit_plan", return_value=plan), \
             patch.object(PT, "_fetch_ta", return_value={}), \
             patch.object(CS, "settle_prepared_exit", return_value=refusal):
            res = self._manage_once(64_700.0)

        self.assertEqual(res.get("closed", 0), 0,
                         "a refused exit was counted as a close")
        self.assertGreaterEqual(res.get("refused", 0), 1,
                                f"the refusal was never reported: {res}")
        pos2, header2, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])
        self.assertEqual(self._portfolio()["cash"], cash)

    def test_an_unusable_exact_book_abstains_from_managing(self):
        """C6 — with no trustworthy price for its OWN product, a canonical
        position gets no management decision at all. Not a close, not a stop
        adjustment, and certainly not one computed from another market's
        print."""
        from jobs import paper_trading as PT
        pos, header = self._enter()
        stop_before = pos.stop_loss
        real_top = MD.latest_top

        def aged(sym):
            top = real_top(sym)
            return dict(top, age_s=3_600.0) if top else top

        with patch.object(PT, "_fetch_ta", return_value={}), \
             patch.object(MD, "latest_top", aged):
            res = self._manage_once(1_000.0)    # absurd cross-market print

        self.assertEqual(res.get("closed", 0), 0)
        self.assertGreaterEqual(res.get("mark_unavailable", 0), 1, res)
        pos2, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual(pos2.stop_loss, stop_before,
                         "a stop was moved from a substituted price")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])

    def test_the_control_an_executable_book_does_close_and_count(self):
        from jobs import paper_trading as PT
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        plan = {"ok": True, "action": "EXIT",
                "reason": "catastrophic backstop — lost $350.00 of "
                          "$1,000.00 margin (35% cap)"}
        with patch.object(PT, "_paper_exit_plan", return_value=plan), \
             patch.object(PT, "_fetch_ta", return_value={}):
            res = self._manage_once(64_700.0)

        self.assertEqual(res.get("closed", 0), 1, res)
        self.assertEqual(res.get("refused", 0), 0)
        pos2, _, _, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(len(outcomes), 1)


# ── C ────────────────────────────────────────────────────────────────────
# The 100x defect. NOT display-only: jobs/paper_trading fed this number to
# the catastrophic backstop, so a perfectly exact fill could still be the
# product of a wrong DECISION.

class CanonicalMarkEconomicsTests(unittest.TestCase):
    """C2/C14 — 26 CONTRACTS at 0.01 are 0.26 BTC of exposure, not 26."""

    def _basis(self, side="long", entry=64_000.0, qty=26.0, margin=1_000.0):
        from lib.paper_mark_economics import MarkBasis
        return MarkBasis(route="CANONICAL", position_side=side,
                         entry_fill=entry, qty=qty, multiplier=0.01,
                         quantity_unit="CONTRACTS", margin=margin,
                         leverage=13.86)

    def test_a_long_hundred_dollar_move_is_twenty_six_dollars(self):
        from lib.paper_mark_economics import gross_at_mark
        gross = gross_at_mark(self._basis(), 63_900.0)
        self.assertAlmostEqual(gross, -26.0, places=9)
        # The defect, named: the old arithmetic said -$2,600.
        self.assertNotAlmostEqual(gross, -2_600.0, places=2)

    def test_a_short_hundred_dollar_move_is_plus_twenty_six(self):
        from lib.paper_mark_economics import gross_at_mark
        gross = gross_at_mark(self._basis(side="short"), 63_900.0)
        self.assertAlmostEqual(gross, +26.0, places=9)

    def test_leverage_never_multiplies_price_pnl_again(self):
        from lib.paper_mark_economics import gross_at_mark
        a = self._basis()
        b = self._basis()
        object.__setattr__(b, "leverage", 1.0)
        self.assertEqual(gross_at_mark(a, 63_900.0),
                         gross_at_mark(b, 63_900.0))

    def test_the_price_distance_for_a_dollar_loss_uses_the_multiplier(self):
        """C9 — $350 across 26 contracts at 0.01 is $1,346.15 of price, not
        $13.46. A stop placed at the wrong distance is a different trade."""
        from lib.paper_mark_economics import price_distance_for_usd
        d = price_distance_for_usd(self._basis(), 350.0)
        self.assertAlmostEqual(d, 350.0 / (26.0 * 0.01), places=6)
        self.assertAlmostEqual(d, 1346.153846, places=4)
        self.assertNotAlmostEqual(d, 350.0 / 26.0, places=2)

    def test_a_legacy_basis_keeps_legacy_arithmetic(self):
        """C4 — the 667 historical positions are not retro-fitted with
        contract semantics."""
        from lib.paper_mark_economics import (MarkBasis, gross_at_mark,
                                              price_distance_for_usd)
        legacy = MarkBasis(route="LEGACY", position_side="long",
                           entry_fill=100.0, qty=10.0, multiplier=1.0,
                           quantity_unit=None, margin=1_000.0, leverage=1.0)
        self.assertAlmostEqual(gross_at_mark(legacy, 111.0), 110.0)
        self.assertAlmostEqual(price_distance_for_usd(legacy, 350.0), 35.0)

    def test_a_hybrid_basis_refuses_to_produce_economics(self):
        """C4 — no management decision from a half-known basis."""
        from lib.paper_mark_economics import MarkBasis, gross_at_mark
        hybrid = MarkBasis(route="HYBRID", position_side="long",
                           entry_fill=64_000.0, qty=26.0, multiplier=None,
                           quantity_unit=None, margin=1_000.0, leverage=1.0)
        self.assertIsNone(gross_at_mark(hybrid, 63_900.0))

    def test_notional_and_margin_use_the_multiplier(self):
        """C11 — qty * entry / leverage is wrong for contract units."""
        from lib.paper_mark_economics import notional_at
        b = self._basis()
        self.assertAlmostEqual(notional_at(b, 64_000.0),
                               26.0 * 64_000.0 * 0.01, places=6)


class CanonicalBasisComesFromTheLedgerTests(_CanonicalHarness):
    """C3/C7 — the basis is read from the frozen header, never inferred
    from the bare symbol."""

    def test_a_canonical_position_reports_its_contract_basis(self):
        from lib.paper_mark_economics import basis_for_position
        pos, header = self._enter()
        basis = basis_for_position(pos.id)
        self.assertEqual(basis.route, "CANONICAL")
        self.assertEqual(basis.quantity_unit, "CONTRACTS")
        self.assertAlmostEqual(basis.multiplier, 0.01)
        self.assertAlmostEqual(basis.entry_fill, header.actual_entry_fill)
        self.assertEqual(basis.qty, pos.qty)

    def test_the_basis_never_asks_the_bare_symbol(self):
        """C7 — instruments.resolve('BTC/USD') answers 1.0 COIN. Poison it
        and the basis must still be right."""
        from lib import instruments as INST
        from lib.paper_mark_economics import basis_for_position
        pos, header = self._enter()

        def explode(*a, **k):
            raise AssertionError("the mark basis consulted the bare symbol")
        with patch.object(INST, "resolve", explode):
            basis = basis_for_position(pos.id)
        self.assertAlmostEqual(basis.multiplier, 0.01)

    def test_the_control_the_poison_is_live(self):
        from lib import instruments as INST

        def explode(*a, **k):
            raise AssertionError("resolve poison reached")
        with patch.object(INST, "resolve", explode):
            with self.assertRaises(AssertionError):
                INST.resolve("BTC/USD")


class CanonicalMarkUsesItsOwnProductTests(_CanonicalHarness):
    """C5/C6 — economics come from the frozen product's book, and when that
    book is unusable the answer is ABSTAIN, never a substituted market."""

    def test_the_canonical_mark_comes_from_the_exact_book(self):
        from lib.paper_mark_economics import canonical_market_mark
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        mark = canonical_market_mark(pos.id)
        self.assertTrue(mark.ok, mark.reason)
        self.assertAlmostEqual(mark.mid, 64_750.0)
        self.assertAlmostEqual(mark.bid, 64_700.0)
        self.assertAlmostEqual(mark.ask, 64_800.0)
        self.assertEqual(mark.instrument_id, PERP_SYM)

    def test_an_unusable_book_abstains_rather_than_substituting(self):
        from lib.paper_mark_economics import canonical_market_mark
        pos, header = self._enter()
        real_top = MD.latest_top

        def aged(sym):
            top = real_top(sym)
            return dict(top, age_s=3_600.0) if top else top
        with patch.object(MD, "latest_top", aged):
            mark = canonical_market_mark(pos.id)
        self.assertFalse(mark.ok)
        self.assertIsNone(mark.mid,
                          "a stale exact book produced a number anyway")

    def test_no_spot_fallback_exists_for_canonical_economics(self):
        """The spot feed is wide open and must still not be consulted."""
        from lib import kraken_stream as KS
        from lib.paper_mark_economics import canonical_market_mark
        pos, header = self._enter()
        real_top = MD.latest_top

        def aged(sym):
            top = real_top(sym)
            return dict(top, age_s=3_600.0) if top else top

        def explode(*a, **k):
            raise AssertionError("canonical economics fell back to spot")
        with patch.object(MD, "latest_top", aged), \
             patch.object(KS, "latest_quote", explode):
            mark = canonical_market_mark(pos.id)
        self.assertFalse(mark.ok)


class RiskManagementDecisionRegressionTests(_CanonicalHarness):
    """C13 — the proof that the defect was DECISION-AFFECTING."""

    def test_a_false_catastrophic_exit_no_longer_fires(self):
        from jobs import paper_trading as PT
        pos, header = self._enter()
        # 26 contracts, ~$64,735 entry. A $100 adverse move is $26 of real
        # loss — nowhere near the catastrophic cap on ~$1,214 of margin.
        # Under the old unit-blind arithmetic it read as $2,600 and tripped
        # the backstop immediately.
        self._set_position(pos.id, stop_loss=1.0, target_price=999_999.0)
        _seed_book(bid=64_635.0, ask=64_645.0)

        with patch.object(PT, "_fetch_ta", return_value={}):
            res = PT._manage_open_positions({"BTC/USD": 64_635.0})

        self.assertEqual(res.get("closed", 0), 0,
                         "a $26 loss triggered the catastrophic backstop — "
                         "the unit-blind arithmetic is still deciding")
        pos2, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])

    def test_a_genuine_catastrophic_loss_still_exits(self):
        """The control: the backstop is not simply disabled."""
        from jobs import paper_trading as PT
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=1.0, target_price=999_999.0)
        # A move large enough that 26 contracts x 0.01 really does exceed
        # the catastrophic cap on the committed margin.
        crash = float(header.actual_entry_fill) - 3_000.0
        _seed_book(bid=crash, ask=crash + 10.0)

        with patch.object(PT, "_fetch_ta", return_value={}):
            res = PT._manage_open_positions({"BTC/USD": crash})

        self.assertEqual(res.get("closed", 0), 1, res)
        pos2, _, _, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(len(outcomes), 1)


class MarkToMarketCanonicalDisplayTests(_CanonicalHarness):
    """C12 — the recorded unrealized P&L is unit-correct too."""

    def test_the_recorded_unrealized_pnl_uses_the_contract_basis(self):
        from lib.paper_engine import mark_to_market
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=1.0, target_price=999_999.0)
        _seed_book(bid=64_635.0, ask=64_645.0)

        mark_to_market({"BTC/USD": 64_635.0})

        pos2, _, _, _ = self._state(pos.id)
        # mark_to_market prices at the mark it was HANDED; the exact-book
        # mid is the management loop's concern, not this one's.
        expected = ((64_635.0 - float(header.actual_entry_fill))
                    * pos.qty * 0.01)
        self.assertAlmostEqual(pos2.unrealized_pnl, round(expected, 2),
                               places=2)
        self.assertLess(abs(pos2.unrealized_pnl), 200.0,
                        "the recorded unrealized P&L is still unit-blind")


if __name__ == "__main__":
    unittest.main()


# ── D ────────────────────────────────────────────────────────────────────
# The legacy hard reset deletes paper_trades, paper_positions and
# paper_portfolio, and knows nothing about the canonical ledger. Against a
# canonical book that leaves settlement headers, legs and realized outcomes
# referring to positions that no longer exist — half an economic record,
# which is worse than either a clean book or an intact one.

class HardResetRefusesACanonicalBookTests(_CanonicalHarness):
    """D1/D2 — refuse, name the reason, and change NOTHING."""

    def test_a_canonical_ledger_refuses_the_hard_reset(self):
        from lib.paper_engine import (CANONICAL_LEDGER_REQUIRES_EPOCH_RESET,
                                      reset_paper_portfolio)
        pos, header = self._enter()
        res = reset_paper_portfolio()
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("error"),
                         CANONICAL_LEDGER_REQUIRES_EPOCH_RESET)

    def test_the_refusal_leaves_every_record_intact(self):
        """The refusal is the whole point: a partial wipe is the failure."""
        from lib.paper_engine import reset_paper_portfolio
        pos, header = self._enter()
        cash = self._portfolio()["cash"]
        reset_paper_portfolio()
        pos2, header2, legs, outcomes = self._state(pos.id)
        self.assertIsNotNone(pos2, "the position was deleted anyway")
        self.assertEqual(pos2.status, "Open")
        self.assertIsNotNone(header2, "the settlement header was orphaned")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(self._portfolio()["cash"], cash)

    def test_a_settled_canonical_book_also_refuses(self):
        """D3 — a CLOSED canonical position still has an outcome and legs.
        Emptiness of the open book is not emptiness of the ledger."""
        from lib import exit_dispatch as ED
        from lib.paper_engine import (CANONICAL_LEDGER_REQUIRES_EPOCH_RESET,
                                      reset_paper_portfolio)
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        out = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(out.get("ok"), out)
        _, _, _, outcomes = self._state(pos.id)
        self.assertEqual(len(outcomes), 1)

        res = reset_paper_portfolio()
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("error"),
                         CANONICAL_LEDGER_REQUIRES_EPOCH_RESET)
        _, _, legs2, outcomes2 = self._state(pos.id)
        self.assertEqual(len(outcomes2), 1, "the realized outcome was wiped")
        self.assertTrue(legs2)

    def test_the_soft_reset_is_still_available(self):
        """D5 — the guard closes the destructive door, not every door. The
        soft reset refills funds and keeps the record, which is what an
        operator wanting a fresh run should be using anyway."""
        from lib.paper_engine import soft_reset_paper_portfolio
        pos, header = self._enter()
        res = soft_reset_paper_portfolio()
        self.assertTrue(res.get("ok"), res)
        pos2, header2, legs, outcomes = self._state(pos.id)
        # It FLATTENS through the dispatcher rather than deleting: the
        # header stands, the entry leg stands, and the exit is settled and
        # recorded like any other. That is the whole difference.
        self.assertIsNotNone(header2)
        self.assertEqual([l.kind for l in legs], ["ENTRY", "FINAL_EXIT"])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(pos2.status, "Closed")


class HardResetStillWorksOnALegacyBookTests(unittest.TestCase):
    """D4 — the operator's book today is pre-B1 and legacy. Nothing about
    this guard may take the reset away from it, and the probe must never
    create the tables it is looking for."""

    def _rows(self, db, table):
        return list(db.execute(text(f"SELECT id FROM {table}")))

    def test_a_pre_b1_legacy_book_still_hard_resets(self):
        from lib.paper_engine import reset_paper_portfolio
        with _PreB1Schema() as db:
            res = reset_paper_portfolio()
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(len(self._rows(db.session, "paper_positions")), 0)
            pf = list(db.session.execute(
                text("SELECT cash FROM paper_portfolio")))
            self.assertEqual(len(pf), 1)
            self.assertAlmostEqual(float(pf[0][0]), 100_000.0)

    def test_the_probe_never_creates_the_canonical_tables(self):
        """D6 — a guard that migrates the operator database as a side
        effect of REFUSING to touch it would be its own incident."""
        from lib.paper_engine import reset_paper_portfolio
        with _PreB1Schema() as db:
            before = db.tables()
            reset_paper_portfolio()
            self.assertEqual(db.tables(), before,
                             "the reset guard altered the schema")

    def test_a_legacy_book_on_a_migrated_schema_still_hard_resets(self):
        """The ledger tables EXISTING is not the same fact as the ledger
        being USED. An empty canonical ledger must not block a legacy
        operator reset."""
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperRealizedOutcome, PaperSettlementLeg,
                                  get_db)
        from lib import exit_dispatch as ED
        from lib.paper_engine import reset_paper_portfolio
        self.assertTrue(ED._canonical_ledger_available(),
                        "this test needs the migrated test schema")
        # The tables exist; empty them, so what is under test is precisely
        # "migrated schema, nothing canonical in it".
        with get_db() as db:
            for model in (PaperRealizedOutcome, PaperSettlementLeg,
                          PaperPositionSettlement, PaperPosition):
                db.query(model).delete()
            db.commit()
        res = reset_paper_portfolio()
        self.assertTrue(res.get("ok"), res)


class TheResetEndpointReportsTheRefusalTests(_CanonicalHarness):
    """D7 — the same B4/B5 property, on the reset door: an API that answers
    "trade history deleted" when nothing was deleted is worse than one that
    errors, because the operator will believe it."""

    def test_a_hard_reset_of_a_canonical_book_is_a_conflict(self):
        from fastapi import HTTPException

        from app.routers.trading import paper_reset
        from lib.paper_engine import CANONICAL_LEDGER_REQUIRES_EPOCH_RESET
        pos, header = self._enter()
        with self.assertRaises(HTTPException) as caught:
            paper_reset(hard=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(CANONICAL_LEDGER_REQUIRES_EPOCH_RESET,
                      str(caught.exception.detail))
        pos2, header2, legs, _ = self._state(pos.id)
        self.assertIsNotNone(pos2)
        self.assertIsNotNone(header2)

    def test_the_endpoint_never_claims_a_deletion_it_did_not_do(self):
        from fastapi import HTTPException

        from app.routers.trading import paper_reset
        pos, header = self._enter()
        try:
            res = paper_reset(hard=True)
        except HTTPException as e:
            res = {"detail": str(e.detail)}
        self.assertNotIn("history deleted", str(res).lower())

    def test_the_soft_reset_path_is_untouched(self):
        from app.routers.trading import paper_reset
        pos, header = self._enter()
        res = paper_reset(hard=False)
        self.assertTrue(res.get("ok"), res)


# ── E5/E6 ───────────────────────────────────────────────────────────────
# The refusal-accounting property, once per caller category. One caller
# reading its own result honestly does not prove the others do.

class EveryCallerCategoryReportsItsRefusalTests(_CanonicalHarness):

    def _manage_once(self, price):
        from jobs import paper_trading as PT
        return PT._manage_open_positions({"BTC/USD": price})

    def _refused(self, plan):
        """Drive one management cycle whose settlement is refused."""
        from jobs import paper_trading as PT
        from lib import canonical_settlement as CS
        _seed_book(bid=64_700.0, ask=64_800.0)
        refusal = {"ok": False, "error": CS.STALE_SETTLEMENT_REVISION,
                   "detail": "injected: another settlement won"}
        with patch.object(PT, "_paper_exit_plan", return_value=plan), \
             patch.object(PT, "_fetch_ta", return_value={}), \
             patch.object(CS, "settle_prepared_exit", return_value=refusal):
            return self._manage_once(64_700.0)

    def _assert_refused(self, res, pos):
        self.assertEqual(res.get("closed", 0), 0, res)
        self.assertGreaterEqual(res.get("refused", 0), 1, res)
        pos2, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])

    def test_a_refused_profit_tier_exit_is_not_counted_closed(self):
        pos, _ = self._enter()
        self._assert_refused(
            self._refused({"ok": True, "action": "EXIT",
                           "reason": ">=10% — take profit"}), pos)

    def test_a_refused_loss_tier_exit_is_not_counted_closed(self):
        pos, _ = self._enter()
        self._assert_refused(
            self._refused({"ok": True, "action": "EXIT",
                           "reason": "<=-4% — cut loss"}), pos)

    def test_a_refused_ai_exit_is_not_counted_closed(self):
        pos, _ = self._enter()
        self._assert_refused(
            self._refused({"ok": True, "action": "EXIT",
                           "reason": "AI EXIT: the thesis broke"}), pos)

    def test_the_refusals_are_reported_with_their_reason(self):
        """A count alone tells an operator something failed but not what.
        The refusal carries the dispatcher's error through."""
        from lib import canonical_settlement as CS
        pos, _ = self._enter()
        res = self._refused({"ok": True, "action": "EXIT",
                             "reason": ">=15% — take profit"})
        detail = res.get("refusals") or []
        self.assertTrue(detail, res)
        self.assertTrue(
            any(CS.STALE_SETTLEMENT_REVISION in str(d.values())
                for d in detail), detail)


class CanonicalStopDistancesUseTheMultiplierTests(_CanonicalHarness):
    """C9/C10/C11 — every dollars→price conversion in the management plan
    goes through qty x multiplier, not qty."""

    def _plan(self, pl_dollar, price=64_700.0):
        from jobs import paper_trading as PT
        from lib import paper_mark_economics as PME
        pos, header = self._enter()
        basis = PME.basis_for_position(pos.id)
        self.assertEqual(basis.route, "CANONICAL")
        d = {"id": pos.id, "asset_symbol": pos.symbol,
             "direction": pos.direction, "qty": pos.qty,
             "entry_price": pos.entry_price, "leverage": pos.leverage,
             "margin_used": pos.margin_used, "stop_loss": pos.stop_loss,
             "target_price": pos.target_price,
             "timeframe": "4H", "_basis": basis}
        return pos, basis, PT._paper_exit_plan(d, price, pl_dollar, {})

    def _open_wide(self):
        """One canonical position with its stop pushed out of the way, so
        the plan's own risk distance is the binding constraint rather than
        an existing stop floor."""
        from lib import paper_mark_economics as PME
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=1.0, target_price=999_999.0)
        from app.database import PaperPosition, get_db
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == pos.id).first()
            db.expunge_all()
        return pos, PME.basis_for_position(pos.id)

    def _plan_with_multiplier(self, pos, basis, mult, pl_dollar, price,
                              ta=None):
        """The same position, priced under two different unit bases. Only
        the multiplier differs, so any difference in the plan is caused by
        it — which is the property under test."""
        import dataclasses

        from jobs import paper_trading as PT
        b = dataclasses.replace(basis, multiplier=mult)
        d = {"id": pos.id, "asset_symbol": pos.symbol,
             "direction": pos.direction, "qty": pos.qty,
             "entry_price": pos.entry_price, "leverage": pos.leverage,
             "margin_used": pos.margin_used, "stop_loss": pos.stop_loss,
             "target_price": pos.target_price,
             "timeframe": "4H", "_basis": b}
        return PT._paper_exit_plan(d, price, pl_dollar, ta or {})

    def test_the_risk_stop_distance_is_driven_by_the_multiplier(self):
        """C9 — hard_loss / (qty x multiplier), not hard_loss / qty. Under
        the qty-only form the widest stop sat 100x closer to entry, which
        is what pinned every stop to a fraction of a percent."""
        from jobs.paper_trading import catastrophic_loss_usd
        pos, basis = self._open_wide()
        self.assertAlmostEqual(basis.multiplier, 0.01)
        hard = catastrophic_loss_usd(basis.margin)
        right = hard / (basis.qty * basis.multiplier)
        wrong = hard / basis.qty
        self.assertAlmostEqual(right / wrong, 100.0, places=6,
                               msg="the two forms must differ by 100x here")
        a = self._plan_with_multiplier(pos, basis, 0.01, 0.0, 64_700.0)
        b = self._plan_with_multiplier(pos, basis, 1.0, 0.0, 64_700.0)
        self.assertEqual(a.get("action"), "ADJUST", a)
        self.assertEqual(b.get("action"), "ADJUST", b)
        self.assertNotAlmostEqual(
            a["stop_loss"], b["stop_loss"], places=2,
            msg="the stop distance ignores the multiplier entirely")
        # Contract units give the WIDER stop — that is the whole point.
        self.assertLess(a["stop_loss"], b["stop_loss"])

    def test_the_profit_lock_distance_is_driven_by_the_multiplier(self):
        """C10 — PROFIT_LOCK_USD / (qty x multiplier). Leverage must not
        reappear here either."""
        from jobs.paper_trading import PROFIT_LOCK_USD
        pos, basis = self._open_wide()
        gain = PROFIT_LOCK_USD + 50.0
        # A wide ATR puts the trailing stop well below entry, so the PROFIT
        # LOCK is the binding constraint and its distance is observable in
        # the result. Without this the trail dominates and the test would
        # pass whatever the lock arithmetic said.
        ta = {"4H": {"atr": {"value": 2_000.0}}}
        a = self._plan_with_multiplier(pos, basis, 0.01, gain, 66_000.0, ta)
        b = self._plan_with_multiplier(pos, basis, 1.0, gain, 66_000.0, ta)
        lock_a = PROFIT_LOCK_USD / (basis.qty * 0.01)
        self.assertAlmostEqual(a["stop_loss"], basis.entry_fill + lock_a,
                               places=1,
                               msg="the lock is not the binding constraint")
        self.assertEqual(a.get("action"), "ADJUST", a)
        self.assertNotAlmostEqual(
            a["stop_loss"], b["stop_loss"], places=2,
            msg="the profit lock distance ignores the multiplier")
        lock = PROFIT_LOCK_USD / (basis.qty * basis.multiplier)
        self.assertAlmostEqual(lock, PROFIT_LOCK_USD / (pos.qty * 0.01),
                               places=6)
        # And leverage plays no part: the old form divided by qty*leverage.
        self.assertNotAlmostEqual(
            lock, PROFIT_LOCK_USD / (pos.qty * float(pos.leverage)),
            places=4)

    def test_the_margin_fallback_multiplies_by_the_contract_size(self):
        """C11 — when margin_used is absent, notional is qty x entry x
        multiplier and margin is that over leverage. qty x entry / leverage
        would be 100x too large and would loosen the backstop by 100x."""
        from jobs import paper_trading as PT
        pos, header = self._enter()
        from lib import paper_mark_economics as PME
        basis = PME.basis_for_position(pos.id)
        d = {"id": pos.id, "asset_symbol": pos.symbol,
             "direction": pos.direction, "qty": pos.qty,
             "entry_price": pos.entry_price, "leverage": pos.leverage,
             "margin_used": 0.0,          # force the fallback
             "stop_loss": pos.stop_loss, "target_price": pos.target_price,
             "timeframe": "4H", "_basis": basis}
        derived = (pos.qty * pos.entry_price * 0.01
                   / max(1.0, float(pos.leverage)))
        wrong = pos.qty * pos.entry_price / max(1.0, float(pos.leverage))
        hard_right = PT.catastrophic_loss_usd(derived)
        hard_wrong = PT.catastrophic_loss_usd(wrong)
        # A loss between the two caps must exit under the correct margin
        # and would have been tolerated under the inflated one.
        between = -(hard_right + (hard_wrong - hard_right) / 2.0)
        plan = PT._paper_exit_plan(d, 64_700.0, between, {})
        self.assertEqual(plan.get("action"), "EXIT", plan)
        self.assertIn(f"${derived:,.2f}", plan["reason"])


# ── F ────────────────────────────────────────────────────────────────────
# Secondary structural defence. The behavioural tests above are the
# authority; these exist because this codebase has a history of wiring that
# was present, correct-looking, and never actually on the path.

class StructuralGuardsTests(unittest.TestCase):

    def _src(self, dotted):
        import importlib
        import inspect
        return inspect.getsource(importlib.import_module(dotted))

    def _func_src(self, dotted, name):
        import ast
        tree = ast.parse(self._src(dotted))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                return ast.get_source_segment(self._src(dotted), node)
        raise AssertionError(f"{dotted}.{name} not found")

    def test_f1_the_dispatcher_guards_the_ledger_query(self):
        """The header is only queried when the table exists — checked at the
        source, not only through the pre-B1 fixture."""
        src = self._func_src("lib.exit_dispatch", "_load")
        self.assertIn("_canonical_ledger_available()", src)
        i = src.index("_canonical_ledger_available()")
        j = src.index("db.query(PaperPositionSettlement)")
        self.assertLess(i, j, "the ledger is queried before the guard")

    def test_f1b_the_pre_b1_path_never_touches_the_settlement_model(self):
        """The behavioural form of the same claim, with a poison: on a
        schema with no ledger, ANY query of the settlement model is a bug."""
        import app.database as dbmod
        from lib import exit_dispatch as ED
        real = dbmod.PaperPositionSettlement

        class Poison:
            def __getattr__(self, item):
                raise AssertionError("the settlement model was queried on a "
                                     "database that has no ledger table")
        # exit_dispatch imports the model from app.database at call time,
        # so that is where the poison has to sit.
        with _PreB1Schema():
            with patch.object(dbmod, "PaperPositionSettlement", Poison()):
                res = ED.request_position_exit(
                    "pos-legacy", caller_price=101.0,
                    caller_reason="manual", caller_source="API_MANUAL")
        self.assertEqual(res.get("route"), "LEGACY", res)
        self.assertIs(dbmod.PaperPositionSettlement, real)

    def test_f2_every_known_source_has_an_explicit_mapping(self):
        from lib import exit_dispatch as ED
        for source in ED.KNOWN_CALLER_SOURCES:
            self.assertTrue(
                source in ED._SOURCE_REASON or source in ED._REASON_SOURCES,
                f"{source} is advertised as known but maps to nothing")

    def test_f3_the_manager_never_counts_a_close_it_did_not_check(self):
        """Every dispatch in the management loop goes through `_exit`, which
        reads the result. A bare request-then-increment is the exact defect
        this gate exists to remove."""
        src = self._func_src("jobs.paper_trading", "_manage_open_positions")
        # The dispatcher is called exactly once in this function: inside
        # the checked helper.
        self.assertEqual(src.count("request_position_exit("), 1, src.count)
        head = src[:src.index("request_position_exit(")]
        self.assertIn("def _exit(", head,
                      "the dispatch call is not inside the checked helper")
        # And no increment of `closed` outside it.
        self.assertEqual(src.count('r["closed"] += 1'), 1)

    def test_f4_canonical_pnl_requires_a_multiplier(self):
        from lib.paper_mark_economics import CANONICAL, MarkBasis
        b = MarkBasis(route=CANONICAL, position_side="long",
                      entry_fill=64_000.0, qty=26.0, multiplier=None,
                      quantity_unit="CONTRACTS", margin=1_000.0,
                      leverage=1.0)
        self.assertFalse(b.usable)
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            self.assertFalse(
                MarkBasis(route=CANONICAL, position_side="long",
                          entry_fill=64_000.0, qty=26.0, multiplier=bad,
                          quantity_unit="CONTRACTS", margin=1_000.0,
                          leverage=1.0).usable, bad)

    def test_f5_dollar_to_price_math_requires_the_multiplier(self):
        from lib.paper_mark_economics import (CANONICAL, MarkBasis,
                                              price_distance_for_usd)
        b = MarkBasis(route=CANONICAL, position_side="long",
                      entry_fill=64_000.0, qty=26.0, multiplier=None,
                      quantity_unit="CONTRACTS", margin=1_000.0,
                      leverage=1.0)
        self.assertIsNone(price_distance_for_usd(b, 350.0))
        src = self._func_src("jobs.paper_trading", "_paper_exit_plan")
        self.assertIn("unit_exposure = qty * mult", src)
        self.assertNotIn("risk_per_unit = hard_loss / qty\n", src)

    def test_f6_the_legacy_arithmetic_branch_is_preserved(self):
        """LEGACY still prices through `_calc_pnl` at multiplier 1.0. The
        667 historical positions are not retro-fitted with contract
        semantics they never traded under."""
        from lib.paper_engine import mark_to_market
        import inspect
        src = inspect.getsource(mark_to_market)
        self.assertIn("_calc_pnl(", src)
        self.assertIn("PME.CANONICAL", src)
        # The legacy branch is the else, and it is still reached.
        self.assertIn("else:", src[src.index("PME.CANONICAL"):])

    def test_f7_the_hard_reset_consults_the_ledger_before_deleting(self):
        src = self._func_src("lib.paper_engine", "reset_paper_portfolio")
        i = src.index("_canonical_book_present()")
        j = src.index("delete()")
        self.assertLess(i, j, "rows are deleted before the guard runs")
        self.assertIn("CANONICAL_LEDGER_REQUIRES_EPOCH_RESET", src)

    def test_f7b_the_probe_never_creates_a_schema(self):
        import ast
        src = self._func_src("lib.paper_engine", "_canonical_book_present")
        # The docstring SAYS it never calls init_db; the body is what has
        # to prove it, so the docstring is removed before checking.
        node = ast.parse(src).body[0]
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            node.body = node.body[1:]
        body = ast.unparse(node)
        for forbidden in ("init_db", "create_all", "metadata.create"):
            self.assertNotIn(forbidden, body)

    def test_f8_no_production_code_calls_the_hard_reset_for_cutover(self):
        """D1 — an epoch cutover must never be implemented by deleting one
        side of the ledger. The ONE production reference is the explicit
        `hard=true` API branch, which now refuses a canonical book."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        hits = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(("tests/", ".venv/")) or "site-packages" in rel:
                continue
            for n, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore")
                        .splitlines(), 1):
                if re.search(r"\breset_paper_portfolio\b", line) and \
                        "soft_reset_paper_portfolio" not in line:
                    hits.append(f"{rel}:{n}: {line.strip()}")
        allowed = ("lib/paper_engine.py", "app/routers/trading.py")
        for h in hits:
            self.assertTrue(h.startswith(allowed),
                            f"unexpected hard-reset caller: {h}")
        self.assertTrue(hits, "the search found nothing — it is broken")
