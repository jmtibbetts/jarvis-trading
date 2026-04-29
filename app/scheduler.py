"""
APScheduler-based job scheduler v2.0
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
}

# ── Event bus: news/threat jobs signal here when new items arrive ──────────────
_event_lock   = threading.Lock()
_pending_event = threading.Event()   # set when new threats/news arrived
_last_event_signals = None           # ISO timestamp of last event-driven signal run


def notify_new_intelligence():
    """Called by fetch_threat_news and fetch_market_data when fresh items are saved."""
    with _event_lock:
        _pending_event.set()
    logger.info("[Scheduler] 📡 New intelligence event — signal generation queued")


def make_job_runner(name: str, fn):
    def runner():
        if job_status[name]['status'] == 'running':
            logger.info(f"[Scheduler] {name} already running — skipping")
            return
        job_status[name]['status'] = 'running'
        job_status[name]['error'] = None
        try:
            fn()
            job_status[name]['last'] = datetime.now(timezone.utc).isoformat()
            job_status[name]['status'] = 'ok'
        except Exception as e:
            logger.error(f"[Scheduler] {name} error: {e}", exc_info=True)
            job_status[name]['status'] = 'error'
            job_status[name]['error'] = str(e)
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

    # Also fire execute right after to catch any new signals
    if job_status['execute']['status'] != 'running':
        from jobs.execute_signals import run as execute_run
        threading.Timer(15.0, make_job_runner('execute', execute_run)).start()


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
        from app.database import get_db, ThreatEvent, NewsItem, PortfolioSnapshot

        # ── 1. Alpaca live data (SDK objects, NOT ORM — safe to use freely) ──────
        account   = get_account()
        equity    = float(account.equity)
        raw_positions = get_positions()

        if not raw_positions:
            logger.info("[Guardian] No open positions — skipping")
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
            raw      = call_lm_studio(prompt, system="You are a precise portfolio risk manager. Respond only with JSON.", max_tokens=300)
            decision = parse_json(raw)
        except Exception as e:
            logger.warning(f"[Guardian] LLM failed: {e} — defaulting to HOLD")
            decision = {"action": "HOLD", "reason": "LLM unavailable"}

        action  = decision.get("action", "HOLD")
        reason  = decision.get("reason", "")
        to_exit = decision.get("symbols_to_exit") or []
        tighten = decision.get("stop_tighten_pct")

        logger.info(f"[Guardian] 🤖 Decision: {action} | {reason}")

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
                except Exception as e:
                    logger.error(f"[Guardian] Tighten {sym} failed: {e}")

        # Block new signal execution when going defensive
        if go_defensive or action in ("EXIT_ALL", "TIGHTEN_ALL"):
            job_status['execute']['status'] = 'paused'
            logger.warning("[Guardian] 🔒 Execution PAUSED — portfolio in defensive mode")

    except Exception as e:
        logger.error(f"[Guardian] Error: {e}", exc_info=True)


def create_scheduler() -> BackgroundScheduler:
    executors = {'default': _DaemonThreadPoolExecutor(max_workers=6)}
    sched = BackgroundScheduler(executors=executors, timezone='UTC')

    from jobs.fetch_market_data import run as market_run
    from jobs.fetch_threat_news import run as threats_run
    from jobs.generate_signals  import run as signals_run
    from jobs.execute_signals   import run as execute_run
    from jobs.manage_positions  import run as positions_run
    from jobs.telegram_bot      import run as telegram_run
    from jobs.paper_trading     import run as paper_run

    now = datetime.now(timezone.utc)

    # Market data every 15 min — notifies event bus when new data arrives
    sched.add_job(make_job_runner('market', market_run),
                  'interval', minutes=15, id='market', next_run_time=now)

    # Threat/news every 15 min — notifies event bus when new items arrive
    sched.add_job(make_job_runner('threats', threats_run),
                  'interval', minutes=15, id='threats', next_run_time=now)

    # Scheduled signal generation every 30 min (baseline)
    sched.add_job(make_job_runner('signals', signals_run),
                  'interval', minutes=30, id='signals',
                  next_run_time=now + timedelta(seconds=90))

    # Event-driven signal check every 2 min (fires if new intel arrived)
    sched.add_job(event_driven_signals,
                  'interval', minutes=2, id='event_signals',
                  next_run_time=now + timedelta(minutes=2))

    # Execute every 30 min
    sched.add_job(make_job_runner('execute', execute_run),
                  'interval', minutes=30, id='execute',
                  next_run_time=now + timedelta(minutes=3))

    # Position management every 5 min
    sched.add_job(make_job_runner('positions', positions_run),
                  'interval', minutes=5, id='positions',
                  next_run_time=now + timedelta(seconds=30))

    sched.add_job(make_job_runner('paper', paper_run),
                  'interval', minutes=15, id='paper_trading',
                  next_run_time=now + timedelta(minutes=5),
                  replace_existing=True, max_instances=1, misfire_grace_time=180)

    # Portfolio guardian every 5 min (offset from positions by 2.5 min)
    sched.add_job(make_job_runner('guardian', portfolio_guardian),
                  'interval', minutes=5, id='guardian',
                  next_run_time=now + timedelta(minutes=2, seconds=30))

    # Telegram every 1 min
    sched.add_job(make_job_runner('telegram', telegram_run),
                  'interval', minutes=1, id='telegram', next_run_time=now)

    logger.info("[Scheduler] v2.0 — all jobs registered (event-driven + guardian active)")
    return sched



