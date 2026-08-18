"""Decisions keep aging through their horizons whether or not JARVIS trades.

WHY THIS IS A SEPARATE RUNTIME.

A decision made at 09:00 has a 4-hour horizon that comes due at 13:00. Whether
the trading scheduler is running in between is irrelevant to that fact — the
market moved, the horizon elapsed, and the evidence either got collected or
was lost forever. Tying outcome resolution to the trading loop would mean
that pausing trading silently stops the very measurement that tells us
whether trading should resume. That is the same coupling `market_data_runtime`
exists to break, applied one layer up:

    application lifespan
      ├─ MarketDataRuntime   read-only feeds        (independent)
      ├─ EvidenceRuntime     resolves observations  (independent)  <- here
      └─ Trading scheduler   OFF

WHY A NEW MODULE RATHER THAN A SCHEDULER JOB. The obvious existing owner is
APScheduler, and it is the wrong one: it is precisely the thing that is
switched off, and registering evidence work there would make the safe half of
the system depend on the half under review. Nothing else in the repository
owns long-lived background work except the lifespan itself, so this follows
the same shape the market-data runtime already established.

WHAT IT MAY DO, AND NOTHING MORE.

    read   DecisionObservation
    read   instrument_quote_samples
    write  decision_observation_outcomes
    update evidence lifecycle

It may not generate a trade, call canonical entry, close a position, create a
PaperTrade or RealizedOutcome, mutate the portfolio, or send anything to a
broker or exchange. That is enforced by an AST test over this module's
imports, not by convention.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DISABLE_ENV = "JARVIS_DISABLE_EVIDENCE_RUNTIME"

# How often to look for due horizons. Horizons are minutes to days, so a
# minute of latency on a 15-minute horizon is immaterial — and the resolver
# reads the CHECKPOINT from stored evidence at due_at rather than from a live
# quote, so running late does not distort what is measured.
CYCLE_S = 60.0

# Bounded batch: a backlog must not turn one cycle into an unbounded
# transaction that blocks writers for minutes.
BATCH = 500

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()
_health = {
    "service_running": False,
    "cycles": 0,
    "last_cycle_at": None,
    "last_success_at": None,
    "last_error": None,
    "scheduled_total": 0,
    "resolved_total": 0,
}


def evidence_runtime_enabled() -> bool:
    """NOT tied to JARVIS_DISABLE_SCHEDULER — see the module docstring."""
    return os.getenv(DISABLE_ENV) != "1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle() -> dict:
    """One bounded pass: schedule new horizons, resolve what is due.

    Exceptions are contained here so a single bad row cannot kill the
    service — the error is recorded in health rather than silently swallowed.
    """
    from lib.decision_outcome import resolve_due, schedule_pending_observations

    out: dict = {"at": _now_iso()}
    try:
        out["scheduled"] = schedule_pending_observations(limit=BATCH)
        out["resolved"] = resolve_due(limit=BATCH)
        with _lock:
            _health["last_success_at"] = out["at"]
            _health["last_error"] = None
            _health["scheduled_total"] += out["scheduled"].get("scheduled", 0)
            _health["resolved_total"] += out["resolved"].get("resolved", 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        with _lock:
            _health["last_error"] = out["error"]
        logger.warning("[EvidenceRuntime] cycle failed: %s", e)
    with _lock:
        _health["cycles"] += 1
        _health["last_cycle_at"] = out["at"]
    return out


def _loop() -> None:
    while not _stop.is_set():
        run_cycle()
        _stop.wait(CYCLE_S)
    with _lock:
        _health["service_running"] = False


def start() -> dict:
    """Start the resolver. Idempotent — never a second worker."""
    global _thread
    if not evidence_runtime_enabled():
        logger.warning("[EvidenceRuntime] disabled by %s=1", DISABLE_ENV)
        return {"enabled": False, "started": False}
    if _thread is not None and _thread.is_alive():
        return {"enabled": True, "started": False, "reason": "already running"}
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="evidence-runtime",
                               daemon=True)
    _thread.start()
    with _lock:
        _health["service_running"] = True
    return {"enabled": True, "started": True}


def stop(timeout: float = 5.0) -> dict:
    """Stop cleanly. Idempotent."""
    global _thread
    if _thread is None or not _thread.is_alive():
        with _lock:
            _health["service_running"] = False
        return {"stopped": False}
    _stop.set()
    _thread.join(timeout=timeout)
    _thread = None
    with _lock:
        _health["service_running"] = False
    return {"stopped": True}


def health() -> dict:
    """Operational state, including how far behind the resolver is.

    `oldest_overdue_at` is the honest backlog signal: a growing pending
    count is ambiguous (it also grows when trading is busy), but a horizon
    that came due hours ago and is still PENDING means the resolver is not
    keeping up or is not running at all.
    """
    from app.database import DecisionOutcome, get_db
    from lib.decision_outcome import PENDING

    with _lock:
        out = dict(_health)
    out["enabled"] = evidence_runtime_enabled()
    try:
        now = _now_iso()
        with get_db() as db:
            out["pending"] = db.query(DecisionOutcome).filter(
                DecisionOutcome.status == PENDING).count()
            oldest = (db.query(DecisionOutcome.due_at)
                        .filter(DecisionOutcome.status == PENDING,
                                DecisionOutcome.due_at <= now)
                        .order_by(DecisionOutcome.due_at.asc()).first())
            out["oldest_overdue_at"] = oldest[0] if oldest else None
            out["overdue"] = db.query(DecisionOutcome).filter(
                DecisionOutcome.status == PENDING,
                DecisionOutcome.due_at <= now).count()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
