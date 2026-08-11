"""
FastAPI routes v6.7 — all /api/* endpoints.
Added: /regime, /portfolio/equity, /market/full, /positions/close, /signals/clear/expired
"""
import json, logging, re, uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional, Union
from app.database import (
    AiDecision, CongressTrade, CryptoDerivativesSnapshot, CryptoLiquidation, InsiderTransaction,
    InstitutionalHolding, IntelligenceIngestionRun, IntelligenceSourceHealth, MarketAsset,
    NewsItem, PlatformConfig, PortfolioSnapshot, Position, ProcessedCongressFiling,
    PsychologySnapshot, SignalEvaluation,
    ThreatEvent, TradeOutcome, TradingSignal, get_db,
)
from lib.learning_engine import get_all_outcomes, get_all_accuracy, get_all_patterns, get_all_regime_stats, get_all_lessons
from app.scheduler import job_status

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/health")
def health():
    return {"status":"ok","time":datetime.now(timezone.utc).isoformat()}


class TradingPreferenceRequest(BaseModel):
    trade_mode: str


@router.get("/preferences/trading")
def get_trading_preference():
    from lib.trading_preferences import get_user_preference
    return get_user_preference()


@router.put("/preferences/trading")
def update_trading_preference(body: TradingPreferenceRequest):
    from lib.trading_preferences import set_trade_mode
    try:
        return set_trade_mode(body.trade_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/auto-paper/summary")
def auto_paper_summary():
    from lib.auto_simulator import get_auto_sim_summary
    return get_auto_sim_summary()


@router.post("/auto-paper/run")
def auto_paper_run():
    from lib.auto_simulator import run_auto_simulator
    return run_auto_simulator()

@router.get("/signals")
def get_signals(status: str = None, limit: int = 150):
    with get_db() as db:
        q = db.query(TradingSignal)
        if status:
            q = q.filter(TradingSignal.status == status)
        else:
            q = q.filter(
                TradingSignal.status.notin_(["Superseded", "Rejected"])
            )
        return [_sig_dict(s) for s in q.order_by(TradingSignal.generated_at.desc()).limit(limit).all()]


@router.get("/signals/performance")
def get_signal_performance(asset_class: str = None, direction: str = None,
                           timeframe: str = None, signal_version: str = None):
    from lib.signal_evaluation import summarize_evaluations
    with get_db() as db:
        query = db.query(SignalEvaluation)
        if asset_class:
            query = query.filter(SignalEvaluation.asset_class == asset_class)
        if direction:
            query = query.filter(SignalEvaluation.direction == direction)
        if timeframe:
            query = query.filter(SignalEvaluation.timeframe == timeframe)
        if signal_version:
            query = query.filter(SignalEvaluation.signal_version == signal_version)
        rows = [_signal_evaluation_dict(row) for row in query.all()]

    grouped = {}
    for field in ("asset_class", "direction", "timeframe", "signal_version"):
        values = sorted({row.get(field) or "Unknown" for row in rows})
        grouped[field] = {
            value: summarize_evaluations([row for row in rows if (row.get(field) or "Unknown") == value])
            for value in values
        }
    return {"summary": summarize_evaluations(rows), "groups": grouped, "evaluations": rows[-100:]}


def _context_terms(signal: dict) -> set[str]:
    symbol = (signal.get("asset_symbol") or "").upper()
    base = symbol.replace("-USD", "").split("/")[0].split("=")[0]
    name = (signal.get("asset_name") or "").upper()
    terms = {term for term in (symbol, base, name) if len(term) >= 3}
    return terms


def _related_signal_context(db, signal: dict) -> tuple[list[dict], list[dict]]:
    terms = _context_terms(signal)
    asset_class = (signal.get("asset_class") or "").lower()
    news_rows = db.query(NewsItem).order_by(NewsItem.created_date.desc()).limit(250).all()
    related_news = []
    for item in news_rows:
        assets = {part.strip().upper() for part in (item.affected_assets or "").split(",") if part.strip()}
        haystack = f"{item.title or ''} {item.summary or ''}".upper()
        direct = bool(terms & assets) or any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in terms)
        class_relevant = asset_class == "crypto" and (item.category or "").lower() == "crypto"
        if direct or class_relevant:
            row = _news_dict(item)
            row["relevance"] = "symbol" if direct else "crypto-market"
            related_news.append(row)
        if len(related_news) >= 15:
            break

    threat_rows = db.query(ThreatEvent).filter(ThreatEvent.status == "Active").order_by(
        ThreatEvent.created_date.desc()
    ).limit(100).all()
    related_threats = []
    trigger_id = signal.get("trigger_event_id")
    for item in threat_rows:
        haystack = f"{item.title or ''} {item.description or ''}".upper()
        direct = bool(trigger_id and item.id == trigger_id) or any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in terms)
        market_wide = (item.severity or "").lower() in ("critical", "high")
        if direct or market_wide:
            row = _threat_dict(item)
            row["relevance"] = "signal-trigger" if trigger_id and item.id == trigger_id else "symbol" if direct else "market-wide"
            related_threats.append(row)
        if len(related_threats) >= 10:
            break
    return related_news, related_threats


@router.get("/signals/{signal_id}/analysis")
def get_signal_analysis(signal_id: str):
    with get_db() as db:
        row = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not row:
            raise HTTPException(404, "Signal not found")
        signal = _sig_dict(row)
        news, threats = _related_signal_context(db, signal)
    try:
        from lib.signal_analysis import build_signal_analysis
        return build_signal_analysis(signal, news, threats)
    except Exception as exc:
        logger.exception("[API] Signal analysis failed for %s", signal_id)
        raise HTTPException(500, str(exc))

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

class NotesRequest(BaseModel):
    notes: str = ""

@router.post("/signals/{signal_id}/notes")
def save_signal_notes(signal_id: str, body: NotesRequest):
    """Trade journal note — freeform, attached to the signal for its whole lifecycle."""
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        sig.notes = body.notes
        sig.updated_date = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

class ExecuteRequest(BaseModel):
    qty: Optional[int] = None

@router.post("/signals/{signal_id}/execute")
def manual_execute(signal_id: str, body: ExecuteRequest = ExecuteRequest()):
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig: raise HTTPException(404)
        if bool(getattr(sig, "paper_mode", False)):
            raise HTTPException(400, "Paper-only signal cannot be sent to Alpaca live execution")
        if getattr(sig, "expires_at", None):
            try:
                expiry = datetime.fromisoformat(sig.expires_at.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    sig.status = "Expired"
                    raise HTTPException(409, "Signal expired; refresh analysis before execution")
            except ValueError:
                pass
        if getattr(sig, "data_quality_score", None) is not None and sig.data_quality_score < 35:
            raise HTTPException(409, "Signal data quality is below the execution threshold")
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
            except Exception as link_err:
                logger.warning(f"[API] Could not link Alpaca order id for signal {signal_id}: {link_err}")
            return {"ok":True,"order":result,"qty":qty,"crypto":crypto}
        except Exception as e:
            raise HTTPException(500, str(e))

_CONFIDENCE_WORDS = {"low": 40, "medium": 60, "moderate": 60, "high": 80, "very high": 90}

class SaveSignalRequest(BaseModel):
    # entry_price/target_price/stop_loss/confidence/key_risks accept loosely-typed
    # values because this model's main caller sends raw LLM-generated JSON
    # (see /analyze's `signal` field) — the model isn't guaranteed to return
    # confidence as a number or key_risks as a single string, so both are
    # normalized below rather than rejected with a 422.
    asset_symbol: Optional[str] = None
    asset_name:   Optional[str] = None
    asset_class:  Optional[str] = "Equity"
    direction:    Optional[str] = "Long"
    confidence:   Optional[Union[int, float, str]] = 65
    timeframe:    Optional[str] = "4H"
    entry_price:  Optional[float] = None
    target_price: Optional[float] = None
    stop_loss:    Optional[float] = None
    reasoning:    Optional[str]   = ""
    key_risks:    Optional[Union[str, list]] = ""
    momentum:     Optional[str]   = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, v):
        if v is None or isinstance(v, (int, float)):
            return v
        s = str(v).strip()
        try:
            return float(s.rstrip("%"))
        except ValueError:
            return _CONFIDENCE_WORDS.get(s.lower(), 65)

    @field_validator("key_risks", mode="before")
    @classmethod
    def _normalize_key_risks(cls, v):
        if isinstance(v, list):
            return "; ".join(str(item) for item in v)
        return v

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
def get_threats(limit: int = 60, confirmation: str = None, min_reliability: float = None):
    with get_db() as db:
        query = db.query(ThreatEvent).filter(ThreatEvent.status == "Active")
        if confirmation:
            query = query.filter(ThreatEvent.confirmation_status == confirmation)
        if min_reliability is not None:
            query = query.filter(ThreatEvent.reliability_score >= min_reliability)
        rows = query.order_by(ThreatEvent.created_date.desc()).limit(min(max(limit, 1), 500)).all()
        return [_threat_dict(t) for t in rows]

@router.get("/news")
def get_news(limit: int = 80, source: str = None, category: str = None,
             asset: str = None, confirmation: str = None,
             min_reliability: float = None, stale: Optional[bool] = None,
             freshness_hours: int = None):
    with get_db() as db:
        query = db.query(NewsItem)
        if source:
            query = query.filter(NewsItem.source == source)
        if category:
            query = query.filter(NewsItem.category == category)
        if asset:
            query = query.filter(NewsItem.affected_assets.contains(asset.upper()))
        if confirmation:
            query = query.filter(NewsItem.confirmation_status == confirmation)
        if min_reliability is not None:
            query = query.filter(NewsItem.reliability_score >= min_reliability)
        # Publication dates were historically stored as both RFC 2822 and ISO strings.
        # Parse them before sorting/filtering so old RFC rows cannot outrank fresh data.
        rows = query.order_by(
            NewsItem.ingested_at.desc(), NewsItem.created_date.desc()
        ).limit(2500).all()
        items = [_news_dict(row) for row in rows]
        if stale is not None:
            items = [item for item in items if bool(item.get("is_stale")) == stale]
        if freshness_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, freshness_hours))
            items = [item for item in items if (
                _parse_datetime(item.get("published_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ) >= cutoff]
        items.sort(
            key=lambda item: _parse_datetime(item.get("published_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return items[:min(max(limit, 1), 500)]


@router.get("/intelligence/sources")
def get_intelligence_sources():
    with get_db() as db:
        rows = db.query(IntelligenceSourceHealth).order_by(
            IntelligenceSourceHealth.consecutive_failures.desc(),
            IntelligenceSourceHealth.source.asc(),
        ).all()
        return [_source_health_dict(row) for row in rows]


@router.get("/intelligence/status")
def get_intelligence_status():
    with get_db() as db:
        sources = db.query(IntelligenceSourceHealth).all()
        latest = db.query(IntelligenceIngestionRun).order_by(
            IntelligenceIngestionRun.finished_at.desc()
        ).first()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            row for row in db.query(NewsItem).all()
            if (_parse_datetime(row.published_at)
                or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
        ]

        source_rows = [_source_health_dict(row) for row in sources]
        healthy = sum(1 for row in source_rows if row["status"] == "healthy")
        failing = sum(1 for row in source_rows if row["status"] == "failing")
        return {
            "status": "degraded" if failing else "healthy" if source_rows else "not_run",
            "source_count": len(source_rows),
            "healthy_sources": healthy,
            "failing_sources": failing,
            "recent_news": len(recent),
            "corroborated_recent": sum(1 for row in recent if row.confirmation_status == "corroborated"),
            "social_unconfirmed_recent": sum(1 for row in recent if row.confirmation_status == "unconfirmed_social"),
            "latest_run": _ingestion_run_dict(latest) if latest else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

def _insider_tx_dict(row):
    return {
        "id": row.id, "accession_number": row.accession_number,
        "issuer_cik": row.issuer_cik, "issuer_name": row.issuer_name, "ticker": row.ticker,
        "owner_cik": row.owner_cik, "owner_name": row.owner_name, "owner_title": row.owner_title,
        "is_director": bool(row.is_director), "is_officer": bool(row.is_officer),
        "is_ten_pct_owner": bool(row.is_ten_pct_owner),
        "security_title": row.security_title, "table": row.table,
        "transaction_date": row.transaction_date, "transaction_code": row.transaction_code,
        "transaction_label": row.transaction_label, "acquired_disposed": row.acquired_disposed,
        "shares": row.shares, "price_per_share": row.price_per_share,
        "total_value": row.total_value, "shares_owned_after": row.shares_owned_after,
        "filing_url": row.filing_url, "filed_at": row.filed_at,
    }


@router.get("/options/{symbol}/summary")
def get_options_summary(symbol: str, current_price: float = None, dte_max: int = 45):
    """Options chain intelligence for one underlying — real chain data from
    Alpaca's options market data API (same broker/credentials as the rest
    of this app, no new vendor). See lib/options_analytics.py for exactly
    what's computed and, just as importantly, what's deliberately excluded
    (no open-interest-based "unusual activity" detection — Alpaca's
    snapshot doesn't expose open interest, so this doesn't approximate it)."""
    from lib.options_analytics import get_chain_summary
    price = current_price
    if price is None:
        with get_db() as db:
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == symbol.upper()).first()
            price = asset.price if asset else None
    if not price:
        raise HTTPException(400, f"No current price available for {symbol} — pass current_price explicitly")
    summary = get_chain_summary(symbol, current_price=price, dte_max=dte_max)
    if not summary:
        raise HTTPException(503, f"Options data unavailable for {symbol} — no chain data returned")
    return summary


@router.get("/orderbook/{symbol}")
def get_orderbook(symbol: str):
    """Latest in-memory Level 2 snapshot for a symbol from both exchanges
    (Binance + Coinbase) — populated by the long-lived WS streams started in
    main.py's lifespan. Used for initial page load; live updates arrive over
    the app's own /ws WebSocket as "orderbook" messages."""
    from lib.orderbook_stream import get_latest_snapshot
    symbol = symbol.upper()
    binance = get_latest_snapshot("binance", symbol)
    coinbase = get_latest_snapshot("coinbase", symbol)
    if not binance and not coinbase:
        raise HTTPException(503, f"No order book data yet for {symbol} — streams may still be connecting")
    return {"symbol": symbol, "binance": binance, "coinbase": coinbase}


@router.get("/alerts")
def get_alerts(hours: int = 24, severity: str = None, limit: int = 200):
    """Cross-module alert feed (lib/alert_engine.py) — insider notable buys,
    large crypto liquidations, kill-switch trips, etc. Complements the
    live WS "alert" broadcast with a page-load-time snapshot."""
    from lib.alert_engine import get_recent_alerts
    return get_recent_alerts(hours=hours, severity=severity, limit=limit)


@router.get("/shortinterest/{symbol}")
def get_short_interest(symbol: str):
    """FINRA consolidated short interest + squeeze-fuel score for one symbol
    — free FINRA Query API, no vendor. This is SEMI-MONTHLY, DELAYED data
    (~8 business day publish lag; see reporting_lag_days), not a live short
    book. Short-interest-as-%-of-float is deliberately absent: FINRA
    publishes no shares-outstanding figure, so it can't be computed
    honestly. See lib/short_interest.py."""
    from lib.short_interest import fetch_symbol_short_interest
    result = fetch_symbol_short_interest(symbol)
    if not result:
        raise HTTPException(404, f"No published short interest for {symbol.upper()} at the latest settlement date")
    return result


@router.get("/shortinterest/squeeze/top")
def get_top_squeeze(limit: int = 25, min_days_to_cover: float = 3.0, exclude_funds: bool = True):
    """Highest squeeze-fuel symbols for the latest published settlement
    date. Excludes OTC, FINRA's 999.99 days-to-cover sentinel, and (by
    default) ETFs/ETNs/SPAC units/warrants — vehicles that structurally
    can't squeeze and would otherwise dominate the ranking. Everything
    dropped is reported in the `excluded` counts rather than hidden."""
    from lib.short_interest import get_top_squeeze_candidates
    result = get_top_squeeze_candidates(
        limit=limit, min_days_to_cover=min_days_to_cover, exclude_funds=exclude_funds
    )
    if not result:
        raise HTTPException(503, "FINRA short interest data unavailable")
    return result


@router.get("/psychology")
def get_market_psychology(persist: bool = True):
    """JARVIS Market Psychology Index — a fear/greed composite computed from
    data this system already collects (VIX history, tracked-universe breadth,
    crypto perp funding, long/short ratio, liquidation skew) rather than
    scraped from a third-party index.

    Components with no data ABSTAIN rather than scoring a neutral 50, so the
    response reports how many of the five actually contributed. See
    lib/market_psychology.py for each mapping and why it was chosen."""
    from lib.market_psychology import (
        breadth_component, compute_psychology_index, compute_rate_of_change,
        funding_component, liquidation_component, long_short_component, vix_component,
    )

    vix_now = vix_history = None
    try:
        from lib.futures_data import fetch_futures_ohlcv, get_cached_futures_price
        # get_cached_futures_price returns a dict ({"symbol", "price", ...}),
        # not a bare float.
        quote = get_cached_futures_price("^VIX")
        vix_now = quote.get("price") if isinstance(quote, dict) else quote
        df = fetch_futures_ohlcv("^VIX", "1D")
        if df is not None and not df.empty:
            vix_history = [float(v) for v in df["close"].tolist()]
            if vix_now is None:
                vix_now = vix_history[-1]
    except Exception as e:
        logger.debug(f"[Psychology] VIX unavailable: {e}")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with get_db() as db:
        changes = [
            r[0] for r in db.query(MarketAsset.change_percent).all() if r[0] is not None
        ]

        # One snapshot per symbol — the newest — so a symbol polled more often
        # doesn't get extra weight in the averages.
        latest_by_symbol: dict = {}
        for row in (
            db.query(CryptoDerivativesSnapshot)
            .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
            .limit(200).all()
        ):
            latest_by_symbol.setdefault(row.symbol, row)
        funding_rates = [r.funding_rate for r in latest_by_symbol.values()]
        ls_ratios = [r.long_short_ratio for r in latest_by_symbol.values()]

        long_usd = short_usd = 0.0
        for liq in db.query(CryptoLiquidation).filter(CryptoLiquidation.liquidated_at >= cutoff).all():
            # notional_usd is computed at ingest; fall back to price*size only
            # if that column is empty on older rows.
            notional = liq.notional_usd if liq.notional_usd is not None else (liq.price or 0) * (liq.size or 0)
            if (liq.pos_side or "").lower() == "long":
                long_usd += notional
            elif (liq.pos_side or "").lower() == "short":
                short_usd += notional

        prior = (
            db.query(PsychologySnapshot)
            .order_by(PsychologySnapshot.created_at.desc()).first()
        )
        prior_score = prior.score if prior else None
        prior_at = prior.created_at if prior else None

    components = {
        "vix": vix_component(vix_now, vix_history),
        "breadth": breadth_component(changes),
        "funding": funding_component(funding_rates),
        "long_short": long_short_component(ls_ratios),
        "liquidations": liquidation_component(long_usd, short_usd),
    }
    result = compute_psychology_index(components)

    hours = None
    if prior_at:
        try:
            hours = (datetime.now(timezone.utc) - datetime.fromisoformat(prior_at)).total_seconds() / 3600
        except ValueError:
            hours = None
    result["rate_of_change"] = compute_rate_of_change(result["score"], prior_score, hours)
    result["computed_at"] = datetime.now(timezone.utc).isoformat()

    if persist and result["score"] is not None:
        with get_db() as db:
            db.add(PsychologySnapshot(
                score=result["score"], label=result["label"],
                components_available=result["components_available"],
                components_json=json.dumps(components, default=str),
            ))
    return result


_CONGRESS_DISCLAIMER = {
    "data_type": "U.S. House Periodic Transaction Reports (STOCK Act), free Clerk of the House data",
    "amounts_are_ranges": (
        "Disclosed amounts are RANGES (e.g. $1,001 - $15,000). Exact transaction "
        "size is never disclosed, and no midpoint is estimated here."
    ),
    "reporting_delay": (
        "Disclosure is delayed by statute — the STOCK Act allows up to 45 days. "
        "filing_delay_days shows the actual gap and is normal, not an irregularity."
    ),
    "interpretation": (
        "These are legally required disclosures. Their presence does not imply "
        "wrongdoing, insider knowledge, or illegality. Trades are frequently made "
        "by financial advisors in managed or blind accounts without the member's "
        "involvement."
    ),
    "coverage": (
        "House only — Senate disclosures use a separate system not ingested here. "
        "Assets disclosed without a ticker (treasuries, bonds, many funds) are "
        "recorded with no symbol rather than having one inferred."
    ),
}


def _congress_trade_dict(t) -> dict:
    return {
        # id is the stable unique key. A single filing can legitimately disclose
        # the same ticker, date, and amount range more than once (separate
        # partial sales), so no combination of the business fields is unique.
        "id": t.id,
        "doc_id": t.doc_id, "member_name": t.member_name, "state_district": t.state_district,
        "chamber": t.chamber, "owner": t.owner, "asset_name": t.asset_name,
        "ticker": t.ticker, "asset_type": t.asset_type,
        "transaction_code": t.transaction_code, "transaction_label": t.transaction_label,
        "transaction_date": t.transaction_date, "notification_date": t.notification_date,
        "filing_date": t.filing_date, "filing_delay_days": t.filing_delay_days,
        "amount_low": t.amount_low, "amount_high": t.amount_high, "amount_text": t.amount_text,
        "pdf_url": t.pdf_url,
    }


@router.get("/congress/trades")
def get_congress_trades(limit: int = 50, ticker: str = None, days: int = 180):
    """Recent congressional stock-trade disclosures. See the disclaimer in the
    response — amounts are ranges, disclosure is delayed by statute, and none
    of this implies wrongdoing."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        q = db.query(CongressTrade).filter(CongressTrade.transaction_date >= cutoff)
        if ticker:
            q = q.filter(CongressTrade.ticker == ticker.upper())
        rows = q.order_by(CongressTrade.transaction_date.desc()).limit(min(max(limit, 1), 200)).all()
        trades = [_congress_trade_dict(t) for t in rows]
        coverage = db.query(ProcessedCongressFiling).count()
    return {
        "trades": trades, "count": len(trades),
        "filings_processed": coverage,
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


@router.get("/congress/activity/top")
def get_congress_top_activity(limit: int = 20, days: int = 180):
    """Most-disclosed tickers, with buy/sell counts and how many distinct
    members disclosed each. Counts are of DISCLOSURES, not dollar flow —
    exact amounts are never disclosed, so summing them is not possible."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).date().isoformat()
    with get_db() as db:
        rows = (
            db.query(CongressTrade)
            .filter(CongressTrade.transaction_date >= cutoff, CongressTrade.ticker.isnot(None))
            .all()
        )
        by_ticker: dict = {}
        for t in rows:
            e = by_ticker.setdefault(t.ticker, {
                "ticker": t.ticker, "purchases": 0, "sales": 0, "other": 0,
                "members": set(), "range_low_total": 0.0, "range_high_total": 0.0,
                "latest_transaction_date": None,
            })
            code = (t.transaction_code or "").upper()
            if code.startswith("P"):
                e["purchases"] += 1
            elif code.startswith("S"):
                e["sales"] += 1
            else:
                e["other"] += 1
            if t.member_name:
                e["members"].add(t.member_name)
            e["range_low_total"] += t.amount_low or 0.0
            e["range_high_total"] += t.amount_high or 0.0
            if not e["latest_transaction_date"] or (t.transaction_date or "") > e["latest_transaction_date"]:
                e["latest_transaction_date"] = t.transaction_date

    results = []
    for e in by_ticker.values():
        total = e["purchases"] + e["sales"] + e["other"]
        results.append({
            **{k: v for k, v in e.items() if k != "members"},
            "member_count": len(e["members"]),
            "disclosure_count": total,
            # Net direction by COUNT of disclosures, not dollars — the data
            # cannot support a dollar-flow figure.
            "net_direction": "net_buying" if e["purchases"] > e["sales"]
                             else "net_selling" if e["sales"] > e["purchases"] else "mixed",
        })
    results.sort(key=lambda r: (-r["disclosure_count"], -r["member_count"]))
    return {
        "tickers": results[:min(max(limit, 1), 100)],
        "window_days": days,
        "note": (
            "Ranked by number of disclosures. range_low_total/range_high_total are "
            "the summed bounds of the disclosed ranges — the true total lies somewhere "
            "between them and cannot be known more precisely."
        ),
        "disclaimer": _CONGRESS_DISCLAIMER,
    }


def _institutional_periods(db, ticker: str | None = None) -> list[str]:
    q = db.query(InstitutionalHolding.period_of_report).distinct()
    if ticker:
        q = q.filter(InstitutionalHolding.ticker == ticker.upper())
    return sorted({r[0] for r in q.all() if r[0]}, reverse=True)


def _prior_quarter_end(period: str) -> str | None:
    """The calendar quarter-end immediately before `period`."""
    try:
        d = datetime.fromisoformat(period).date()
    except ValueError:
        return None
    ends = {(3, 31): (1, 1), (6, 30): (4, 1), (9, 30): (7, 1), (12, 31): (10, 1)}
    if (d.month, d.day) not in ends:
        return None
    quarter_index = (d.month - 1) // 3          # 0..3
    if quarter_index == 0:
        return f"{d.year - 1}-12-31"
    prior_month = quarter_index * 3             # 3, 6, or 9
    last_day = 31 if prior_month == 12 else (31 if prior_month == 3 else 30)
    return f"{d.year}-{prior_month:02d}-{last_day:02d}"


def _select_comparison_periods(periods: list[str]) -> tuple[str | None, str | None]:
    """Pick (current, prior) for quarter-over-quarter comparison.

    The prior period must be the ACTUAL preceding calendar quarter, not merely
    the next-most-recent period on file. Managers file late and amended 13Fs
    for old quarters — without this check a stale filing (observed live: a
    2008-09-30 period sitting alongside 2026-06-30) becomes the comparison
    baseline and every "quarter-over-quarter change" is nonsense."""
    if not periods:
        return None, None
    current = periods[0]
    expected_prior = _prior_quarter_end(current)
    if expected_prior and expected_prior in periods:
        return current, expected_prior
    return current, None


def _holdings_for_period(db, period: str, ticker: str | None = None) -> list[dict]:
    q = db.query(InstitutionalHolding).filter(InstitutionalHolding.period_of_report == period)
    if ticker:
        q = q.filter(InstitutionalHolding.ticker == ticker.upper())
    return [{
        "ticker": h.ticker, "filer_name": h.filer_name, "issuer_name": h.issuer_name,
        "value_usd": h.value_usd, "shares": h.shares,
    } for h in q.all()]


def _institutional_disclaimer(periods: list[str]) -> dict:
    return {
        "data_type": "SEC Form 13F quarterly holdings (free EDGAR data)",
        "caveat": (
            "Quarterly snapshot filed up to 45 days after quarter-end — up to ~4.5 months "
            "stale, and long US-listed equity positions only. 13F never shows short "
            "positions, hedges, cash, or non-US holdings, and cannot see intra-quarter "
            "trading. This is what managers reported holding on the quarter-end date, "
            "not what they are buying now."
        ),
        "periods_ingested": periods,
        "coverage_note": (
            "Coverage builds up from first ingestion — there is no historical backfill, "
            "so quarter-over-quarter comparison requires two ingested quarters."
        ),
    }


@router.get("/institutional/{symbol}")
def get_institutional_holdings(symbol: str):
    """Institutional (13F) holders of one ticker, with quarter-over-quarter
    change when two quarters have been ingested. See lib/sec_13f.py and
    lib/institutional_analytics.py for the full honesty caveats — most
    importantly that this is stale quarterly data, long-only, and never
    evidence of current buying."""
    from lib.institutional_analytics import aggregate_by_ticker, compare_quarters

    ticker = symbol.upper()
    with get_db() as db:
        periods = _institutional_periods(db, ticker)
        if not periods:
            raise HTTPException(404, f"No 13F holdings ingested for {ticker} yet")
        cur_period, prior_period = _select_comparison_periods(periods)
        current = aggregate_by_ticker(_holdings_for_period(db, cur_period, ticker))
        prior = aggregate_by_ticker(_holdings_for_period(db, prior_period, ticker)) if prior_period else {}

    rows = compare_quarters(current, prior)
    row = rows[0] if rows else None
    return {
        "symbol": ticker,
        "current_period": cur_period,
        "prior_period": prior_period,
        "summary": row,
        "holders": current.get(ticker, {}).get("holders", []),
        "disclaimer": _institutional_disclaimer(periods),
    }


@router.get("/institutional/accumulation/top")
def get_institutional_accumulation(limit: int = 25):
    """Tickers ranked by quarter-over-quarter institutional share change.
    Returns an explicit insufficient_history marker (rather than a
    misleading ranking) until two quarters have been ingested."""
    from lib.institutional_analytics import aggregate_by_ticker, compare_quarters

    with get_db() as db:
        periods = _institutional_periods(db)
        if not periods:
            raise HTTPException(503, "No 13F holdings ingested yet")
        cur_period, prior_period = _select_comparison_periods(periods)
        current = aggregate_by_ticker(_holdings_for_period(db, cur_period))
        prior = aggregate_by_ticker(_holdings_for_period(db, prior_period)) if prior_period else {}

    rows = compare_quarters(current, prior)
    return {
        "current_period": cur_period,
        "prior_period": prior_period,
        "insufficient_history": prior_period is None,
        "tickers": rows[:min(max(limit, 1), 100)],
        "disclaimer": _institutional_disclaimer(periods),
    }


@router.get("/opportunities/ranked")
def get_ranked_opportunities(limit: int = 30):
    """JARVIS Opportunity Score: ranks active trade signals by combining the
    existing TA/regime composite_score (lib/signal_scorer.py) with
    smart-money alignment (insider clusters + dark-pool activity for
    equities) and that symbol's historical win rate. See
    lib/signal_fusion.py for the full scoring rationale — every component
    is returned alongside the composite, nothing is hidden behind one
    number. Crypto signals get descriptive derivatives context (funding/OI/
    long-short ratio) rather than a smart-money score — see that module's
    docstring for why funding rate isn't scored as directional "smart
    money." Options are intentionally excluded here: pulling a live chain
    per active signal on every request would mean many synchronous external
    calls per page load; see GET /options/{symbol}/summary for the
    per-symbol version already used in the Signal Analysis Modal."""
    from lib.insider_analytics import cluster_summary
    from lib.finra_ats import get_top_activity
    from lib.signal_fusion import (
        compute_anomaly_flags, compute_dark_pool_component, compute_insider_component,
        compute_opportunity_score, compute_smart_money_alignment,
    )
    from lib.learning_engine import get_all_accuracy

    with get_db() as db:
        signal_rows = (
            db.query(TradingSignal)
            .filter(TradingSignal.status.in_(["Active", "PendingApproval"]))
            .order_by(TradingSignal.generated_at.desc())
            .limit(200)
            .all()
        )
        if not signal_rows:
            return []
        signals = [{
            "id": s.id, "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
            "direction": s.direction, "timeframe": s.timeframe, "composite_score": s.composite_score,
        } for s in signal_rows]

        tickers = {s["asset_symbol"] for s in signals if (s["asset_class"] or "").lower() == "equity"}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        insider_rows = (
            db.query(InsiderTransaction)
            .filter(InsiderTransaction.ticker.in_(tickers), InsiderTransaction.transaction_date >= cutoff)
            .all() if tickers else []
        )
        insider_by_ticker: dict = {}
        for r in insider_rows:
            insider_by_ticker.setdefault(r.ticker, []).append({
                "owner_cik": r.owner_cik, "owner_name": r.owner_name, "is_officer": r.is_officer,
                "transaction_code": r.transaction_code, "total_value": r.total_value,
            })

        crypto_symbols = {s["asset_symbol"].upper().split("/")[0] for s in signals if (s["asset_class"] or "").lower() == "crypto"}
        crypto_snapshots: dict = {}
        for sym in crypto_symbols:
            row = (
                db.query(CryptoDerivativesSnapshot)
                .filter(CryptoDerivativesSnapshot.symbol == sym)
                .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
                .first()
            )
            if row:
                crypto_snapshots[sym] = {
                    "funding_rate": row.funding_rate, "open_interest_usd": row.open_interest_usd,
                    "long_short_ratio": row.long_short_ratio,
                }

        accuracy_by_symbol: dict = {}
        for row in get_all_accuracy():
            existing = accuracy_by_symbol.get(row["symbol"])
            if not existing or (row["total_trades"] or 0) > (existing["total_trades"] or 0):
                accuracy_by_symbol[row["symbol"]] = row

    dark_pool_snapshot = get_top_activity(tier="T1", limit=100) or {}
    dark_pool_by_symbol = {r["symbol"]: r for r in dark_pool_snapshot.get("symbols", [])}

    results = []
    for s in signals:
        symbol = s["asset_symbol"]
        asset_class = (s["asset_class"] or "").lower()
        smart_money = None
        anomaly = None
        crypto_context = None

        if asset_class == "equity":
            txs = insider_by_ticker.get(symbol)
            cluster = cluster_summary(txs) if txs else None
            insider_comp = compute_insider_component(cluster) if cluster and cluster["flags"] else None
            dark_pool_row = dark_pool_by_symbol.get(symbol)
            dark_pool_comp = compute_dark_pool_component(dark_pool_row) if dark_pool_row else None
            smart_money = compute_smart_money_alignment(insider=insider_comp, dark_pool=dark_pool_comp)
            anomaly = compute_anomaly_flags(dark_pool=dark_pool_row)
        elif asset_class == "crypto":
            crypto_context = crypto_snapshots.get(symbol.upper().split("/")[0])

        historical = accuracy_by_symbol.get(symbol)
        opp = compute_opportunity_score(s["composite_score"], s["direction"], smart_money=smart_money, historical=historical)

        results.append({
            "signal_id": s["id"], "symbol": symbol, "asset_class": s["asset_class"], "direction": s["direction"],
            "timeframe": s["timeframe"], "base_composite_score": s["composite_score"],
            "opportunity_score": opp["opportunity_score"], "opportunity_breakdown": opp["breakdown"],
            "smart_money": smart_money, "anomaly": anomaly, "crypto_context": crypto_context,
            "historical": {"total_trades": historical["total_trades"], "win_rate": historical["win_rate"]} if historical else None,
        })

    results.sort(key=lambda r: r["opportunity_score"], reverse=True)
    return results[:min(max(limit, 1), 100)]


@router.get("/crypto/{symbol}/derivatives")
def get_crypto_derivatives(symbol: str, liquidation_hours: int = 24):
    """Perpetual-futures state (funding rate, open interest, long/short
    account ratio) plus recent liquidations — free OKX public data, no
    vendor. OI/price divergence is computed from the two most recent stored
    snapshots (~10 min apart); a brand-new symbol will show it as null until
    a second snapshot exists. See lib/crypto_derivatives.py for why OKX is
    the sole source (Binance derivatives geo-blocked, Bybit CloudFront-blocked
    from this deployment)."""
    from lib.crypto_derivatives import classify_oi_price_action, summarize_liquidations
    base = symbol.upper().split("/")[0]
    with get_db() as db:
        snapshots = (
            db.query(CryptoDerivativesSnapshot)
            .filter(CryptoDerivativesSnapshot.symbol == base)
            .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
            .limit(2).all()
        )
        if not snapshots:
            raise HTTPException(503, f"No derivatives data yet for {base} — job may still be running")
        latest, prev = snapshots[0], (snapshots[1] if len(snapshots) > 1 else None)

        oi_price_action = None
        if prev and prev.open_interest_usd and prev.price and latest.price:
            oi_change_pct = (latest.open_interest_usd - prev.open_interest_usd) / prev.open_interest_usd * 100
            price_change_pct = (latest.price - prev.price) / prev.price * 100
            oi_price_action = classify_oi_price_action(oi_change_pct, price_change_pct)

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, liquidation_hours))).isoformat()
        liquidations = (
            db.query(CryptoLiquidation)
            .filter(CryptoLiquidation.symbol == base, CryptoLiquidation.liquidated_at >= cutoff)
            .order_by(CryptoLiquidation.liquidated_at.desc())
            .limit(200).all()
        )
        liq_dicts = [{
            "side": l.side, "pos_side": l.pos_side, "price": l.price, "size": l.size,
            "notional_usd": l.notional_usd, "liquidated_at": l.liquidated_at,
        } for l in liquidations]

        return {
            "symbol": base,
            "price": latest.price,
            "funding_rate": latest.funding_rate,
            "open_interest_usd": latest.open_interest_usd,
            "long_short_ratio": latest.long_short_ratio,
            "oi_price_action": oi_price_action,
            "fetched_at": latest.fetched_at,
            "liquidations": liq_dicts,
            "liquidations_summary": summarize_liquidations(liq_dicts),
        }


@router.get("/darkpool/top")
def get_darkpool_top(tier: str = "T1", limit: int = 25):
    """Top symbols by off-exchange (ATS/dark pool) share volume for the
    latest available week — free FINRA Off-Exchange Transparency data, no
    vendor. This is DELAYED, WEEKLY-AGGREGATED data (~2-4 week publish lag,
    see reporting_delay_days per row), not real-time order flow — there is
    no free source for individual dark-pool prints."""
    from lib.finra_ats import get_top_activity
    snapshot = get_top_activity(tier=tier, limit=min(max(limit, 1), 100))
    if not snapshot:
        raise HTTPException(503, "FINRA ATS data unavailable")
    return snapshot


@router.get("/darkpool/{symbol}/venues")
def get_darkpool_venues(symbol: str, week_start: str = None):
    """Per-venue (individual dark pool) breakdown for one symbol. Pass
    week_start from a /darkpool/top row to avoid a redundant week-discovery
    lookup; omit it to use the latest available week."""
    from lib.finra_ats import get_symbol_venues
    result = get_symbol_venues(symbol, week_start=week_start)
    if not result:
        raise HTTPException(503, "FINRA ATS data unavailable")
    return result


@router.get("/macro/yield-curve")
def get_yield_curve():
    """US Treasury daily yield curve — free, unauthenticated Treasury.gov
    data (lib/treasury_yields.py), no vendor. Includes the 2s10s and 3m10y
    inversion spreads, the two classic yield-curve recession indicators."""
    from lib.treasury_yields import get_yield_curve_snapshot
    snapshot = get_yield_curve_snapshot()
    if not snapshot:
        raise HTTPException(503, "Treasury yield curve data unavailable")
    return snapshot


@router.get("/macro/fred")
def get_macro_fred():
    """CPI/core CPI, PCE/core PCE, unemployment, nonfarm payrolls, real GDP,
    fed funds rate, jobless claims — free FRED (St. Louis Fed) data. Requires
    the user's own free FRED_API_KEY (lib/fred_client.py); returns a clear
    "not configured" response rather than an error when the key is missing,
    since this integration is opt-in."""
    from lib.fred_client import get_macro_snapshot, is_configured
    if not is_configured():
        return {"configured": False, "readings": None, "fetched_at": None}
    snapshot = get_macro_snapshot()
    if not snapshot:
        raise HTTPException(503, "FRED data unavailable")
    return {"configured": True, **snapshot}


@router.get("/insider/activity")
def get_insider_activity(ticker: str = None, days: int = 30, limit: int = 200):
    """Recent SEC Form 4 insider transactions — free EDGAR data, no vendor.
    Every transaction code is included (not just buys/sells) so a caller can
    see the full picture; see /insider/clusters for the P/S-only signal view."""
    with get_db() as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
        query = db.query(InsiderTransaction).filter(InsiderTransaction.transaction_date >= cutoff)
        if ticker:
            query = query.filter(InsiderTransaction.ticker == ticker.upper())
        rows = query.order_by(InsiderTransaction.transaction_date.desc()).limit(min(max(limit, 1), 500)).all()
        return [_insider_tx_dict(r) for r in rows]


@router.get("/insider/clusters")
def get_insider_clusters(days: int = 14):
    """Tickers where multiple insiders bought/sold, an officer bought, or
    activity was one-directional within the window — computed from real Form 4
    data via lib/insider_analytics.rank_clusters. Flags are descriptive only;
    this never asserts wrongdoing or predicts price direction."""
    from lib.insider_analytics import rank_clusters
    with get_db() as db:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).date().isoformat()
        rows = db.query(InsiderTransaction).filter(
            InsiderTransaction.transaction_date >= cutoff,
            InsiderTransaction.transaction_code.in_(["P", "S"]),
        ).all()
        txs = [_insider_tx_dict(r) for r in rows]
    return {"window_days": days, "transactions_analyzed": len(txs), "clusters": rank_clusters(txs)}


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


@router.get("/ops/error-rate")
def get_error_rate(window_minutes: int = 15):
    from app.request_metrics import error_rate_summary
    return error_rate_summary(window_minutes)


@router.get("/positions/threat-exposure")
def get_positions_threat_exposure():
    """Which currently-held symbols (live + paper) are directly named in an
    active geopolitical threat — reuses the same term-matching logic as
    per-signal threat linking (_context_terms/_related_signal_context),
    scoped to direct symbol/name mentions only so this doesn't just flag
    every position against generic market-wide threats."""
    symbols = set()
    try:
        from lib.alpaca_client import get_positions
        for p in get_positions():
            symbols.add(str(p.symbol).upper())
    except Exception:
        pass
    with get_db() as db:
        from app.database import PaperPosition
        for row in db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all():
            if row[0]:
                symbols.add(row[0].upper())

        threat_rows = db.query(ThreatEvent).filter(ThreatEvent.status == "Active").order_by(
            ThreatEvent.created_date.desc()
        ).limit(200).all()

        exposure = {}
        for sym in symbols:
            terms = _context_terms({"asset_symbol": sym, "asset_name": ""})
            if not terms:
                continue
            matches = []
            for t in threat_rows:
                haystack = f"{t.title or ''} {t.description or ''}".upper()
                if any(re.search(rf"\b{re.escape(term)}\b", haystack) for term in terms):
                    matches.append({"id": t.id, "title": t.title, "severity": t.severity, "country": t.country, "region": t.region})
            if matches:
                exposure[sym] = matches[:5]
    return {"exposure": exposure, "symbols_checked": len(symbols), "symbols_exposed": len(exposure)}


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
            if not sym:
                continue
            # Index by every form: "BTC/USD", "BTC", "BTCUSD"
            for key in [sym, sym.replace("/USD",""), sym.replace("/",""), sym.upper(), sym.lower()]:
                if key and key not in sig_map:
                    sig_map[key] = s

        # Filter out zero-qty and dust positions (Alpaca retains sub-cent crypto leftovers)
        positions = [p for p in positions if abs(float(p.qty or 0)) >= 0.0001 and abs(float(p.market_value or 0)) >= 1.0]
        result = []
        for p in positions:
            sym = str(p.symbol)
            pos_dict = _position_dict(p)
            # Alpaca returns crypto as "BTCUSD", DB stores as "BTC/USD" — try all forms
            sym_slash = (sym[:-3] + "/USD") if (len(sym) > 3 and sym.endswith("USD") and "/" not in sym) else sym
            sig = (sig_map.get(sym) or
                   sig_map.get(sym_slash) or
                   sig_map.get(sym.replace("/USD","")) or
                   sig_map.get(sym.replace("/","")) or
                   sig_map.get(sym + "/USD"))
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
        # Filter out zero-qty and dust positions (Alpaca retains sub-cent crypto leftovers)
        positions = [p for p in positions if abs(float(p.qty or 0)) >= 0.0001 and abs(float(p.market_value or 0)) >= 1.0]
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


class TradingStatusRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = None

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


@router.get("/execution/slippage")
def get_slippage_summary(limit: int = 200):
    """Execution quality: gap between a live signal's intended entry price and
    the broker's actual fill price, recorded the first time each position is
    observed by manage_positions. Paper fills are never included — they always
    fill at the requested price by construction."""
    with get_db() as db:
        rows = db.query(TradingSignal).filter(
            TradingSignal.slippage_pct.is_not(None),
            TradingSignal.paper_mode.is_not(True),
        ).order_by(TradingSignal.fill_recorded_at.desc()).limit(min(max(limit, 1), 1000)).all()
        trades = [{
            "symbol": r.asset_symbol,
            "asset_class": r.asset_class,
            "entry_price": r.entry_price,
            "actual_fill_price": r.actual_fill_price,
            "slippage_pct": r.slippage_pct,
            "fill_recorded_at": r.fill_recorded_at,
        } for r in rows]
    if not trades:
        return {"count": 0, "avg_slippage_pct": None, "median_slippage_pct": None,
                "worst_slippage_pct": None, "trades": []}
    values = sorted(t["slippage_pct"] for t in trades)
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    worst = max(values, key=abs)
    return {
        "count": n,
        "avg_slippage_pct": round(sum(values) / n, 4),
        "median_slippage_pct": round(median, 4),
        "worst_slippage_pct": round(worst, 4),
        "trades": trades,
    }


@router.get("/earnings/watchlist")
def get_earnings_watchlist():
    """Which currently-held or active-signal equity symbols report earnings
    within the next 5 days (Yahoo Finance calendar, no paid API)."""
    from lib.earnings_calendar import get_earnings_this_week
    reporting = get_earnings_this_week()
    with get_db() as db:
        symbols = set()
        for row in db.query(TradingSignal.asset_symbol).filter(TradingSignal.status.in_(["Active", "PendingApproval", "Executed"])).all():
            symbols.add((row[0] or "").upper())
        from app.database import PaperPosition
        for row in db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all():
            symbols.add((row[0] or "").upper())
    at_risk = sorted(s for s in symbols if s.replace("/USD", "") in reporting)
    return {"at_risk_symbols": at_risk, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.get("/performance/r-multiples")
def get_r_multiples(limit: int = 200):
    """R-multiple (realized P&L / initial $ risk) for closed paper trades.
    Joins paper_trades back to paper_positions for the original stop_loss,
    since paper_trades itself doesn't store it."""
    from lib.performance_analytics import compute_r_multiples
    from app.database import PaperTrade, PaperPosition
    with get_db() as db:
        trade_rows = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(limit).all()
        position_ids = [t.position_id for t in trade_rows if t.position_id]
        stop_by_pos = {}
        if position_ids:
            positions = db.query(PaperPosition).filter(PaperPosition.id.in_(position_ids)).all()
            stop_by_pos = {p.id: p.stop_loss for p in positions}
        trades = [{
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "stop_loss": stop_by_pos.get(t.position_id),
            "exit_price": t.exit_price,
            "qty": t.qty,
            "realized_pnl": t.realized_pnl,
            "pnl_pct": t.pnl_pct,
            "close_reason": t.close_reason,
            "closed_at": t.closed_at,
        } for t in trade_rows]
    return compute_r_multiples(trades)


@router.get("/performance/analytics")
def get_performance_analytics(days: int = 90):
    """Real portfolio analytics computed from history: Sharpe ratio and max
    drawdown from daily equity snapshots, plus win-rate/avg P&L broken down
    by originating signal source (watchlist LLM vs ta_fallback vs scanner)."""
    from lib.performance_analytics import (
        daily_equity_curve, compute_max_drawdown, compute_sharpe_ratio, signal_source_breakdown,
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_db() as db:
        snapshots = [
            {"snapshot_at": s.snapshot_at, "equity": s.equity}
            for s in db.query(PortfolioSnapshot)
                .filter(PortfolioSnapshot.snapshot_at >= cutoff)
                .order_by(PortfolioSnapshot.snapshot_at.asc()).all()
        ]

        outcome_rows = db.query(TradeOutcome).filter(TradeOutcome.exited_at >= cutoff).all()
        signal_ids = [o.signal_id for o in outcome_rows if o.signal_id]
        source_by_id = {}
        if signal_ids:
            sigs = db.query(TradingSignal).filter(TradingSignal.id.in_(signal_ids)).all()
            source_by_id = {s.id: (s.signal_source or "watchlist") for s in sigs}
        outcomes = [{
            "signal_source": source_by_id.get(o.signal_id, "unknown"),
            "outcome": o.outcome,
            "pnl_pct": o.pnl_pct,
        } for o in outcome_rows]

    curve = daily_equity_curve(snapshots)
    drawdown = compute_max_drawdown(curve)
    sharpe = compute_sharpe_ratio(curve)

    return {
        "period_days": days,
        "equity_curve_points": len(curve),
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": drawdown["max_drawdown_pct"],
        "drawdown_peak_date": drawdown["peak_date"],
        "drawdown_trough_date": drawdown["trough_date"],
        "trades_analyzed": len(outcomes),
        "by_signal_source": signal_source_breakdown(outcomes),
    }

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
             "scanner_futures":("jobs.scan_opportunities","run_futures")}
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


class TelegramSetupRequest(BaseModel):
    config_id: Optional[str] = ""
    bot_token: Optional[str] = ""
    chat_id: Optional[str] = ""


def _telegram_setup_credentials(body: TelegramSetupRequest) -> tuple[str, str]:
    token = str(body.bot_token or "").strip()
    chat_id = str(body.chat_id or "").strip()
    if body.config_id:
        with get_db() as db:
            cfg = db.query(PlatformConfig).filter(
                PlatformConfig.id == body.config_id,
                PlatformConfig.platform == "telegram",
            ).first()
            if not cfg:
                raise HTTPException(404, "Telegram configuration not found")
            token = token or str(cfg.api_key or "").strip()
            chat_id = chat_id or str(cfg.extra_field_1 or "").strip()
    return token, chat_id


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

def _is_crypto_like_signal(sig) -> bool:
    cls = (getattr(sig, "asset_class", "") or "").strip().lower()
    if cls == "crypto":
        return True
    sym = (getattr(sig, "asset_symbol", "") or "").upper().strip()
    if not sym or sym.endswith(("=F", "=X")):
        return False
    if "/" in sym or sym.endswith("-USD"):
        return True
    return sym.endswith("USD") and sym[:-3] in {
        "BTC", "ETH", "SOL", "XRP", "BNB", "AVAX", "LINK", "DOGE",
        "ADA", "AAVE", "DOT", "ATOM", "SUI", "RENDER", "INJ",
        "NEAR", "OP", "ARB", "MATIC", "UNI", "PEPE", "LTC",
    }


def _is_pending_equity_candidate(sig) -> bool:
    if bool(getattr(sig, "paper_mode", False)):
        return False
    return not _is_crypto_like_signal(sig)


@router.get("/signals/pending")
def get_pending_signals():
    """Get live non-crypto signals queued for the next equity-market session."""
    with get_db() as db:
        rows = db.query(TradingSignal).filter(
            TradingSignal.status == "PendingApproval"
        ).order_by(TradingSignal.confidence.desc()).all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
        return [_sig_dict(s) for s in sigs]

@router.post("/signals/{signal_id}/approve")
def approve_signal(signal_id: str):
    """Approve a pending signal — immediately submit the order to Alpaca."""
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if sig.status != "PendingApproval":
            raise HTTPException(400, f"Signal is {sig.status}, not PendingApproval")
        if bool(getattr(sig, "paper_mode", False)):
            raise HTTPException(400, "Paper-only signal cannot be sent to Alpaca live execution")
        sym_raw = sig.asset_symbol
        entry  = float(sig.entry_price or 0)
        target = float(sig.target_price or 0)
        stop   = float(sig.stop_loss or 0)

    # The broker call runs outside the session above: raising an HTTPException from
    # within that `with get_db()` block rolls back the ENTIRE transaction (including
    # any status write made in the same except block), so a failed submission was
    # silently leaving the signal stuck at PendingApproval forever. Each status write
    # below now commits in its own session, independent of whether we raise after it.
    try:
        from lib.alpaca_client import submit_bracket_order, normalize_symbol, is_crypto, get_account
        sym, crypto = normalize_symbol(sym_raw)
        if not entry or not target or not stop:
            raise ValueError("Signal missing price levels")
        account = get_account()
        buying_power = float(account.buying_power)
        qty = max(1, int(min(1500, buying_power * 0.2) / entry)) if not crypto else round(min(1000, buying_power * 0.1) / entry, 6)
        if qty <= 0:
            raise ValueError(f"Insufficient buying power ${buying_power:.0f}")
        result = submit_bracket_order(symbol=sym, qty=qty, entry_price=entry, take_profit=target, stop_loss=stop)
    except Exception as e:
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if sig:
                sig.status = "Rejected"
                sig.updated_date = datetime.now(timezone.utc).isoformat()
        raise HTTPException(500, str(e))

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if sig:
            sig.status = "Executed"
            sig.updated_date = datetime.now(timezone.utc).isoformat()
    return {"ok": True, "order": result, "qty": qty, "symbol": sym}

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
    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        raise HTTPException(423, f"Live trading is paused: {kill_state.get('paused_reason') or 'manually paused'}")
    from lib.alpaca_client import submit_bracket_order, normalize_symbol, get_account
    account = get_account()
    buying_power = float(account.buying_power)
    now_iso = datetime.now(timezone.utc).isoformat()
    approved = rejected = 0
    with get_db() as db:
        rows = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").order_by(TradingSignal.confidence.desc()).all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
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
        rows = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").all()
        sigs = [s for s in rows if _is_pending_equity_candidate(s)]
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
    try:
        score_breakdown = json.loads(getattr(s, "score_breakdown", None) or "{}")
    except (TypeError, ValueError):
        score_breakdown = {}
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
        "paper_mode":    bool(getattr(s, "paper_mode", False)),
        "paper_direction": getattr(s, "paper_direction", None),
        "trigger_event": getattr(s, "trigger_event", None),
        "trigger_event_id": getattr(s, "trigger_event_id", None),
        "calibrated_confidence": getattr(s, "calibrated_confidence", None),
        "score_breakdown": score_breakdown,
        "data_quality_score": getattr(s, "data_quality_score", None),
        "freshness_score": getattr(s, "freshness_score", None),
        "news_confidence": getattr(s, "news_confidence", None),
        "setup_type": getattr(s, "setup_type", None),
        "invalidation": getattr(s, "invalidation", None),
        "signal_version": getattr(s, "signal_version", None),
        "market_data_at": getattr(s, "market_data_at", None),
        "expires_at": getattr(s, "expires_at", None),
        "trade_horizon": getattr(s, "trade_horizon", None),
        "notes": getattr(s, "notes", None),
    }

def _threat_dict(t):
    return {"id":t.id,"title":t.title,"description":t.description,"event_type":t.event_type,
            "severity":t.severity,"country":t.country,"region":t.region,
            "latitude":getattr(t,"latitude",None),"longitude":getattr(t,"longitude",None),
            "source":t.source,"source_url":t.source_url,"status":t.status,
            "published_at":t.published_at,"created_date":t.created_date,
            "source_kind":getattr(t,"source_kind",None),
            "reliability_score":getattr(t,"reliability_score",None),
            "confirmation_status":getattr(t,"confirmation_status",None),
            "corroboration_count":getattr(t,"corroboration_count",0) or 0,
            "claim_confidence":getattr(t,"claim_confidence",None),
            "cluster_id":getattr(t,"cluster_id",None)}

def _news_dict(n):
    try:
        corroborated_sources = json.loads(getattr(n, "corroborated_sources", None) or "[]")
    except (TypeError, ValueError):
        corroborated_sources = []
    try:
        entities = json.loads(getattr(n, "entities", None) or "{}")
    except (TypeError, ValueError):
        entities = {}
    published_at = _parse_datetime(n.published_at)
    computed_stale = bool(
        published_at and published_at < datetime.now(timezone.utc) - timedelta(hours=72)
    )
    return {"id":n.id,"title":n.title,"summary":n.summary,"source":n.source,"url":n.url,
            "category":n.category,"sentiment":n.sentiment,
            "affected_assets":n.affected_assets.split(",") if n.affected_assets else [],
            "region":n.region,"published_at":n.published_at,"created_date":n.created_date,
            "canonical_url":getattr(n,"canonical_url",None),
            "source_kind":getattr(n,"source_kind",None),"provider":getattr(n,"provider",None),
            "ingested_at":getattr(n,"ingested_at",None),
            "reliability_score":getattr(n,"reliability_score",None),
            "confirmation_status":getattr(n,"confirmation_status",None),
            "corroboration_count":getattr(n,"corroboration_count",0) or 0,
            "corroborated_sources":corroborated_sources,
            "claim_confidence":getattr(n,"claim_confidence",None),
            "is_stale":bool(getattr(n,"is_stale",False)) or computed_stale,"entities":entities,
            "cluster_id":getattr(n,"cluster_id",None)}


def _source_health_dict(row):
    if int(row.consecutive_failures or 0) >= 2:
        status = "failing"
    elif int(row.consecutive_failures or 0) == 1:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "source": row.source, "source_kind": row.source_kind, "provider": row.provider,
        "url": row.url, "reliability_score": row.reliability_score,
        "status": status, "success_count": row.success_count or 0,
        "failure_count": row.failure_count or 0,
        "consecutive_failures": row.consecutive_failures or 0,
        "last_success_at": row.last_success_at, "last_failure_at": row.last_failure_at,
        "last_error": row.last_error, "last_latency_ms": row.last_latency_ms,
        "last_article_count": row.last_article_count or 0, "updated_at": row.updated_at,
    }


def _ingestion_run_dict(row):
    return {
        "id": row.id, "started_at": row.started_at, "finished_at": row.finished_at,
        "status": row.status, "source_count": row.source_count or 0,
        "failed_sources": row.failed_sources or 0, "fetched_count": row.fetched_count or 0,
        "fresh_count": row.fresh_count or 0, "selected_count": row.selected_count or 0,
        "saved_news": row.saved_news or 0, "saved_threats": row.saved_threats or 0,
        "error": row.error,
    }


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _signal_evaluation_dict(row):
    return {
        "signal_id": row.signal_id, "symbol": row.symbol,
        "asset_class": row.asset_class, "direction": row.direction,
        "timeframe": row.timeframe, "signal_version": row.signal_version,
        "generated_at": row.generated_at, "first_bar_at": row.first_bar_at,
        "last_bar_at": row.last_bar_at, "bars_observed": row.bars_observed or 0,
        "entry_price": row.entry_price, "target_price": row.target_price,
        "stop_loss": row.stop_loss, "mfe_pct": row.mfe_pct or 0,
        "mae_pct": row.mae_pct or 0, "outcome": row.outcome,
        "target_hit_at": row.target_hit_at, "stop_hit_at": row.stop_hit_at,
        "data_issue": row.data_issue, "evaluated_at": row.evaluated_at,
    }

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
    # Detect asset class: prefer Alpaca's own asset_class attribute, fall back to heuristics
    # Alpaca returns crypto symbols as e.g. "BTCUSD" (no slash) with asset_class="crypto"
    # Detect crypto: check Alpaca's asset_class attr (may be enum like AssetClass.CRYPTO
    # or string "crypto" / "cryptocurrency"), then symbol heuristics
    try:
        raw_class = str(getattr(p, "asset_class", "") or "").lower()
    except Exception:
        raw_class = ""
    # Alpaca SDK enum stringifies as e.g. "AssetClass.CRYPTO" or just "crypto"
    if "crypto" in raw_class:
        asset_class = "Crypto"
    elif "/" in sym:
        asset_class = "Crypto"
    elif sym.endswith("USD") and len(sym) > 5:
        base = sym[:-3]  # strip "USD"
        if len(base) >= 2 and base.isalpha():
            asset_class = "Crypto"
        else:
            asset_class = "Equity"
    else:
        asset_class = "Equity"
    return {
        "symbol":          sym,
        "qty":             float(p.qty or 0),
        "avg_entry":       float(p.avg_entry_price or 0),
        "market_value":    float(p.market_value or 0),
        "unrealized_pl":   float(p.unrealized_pl or 0),
        "unrealized_plpc": round(plpc, 4),
        # Alpaca SDK's PositionSide is a plain Enum — str() on it yields the
        # repr "PositionSide.LONG", not the clean value. Same defensive
        # lower().split(".")[-1] pattern already used for order/side enums
        # elsewhere in this codebase (e.g. jobs/manage_positions.py).
        "side":            str(p.side).lower().split(".")[-1],
        "asset_class":     asset_class,
        "current_price":   float(p.current_price or 0),
    }

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

def _config_dict(c):
    return {"id":c.id,"key":c.key,"label":c.label,"platform":c.platform,"config_type":c.config_type,
            "api_key":"[REDACTED]" if c.api_key else "",
            "api_secret":"[REDACTED]" if c.api_secret else "","api_url":c.api_url,
            "has_api_key":bool(c.api_key),"has_api_secret":bool(c.api_secret),
            "extra_field_1":c.extra_field_1,"extra_field_2":c.extra_field_2,
            "is_active":c.is_active,"is_default":c.is_default,"notes":c.notes,
            "created_date":c.created_date,"updated_date":c.updated_date}





# ═══════════════════════════════════════════════════════════════
#  Paper Trading Endpoints
# ═══════════════════════════════════════════════════════════════

class PaperOpenRequest(BaseModel):
    symbol:          str
    asset_class:     Optional[str] = "Equity"
    paper_direction: Optional[str] = "Long"   # Long | Long_Leveraged | Short | Short_Leveraged
    entry_price:     Optional[float] = None
    target_price:    Optional[float] = None
    stop_loss:       Optional[float] = None
    signal_id:       Optional[str]   = None

@router.get("/paper/summary")
def get_paper_summary_route():
    """Full paper portfolio summary — positions, trades, equity curve."""
    try:
        from lib.paper_engine import get_paper_summary
        return get_paper_summary()
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/paper/open")
def paper_open(body: PaperOpenRequest):
    """Manually open a paper position."""
    try:
        from lib.paper_engine import open_paper_position
        from app.database import MarketAsset
        price = body.entry_price
        if not price:
            with get_db() as db:
                a = db.query(MarketAsset).filter(MarketAsset.symbol == body.symbol).first()
                if a: price = float(a.price)
        if not price:
            raise HTTPException(400, f"No price available for {body.symbol}")
        signal = {
            "id": body.signal_id, "asset_symbol": body.symbol, "asset_class": body.asset_class,
            "paper_direction": body.paper_direction, "direction": body.paper_direction,
            "entry_price": price, "target_price": body.target_price, "stop_loss": body.stop_loss,
        }
        result = open_paper_position(signal, current_price=price)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/paper/close/{pos_id}")
def paper_close(pos_id: str, price: Optional[float] = None):
    """Close a paper position at current market price."""
    try:
        from lib.paper_engine import close_paper_position
        from app.database import PaperPosition, MarketAsset
        close_price = price
        if not close_price:
            with get_db() as db:
                pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
                if pos:
                    sym = pos.symbol
                    a = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
                    if a: close_price = float(a.price or pos.current_price or pos.entry_price)
        if not close_price:
            raise HTTPException(400, "No close price available")
        return close_paper_position(pos_id, close_price, reason="manual")
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/paper/reset")
def paper_reset():
    """Reset paper portfolio to starting capital ($100k). Wipes all positions and trades."""
    try:
        from app.database import PaperPosition, PaperTrade, PaperPortfolio, new_id, now_iso
        with get_db() as db:
            # Hard delete ALL trades and positions, then recreate a clean portfolio row
            db.query(PaperTrade).delete()
            db.query(PaperPosition).delete()
            db.query(PaperPortfolio).delete()
            db.flush()
            db.add(PaperPortfolio(
                id=new_id(), cash=100_000.0, total_trades=0,
                winning_trades=0, realized_pnl=0.0, updated_at=now_iso()
            ))
        return {"ok": True, "message": "Paper account reset to $100,000", "cash": 100000.0}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/scanner/run")
async def run_scanner(request: Request):
    """Manually trigger the opportunity scanner. mode: pre_market|intraday|crypto|futures|all"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = body.get("mode", "all")
    valid_modes = {"pre_market", "intraday", "crypto", "futures", "all"}
    if mode not in valid_modes:
        return JSONResponse({"error": f"Invalid mode. Use: {valid_modes}"}, status_code=400)

    import threading
    from jobs.scan_opportunities import run as scanner_run
    def _run():
        try:
            scanner_run(mode)
        except Exception as e:
            logger.error(f"[Routes] Scanner run({mode}) failed: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started", "mode": mode, "message": f"Opportunity scanner [{mode}] running in background"}


@router.get("/scanner/status")
def get_scanner_status():
    """Get opportunity scanner job status, broken out per scan mode."""
    try:
        from app.scheduler import job_status
        modes = {
            "pre_market": job_status.get("scanner_premarket", {"status": "unknown"}),
            "intraday":   job_status.get("scanner_intraday", {"status": "unknown"}),
            "crypto":     job_status.get("scanner_crypto", {"status": "unknown"}),
            "futures":    job_status.get("scanner_futures", {"status": "unknown"}),
        }
        return {"scanner": modes}
    except Exception as e:
        return {"error": str(e)}


@router.post("/backtest/run")
async def run_backtest_endpoint(request: Request):
    """Kick off a historical, no-LLM backtest in a background thread. Body:
    {symbols: list[str], start_date: str, end_date: str,
     timeframes: list[str] = None, trade_mode: str = "longer"}
    Returns immediately with a run_id; poll GET /api/backtest/{run_id}."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    symbols = body.get("symbols") or []
    start_date = body.get("start_date")
    end_date = body.get("end_date")
    timeframes = body.get("timeframes")
    trade_mode = body.get("trade_mode", "longer")

    if not isinstance(symbols, list) or not symbols:
        return JSONResponse({"error": "symbols must be a non-empty list"}, status_code=400)
    MAX_BACKTEST_SYMBOLS = 10
    if len(symbols) > MAX_BACKTEST_SYMBOLS:
        return JSONResponse(
            {"error": f"Too many symbols — max {MAX_BACKTEST_SYMBOLS} per backtest run"},
            status_code=400,
        )
    if not start_date or not end_date:
        return JSONResponse({"error": "start_date and end_date are required"}, status_code=400)

    from app.database import BacktestRun, new_id, now_iso

    run_id = new_id()
    with get_db() as db:
        db.add(BacktestRun(
            id=run_id,
            symbols=json.dumps(symbols),
            timeframes=json.dumps(timeframes) if timeframes else json.dumps([]),
            trade_mode=trade_mode,
            start_date=start_date,
            end_date=end_date,
            status="running",
            created_at=now_iso(),
        ))

    import threading
    def _run():
        from app.database import BacktestRun as _BacktestRun
        try:
            from lib.backtester import run_backtest
            result = run_backtest(
                symbols=symbols, start_date=start_date, end_date=end_date,
                timeframes=timeframes, trade_mode=trade_mode,
            )
            with get_db() as db:
                row = db.query(_BacktestRun).filter(_BacktestRun.id == run_id).first()
                if row:
                    row.status = "completed"
                    row.result_json = json.dumps(result, default=str)
                    row.finished_at = now_iso()
        except Exception as e:
            logger.error(f"[Routes] Backtest run {run_id} failed: {e}")
            try:
                with get_db() as db:
                    row = db.query(_BacktestRun).filter(_BacktestRun.id == run_id).first()
                    if row:
                        row.status = "failed"
                        row.error = str(e)
                        row.finished_at = now_iso()
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"run_id": run_id, "status": "started"}


@router.get("/backtest/{run_id}")
def get_backtest_run(run_id: str):
    """Return a backtest run's status, and its parsed result once completed."""
    from app.database import BacktestRun
    with get_db() as db:
        row = db.query(BacktestRun).filter(BacktestRun.id == run_id).first()
        if not row:
            raise HTTPException(404, "Backtest run not found")
        out = {
            "id": row.id,
            "symbols": json.loads(row.symbols) if row.symbols else [],
            "timeframes": json.loads(row.timeframes) if row.timeframes else [],
            "trade_mode": row.trade_mode,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "status": row.status,
            "error": row.error,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }
        if row.status == "completed" and row.result_json:
            try:
                out["result"] = json.loads(row.result_json)
            except Exception:
                out["result"] = None
        return out


@router.get("/backtest")
def list_backtest_runs():
    """List recent backtest runs (light — no full result payload)."""
    from app.database import BacktestRun
    with get_db() as db:
        rows = (
            db.query(BacktestRun)
            .order_by(BacktestRun.created_at.desc())
            .limit(50)
            .all()
        )
        return {
            "runs": [
                {
                    "id": r.id,
                    "symbols": json.loads(r.symbols) if r.symbols else [],
                    "trade_mode": r.trade_mode,
                    "start_date": r.start_date,
                    "end_date": r.end_date,
                    "status": r.status,
                    "created_at": r.created_at,
                    "finished_at": r.finished_at,
                }
                for r in rows
            ]
        }


@router.post("/paper/run-mtm")
def paper_run_mtm():
    """Manually trigger mark-to-market cycle."""
    try:
        from lib.paper_engine import mark_to_market
        with get_db() as db:
            assets = db.query(MarketAsset).all()
            prices = {a.symbol: float(a.price) for a in assets if a.price}
        result = mark_to_market(prices)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/paper/debug")
def paper_debug():
    """Debug endpoint — returns raw DB counts and first few records for diagnosis."""
    try:
        from app.database import PaperPosition, PaperTrade, PaperPortfolio
        with get_db() as db:
            portfolio = db.query(PaperPortfolio).first()
            all_positions = db.query(PaperPosition).all()
            open_positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            all_trades = db.query(PaperTrade).all()
            
            return {
                "portfolio": {
                    "cash": float(portfolio.cash) if portfolio else None,
                    "total_trades": portfolio.total_trades if portfolio else None,
                    "winning_trades": portfolio.winning_trades if portfolio else None,
                    "realized_pnl": float(portfolio.realized_pnl) if portfolio else None,
                } if portfolio else None,
                "position_counts": {
                    "total": len(all_positions),
                    "open": len(open_positions),
                    "by_status": {s: sum(1 for p in all_positions if p.status == s) 
                                  for s in set(p.status for p in all_positions)},
                },
                "trade_count": len(all_trades),
                "sample_open_positions": [
                    {
                        "id": p.id, "symbol": p.symbol, "status": p.status,
                        "direction": p.direction, "side": p.side,
                        "entry_price": float(p.entry_price) if p.entry_price else None,
                        "qty": float(p.qty) if p.qty else None,
                        "margin_used": float(p.margin_used) if p.margin_used else None,
                        "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl is not None else None,
                        "opened_at": p.opened_at,
                    }
                    for p in open_positions[:5]
                ],
                "sample_trades": [
                    {
                        "id": t.id, "symbol": t.symbol, "direction": t.direction,
                        "realized_pnl": float(t.realized_pnl) if t.realized_pnl else None,
                        "close_reason": t.close_reason, "closed_at": t.closed_at,
                    }
                    for t in all_trades[:5]
                ],
            }
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/paper/positions")
def get_paper_positions(status: str = "Open"):
    """List paper positions filtered by status."""
    try:
        from app.database import PaperPosition
        with get_db() as db:
            q = db.query(PaperPosition)
            if status != "all": q = q.filter(PaperPosition.status == status)
            positions = q.order_by(PaperPosition.opened_at.desc()).limit(100).all()
            return [
                {
                    "id": p.id, "symbol": p.symbol, "asset_class": p.asset_class,
                    "direction": p.direction, "side": p.side, "leverage": p.leverage,
                    "qty": float(p.qty), "entry_price": float(p.entry_price),
                    "current_price": float(p.current_price or p.entry_price),
                    "target_price": float(p.target_price or 0),
                    "stop_loss": float(p.stop_loss or 0),
                    "notional": float(p.notional or 0),
                    "unrealized_pnl": float(p.unrealized_pnl or 0),
                    "unrealized_pct": float(p.unrealized_pct or 0),
                    "status": p.status, "opened_at": p.opened_at,
                }
                for p in positions
            ]
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/paper/trades")
def get_paper_trades(limit: int = 100):
    """List completed paper trades."""
    try:
        from app.database import PaperTrade
        with get_db() as db:
            trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(limit).all()
            return [
                {
                    "id": t.id, "symbol": t.symbol, "direction": t.direction,
                    "leverage": t.leverage, "entry_price": float(t.entry_price),
                    "exit_price": float(t.exit_price),
                    "realized_pnl": round(float(t.realized_pnl), 2),
                    "pnl_pct": round(float(t.pnl_pct), 2),
                    "close_reason": t.close_reason, "opened_at": t.opened_at, "closed_at": t.closed_at,
                    "asset_class": t.asset_class,
                }
                for t in trades
            ]
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/signals/{signal_id}/paper-execute")
def paper_execute_signal(signal_id: str, direction: str = "Long"):
    """Send an existing signal to the paper engine with specified direction."""
    try:
        from lib.paper_engine import open_paper_position
        from app.database import MarketAsset
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if not sig: raise HTTPException(404, "Signal not found")
            sym = sig.asset_symbol
            a = db.query(MarketAsset).filter(MarketAsset.symbol == sym).first()
            price = float(a.price) if a and a.price else float(sig.entry_price or 0)
            sig_data = {
                "id": sig.id, "asset_symbol": sym, "asset_class": sig.asset_class,
                "paper_direction": direction, "entry_price": price,
                "target_price": sig.target_price, "stop_loss": sig.stop_loss,
            }

        result = open_paper_position(sig_data, current_price=price)
        if "error" in result:
            raise HTTPException(400, result["error"])
        return result
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ── AI Decision Log ────────────────────────────────────────────────────────────

def log_decision(source: str, action: str, reasoning: str,
                 symbol: str = None, price: float = None,
                 pnl_pct: float = None, score: float = None,
                 thinking: bool = True):
    """Persist an AI decision to the ai_decisions table using raw SQL for reliability.
    thinking=True → full chain-of-thought was used. thinking=False → /no_think fast path.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)
    try:
        from app.database import engine
        from sqlalchemy import text as _text
        with engine.begin() as conn:
            # Self-heal: ensure table exists before every write
            conn.execute(_text("""
                CREATE TABLE IF NOT EXISTS ai_decisions (
                    id         TEXT PRIMARY KEY,
                    source     TEXT,
                    symbol     TEXT,
                    action     TEXT,
                    reasoning  TEXT,
                    price      REAL,
                    pnl_pct    REAL,
                    score      REAL,
                    thinking   INTEGER DEFAULT 1,
                    created_at TEXT
                )
            """))
            # Self-heal: add thinking column if missing (existing DBs)
            try:
                conn.execute(_text("ALTER TABLE ai_decisions ADD COLUMN thinking INTEGER DEFAULT 1"))
            except Exception:
                pass  # Column already exists

            conn.execute(_text("""
                INSERT INTO ai_decisions (id, source, symbol, action, reasoning, price, pnl_pct, score, thinking, created_at)
                VALUES (:id, :source, :symbol, :action, :reasoning, :price, :pnl_pct, :score, :thinking, :created_at)
            """), {
                "id":         str(__import__("uuid").uuid4()),
                "source":     source,
                "symbol":     symbol,
                "action":     action,
                "reasoning":  (reasoning or "")[:2000],
                "price":      price,
                "pnl_pct":    pnl_pct,
                "score":      score,
                "thinking":   1 if thinking else 0,
                "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            })
        _logger.debug(f"[log_decision] Saved: {source} | {action} | {symbol}")
    except Exception as e:
        _logger.warning(f"[log_decision] Failed to save: {e}", exc_info=True)


@router.get("/decisions")
def get_decisions(limit: int = 200):
    """Return recent AI decisions newest-first — raw SQL so it works even if table is brand new."""
    from app.database import engine
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        conn.execute(_text("""
            CREATE TABLE IF NOT EXISTS ai_decisions (
                id TEXT PRIMARY KEY, source TEXT, symbol TEXT, action TEXT,
                reasoning TEXT, price REAL, pnl_pct REAL, score REAL,
                thinking INTEGER DEFAULT 1, created_at TEXT
            )
        """))
        try:
            conn.execute(_text("ALTER TABLE ai_decisions ADD COLUMN thinking INTEGER DEFAULT 1"))
        except Exception:
            pass
        rows = conn.execute(_text(
            "SELECT id, source, symbol, action, reasoning, price, pnl_pct, score, thinking, created_at "
            "FROM ai_decisions ORDER BY created_at DESC LIMIT :lim"
        ), {"lim": limit}).fetchall()
    return [
        {"id": r[0], "source": r[1], "symbol": r[2], "action": r[3],
         "reasoning": r[4], "price": r[5], "pnl_pct": r[6], "score": r[7],
         "thinking": r[8] if r[8] is not None else 1, "created_at": r[9]}
        for r in rows
    ]


@router.delete("/decisions/clear")
def clear_decisions():
    """Clear all AI decision log entries."""
    from app.database import engine
    from sqlalchemy import text as _text
    with engine.begin() as conn:
        conn.execute(_text("CREATE TABLE IF NOT EXISTS ai_decisions (id TEXT PRIMARY KEY, source TEXT, symbol TEXT, action TEXT, reasoning TEXT, price REAL, pnl_pct REAL, score REAL, created_at TEXT)"))
        count = conn.execute(_text("SELECT COUNT(*) FROM ai_decisions")).scalar()
        conn.execute(_text("DELETE FROM ai_decisions"))
    return {"ok": True, "deleted": count}

# ── Learning Engine — Tier 1 & 2 ─────────────────────────────────────────────

@router.get("/learning/outcomes")
def get_outcomes(limit: int = 200, paper: str = "false"):
    """Return closed trade outcomes for the performance log.
    paper: 'true'=paper only, 'false'=live only, 'all'=both
    """
    if paper == "all":
        paper_mode = None  # None = no filter
    elif paper in ("true", "1", "yes"):
        paper_mode = True
    else:
        paper_mode = False
    return get_all_outcomes(limit=limit, paper_mode=paper_mode)

@router.get("/learning/accuracy")
def get_accuracy():
    """Return per-symbol signal accuracy stats."""
    return get_all_accuracy()

@router.get("/learning/summary")
def get_learning_summary(paper: str = "live"):
    """Return a portfolio-level learning summary.
    paper: 'live' = live only, 'paper' = paper only, 'all' = combined
    """
    from app.database import engine
    from sqlalchemy import text as _text
    if paper == "paper":
        where = "WHERE paper_mode = 1"
    elif paper == "all":
        where = ""
    else:
        where = "WHERE paper_mode = 0"
    with engine.connect() as conn:
        rows = conn.execute(_text(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                ROUND(AVG(pnl_pct),3) as avg_pnl,
                ROUND(AVG(hold_duration_m),1) as avg_hold_min,
                ROUND(MAX(pnl_pct),3) as best_trade,
                ROUND(MIN(pnl_pct),3) as worst_trade,
                SUM(pnl_usd) as total_pnl_usd
            FROM trade_outcomes {where}
        """)).fetchone()
    if not rows or rows[0] == 0:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_pnl": 0, "avg_hold_min": 0, "best_trade": 0,
                "worst_trade": 0, "total_pnl_usd": 0}
    total = rows[0] or 0
    wins  = rows[1] or 0
    return {
        "total": total, "wins": wins, "losses": rows[2] or 0,
        "win_rate": round(wins / total, 4) if total else 0,
        "avg_pnl": rows[3], "avg_hold_min": rows[4],
        "best_trade": rows[5], "worst_trade": rows[6],
        "total_pnl_usd": round(rows[7] or 0, 2),
    }

@router.get("/learning/patterns")
def get_patterns():
    """Return Tier 3 pattern memory — TA setup win/loss history."""
    return get_all_patterns()

@router.get("/learning/regimes")
def get_regimes():
    """Return Tier 4 regime performance — win rates per market regime."""
    return get_all_regime_stats()

@router.get("/learning/lessons")
def get_lessons(limit: int = 50):
    """Return Tier 5 LLM reasoning audit lessons."""
    return get_all_lessons(limit=limit)

@router.post("/learning/seed-test")
def seed_test_outcome():
    """Dev helper: inject a fake closed trade so the Learning Engine tables populate and the UI can be verified."""
    from lib.learning_engine import record_trade_outcome
    import random, datetime, uuid
    symbols = ["AAPL", "NVDA", "SPY", "BTC/USD", "ETH/USD", "GC=F"]
    sym = random.choice(symbols)
    direction = random.choice(["BUY", "SELL"])
    entry = round(random.uniform(50, 400), 2)
    pnl_pct = round(random.uniform(-8, 15), 2)
    exit_p = round(entry * (1 + pnl_pct / 100), 2)
    outcome = "WIN" if pnl_pct > 0.5 else "LOSS" if pnl_pct < -0.5 else "BREAKEVEN"
    entered_at = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=random.randint(1,48))).isoformat()
    exited_at  = datetime.datetime.now(datetime.timezone.utc).isoformat()
    hold_min   = round(random.uniform(15, 600), 1)
    exit_reasons = ["TAKE_PROFIT", "HARD_STOP", "LLM_EXIT", "TIMEOUT", "MANUAL"]
    regimes = ["Risk-On Bull", "Range-Bound", "Bear / Risk-Off", "Neutral"]
    record_trade_outcome(
        signal_id=str(uuid.uuid4()),
        symbol=sym,
        asset_class="crypto" if "/" in sym else ("futures" if "=" in sym else "equity"),
        direction=direction,
        timeframe="4H",
        entry_price=entry,
        exit_price=exit_p,
        qty=round(random.uniform(1, 20), 4),
        pnl_usd=round(entry * random.uniform(0.01, 0.5) * (1 if outcome == "WIN" else -1), 2),
        pnl_pct=pnl_pct,
        outcome=outcome,
        exit_reason=random.choice(exit_reasons),
        hold_duration_m=hold_min,
        signal_confidence=random.randint(55, 92),
        signal_score=round(random.uniform(60, 95), 1),
        signal_reasoning=f"Test seed: {sym} showed momentum setup on 4H.",
        ta_summary="RSI oversold, MACD crossover, above VWAP",
        market_regime=random.choice(regimes),
        paper_mode=True,
        entered_at=entered_at,
        exited_at=exited_at,
    )
    return {"ok": True, "seeded": sym, "outcome": outcome, "pnl_pct": pnl_pct}

# ─── Futures Data ─────────────────────────────────────────────────────────────


@router.post("/learning/backfill-paper")
def backfill_paper_outcomes():
    """One-time backfill: copy all closed PaperTrades into trade_outcomes so learning engine can process them."""
    from lib.learning_engine import backfill_paper_trades
    result = backfill_paper_trades()
    return result

@router.get("/futures/prices")
def get_futures_prices(paper_only: bool = False):
    """Return latest futures/forex/commodity prices."""
    try:
        from lib.futures_data import fetch_all_futures_prices, PAPER_FUTURES, FUTURES_UNIVERSE
        syms = PAPER_FUTURES if paper_only else list(FUTURES_UNIVERSE.keys())
        return fetch_all_futures_prices(syms)
    except Exception as e:
        logger.error(f"[API] /futures/prices: {e}")
        return {}

@router.get("/futures/news")
def get_futures_news(limit: int = 30):
    """Return recent futures/commodity/forex news articles."""
    try:
        from lib.futures_data import fetch_futures_news
        return fetch_futures_news(max_total=limit)
    except Exception as e:
        logger.error(f"[API] /futures/news: {e}")
        return []

@router.get("/futures/universe")
def get_futures_universe():
    """Return the full futures symbol registry."""
    try:
        from lib.futures_data import FUTURES_UNIVERSE, CATEGORY_ICONS
        return [
            {"symbol": sym, **meta, "icon": CATEGORY_ICONS.get(meta.get("category",""), "📊")}
            for sym, meta in FUTURES_UNIVERSE.items()
        ]
    except Exception as e:
        logger.error(f"[API] /futures/universe: {e}")
        return []



