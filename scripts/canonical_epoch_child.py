"""The only process in the dry run that imports application code.

WHY IT IS A SEPARATE PROCESS. `app.database` builds its engine at import
time from JARVIS_DB_PATH, and that engine sets `PRAGMA journal_mode=WAL` on
connect. The environment therefore has to be right BEFORE the interpreter
starts — an in-process `os.environ[...] = ...` after the module is loaded is
already too late. The parent sets JARVIS_DB_PATH, JARVIS_EVENTS_DB_PATH and
JARVIS_OHLCV_DB_PATH and spawns this file; nothing here can reach a live
store because no live path is in its environment.

It prints exactly one machine-readable line, prefixed `@@RESULT@@`, so the
parent never has to parse prose.

PHASES

    init_schema      create the CURRENT schema with the app's own init_db
    seed_portfolio   one fresh wallet at the stated starting cash
    fd_report        this process's open file descriptors, for the parent
    lifecycle        entry -> partial -> final -> outcome -> learning
    restart_verify   reopen in a new process and confirm what persisted
    empty_boot       an economically empty book must still be usable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULT = "@@RESULT@@"

# The PBTC perpetual the canonical machinery was built around: a contract
# instrument whose quantity unit is CONTRACTS at a 0.01 multiplier, which is
# exactly the case unit-blind arithmetic gets wrong by 100x.
PERP_SYM = "PBTCUCZ50"
TICK = 5.0
ENTRY_BID, ENTRY_ASK = 64_500.0, 64_600.0
EXIT_BID, EXIT_ASK = 65_200.0, 65_300.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0


def emit(payload: dict) -> None:
    print(RESULT + json.dumps(payload, default=str))


def _guard_environment() -> None:
    """Refuse to run against anything that is not an explicitly redirected
    database. A child that silently fell back to the operator DB would be
    the one failure this whole design exists to prevent."""
    db = os.environ.get("JARVIS_DB_PATH")
    if not db:
        raise SystemExit("JARVIS_DB_PATH is not set — refusing to run")
    if Path(db).resolve() == (REPO / "data" / "jarvis.db").resolve():
        raise SystemExit("JARVIS_DB_PATH points at the operator database")


# ── market boundary ──────────────────────────────────────────────────────
def _at(seconds_ago: float = 0.1):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def seed_book(bid: float, ask: float):
    """Deterministic executable depth for the frozen contract. This is the
    ONLY thing stubbed: the market boundary. Everything downstream is the
    real production call graph."""
    from lib import bitnomial_market_data as MD
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at().isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid / TICK), 500]],
                "asks": [[int(ask / TICK), 500]]})
    return book


def spot_feed():
    from unittest.mock import patch
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": SPOT_BID, "ask": SPOT_ASK,
                                     "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


def signal():
    from lib import product_router as PR
    return {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": "sig-cutover-dry-run-1",
            "product": PR.CRYPTO_PERP}


# ── phases ───────────────────────────────────────────────────────────────
def phase_init_schema(_payload: dict) -> dict:
    from app.database import engine, init_db
    from sqlalchemy import inspect
    init_db()
    insp = inspect(engine)
    tables = sorted(insp.get_table_names())
    import hashlib
    h = hashlib.sha256()
    for t in tables:
        cols = sorted(c["name"] for c in insp.get_columns(t))
        h.update((t + ":" + ",".join(cols) + "\n").encode())
    return {"ok": True, "table_count": len(tables),
            "schema_fingerprint": h.hexdigest(), "db": os.environ["JARVIS_DB_PATH"]}


def phase_seed_portfolio(payload: dict) -> dict:
    """One fresh wallet. Deliberately NOT reset_paper_portfolio(): that is a
    destructive legacy reset which now refuses a canonical ledger anyway. A
    cutover creates a new book, it does not mutate an old one until it looks
    new."""
    from app.database import PaperPortfolio, get_db, new_id
    cash = float(payload["starting_cash"])
    with get_db() as db:
        existing = db.query(PaperPortfolio).all()
        if len(existing) > 1:
            return {"ok": False, "error": f"{len(existing)} portfolios"}
        if not existing:
            db.add(PaperPortfolio(id=new_id(), cash=cash, total_trades=0,
                                  winning_trades=0, realized_pnl=0.0))
            db.commit()
        row = db.query(PaperPortfolio).first()
        out = {"ok": True, "id": row.id, "cash": float(row.cash),
               "total_trades": int(row.total_trades or 0),
               "winning_trades": int(row.winning_trades or 0),
               "realized_pnl": float(row.realized_pnl or 0.0)}
    return out


def phase_fd_report(_payload: dict) -> dict:
    """What this process actually has open — the parent asserts on it."""
    import app.database  # noqa: F401  (force the engine to connect)
    from app.database import get_db, PaperPortfolio
    with get_db() as db:
        db.query(PaperPortfolio).first()
    fds = []
    fd_dir = Path("/proc") / str(os.getpid()) / "fd"
    try:
        for fd in fd_dir.iterdir():
            try:
                fds.append(os.readlink(fd))
            except OSError:
                continue
    except (FileNotFoundError, PermissionError, OSError):
        fds = ["<'/proc' unavailable on this platform>"]
    return {"ok": True, "fds": fds, "pid": os.getpid(),
            "db_path": os.environ.get("JARVIS_DB_PATH"),
            "events_path": os.environ.get("JARVIS_EVENTS_DB_PATH"),
            "ohlcv_path": os.environ.get("JARVIS_OHLCV_DB_PATH")}


def _economy() -> dict:
    from app.database import (PaperPortfolio, PaperPosition,
                              PaperPositionSettlement, PaperRealizedOutcome,
                              PaperSettlementLeg, PaperTrade, TradeOutcome,
                              get_db)
    with get_db() as db:
        pf = db.query(PaperPortfolio).first()
        return {
            "cash": float(pf.cash) if pf else None,
            "total_trades": int(pf.total_trades or 0) if pf else None,
            "positions": db.query(PaperPosition).count(),
            "trades": db.query(PaperTrade).count(),
            "trade_outcomes": db.query(TradeOutcome).count(),
            "headers": db.query(PaperPositionSettlement).count(),
            "legs": db.query(PaperSettlementLeg).count(),
            "realized": db.query(PaperRealizedOutcome).count(),
        }


def phase_lifecycle(payload: dict) -> dict:
    """P10 — the real call graph, end to end, on the candidate."""
    from app.database import (PaperPosition, PaperPositionSettlement,
                              PaperRealizedOutcome, PaperSettlementLeg,
                              TradeOutcome, get_db)
    from lib import canonical_entry as CE
    from lib import exit_dispatch as ED
    from lib.engine_epoch import ENGINE_EPOCH

    c0 = float(payload["starting_cash"])
    checks: dict[str, bool] = {}
    before = _economy()
    if before["cash"] != c0 or before["positions"] or before["trades"]:
        return {"ok": False, "error": f"candidate not at zero state: {before}"}

    # ── ENTRY ────────────────────────────────────────────────────────────
    seed_book(ENTRY_BID, ENTRY_ASK)
    with spot_feed():
        entered = CE.open_canonical_position(signal(), decision_price=64_400.0)
    if not entered.get("ok"):
        return {"ok": False, "error": f"entry refused: {entered}"}
    pos_id = entered["position"]["id"]

    with get_db() as db:
        header = db.query(PaperPositionSettlement).filter(
            PaperPositionSettlement.position_id == pos_id).first()
        pos = db.query(PaperPosition).filter(
            PaperPosition.id == pos_id).first()
        entry_leg = db.query(PaperSettlementLeg).filter(
            PaperSettlementLeg.position_id == pos_id).all()
        basis = {"symbol": header.symbol, "product": header.product,
                 "venue": header.venue, "instrument": header.instrument_id,
                 "quantity_unit": header.quantity_unit,
                 "multiplier": float(header.multiplier),
                 "qty": float(header.quantity)}
        header_epoch = header.engine_epoch
        entry_fill = float(header.actual_entry_fill)
        entry_fee = float(header.entry_fee_usd or 0.0)
        committed_margin = float(header.committed_margin_usd or 0.0)
        pos_qty = float(pos.qty)
        db.expunge_all()

    after_entry = _economy()
    checks["entry"] = (after_entry["positions"] == 1
                       and after_entry["headers"] == 1
                       and len(entry_leg) == 1
                       and after_entry["realized"] == 0
                       and after_entry["trade_outcomes"] == 0
                       and after_entry["total_trades"] == 0)
    cash_after_entry = after_entry["cash"]
    checks["entry_cash"] = abs(
        cash_after_entry - (c0 - committed_margin - entry_fee)) < 1e-6

    # P10.3 — the NEW epoch, not the one that predates exact exit settlement.
    checks["new_epoch"] = header_epoch == ENGINE_EPOCH
    # P10.8 — contract-native throughout. No COINS, no multiplier 1.
    checks["unit_basis"] = (basis["symbol"] == "BTC/USD"
                            and basis["product"] == "CRYPTO_PERP"
                            and basis["venue"] == "kraken_derivatives_us"
                            and basis["instrument"] == PERP_SYM
                            and basis["quantity_unit"] == "CONTRACTS"
                            and abs(basis["multiplier"] - 0.01) < 1e-12)

    # ── PARTIAL ──────────────────────────────────────────────────────────
    seed_book(EXIT_BID, EXIT_ASK)
    partial = ED.request_position_partial_exit(
        pos_id, fraction=0.5, caller_price=EXIT_BID,
        caller_reason="scale_out_tp1", caller_source="PAPER_TP1")
    if not partial.get("ok"):
        return {"ok": False, "error": f"partial refused: {partial}",
                "checks": checks}

    with get_db() as db:
        legs = (db.query(PaperSettlementLeg)
                .filter(PaperSettlementLeg.position_id == pos_id)
                .order_by(PaperSettlementLeg.settlement_revision).all())
        partial_leg = [l for l in legs if l.kind == "PARTIAL_EXIT"]
        p_leg = partial_leg[0] if partial_leg else None
        partial_facts = {
            "kind": p_leg.kind if p_leg else None,
            "revision": int(p_leg.settlement_revision) if p_leg else None,
            "qty": float(p_leg.filled_quantity) if p_leg else None,
            "fee": float(p_leg.fee_usd or 0.0) if p_leg else None,
            "holding": float(p_leg.holding_cost_usd or 0.0) if p_leg else None,
            "gross": float(p_leg.gross_pnl_usd or 0.0) if p_leg else None,
            "released": float(p_leg.released_margin_usd or 0.0) if p_leg else None,
        }
        db.expunge_all()

    after_partial = _economy()
    checks["partial"] = (p_leg is not None
                         and partial_facts["revision"] == 1
                         and after_partial["realized"] == 0
                         and after_partial["trade_outcomes"] == 0
                         and after_partial["total_trades"] == 0
                         and after_partial["positions"] == 1)

    # ── FINAL ────────────────────────────────────────────────────────────
    final = ED.request_position_exit(
        pos_id, caller_price=EXIT_BID, caller_reason="api_manual",
        caller_source="API_MANUAL")
    if not final.get("ok"):
        return {"ok": False, "error": f"final refused: {final}",
                "checks": checks}

    with get_db() as db:
        pos = db.query(PaperPosition).filter(
            PaperPosition.id == pos_id).first()
        header = db.query(PaperPositionSettlement).filter(
            PaperPositionSettlement.position_id == pos_id).first()
        legs = (db.query(PaperSettlementLeg)
                .filter(PaperSettlementLeg.position_id == pos_id)
                .order_by(PaperSettlementLeg.settlement_revision).all())
        realized = db.query(PaperRealizedOutcome).filter(
            PaperRealizedOutcome.position_id == pos_id).all()
        outcome = realized[0] if realized else None
        final_leg = [l for l in legs if l.kind == "FINAL_EXIT"]
        f_leg = final_leg[0] if final_leg else None
        facts = {
            "position_status": pos.status if pos else None,
            "header_status": header.status if header else None,
            "leg_kinds": [l.kind for l in legs],
            "final_revision": int(f_leg.settlement_revision) if f_leg else None,
            "remaining_qty": float(header.remaining_quantity or 0.0) if header else None,
            "remaining_margin": float(header.remaining_margin_usd or 0.0) if header else None,
            "learning_state": outcome.learning_state if outcome else None,
            "outcome_epoch": outcome.engine_epoch if outcome else None,
        }
        exit_fees = sum(float(l.fee_usd or 0.0) for l in legs
                        if l.kind in ("PARTIAL_EXIT", "FINAL_EXIT"))
        holding = sum(float(l.holding_cost_usd or 0.0) for l in legs)
        gross = sum(float(l.gross_pnl_usd or 0.0) for l in legs)
        released = sum(float(l.released_margin_usd or 0.0) for l in legs)
        final_facts = {
            "qty": float(f_leg.filled_quantity) if f_leg else None,
            "fee": float(f_leg.fee_usd or 0.0) if f_leg else None,
            "holding": float(f_leg.holding_cost_usd or 0.0) if f_leg else None,
            "gross": float(f_leg.gross_pnl_usd or 0.0) if f_leg else None,
            "released": float(f_leg.released_margin_usd or 0.0) if f_leg else None,
        }
        canonical_outcomes = db.query(TradeOutcome).count()
        outcome_epochs = sorted({r[0] for r in db.query(
            TradeOutcome.engine_epoch).all()})
        db.expunge_all()

    end = _economy()
    checks["final"] = (facts["position_status"] == "Closed"
                       and facts["leg_kinds"] == ["ENTRY", "PARTIAL_EXIT",
                                                  "FINAL_EXIT"]
                       and abs(facts["remaining_qty"] or 0.0) < 1e-9
                       and abs(facts["remaining_margin"] or 0.0) < 1e-9)
    checks["outcome"] = end["realized"] == 1
    checks["learning"] = (facts["learning_state"] == "APPLIED"
                          and canonical_outcomes == 1)

    # P10.7 — one thesis, one vote. Three settlement events, one result.
    checks["one_vote"] = (end["total_trades"] == 1 and end["realized"] == 1
                          and canonical_outcomes == 1 and end["trades"] == 1)

    # P10.6 — the full cash identity. Margin cancels as returned capital.
    expected = c0 + gross - entry_fee - exit_fees - holding
    actual = end["cash"]
    checks["cash_identity"] = abs(expected - actual) < 1e-6
    checks["margin_released"] = abs(released - committed_margin) < 1e-6
    # The outcome must carry the epoch that produced it, and that epoch must
    # be the one the learners filter on.
    checks["outcome_epoch_current"] = outcome_epochs == [ENGINE_EPOCH]

    return {
        "ok": all(checks.values()),
        "checks": checks,
        "position_id": pos_id,
        "unit_basis": basis,
        "header_epoch": header_epoch,
        "outcome_epochs": outcome_epochs,
        "entry_fill": entry_fill,
        "entry_fee": entry_fee,
        "committed_margin": committed_margin,
        "released_margin": released,
        "exit_fees": exit_fees,
        "holding_costs": holding,
        "gross": gross,
        "entry_qty": pos_qty,
        "partial": partial_facts,
        "final": final_facts,
        "cash_c0": c0,
        "cash_after_entry": cash_after_entry,
        "cash_expected": expected,
        "cash_actual": actual,
        "total_trades": end["total_trades"],
        "realized_outcomes": end["realized"],
        "trade_outcomes": canonical_outcomes,
        "learning_state": facts["learning_state"],
        "facts": facts,
    }


def phase_restart_verify(_payload: dict) -> dict:
    """P11 — a NEW process, same database. What survived?"""
    from app.database import (PaperPosition, PaperRealizedOutcome,
                              TradeOutcome, get_db)
    from sqlalchemy import text
    with get_db() as db:
        integrity = db.execute(
            text("PRAGMA integrity_check")).fetchone()[0]
        realized = db.query(PaperRealizedOutcome).all()
        outcome = realized[0] if realized else None
        out = {
            "ok": (integrity == "ok" and len(realized) == 1
                   and outcome.learning_state == "APPLIED"),
            "integrity": integrity,
            "realized_outcomes": len(realized),
            "trade_outcomes": db.query(TradeOutcome).count(),
            "learning_state": outcome.learning_state if outcome else None,
            "closed_positions": db.query(PaperPosition).filter(
                PaperPosition.status == "Closed").count(),
        }
        db.expunge_all()
    return out


def phase_empty_boot(_payload: dict) -> dict:
    """P12 — an economically empty book must still be readable.

    Something that breaks only because there is no history has an
    empty-state bug. The fix is the empty-state assumption, never copying
    old economics forward to satisfy a dashboard.
    """
    surfaces: dict[str, str] = {}

    def attempt(name, fn):
        try:
            fn()
            surfaces[name] = "ok"
        except Exception as exc:               # noqa: BLE001 - reporting
            surfaces[name] = f"{type(exc).__name__}: {exc}"

    def _calibration():
        from lib.calibration import build_calibration_table
        build_calibration_table(force=True)

    def _expectancy():
        from lib import expectancy
        for name in ("build_expectancy_table", "build_table", "table"):
            fn = getattr(expectancy, name, None)
            if callable(fn):
                try:
                    fn(force=True)
                except TypeError:
                    fn()
                return

    def _portfolio():
        from lib.paper_engine import get_paper_portfolio
        get_paper_portfolio()

    def _mark():
        from lib.paper_engine import mark_to_market
        mark_to_market({})

    def _positions():
        from lib.paper_engine import get_open_paper_positions
        get_open_paper_positions()

    attempt("calibration", _calibration)
    attempt("expectancy", _expectancy)
    attempt("paper_portfolio", _portfolio)
    attempt("mark_to_market", _mark)
    attempt("open_positions", _positions)

    bad = {k: v for k, v in surfaces.items() if v != "ok"}
    return {"ok": not bad, "surfaces": surfaces,
            "error": None if not bad else f"empty-state failures: {bad}"}


PHASES = {
    "init_schema": phase_init_schema,
    "seed_portfolio": phase_seed_portfolio,
    "fd_report": phase_fd_report,
    "lifecycle": phase_lifecycle,
    "restart_verify": phase_restart_verify,
    "empty_boot": phase_empty_boot,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--payload", default="{}")
    args = ap.parse_args()
    _guard_environment()
    try:
        emit(PHASES[args.phase](json.loads(args.payload)))
    except Exception as exc:                   # noqa: BLE001 - reported up
        import traceback
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}",
              "traceback": traceback.format_exc()[-3000:]})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
