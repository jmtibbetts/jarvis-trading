"""The prospective evidence daemon: decide for real, touch the book never.

WHAT RUNS HERE

    MarketDataRuntime   real Bitnomial/Kraken books, read-only
    candidate loop      the SAME evaluator the trading path uses
    EvidenceRuntime     schedules and resolves forward horizons

WHAT DOES NOT

    mark_to_market, open-position management, canonical entry, any broker.

WHY NOT `jobs.paper_trading.run()`. That function opens with
`mark_to_market()` and position management. The EVIDENCE_ONLY mutation guards
would refuse the economic parts, but a research daemon repeatedly walking
into code it is forbidden to execute is bad architecture, not defence in
depth. This calls `evaluate_pending_candidates` directly — the decision half
only, and the very same function `run()` uses, so the dataset describes the
real system rather than a research replica of it.

THE ACTIVATION BOUNDARY IS LOAD-BEARING. The evidence database is SEEDED from
the operator DB, so it arrives already full of old signals. Treating those as
prospective would backfill thousands of stale rows into a dataset whose whole
value is that it is forward-looking. Only signals generated at or after the
epoch start are eligible; anything older needs replay semantics, which is a
different thing with a different label.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# How often to look for fresh candidates. Matched to the scheduler's own
# paper cadence rather than invented — a faster loop would not find more
# opportunities, it would only re-ask the same question.
CANDIDATE_INTERVAL_S = float(os.getenv("JARVIS_EVIDENCE_SCAN_S", "300"))

# ── THE SAFE ALLOW-LIST ─────────────────────────────────────────────────
#
# Candidates do not appear on their own: with the autonomous scheduler off,
# nothing refreshes market data and nothing generates signals, so the
# evaluator would correctly find zero candidates forever and the dataset
# would never fill. These two jobs are therefore run here — and ONLY these
# two, on their production cadences.
#
# Both were audited for economic surface before being allowed in: neither
# `fetch_market_data` nor `generate_signals` references open_paper_position,
# prepare_entry, settle_position_entry, close_paper_position, submit_order
# or TradingClient. They read markets and write analysis rows.
#
# NOT allow-listed, deliberately: paper_trading (position management),
# execute_signals (broker), manage_positions, guardian, autosim, dex
# autotrade — anything that can move an economic book or reach a venue with
# an order. A guard would refuse them; not running them is better.
# Bounded by one in-flight provider call (ALPACA_READ_TIMEOUT, 12s) plus
# teardown, so a normal stop lands well inside systemd TimeoutStopSec=30.
WORKER_JOIN_TIMEOUT_S = float(os.getenv("JARVIS_EVIDENCE_JOIN_S", "20"))

MARKET_INTERVAL_S = float(os.getenv("JARVIS_EVIDENCE_MARKET_S", "900"))    # 15m
SIGNAL_INTERVAL_S = float(os.getenv("JARVIS_EVIDENCE_SIGNAL_S", "1800"))   # 30m

_stop = threading.Event()

# ── PER-STAGE HEALTH ─────────────────────────────────────────────────────
#
# THE DISTINCTION THAT MATTERS IS ATTEMPT vs SUCCESS. The starvation bug hid
# because "the service is active" and "quote evidence is growing" were both
# true while the signal stage had never run once. A counter of attempts would
# not have caught it either, because the stage was never even entered. What
# exposes it is asking each stage separately: when did you last SUCCEED, and
# is that older than your own cadence allows?
STAGE_MARKET = "MARKET_REFRESH"
STAGE_SIGNAL = "SIGNAL_GENERATION"
STAGE_SCAN = "CANDIDATE_EVALUATION"

HEALTHY, RUNNING_LONG, STARTING = "HEALTHY", "RUNNING_LONG", "STARTING"
DEGRADED, STALE, FAILED, NEVER_RAN = "DEGRADED", "STALE", "FAILED", "NEVER_RAN"

_stage_lock = threading.Lock()


def _new_stage(cadence_s):
    return {"cadence_s": cadence_s, "attempts": 0, "successes": 0,
            "failures": 0, "consecutive_failures": 0,
            "last_attempt_at": None, "last_success_at": None,
            "last_failure_at": None, "last_error": None,
            "last_duration_s": None, "in_progress": False,
            "started_at": None, "thread_alive": None}


_stages = {
    STAGE_MARKET: _new_stage(900.0),
    STAGE_SIGNAL: _new_stage(1800.0),
    STAGE_SCAN: _new_stage(300.0),
}
_threads = {}

_state = {
    "started_at": None,
    "scans": 0,
    "last_scan_at": None,
    "last_error": None,
    "candidates_seen": 0,
    "evaluated_total": 0,
    "market_refreshes": 0,
    "signal_runs": 0,
    "signals_generated_total": 0,
    "last_market_at": None,
    "last_signal_at": None,
}


def _stage_begin(name):
    with _stage_lock:
        st = _stages[name]
        st["attempts"] += 1
        st["in_progress"] = True
        st["started_at"] = _now().isoformat()
        st["last_attempt_at"] = st["started_at"]
    return time.monotonic()


def _stage_end(name, t0, ok, error=None):
    """A FAILURE MUST NOT REFRESH THE SUCCESS CLOCK. That is the whole
    contract; otherwise a stage failing forever looks recently alive."""
    with _stage_lock:
        st = _stages[name]
        st["in_progress"] = False
        st["started_at"] = None
        st["last_duration_s"] = round(time.monotonic() - t0, 2)
        if ok:
            st["successes"] += 1
            st["consecutive_failures"] = 0
            st["last_success_at"] = _now().isoformat()
        else:
            st["failures"] += 1
            st["consecutive_failures"] += 1
            st["last_failure_at"] = _now().isoformat()
            st["last_error"] = error


def _age_s(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (_now() - t).total_seconds()
    except (TypeError, ValueError):
        return None


def stage_health(name, uptime_s=0.0, grace_multiple=2.0):
    """One stage verdict, cadence-aware.

    A 30-minute stage is not stale 30 seconds after boot, so the grace comes
    from the stage OWN cadence. Once a couple of expected cycles pass with no
    success, health says so plainly, which is exactly what was missing when
    the LLM stage never ran.
    """
    with _stage_lock:
        st = dict(_stages[name])
    st["thread_alive"] = (_threads[name].is_alive()
                          if name in _threads else None)
    st["last_success_age_s"] = _age_s(st["last_success_at"])
    grace = st["cadence_s"] * grace_multiple

    if st["successes"] == 0:
        if st["in_progress"]:
            st["status"] = RUNNING_LONG if uptime_s >= grace else STARTING
        elif uptime_s < grace:
            st["status"] = STARTING
        else:
            # THE STARVATION SIGNATURE: alive, past grace, never succeeded.
            st["status"] = NEVER_RAN
    elif st["thread_alive"] is False:
        st["status"] = FAILED
    elif st["consecutive_failures"] >= 3:
        st["status"] = DEGRADED
    elif (st["last_success_age_s"] or 0) > grace:
        st["status"] = RUNNING_LONG if st["in_progress"] else STALE
    else:
        st["status"] = HEALTHY
    return st


def aggregate_health(stages):
    order = [FAILED, NEVER_RAN, STALE, DEGRADED, RUNNING_LONG, STARTING, HEALTHY]
    worst = HEALTHY
    for st in stages.values():
        if order.index(st["status"]) < order.index(worst):
            worst = st["status"]
    return worst


def _now():
    return datetime.now(timezone.utc)


def epoch_name(started: datetime) -> str:
    return "FORWARD_EVIDENCE_" + started.strftime("%Y%m%dT%H%M%SZ")


def _scan_once() -> dict:
    """One candidate pass. Exceptions are contained, never fatal."""
    from jobs.paper_trading import _get_all_prices, evaluate_pending_candidates

    out = {"at": _now().isoformat()}
    t0 = _stage_begin(STAGE_SCAN)
    try:
        prices = _get_all_prices()
        # auto_trade_enabled is irrelevant in EVIDENCE_ONLY — the evaluator
        # deliberately does not let an economic switch silence research.
        r = evaluate_pending_candidates(prices, auto_trade_enabled=True)
        out.update(r)
        _state["evaluated_total"] += int(r.get("evaluated") or 0)
        _state["last_error"] = None
        _stage_end(STAGE_SCAN, t0, True)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        _state["last_error"] = out["error"]
        _stage_end(STAGE_SCAN, t0, False, out["error"])
        logger.warning("[EvidenceCollector] scan failed: %s", e, exc_info=True)
    _state["scans"] += 1
    _state["last_scan_at"] = out["at"]
    return out


def _run_safe_job(name: str, fn, stage: str) -> dict:
    """Run one allow-listed generator. A failure degrades health, never the
    daemon — a bad provider must not become a restart storm."""
    t0 = _stage_begin(stage)
    try:
        r = fn()
        logger.info("[EvidenceCollector] %s: %s", name,
                    str(r)[:300] if r is not None else "ok")
        _stage_end(stage, t0, True)
        return {"ok": True, "result": r}
    except Exception as e:
        logger.warning("[EvidenceCollector] %s failed: %s", name, e)
        _state["last_error"] = f"{name}: {type(e).__name__}: {e}"
        _stage_end(stage, t0, False, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}


def _refresh_market() -> None:
    from jobs.fetch_market_data import run as market_run
    # The SAME stop event the daemon uses — one cancellation authority, not
    # a second one that could disagree with it.
    _run_safe_job("market refresh", lambda: market_run(cancel_event=_stop),
                  STAGE_MARKET)
    _state["market_refreshes"] += 1
    _state["last_market_at"] = _now().isoformat()


def _generate_signals() -> None:
    from jobs.generate_signals import run as signals_run
    r = _run_safe_job("signal generation", signals_run, STAGE_SIGNAL)
    _state["signal_runs"] += 1
    _state["last_signal_at"] = _now().isoformat()
    res = r.get("result")
    if isinstance(res, dict):
        for k in ("created", "generated", "signals", "new"):
            if isinstance(res.get(k), int):
                _state["signals_generated_total"] += res[k]
                break


def health() -> dict:
    from lib import evidence_runtime as ER
    from lib import market_data_runtime as MDR
    from lib import runtime_mode as RM

    up = _age_s(_state["started_at"]) or 0.0
    stages = {n: stage_health(n, uptime_s=up) for n in _stages}
    h = {"aggregate": aggregate_health(stages),
         "uptime_s": round(up, 1),
         "stages": stages,
         "runtime_mode": RM.current_mode(),
         "db_path": os.getenv("JARVIS_DB_PATH"),
         "evidence_epoch": os.getenv("JARVIS_EVIDENCE_EPOCH"),
         "candidate_interval_s": CANDIDATE_INTERVAL_S,
         **_state}
    try:
        h["market_data"] = MDR.health()
    except Exception as e:
        h["market_data"] = {"error": str(e)}
    try:
        h["evidence_runtime"] = ER.health()
    except Exception as e:
        h["evidence_runtime"] = {"error": str(e)}
    try:
        from app.database import (DecisionObservation, DecisionOutcome,
                                  InstrumentQuoteSample, get_db)
        with get_db() as db:
            h["observations"] = db.query(DecisionObservation).filter(
                DecisionObservation.engine_epoch == h["evidence_epoch"]).count()
            h["quote_samples"] = db.query(InstrumentQuoteSample).count()
            h["outcome_rows"] = db.query(DecisionOutcome).count()
    except Exception as e:
        h["counts_error"] = str(e)
    return h


def main() -> None:                       # pragma: no cover - daemon entry
    from lib import evidence_runtime as ER
    from lib import market_data_runtime as MDR
    from lib import runtime_mode as RM
    from app.database import init_db

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not RM.is_evidence_only():
        raise SystemExit(
            f"REFUSING TO START: runtime mode is {RM.current_mode()}. This "
            f"daemon exists to collect evidence without touching the book; "
            f"running it in a mode that permits economic mutation would "
            f"defeat the only guarantee it makes.")

    started = _now()
    _state["started_at"] = started.isoformat()

    # CAMPAIGN IDENTITY IS DURABLE, NOT REGENERATED PER PROCESS.
    # This used to be os.environ.setdefault(..., epoch_name(now)), which
    # minted a brand-new epoch and activation boundary on EVERY restart —
    # it already did so three times — silently shattering one prospective
    # dataset into fragments that each looked healthy alone.
    from lib import evidence_campaign as EC
    camp = EC.get_or_create(os.environ["JARVIS_DB_PATH"], started=started)
    epoch = camp["epoch"]
    os.environ["JARVIS_EVIDENCE_EPOCH"] = epoch
    os.environ["JARVIS_EVIDENCE_BOUNDARY"] = camp["boundary_at"]

    logger.info("=" * 66)
    logger.info("JARVIS EVIDENCE COLLECTOR — decide for real, execute never")
    logger.info("  runtime mode : %s", RM.current_mode())
    logger.info("  database     : %s", os.getenv("JARVIS_DB_PATH"))
    logger.info("  epoch        : %s", epoch)
    logger.info("  boundary     : signals at/after %s", camp["boundary_at"])
    logger.info("  campaign     : %s", "CREATED" if camp["created"] else "CONTINUING")
    logger.info("  scan every   : %.0fs", CANDIDATE_INTERVAL_S)
    logger.info("=" * 66)

    init_db()
    logger.info("[EvidenceCollector] market data: %s", MDR.start())
    logger.info("[EvidenceCollector] evidence runtime: %s", ER.start())

    def _shutdown(signum, _frame):
        logger.info("[EvidenceCollector] signal %s — stopping cleanly", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # EACH GENERATOR OWNS ITS OWN THREAD.
    #
    # The first version ran both inline in this loop and it starved the one
    # that matters most: the market refresh warms an OHLCV cache across ~157
    # symbols and took over twenty minutes to return, so `_generate_signals`
    # — the only step that calls the LLM — was never entered at all. The
    # collector looked healthy, quote evidence accumulated, and the GPU sat
    # idle because a slow sibling held the loop.
    #
    # Independent threads mean a slow provider delays only its own cadence.
    def _periodic(name, fn, interval):
        def _loop():
            while not _stop.is_set():
                fn()
                _stop.wait(interval)
        t = threading.Thread(target=_loop, name=name, daemon=True)
        t.start()
        return t

    _threads[STAGE_MARKET] = _periodic("evidence-market", _refresh_market,
                                       MARKET_INTERVAL_S)
    _threads[STAGE_SIGNAL] = _periodic("evidence-signals", _generate_signals,
                                       SIGNAL_INTERVAL_S)

    try:
        while not _stop.is_set():
            r = _scan_once()
            logger.info("[EvidenceCollector] scan #%d %s", _state["scans"],
                        {k: v for k, v in r.items() if k != "at"})
            _stop.wait(CANDIDATE_INTERVAL_S)
    finally:
        # ORDER MATTERS, AND SO DOES WAITING FOR THE PRODUCERS FIRST.
        # Tearing down the runtimes while a worker is still mid-refresh
        # would pull resources out from under it. Joins are bounded: a
        # worker that will not exit is REPORTED, never waited on forever
        # and never silently treated as clean.
        _stop.set()
        for stage, t in list(_threads.items()):
            if t is None or not t.is_alive():
                continue
            t.join(timeout=WORKER_JOIN_TIMEOUT_S)
            if t.is_alive():
                with _stage_lock:
                    st = dict(_stages.get(stage, {}))
                logger.warning(
                    "[EvidenceCollector] worker %s did not exit within %.0fs "
                    "(in_progress=%s, started_at=%s) — shutdown is DEGRADED",
                    stage, WORKER_JOIN_TIMEOUT_S,
                    st.get("in_progress"), st.get("started_at"))
            else:
                logger.info("[EvidenceCollector] worker %s exited", stage)

        logger.info("[EvidenceCollector] evidence runtime: %s", ER.stop())
        logger.info("[EvidenceCollector] market data: %s", MDR.stop())
        logger.info("[EvidenceCollector] stopped cleanly")


if __name__ == "__main__":                # pragma: no cover
    main()
