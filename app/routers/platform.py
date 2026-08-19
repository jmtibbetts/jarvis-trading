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


@router.get("/platform/mode")
def platform_mode_status():
    """What JARVIS is currently permitted to do with real money.

    Surfaced so the UI can render LIVE EXECUTION DISABLED — TRAINING MODE
    as the CURRENT STATE rather than as a broken feed. An operator who
    sees a red "unavailable" badge will try to repair it; one who sees a
    declared training mode will not.
    """
    from lib.platform_mode import status as mode_status
    return mode_status()


@router.get("/platform/size")
def platform_size(symbol: str, entry: float, stop: float,
                  equity: float = 0.0, risk_pct: float = 1.0,
                  risk_usd: float = 0.0, side: str = "Long",
                  product: str | None = None, leverage: float = 1.0):
    """THE canonical position sizer. The UI renders this; it never derives it.

    The frontend calculator computed:

        qty      = riskDollars / |entry - stop|
        notional = qty * entry

    That is share arithmetic. It is correct for equities and crypto and
    wrong for every contract product: risk per unit is price distance TIMES
    the multiplier, so a 10-point MES stop is $50 of risk rather than $10,
    and a "1% risk" position was really 5% — on gold, 100%. Notional was
    understated by the same factor, so the exposure the operator was shown
    bore no relation to the exposure they had.

    A browser cannot know a multiplier, a tick size, a margin requirement
    or whether a contract may be fractional. This does, from the one
    instrument authority, and returns the answer already computed.
    """
    from lib.instruments import UnsupportedInstrument, resolve

    budget = float(risk_usd) if risk_usd else float(equity) * (float(risk_pct) / 100.0)
    out = {"symbol": symbol, "side": side, "entry": entry, "stop": stop,
           "risk_budget_usd": round(budget, 2), "ok": False}

    try:
        inst = resolve(symbol, product=product).require_executable()
    except UnsupportedInstrument as e:
        # A contract whose units are unknown cannot be sized. Refusing is
        # the whole point — the old path would have returned a number.
        return {**out, "reason": "UNSUPPORTED_INSTRUMENT", "detail": str(e)}

    distance = abs(float(entry) - float(stop))
    if distance <= 0 or float(entry) <= 0 or budget <= 0:
        return {**out, "reason": "NO_RISK_DISTANCE",
                "detail": "entry, stop and a risk budget are all required"}

    mult = float(inst.multiplier or 1.0)
    risk_per_unit = distance * mult
    whole = inst.quantity_unit == "CONTRACTS"
    qty = (float(int(budget // risk_per_unit)) if whole
           else budget / risk_per_unit)

    if whole and qty < 1:
        return {**out, "reason": "BELOW_MIN_SIZE",
                "quantity_unit": inst.quantity_unit,
                "risk_per_contract_usd": round(risk_per_unit, 2),
                "detail": (f"${budget:,.0f} is below the ${risk_per_unit:,.0f} "
                           f"risk of one contract — rounding up would take "
                           f"more risk than authorised")}

    notional = qty * float(entry) * mult
    margin = (qty * float(inst.initial_margin)) if inst.initial_margin else (
        notional / max(1.0, float(leverage)))

    return {
        **out, "ok": True,
        "instrument_id": inst.instrument_id,
        "asset_class": inst.asset_class, "product": inst.product,
        "quantity": qty, "quantity_unit": inst.quantity_unit,
        "multiplier": mult, "tick_size": inst.tick_size,
        "risk_per_unit_usd": round(risk_per_unit, 4),
        "risk_usd": round(qty * risk_per_unit, 2),
        "notional_usd": round(notional, 2),
        "margin_usd": round(margin, 2),
        "notional_pct_of_equity": (round(notional / float(equity) * 100.0, 2)
                                   if equity else None),
        "limiting_constraint": "whole_contracts" if whole else "risk_budget",
        "provenance": "CALCULATED — canonical instrument multiplier and units",
    }


@router.get("/attention")
def attention_queue(limit: int = 60):
    """What requires the operator's attention right now, ranked.

    The Command Center's input. Deliberately NOT snapshot-cached: a stale
    attention queue is worse than a slow one, because the whole claim it
    makes is about the present. Measured at ~0.9s over the live book, and
    the one expensive input inside it (the earnings calendar, five Yahoo
    requests cold) goes through the same 6h cache the earnings panel uses.

    `complete` is false when any producer failed. An empty queue means
    "nothing needs you", which is exactly what a silently-failing producer
    would fabricate — so the failures are reported alongside.
    """
    from lib.attention import collect
    return collect(limit=max(1, min(int(limit), 200)))


@router.get("/instrument/{symbol:path}")
def instrument_workspace(symbol: str, product: str | None = None,
                         entry: float | None = None):
    """One instrument, everything known about it, with every gap named.

    An instrument's facts lived on four surfaces that each answered a
    different question, and nothing said whether they agreed. The refusal
    case is the one that most needed a home: `6J=F` resolves UNSUPPORTED
    with signals sitting behind it, and an empty panel reads as a bug
    while a stated refusal with a count reads as a work item.

    `entry` is supplied by the caller (the chart already has the last
    close) rather than fetched here — a workspace open on a second monitor
    must not spend a quote on every poll.
    """
    from lib.instrument_workspace import workspace
    return workspace(symbol, product=product, entry=entry)


@router.get("/platform/integrity")
def platform_integrity():
    """Every training-data invariant, run against live rows.

    Each defect this programme found was CONFIDENT and WRONG, and nothing
    in the interface distinguished that from confident and right. These
    checks are the remedy made continuous, so the next one is visible
    before it has poisoned a season of training rather than after.
    """
    from lib.integrity_panel import run_all
    return run_all()


@router.get("/platform/venues")
def platform_venues():
    """What can actually be executed, and what is merely visible.

    UI availability is not API availability — Kraken Pro shows a human
    11,000 US equities while the API Center documents no stock trading
    contract, and the difference belongs on screen rather than in a
    developer's memory.
    """
    from lib.execution_venue import registry
    from lib.venue_capabilities import snapshot
    return {**snapshot(), "adapters": registry()["adapters"]}


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


@router.get("/brief/news")
def brief_news_surface(hours: int = 24, per_bucket: int = 6):
    """Market-moving news for the brief, grouped by what it could move.

    Separate from /brief on purpose. The brief is a 172-second snapshot
    refreshed on a slow clock; news lands every 15 minutes and is cheap to
    query, so tying it to the snapshot would have made it as stale as the
    thing it sits next to. This reads the live table each call.
    """
    from lib.brief_news import brief_news
    return brief_news(hours=hours, per_bucket=per_bucket)


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
def jobs_status():
    """Per-job state, plus WHO OWNS the economic scheduler.

    A desk cannot tell "nothing qualified" from "this process is not the one
    trading" if ownership is invisible. Two processes against one book both
    used to start schedulers silently; now the second stands by, and this is
    where an operator sees that.
    """
    import os as _os

    from lib import scheduler_lease as SL
    owner = SL.current_owner()
    if _os.getenv("JARVIS_DISABLE_SCHEDULER") == "1":
        role = "DISABLED"
    elif owner and owner.get("pid") == _os.getpid():
        role = "OWNER"
    elif owner:
        role = "STANDBY"
    else:
        role = "UNCLAIMED"
    return {"jobs": job_status,
            "scheduler": {"role": role, "owner": owner,
                          "disabled_by_env":
                              _os.getenv("JARVIS_DISABLE_SCHEDULER") == "1"},
            **job_status}


@router.get("/system/runtime")
def system_runtime():
    """Where JARVIS is actually running, and on what storage.

    Exists so "is this the Linux runtime or a stray Windows one?" is a
    question with an answer, rather than something inferred from a log.
    """
    from lib.runtime_paths import runtime_report
    return runtime_report()


@router.get("/providers/health")
def providers_health():
    """Every provider capability, its status, freshness and quota.

    Exists so nobody has to SSH in and hand-probe. A 429 on Massive and a
    402 on LunarCrush both went unnoticed for want of exactly this — the
    only health table in the system covered the 43 RSS feeds and no paid
    API at all.

    Contains no credentials: error text is sanitised before it is stored.
    """
    from lib import provider_health as PH
    rows = PH.snapshot()
    actionable = [r for r in rows if r.get("actionable")]
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "providers": rows,
        "counts": by_status,
        "actionable": actionable,
        "tracked": len(rows),
        "statuses": list(PH.STATUSES),
    }


@router.get("/providers/health/{provider}")
def provider_health_detail(provider: str):
    from lib import provider_health as PH
    rows = [r for r in PH.snapshot() if r["provider"] == provider]
    if not rows:
        raise HTTPException(404, f"no health records for {provider!r}")
    return {"provider": provider, "capabilities": rows}


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
             "guardian":("app.scheduler","external_alpaca_guardian"),
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

