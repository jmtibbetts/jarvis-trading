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
        # LOWER() is load-bearing: rows are written with status "Open" and
        # SQLite's `=` is case-sensitive, so this counted zero every
        # morning and the brief reported an empty book regardless of what
        # was actually held. Same defect as the concentration guard.
        open_row = c.execute(text("""
            SELECT COUNT(*), ROUND(SUM(unrealized_pnl), 2)
            FROM paper_positions WHERE LOWER(status) = 'open'""")).fetchone()
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


PULSE_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD", "SPY", "QQQ",
                 "CL=F", "GC=F", "NG=F", "EURUSD=X")


def _market_pulse() -> list[dict]:
    """Last close vs prior close across the desk's core instruments —
    1D bars preferred, last-24-1H fallback where dailies are thin."""
    from lib.signal_replay import load_cached_bars

    out = []
    for sym in PULSE_SYMBOLS:
        try:
            bars = load_cached_bars(sym, "1D")
            if bars is None or len(bars) < 2:
                hourly = load_cached_bars(sym, "1H")
                if hourly is None or len(hourly) < 25:
                    continue
                last, prev = float(hourly["close"].iloc[-1]), float(
                    hourly["close"].iloc[-25])
            else:
                last, prev = float(bars["close"].iloc[-1]), float(
                    bars["close"].iloc[-2])
            if prev > 0:
                out.append({"symbol": sym, "last": round(last, 4),
                            "change_pct": round((last - prev) / prev * 100, 2)})
        except Exception:
            continue
    return out


def _derivatives_now() -> list[dict]:
    """Latest stored funding/OI/long-short per Tier-1 base — the perp
    market's current lean, from rows the derivatives job keeps fresh."""
    from sqlalchemy import text

    from app.database import engine

    out = []
    with engine.connect() as c:
        for base in ("BTC", "ETH"):
            row = c.execute(text("""
                SELECT funding_rate, open_interest_usd, long_short_ratio,
                       fetched_at
                FROM crypto_derivatives_snapshots
                WHERE symbol = :b AND venue = 'okx'
                ORDER BY fetched_at DESC LIMIT 1"""), {"b": base}).fetchone()
            if row:
                out.append({"symbol": base, "funding_rate_8h": row[0],
                            "oi_usd": row[1], "long_short_ratio": row[2],
                            "as_of": row[3]})
    return out


def _positioning_table() -> list[dict]:
    """EVERY sector instrument's positioning + curve — the full table,
    not just the tails (the tails stay in positioning_extremes)."""
    out = []
    try:
        from lib.sector_engine import SECTORS, sector_snapshot
        for sector in SECTORS:
            snap = sector_snapshot(sector)
            for key, inst in snap["instruments"].items():
                p, cv = inst.get("positioning", {}), inst.get("curve", {})
                if "abstain" in p and "abstain" in cv:
                    continue
                out.append({
                    "sector": sector, "instrument": key,
                    "spec_pctile_3y": p.get("spec_pctile_3y"),
                    "spec_net": p.get("spec_net"),
                    "curve": cv.get("structure"),
                    "roll_pct": cv.get("annualized_roll_pct"),
                })
    except Exception as e:
        logger.debug(f"[Brief] positioning table unavailable: {e}")
    return out


def _analog_reads() -> list[dict]:
    """The flagship two lines: what followed the moments most similar
    to RIGHT NOW for the Tier-1 symbols. History, not prediction."""
    out = []
    try:
        from lib.analogs import analogs_for
        for sym in ("BTC/USD", "ETH/USD"):
            a = analogs_for(sym, "15m")
            if not a:
                continue
            day = a["forward_summary"].get("fwd_96b", {})
            hour4 = a["forward_summary"].get("fwd_16b", {})
            out.append({
                "symbol": sym, "n_analogs": day.get("n"),
                "candidates_searched": a["candidates_searched"],
                "fwd_1d_median_pct": day.get("median_pct"),
                "fwd_1d_up_rate": day.get("up_rate"),
                "fwd_4h_median_pct": hour4.get("median_pct"),
                "fwd_4h_up_rate": hour4.get("up_rate"),
            })
    except Exception as e:
        logger.debug(f"[Brief] analogs unavailable: {e}")
    return out


def _alerts_in_window(cutoff: str) -> dict:
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT severity, COUNT(*) FROM alerts
            WHERE created_at > :c GROUP BY severity"""),
            {"c": cutoff}).fetchall()
    return {sev: n for sev, n in rows}


def _training_state() -> dict:
    """What the laboratory itself is doing, and whether it can be trusted.

    JARVIS is a training platform before it is a trading one, so the brief
    should open with the health of the training data — not only with what
    the market did. An integrity violation discovered a week later has
    already poisoned a week of evidence.
    """
    from lib.integrity_panel import run_all
    from lib.platform_mode import status as mode_status

    panel = run_all()
    mode = mode_status()
    failing = [c for c in panel["checks"] if c["status"] == "VIOLATION"]
    return {
        "mode": mode["mode"],
        "live_execution_allowed": mode["live_execution_allowed"],
        "mode_detail": mode["detail"],
        "integrity_verdict": panel["verdict"],
        "integrity_healthy": panel["healthy"],
        "violations": panel["violations"],
        "critical": panel["critical"],
        # Named, so the brief says WHICH invariant broke rather than
        # reporting a count nobody can act on.
        "failing_checks": [
            {"title": c["title"], "count": c["count"],
             "severity": c["severity"], "detail": c["detail"]}
            for c in failing[:5]],
        # A check that could not RUN is not a check that passed.
        "checks_unavailable": panel["unavailable"] + panel["errors"],
    }


def build_brief(window_hours: int = 24) -> dict:
    now = _now()
    cutoff_dt = now - timedelta(hours=window_hours)
    cutoff = cutoff_dt.isoformat()
    from lib.brief_news import brief_news
    from lib.incubator import incubator_report
    from lib.threat_transmission import transmission_watch

    def _safe(fn, default):
        try:
            return fn()
        except Exception as e:
            logger.debug(f"[Brief] section failed (served empty): {e}")
            return default

    return {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "market_pulse": _safe(_market_pulse, []),
        "gate_experiment": _gate_movement(cutoff),
        "analog_reads": _safe(_analog_reads, []),
        "derivatives_now": _safe(_derivatives_now, []),
        "positioning": _safe(_positioning_table, []),
        "positioning_extremes": _extremes(),
        # Price-transmission hypotheses only — the geographic threat view
        # lives in the ThreatMap and is deliberately not duplicated here.
        "threat_transmission": _safe(
            lambda: transmission_watch(hours=48)[:8], []),
        "incubator": _safe(lambda: incubator_report(limit=8), {}),
        "alerts": _safe(lambda: _alerts_in_window(cutoff), {}),
        "corpus": _corpus_movement(cutoff),
        "book": _book_movement(cutoff),
        "platform": _platform_movement(cutoff_dt.timestamp()),
        "releases_today": _releases_today(),

        # OVERNIGHT NEWS. `lib/brief_news` was written and wired into
        # nothing — the brief has been reporting market internals every
        # morning with no account of what actually HAPPENED while the desk
        # slept. Bucketed and de-duplicated by the module itself, and
        # symbols the book holds are flagged there rather than here.
        "news": _safe(lambda: brief_news(hours=window_hours, per_bucket=6), {}),

        # TRAINING STATE. The brief is the first screen of the day, so
        # "are my own invariants holding?" belongs on it. A green desk
        # running on corrupted evidence is worse than a red one, and the
        # integrity verdict is the one number that says which.
        "training_state": _safe(_training_state, {}),

        "note": ("assembled from the owning endpoints' own numbers; "
                 "nothing computed fresh here"),
    }
