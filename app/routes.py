"""
FastAPI routes v6.0 — all /api/* endpoints.
Added: /regime, /portfolio/equity, /market/full, /positions/close, /signals/clear/expired
"""
import logging, uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset, Position, PlatformConfig, PortfolioSnapshot
from app.scheduler import job_status

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/health")
def health():
    return {"status":"ok","time":datetime.now(timezone.utc).isoformat()}

@router.get("/signals")
def get_signals(status: str = None, limit: int = 150):
    with get_db() as db:
        q = db.query(TradingSignal)
        if status: q = q.filter(TradingSignal.status == status)
        return [_sig_dict(s) for s in q.order_by(TradingSignal.generated_at.desc()).limit(limit).all()]

@router.delete("/signals/clear/expired")
def clear_expired():
    with get_db() as db:
        n = db.query(TradingSignal).filter(TradingSignal.status.in_(["Expired","Rejected"])).delete()
    return {"ok":True,"deleted":n}

@router.delete("/signals/{signal_id}")
def delete_signal(signal_id: str):
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        db.delete(sig)
    return {"ok":True}

class ExecuteRequest(BaseModel):
    qty: Optional[int] = None

@router.post("/signals/{signal_id}/execute")
def manual_execute(signal_id: str, body: ExecuteRequest = ExecuteRequest()):
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        try:
            from lib.alpaca_client import submit_bracket_order, normalize_symbol, is_crypto
            sym, crypto = normalize_symbol(sig.asset_symbol)
            entry  = float(sig.entry_price or 100)
            if body.qty and body.qty > 0:
                qty = float(body.qty)
            elif crypto:
                # Fractional qty for crypto — $1000 notional
                qty = round(1000.0 / entry, 6) if entry > 0 else 0.01
            else:
                qty = max(1, int(1000 / entry)) if entry > 0 else 1
            result = submit_bracket_order(symbol=sym, qty=qty, entry_price=sig.entry_price,
                                          take_profit=sig.target_price, stop_loss=sig.stop_loss)
            sig.status = "Executed"; sig.updated_date = datetime.now(timezone.utc).isoformat()
            # Store Alpaca order ID on the signal for position linking
            try:
                order_id = result.get("id") or result.get("order_id") if isinstance(result, dict) else getattr(result, "id", None)
                if order_id:
                    sig.alpaca_order_id = str(order_id)
            except: pass
            return {"ok":True,"order":result,"qty":qty,"crypto":crypto}
        except Exception as e:
            raise HTTPException(500, str(e))

class SaveSignalRequest(BaseModel):
    asset_symbol: Optional[str] = None
    asset_name:   Optional[str] = None
    asset_class:  Optional[str] = "Equity"
    direction:    Optional[str] = "Long"
    confidence:   Optional[int] = 65
    timeframe:    Optional[str] = "4H"
    entry_price:  Optional[float] = None
    target_price: Optional[float] = None
    stop_loss:    Optional[float] = None
    reasoning:    Optional[str]   = ""
    key_risks:    Optional[str]   = ""
    momentum:     Optional[str]   = ""

@router.post("/signals/save")
def save_signal(body: SaveSignalRequest):
    """Save a manually-scanned signal to the DB."""
    import uuid as _uuid
    now_iso = datetime.now(timezone.utc).isoformat()
    rr = None
    if body.entry_price and body.target_price and body.stop_loss and body.entry_price > body.stop_loss:
        try: rr = round((body.target_price - body.entry_price) / (body.entry_price - body.stop_loss), 2)
        except: pass
    with get_db() as db:
        existing = db.query(TradingSignal).filter(
            TradingSignal.asset_symbol == body.asset_symbol,
            TradingSignal.status == "Active"
        ).first()
        if existing:
            return {"error": f"Active signal for {body.asset_symbol} already exists"}
        sig = TradingSignal(
            id           = str(_uuid.uuid4()),
            asset_symbol = body.asset_symbol,
            asset_name   = body.asset_name or body.asset_symbol,
            asset_class  = body.asset_class or "Equity",
            direction    = body.direction or "Long",
            confidence   = body.confidence or 65,
            composite_score = body.confidence or 65,
            timeframe    = body.timeframe or "4H",
            entry_price  = body.entry_price,
            target_price = body.target_price,
            stop_loss    = body.stop_loss,
            reasoning    = body.reasoning or "",
            key_risks    = body.key_risks or "",
            momentum     = body.momentum or "",
            signal_source = "scanner",
            rr_ratio     = rr,
            status       = "Active",
            generated_at = now_iso,
            created_date = now_iso,
            updated_date = now_iso,
        )
        db.add(sig)
        sig_id = sig.id  # capture before session closes
    return {"ok": True, "id": sig_id}

@router.get("/threats")
def get_threats(limit: int = 60):
    with get_db() as db:
        return [_threat_dict(t) for t in db.query(ThreatEvent).filter(ThreatEvent.status=="Active").order_by(ThreatEvent.published_at.desc()).limit(limit).all()]

@router.get("/news")
def get_news(limit: int = 80):
    with get_db() as db:
        return [_news_dict(n) for n in db.query(NewsItem).order_by(NewsItem.published_at.desc()).limit(limit).all()]

@router.get("/market")
def get_market():
    with get_db() as db:
        return [_asset_dict(a) for a in db.query(MarketAsset).order_by(MarketAsset.symbol).all()]

@router.get("/market/full")
def get_market_full():
    with get_db() as db:
        assets = db.query(MarketAsset).order_by(MarketAsset.change_percent.desc()).all()
        # Serialize inside session — avoids DetachedInstanceError
        asset_dicts = [_asset_dict(a) for a in assets]
    equities = [a for a in asset_dicts if a.get("asset_class") != "Crypto"]
    crypto   = [a for a in asset_dicts if a.get("asset_class") == "Crypto"]
    return {"equities": equities, "crypto": crypto, "count": len(asset_dicts)}


@router.get("/positions/with-signals")
def get_positions_with_signals():
    """Positions enriched with their originating signal data."""
    try:
        from lib.alpaca_client import get_positions, get_account
        positions = get_positions()
        account   = get_account()
        equity    = float(account.equity)
        mv_total  = sum(float(p.market_value or 0) for p in positions)
        pl_total  = sum(float(p.unrealized_pl or 0) for p in positions)

        # Build symbol → signal map — serialize to dicts INSIDE session
        with get_db() as db:
            db_sigs = db.query(TradingSignal).filter(
                TradingSignal.status.in_(["Executed", "Active", "Closed"])
            ).order_by(TradingSignal.generated_at.desc()).all()
            # Convert to plain dicts while session is still open
            sig_dicts = [_sig_dict(s) for s in db_sigs]

        sig_map = {}
        for s in sig_dicts:
            sym = s.get("asset_symbol", "")
            if sym and sym not in sig_map:
                sig_map[sym] = s
            base = sym.replace("/USD", "")
            if base and base not in sig_map:
                sig_map[base] = s

        result = []
        for p in positions:
            sym = str(p.symbol)
            pos_dict = _position_dict(p)
            sig = sig_map.get(sym) or sig_map.get(sym.replace("/USD","")) or sig_map.get(sym + "/USD") or sig_map.get(sym.lower()) or sig_map.get(sym.upper())
            if sig:
                entry  = float(sig.get("entry_price") or 0)
                target = float(sig.get("target_price") or 0)
                stop   = float(sig.get("stop_loss") or 0)
                curr   = float(p.current_price or 0)
                rr     = round((target - entry) / (entry - stop), 2) if entry > stop and target > entry else None
                progress = round((curr - entry) / (target - entry) * 100, 1) if target > entry and curr else None
                pos_dict["signal"] = dict(sig, rr=rr, progress_pct=progress)
            else:
                # No DB signal — build synthetic context from Alpaca position data
                avg = float(p.avg_entry_price or 0)
                curr = float(p.current_price or 0)
                cost_basis = float(p.cost_basis or 0)
                pos_dict["signal"] = {
                    "asset_symbol": sym,
                    "direction": "Long" if float(p.qty or 0) > 0 else "Short",
                    "entry_price": avg,
                    "target_price": None,
                    "stop_loss": None,
                    "confidence": None,
                    "composite_score": None,
                    "timeframe": None,
                    "rr": None,
                    "progress_pct": None,
                    "reasoning": f"Position entered manually or via external order. Cost basis: ${cost_basis:,.2f}",
                    "key_risks": None,
                    "momentum": None,
                    "signal_source": "manual",
                    "generated_at": None,
                    "_manual": True,
                }
            result.append(pos_dict)

        return {
            "positions": result,
            "account": {
                "equity":         equity,
                "cash":           float(account.cash),
                "buying_power":   float(account.buying_power),
                "market_value":   mv_total,
                "unrealized_pl":  pl_total,
                "unrealized_plpc": (pl_total / (equity - pl_total) * 100) if (equity - pl_total) > 0 else 0,
                "day_trade_count": int(account.daytrade_count or 0),
            }
        }
    except Exception as e:
        raise HTTPException(500, f"Alpaca error: {e}")


@router.get("/positions")
def get_positions_live():
    try:
        from lib.alpaca_client import get_positions, get_account
        positions = get_positions(); account = get_account()
        equity = float(account.equity); mv = sum(float(p.market_value or 0) for p in positions)
        pl     = sum(float(p.unrealized_pl or 0) for p in positions)
        return {"positions":[_position_dict(p) for p in positions],
                "account":{"equity":equity,"cash":float(account.cash),
                            "buying_power":float(account.buying_power),"market_value":mv,
                            "unrealized_pl":pl,"unrealized_plpc":(pl/(equity-pl)*100) if (equity-pl)>0 else 0,
                            "day_trade_count":int(account.daytrade_count or 0)}}
    except Exception as e:
        raise HTTPException(500, f"Alpaca error: {e}")

@router.post("/positions/{symbol}/close")
def close_pos(symbol: str):
    try:
        from lib.alpaca_client import close_position
        close_position(symbol)
        return {"ok":True,"symbol":symbol}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/portfolio/equity")
def get_equity(hours: int = 24):
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
    with get_db() as db:
        snaps = db.query(PortfolioSnapshot).filter(PortfolioSnapshot.snapshot_at>=cutoff).order_by(PortfolioSnapshot.snapshot_at.asc()).all()
        return [{"time":s.snapshot_at,"equity":s.equity,"cash":s.cash,"market_value":s.market_value,"unrealized_pl":s.unrealized_pl,"position_count":s.position_count} for s in snaps]

@router.get("/regime")
def get_regime_endpoint():
    try:
        from lib.market_regime import get_regime
        return get_regime()
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/jobs/status")
def jobs_status(): return job_status

@router.post("/jobs/{job_name}/trigger")
def trigger_job(job_name: str):
    job_map={"market":"jobs.fetch_market_data","threats":"jobs.fetch_threat_news",
             "signals":"jobs.generate_signals","execute":"jobs.execute_signals",
             "positions":"jobs.manage_positions","telegram":"jobs.telegram_bot"}
    if job_name not in job_map: raise HTTPException(404)
    import importlib, threading
    from app.scheduler import make_job_runner
    mod = importlib.import_module(job_map[job_name])
    threading.Thread(target=make_job_runner(job_name, mod.run), daemon=True).start()
    return {"ok":True,"job":job_name}

@router.get("/llm/health")
def llm_health():
    try:
        from lib.lmstudio import check_health
        return check_health()
    except Exception as e:
        return {"ok":False,"error":str(e)}

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

class ConfigCreate(BaseModel):
    label: str; platform: str; config_type: Optional[str]="api"
    api_key: Optional[str]=""; api_secret: Optional[str]=""; api_url: Optional[str]=""
    extra_field_1: Optional[str]=""; extra_field_2: Optional[str]=""
    is_active: Optional[bool]=True; is_default: Optional[bool]=False; notes: Optional[str]=""

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
def update_setting(cfg_id: str, body: ConfigCreate):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id==cfg_id).first()
        if not cfg: raise HTTPException(404)
        for k,v in body.dict().items():
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

@router.get("/alpaca/orders")
def get_orders():
    try:
        from lib.alpaca_client import get_open_orders
        orders=get_open_orders()
        return [{"id":str(o.id),"symbol":str(o.symbol),"qty":float(o.qty or 0),"side":str(o.side),"status":str(o.status),"type":str(o.order_type)} for o in orders]
    except Exception as e: raise HTTPException(500,str(e))

@router.delete("/alpaca/orders/{order_id}")
def cancel_order(order_id: str):
    try:
        from lib.alpaca_client import get_trading_client
        get_trading_client().cancel_order_by_id(order_id)
        return {"ok":True}
    except Exception as e: raise HTTPException(500,str(e))

@router.delete("/alpaca/orders")
def cancel_all_orders():
    """Cancel ALL open orders on Alpaca and reset their signals back to Active."""
    try:
        from lib.alpaca_client import get_trading_client
        client = get_trading_client()
        client.cancel_orders()  # cancels all open orders
        # Also reset any PendingApproval signals back to Active so they can re-queue
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            pending = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").all()
            for s in pending:
                s.status = "Active"
                s.updated_date = now_iso
            cancelled_count = len(pending)
        return {"ok": True, "orders_cancelled": True, "signals_reset": cancelled_count}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/signals/pending")
def get_pending_signals():
    """Get all signals queued for Monday morning approval."""
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(
            TradingSignal.status == "PendingApproval"
        ).order_by(TradingSignal.confidence.desc()).all()
        return [_sig_dict(s) for s in sigs]

@router.post("/signals/{signal_id}/approve")
def approve_signal(signal_id: str):
    """Approve a pending signal — immediately submit the order to Alpaca."""
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if sig.status != "PendingApproval":
            raise HTTPException(400, f"Signal is {sig.status}, not PendingApproval")
        try:
            from lib.alpaca_client import submit_bracket_order, normalize_symbol, is_crypto, get_account
            sym, crypto = normalize_symbol(sig.asset_symbol)
            entry  = float(sig.entry_price or 0)
            target = float(sig.target_price or 0)
            stop   = float(sig.stop_loss or 0)
            if not entry or not target or not stop:
                raise ValueError("Signal missing price levels")
            # Check buying power
            account = get_account()
            buying_power = float(account.buying_power)
            qty = max(1, int(min(1500, buying_power * 0.2) / entry)) if not crypto else round(min(1000, buying_power * 0.1) / entry, 6)
            if qty <= 0:
                raise ValueError(f"Insufficient buying power ${buying_power:.0f}")
            result = submit_bracket_order(symbol=sym, qty=qty, entry_price=entry, take_profit=target, stop_loss=stop)
            sig.status = "Executed"
            sig.updated_date = datetime.now(timezone.utc).isoformat()
            return {"ok": True, "order": result, "qty": qty, "symbol": sym}
        except Exception as e:
            sig.status = "Rejected"
            sig.updated_date = datetime.now(timezone.utc).isoformat()
            raise HTTPException(500, str(e))

@router.post("/signals/{signal_id}/reject")
def reject_signal(signal_id: str):
    """Reject a pending signal — discard without trading."""
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404)
        sig.status = "Rejected"
        sig.updated_date = datetime.now(timezone.utc).isoformat()
        return {"ok": True}

@router.post("/signals/approve-all")
def approve_all_signals():
    """Approve ALL pending signals — submit all to Alpaca."""
    from lib.alpaca_client import submit_bracket_order, normalize_symbol, get_account
    account = get_account()
    buying_power = float(account.buying_power)
    now_iso = datetime.now(timezone.utc).isoformat()
    approved = rejected = 0
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").order_by(TradingSignal.confidence.desc()).all()
        for sig in sigs:
            if buying_power < 100:
                break
            try:
                sym, crypto = normalize_symbol(sig.asset_symbol)
                entry  = float(sig.entry_price or 0)
                target = float(sig.target_price or 0)
                stop   = float(sig.stop_loss or 0)
                if not entry or not target or not stop or stop >= entry or target <= entry:
                    sig.status = "Rejected"; sig.updated_date = now_iso; rejected += 1; continue
                trade_budget = min(buying_power * 0.15, 1500)
                qty = max(1, int(trade_budget / entry)) if not crypto else round(trade_budget / entry, 6)
                submit_bracket_order(symbol=sym, qty=qty, entry_price=entry, take_profit=target, stop_loss=stop)
                sig.status = "Executed"; sig.updated_date = now_iso
                buying_power -= qty * entry
                approved += 1
            except Exception as e:
                sig.status = "Rejected"; sig.updated_date = now_iso; rejected += 1
    return {"ok": True, "approved": approved, "rejected": rejected, "buying_power_remaining": round(buying_power, 2)}

@router.post("/signals/reject-all")
def reject_all_pending():
    """Reject all pending signals."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").all()
        for s in sigs:
            s.status = "Rejected"; s.updated_date = now_iso
        return {"ok": True, "rejected": len(sigs)}

class AnalyzeRequest(BaseModel):
    symbol: str; timeframes: Optional[list]=["1H","4H","1D"]; generate_signal: Optional[bool]=False

@router.post("/analyze")
def analyze(body: AnalyzeRequest):
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import analyze_symbol, build_ta_prompt_block
        bars=fetch_multi_timeframe(body.symbol.upper(), body.timeframes)
        ta=analyze_symbol(bars); pb=build_ta_prompt_block(body.symbol.upper(),ta)
        signal=None
        if body.generate_signal:
            try:
                from lib.lmstudio import call_lm_studio, parse_json
                from lib.market_regime import get_regime
                regime=get_regime()
                prompt=f"""Analyze this ticker for a trade setup:\n\n{pb}\n\nRegime: {regime.get("label")} | Risk: {regime.get("risk")}\n\nGenerate ONE signal as JSON object with keys: asset_symbol, asset_class, direction (Long/Bounce only), confidence, timeframe, entry_price, target_price, stop_loss, reasoning, key_risks, momentum. Return ONLY the JSON."""
                raw=call_lm_studio(prompt,max_tokens=800,temperature=0.1)
                parsed=parse_json(raw)
                signal=parsed[0] if isinstance(parsed,list) else parsed
            except Exception as e:
                signal={"error":str(e)}
        return {"symbol":body.symbol.upper(),"ta":ta,"prompt_block":pb,"signal":signal}
    except Exception as e: raise HTTPException(500,str(e))


@router.get("/performance")
def get_performance(days: int = 30):
    """Trade performance statistics over the last N days."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Serialize everything INSIDE the session — avoids DetachedInstanceError
    with get_db() as db:
        all_trades = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Closed", "Executed", "Rejected"]),
            TradingSignal.updated_date >= cutoff
        ).order_by(TradingSignal.updated_date.desc()).all()

        total    = len(all_trades)
        executed = [_sig_dict(t) for t in all_trades if t.status in ("Closed", "Executed")]
        rejected = [t for t in all_trades if t.status == "Rejected"]
        rej_count = len(rejected)

    # All computation now works on plain dicts — no ORM access after session close
    rr_list, scores, classes = [], [], {}
    for t in executed:
        cl = t.get("asset_class") or "Equity"
        classes[cl] = classes.get(cl, 0) + 1
        ep = t.get("entry_price"); tp = t.get("target_price"); sl = t.get("stop_loss")
        if ep and tp and sl and ep > sl:
            rr = round((tp - ep) / (ep - sl), 2)
            rr_list.append(rr)
        sc = t.get("composite_score") or t.get("confidence")
        if sc:
            scores.append(sc)

    avg_rr    = round(sum(rr_list) / len(rr_list), 2) if rr_list else None
    avg_score = round(sum(scores)  / len(scores),  1) if scores  else None
    good_rr   = [r for r in rr_list if r >= 2.0]
    by_class  = [{"class": k, "count": v} for k, v in classes.items()]

    daily = {}
    for t in executed:
        day = (t.get("generated_at") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
    daily_list = sorted([{"date": d, "count": c} for d, c in daily.items()], key=lambda x: x["date"])

    return {
        "period_days":    days,
        "total_signals":  total,
        "executed":       len(executed),
        "rejected":       rej_count,
        "avg_rr":         avg_rr,
        "avg_score":      avg_score,
        "good_rr_count":  len(good_rr),
        "by_class":       by_class,
        "daily_volume":   daily_list,
        "recent_trades":  executed[:50],
    }

def _sig_dict(s):
    return {
        "id":            s.id,
        "asset_symbol":  s.asset_symbol,
        "asset_name":    s.asset_name,
        "asset_class":   s.asset_class,
        "direction":     s.direction,
        "confidence":    s.confidence,
        "composite_score": s.composite_score,
        "timeframe":     s.timeframe,
        "reasoning":     s.reasoning,
        "entry_price":   s.entry_price,
        "target_price":  s.target_price,
        "stop_loss":     s.stop_loss,
        "key_risks":     s.key_risks,
        "momentum":      s.momentum,
        "status":        s.status,
        "generated_at":  s.generated_at,
        "signal_source": getattr(s, "signal_source", "watchlist"),
        "earnings_risk": bool(getattr(s, "earnings_risk", False)),
        "rr_ratio":      getattr(s, "rr_ratio", None),
    }

def _threat_dict(t):
    return {"id":t.id,"title":t.title,"description":t.description,"event_type":t.event_type,
            "severity":t.severity,"country":t.country,"region":t.region,
            "source":t.source,"source_url":t.source_url,"status":t.status,"published_at":t.published_at}

def _news_dict(n):
    return {"id":n.id,"title":n.title,"summary":n.summary,"source":n.source,"url":n.url,
            "category":n.category,"sentiment":n.sentiment,
            "affected_assets":n.affected_assets.split(",") if n.affected_assets else [],
            "region":n.region,"published_at":n.published_at}

def _asset_dict(a):
    return {"id":a.id,"symbol":a.symbol,"name":a.name,"asset_class":a.asset_class,"price":a.price,
            "change_percent":a.change_percent,"volume":a.volume,"market_cap":a.market_cap,
            "region":a.region,"last_updated":a.last_updated}

def _position_dict(p):
    sym = str(p.symbol)
    # Alpaca SDK returns unrealized_plpc as a decimal fraction (e.g. 0.025 = 2.5%)
    plpc_raw = float(p.unrealized_plpc or 0)
    # Convert to percentage: if abs value > 1, it's already in pct; otherwise multiply
    plpc = plpc_raw * 100 if abs(plpc_raw) <= 1 else plpc_raw
    return {
        "symbol":          sym,
        "qty":             float(p.qty or 0),
        "avg_entry":       float(p.avg_entry_price or 0),
        "market_value":    float(p.market_value or 0),
        "unrealized_pl":   float(p.unrealized_pl or 0),
        "unrealized_plpc": round(plpc, 4),
        "side":            str(p.side),
        "asset_class":     "Crypto" if "/" in sym else "Equity",
        "current_price":   float(p.current_price or 0),
    }

def _config_dict(c):
    return {"id":c.id,"key":c.key,"label":c.label,"platform":c.platform,"config_type":c.config_type,
            "api_key":c.api_key,"api_secret":c.api_secret,"api_url":c.api_url,
            "extra_field_1":c.extra_field_1,"extra_field_2":c.extra_field_2,
            "is_active":c.is_active,"is_default":c.is_default,"notes":c.notes,
            "created_date":c.created_date,"updated_date":c.updated_date}


