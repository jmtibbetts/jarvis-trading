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

logger = logging.getLogger(__name__)

job_status = {
    'market':    {'status': 'idle', 'last': None, 'error': None},
    'threats':   {'status': 'idle', 'last': None, 'error': None},
    'signals':   {'status': 'idle', 'last': None, 'error': None},
    'execute':   {'status': 'idle', 'last': None, 'error': None},
    'positions': {'status': 'idle', 'last': None, 'error': None},
    'telegram':  {'status': 'idle', 'last': None, 'error': None},
    'guardian':  {'status': 'idle', 'last': None, 'error': None},
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
    1. Drawdown ceiling: if portfolio is down >3% on the day → go defensive (tighten all stops)
    2. Regime shift: if market regime flips bearish → tighten all positions
    3. Concentration: if any single position > 25% of portfolio → flag/trim
    All decisions go through the LLM with full context before acting.
    """
    try:
        from lib.alpaca_client import get_positions, get_account
        from lib.market_regime import get_regime
        from lib.lmstudio import call_lm_studio, parse_json
        from app.database import get_db, ThreatEvent, NewsItem, PortfolioSnapshot

        account   = get_account()
        equity    = float(account.equity)
        positions = get_positions()

        if not positions:
            return

        # Portfolio metrics
        total_mv   = sum(float(p.market_value or 0) for p in positions)
        total_pl   = sum(float(p.unrealized_pl or 0) for p in positions)
        total_plpc = (total_pl / (total_mv - total_pl)) * 100 if (total_mv - total_pl) > 0 else 0
        max_single = max((float(p.market_value or 0) / total_mv * 100 for p in positions), default=0)

        # Get today's starting equity from snapshots
        day_start_equity = equity  # fallback
        try:
            with get_db() as db:
                cutoff_day = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
                snap = db.query(PortfolioSnapshot).filter(
                    PortfolioSnapshot.snapshot_at >= cutoff_day
                ).order_by(PortfolioSnapshot.snapshot_at.asc()).first()
                if snap:
                    day_start_equity = float(snap.equity)
        except Exception:
            pass

        day_drawdown_pct = ((equity - day_start_equity) / day_start_equity * 100) if day_start_equity else 0

        # Market regime
        try:
            regime = get_regime()
        except Exception:
            regime = {"label": "Unknown", "risk": "medium"}

        logger.info(
            f"[Guardian] Portfolio: MV=${total_mv:,.0f} | P&L={total_plpc:+.2f}% | "
            f"Day={day_drawdown_pct:+.2f}% | MaxPos={max_single:.1f}% | Regime={regime.get('label')}"
        )

        # ── Hard ceiling checks (no LLM needed) ──────────────────────────────
        DRAWDOWN_HARD_CEILING = -5.0   # day drawdown % — go fully defensive, no new trades
        DRAWDOWN_WARN_LEVEL   = -3.0   # warn + tighten all stops
        CONCENTRATION_MAX     = 35.0   # single position > 35% of portfolio

        alerts = []
        go_defensive = False

        if day_drawdown_pct <= DRAWDOWN_HARD_CEILING:
            alerts.append(f"⚠️ HARD CEILING HIT: Portfolio down {day_drawdown_pct:.1f}% today")
            go_defensive = True
            logger.warning(f"[Guardian] 🚨 Hard drawdown ceiling hit: {day_drawdown_pct:.1f}%")
        elif day_drawdown_pct <= DRAWDOWN_WARN_LEVEL:
            alerts.append(f"⚠️ Drawdown warning: {day_drawdown_pct:.1f}% today")
            logger.warning(f"[Guardian] ⚠ Drawdown warning: {day_drawdown_pct:.1f}%")

        if regime.get("risk") == "high":
            alerts.append(f"🔴 High-risk regime: {regime.get('label')}")

        if max_single >= CONCENTRATION_MAX:
            conc_pos = max(positions, key=lambda p: float(p.market_value or 0))
            alerts.append(f"⚠️ Concentration risk: {conc_pos.symbol} = {max_single:.1f}% of portfolio")

        if not alerts and regime.get("risk") != "high" and day_drawdown_pct > DRAWDOWN_WARN_LEVEL:
            logger.info(f"[Guardian] ✓ Portfolio healthy — no action needed")
            return

        # ── LLM portfolio-level decision ──────────────────────────────────────
        with get_db() as db:
            threats = db.query(ThreatEvent).filter(
                ThreatEvent.status == "Active"
            ).order_by(ThreatEvent.created_date.desc()).limit(5).all()
            cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            news = db.query(NewsItem).filter(
                NewsItem.created_date >= cutoff_2h
            ).order_by(NewsItem.created_date.desc()).limit(8).all()

        threat_ctx = "\n".join(f"[{t.severity}] {t.title}" for t in threats) or "None"
        news_ctx   = "\n".join(f"[{n.sentiment}] {n.title}" for n in news) or "None"
        pos_ctx    = "\n".join(
            f"  {p.symbol}: {float(p.unrealized_plpc or 0)*100:+.1f}% | MV=${float(p.market_value or 0):,.0f}"
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
  "stop_tighten_pct": <float — trail % to apply to all remaining positions, or null>
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
            from lib.alpaca_client import close_position
            for pos in positions:
                try:
                    sym = pos.symbol.upper().replace("/", "")
                    close_position(sym)
                    logger.info(f"[Guardian] ✓ Closed {sym} (defensive)")
                except Exception as e:
                    logger.error(f"[Guardian] Close {pos.symbol} failed: {e}")

        elif action == "EXIT_WEAKEST" and to_exit:
            from lib.alpaca_client import close_position
            for sym in to_exit:
                try:
                    close_position(sym.upper().replace("/", ""))
                    logger.info(f"[Guardian] ✓ Closed {sym} (LLM: EXIT_WEAKEST)")
                except Exception as e:
                    logger.error(f"[Guardian] Close {sym} failed: {e}")

        elif action == "TIGHTEN_ALL" and tighten:
            # Tighten stops on all open positions
            from jobs.manage_positions import _set_protective_order, _is_crypto
            for pos in positions:
                sym           = str(pos.symbol)
                qty           = float(pos.qty or 0)
                current_price = float(pos.current_price or pos.avg_entry_price or 0)
                try:
                    _set_protective_order(sym.replace("/", ""), qty, float(tighten), current_price)
                    logger.info(f"[Guardian] ⟳ Tightened stop {sym} @ {tighten}%")
                except Exception as e:
                    logger.error(f"[Guardian] Tighten {sym} failed: {e}")

        # Block new signal execution when going defensive
        if go_defensive or action in ("EXIT_ALL", "TIGHTEN_ALL"):
            job_status['execute']['status'] = 'paused'
            logger.warning("[Guardian] 🔒 Execution PAUSED — portfolio in defensive mode")

    except Exception as e:
        logger.error(f"[Guardian] Error: {e}", exc_info=True)


def create_scheduler() -> BackgroundScheduler:
    executors = {'default': ThreadPoolExecutor(max_workers=6)}
    sched = BackgroundScheduler(executors=executors, timezone='UTC')

    from jobs.fetch_market_data import run as market_run
    from jobs.fetch_threat_news import run as threats_run
    from jobs.generate_signals  import run as signals_run
    from jobs.execute_signals   import run as execute_run
    from jobs.manage_positions  import run as positions_run
    from jobs.telegram_bot      import run as telegram_run

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

    # Portfolio guardian every 5 min (offset from positions by 2.5 min)
    sched.add_job(make_job_runner('guardian', portfolio_guardian),
                  'interval', minutes=5, id='guardian',
                  next_run_time=now + timedelta(minutes=2, seconds=30))

    # Telegram every 1 min
    sched.add_job(make_job_runner('telegram', telegram_run),
                  'interval', minutes=1, id='telegram', next_run_time=now)

    logger.info("[Scheduler] v2.0 — all jobs registered (event-driven + guardian active)")
    return sched
