"""One read-only background loop: Helius wallet intelligence, on a timer.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT. The wallet observation
pipeline already exists and is verified end to end — `wallet_activity.
collect_once()` polls the Helius Wallet Transfers API, parses it, and lands
idempotent observations. The only thing missing was something to call it
while JARVIS is running, because the legacy mixed scheduler that used to own
that job is disabled and must stay disabled: it also opens positions,
executes signals and manages the book.

So this is a loop and nothing else. It adds no provider client, no second
pipeline, no cursor, no persistence model, no inbound receiver, no webhook,
no queue. It calls ONE existing function.

INDEPENDENTLY GATED, AND OFF UNTIL SAID OTHERWISE.
`JARVIS_HELIUS_WALLET_POLLING_ENABLED` controls this loop ALONE — not the
scheduler, and the scheduler does not control it. Absent, empty, misspelled,
or set to anything outside an explicit true-token list, it stays OFF. A
truthy-by-accident value ("no", "false", "0", "disabled", "maybe") must
never start a provider loop, which is why this reads an ALLOWLIST rather
than calling `bool()` on a string — `bool("false")` is True.

WHY 15 MINUTES. That is the cadence the legacy scheduler already declared
for this exact job, so it is the interval this pipeline's pagination,
pacing and backfill budgets were chosen against. Cost per pass is bounded
by the existing limits, not by this file: 5 monitorable wallets at up to
`HELIUS_MAX_PAGES_PER_POLL` (default 5) is at most 25 provider calls, so
at most ~100/hour. Wallet flow is the SLOW shadow-only context layer — it
describes who is moving what, never when to enter — so latency does not
bind and a conservative interval costs nothing.

NON-OVERLAPPING BY CONSTRUCTION. One thread runs poll-then-sleep, so the
loop cannot lap itself; and a non-blocking lock refuses any concurrent
entry, so an operator-invoked pass during a slow scheduled one is REFUSED
rather than queued. A refusal is a result and is reported as one.

NO ADDRESS, KEY OR CREDENTIAL-BEARING URL LEAVES THIS MODULE. The upstream
error strings are formatted `"{address[:8]}…: {reason}"`, so even a failure
message carries a partial wallet. `_redact()` removes the producer's own
prefix and then scrubs anything else address-shaped, and a test injects a
known address to prove it.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

WALLET_POLLER_VERSION = "wallet_poller_v1"

POLLING_ENABLED_ENV = "JARVIS_HELIUS_WALLET_POLLING_ENABLED"
POLL_INTERVAL_ENV = "JARVIS_HELIUS_WALLET_POLL_INTERVAL_SECONDS"

#: The cadence the legacy scheduler already used for this job.
DEFAULT_INTERVAL_S = 900
#: Floor: below this the existing pacing and page budgets stop being
#: conservative for a provider this desk depends on for fee estimation too.
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 86_400

#: EXPLICIT true tokens. Anything else — including "false", "no" and "0",
#: all of which are truthy strings — leaves the loop OFF.
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on", "enable", "enabled"})

# Results.
POLL_DISABLED = "POLL_DISABLED"
POLL_ALREADY_RUNNING = "POLL_ALREADY_RUNNING"
POLL_OK = "POLL_OK"
POLL_SKIPPED = "POLL_SKIPPED"
POLL_FAILED = "POLL_FAILED"

_RUN_LOCK = threading.Lock()          # refuses overlap; never blocks
_STATE_LOCK = threading.Lock()        # guards the reported state
_STOP = threading.Event()
_THREAD: threading.Thread | None = None

_STATE: dict = {
    "running": False,
    "last_started_at": None,
    "last_completed_at": None,
    "last_result": None,
    "observed": None,
    "inserted": None,
    "deduplicated": None,
    "provider_calls": None,
    "last_error": None,
    "polls_completed": 0,
    "polls_refused_overlapping": 0,
    "polls_failed": 0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Configuration ────────────────────────────────────────────────────────
def polling_enabled() -> bool:
    """OFF unless explicitly and recognisably enabled.

    `bool(os.getenv(...))` is True for the string "false". This reads an
    allowlist so a typo, a stale value or a well-meant "no" cannot start a
    loop that talks to a paid provider.
    """
    import os

    raw = (os.getenv(POLLING_ENABLED_ENV) or "").strip().lower()
    return raw in _TRUE_TOKENS


def poll_interval_seconds() -> int:
    """Bounded, with a malformed value falling back rather than crashing."""
    import os

    raw = (os.getenv(POLL_INTERVAL_ENV) or "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("[WalletPoller] %s=%r is not a number — using %ss",
                       POLL_INTERVAL_ENV, raw, DEFAULT_INTERVAL_S)
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(value, MAX_INTERVAL_S))


# ── Redaction ────────────────────────────────────────────────────────────
# Base58 excludes 0, O, I and l, so this cannot match ordinary prose words
# containing them. Applied only to provider error text, never to the whole
# payload, so a long non-address token is not mangled for nothing.
_ADDRESSY = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{8,}\b")


def _redact(text) -> str | None:
    """Remove the wallet the producer put in front of its own message.

    `collect_once` formats failures as `"{address[:8]}…: {reason}"`, so even
    an error string carries eight characters of a real wallet. The prefix is
    removed deterministically, then anything still address-shaped is
    scrubbed — belt and braces, because the interesting part of an error is
    always the reason, never the account.
    """
    if text is None:
        return None
    s = str(text)
    if "…: " in s:
        s = s.split("…: ", 1)[1]
    s = _ADDRESSY.sub("<wallet>", s)
    return s[:400]


def _summarise_errors(result: dict) -> str | None:
    errors = result.get("errors") or []
    truncated = result.get("truncated_wallets") or []
    parts = []
    if errors:
        parts.append(f"{len(errors)} wallet error(s): "
                     + "; ".join(_redact(e) for e in errors[:3]))
    if truncated:
        parts.append(f"{len(truncated)} wallet(s) budget-truncated: "
                     + "; ".join(_redact(t) for t in truncated[:3]))
    return " | ".join(parts) if parts else None


# ── One pass ─────────────────────────────────────────────────────────────
def poll_once() -> dict:
    """One read-only collection pass. REFUSES to overlap another.

    Deliberately NOT gated on `polling_enabled()`: that flag governs whether
    JARVIS polls AUTOMATICALLY, and an explicitly invoked single read is not
    automatic polling. `start()` is the gated entry point.

    Calls `wallet_activity.collect_once()` and nothing else. That function
    never raises, but this is wrapped anyway — a context feed must not be
    able to take the desk down, and a loop that dies silently is worse than
    one that reports a failure.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            _STATE["polls_refused_overlapping"] += 1
        logger.info("[WalletPoller] pass refused — one is already running")
        return {"ok": False, "result": POLL_ALREADY_RUNNING,
                "detail": ("a collection pass is already in flight; a slow "
                           "poll must not start a second one")}

    started = _now()
    with _STATE_LOCK:
        _STATE["running"] = True
        _STATE["last_started_at"] = started

    try:
        from lib import wallet_activity

        raw = wallet_activity.collect_once()
    except Exception as e:                                   # noqa: BLE001
        logger.error("[WalletPoller] collection failed: %s", e, exc_info=True)
        with _STATE_LOCK:
            _STATE.update({
                "running": False, "last_completed_at": _now(),
                "last_result": POLL_FAILED,
                # A FAILURE IS NOT AN EMPTY COLLECTION. Leaving the counts
                # None rather than zeroing them is what keeps "we could not
                # look" distinguishable from "we looked and the chain was
                # quiet" — see the test of the same name.
                "observed": None, "inserted": None,
                "deduplicated": None, "provider_calls": None,
                "last_error": _redact(f"{type(e).__name__}: {e}"),
            })
            _STATE["polls_failed"] += 1
        return {"ok": False, "result": POLL_FAILED,
                "detail": _redact(f"{type(e).__name__}: {e}")}
    finally:
        _RUN_LOCK.release()

    skipped = raw.get("skipped")
    observed = raw.get("observations")
    inserted = raw.get("stored")
    deduped = raw.get("duplicates")
    calls = raw.get("pages_fetched")
    error = _summarise_errors(raw) or (_redact(skipped) if skipped else None)

    with _STATE_LOCK:
        _STATE.update({
            "running": False,
            "last_completed_at": _now(),
            "last_result": POLL_SKIPPED if skipped else POLL_OK,
            # A skipped pass observed nothing and did not look; None, not 0.
            "observed": None if skipped else observed,
            "inserted": None if skipped else inserted,
            "deduplicated": None if skipped else deduped,
            "provider_calls": None if skipped else calls,
            "last_error": error,
        })
        _STATE["polls_completed"] += 1

    return {
        "ok": not skipped,
        "result": POLL_SKIPPED if skipped else POLL_OK,
        "observed": None if skipped else observed,
        "inserted": None if skipped else inserted,
        "deduplicated": None if skipped else deduped,
        "provider_calls": None if skipped else calls,
        "wallets": raw.get("wallets"),
        "wallets_truncated": raw.get("wallets_truncated"),
        "detail": error,
    }


# ── The loop ─────────────────────────────────────────────────────────────
def _loop(interval_s: int) -> None:
    """Poll, then wait. Interruptible, so shutdown is immediate."""
    logger.info("[WalletPoller] started — every %ss", interval_s)
    # First pass immediately: a restart that waits a full interval before
    # looking is indistinguishable from one that never started.
    while not _STOP.is_set():
        try:
            poll_once()
        except Exception as e:                               # noqa: BLE001
            logger.error("[WalletPoller] loop iteration failed: %s", e)
        # Event.wait returns as soon as stop() is called, so shutdown does
        # not have to outlast the interval.
        _STOP.wait(interval_s)
    logger.info("[WalletPoller] stopped")


def start() -> dict:
    """Start the loop IF explicitly enabled. Idempotent.

    Independent of `JARVIS_DISABLE_SCHEDULER`: this neither reads it nor is
    read by it. The legacy scheduler stays off; this is one read-only feed.
    """
    global _THREAD

    if not polling_enabled():
        return {"started": False, "reason": POLL_DISABLED,
                "detail": (f"{POLLING_ENABLED_ENV} is not set to an explicit "
                           f"true value; wallet polling stays off")}
    if _THREAD is not None and _THREAD.is_alive():
        return {"started": False, "reason": "ALREADY_RUNNING",
                "interval_seconds": poll_interval_seconds()}

    interval = poll_interval_seconds()
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(interval,),
                               name="helius-wallet-poller", daemon=True)
    _THREAD.start()
    return {"started": True, "interval_seconds": interval,
            "version": WALLET_POLLER_VERSION}


def stop(timeout: float = 5.0) -> dict:
    """Signal the loop and wait briefly. Safe to call when not running."""
    global _THREAD

    _STOP.set()
    thread, _THREAD = _THREAD, None
    if thread is None:
        return {"stopped": False, "reason": "NOT_RUNNING"}
    thread.join(timeout=timeout)
    alive = thread.is_alive()
    if alive:
        # Daemon thread: it cannot outlive the process, and it holds no
        # economic lock — only an HTTP read.
        logger.warning("[WalletPoller] still finishing a pass at shutdown")
    return {"stopped": not alive, "still_finishing_a_pass": alive}


def is_running() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


# ── Truthful status ──────────────────────────────────────────────────────
def status() -> dict:
    """What this loop is doing — with no address, key or credential in it."""
    interval = poll_interval_seconds()
    with _STATE_LOCK:
        st = dict(_STATE)

    next_run = None
    if is_running():
        anchor = st["last_completed_at"] or st["last_started_at"]
        if anchor:
            try:
                nxt = (datetime.fromisoformat(anchor)
                       + timedelta(seconds=interval))
                next_run = nxt.isoformat()
            except ValueError:
                next_run = None
        else:
            next_run = "imminent"

    subsystem = {}
    try:
        from lib import wallet_activity
        s = wallet_activity.status()
        subsystem = {
            "helius_key_configured": s.get("has_key"),
            "wallets_watched": s.get("wallets_watched"),
            "population_source": s.get("population_source"),
            "parser": s.get("parser"),
        }
    except Exception as e:                                   # noqa: BLE001
        subsystem = {"unavailable": _redact(str(e))}

    return {
        "enabled": polling_enabled(),
        "running": is_running(),
        "poll_in_progress": st["running"],
        "interval_seconds": interval,
        "interval_default_seconds": DEFAULT_INTERVAL_S,
        "last_started_at": st["last_started_at"],
        "last_completed_at": st["last_completed_at"],
        "last_result": st["last_result"],
        "next_run_at": next_run,
        "observed": st["observed"],
        "inserted": st["inserted"],
        "deduplicated": st["deduplicated"],
        "provider_calls": st["provider_calls"],
        "last_error": st["last_error"],
        "polls_completed": st["polls_completed"],
        "polls_refused_overlapping": st["polls_refused_overlapping"],
        "polls_failed": st["polls_failed"],
        "subsystem": subsystem,
        "version": WALLET_POLLER_VERSION,
        "note": ("read-only wallet observation only. This loop places no "
                 "order, moves no cash, signs nothing and is independent of "
                 "JARVIS_DISABLE_SCHEDULER. Counts are None when a pass did "
                 "not look — missing is not zero"),
    }


def _reset_for_tests() -> None:
    """Test hook: forget accumulated state. Never called in production."""
    with _STATE_LOCK:
        _STATE.update({
            "running": False, "last_started_at": None,
            "last_completed_at": None, "last_result": None,
            "observed": None, "inserted": None, "deduplicated": None,
            "provider_calls": None, "last_error": None,
            "polls_completed": 0, "polls_refused_overlapping": 0,
            "polls_failed": 0,
        })
