"""
APScheduler-based job scheduler v2.1
- Event-driven signal generation: fires immediately when new threats/news arrive
- Portfolio drawdown ceiling: checked every 5 min, goes defensive if breached
- Cross-position regime shift detection: tightens all crypto/equity if regime flips
- News sentiment per-symbol scoring fed into position manager
- Signal generation is aware of current positions (no duplicate buys, adds to winners)
"""
import logging, threading
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
import concurrent.futures as _cf

class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """APScheduler executor that uses daemon threads — exits cleanly on Ctrl+C."""
    def _create_executor(self, max_workers):
        # Override to force daemon=True on all worker threads so Python's
        # atexit/threading._shutdown doesn't hang waiting for them on exit.
        executor = _cf.ThreadPoolExecutor(max_workers=max_workers,
                                          thread_name_prefix='apscheduler')
        # Patch existing threads created before our override takes effect
        for t in executor._threads:
            t.daemon = True
        # Patch the initializer to make future threads daemon
        _orig_init = executor._initializer
        def _daemon_init(*args, **kwargs):
            import threading
            threading.current_thread().daemon = True
            if _orig_init:
                _orig_init(*args, **kwargs)
        executor._initializer = _daemon_init
        return executor

logger = logging.getLogger(__name__)

job_status = {
    'market':    {'status': 'idle', 'last': None, 'error': None},
    'threats':   {'status': 'idle', 'last': None, 'error': None},
    'signals':   {'status': 'idle', 'last': None, 'error': None},
    'execute':   {'status': 'idle', 'last': None, 'error': None},
    'positions': {'status': 'idle', 'last': None, 'error': None},
    'telegram':  {'status': 'idle', 'last': None, 'error': None},
    'guardian':  {'status': 'idle', 'last': None, 'error': None},
    'paper':     {'status': 'idle', 'last': None, 'error': None},
    # Each scanner mode gets its own key — they run on independent schedules
    # (cron for pre_market/intraday, interval for crypto/futures) and can
    # legitimately overlap; sharing one 'scanner' key let one mode's "running"
    # status mask another's and silently drop its run via the runner's
    # already-running guard.
    'scanner_premarket': {'status': 'idle', 'last': None, 'error': None},
    'scanner_intraday':  {'status': 'idle', 'last': None, 'error': None},
    'scanner_crypto':    {'status': 'idle', 'last': None, 'error': None},
    'scanner_futures':   {'status': 'idle', 'last': None, 'error': None},
    'evaluation':{'status': 'idle', 'last': None, 'error': None},
    'autosim':   {'status': 'idle', 'last': None, 'error': None},
    'insider':   {'status': 'idle', 'last': None, 'error': None},
    'inst13f':   {'status': 'idle', 'last': None, 'error': None},
    'congress':  {'status': 'idle', 'last': None, 'error': None},
    'senate':    {'status': 'idle', 'last': None, 'error': None},
    'ipo':       {'status': 'idle', 'last': None, 'error': None},
    'postmortem':{'status': 'idle', 'last': None, 'error': None},
    'crypto_derivatives': {'status': 'idle', 'last': None, 'error': None},
    'candidates': {'status': 'idle', 'last': None, 'error': None},
    'wallet_discovery': {'status': 'idle', 'last': None, 'error': None},
    'kraken_sync': {'status': 'idle', 'last': None, 'error': None},
    'feature_snapshots': {'status': 'idle', 'last': None, 'error': None},
    'feature_labels': {'status': 'idle', 'last': None, 'error': None},
    'official_data': {'status': 'idle', 'last': None, 'error': None},
    'futures_curve': {'status': 'idle', 'last': None, 'error': None},
    'brief_push': {'status': 'idle', 'last': None, 'error': None},
    'onchain': {'status': 'idle', 'last': None, 'error': None},
    'wallet_activity': {'status': 'idle', 'last': None, 'error': None},
}

# Guards the check-then-set on job_status[name]['status'] below so two threads
# racing to start the same job (e.g. the 30-min execute interval and the
# event-driven Timer-delayed execute call) can't both observe 'idle' and both
# proceed to run.
_job_status_lock = threading.Lock()

# ── Event bus: news/threat jobs signal here when new items arrive ──────────────
_event_lock   = threading.Lock()
_pending_event = threading.Event()   # set when new threats/news arrived
_last_event_signals = None           # ISO timestamp of last event-driven signal run


def notify_new_intelligence():
    """Called by fetch_threat_news and fetch_market_data when fresh items are saved."""
    with _event_lock:
        _pending_event.set()
    logger.info("[Scheduler] 📡 New intelligence event — signal generation queued")


def _broadcast_job_status(name: str):
    """Push the current job_status snapshot to any connected /next dashboard
    clients. Best-effort — the new frontend also polls on load, so a missed
    broadcast (no clients connected, or called before the loop is bound) just
    means that one update arrives on the next poll instead of instantly."""
    try:
        from app.ws import manager
        manager.broadcast_from_thread("job_status", {name: job_status[name]})
    except Exception:
        pass


def _persist_job_status(name: str):
    """Persist this job's last run to the api_cache table. The in-memory
    job_status dict resets to 'never' on every restart, which made the Ops
    page look like automation was dead (user restarts frequently and asked
    why jobs 'never run'). One merged row keyed per job survives restarts."""
    try:
        from lib.api_cache import put_cached
        put_cached(f"jobstatus:{name}", {
            "last": job_status[name].get("last"),
            "status": job_status[name].get("status"),
            "error": job_status[name].get("error"),
        })
    except Exception as e:
        logger.debug(f"[Scheduler] status persist failed for {name}: {e}")


def load_persisted_job_status():
    """Seed the in-memory job_status with last-run history from previous
    server lifetimes, so 'last=never' means genuinely never — not 'since
    the last restart'."""
    try:
        from app.database import get_db, ApiCacheEntry
        import json as _json
        with get_db() as db:
            rows = db.query(ApiCacheEntry).filter(ApiCacheEntry.key.like("jobstatus:%")).all()
            for row in rows:
                name = row.key.split(":", 1)[1]
                if name in job_status:
                    saved = _json.loads(row.payload)
                    if saved.get("last"):
                        job_status[name]["last"] = saved["last"]
                    # a run 'in flight' when the old process died is not running now
                    if saved.get("status") and saved["status"] != "running":
                        job_status[name]["status"] = saved["status"]
                        job_status[name]["error"] = saved.get("error")
    except Exception as e:
        logger.debug(f"[Scheduler] status restore failed: {e}")


def make_job_runner(name: str, fn):
    # Self-seeding, because the alternative already failed: the 'candidates'
    # job was registered without a row in the job_status dict above, so this
    # runner's first line raised KeyError on EVERY firing and the job
    # silently never ran — while looking merely "not yet fired" from the
    # outside. Registration and seeding must not be two separate manual
    # steps that can disagree.
    job_status.setdefault(name, {'status': 'idle', 'last': None, 'error': None})

    def runner():
        with _job_status_lock:
            if job_status[name]['status'] == 'running':
                logger.info(f"[Scheduler] {name} already running — skipping")
                return
            job_status[name]['status'] = 'running'
            job_status[name]['error'] = None
        _broadcast_job_status(name)
        try:
            fn()
            job_status[name]['last'] = datetime.now(timezone.utc).isoformat()
            job_status[name]['status'] = 'ok'
        except Exception as e:
            logger.error(f"[Scheduler] {name} error: {e}", exc_info=True)
            job_status[name]['status'] = 'error'
            job_status[name]['error'] = str(e)
        _broadcast_job_status(name)
        _persist_job_status(name)
    return runner


def event_driven_signals():
    """
    Fires signal generation immediately when new threats/news arrive.
    Debounced: won't fire more than once per 10 minutes regardless of event volume.
    """
    global _last_event_signals
    if not _pending_event.is_set():
        return
    now = datetime.now(timezone.utc)
    with _event_lock:
        if _last_event_signals:
            elapsed = (now - datetime.fromisoformat(_last_event_signals)).total_seconds()
            if elapsed < 600:  # 10 min debounce
                logger.debug(f"[Scheduler] Event signals debounced ({elapsed:.0f}s < 600s)")
                return
        _pending_event.clear()
        _last_event_signals = now.isoformat()

    logger.info("[Scheduler] ⚡ Event-driven signal generation triggered by new intelligence")
    if job_status['signals']['status'] == 'running':
        logger.info("[Scheduler] Signals already running — event will retry next check")
        _pending_event.set()  # re-arm
        return

    from jobs.generate_signals import run as signals_run
    make_job_runner('signals', signals_run)()

    # Also fire execute right after to catch any new signals.
    # This is a best-effort early check — make_job_runner's lock is the
    # authoritative guard against racing the regular 30-min execute interval.
    if job_status['execute']['status'] != 'running':
        from jobs.execute_signals import run as execute_run
        timer = threading.Timer(15.0, make_job_runner('execute', execute_run))
        timer.daemon = True
        timer.start()


def portfolio_guardian():
    """
    Portfolio-level risk checks every 5 minutes:
    1. Drawdown ceiling: if portfolio is down >5% on the day → go defensive
    2. Regime shift: if market regime flips bearish → tighten all positions
    3. Concentration: if any single position > 35% of portfolio → flag/trim

    All DB queries eagerly converted to plain dicts INSIDE their session blocks.
    No SQLAlchemy ORM object ever leaves a with get_db() context.
    """
    try:
        from lib.alpaca_client import get_positions, get_account
        from lib.market_regime import get_regime
        from lib.lmstudio import call_lm_studio, parse_json
        from app.routes import log_decision
        from app.database import get_db, ThreatEvent, NewsItem, PortfolioSnapshot

        # ── 1. Alpaca live data (SDK objects, NOT ORM — safe to use freely) ──────
        account   = get_account()
        equity    = float(account.equity)
        raw_positions = get_positions()

        if not raw_positions:
            logger.info("[Guardian] No open positions — skipping")
            log_decision("guardian", "IDLE", "No open positions — guardian standing by", thinking=False)
            return

        # Convert alpaca-py Position SDK objects to plain dicts immediately
        # so there is zero ambiguity about what type they are downstream
        positions = [
            {
                "symbol":          str(p.symbol),
                "qty":             float(p.qty or 0),
                "market_value":    float(p.market_value or 0),
                "unrealized_pl":   float(p.unrealized_pl or 0),
                "unrealized_plpc": float(p.unrealized_plpc or 0),
                "avg_entry_price": float(p.avg_entry_price or 0),
                "current_price":   float(p.current_price or 0),
            }
            for p in raw_positions
        ]

        # Portfolio metrics (all from plain dicts — no ORM)
        total_mv   = sum(p["market_value"] for p in positions)
        total_pl   = sum(p["unrealized_pl"] for p in positions)
        total_plpc = (total_pl / (total_mv - total_pl)) * 100 if (total_mv - total_pl) > 0 else 0
        max_single = max((p["market_value"] / total_mv * 100 for p in positions), default=0)

        # ── 2. DB snapshot query — extract scalar immediately, no ORM outside block ─
        day_start_equity = equity  # fallback if no snapshot
        try:
            with get_db() as db:
                cutoff_day = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
                snap = db.query(PortfolioSnapshot).filter(
                    PortfolioSnapshot.snapshot_at >= cutoff_day
                ).order_by(PortfolioSnapshot.snapshot_at.asc()).first()
                # Extract float INSIDE the session — snap must not leave the block
                if snap is not None:
                    day_start_equity = float(snap.equity)
        except Exception as snap_err:
            logger.debug(f"[Guardian] Snapshot lookup failed: {snap_err}")

        day_drawdown_pct = ((equity - day_start_equity) / day_start_equity * 100) if day_start_equity else 0

        # ── 3. Market regime ────────────────────────────────────────────────────
        try:
            regime = get_regime()
        except Exception:
            regime = {"label": "Unknown", "risk": "medium"}

        logger.info(
            f"[Guardian] Portfolio: MV=${total_mv:,.0f} | P&L={total_plpc:+.2f}% | "
            f"Day={day_drawdown_pct:+.2f}% | MaxPos={max_single:.1f}% | Regime={regime.get('label')}"
        )

        # ── 4. Threshold checks ─────────────────────────────────────────────────
        DRAWDOWN_HARD_CEILING = -5.0
        DRAWDOWN_WARN_LEVEL   = -3.0
        CONCENTRATION_MAX     = 35.0

        alerts       = []
        go_defensive = False

        if day_drawdown_pct <= DRAWDOWN_HARD_CEILING:
            alerts.append(f"⚠️ HARD CEILING HIT: Portfolio down {day_drawdown_pct:.1f}% today")
            go_defensive = True
            logger.warning(f"[Guardian] 🚨 Hard drawdown ceiling: {day_drawdown_pct:.1f}%")
            log_decision("guardian", "EXIT_ALL", f"Hard drawdown ceiling hit: {day_drawdown_pct:.1f}% day loss", thinking=False)
        elif day_drawdown_pct <= DRAWDOWN_WARN_LEVEL:
            alerts.append(f"⚠️ Drawdown warning: {day_drawdown_pct:.1f}% today")
            logger.warning(f"[Guardian] ⚠ Drawdown warning: {day_drawdown_pct:.1f}%")

        if regime.get("risk") == "high":
            alerts.append(f"🔴 High-risk regime: {regime.get('label')}")

        if max_single >= CONCENTRATION_MAX:
            conc = max(positions, key=lambda p: p["market_value"])
            alerts.append(f"⚠️ Concentration risk: {conc['symbol']} = {max_single:.1f}% of portfolio")

        if not alerts and regime.get("risk") != "high" and day_drawdown_pct > DRAWDOWN_WARN_LEVEL:
            logger.info("[Guardian] ✓ Portfolio healthy — no action needed")
            log_decision("guardian", "HOLD", "Portfolio healthy — no action needed", thinking=False)
            return

        # ── 5. DB context for LLM — all converted to dicts inside session ─────
        with get_db() as db:
            cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            threats = [
                {"severity": t.severity or "Unknown", "title": t.title or ""}
                for t in db.query(ThreatEvent).filter(
                    ThreatEvent.status == "Active"
                ).order_by(ThreatEvent.created_date.desc()).limit(5).all()
            ]
            news = [
                {"sentiment": n.sentiment or "neutral", "title": n.title or ""}
                for n in db.query(NewsItem).filter(
                    NewsItem.created_date >= cutoff_2h
                ).order_by(NewsItem.created_date.desc()).limit(8).all()
            ]

        # ── 6. Build prompt context (all plain dicts — no ORM anywhere) ────────
        threat_ctx = "\n".join(f"[{t['severity']}] {t['title']}" for t in threats) or "None"
        news_ctx   = "\n".join(f"[{n['sentiment']}] {n['title']}" for n in news) or "None"
        pos_ctx    = "\n".join(
            f"  {p['symbol']}: {p['unrealized_plpc'] * 100:+.1f}% | MV=${p['market_value']:,.0f}"
            for p in positions
        )

        prompt = f"""You are a portfolio risk manager. Evaluate the portfolio-level situation and decide what to do.

PORTFOLIO STATUS:
  Total Market Value: ${total_mv:,.0f}
  Unrealized P&L:     {total_plpc:+.2f}%
  Day Drawdown:       {day_drawdown_pct:+.2f}%
  Max Single Position: {max_single:.1f}%
  Market Regime:      {regime.get('label')} (risk={regime.get('risk')})

OPEN POSITIONS:
{pos_ctx}

ALERTS TRIGGERED:
{chr(10).join(alerts)}

ACTIVE THREATS:
{threat_ctx}

RECENT NEWS (2h):
{news_ctx}

Decide the portfolio-level action. Consider:
- Are the drawdown/regime concerns temporary or structural?
- Should we exit specific positions, tighten all stops, or hold?
- Are the losses correlated (macro) or idiosyncratic (position-specific)?

Respond ONLY with valid JSON:
{{
  "action": "HOLD" | "TIGHTEN_ALL" | "EXIT_WEAKEST" | "EXIT_ALL",
  "reason": "2-3 sentence explanation",
  "symbols_to_exit": ["SYM1", "SYM2"],
  "stop_tighten_pct": <float between 0.5 and 5.0 — trail % to apply to all remaining positions, or null>
}}"""

        try:
            from lib import llm_router as llm
            raw      = llm.call(prompt, task="risk_guardian", mode=llm.DEEP,
                                system="You are a precise portfolio risk manager. Respond only with JSON.",
                                max_tokens=300)
            decision = parse_json(raw)
            if not isinstance(decision, dict):
                logger.warning(f"[Guardian] LLM returned unparseable response (len={len(raw)}) — defaulting to HOLD")
                decision = {"action": "HOLD", "reason": "LLM parse failed"}
        except Exception as e:
            logger.warning(f"[Guardian] LLM failed: {e} — defaulting to HOLD")
            decision = {"action": "HOLD", "reason": "LLM unavailable"}

        action  = decision.get("action", "HOLD")
        reason  = decision.get("reason", "")
        to_exit = decision.get("symbols_to_exit") or []
        tighten = decision.get("stop_tighten_pct")

        logger.info(f"[Guardian] 🤖 Decision: {action} | {reason}")
        log_decision("guardian", action, reason, thinking=True)

        if action in ("EXIT_ALL",) or go_defensive:
            # Hard ceiling hit — close everything
            from lib.alpaca_client import close_position, cancel_open_orders_for_symbol
            for pos in positions:
                try:
                    raw_sym  = pos["symbol"]
                    sym      = raw_sym.upper().replace("/", "")
                    mv       = abs(float(pos.get("market_value") or 0))
                    # Skip dust positions — notional < $1 will always error
                    if mv < 1.0:
                        logger.warning(f"[Guardian] Skipping {sym} — dust position (MV=${mv:.4f})")
                        continue
                    cancel_open_orders_for_symbol(sym)
                    close_position(sym)
                    logger.info(f"[Guardian] ✓ Closed {sym} (defensive)")
                    log_decision("guardian", "EXIT", f"Defensive close: {reason}", symbol=sym, thinking=False)
                except Exception as e:
                    logger.error(f"[Guardian] Close {pos['symbol']} failed: {e}")

        elif action == "EXIT_WEAKEST" and to_exit:
            from lib.alpaca_client import close_position, cancel_open_orders_for_symbol
            # Build a lookup for market value by symbol
            mv_lookup = {p["symbol"].upper().replace("/", ""): abs(float(p.get("market_value") or 0))
                         for p in positions}
            for sym in to_exit:
                try:
                    clean_sym = sym.upper().replace("/", "")
                    mv = mv_lookup.get(clean_sym, 999)
                    if mv < 1.0:
                        logger.warning(f"[Guardian] Skipping {clean_sym} — dust position (MV=${mv:.4f})")
                        continue
                    cancel_open_orders_for_symbol(clean_sym)
                    close_position(clean_sym)
                    logger.info(f"[Guardian] ✓ Closed {sym} (LLM: EXIT_WEAKEST)")
                    log_decision("guardian", "EXIT_WEAKEST", reason, symbol=sym, thinking=False)
                except Exception as e:
                    logger.error(f"[Guardian] Close {sym} failed: {e}")

        elif action == "TIGHTEN_ALL" and tighten:
            # Clamp stop_tighten_pct: equity trailing stops need >= 0.1%, sane range 0.5-15%
            tighten_pct = max(0.5, min(float(tighten), 15.0))
            if tighten_pct != float(tighten):
                logger.info(f"[Guardian] stop_tighten_pct clamped {tighten} → {tighten_pct}%")
            # Tighten stops on all open positions
            from jobs.manage_positions import _set_protective_order, _is_crypto
            for pos in positions:
                sym           = pos["symbol"]
                qty           = pos["qty"]
                current_price = pos["current_price"] or pos["avg_entry_price"]
                try:
                    _set_protective_order(sym.replace("/", ""), qty, tighten_pct, current_price)
                    logger.info(f"[Guardian] ⟳ Tightened stop {sym} @ {tighten_pct}%")
                    log_decision("guardian", "TIGHTEN_STOP", f"{reason} — tightened to {tighten_pct}%", symbol=sym, thinking=False)
                except Exception as e:
                    logger.error(f"[Guardian] Tighten {sym} failed: {e}")

        # Block new signal execution when going defensive
        if go_defensive or action in ("EXIT_ALL", "TIGHTEN_ALL"):
            job_status['execute']['status'] = 'paused'
            logger.warning("[Guardian] 🔒 Execution PAUSED — portfolio in defensive mode")

    except Exception as e:
        logger.error(f"[Guardian] Error: {e}", exc_info=True)


def create_scheduler() -> BackgroundScheduler:
    load_persisted_job_status()  # 'last run' survives restarts — see docstring
    executors = {'default': _DaemonThreadPoolExecutor(max_workers=10)}
    # job_defaults are the fix for "jobs only run when triggered manually":
    # APScheduler's DEFAULT misfire_grace_time is 1 second — if a job's fire
    # moment passes while all workers are busy (LLM batches, 13F/congress
    # downloads regularly hold threads for minutes), the run is silently
    # skipped as a misfire. With ~20 registered jobs and 6 workers that
    # happened constantly. Now: late jobs run late (up to 10 min) instead of
    # not at all, coalesce collapses a backlog into one run, and the pool is
    # larger.
    sched = BackgroundScheduler(
        executors=executors, timezone='UTC',
        job_defaults={'misfire_grace_time': 600, 'coalesce': True},
    )

    from jobs.fetch_market_data import run as market_run
    from jobs.fetch_threat_news import run as threats_run
    from jobs.generate_signals  import run as signals_run
    from jobs.execute_signals   import run as execute_run
    from jobs.manage_positions  import run as positions_run
    from jobs.telegram_bot      import run as telegram_run
    from jobs.paper_trading     import run as paper_run
    from jobs.scan_opportunities import run as scanner_run
    from jobs.evaluate_signals import run as evaluation_run
    from jobs.auto_simulator import run as autosim_run
    from jobs.fetch_insider_activity import run as insider_run
    from jobs.fetch_13f_filings import run as inst13f_run
    from jobs.fetch_congress_trades import run as congress_run
    from jobs.fetch_ipo_filings import run as ipo_run
    from jobs.collect_postmortems import run as postmortem_run
    from jobs.fetch_crypto_derivatives import run as crypto_derivatives_run

    now = datetime.now(timezone.utc)

    # ── STARTUP SEQUENCE ───────────────────────────────────────────────────────
    # Phase 1 — Data ingestion (runs immediately): market data + threats/news
    #           These populate the DB/cache so the LLM has real data to work with.
    # Phase 2 — LLM tasks (wait 3 min): signals, execute, guardian
    #           By T+3min both market and threat jobs have finished their first run,
    #           the OHLCV cache is warm, news/threats are in DB.
    # Phase 3 — Housekeeping (staggered): positions, paper, telegram
    #
    # This prevents the model hitting its token budget on an empty DB while also
    # fighting for threads with 6 other jobs all starting simultaneously.
    # ──────────────────────────────────────────────────────────────────────────

    # ── PHASE 1: Data pipeline — fires immediately ─────────────────────────────
    # Market data every 15 min
    sched.add_job(make_job_runner('market', market_run),
                  'interval', minutes=15, id='market', next_run_time=now)

    # Threat/news every 15 min
    sched.add_job(make_job_runner('threats', threats_run),
                  'interval', minutes=15, id='threats',
                  next_run_time=now + timedelta(seconds=5))  # 5s after market

    # ── PHASE 2: LLM tasks — wait 3 min for data pipeline to complete ─────────
    # Signal generation every 30 min — first run at T+3min
    sched.add_job(make_job_runner('signals', signals_run),
                  'interval', minutes=30, id='signals',
                  next_run_time=now + timedelta(minutes=3))

    # Execute every 30 min — first run at T+4min (signals need to be saved first)
    sched.add_job(make_job_runner('execute', execute_run),
                  'interval', minutes=30, id='execute',
                  next_run_time=now + timedelta(minutes=4))

    # Portfolio guardian every 5 min — first run at T+3.5min
    sched.add_job(make_job_runner('guardian', portfolio_guardian),
                  'interval', minutes=5, id='guardian',
                  next_run_time=now + timedelta(minutes=3, seconds=30))

    # Event-driven signal check every 2 min — arms after T+3min so events
    # during the data pipeline don't fire the LLM before cache is warm
    sched.add_job(event_driven_signals,
                  'interval', minutes=2, id='event_signals',
                  next_run_time=now + timedelta(minutes=3))

    # ── PHASE 3: Housekeeping — staggered starts ───────────────────────────────
    # Position management every 5 min — first run at T+30s (non-LLM, just Alpaca)
    sched.add_job(make_job_runner('positions', positions_run),
                  'interval', minutes=5, id='positions',
                  next_run_time=now + timedelta(seconds=30))

    # Paper trading every 15 min — first run at T+5min
    sched.add_job(make_job_runner('paper', paper_run),
                  'interval', minutes=15, id='paper_trading',
                  next_run_time=now + timedelta(minutes=5),
                  replace_existing=True, max_instances=1, misfire_grace_time=180)

    # Forward-only signal evaluation reads cached bars and never places orders.
    sched.add_job(make_job_runner('evaluation', evaluation_run),
                  'interval', minutes=15, id='signal_evaluation',
                  next_run_time=now + timedelta(minutes=6),
                  replace_existing=True, max_instances=1)

    # Separate paper-only ledger: follows every eligible signal, never a broker.
    sched.add_job(make_job_runner('autosim', autosim_run),
                  'interval', minutes=1, id='auto_simulator',
                  next_run_time=now + timedelta(seconds=10),
                  replace_existing=True, max_instances=1)

    # Telegram every 1 min — fires immediately (no LLM, just polls)
    sched.add_job(make_job_runner('telegram', telegram_run),
                  'interval', minutes=1, id='telegram', next_run_time=now)

    # SEC Form 4 insider activity every 30 min — free EDGAR API, no LLM.
    # Filings must be submitted within 2 business days of the transaction, so
    # this cadence is plenty fresh without leaning on SEC's fair-access limits.
    sched.add_job(make_job_runner('insider', insider_run),
                  'interval', minutes=30, id='insider',
                  next_run_time=now + timedelta(minutes=2),
                  replace_existing=True, max_instances=1)

    # SEC Form 13F institutional holdings every 2 hours — free EDGAR API, no LLM.
    # 13F is quarterly data with a 45-day filing deadline, so there is nothing to
    # gain from polling faster. The interval also paces CUSIP->ticker resolution:
    # OpenFIGI's keyless tier caps how many new CUSIPs each run can map, and
    # unfinished filings are reprocessed next run until fully resolved.
    sched.add_job(make_job_runner('inst13f', inst13f_run),
                  'interval', hours=2, id='inst13f',
                  next_run_time=now + timedelta(minutes=5),
                  replace_existing=True, max_instances=1)

    # House STOCK Act trade disclosures every 6 hours — free Clerk of the House
    # data, no LLM. Disclosure is delayed up to 45 days by statute, so there is
    # nothing to gain from polling faster; the interval also paces the per-filing
    # PDF downloads, which are bounded per run and resume next run.
    sched.add_job(make_job_runner('congress', congress_run),
                  'interval', hours=2, id='congress',
                  next_run_time=now + timedelta(minutes=8),
                  replace_existing=True, max_instances=1)

    # The Senate half, same cadence and the same table with chamber="Senate".
    # Offset from the House run so two disclosure scrapers are not competing
    # for the same window. Cheaper per filing — eFD publishes an HTML table
    # rather than the House's scanned PDFs — so the per-run bound is higher.
    from jobs.fetch_senate_trades import run as senate_run
    sched.add_job(make_job_runner('senate', senate_run),
                  'interval', hours=2, id='senate',
                  next_run_time=now + timedelta(minutes=11),
                  replace_existing=True, max_instances=1)

    # IPO registration pipeline every 4 hours — free EDGAR feeds, no LLM.
    # Registration filings arrive at business pace; the interval also bounds
    # the multi-MB 424B4 cover downloads (already capped per run).
    sched.add_job(make_job_runner('ipo', ipo_run),
                  'interval', hours=4, id='ipo',
                  next_run_time=now + timedelta(minutes=10),
                  replace_existing=True, max_instances=1)

    # Failure postmortems every 30 min — pure DB sweep classifying terminal
    # signals into the failure taxonomy that feeds the scoring penalty.
    sched.add_job(make_job_runner('postmortem', postmortem_run),
                  'interval', minutes=30, id='postmortem',
                  next_run_time=now + timedelta(minutes=4),
                  replace_existing=True, max_instances=1)

    # Crypto derivatives (funding/OI/long-short ratio/liquidations) every 10 min —
    # free OKX public REST, no LLM. 10 min balances freshness against OKX's rate
    # limits across a 5-symbol watchlist x 4 endpoints per run.
    sched.add_job(make_job_runner('crypto_derivatives', crypto_derivatives_run),
                  'interval', minutes=10, id='crypto_derivatives',
                  next_run_time=now + timedelta(minutes=2, seconds=30),
                  replace_existing=True, max_instances=1)

    # Counterfactual resolution: what would have happened to the candidates
    # the filters rejected. Local bar walk over the OHLCV cache — no LLM, no
    # network. 30 min because a candidate needs forward bars to judge anyway;
    # resolving more eagerly would only find "too young".
    # Autonomous wallet discovery. Ranked by volume/transaction
    # ACCELERATION rather than size, so it looks at tokens that just woke
    # up — where wallets that were early are still visible — instead of at
    # whichever pair is permanently largest.
    #
    # 20 minutes because that is roughly the useful life of the signal: a
    # surge worth investigating is hours old at most, and each pass costs
    # 2 GeckoTerminal calls plus 2 Helius RPC calls per token.
    def wallet_discovery_run():
        from lib.wallet_discovery import discover_from_tokens
        return discover_from_tokens(max_tokens=5)
    sched.add_job(make_job_runner('wallet_discovery', wallet_discovery_run),
                  'interval', minutes=20, id='wallet_discovery',
                  next_run_time=now + timedelta(minutes=3),
                  replace_existing=True, max_instances=1)

    def candidates_run():
        from lib.candidates import relink_orphan_signals, resolve_pending
        # Relink first. A live signal whose candidate row points at a
        # superseded predecessor reads UNMEASURED on its card even though the
        # gate judged that exact setup — 48 of 138 active signals were in
        # that state on 2026-08-15. record_candidate now re-points as it
        # writes, so this is the sweeper for anything that slipped through
        # (a setup only re-enters that path if the scanner sees the very
        # same levels again, and prices move).
        relinked = relink_orphan_signals(limit=2000)
        resolved = resolve_pending(limit=500)
        return {"relinked": relinked, "resolved": resolved}
    sched.add_job(make_job_runner('candidates', candidates_run),
                  'interval', minutes=30, id='candidates',
                  next_run_time=now + timedelta(minutes=6),
                  replace_existing=True, max_instances=1)

    # The operator's REAL Kraken fills, synced read-only. Ground truth the
    # execution model trains against; also the real portfolio the Positions
    # view was blind to while it showed only Alpaca's sliver. Uses only the
    # read scopes the operator granted — no order placement exists here.
    def kraken_sync_run():
        from lib.kraken_sync import sync_trades
        return sync_trades()
    sched.add_job(make_job_runner('kraken_sync', kraken_sync_run),
                  'interval', minutes=30, id='kraken_sync',
                  next_run_time=now + timedelta(minutes=4),
                  replace_existing=True, max_instances=1)

    # Clock-driven feature snapshots + independent-horizon labels (P4).
    # The cadence matches the 15m bar: each pass snapshots any Tier-1
    # symbol whose newest bar hasn't been captured yet, and resolves every
    # label whose own horizon has elapsed. Selection bias is removed at
    # capture time — this corpus exists whether or not anything was
    # "interesting", which is exactly what makes it trainable.
    def feature_snapshots_run():
        from lib.feature_snapshots import run_clock_snapshots
        return run_clock_snapshots()
    sched.add_job(make_job_runner('feature_snapshots', feature_snapshots_run),
                  'interval', minutes=15, id='feature_snapshots',
                  next_run_time=now + timedelta(minutes=5),
                  replace_existing=True, max_instances=1)

    def feature_labels_run():
        from lib.feature_snapshots import resolve_due_labels
        return resolve_due_labels()
    sched.add_job(make_job_runner('feature_labels', feature_labels_run),
                  'interval', minutes=15, id='feature_labels',
                  next_run_time=now + timedelta(minutes=8),
                  replace_existing=True, max_instances=1)

    # Official releases (4A): COT weekly, FINRA short volume daily, EIA
    # weekly. Six-hourly polling of slow sources is deliberate slack — the
    # dedup key makes every overlap idempotent, and a release delayed by a
    # holiday is caught within hours without any schedule cleverness.
    def official_data_run():
        from lib.official_data import sync_all
        return sync_all()
    # The Brief meets the operator at breakfast: 13:00 UTC = 6am Pacific.
    # Same build_brief() the UI serves — the push cannot disagree with
    # the page.
    def brief_push_run():
        from jobs.push_brief import run as push_brief
        return push_brief()
    sched.add_job(make_job_runner('brief_push', brief_push_run),
                  'cron', hour=13, minute=0, id='brief_push',
                  timezone='UTC', replace_existing=True, max_instances=1)

    # On-chain fundamentals are DAILY by definition (Coin Metrics
    # publishes at 1d frequency), so polling faster would manufacture
    # copies of one observation rather than information.
    def onchain_run():
        from lib.onchain import sync as onchain_sync
        return onchain_sync()
    sched.add_job(make_job_runner('onchain', onchain_run),
                  'cron', hour=2, minute=30, id='onchain', timezone='UTC',
                  replace_existing=True, max_instances=1)

    sched.add_job(make_job_runner('official_data', official_data_run),
                  'interval', hours=6, id='official_data',
                  next_run_time=now + timedelta(minutes=10),
                  replace_existing=True, max_instances=1)

    # Solana wallet flow (Helius). POLLED, not pushed — the webhook design
    # this replaced needed an internet-facing mailbox host, and the most
    # secure version of an exposed service is not running one. Inert
    # without HELIUS_API_KEY and a non-empty HELIUS_WATCH_WALLETS, so
    # registering it unconditionally costs an unconfigured desk nothing.
    #
    # 15 minutes because dedup_key makes an overlapping re-poll free: the
    # window deliberately overlaps rather than tracking a cursor that
    # could drift and skip.
    def wallet_activity_run():
        from lib.wallet_activity import collect_once
        return collect_once()
    sched.add_job(make_job_runner('wallet_activity', wallet_activity_run),
                  'interval', minutes=15, id='wallet_activity',
                  next_run_time=now + timedelta(minutes=3),
                  replace_existing=True, max_instances=1)

    # Futures term structure (4B): one snapshot per root per 4h bucket.
    # The dedup bucket makes restarts harmless; front_code in every
    # snapshot is the roll-provenance record.
    def futures_curve_run():
        from lib.futures_curve import sync_curves
        return sync_curves()
    sched.add_job(make_job_runner('futures_curve', futures_curve_run),
                  'interval', hours=4, id='futures_curve',
                  next_run_time=now + timedelta(minutes=12),
                  replace_existing=True, max_instances=1)


    # ── OPPORTUNITY SCANNER ────────────────────────────────────────────────────
    # Each mode uses its own job_status key (see job_status dict above) since
    # these schedules can legitimately overlap.
    # Pre-market scan at 6:30 AM PT (13:30 UTC) every weekday
    sched.add_job(make_job_runner('scanner_premarket', lambda: scanner_run('pre_market')),
                  'cron', day_of_week='mon-fri', hour=13, minute=30,
                  id='scanner_premarket', timezone='UTC')

    # Intraday scalp scan every 15 min during market hours (Mon-Fri 13:30-20:00 UTC)
    sched.add_job(make_job_runner('scanner_intraday', lambda: scanner_run('intraday')),
                  'cron', day_of_week='mon-fri', hour='13-19', minute='0,15,30,45',
                  id='scanner_intraday', timezone='UTC')

    # Crypto scalp scan every 15 min, 24/7
    sched.add_job(make_job_runner('scanner_crypto', lambda: scanner_run('crypto')),
                  'interval', minutes=15, id='scanner_crypto',
                  next_run_time=now + timedelta(minutes=8))

    # Futures/forex scan every 4 hours, 24/7
    sched.add_job(make_job_runner('scanner_futures', lambda: scanner_run('futures')),
                  'interval', hours=4, id='scanner_futures',
                  next_run_time=now + timedelta(minutes=10))

    logger.info(
        "[Scheduler] v2.2 — startup sequenced: data pipeline T+0s → LLM tasks T+3min → "
        "execute T+4min → guardian T+3.5min | scanner: crypto T+8min / futures T+10min"
    )
    return sched




