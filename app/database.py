"""
Jarvis Trading AI — SQLAlchemy + SQLite database layer.
v6.1: Added earnings_risk column to TradingSignal. Better migration coverage.
"""
import os, uuid, json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (create_engine, Column, String, Float, Boolean, Text, Integer,
                        UniqueConstraint, event, text)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from contextlib import contextmanager

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "jarvis.db"
DEFAULT_USER_ID = "local"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def now_iso(): return datetime.now(timezone.utc).isoformat()
def new_id():  return str(uuid.uuid4())

class Base(DeclarativeBase): pass

class TradingSignal(Base):
    __tablename__ = "trading_signals"
    id               = Column(String, primary_key=True, default=new_id)
    asset_symbol     = Column(String, nullable=False)
    asset_name       = Column(String)
    asset_class      = Column(String)
    direction        = Column(String)
    confidence       = Column(Float)
    composite_score  = Column(Float)
    timeframe        = Column(String)
    reasoning        = Column(Text)
    trigger_event    = Column(Text)
    trigger_event_id = Column(String)
    entry_price      = Column(Float)
    target_price     = Column(Float)
    stop_loss        = Column(Float)
    status           = Column(String, default="Active")
    generated_at     = Column(String)
    momentum         = Column(String)
    key_risks        = Column(Text)
    signal_source    = Column(String, default="watchlist")
    earnings_risk    = Column(Boolean, default=False)
    rr_ratio         = Column(Float)
    paper_mode       = Column(Boolean, default=False)    # Route to paper engine
    paper_direction  = Column(String)                    # Long_Leveraged | Short | Short_Leveraged
    calibrated_confidence = Column(Float)
    score_breakdown  = Column(Text)
    data_quality_score = Column(Float)
    freshness_score  = Column(Float)
    news_confidence  = Column(Float)
    setup_type       = Column(String)
    invalidation     = Column(Text)
    signal_version   = Column(String, default="v7.2")
    market_data_at   = Column(String)
    expires_at       = Column(String)
    trade_horizon    = Column(String, default="all")
    alpaca_order_id  = Column(String)
    actual_fill_price = Column(Float)   # Alpaca avg_entry_price once the position is observed live
    slippage_pct     = Column(Float)    # (actual_fill_price - entry_price) / entry_price * 100
    fill_recorded_at = Column(String)
    scaled_out       = Column(Boolean, default=False)   # partial-close-at-TP1 already applied
    scaled_out_qty   = Column(Float)
    notes            = Column(Text)                     # free-text trade journal note
    user_id          = Column(String, default=DEFAULT_USER_ID)
    created_date     = Column(String, default=now_iso)
    updated_date     = Column(String, default=now_iso)

class ThreatEvent(Base):
    __tablename__ = "threat_events"
    id          = Column(String, primary_key=True, default=new_id)
    title       = Column(String, nullable=False)
    description = Column(Text)
    event_type  = Column(String)
    severity    = Column(String)
    country     = Column(String)
    region      = Column(String)
    latitude    = Column(Float)
    longitude   = Column(Float)
    source      = Column(String)
    source_url  = Column(String)
    status      = Column(String, default="Active")
    published_at= Column(String)
    source_kind = Column(String)
    reliability_score = Column(Float)
    confirmation_status = Column(String)
    corroboration_count = Column(Integer, default=0)
    claim_confidence = Column(Float)
    cluster_id = Column(String)
    created_date= Column(String, default=now_iso)
    updated_date= Column(String, default=now_iso)

class InsiderTransaction(Base):
    """SEC Form 4 insider (officer/director/10%-owner) transactions — ingested
    from the free, unauthenticated data.sec.gov / EDGAR Archives APIs, no
    paid vendor. accession_number is the dedup key: SEC assigns exactly one
    per filing, and one filing can contain several transactions."""
    __tablename__ = "insider_transactions"
    id                = Column(String, primary_key=True, default=new_id)
    accession_number  = Column(String, nullable=False, index=True)
    issuer_cik        = Column(String)
    issuer_name       = Column(String)
    ticker            = Column(String, index=True)
    owner_cik         = Column(String)
    owner_name        = Column(String)
    owner_title       = Column(String)
    is_director       = Column(Boolean, default=False)
    is_officer        = Column(Boolean, default=False)
    is_ten_pct_owner  = Column(Boolean, default=False)
    security_title    = Column(String)
    table             = Column(String)          # "non_derivative" | "derivative"
    transaction_date  = Column(String)
    transaction_code  = Column(String)           # raw SEC code: P, S, A, M, G, F, ...
    transaction_label = Column(String)           # human label derived from the code
    acquired_disposed = Column(String)           # "A" | "D"
    shares            = Column(Float)
    price_per_share   = Column(Float)
    total_value       = Column(Float)
    shares_owned_after= Column(Float)
    filing_url        = Column(String)
    filed_at          = Column(String)
    created_date      = Column(String, default=now_iso)


class CryptoDerivativesSnapshot(Base):
    """Periodic snapshot of perpetual-futures market state — free, unauthenticated
    OKX public REST (api.okx.com), no vendor key. Binance/Bybit derivatives APIs are
    both geo-blocked from this deployment (confirmed live), so OKX is the sole source.
    One row per (symbol, fetch), used to compute funding/OI divergence over time —
    single-snapshot values alone don't reveal whether funding or OI is rising or
    falling relative to price."""
    __tablename__ = "crypto_derivatives_snapshots"
    id               = Column(String, primary_key=True, default=new_id)
    symbol           = Column(String, nullable=False, index=True)   # app-native BASE/USD
    inst_id          = Column(String)                               # OKX instId, e.g. BTC-USDT-SWAP
    price            = Column(Float)
    funding_rate     = Column(Float)
    open_interest_usd= Column(Float)
    long_short_ratio = Column(Float)                                # accounts long/short, OKX contract ratio
    fetched_at       = Column(String, default=now_iso, index=True)


class CryptoLiquidation(Base):
    """Individual liquidation fills from OKX's public liquidation-orders endpoint.
    Dedup key is (inst_id, ts, bk_px, size) since OKX doesn't assign a stable event id."""
    __tablename__ = "crypto_liquidations"
    id          = Column(String, primary_key=True, default=new_id)
    symbol      = Column(String, nullable=False, index=True)
    inst_id     = Column(String)
    side        = Column(String)      # "buy" | "sell" (the liquidation's forced order side)
    pos_side    = Column(String)      # "long" | "short" (the position that got liquidated)
    price       = Column(Float)
    size        = Column(Float)
    notional_usd= Column(Float)
    liquidated_at = Column(String, index=True)   # OKX event ts, ISO
    ingested_at = Column(String, default=now_iso)
    __table_args__ = (UniqueConstraint("inst_id", "liquidated_at", "price", "size", name="uq_liquidation_event"),)


class Alert(Base):
    """Generic cross-module alert — the single place every intelligence
    source (insider, crypto liquidations, kill switch, future modules)
    raises a notification through, instead of each module pushing straight
    to Telegram with its own ad-hoc dedup logic. dedup_key + cooldown lets
    raise_alert() (lib/alert_engine.py) suppress repeat noise for the same
    underlying event without every caller reimplementing that check."""
    __tablename__ = "alerts"
    id          = Column(String, primary_key=True, default=new_id)
    source      = Column(String, nullable=False, index=True)   # e.g. "insider", "crypto_derivatives", "kill_switch"
    severity    = Column(String, nullable=False, index=True)   # INFO | WATCH | ACTIONABLE | HIGH_PRIORITY | CRITICAL
    title       = Column(String, nullable=False)
    detail      = Column(Text)
    dedup_key   = Column(String, index=True)
    extra_json  = Column(Text)          # arbitrary structured payload, e.g. {"symbol": "BTC", "value": 123}
    delivered_telegram = Column(Boolean, default=False)
    created_at  = Column(String, default=now_iso, index=True)


class InstitutionalHolding(Base):
    """One line item from a Form 13F-HR information table — free EDGAR data,
    no vendor. See lib/sec_13f.py for the full honesty caveats; the critical
    ones: this is a QUARTERLY snapshot filed up to 45 days after quarter-end
    (so up to ~4.5 months stale), and it covers LONG US-listed equity
    positions only — never short positions or hedges.

    Dedup key is (accession_number, cusip): one filing reports a given
    security once. ticker is resolved from cusip via OpenFIGI and may be
    NULL when no US-listed equity match exists — the raw cusip is always
    kept so an unresolved holding is visibly unresolved, not silently lost."""
    __tablename__ = "institutional_holdings"
    id               = Column(String, primary_key=True, default=new_id)
    accession_number = Column(String, nullable=False, index=True)
    filer_cik        = Column(String, index=True)
    filer_name       = Column(String, index=True)
    period_of_report = Column(String, index=True)   # ISO date of the quarter end
    cusip            = Column(String, nullable=False, index=True)
    ticker           = Column(String, index=True)   # resolved via OpenFIGI; NULL if unmapped
    issuer_name      = Column(String)
    title_of_class   = Column(String)
    value_usd        = Column(Float)
    shares           = Column(Float)
    shares_type      = Column(String)               # "SH" (shares) | "PRN" (principal)
    filed_at         = Column(String)
    created_date     = Column(String, default=now_iso)


class PsychologySnapshot(Base):
    """Point-in-time reading of the JARVIS Market Psychology Index.

    Persisted purely so rate-of-change is computable: the index's level and
    the speed it is moving are different signals, and the speed needs history.
    components_json keeps the per-component breakdown so a past reading can be
    explained later rather than being an unexplainable number."""
    __tablename__ = "psychology_snapshots"
    id                   = Column(String, primary_key=True, default=new_id)
    score                = Column(Float)
    label                = Column(String)
    components_available = Column(Integer, default=0)
    components_json      = Column(Text)
    created_at           = Column(String, default=now_iso, index=True)


class CongressTrade(Base):
    """A single stock transaction disclosed in a U.S. House Periodic
    Transaction Report (STOCK Act) — free Clerk of the House data, no vendor.
    See lib/congress_trading.py for parsing details and caveats.

    Load-bearing honesty notes:
      - amount_low/amount_high are the DISCLOSED RANGE. Exact transaction size
        is never disclosed; there is deliberately no midpoint column, because
        a stored midpoint would inevitably be read as an actual amount.
      - ticker is NULL for assets disclosed without one (treasuries, bonds,
        many funds). The trade is still recorded; no symbol is inferred.
      - filing_delay_days is normal statutory reporting lag (the STOCK Act
        allows up to 45 days), not an irregularity.
      - These are legally required disclosures. Nothing in this table implies
        wrongdoing, insider knowledge, or illegality, and trades are often
        executed by advisors in managed accounts without member involvement."""
    __tablename__ = "congress_trades"
    id                = Column(String, primary_key=True, default=new_id)
    doc_id            = Column(String, nullable=False, index=True)
    member_name       = Column(String, index=True)
    state_district    = Column(String)
    chamber           = Column(String, default="House")
    owner             = Column(String)      # SP (spouse) | JT (joint) | DC (dependent child)
    asset_name        = Column(String)
    ticker            = Column(String, index=True)
    asset_type        = Column(String)      # House asset code, e.g. ST (stock), OT (other)
    transaction_code  = Column(String)      # P | S | S (partial) | E
    transaction_label = Column(String)
    transaction_date  = Column(String, index=True)
    notification_date = Column(String)
    filing_date       = Column(String)
    filing_delay_days = Column(Integer)
    amount_low        = Column(Float)
    amount_high       = Column(Float)
    amount_text       = Column(String)
    pdf_url           = Column(String)
    created_date      = Column(String, default=now_iso)


class ProcessedCongressFiling(Base):
    """Tracks which PTR filings have been downloaded and parsed, so each PDF
    is fetched once. rows_unparsed is persisted rather than discarded: it is
    the per-filing coverage signal, and a filing that parsed zero rows needs
    to be distinguishable from one that genuinely disclosed no trades."""
    __tablename__ = "processed_congress_filings"
    doc_id             = Column(String, primary_key=True)
    member_name        = Column(String)
    rows_seen          = Column(Integer, default=0)
    rows_unparsed      = Column(Integer, default=0)
    transactions_saved = Column(Integer, default=0)
    processed_at       = Column(String, default=now_iso)


class Processed13FFiling(Base):
    """Tracks which 13F filings have been fully processed.

    This exists because "we saved some holdings from this filing" is NOT the
    same as "we're done with it": OpenFIGI's rate limit caps how many new
    CUSIPs one run can resolve, so an early run may parse a filing while most
    of its CUSIPs are still unmapped. Deduping on saved holdings alone would
    mark such a filing complete and silently lose every holding whose CUSIP
    hadn't been resolved yet. Only filings with fully_resolved=True are
    skipped on later runs; the rest are reprocessed once the CUSIP map has
    grown."""
    __tablename__ = "processed_13f_filings"
    accession_number = Column(String, primary_key=True)
    filer_cik        = Column(String)
    fully_resolved   = Column(Boolean, default=False, index=True)
    unresolved_count = Column(Integer, default=0)
    processed_at     = Column(String, default=now_iso)


class CusipTickerMap(Base):
    """Persistent CUSIP -> ticker cache. CUSIP-to-ticker is a licensed
    dataset, so mappings are resolved through OpenFIGI's free API and cached
    here permanently — the mapping is stable, and OpenFIGI's keyless tier is
    rate-limited enough that re-resolving on every ingest run would throttle.
    resolved_ticker NULL means "looked up, genuinely no US equity match" —
    distinct from "not yet looked up" (no row at all), so failed lookups
    aren't retried forever."""
    __tablename__ = "cusip_ticker_map"
    cusip            = Column(String, primary_key=True)
    resolved_ticker  = Column(String, index=True)
    resolved_at      = Column(String, default=now_iso)


class NewsItem(Base):
    __tablename__ = "news_items"
    id              = Column(String, primary_key=True, default=new_id)
    title           = Column(String, nullable=False)
    summary         = Column(Text)
    source          = Column(String)
    url             = Column(String)
    category        = Column(String)
    sentiment       = Column(String)
    affected_assets = Column(Text)
    region          = Column(String)
    published_at    = Column(String)
    canonical_url   = Column(String)
    source_kind     = Column(String)
    provider        = Column(String)
    ingested_at     = Column(String)
    reliability_score = Column(Float)
    confirmation_status = Column(String)
    corroboration_count = Column(Integer, default=0)
    corroborated_sources = Column(Text)
    claim_confidence = Column(Float)
    is_stale        = Column(Boolean, default=False)
    entities        = Column(Text)
    cluster_id      = Column(String)
    created_date    = Column(String, default=now_iso)
    updated_date    = Column(String, default=now_iso)

class MarketAsset(Base):
    __tablename__ = "market_assets"
    id            = Column(String, primary_key=True, default=new_id)
    symbol        = Column(String, unique=True, nullable=False)
    name          = Column(String)
    asset_class   = Column(String)
    price         = Column(Float)
    change_percent= Column(Float)
    volume        = Column(Float)
    market_cap    = Column(Float)
    region        = Column(String)
    last_updated  = Column(String)
    created_date  = Column(String, default=now_iso)
    updated_date  = Column(String, default=now_iso)

class PlatformConfig(Base):
    __tablename__ = "platform_configs"
    id           = Column(String, primary_key=True, default=new_id)
    key          = Column(String, unique=True)
    label        = Column(String)
    platform     = Column(String)
    config_type  = Column(String)
    api_key      = Column(String)
    api_secret   = Column(String)
    api_url      = Column(String)
    extra_field_1= Column(String)
    extra_field_2= Column(String)
    extra_field_3= Column(String)
    is_active    = Column(Boolean, default=True)
    is_default   = Column(Boolean, default=False)
    notes        = Column(Text)
    created_date = Column(String, default=now_iso)
    updated_date = Column(String, default=now_iso)
    user_id      = Column(String, default=DEFAULT_USER_ID)

class Position(Base):
    __tablename__ = "positions_cache"
    symbol          = Column(String, primary_key=True)
    qty             = Column(Float)
    avg_entry       = Column(Float)
    market_value    = Column(Float)
    unrealized_pl   = Column(Float)
    unrealized_plpc = Column(Float)
    side            = Column(String)
    asset_class     = Column(String)
    updated_at      = Column(String, default=now_iso)

class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id             = Column(String, primary_key=True, default=new_id)
    equity         = Column(Float)
    cash           = Column(Float)
    market_value   = Column(Float)
    unrealized_pl  = Column(Float)
    position_count = Column(Float)
    snapshot_at    = Column(String, default=now_iso)


class SystemState(Base):
    """System-wide operational state (kill switch), separate from per-user
    preferences — a global control an operator flips regardless of which
    user's settings say, independent of the paper-only Auto Trading toggle."""
    __tablename__ = "system_state"
    id                    = Column(String, primary_key=True, default=lambda: "global")
    live_trading_enabled  = Column(Boolean, default=True)
    paused_reason         = Column(Text)
    paused_at             = Column(String)
    updated_at            = Column(String, default=now_iso)


class TradeOutcome(Base):
    """Records the final outcome of every closed trade for learning/backreview."""
    __tablename__ = "trade_outcomes"
    id               = Column(String, primary_key=True, default=new_id)
    signal_id        = Column(String)          # FK → trading_signals.id
    symbol           = Column(String)
    asset_class      = Column(String)          # equity | crypto
    direction        = Column(String)          # BUY | SELL
    timeframe        = Column(String)
    entry_price      = Column(Float)
    exit_price       = Column(Float)
    qty              = Column(Float)
    pnl_usd          = Column(Float)           # realized P&L in dollars
    pnl_pct          = Column(Float)           # realized P&L in percent
    outcome          = Column(String)          # WIN | LOSS | BREAKEVEN
    exit_reason      = Column(String)          # HARD_STOP | TAKE_PROFIT | LLM_EXIT | MANUAL | TIMEOUT
    hold_duration_m  = Column(Float)           # minutes held
    signal_confidence= Column(Float)           # original signal confidence
    signal_score     = Column(Float)           # original composite score
    signal_reasoning = Column(Text)            # original LLM reasoning
    ta_summary       = Column(Text)            # TA snapshot at entry
    market_regime    = Column(String)          # trending | ranging | volatile at entry
    paper_mode       = Column(Boolean, default=False)
    entered_at       = Column(String)
    exited_at        = Column(String, default=now_iso)

class SignalAccuracy(Base):
    """Aggregated win-rate stats per symbol+timeframe for LLM prompt injection."""
    __tablename__ = "signal_accuracy"
    id               = Column(String, primary_key=True, default=new_id)
    symbol           = Column(String)
    asset_class      = Column(String)
    timeframe        = Column(String)
    total_trades     = Column(Integer, default=0)
    wins             = Column(Integer, default=0)
    losses           = Column(Integer, default=0)
    win_rate         = Column(Float, default=0.0)   # 0.0–1.0
    avg_pnl_pct      = Column(Float, default=0.0)
    avg_hold_min     = Column(Float, default=0.0)
    best_pnl_pct     = Column(Float, default=0.0)
    worst_pnl_pct    = Column(Float, default=0.0)
    last_updated     = Column(String, default=now_iso)


class IntelligenceSourceHealth(Base):
    __tablename__ = "intelligence_source_health"
    source               = Column(String, primary_key=True)
    source_kind          = Column(String)
    provider             = Column(String)
    url                  = Column(String)
    reliability_score    = Column(Float, default=0.5)
    success_count        = Column(Integer, default=0)
    failure_count        = Column(Integer, default=0)
    consecutive_failures = Column(Integer, default=0)
    last_success_at      = Column(String)
    last_failure_at      = Column(String)
    last_error           = Column(Text)
    last_latency_ms      = Column(Float)
    last_article_count   = Column(Integer, default=0)
    updated_at           = Column(String, default=now_iso)


class IntelligenceIngestionRun(Base):
    __tablename__ = "intelligence_ingestion_runs"
    id                = Column(String, primary_key=True, default=new_id)
    started_at        = Column(String)
    finished_at       = Column(String)
    status            = Column(String)
    source_count      = Column(Integer, default=0)
    failed_sources    = Column(Integer, default=0)
    fetched_count     = Column(Integer, default=0)
    fresh_count       = Column(Integer, default=0)
    selected_count    = Column(Integer, default=0)
    saved_news        = Column(Integer, default=0)
    saved_threats     = Column(Integer, default=0)
    error             = Column(Text)


class SignalEvaluation(Base):
    """Forward-only paper evaluation of a generated signal against later bars."""
    __tablename__ = "signal_evaluations"
    signal_id          = Column(String, primary_key=True)
    symbol             = Column(String)
    asset_class        = Column(String)
    direction          = Column(String)
    timeframe          = Column(String)
    signal_version     = Column(String)
    generated_at       = Column(String)
    first_bar_at       = Column(String)
    last_bar_at        = Column(String)
    bars_observed      = Column(Integer, default=0)
    entry_price        = Column(Float)
    target_price       = Column(Float)
    stop_loss          = Column(Float)
    mfe_pct            = Column(Float, default=0.0)
    mae_pct            = Column(Float, default=0.0)
    outcome            = Column(String, default="OPEN")
    data_issue         = Column(Text)
    target_hit_at      = Column(String)
    stop_hit_at        = Column(String)
    evaluated_at       = Column(String, default=now_iso)


class AppUser(Base):
    __tablename__ = "app_users"
    id           = Column(String, primary_key=True, default=new_id)
    email        = Column(String, unique=True)
    display_name = Column(String)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(String, default=now_iso)
    updated_at   = Column(String, default=now_iso)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    user_id             = Column(String, primary_key=True)
    trade_mode          = Column(String, default="all")  # scalp | longer | all
    min_confidence      = Column(Float, default=60.0)
    asset_classes       = Column(Text, default="[]")
    directions          = Column(Text, default="[]")
    telegram_enabled    = Column(Boolean, default=False)
    auto_sim_enabled    = Column(Boolean, default=True)
    paper_auto_trade_enabled = Column(Boolean, default=True)
    updated_at          = Column(String, default=now_iso)


class TelegramLinkToken(Base):
    __tablename__ = "telegram_link_tokens"
    id           = Column(String, primary_key=True, default=new_id)
    user_id      = Column(String, nullable=False)
    token_hash   = Column(String, unique=True, nullable=False)
    expires_at   = Column(String, nullable=False)
    used_at      = Column(String)
    created_at   = Column(String, default=now_iso)


class UserTelegramLink(Base):
    __tablename__ = "user_telegram_links"
    id           = Column(String, primary_key=True, default=new_id)
    user_id      = Column(String, unique=True, nullable=False)
    chat_id      = Column(String, unique=True, nullable=False)
    is_active    = Column(Boolean, default=True)
    linked_at    = Column(String, default=now_iso)


class TelegramDelivery(Base):
    __tablename__ = "telegram_deliveries"
    id           = Column(String, primary_key=True, default=new_id)
    user_id      = Column(String, nullable=False)
    chat_id      = Column(String, nullable=False)
    signal_id    = Column(String, nullable=False)
    setup_key    = Column(String)
    setup_state  = Column(Text)
    message_id   = Column(String)
    delivered_at = Column(String, default=now_iso)
    updated_at   = Column(String, default=now_iso)
    status       = Column(String, default="sent")


class TelegramCallback(Base):
    __tablename__ = "telegram_callbacks"
    callback_id  = Column(String, primary_key=True)
    user_id      = Column(String, nullable=False)
    chat_id      = Column(String, nullable=False)
    signal_id    = Column(String, nullable=False)
    action       = Column(String, nullable=False)
    processed_at = Column(String, default=now_iso)


class AutoSimPosition(Base):
    __tablename__ = "auto_sim_positions"
    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_auto_sim_position_user_signal"),
    )
    id                = Column(String, primary_key=True, default=new_id)
    user_id           = Column(String, default=DEFAULT_USER_ID)
    signal_id         = Column(String, nullable=False)
    symbol            = Column(String, nullable=False)
    asset_class       = Column(String)
    direction         = Column(String)
    side              = Column(String)
    leverage          = Column(Float, default=1.0)
    qty               = Column(Float)
    entry_price       = Column(Float)
    current_price     = Column(Float)
    target_price      = Column(Float)
    stop_loss         = Column(Float)
    margin_used       = Column(Float, default=1000.0)
    unrealized_pnl    = Column(Float, default=0.0)
    status            = Column(String, default="Open")
    signal_updated_at = Column(String)
    opened_at         = Column(String, default=now_iso)
    updated_at        = Column(String, default=now_iso)


class AutoSimTrade(Base):
    __tablename__ = "auto_sim_trades"
    id           = Column(String, primary_key=True, default=new_id)
    user_id      = Column(String, default=DEFAULT_USER_ID)
    signal_id    = Column(String)
    symbol       = Column(String)
    asset_class  = Column(String)
    direction    = Column(String)
    side         = Column(String)
    leverage     = Column(Float, default=1.0)
    qty          = Column(Float)
    entry_price  = Column(Float)
    exit_price   = Column(Float)
    realized_pnl = Column(Float, default=0.0)
    pnl_pct      = Column(Float, default=0.0)
    close_reason = Column(String)
    opened_at    = Column(String)
    closed_at    = Column(String, default=now_iso)


class AutoSimPortfolio(Base):
    __tablename__ = "auto_sim_portfolios"
    user_id      = Column(String, primary_key=True, default=DEFAULT_USER_ID)
    starting_cash= Column(Float, default=100000.0)
    realized_pnl = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    wins         = Column(Integer, default=0)
    losses       = Column(Integer, default=0)
    updated_at   = Column(String, default=now_iso)

class BacktestRun(Base):
    """A historical backtest run (lib/backtester.run_backtest) — purely
    additive new table, not touching any existing table's columns."""
    __tablename__ = "backtest_runs"
    id           = Column(String, primary_key=True, default=new_id)
    symbols      = Column(Text)             # JSON-encoded list[str]
    timeframes   = Column(Text)             # JSON-encoded list[str]
    trade_mode   = Column(String)
    start_date   = Column(String)
    end_date     = Column(String)
    status       = Column(String, default="running")   # running | completed | failed
    result_json  = Column(Text)             # JSON-encoded full result dict once complete
    error        = Column(Text)
    created_at   = Column(String, default=now_iso)
    finished_at  = Column(String)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Run migrations for any missing columns
    _migrate_columns()
    _seed_local_account()
    # Seed paper portfolio if missing
    _seed_paper_portfolio()
    _seed_system_state()
    print("[DB] Schema initialized")


def _seed_system_state():
    """Ensure the global kill-switch row exists, defaulting to trading enabled."""
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT OR IGNORE INTO system_state
                    (id, live_trading_enabled, paused_reason, paused_at, updated_at)
                VALUES ('global', 1, NULL, NULL, :now)
            """), {"now": now_iso()})
    except Exception as exc:
        print(f"[DB] system_state seed skipped: {exc}")


def _seed_local_account():
    """Backwards-compatible owner for data created before account support."""
    now = now_iso()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT OR IGNORE INTO app_users
                    (id, email, display_name, is_active, created_at, updated_at)
                VALUES ('local', NULL, 'Local User', 1, :now, :now)
            """), {"now": now})
            conn.execute(text("""
                INSERT OR IGNORE INTO user_preferences
                    (user_id, trade_mode, min_confidence, asset_classes, directions,
                     telegram_enabled, auto_sim_enabled, updated_at)
                VALUES ('local', 'all', 60.0, '[]', '[]', 0, 1, :now)
            """), {"now": now})
            conn.execute(text("""
                INSERT OR IGNORE INTO auto_sim_portfolios
                    (user_id, starting_cash, realized_pnl, total_trades, wins, losses, updated_at)
                VALUES ('local', 100000.0, 0.0, 0, 0, 0, :now)
            """), {"now": now})
            backfill_legacy_user_ids(conn)
    except Exception as exc:
        print(f"[DB] Local account seed warning: {exc}")


def backfill_legacy_user_ids(conn, tables=None, user_id: str = DEFAULT_USER_ID):
    """Assign pre-account rows to the backward-compatible local owner."""
    tables = tables or ("trading_signals", "platform_configs", "paper_positions", "paper_trades", "paper_portfolio")
    existing_tables = {
        row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    }
    for table in tables:
        if table not in existing_tables:
            continue
        columns = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if "user_id" in columns:
            conn.execute(
                text(f"UPDATE {table} SET user_id=:user_id WHERE user_id IS NULL OR user_id=''"),
                {"user_id": user_id},
            )

def _repair_auto_sim_history():
    """Remove replayed signal rows created before Auto Sim runs were serialized."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE auto_sim_positions SET user_id=:user_id "
                "WHERE user_id IS NULL OR user_id=''"
            ), {"user_id": DEFAULT_USER_ID})
            conn.execute(text(
                "UPDATE auto_sim_trades SET user_id=:user_id "
                "WHERE user_id IS NULL OR user_id=''"
            ), {"user_id": DEFAULT_USER_ID})

            duplicate_positions = conn.execute(text("""
                DELETE FROM auto_sim_positions
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY user_id, signal_id
                                   ORDER BY COALESCE(opened_at, ''), id
                               ) AS replay_number
                        FROM auto_sim_positions
                        WHERE signal_id IS NOT NULL
                    ) replays
                    WHERE replay_number > 1
                )
            """)).rowcount
            duplicate_trades = conn.execute(text("""
                DELETE FROM auto_sim_trades
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY user_id, signal_id
                                   ORDER BY COALESCE(closed_at, ''), id
                               ) AS replay_number
                        FROM auto_sim_trades
                        WHERE signal_id IS NOT NULL
                    ) replays
                    WHERE replay_number > 1
                )
            """)).rowcount

            conn.execute(text("""
                UPDATE auto_sim_portfolios
                SET realized_pnl = COALESCE((
                        SELECT SUM(t.realized_pnl)
                        FROM auto_sim_trades t
                        WHERE t.user_id = auto_sim_portfolios.user_id
                    ), 0),
                    total_trades = (
                        SELECT COUNT(*) FROM auto_sim_trades t
                        WHERE t.user_id = auto_sim_portfolios.user_id
                    ),
                    wins = (
                        SELECT COUNT(*) FROM auto_sim_trades t
                        WHERE t.user_id = auto_sim_portfolios.user_id
                          AND t.realized_pnl > 0
                    ),
                    losses = (
                        SELECT COUNT(*) FROM auto_sim_trades t
                        WHERE t.user_id = auto_sim_portfolios.user_id
                          AND t.realized_pnl < 0
                    ),
                    updated_at = :updated_at
            """), {"updated_at": now_iso()})
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_sim_position_user_signal
                ON auto_sim_positions (user_id, signal_id)
            """))

        if duplicate_positions or duplicate_trades:
            print(
                "[DB] Auto Sim repair: removed "
                f"{duplicate_positions} replayed positions and {duplicate_trades} replayed trades"
            )
    except Exception as exc:
        print(f"[DB] Auto Sim repair warning: {exc}")


def _ensure_paper_position_unique_open_index():
    """
    Guard against duplicate open paper positions for the same (user, symbol).

    open_paper_position() does a non-atomic check-then-insert (SELECT then
    INSERT in separate statements), so a concurrent scheduler cycle and a
    Telegram callback could both pass the "already open?" check and insert
    duplicate open positions before either commits. A partial unique index
    (SQLite supports WHERE-qualified indexes) makes the second INSERT raise
    an IntegrityError instead of silently succeeding; open_paper_position()
    catches that and returns the same "already open" error it would have
    returned had the check caught the race.

    This is created best-effort: if duplicate open rows already exist on an
    existing DB, CREATE UNIQUE INDEX will fail — that's logged and skipped
    rather than crashing startup. Run a manual cleanup + retry if it warns.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_position_open_symbol
                ON paper_positions (user_id, symbol)
                WHERE status = 'Open'
            """))
            conn.commit()
    except Exception as exc:
        print(
            "[DB] paper_positions unique-open-symbol index not created "
            f"(likely pre-existing duplicate open rows): {exc}"
        )


def _migrate_columns():
    """Add any missing columns to existing tables without data loss."""
    migrations = {
        "trading_signals": [
            ("composite_score",  "REAL"),
            ("signal_source",    "TEXT DEFAULT 'watchlist'"),
            ("earnings_risk",    "INTEGER DEFAULT 0"),
            ("rr_ratio",         "REAL"),
            ("momentum",         "TEXT"),
            ("key_risks",        "TEXT"),
            ("paper_mode",       "INTEGER DEFAULT 0"),
            ("paper_direction",  "TEXT"),
            ("calibrated_confidence", "REAL"),
            ("score_breakdown", "TEXT"),
            ("data_quality_score", "REAL"),
            ("freshness_score", "REAL"),
            ("news_confidence", "REAL"),
            ("setup_type", "TEXT"),
            ("invalidation", "TEXT"),
            ("signal_version", "TEXT DEFAULT 'v7.2'"),
            ("market_data_at", "TEXT"),
            ("expires_at", "TEXT"),
            ("trade_horizon", "TEXT DEFAULT 'all'"),
            ("user_id", "TEXT DEFAULT 'local'"),
            ("trigger_event", "TEXT"),
            ("trigger_event_id", "TEXT"),
            ("alpaca_order_id", "TEXT"),
            ("actual_fill_price", "REAL"),
            ("slippage_pct", "REAL"),
            ("fill_recorded_at", "TEXT"),
            ("scaled_out", "INTEGER DEFAULT 0"),
            ("scaled_out_qty", "REAL"),
            ("notes", "TEXT"),
        ],
        "news_items": [
            ("canonical_url", "TEXT"),
            ("source_kind", "TEXT"),
            ("provider", "TEXT"),
            ("ingested_at", "TEXT"),
            ("reliability_score", "REAL"),
            ("confirmation_status", "TEXT"),
            ("corroboration_count", "INTEGER DEFAULT 0"),
            ("corroborated_sources", "TEXT"),
            ("claim_confidence", "REAL"),
            ("is_stale", "INTEGER DEFAULT 0"),
            ("entities", "TEXT"),
            ("cluster_id", "TEXT"),
        ],
        "threat_events": [
            ("source_kind", "TEXT"),
            ("reliability_score", "REAL"),
            ("confirmation_status", "TEXT"),
            ("corroboration_count", "INTEGER DEFAULT 0"),
            ("claim_confidence", "REAL"),
            ("cluster_id", "TEXT"),
        ],
        "signal_evaluations": [
            ("data_issue", "TEXT"),
        ],
        "platform_configs": [
            ("user_id", "TEXT DEFAULT 'local'"),
        ],
        "paper_positions": [
            ("user_id",          "TEXT DEFAULT 'local'"),
            ("asset_class",     "TEXT"),
            ("direction",       "TEXT"),
            ("side",            "TEXT"),
            ("leverage",        "REAL DEFAULT 1.0"),
            ("notional",        "REAL"),
            ("margin_used",     "REAL"),
            ("unrealized_pnl",  "REAL DEFAULT 0.0"),
            ("unrealized_pct",  "REAL DEFAULT 0.0"),
            ("signal_id",       "TEXT"),
            ("scaled_out",      "INTEGER DEFAULT 0"),
            ("scaled_out_qty",  "REAL"),
        ],
        "paper_trades": [
            ("user_id",          "TEXT DEFAULT 'local'"),
            ("asset_class",     "TEXT"),
            ("direction",       "TEXT"),
            ("side",            "TEXT"),
            ("leverage",        "REAL DEFAULT 1.0"),
            ("notional",        "REAL"),
            ("signal_id",       "TEXT"),
            ("position_id",     "TEXT"),
        ],
        "paper_portfolio": [
            ("user_id",          "TEXT DEFAULT 'local'"),
        ],
        "telegram_deliveries": [
            ("setup_key", "TEXT"),
            ("setup_state", "TEXT"),
            ("message_id", "TEXT"),
            ("updated_at", "TEXT"),
        ],
        "user_preferences": [
            ("paper_auto_trade_enabled", "INTEGER DEFAULT 1"),
        ],
    }
    try:
        with engine.connect() as conn:
            for table, cols in migrations.items():
                existing = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
                for col_name, col_def in cols:
                    if col_name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
                        conn.commit()
                        print(f"[DB] Migrated: added {table}.{col_name}")
            # Ensure ai_decisions table exists (may be missing on older DBs)
            tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
            if "telegram_deliveries" in tables:
                conn.execute(text("""
                    UPDATE telegram_deliveries
                    SET setup_key = (
                        SELECT UPPER(s.asset_symbol) || ':' ||
                               CASE WHEN LOWER(COALESCE(s.direction, '')) LIKE '%short%'
                                    THEN 'short' ELSE 'long' END
                        FROM trading_signals s
                        WHERE s.id = telegram_deliveries.signal_id
                    )
                    WHERE setup_key IS NULL OR setup_key = ''
                """))
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_telegram_delivery_setup
                    ON telegram_deliveries (user_id, chat_id, setup_key)
                """))
                conn.commit()
            if "ai_decisions" not in tables:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS ai_decisions (
                        id         TEXT PRIMARY KEY,
                        source     TEXT,
                        symbol     TEXT,
                        action     TEXT,
                        reasoning  TEXT,
                        price      REAL,
                        pnl_pct    REAL,
                        score      REAL,
                        created_at TEXT
                    )
                """))
                conn.commit()
                print("[DB] Migrated: created ai_decisions table")
    except Exception as e:
        print(f"[DB] Migration warning: {e}")

    _repair_auto_sim_history()
    _ensure_paper_position_unique_open_index()

    # ── Learning engine tables (Tiers 1-5) ─────────────────────────────────
    # Safe to run on every startup — CREATE TABLE IF NOT EXISTS is idempotent
    try:
        with engine.begin() as conn:
            # Tier 1+2 — trade history & accuracy tracking
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS trade_outcomes (
                    id TEXT PRIMARY KEY, signal_id TEXT, symbol TEXT, asset_class TEXT,
                    direction TEXT, timeframe TEXT, entry_price REAL, exit_price REAL,
                    qty REAL, pnl_usd REAL, pnl_pct REAL, outcome TEXT, exit_reason TEXT,
                    hold_duration_m REAL, signal_confidence REAL, signal_score REAL,
                    signal_reasoning TEXT, ta_summary TEXT, market_regime TEXT,
                    paper_mode INTEGER DEFAULT 0, entered_at TEXT, exited_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS signal_accuracy (
                    id TEXT PRIMARY KEY, symbol TEXT, asset_class TEXT, timeframe TEXT,
                    total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0, win_rate REAL DEFAULT 0.0,
                    avg_pnl_pct REAL DEFAULT 0.0, avg_hold_min REAL DEFAULT 0.0,
                    best_pnl_pct REAL DEFAULT 0.0, worst_pnl_pct REAL DEFAULT 0.0,
                    last_updated TEXT
                )
            """))
            # Tier 3 — Pattern Memory (TA setup fingerprints → win/loss history)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS pattern_memory (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT UNIQUE,
                    pattern_desc TEXT,
                    asset_class TEXT,
                    timeframe TEXT,
                    total INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    avg_pnl_pct REAL DEFAULT 0.0,
                    last_seen TEXT,
                    last_updated TEXT
                )
            """))
            # Tier 4 — Regime Performance (strategy perf by market regime)
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS regime_performance (
                    id TEXT PRIMARY KEY,
                    regime TEXT UNIQUE,
                    total INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    avg_pnl_pct REAL DEFAULT 0.0,
                    avg_confidence REAL DEFAULT 0.0,
                    last_updated TEXT
                )
            """))
            # Tier 5 — LLM Reasoning Audit / Lessons
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_lessons (
                    id TEXT PRIMARY KEY,
                    outcome_id TEXT,
                    symbol TEXT,
                    asset_class TEXT,
                    outcome TEXT,
                    pnl_pct REAL,
                    original_reasoning TEXT,
                    lesson TEXT,
                    lesson_category TEXT,
                    market_regime TEXT,
                    paper_mode INTEGER DEFAULT 0,
                    created_at TEXT
                )
            """))
            print("[DB] Learning engine tables (Tiers 1-5) ready")
    except Exception as e:
        print(f"[DB] Learning table migration warning: {e}")


def _seed_paper_portfolio():
    """Ensure a PaperPortfolio row exists with starting capital.
    Safe to call on every startup — only inserts if the table is empty."""
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
            if "paper_portfolio" not in tables:
                print("[DB] paper_portfolio table not yet created — skipping seed")
                return
            row = conn.execute(text("SELECT cash FROM paper_portfolio LIMIT 1")).fetchone()
            if row is None:
                conn.execute(text(
                    "INSERT INTO paper_portfolio (id, cash, total_trades, winning_trades, realized_pnl, updated_at) "
                    "VALUES (:id, :cash, 0, 0, 0.0, :ts)"
                ), {"id": str(uuid.uuid4()), "cash": 100000.0, "ts": datetime.now(timezone.utc).isoformat()})
                conn.commit()
                print("[DB] Paper portfolio seeded with $100,000 starting capital")
            elif float(row[0]) == 0.0:
                # Corrupted zero-cash row — reset it
                conn.execute(text("UPDATE paper_portfolio SET cash=100000.0, updated_at=:ts"),
                             {"ts": datetime.now(timezone.utc).isoformat()})
                conn.commit()
                print("[DB] Paper portfolio cash was $0 — reset to $100,000")
    except Exception as e:
        print(f"[DB] Paper portfolio seed warning: {e}")


# ── Paper Trading Models ──────────────────────────────────────────────────────

class PaperPosition(Base):
    """Open or closed virtual positions for the paper trading engine."""
    __tablename__ = "paper_positions"
    id            = Column(String, primary_key=True, default=new_id)
    user_id       = Column(String, default=DEFAULT_USER_ID)
    symbol        = Column(String, nullable=False)
    asset_class   = Column(String)          # Equity | Crypto
    direction     = Column(String)          # Long | Long_Leveraged | Short | Short_Leveraged
    side          = Column(String)          # long | short
    leverage      = Column(Float, default=1.0)
    qty           = Column(Float)
    entry_price   = Column(Float)
    current_price = Column(Float)
    target_price  = Column(Float)
    stop_loss     = Column(Float)
    notional      = Column(Float)           # total exposure = qty * entry_price * leverage
    margin_used   = Column(Float)           # cash reserved = notional / leverage
    unrealized_pnl= Column(Float, default=0.0)
    unrealized_pct= Column(Float, default=0.0)
    signal_id     = Column(String)          # FK to trading_signals.id (optional)
    status        = Column(String, default="Open")  # Open | Closed
    scaled_out    = Column(Boolean, default=False)  # partial-close-at-TP1 already applied
    scaled_out_qty = Column(Float)
    opened_at     = Column(String, default=now_iso)
    updated_at    = Column(String, default=now_iso)


class PaperTrade(Base):
    """Completed paper trades — the historical ledger."""
    __tablename__ = "paper_trades"
    id            = Column(String, primary_key=True, default=new_id)
    user_id       = Column(String, default=DEFAULT_USER_ID)
    position_id   = Column(String)          # FK to paper_positions.id
    symbol        = Column(String)
    asset_class   = Column(String)
    direction     = Column(String)
    side          = Column(String)
    leverage      = Column(Float, default=1.0)
    qty           = Column(Float)
    entry_price   = Column(Float)
    exit_price    = Column(Float)
    notional      = Column(Float)
    realized_pnl  = Column(Float)
    pnl_pct       = Column(Float)
    close_reason  = Column(String)          # stop_loss | take_profit | manual | margin_call
    signal_id     = Column(String)
    opened_at     = Column(String)
    closed_at     = Column(String, default=now_iso)


class PaperPortfolio(Base):
    """Single-row virtual account state."""
    __tablename__ = "paper_portfolio"
    id             = Column(String, primary_key=True, default=new_id)
    user_id        = Column(String, default=DEFAULT_USER_ID)
    cash           = Column(Float, default=100000.0)
    total_trades   = Column(Float, default=0)
    winning_trades = Column(Float, default=0)
    realized_pnl   = Column(Float, default=0.0)
    updated_at     = Column(String, default=now_iso)

class AiDecision(Base):
    """Log of every AI decision made by Guardian, position manager, and paper trading."""
    __tablename__ = "ai_decisions"
    id          = Column(String, primary_key=True, default=new_id)
    source      = Column(String)   # guardian | positions | paper | signals
    symbol      = Column(String)   # affected symbol (None for portfolio-level decisions)
    action      = Column(String)   # HOLD | EXIT | TIGHTEN_STOP | EXIT_WEAKEST | EXIT_ALL | TIGHTEN_ALL | APPROVED | REJECTED
    reasoning   = Column(String)   # LLM reasoning text
    price       = Column(Float)    # current price at decision time (optional)
    pnl_pct     = Column(Float)    # P&L% of position at decision time (optional)
    score       = Column(Float)    # confidence/score if entry eval (optional)
    created_at  = Column(String, default=now_iso)
