"""Shared preamble and helpers for the domain routers — extracted
verbatim from the monolithic app/routes.py (Phase 7). Order preserved;
every helper, model and constant the routes closed over lives here.
"""
"""
FastAPI routes v6.7 — all /api/* endpoints.
Added: /regime, /portfolio/equity, /market/full, /positions/close, /signals/clear/expired
"""
import json, logging, re, threading, uuid
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional, Union
from app.database import (
    AiDecision, Alert, CongressTrade, CryptoDerivativesSnapshot, CryptoLiquidation, InsiderTransaction,
    InstitutionalHolding, IntelligenceIngestionRun, IntelligenceSourceHealth, MarketAsset,
    IpoFiling, NewsItem, PlatformConfig, PortfolioSnapshot, Position, ProcessedCongressFiling,
    PsychologySnapshot, SignalEvaluation,
    ThreatEvent, TradeOutcome, TradingSignal, get_db,
)
from lib.learning_engine import get_all_outcomes, get_all_accuracy, get_all_patterns, get_all_regime_stats, get_all_lessons
from app.scheduler import job_status

logger = logging.getLogger(__name__)

def _ui_build() -> str | None:
    """The bundle filename the SHELL currently points at.

    "Is my browser on the latest build?" was answerable only by opening
    devtools and reading the network tab, which made a caching problem
    hard to distinguish from a rendering one — a button that shipped
    looked identical to a button that was never written. The UI compares
    this against the bundle it actually loaded and says so when they
    differ.
    """
    try:
        from pathlib import Path
        index = Path(__file__).parent.parent / "static" / "dist" / "index.html"
        m = re.search(r'src="[^"]*/(index-[A-Za-z0-9_-]+\.js)"', index.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except Exception:
        return None


class TradingPreferenceRequest(BaseModel):
    trade_mode: str


class ExecutionCriteriaRequest(BaseModel):
    live_min_score: Optional[float] = None
    live_min_rr: Optional[float] = None
    live_min_confidence: Optional[float] = None


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


class NotesRequest(BaseModel):
    notes: str = ""

class ExecuteRequest(BaseModel):
    qty: Optional[int] = None

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


class ReverseSignalRequest(BaseModel):
    """Accepting a reversal proposal. Levels are re-derived server-side from
    a fresh deep verify — the client cannot dictate prices."""
    supersede_original: bool = True


def _build_provider_status() -> dict:
    from datetime import datetime as _dt, timezone as _tz
    import os as _os
    now = _dt.now(_tz.utc)
    providers = []

    def add(name, ok, detail):
        providers.append({"name": name, "ok": ok, "detail": detail})

    # LM Studio — live ping (local, 5s timeout)
    try:
        from lib.lmstudio import check_health
        h = check_health()
        add("LM Studio", bool(h.get("ok")), h.get("model") or h.get("error") or "")
    except Exception as e:
        add("LM Studio", False, str(e)[:60])

    # Alpaca — live clock call (light, authenticated)
    try:
        from lib.alpaca_client import get_trading_client
        clock = get_trading_client().get_clock()
        add("Alpaca", True, "market open" if clock.is_open else "market closed")
    except Exception as e:
        try:
            from lib.alpaca_client import describe_cred_source
            src = describe_cred_source()
        except Exception:
            src = "unknown source"
        detail = " ".join(str(e)[:60].split())
        add("Alpaca", False, f"{detail} — creds from {src}")

    # Massive REST — key presence only; a live call would burn the 5/min budget
    add("Massive", bool(_os.getenv("MASSIVE_API_KEY")), "REST (key set)" if _os.getenv("MASSIVE_API_KEY") else "no key")

    # CoinGecko — live ping (their documented health endpoint)
    try:
        import httpx as _hx
        hdr = {}
        if _os.getenv("COINGECKO_API_KEY"):
            hdr["x-cg-demo-api-key"] = _os.getenv("COINGECKO_API_KEY")
        r = _hx.get("https://api.coingecko.com/api/v3/ping", headers=hdr, timeout=6)
        add("CoinGecko", r.status_code == 200, "demo key" if hdr else "keyless")
    except Exception as e:
        add("CoinGecko", False, str(e)[:60])

    # Frankfurter — the FX source, listed by name alongside every other
    # provider. Keyless and quota-free, read from the lib's hourly cache.
    try:
        from lib.fx_rates import fetch_rates
        latest = fetch_rates("USD", ["EUR"])
        ok = bool((latest or {}).get("rates", {}).get("EUR"))
        add("Frankfurter", ok,
            f"ECB reference rates ({latest.get('date')})" if ok else "no FX data")
    except Exception as e:
        add("Frankfurter", False, str(e)[:60])

    # Web-search MCPs — initialize-level check via list_tools (cached inside
    # mcp_client per process; failures return empty rather than raising)
    try:
        from lib.mcp_client import list_tools
        for server, label in (("tavily", "Tavily"), ("exa", "Exa"), ("firecrawl", "Firecrawl")):
            try:
                tools = list_tools(server)
                add(label, bool(tools), f"{len(tools)} tools" if tools else "unreachable/no key")
            except Exception as e:
                add(label, False, str(e)[:60])
    except Exception:
        pass

    return {"providers": providers, "checked_at": now.isoformat()}


def _build_fx_rates() -> dict | None:
    """Full FX payload — TWO upstream calls total.

    Frankfurter returns every pair against a base in one request, so the
    12-pair panel costs one latest call plus one series call instead of the
    24 it used to (12 pairs x rate+history). That per-pair design burned
    AllRatesToday's 300-request LIFETIME free quota in a few hours."""
    from datetime import datetime as _dt, timezone as _tz
    from lib.fx_rates import fetch_rates, fetch_series, allrates_quota_state
    now = _dt.now(_tz.utc)

    # USD-based majors plus the EM pairs that give macro context. Cross
    # pairs (EUR/GBP, EUR/JPY, GBP/JPY) are DERIVED from the same response —
    # no extra calls, and arithmetically exact against a common base.
    quoted = ["EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD", "CNY", "MXN"]
    latest = fetch_rates("USD", quoted)
    series = fetch_series("USD", quoted, days=30)
    if not latest or not (latest.get("rates")):
        return None

    r_now = latest["rates"]
    hist = sorted(((series or {}).get("rates") or {}).items())

    def usd_rate(ccy: str, table: dict) -> float | None:
        return table.get(ccy) if ccy != "USD" else 1.0

    def build(src: str, tgt: str) -> dict | None:
        """Rate for src/tgt from USD-based quotes: USD/tgt divided by USD/src."""
        s_now, t_now = usd_rate(src, r_now), usd_rate(tgt, r_now)
        if not s_now or not t_now:
            return None
        rate = t_now / s_now
        points = []
        for day, table in hist:
            s_d, t_d = usd_rate(src, table), usd_rate(tgt, table)
            if s_d and t_d:
                points.append({"date": day, "rate": round(t_d / s_d, 6)})
        chg = None
        if len(points) >= 2 and points[0]["rate"]:
            chg = (points[-1]["rate"] - points[0]["rate"]) / points[0]["rate"] * 100
        return {
            "symbol": f"{src}{tgt}=X", "pair": f"{src}/{tgt}",
            "rate": round(rate, 6),
            "rate_source": "ecb_reference",
            "history": points,
            "change_pct": round(chg, 3) if chg is not None else None,
        }

    wanted = [
        ("EUR", "USD"), ("USD", "JPY"), ("GBP", "USD"), ("USD", "CHF"),
        ("AUD", "USD"), ("NZD", "USD"), ("USD", "CAD"), ("EUR", "GBP"),
        ("EUR", "JPY"), ("GBP", "JPY"), ("USD", "CNY"), ("USD", "MXN"),
    ]
    pairs_out = [row for row in (build(a, b) for a, b in wanted) if row]
    if not pairs_out:
        return None
    return {
        "pairs": pairs_out,
        "as_of": now.isoformat(),
        "rate_date": latest.get("date"),
        "provider": "frankfurter (ECB reference rates)",
        "allrates": allrates_quota_state(),
        "note": (
            "ECB reference rates via Frankfurter — free and keyless, published once per "
            "business day, so these are NOT live interbank ticks. Cross pairs are derived "
            "from a common USD base. Two upstream calls serve the whole panel."
        ),
    }


def _build_crypto_markets() -> dict | None:
    from datetime import datetime as _dt, timezone as _tz
    from lib.mcp_client import coingecko_snapshot, COINGECKO_IDS
    raw = coingecko_snapshot([f"{base}/USD" for base in COINGECKO_IDS])
    rows = []
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            rows = parsed.get("result") or []
        except Exception as e:
            logger.debug(f"[CryptoMarkets] parse failed: {e}")
    if not rows:
        return None
    return {
        "coins": rows,
        "as_of": _dt.now(_tz.utc).isoformat(),
        "note": "Live CoinGecko market data (keyless MCP + demo key). Cached 5 min; stale payloads serve instantly while refreshing.",
    }


class WatchlistAdd(BaseModel):
    symbol: str


class FocusRequest(BaseModel):
    symbol: str
    focus: bool = True
    note: Optional[str] = None


# Scan state is PER SYMBOL, so interrogating one coin never blocks another
# and each row reports only its own progress.
_FOCUS_SCANS: dict[str, dict] = {}
_FOCUS_SCAN_LOCK = threading.Lock()


def _focus_signal_ids(symbol: str) -> set:
    """Live signal ids for one focus symbol."""
    with get_db() as db:
        return {s.id for s in db.query(TradingSignal).filter(
            TradingSignal.asset_symbol == symbol,
            TradingSignal.status.in_(("Active", "PendingApproval")),
        ).all()}


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


class TradingStatusRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = None

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


class ConfigCreate(BaseModel):
    label: str; platform: str; config_type: Optional[str]="api"
    api_key: Optional[str]=""; api_secret: Optional[str]=""; api_url: Optional[str]=""
    extra_field_1: Optional[str]=""; extra_field_2: Optional[str]=""
    is_active: Optional[bool]=True; is_default: Optional[bool]=False; notes: Optional[str]=""

class ConfigUpdate(BaseModel):
    """Partial update — EVERY field optional. ConfigCreate was reused here
    originally, but its required label/platform meant the Ops edit form
    (which sends neither) got a 422 and no config could ever be saved."""
    label: Optional[str] = None
    platform: Optional[str] = None
    config_type: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_url: Optional[str] = None
    extra_field_1: Optional[str] = None
    extra_field_2: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    notes: Optional[str] = None


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


class AnalyzeRequest(BaseModel):
    symbol: str; timeframes: Optional[list]=["1H","4H","1D"]; generate_signal: Optional[bool]=False

def _sig_dict(s):
    try:
        score_breakdown = json.loads(getattr(s, "score_breakdown", None) or "{}")
    except (TypeError, ValueError):
        score_breakdown = {}
    # The horizon travels WITH the signal rather than being re-derived by each
    # consumer. The same table was previously copied into the signal card, the
    # Telegram formatter and the analyze endpoint; sending it means the card
    # stops guessing and every surface agrees by construction.
    from lib.trade_horizon import category as _tf_category, hold_estimate as _tf_hold
    return {
        "id":            s.id,
        "asset_symbol":  s.asset_symbol,
        "asset_name":    s.asset_name,
        "asset_class":   s.asset_class,
        "direction":     s.direction,
        "confidence":    s.confidence,
        "composite_score": s.composite_score,
        "horizon":       _tf_category(s.timeframe),
        "hold_estimate": _tf_hold(s.timeframe),
        "strategy":      getattr(s, "strategy", None),
        "strategy_score": getattr(s, "strategy_score", None),
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
        # Both spellings. The frontend type and every call site use
        # avg_entry_price, so shipping only avg_entry left the Live table's
        # Entry column blank and fed `undefined` into the exposure maths —
        # silently, because an undefined price renders as nothing rather
        # than as an error. avg_entry stays for any other consumer.
        "avg_entry_price": float(p.avg_entry_price or 0),
        "avg_entry":       float(p.avg_entry_price or 0),
        # Cash actually committed. Alpaca reports no per-position margin
        # figure, and inventing one would be worse than labelling this what
        # it is: cost basis is the capital in the trade, which for the
        # unleveraged equity positions this account holds IS the margin.
        "cost_basis":      float(getattr(p, "cost_basis", 0) or 0),
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

class FlattenRequest(BaseModel):
    scope: str            # "live" | "paper" | "all"
    confirm: str          # must be exactly "FLATTEN" — typed confirmation


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


