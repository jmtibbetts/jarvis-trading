"""Platform & ops routes — Phase 7 split of app/routes.py. Bodies are
verbatim; shared helpers live in app.routers.common."""
from fastapi import APIRouter

from app.routers.common import *  # noqa: F401,F403
from app.routers.common import _build_provider_status, _config_dict, _position_dict, _telegram_setup_credentials, _ui_build  # noqa: E501

router = APIRouter()


@router.get("/health")
def health():
    # Version is served here so a running instance can be identified without
    # guessing from git — run.ps1 -Status and any deploy check can read it.
    from app.version import VERSION
    return {"status": "ok", "version": VERSION,
            "ui_build": _ui_build(),
            "time": datetime.now(timezone.utc).isoformat()}


@router.get("/mcp/status")
def get_mcp_status():
    """Connectivity/auth status of the configured MCP servers — the same
    ones the user runs in LM Studio, driven directly by Jarvis because
    LM Studio's MCP config never reaches API callers. Servers reporting
    needs_key_env light up once the key lands in .env."""
    from lib.mcp_client import available_servers
    return {
        "servers": available_servers(),
        "note": (
            "Jarvis speaks MCP directly (lib/mcp_client.py); the analyst uses "
            "these tools for at most one web-research call per question."
        ),
    }


@router.get("/status/providers")
def get_provider_status():
    """Per-provider connectivity for the HUD header. Serve-stale-while-
    revalidate: the header renders instantly from the last persisted check
    (even right after a restart) while a background sweep refreshes it."""
    from lib.api_cache import serve_with_refresh
    payload, stale = serve_with_refresh("provider_status", 120, _build_provider_status)
    if payload is None:
        return {"providers": [], "checked_at": None}
    return {**payload, "stale": stale}


@router.get("/data-platform/parity")
def data_platform_parity(symbol: str = "BTC", window_min: int = 60):
    """Phase 5's gate: independent feeds of the same instrument compared
    as time-aligned mids, in bps. Cross-venue pairs are the standing
    health baseline; a candidate adapter must pass the tight same-venue
    threshold before it may replace an incumbent."""
    from lib.feed_parity import parity_report
    return parity_report(symbol_base=symbol, window_min=window_min)


@router.get("/brief")
def morning_brief(window_hours: int = 24):
    """One answer to 'what happened while I slept?' — gate movement,
    corpus maturation, book activity, platform flow, positioning
    extremes and today's expected releases, assembled from the owning
    modules' own numbers.

    Served from a snapshot. Measured at **172 seconds** to derive on
    2026-08-15, which is why the panel sat on "assembling…" — and paid
    again from zero after every restart. The snapshot is refreshed behind
    the response and carries its own age, so the operator reads a brief
    from a few minutes ago instead of watching a spinner (§140.3).
    """
    from lib.morning_brief import build_brief
    from lib.snapshot_cache import cached

    hours = max(1, min(window_hours, 168))
    # 20 minutes: the brief's inputs are daily-to-hourly clocks, and §140.4
    # asks for roughly three refreshes a day of the news half. A shorter TTL
    # would spend three minutes of CPU to change almost nothing.
    return cached(f"brief:{hours}", 1200, lambda: build_brief(hours))


@router.get("/data-platform/health")
def data_platform_health():
    """Phase 3 observability: bounded-queue drop counts (a pipeline that
    drops silently reports completeness it doesn't have), the raw-event
    store's size, the §46 bytes/day measurement that decides any storage
    migration, and each live book's health verdict."""
    from lib.event_store import get_store, tier_of
    from lib.market_events import all_queue_stats
    from lib.orderbook_stream import _latest_snapshot

    store = get_store()
    books = []
    for key, snap in _latest_snapshot.items():
        h = snap.get("health") or {}
        books.append({"stream": key, "tier": tier_of(snap.get("symbol")),
                      "valid": h.get("valid"), "reason": h.get("reason"),
                      "age_seconds": h.get("age_seconds")})
    return {
        "queues": all_queue_stats(),
        "store": store.summary(),
        "bytes_by_day": store.bytes_by_day()[:30],
        "books": books,
    }


@router.get("/ops/error-rate")
def get_error_rate(window_minutes: int = 15):
    from app.request_metrics import error_rate_summary
    return error_rate_summary(window_minutes)


@router.get("/jobs/status")
def jobs_status(): return job_status


@router.get("/system/trading-status")
def get_trading_status():
    """Global kill-switch state. When live_trading_enabled is False, execute_signals
    and the manual approve/execute routes refuse to submit new live orders — existing
    positions' hard stop-loss/take-profit enforcement in manage_positions is unaffected."""
    from lib.kill_switch import get_kill_switch_state
    return get_kill_switch_state()

@router.post("/system/trading-status")
def set_trading_status(body: TradingStatusRequest):
    from lib.kill_switch import set_live_trading_enabled
    return set_live_trading_enabled(body.enabled, body.reason)


@router.post("/jobs/{job_name}/trigger")
def trigger_job(job_name: str):
    # (module path, function name) — defaults to "run" for the common case.
    # Scanner modes each have a dedicated zero-arg entry point (matching how
    # app/scheduler.py itself invokes them); "guardian" lives directly in
    # app.scheduler rather than a jobs.* module, so it can't use the generic
    # "jobs.X".run pattern either.
    job_map={"market":("jobs.fetch_market_data","run"),"threats":("jobs.fetch_threat_news","run"),
             "signals":("jobs.generate_signals","run"),"execute":("jobs.execute_signals","run"),
             "positions":("jobs.manage_positions","run"),"telegram":("jobs.telegram_bot","run"),
             "autosim":("jobs.auto_simulator","run"),
             "evaluation":("jobs.evaluate_signals","run"),
             "paper":("jobs.paper_trading","run"),
             "guardian":("app.scheduler","portfolio_guardian"),
             "scanner_premarket":("jobs.scan_opportunities","run_pre_market"),
             "scanner_intraday":("jobs.scan_opportunities","run_intraday"),
             "scanner_crypto":("jobs.scan_opportunities","run_crypto"),
             "scanner_futures":("jobs.scan_opportunities","run_futures"),
             # Jobs added later were missing here, so their Ops "Run Now"
             # buttons 404'd — the map must grow with app/scheduler.py.
             "insider":("jobs.fetch_insider_activity","run"),
             "inst13f":("jobs.fetch_13f_filings","run"),
             "congress":("jobs.fetch_congress_trades","run"),
             "ipo":("jobs.fetch_ipo_filings","run"),
             "postmortem":("jobs.collect_postmortems","run"),
             "crypto_derivatives":("jobs.fetch_crypto_derivatives","run")}
    if job_name not in job_map: raise HTTPException(404)
    # make_job_runner silently no-ops a trigger while the job is already
    # "running" — that's correct for the scheduler (avoids double-execution),
    # but a manual "Run Now" click would otherwise appear to succeed (200 OK)
    # while doing nothing, with zero feedback that it was skipped. Surface it
    # instead of spawning a thread we already know will no-op.
    if job_status.get(job_name, {}).get("status") == "running":
        return {"ok": False, "already_running": True,
                "detail": f"'{job_name}' is already running — wait for it to finish, or use Reset if it's stuck."}
    import importlib, threading
    from app.scheduler import make_job_runner
    module_path, func_name = job_map[job_name]
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    threading.Thread(target=make_job_runner(job_name, fn), daemon=True).start()
    return {"ok":True,"job":job_name}


@router.post("/jobs/{job_name}/reset")
def reset_job_status(job_name: str):
    """Force a job's tracked status back to idle without restarting the app.
    For recovering from a genuinely hung run (e.g. a network call that never
    timed out) whose status is permanently stuck at "running", which — by
    design — blocks every subsequent scheduled or manual trigger for that
    job. This does NOT stop whatever thread is actually still running (Python
    has no safe way to kill a thread); it only clears the tracking flag so
    new runs aren't blocked. If the old thread eventually finishes, it will
    overwrite the status again with its own (stale) result."""
    if job_name not in job_status:
        raise HTTPException(404, f"Unknown job '{job_name}'")
    job_status[job_name]["status"] = "idle"
    job_status[job_name]["error"] = None
    return {"ok": True, "job": job_name, "status": "idle"}


@router.get("/llm/health")
def llm_health():
    try:
        from lib.lmstudio import check_health
        return check_health()
    except Exception as e:
        return {"ok":False,"error":str(e)}

@router.get("/llm/routing")
def llm_routing(days: int = 30):
    """What the router decided, what it cost, and whether it paid off.

    `effectiveness` is empty until both arms of a task have enough closed
    trades to compare — that is the honest state, not a failure. An empty
    list means the question is not yet answerable, which is different from
    the answer being "no difference".
    """
    try:
        from lib import llm_router as llm
        eff = llm.thinking_effectiveness(days=max(days, 90))
        return {
            "coverage": llm.coverage(days),
            "usage": llm.usage_stats(days),
            "effectiveness": eff,
            "effectiveness_note": (
                None if eff else
                "Not enough closed trades in both arms yet to compare thinking "
                "against non-thinking. Rows appear once each side clears 20 trades."
            ),
            "tasks": [
                {"task": t.name, "default_mode": t.default_mode, "why": t.why}
                for t in sorted(llm.TASKS.values(), key=lambda x: x.name)
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@router.post("/llm/routing/preview")
def llm_routing_preview(body: dict = Body(...)):
    """Ask the router what it WOULD do, without calling the model.

    The policy is a set of fixed thresholds; this makes them inspectable
    rather than something to be inferred from logs after the fact.
    """
    try:
        from lib import llm_router as llm
        r = llm.route(str(body.get("task") or "unspecified"),
                      str(body.get("mode") or llm.AUTO),
                      body.get("context") or {})
        return {"task": r.task, "requested": r.mode, "resolved": r.resolved_mode,
                "thinking": r.thinking, "reason": r.reason,
                "triggers": llm.auto_triggers(body.get("context") or {})}
    except Exception as e:
        return {"error": str(e)}


@router.get("/cache/stats")
def cache_stats():
    try:
        from lib.ohlcv_cache import get_cache_stats
        return get_cache_stats()
    except Exception as e:
        return {"error":str(e)}

@router.post("/cache/backfill")
def trigger_backfill():
    import threading
    def run_backfill():
        try:
            from lib.ohlcv_cache import backfill_symbol, init_cache_db
            from jobs.generate_signals import ALL_SYMBOLS
            init_cache_db()
            for sym in ALL_SYMBOLS[:30]:
                backfill_symbol(sym, "1D", days=730)
                backfill_symbol(sym, "4H", days=180)
                backfill_symbol(sym, "1H", days=90)
        except Exception as e:
            logger.error(f"[Backfill] Error: {e}")
    threading.Thread(target=run_backfill, daemon=True).start()
    return {"ok":True,"message":"Backfill started in background"}

@router.get("/settings")
def get_settings():
    with get_db() as db:
        return [_config_dict(c) for c in db.query(PlatformConfig).all()]


@router.post("/settings/telegram/detect-chat")
def detect_telegram_chat(body: TelegramSetupRequest):
    from lib.telegram_setup import detect_recent_chat
    token, _ = _telegram_setup_credentials(body)
    try:
        return {"ok": True, **detect_recent_chat(token)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))


@router.post("/settings/telegram/test")
def test_telegram_setup(body: TelegramSetupRequest):
    from lib.telegram_setup import verify_bot_connection
    token, chat_id = _telegram_setup_credentials(body)
    try:
        return verify_bot_connection(token, chat_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))

@router.post("/settings")
def create_setting(body: ConfigCreate):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cfg = PlatformConfig(id=str(uuid.uuid4()), key=f"{body.platform}_{body.label}_{now[:10]}",
            label=body.label, platform=body.platform, config_type=body.config_type,
            api_key=body.api_key, api_secret=body.api_secret, api_url=body.api_url,
            extra_field_1=body.extra_field_1, extra_field_2=body.extra_field_2,
            is_active=body.is_active, is_default=body.is_default, notes=body.notes,
            created_date=now, updated_date=now)
        db.add(cfg); return _config_dict(cfg)

@router.put("/settings/{cfg_id}")
def update_setting(cfg_id: str, body: ConfigUpdate):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id==cfg_id).first()
        if not cfg: raise HTTPException(404)
        # exclude_unset: only fields the client actually sent should be applied —
        # otherwise every field's schema default (is_active=True, is_default=False,
        # notes="") gets written back on every partial update, silently resetting
        # whatever was previously stored.
        for k,v in body.dict(exclude_unset=True).items():
            if k in {"api_key", "api_secret"} and not v:
                continue
            if hasattr(cfg,k) and v is not None: setattr(cfg,k,v)
        cfg.updated_date=now; return _config_dict(cfg)

@router.delete("/settings/{cfg_id}")
def delete_setting(cfg_id: str):
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id==cfg_id).first()
        if not cfg: raise HTTPException(404)
        db.delete(cfg)
    return {"ok":True}

@router.post("/settings/{cfg_id}/set-default")
def set_default(cfg_id: str):
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id==cfg_id).first()
        if not cfg: raise HTTPException(404)
        [setattr(o,"is_default",False) for o in db.query(PlatformConfig).filter(PlatformConfig.platform==cfg.platform, PlatformConfig.id!=cfg_id).all()]
        cfg.is_default=True
    return {"ok":True}

@router.get("/debug/positions")
def debug_positions_raw():
    """Debug: return raw Alpaca position data to diagnose filtering/class issues."""
    try:
        from lib.alpaca_client import get_positions
        positions = get_positions()
        out = []
        for p in positions:
            raw_class = str(getattr(p, "asset_class", "") or "")
            qty_raw   = str(getattr(p, "qty", ""))
            out.append({
                "symbol":          str(p.symbol),
                "qty_raw":         qty_raw,
                "qty_float":       float(p.qty or 0),
                "raw_asset_class": raw_class,
                "resolved_class":  _position_dict(p)["asset_class"],
                "market_value":    float(p.market_value or 0),
                "side":            str(p.side).lower().split(".")[-1],
            })
        return {"count": len(out), "positions": out}
    except Exception as e:
        return {"error": str(e)}

