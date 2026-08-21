"""The bounded wallet-intelligence cycle: observation to shadow verdict.

WHAT THIS IS. One controlled pathway that runs after each wallet poll and
carries the evidence the rest of the way:

    poll (already done by lib/wallet_poller)
      -> enrich the signatures the transfers feed could not explain
      -> rescore the wallets whose evidence actually changed
      -> collect prices for the exact mints that need one
      -> classify / reclassify the affected observations
      -> create eligible SHADOW theses, or named refusals
      -> resolve forward outcomes whose evidence is due
      -> refresh what the desk reads

WHAT THIS IS NOT. It is not a scheduler. It owns no timer, spawns no
thread and has no queue: `lib/wallet_poller` already has the only loop, and
this runs INSIDE that loop's pass. The legacy mixed scheduler stays off and
is not consulted. Nothing here places an order, signs anything, moves cash
or touches either virtual book.

EVERY STAGE IS BOUNDED AND EVERY STAGE IS OPTIONAL. A stage that fails
records its error and the next one still runs on whatever evidence exists —
the alternative is one unreachable provider stopping outcome resolution
that needs no provider at all. Counts are None when a stage did not look,
because MISSING IS NOT ZERO.

DELTAS, NOT REPLAYS. The cycle processes recent legs and explicitly pending
work rather than replaying 20,785 rows every fifteen minutes. Full
reprocessing stays available for a deliberate operator call.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CYCLE_VERSION = "wallet_intel_cycle_v1"

# Stage names, in order. These are what the desk shows as "current stage".
STAGES = ("ENRICH_SWAP_EVIDENCE", "BACKFILL_WALLET_HISTORY",
          "COLLECT_PRICE_SNAPSHOTS", "RESOLVE_WALLET_ALPHA",
          "RESCORE_AFFECTED_WALLETS", "APPLY_WALLET_LIFECYCLE",
          "PROCESS_SHADOW_EVENTS", "RESOLVE_OUTCOMES", "REFRESH_SUMMARIES")

#: How many holder observations may be tested against the ledger per pass.
MAX_PROMOTIONS = 200
#: How many observations may have their post-entry horizons resolved.
MAX_ALPHA_RESOLVED = 300

CYCLE_OK = "CYCLE_OK"
CYCLE_PARTIAL = "CYCLE_PARTIAL"
CYCLE_FAILED = "CYCLE_FAILED"
CYCLE_ALREADY_RUNNING = "CYCLE_ALREADY_RUNNING"
CYCLE_DISABLED = "CYCLE_DISABLED"

ENABLED_ENV = "JARVIS_WALLET_INTEL_CYCLE_ENABLED"
_TRUE = frozenset({"1", "true", "yes", "on", "enable", "enabled"})

#: How far back the incremental pass reads legs. Wide enough to cover every
#: signature enrichment is allowed to touch, narrow enough that a pass is
#: not a full replay.
PROCESS_WINDOW_SECONDS = 3 * 24 * 3600

#: Rescoring costs one provider call per wallet, so it is capped per cycle.
MAX_WALLETS_RESCORED = 12

# One watched wallet advances by one 25-signature page per cycle.  Each
# signature needs a transaction read, so this is intentionally much smaller
# than the transfer poll budget.  Progress is durable through the registry's
# oldest-signature cursor and eventually reaches the beginning of history.
HISTORY_WALLETS_PER_CYCLE = 1
HISTORY_PAGE_SIZE = 25

_RUN_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()

_STATE: dict = {
    "cycles_completed": 0,
    "cycles_failed": 0,
    "cycles_refused_overlapping": 0,
    "running": False,
    "current_stage": None,
    "last_started_at": None,
    "last_completed_at": None,
    "last_result": None,
    "last_duration_seconds": None,
    "last_error": None,
    "stages": {},
    # Counts from the last pass. None means the stage did not look.
    "signatures_considered": None,
    "signatures_enriched": None,
    "signatures_answered": None,
    "signatures_refused_non_trading": None,
    "signatures_partial": None,
    "enrichment_failures": None,
    "enrichment_calls": None,
    "history_wallets_attempted": None,
    "history_records_loaded": None,
    "history_swaps_stored": None,
    "history_backfills_completed": None,
    "entries_promoted": None,
    "alpha_horizons_filled": None,
    "alpha_awaiting_price": None,
    "wallets_rescored": None,
    "wallets_promoted": None,
    "wallets_demoted": None,
    "price_snapshots": None,
    "mints_considered": None,
    "events_processed": None,
    "events_reclassified": None,
    "events_superseded": None,
    "theses_created": None,
    "outcomes_resolved": None,
    "outcomes_unresolved": None,
}


def cycle_enabled() -> bool:
    """ON by default, and independent of the legacy scheduler.

    The cycle is the completion of a poll that is already running; a
    deployment that wants observation without interpretation can turn it
    off here without touching polling.
    """
    raw = os.getenv(ENABLED_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() in _TRUE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return None if dt is None else dt.isoformat()


def _set(**kw) -> None:
    with _STATE_LOCK:
        _STATE.update(kw)


def _stage(name: str) -> None:
    _set(current_stage=name)


# -- The pass -------------------------------------------------------------
def run_once(*, full: bool = False, enrich: bool = True, score: bool = True,
             prices: bool = True, max_wallets: int | None = None) -> dict:
    """One bounded pass. REFUSES to overlap another. NEVER RAISES.

    `full=True` reprocesses every stored leg instead of the recent window —
    an operator action for after a classifier change, not the automatic
    behaviour.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            _STATE["cycles_refused_overlapping"] += 1
        logger.info("[IntelCycle] refused — a cycle is already running")
        return {"ok": False, "result": CYCLE_ALREADY_RUNNING,
                "detail": ("a cycle is already in flight; a slow pass must "
                           "not start a second one")}

    started_dt = _now()
    started = time.time()
    _set(running=True, last_started_at=_iso(started_dt), last_error=None,
         current_stage=STAGES[0], stages={})

    out: dict = {"version": CYCLE_VERSION, "started_at": _iso(started_dt),
                 "stages": {}, "errors": []}
    stages: dict = {}

    def _run_stage(name, fn, *, skipped_reason=None):
        """One stage. Its failure is ITS failure, not the cycle's."""
        _stage(name)
        if skipped_reason:
            stages[name] = {"state": "SKIPPED", "reason": skipped_reason}
            return None
        t0 = time.time()
        try:
            result = fn()
            stages[name] = {"state": "OK", "seconds": round(time.time() - t0, 2),
                            "result": result}
            return result
        except Exception as e:                               # noqa: BLE001
            logger.error("[IntelCycle] %s failed: %s", name, e, exc_info=True)
            detail = f"{type(e).__name__}: {str(e)[:200]}"
            stages[name] = {"state": "FAILED",
                            "seconds": round(time.time() - t0, 2),
                            "error": detail}
            out["errors"].append(f"{name}: {detail}")
            return None

    try:
        # A. + B. + C. + D. Swap-grade evidence for the bounded set of
        #    signatures whose classification the transfers feed could not
        #    settle. Collection itself already happened in the poll.
        enrichment = _run_stage(
            "ENRICH_SWAP_EVIDENCE", _enrich,
            skipped_reason=None if enrich else "disabled by caller")

        # The scoring model needs complete economic lifecycles, not the
        # newest transfer page.  Advance one pinned wallet's durable history
        # cursor every cycle; the resulting wallet_trades ledger is what the
        # scoring stage consumes.
        history = _run_stage(
            "BACKFILL_WALLET_HISTORY", _backfill_history,
            skipped_reason=None if score else "disabled by caller")

        # Prices must be current BEFORE alpha resolution and scoring.  The
        # former ordering refreshed SOL after scoring and left every
        # SOL-quoted round trip one cycle behind its own evidence.
        pricing = _run_stage(
            "COLLECT_PRICE_SNAPSHOTS", _collect_prices,
            skipped_reason=None if prices else "disabled by caller")

        # E-bis. THE HONEST BOOTSTRAP, and the answer to the circular
        #    failure. A wallet needs evidence to be scored; requiring a
        #    JARVIS thesis for that evidence would mean no wallet is ever
        #    scored and no thesis is ever justified. It is not required:
        #    `lib/wallet_alpha` measures what the TOKEN did after the
        #    wallet entered, from market data alone. That is the wallet's
        #    own population, entirely separate from how a JARVIS thesis
        #    derived from it performs.
        alpha = _run_stage(
            "RESOLVE_WALLET_ALPHA", _resolve_alpha,
            skipped_reason=None if score else "disabled by caller")

        # E. Only the wallets whose evidence CHANGED. Rescoring 1,086
        #    registry rows every fifteen minutes would be a provider bill
        #    for an answer nobody asked for.
        scoring = _run_stage(
            "RESCORE_AFFECTED_WALLETS",
            lambda: _rescore(started_dt, max_wallets=max_wallets),
            skipped_reason=None if score else "disabled by caller")

        # Scoring measures; lifecycle decides.  The legacy scheduler used to
        # run this decision later, but that scheduler is intentionally off.
        # Without this stage a newly proven candidate never enters WATCH and
        # therefore is never polled for the evidence needed by shadow picks.
        lifecycle = _run_stage(
            "APPLY_WALLET_LIFECYCLE", _apply_lifecycle,
            skipped_reason=None if score else "disabled by caller")

        # G. + H. Classify, cluster, gate, persist. Idempotent on cluster_id.
        processing = _run_stage(
            "PROCESS_SHADOW_EVENTS", lambda: _process(full=full))

        # I. + J. Checkpoints whose due time has passed AND whose price
        #    exists. UNRESOLVED is never a loss.
        outcomes = _run_stage("RESOLVE_OUTCOMES", _resolve)

        # K. Source-isolated performance, recomputed on read.
        summaries = _run_stage("REFRESH_SUMMARIES", _summaries)

    finally:
        _RUN_LOCK.release()

    duration = round(time.time() - started, 2)
    failed = [k for k, v in stages.items() if v.get("state") == "FAILED"]
    result = (CYCLE_FAILED if len(failed) == len(STAGES)
              else CYCLE_PARTIAL if failed else CYCLE_OK)

    counts = {
        "signatures_considered": (enrichment or {}).get("considered"),
        "signatures_enriched": (enrichment or {}).get("enriched"),
        # AN ANSWER IS NOT A FAILURE. "40 considered, 0 enriched" reads as a
        # broken pass; what actually happened on the live store is that all
        # 47 signatures resolved to NOT a trade — value arrived and nothing
        # was paid — which is a finding, and the whole reason the stage
        # exists. Reporting only the trades would make the most common true
        # outcome invisible.
        "signatures_answered": (
            None if not enrichment else
            (enrichment.get("enriched", 0)
             + enrichment.get("refused_non_trading", 0)
             + enrichment.get("partial", 0))),
        "signatures_refused_non_trading": (
            (enrichment or {}).get("refused_non_trading")),
        "signatures_partial": (enrichment or {}).get("partial"),
        "enrichment_failures": (enrichment or {}).get("failures"),
        "enrichment_calls": (enrichment or {}).get("provider_calls"),
        "history_wallets_attempted": (history or {}).get("wallets_attempted"),
        "history_records_loaded": (history or {}).get("records_loaded"),
        "history_swaps_stored": (history or {}).get("swaps"),
        "history_backfills_completed": (history or {}).get("completed"),
        "entries_promoted": (alpha or {}).get("promoted"),
        "alpha_horizons_filled": (alpha or {}).get("horizons_filled"),
        "alpha_awaiting_price": (alpha or {}).get("awaiting_price"),
        "wallets_rescored": (scoring or {}).get("scored"),
        "wallets_attempted": (scoring or {}).get("attempted"),
        "wallets_promoted": (lifecycle or {}).get("promoted"),
        "wallets_demoted": (lifecycle or {}).get("demoted"),
        "price_snapshots": (pricing or {}).get("snapshots"),
        "mints_considered": (pricing or {}).get("requested"),
        "events_processed": (processing or {}).get("clusters"),
        "events_reclassified": (processing or {}).get("reclassified"),
        "events_superseded": (processing or {}).get("superseded"),
        "theses_created": (processing or {}).get("eligible"),
        "outcomes_resolved": (outcomes or {}).get("resolved"),
        "outcomes_unresolved": (outcomes or {}).get("still_unresolved"),
    }

    completed = _now()
    with _STATE_LOCK:
        _STATE.update({
            "running": False, "current_stage": None,
            "last_completed_at": _iso(completed),
            "last_duration_seconds": duration,
            "last_result": result,
            "last_error": "; ".join(out["errors"])[:400] or None,
            "stages": stages,
            **counts,
        })
        if result == CYCLE_FAILED:
            _STATE["cycles_failed"] += 1
        else:
            _STATE["cycles_completed"] += 1

    out.update({"ok": result != CYCLE_FAILED, "result": result,
                "completed_at": _iso(completed), "duration_seconds": duration,
                "stages": stages, "summary": summaries, **counts})
    return out


# -- Stages ---------------------------------------------------------------
def _enrich() -> dict:
    from lib import wallet_swap_enrichment as E
    return E.enrich_pending()


def _backfill_history() -> dict:
    """Advance durable balance-delta history for pinned wallets.

    The previous cycle called the full-transaction decoder only for recent
    transfer observations, while scoring fetched one shallow transfer page.
    That combination can never close a position whose acquisition is older
    than the page.  This stage makes bounded, restart-safe progress through
    the canonical ``wallet_swaps`` cursor instead.
    """
    from sqlalchemy import text

    from app.database import engine, get_db
    from lib import wallet_swaps

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT address, history_oldest_signature, "
            "COALESCE(history_backfill_complete,0) "
            "FROM wallet_registry "
            "WHERE pinned=1 "
            "  AND status NOT IN ('EXCLUDED_ENTITY','ARCHIVED') "
            "  AND COALESCE(history_backfill_complete,0)=0 "
            "ORDER BY COALESCE(last_deep_backfill_at,'') ASC, address "
            "LIMIT :lim"), {"lim": HISTORY_WALLETS_PER_CYCLE}).fetchall()

    out = {"wallets_attempted": 0, "records_loaded": 0, "swaps": 0,
           "not_trades": 0, "unvalued": 0, "completed": 0,
           "page_size": HISTORY_PAGE_SIZE}
    for address, oldest, _complete in rows:
        out["wallets_attempted"] += 1
        # Establish the newest/oldest cursor on the first pass; subsequent
        # passes continue backward from the durable oldest signature.
        deep = bool(oldest)
        with get_db() as db:
            result = wallet_swaps.sync_wallet_history(
                address, session=db, max_pages=1,
                page_size=HISTORY_PAGE_SIZE, deep=deep)
        out["records_loaded"] += int(result.get("inspected") or 0)
        out["swaps"] += int(result.get("swaps") or 0)
        out["not_trades"] += int(result.get("not_trades") or 0)
        out["unvalued"] += int(result.get("unvalued") or 0)
        if not result.get("error"):
            with engine.connect() as conn:
                complete = conn.execute(text(
                    "SELECT COALESCE(history_backfill_complete,0) "
                    "FROM wallet_registry WHERE address=:a"),
                    {"a": address}).scalar()
            out["completed"] += int(bool(complete))
    return out


def _rescore(started_dt, *, max_wallets=None) -> dict:
    """Rescore the wallets whose evidence moved, and nothing else.

    THREE POPULATIONS, all bounded:
      1. wallets whose signatures were enriched in THIS pass
      2. watched wallets that have never carried a usable score
      3. wallets holding an outcome that just resolved

    Note what is NOT here: every registry row. A wallet nobody observed has
    no new evidence, so rescoring it would spend a provider call to write
    the same answer.
    """
    from sqlalchemy import text

    from app.database import engine
    from lib import wallet_scoring

    cap = int(max_wallets if max_wallets is not None else MAX_WALLETS_RESCORED)
    since = _iso(started_dt - timedelta(seconds=60))
    addresses: list = []
    seen: set = set()

    def add(addr):
        if addr and addr not in seen:
            seen.add(addr)
            addresses.append(addr)

    with engine.connect() as conn:
        # A bounded history pass changes the durable ledger even when the
        # wallet was previously labelled NO_VERIFIED_TRADES/INSUFFICIENT.
        # Such labels describe the old evidence and must not permanently
        # exclude the wallet from rescoring.
        for (addr,) in conn.execute(text(
                "SELECT address FROM wallet_registry "
                "WHERE last_history_sync_at >= :s"), {"s": since}).fetchall():
            add(addr)
        for (addr,) in conn.execute(text(
                "SELECT DISTINCT wallet_address FROM wallet_swap_enrichment "
                "WHERE updated_at >= :s"), {"s": since}).fetchall():
            add(addr)
        if len(addresses) < cap:
            # A watched wallet with no usable score is the binding
            # constraint on every thesis this desk could produce, so it is
            # worth a call even when nothing of its moved this pass.
            for (addr,) in conn.execute(text(
                    "SELECT address FROM wallet_registry "
                    "WHERE status NOT IN ('EXCLUDED_ENTITY', 'ARCHIVED') "
                    "  AND (measurable IS NULL OR measurable = 0) "
                    "  AND (analysis_status IS NULL "
                    "       OR analysis_status NOT IN "
                    "          ('NO_VERIFIED_TRADES', 'INSUFFICIENT')) "
                    "ORDER BY COALESCE(last_analysis_at, '') ASC "
                    "LIMIT :lim"), {"lim": cap}).fetchall():
                add(addr)

    if not addresses:
        return {"attempted": 0, "scored": 0, "requested": 0,
                "reason": "no wallet's evidence changed this pass"}
    return wallet_scoring.score_wallets(addresses[:cap], limit=cap)


def _resolve_alpha() -> dict:
    """Promote proven acquisitions, then fill their elapsed horizons.

    TWO POPULATIONS, KEPT APART (and this is the stage that proves it):

      A. THE WALLET'S OWN entries and what the token did next. Measured
         here, from market data, with no reference to whether JARVIS ever
         formed a view. This is what a wallet score may learn from.

      B. JARVIS SHADOW THESES derived from those wallets, measured in
         `wallet_shadow_outcomes` and never fed back into a wallet score.

    A HOLDER_SNAPSHOT says "owns 500,000 of this token" and nothing about
    when or at what price, so it is not evidence about timing. Promotion
    only happens when the LEDGER proves the acquisition — which is why the
    enrichment stage above lands `wallet_trades` rows from the same fetch.
    An unpromoted observation keeps its weaker class and stays ineligible.
    """
    from app.database import WalletObservation, get_db
    from lib import wallet_alpha, wallet_swaps

    out = {"examined": 0, "promoted": 0, "refused": 0,
           "horizons_filled": 0, "completed": 0, "awaiting_price": 0,
           "refusal_reasons": {}}

    with get_db() as db:
        rows = (db.query(WalletObservation)
                .filter(WalletObservation.alpha_eligible == 0)
                .order_by(WalletObservation.observed_at.desc())
                .limit(MAX_PROMOTIONS).all())
        out["examined"] = len(rows)
        for obs in rows:
            try:
                r = wallet_swaps.promote_holder_to_verified_entry(db, obs)
            except Exception as e:                           # noqa: BLE001
                logger.debug("[IntelCycle] promotion failed: %s", e)
                continue
            if r.get("promoted"):
                out["promoted"] += 1
            else:
                out["refused"] += 1
                why = str(r.get("reason") or "unstated")[:80]
                out["refusal_reasons"][why] = \
                    out["refusal_reasons"].get(why, 0) + 1

    resolved = wallet_alpha.resolve_due(limit=MAX_ALPHA_RESOLVED)
    out.update({k: resolved.get(k, out.get(k))
                for k in ("horizons_filled", "completed", "awaiting_price")})
    out["alpha_examined"] = resolved.get("examined")
    # Only the top few reasons; the whole distribution is noise on a page.
    out["refusal_reasons"] = dict(sorted(out["refusal_reasons"].items(),
                                         key=lambda kv: -kv[1])[:4])
    return out


def _apply_lifecycle() -> dict:
    """Apply the existing, pure-evidence promotion rules.

    This is deliberately a separate stage from scoring: arithmetic still
    cannot promote a wallet as a side effect.  It merely gives the lifecycle
    engine a production caller while the mixed scheduler remains disabled.

    THE TRANSITION LIST IS REDACTED HERE, and it has to be. Every stage
    result is stored in `_STATE["stages"]` and served verbatim by
    `/api/onchain/intel/cycle`, and `wallet_lifecycle.run` reports
    `{"address": w.address}` — the FULL address. The leak is dormant only
    while nothing transitions, which is precisely the condition this stage
    exists to end: the first promotion would publish real wallet addresses
    to the desk and to anyone reading the API.
    """
    from lib import wallet_lifecycle
    from lib.wallet_shadow_intel import safe_label

    out = dict(wallet_lifecycle.run(limit=200))
    out["transitions"] = [
        {"wallet": safe_label(t.get("address")),
         "from": t.get("from"), "to": t.get("to"),
         "reasons": t.get("reasons")}
        for t in (out.get("transitions") or [])
    ]
    return out


def _collect_prices() -> dict:
    """Quote assets FIRST, then the exact mints.

    Order matters: a wallet score is computed in USD, and every SOL-quoted
    round trip is unpriceable until the SOL series is current. Refreshing it
    after the mints would leave scoring a cycle behind its own evidence.
    """
    from lib import wallet_price_snapshots as P

    quotes = P.refresh_quote_series()
    mints = P.collect()
    return {**mints, "quote_series": quotes}


def _process(*, full: bool) -> dict:
    from lib import wallet_shadow_intel as W

    if full:
        return W.process()
    since = (_now() - timedelta(seconds=PROCESS_WINDOW_SECONDS)).timestamp()
    return W.process(since_ts=since)


def _resolve() -> dict:
    from lib import wallet_shadow_intel as W
    return W.resolve_outcomes()


def _summaries() -> dict:
    from lib import wallet_shadow_intel as W
    p = W.performance()
    return {"eligible": p.get("eligible"), "refused": p.get("refused"),
            "observations": p.get("observations")}


# -- What the desk reads --------------------------------------------------
def status() -> dict:
    """Cycle state. Counts are None when a pass did not look."""
    from lib import wallet_poller

    with _STATE_LOCK:
        s = dict(_STATE)
        s["stages"] = {k: dict(v) for k, v in (s.get("stages") or {}).items()}

    poll = {}
    try:
        poll = wallet_poller.status() or {}
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[IntelCycle] poller status unavailable: %s", e)

    s.update({
        "enabled": cycle_enabled(),
        "version": CYCLE_VERSION,
        "stage_order": list(STAGES),
        # The cycle has no timer of its own: it runs at the END of a poll,
        # so the next cycle is the next poll.
        "next_cycle_at": poll.get("next_run_at"),
        "interval_seconds": poll.get("interval_seconds"),
        "driven_by": "wallet_poller",
        "transfers_collected": poll.get("inserted"),
        "transfers_observed": poll.get("observed"),
        "process_window_seconds": PROCESS_WINDOW_SECONDS,
        "max_wallets_rescored": MAX_WALLETS_RESCORED,
        "history_wallets_per_cycle": HISTORY_WALLETS_PER_CYCLE,
        "history_page_size": HISTORY_PAGE_SIZE,
        "note": ("one bounded pass per wallet poll. It reads chain and "
                 "market data and writes shadow intelligence only: no "
                 "order, no signing, no cash movement, and no scheduler. "
                 "A None count means that stage did not look — missing is "
                 "not zero"),
    })
    return s


def _reset_for_tests() -> None:
    with _STATE_LOCK:
        for k in list(_STATE):
            if isinstance(_STATE[k], int) and not isinstance(_STATE[k], bool):
                _STATE[k] = 0
            elif k == "stages":
                _STATE[k] = {}
            else:
                _STATE[k] = None
        _STATE["running"] = False
