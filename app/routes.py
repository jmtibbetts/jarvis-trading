"""
FastAPI routes — all /api/* endpoints.
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset, Position, PlatformConfig
from app.scheduler import job_status

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Health ─────────────────────────────────────────────────────────────────────
@router.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

# ── Signals ────────────────────────────────────────────────────────────────────
@router.get("/signals")
def get_signals(status: str = None, limit: int = 50):
    with get_db() as db:
        q = db.query(TradingSignal)
        if status:
            q = q.filter(TradingSignal.status == status)
        signals = q.order_by(TradingSignal.generated_at.desc()).limit(limit).all()
        return [_sig_dict(s) for s in signals]

@router.delete("/signals/{signal_id}")
def delete_signal(signal_id: str):
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        db.delete(sig)
    return {"ok": True}

@router.post("/signals/{signal_id}/execute")
def manual_execute(signal_id: str):
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        try:
            from lib.alpaca_client import submit_bracket_order
            result = submit_bracket_order(
                symbol=sig.asset_symbol,
                qty=max(1, int(1000 / (sig.entry_price or 100))),
                entry_price=sig.entry_price,
                take_profit=sig.target_price,
                stop_loss=sig.stop_loss
            )
            sig.status = 'Executed'
            sig.updated_date = datetime.now(timezone.utc).isoformat()
            return {"ok": True, "order": result}
        except Exception as e:
            raise HTTPException(500, str(e))

# ── Threats ────────────────────────────────────────────────────────────────────
@router.get("/threats")
def get_threats(limit: int = 50):
    with get_db() as db:
        threats = db.query(ThreatEvent).filter(
            ThreatEvent.status == 'Active'
        ).order_by(ThreatEvent.published_at.desc()).limit(limit).all()
        return [_threat_dict(t) for t in threats]

# ── News ───────────────────────────────────────────────────────────────────────
@router.get("/news")
def get_news(limit: int = 50):
    with get_db() as db:
        news = db.query(NewsItem).order_by(
            NewsItem.published_at.desc()
        ).limit(limit).all()
        return [_news_dict(n) for n in news]

# ── Market Assets ──────────────────────────────────────────────────────────────
@router.get("/market")
def get_market():
    with get_db() as db:
        assets = db.query(MarketAsset).order_by(MarketAsset.symbol).all()
        return [_asset_dict(a) for a in assets]

# ── Positions ──────────────────────────────────────────────────────────────────
@router.get("/positions")
def get_positions_live():
    try:
        from lib.alpaca_client import get_positions, get_account
        positions = get_positions()
        account   = get_account()
        return {
            "positions": [_position_dict(p) for p in positions],
            "account": {
                "equity":    float(account.equity),
                "cash":      float(account.cash),
                "buying_power": float(account.buying_power),
                "day_trade_count": int(account.daytrade_count or 0)
            }
        }
    except Exception as e:
        raise HTTPException(500, f"Alpaca error: {e}")

# ── Jobs ───────────────────────────────────────────────────────────────────────
@router.get("/jobs/status")
def jobs_status():
    return job_status

@router.post("/jobs/{job_name}/trigger")
def trigger_job(job_name: str):
    from app.scheduler import make_job_runner
    job_map = {
        'market':    'jobs.fetch_market_data',
        'threats':   'jobs.fetch_threat_news',
        'signals':   'jobs.generate_signals',
        'execute':   'jobs.execute_signals',
        'positions': 'jobs.manage_positions',
        'telegram':  'jobs.telegram_bot',
    }
    if job_name not in job_map:
        raise HTTPException(404, f"Unknown job: {job_name}")
    import importlib, threading
    mod = importlib.import_module(job_map[job_name])
    runner = make_job_runner(job_name, mod.run)
    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "job": job_name, "status": "triggered"}

# ── Settings ───────────────────────────────────────────────────────────────────
@router.get("/settings")
def get_settings():
    with get_db() as db:
        configs = db.query(PlatformConfig).all()
        return [_config_dict(c) for c in configs]

class ConfigCreate(BaseModel):
    label: str
    platform: str
    config_type: Optional[str] = 'api'
    api_key: Optional[str] = ''
    api_secret: Optional[str] = ''
    api_url: Optional[str] = ''
    extra_field_1: Optional[str] = ''
    extra_field_2: Optional[str] = ''
    is_active: Optional[bool] = True
    is_default: Optional[bool] = False
    notes: Optional[str] = ''

@router.post("/settings")
def create_setting(body: ConfigCreate):
    import uuid
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cfg = PlatformConfig(
            id=str(uuid.uuid4()),
            key=f"{body.platform}_{body.label}_{now[:10]}",
            label=body.label,
            platform=body.platform,
            config_type=body.config_type,
            api_key=body.api_key,
            api_secret=body.api_secret,
            api_url=body.api_url,
            extra_field_1=body.extra_field_1,
            extra_field_2=body.extra_field_2,
            is_active=body.is_active,
            is_default=body.is_default,
            notes=body.notes,
            created_date=now, updated_date=now
        )
        db.add(cfg)
        return _config_dict(cfg)

@router.put("/settings/{cfg_id}")
def update_setting(cfg_id: str, body: ConfigCreate):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id == cfg_id).first()
        if not cfg:
            raise HTTPException(404)
        for k, v in body.dict().items():
            if hasattr(cfg, k) and v is not None:
                setattr(cfg, k, v)
        cfg.updated_date = now
        return _config_dict(cfg)

@router.delete("/settings/{cfg_id}")
def delete_setting(cfg_id: str):
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id == cfg_id).first()
        if not cfg:
            raise HTTPException(404)
        db.delete(cfg)
    return {"ok": True}

@router.post("/settings/{cfg_id}/set-default")
def set_default(cfg_id: str):
    with get_db() as db:
        cfg = db.query(PlatformConfig).filter(PlatformConfig.id == cfg_id).first()
        if not cfg:
            raise HTTPException(404)
        # Clear other defaults for same platform
        others = db.query(PlatformConfig).filter(
            PlatformConfig.platform == cfg.platform,
            PlatformConfig.id != cfg_id
        ).all()
        for o in others:
            o.is_default = False
        cfg.is_default = True
        return {"ok": True}

# ── Alpaca Proxy ───────────────────────────────────────────────────────────────
@router.get("/alpaca/orders")
def get_orders():
    try:
        from lib.alpaca_client import get_open_orders
        orders = get_open_orders()
        return [{'id': str(o.id), 'symbol': str(o.symbol),
                 'qty': float(o.qty or 0), 'side': str(o.side),
                 'status': str(o.status), 'type': str(o.order_type)} for o in orders]
    except Exception as e:
        raise HTTPException(500, str(e))

@router.delete("/alpaca/orders/{order_id}")
def cancel_order(order_id: str):
    try:
        from lib.alpaca_client import get_trading_client
        get_trading_client().cancel_order_by_id(order_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Analyze (manual scanner) ───────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    symbol: str
    timeframes: Optional[list] = ['1H', '4H', '1D']

@router.post("/analyze")
def analyze_symbol_endpoint(body: AnalyzeRequest):
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import analyze_symbol, build_ta_prompt_block
        bars = fetch_multi_timeframe(body.symbol, body.timeframes)
        ta = analyze_symbol(bars)
        prompt_block = build_ta_prompt_block(body.symbol, ta)
        return {"symbol": body.symbol, "ta": ta, "prompt_block": prompt_block}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Serializers ────────────────────────────────────────────────────────────────
def _sig_dict(s):
    return {
        'id': s.id, 'asset_symbol': s.asset_symbol, 'asset_name': s.asset_name,
        'asset_class': s.asset_class, 'direction': s.direction,
        'confidence': s.confidence, 'timeframe': s.timeframe,
        'reasoning': s.reasoning, 'entry_price': s.entry_price,
        'target_price': s.target_price, 'stop_loss': s.stop_loss,
        'key_risks': s.key_risks, 'momentum': s.momentum,
        'status': s.status, 'generated_at': s.generated_at,
        'signal_source': s.signal_source
    }

def _threat_dict(t):
    return {
        'id': t.id, 'title': t.title, 'description': t.description,
        'event_type': t.event_type, 'severity': t.severity,
        'country': t.country, 'region': t.region,
        'source': t.source, 'source_url': t.source_url,
        'status': t.status, 'published_at': t.published_at
    }

def _news_dict(n):
    return {
        'id': n.id, 'title': n.title, 'summary': n.summary,
        'source': n.source, 'url': n.url, 'category': n.category,
        'sentiment': n.sentiment, 'affected_assets': n.affected_assets,
        'region': n.region, 'published_at': n.published_at
    }

def _asset_dict(a):
    return {
        'id': a.id, 'symbol': a.symbol, 'name': a.name,
        'asset_class': a.asset_class, 'price': a.price,
        'change_percent': a.change_percent, 'volume': a.volume,
        'last_updated': a.last_updated
    }

def _position_dict(p):
    return {
        'symbol': str(p.symbol), 'qty': float(p.qty or 0),
        'avg_entry': float(p.avg_entry_price or 0),
        'market_value': float(p.market_value or 0),
        'unrealized_pl': float(p.unrealized_pl or 0),
        'unrealized_plpc': float(p.unrealized_plpc or 0) * 100,
        'side': str(p.side)
    }

def _config_dict(c):
    return {
        'id': c.id, 'key': c.key, 'label': c.label,
        'platform': c.platform, 'config_type': c.config_type,
        'api_key': c.api_key, 'api_secret': '***' if c.api_secret else '',
        'api_url': c.api_url,
        'extra_field_1': c.extra_field_1, 'extra_field_2': c.extra_field_2,
        'is_active': c.is_active, 'is_default': c.is_default,
        'notes': c.notes, 'created_date': c.created_date
    }
