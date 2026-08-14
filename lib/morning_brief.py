"""The Morning Brief — one answer to "what happened while I slept?".

The desk runs a dozen slow processes that each report in their own
corner: gate arms resolving, labels maturing, context buckets filling,
official releases landing, streams accumulating. The brief is a single
read over all of them for a time window — nothing here computes anything
new, it only ASSEMBLES what the existing machinery already measured.
Every number is traceable to the endpoint that owns it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gate_movement(cutoff: str) -> dict:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        totals = {d: {"candidates": n, "resolved": r or 0}
                  for d, n, r in c.execute(text("""
            SELECT gate_v8_decision, COUNT(*), SUM(resolved)
            FROM candidate_signals WHERE gate_v8_decision IS NOT NULL
            GROUP BY gate_v8_decision"""))}
        fresh = {d: {"resolved": n, "win_rate": wr, "avg_pnl_pct": pnl}
                 for d, n, wr, pnl in c.execute(text("""
            SELECT gate_v8_decision, COUNT(*),
                   ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 100.0 ELSE 0 END), 1),
                   ROUND(AVG(pnl_pct), 3)
            FROM candidate_signals
            WHERE resolved = 1 AND gate_v8_decision IS NOT NULL
              AND resolved_at > :cutoff
            GROUP BY gate_v8_decision"""), {"cutoff": cutoff})}
        new_trades = [r[0] for r in c.execute(text("""
            SELECT DISTINCT symbol FROM candidate_signals
            WHERE gate_v8_decision = 'TRADE' AND created_at > :cutoff
            LIMIT 8"""), {"cutoff": cutoff})]
    return {"arms": totals, "resolved_in_window": fresh,
            "new_trade_picks": new_trades}


def _corpus_movement(cutoff: str) -> dict:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        snaps, = c.execute(text("""
            SELECT COUNT(*) FROM feature_snapshots
            WHERE created_at > :c"""), {"c": cutoff}).fetchone()
        labels = [{"horizon_min": h, "status": s, "n": n}
                  for h, s, n in c.execute(text("""
            SELECT horizon_min, status, COUNT(*) FROM feature_labels
            WHERE resolved_at > :c GROUP BY horizon_min, status
            ORDER BY horizon_min"""), {"c": cutoff})]
        due_today, = c.execute(text("""
            SELECT COUNT(*) FROM feature_labels
            WHERE status = 'pending' AND due_at < :eod"""),
            {"eod": (_now().replace(hour=23, minute=59)).isoformat()}
        ).fetchone()
        ctx = c.execute(text("""
            SELECT SUM(CASE WHEN market_context IS NOT NULL THEN 1 ELSE 0 END),
                   COUNT(*) FROM candidate_signals WHERE resolved = 1
        """)).fetchone()
    return {"snapshots_taken": snaps, "labels_moved": labels,
            "labels_due_today": due_today,
            "ablation_coverage": {"with_context": ctx[0] or 0,
                                  "resolved_total": ctx[1] or 0}}


def _book_movement(cutoff: str) -> dict:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        open_row = c.execute(text("""
            SELECT COUNT(*), ROUND(SUM(unrealized_pnl), 2)
            FROM paper_positions WHERE status = 'open'""")).fetchone()
        closed = c.execute(text("""
            SELECT COUNT(*), ROUND(SUM(realized_pnl), 2)
            FROM paper_trades WHERE closed_at > :c"""),
            {"c": cutoff}).fetchone()
        signals, = c.execute(text("""
            SELECT COUNT(*) FROM trading_signals
            WHERE generated_at > :c"""), {"c": cutoff}).fetchone()
    return {"open_positions": open_row[0] or 0,
            "open_unrealized_pnl": open_row[1],
            "closed_in_window": closed[0] or 0,
            "realized_pnl_window": closed[1],
            "new_signals": signals}


def _platform_movement(cutoff_ts: float) -> dict:
    out: dict = {"events_by_kind": {}}
    try:
        from lib.event_store import get_store
        import sqlite3

        store = get_store()
        with sqlite3.connect(store.path) as c:
            for kind, n in c.execute(
                    "SELECT kind, COUNT(*) FROM events WHERE ingest_ts > ? "
                    "GROUP BY kind ORDER BY COUNT(*) DESC", (cutoff_ts,)):
                out["events_by_kind"][kind] = n
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def _extremes() -> list[dict]:
    """Positioning percentiles at the tails across every sector — the
    'what is stretched' line. Abstaining blocks simply don't appear."""
    out = []
    try:
        from lib.sector_engine import SECTORS, sector_snapshot
        for sector in SECTORS:
            snap = sector_snapshot(sector)
            for key, inst in snap["instruments"].items():
                p = inst.get("positioning", {})
                pct = p.get("spec_pctile_3y")
                if pct is not None and (pct <= 10 or pct >= 90):
                    out.append({"sector": sector, "instrument": key,
                                "spec_pctile_3y": pct,
                                "spec_net": p.get("spec_net")})
    except Exception as e:
        logger.debug(f"[Brief] extremes unavailable: {e}")
    return out


def _releases_today() -> list[str]:
    wd = _now().weekday()
    out = []
    if wd == 2:
        out.append("EIA petroleum status (10:30 ET) — crude stocks")
    if wd == 3:
        out.append("EIA natgas storage (10:30 ET)")
    if wd == 4:
        out.append("CFTC COT release (15:30 ET) — all tracked markets")
    if wd in (5, 6):
        out.append("weekend — equity/futures closed, crypto trades")
    return out


def build_brief(window_hours: int = 24) -> dict:
    now = _now()
    cutoff_dt = now - timedelta(hours=window_hours)
    cutoff = cutoff_dt.isoformat()
    return {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "gate_experiment": _gate_movement(cutoff),
        "corpus": _corpus_movement(cutoff),
        "book": _book_movement(cutoff),
        "platform": _platform_movement(cutoff_dt.timestamp()),
        "positioning_extremes": _extremes(),
        "releases_today": _releases_today(),
        "note": ("assembled from the owning endpoints' own numbers; "
                 "nothing computed fresh here"),
    }
