"""
Jarvis Trading AI — SQLAlchemy + SQLite database layer.
v6.1: Added earnings_risk column to TradingSignal. Better migration coverage.
"""
import os, uuid, json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (create_engine, Column, String, Float, Boolean, Text, Integer,
                        Index, UniqueConstraint, event, text)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from contextlib import contextmanager

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
_OPERATOR_DB = DATA_DIR / "jarvis.db"


def _resolve_db_path() -> Path:
    """The operator database is structurally unreachable from pytest.

    A test once reset the active paper book, and this week a test's
    fixture rows leaked into the live candidate tables — per-test
    discipline demonstrably isn't enough. The rule is enforced HERE, at
    engine construction, so no amount of forgotten cleanup in a test file
    can touch real state:

      - JARVIS_DB_PATH overrides the location (tests point it at a temp
        dir; deployments may relocate the data directory).
      - Under pytest (JARVIS_UNDER_PYTEST, set by conftest.py before any
        app import), resolving to the operator database is a hard error
        unless JARVIS_ALLOW_OPERATOR_DB=1 is set EXPLICITLY — the
        integration-test escape hatch, never a default.
    """
    override = os.getenv("JARVIS_DB_PATH", "").strip()
    path = Path(override) if override else _OPERATOR_DB
    under_pytest = os.getenv("JARVIS_UNDER_PYTEST") == "1"
    allowed = os.getenv("JARVIS_ALLOW_OPERATOR_DB") == "1"
    if under_pytest and not allowed and path.resolve() == _OPERATOR_DB.resolve():
        raise RuntimeError(
            "Refusing to open the operator database under pytest. "
            "conftest.py should have pointed JARVIS_DB_PATH at a temp dir; "
            "set JARVIS_ALLOW_OPERATOR_DB=1 only for explicit integration runs."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DB_PATH = _resolve_db_path()
DEFAULT_USER_ID = "local"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
    # SQLite connections are just file handles — the default pool (5+10) is
    # pure downside here: 10 scheduler workers + API requests each hold a
    # connection for up to busy_timeout (30s) when a writer has the lock, so
    # a write burst exhausted the pool and starved the whole API (observed
    # in production: QueuePool limit reached, UI hung). Size the pool above
    # worst-case concurrency and let stragglers wait longer than one full
    # busy_timeout cycle.
    pool_size=25,
    max_overflow=25,
    pool_timeout=60,
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
    # Which named strategy this setup matched (lib/strategies.py), so
    # performance can be attributed to a strategy rather than pooled into
    # one unexplainable win rate. Deterministic, never LLM-assigned.
    strategy         = Column(String)
    strategy_score   = Column(Float)
    invalidation     = Column(Text)
    signal_version   = Column(String, default="v7.2")
    # Which brain actually answered — from the LLM RESPONSE, not the
    # request config (LM Studio swaps loads under the desk's feet). The
    # join key for per-model outcome comparison.
    llm_model        = Column(String)
    market_data_at   = Column(String)
    expires_at       = Column(String)
    trade_horizon    = Column(String, default="all")
    alpaca_order_id  = Column(String)
    actual_fill_price = Column(Float)   # Alpaca avg_entry_price once the position is observed live
    slippage_pct     = Column(Float)    # (actual_fill_price - entry_price) / entry_price * 100
    fill_recorded_at = Column(String)
    scaled_out       = Column(Boolean, default=False)   # partial-close-at-TP1 already applied
    scaled_out_qty   = Column(Float)
    notes            = Column(Text)
    # ── Level provenance ─────────────────────────────────────────────────
    # Where each price came from. When Jarvis synthesises a stop and target
    # from a fixed ATR multiple, the resulting R:R is its OWN arithmetic —
    # crediting the score for that ratio is the model grading its own
    # homework. See lib/signal_levels.LevelSource.
    entry_source     = Column(String)
    stop_source      = Column(String)
    target_source    = Column(String)
    verification_json = Column(Text)   # last double-check result (verdict, checks, prices)
    verified_at      = Column(String)                     # free-text trade journal note
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
    inst_id          = Column(String)                               # venue instId, e.g. BTC-USDT-SWAP
    # Which exchange this observation came from. Funding rates are NOT
    # comparable across venues without knowing each one's funding interval
    # (OKX pays 8-hourly on these majors, Crypto.com hourly), so every
    # consumer must either filter to one venue or normalize explicitly —
    # never average rows blind. transaction_costs pins itself to 'okx'.
    venue            = Column(String, default="okx", index=True)
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


class IpoFiling(Base):
    """One company's registration pipeline, keyed by CIK — free EDGAR data.
    stage progresses filed -> amended -> priced as S-1 / S-1/A / 424B4
    filings arrive (lib/ipo_intelligence.py). A row is updated in place when
    a later-stage filing appears; it never regresses to an earlier stage.

    Offering-term columns are populated ONLY from a 424B4 cover page and only
    when the conservative extraction patterns matched — NULL means "not
    stated / not extracted", never zero. is_likely_spac is a NAME heuristic
    (see _SPAC_NAME_RE) and is labeled as such in the UI."""
    __tablename__ = "ipo_filings"
    cik                = Column(String, primary_key=True)
    company_name       = Column(String, index=True)
    stage              = Column(String, index=True)      # filed | amended | priced
    latest_form        = Column(String)
    latest_accession   = Column(String)
    first_seen_at      = Column(String)
    latest_filed_at    = Column(String, index=True)
    ticker             = Column(String, index=True)
    exchange           = Column(String)
    offer_price        = Column(Float)
    shares_offered     = Column(Float)
    total_offering_usd = Column(Float)
    is_likely_spac     = Column(Boolean, default=False)
    # True/False only when a 424B4 cover was actually parsed; NULL = unknown
    # (cover not yet downloaded). False means the prospectus is a follow-on by
    # an already-listed company — Rule 424(b)(4) covers those too (observed
    # live), and they must not be presented as IPO pricings.
    cover_mentions_ipo = Column(Boolean)
    filing_url         = Column(String)
    updated_date       = Column(String, default=now_iso)


class SignalPostmortem(Base):
    """One row per signal that reached a terminal failure/cancel state — the
    memory of WHY things didn't work, keyed by a deterministic reason
    taxonomy (lib/postmortem.py) rather than free text, so failure modes can
    be counted, aggregated, and fed back into scoring. reason_detail keeps
    the human-readable specifics; market context (regime at collection) is
    recorded because the same setup can fail for regime reasons."""
    __tablename__ = "signal_postmortems"
    id             = Column(String, primary_key=True, default=new_id)
    signal_id      = Column(String, nullable=False, unique=True, index=True)
    symbol         = Column(String, index=True)
    asset_class    = Column(String)
    direction      = Column(String)
    timeframe      = Column(String)
    setup_type     = Column(String, index=True)
    signal_source  = Column(String)
    composite_score= Column(Float)
    terminal_status= Column(String)                 # Rejected | Expired | evaluation outcome
    reason_code    = Column(String, index=True)     # taxonomy in lib/postmortem.py
    reason_detail  = Column(Text)
    regime_label   = Column(String)
    generated_at   = Column(String)
    collected_at   = Column(String, default=now_iso, index=True)


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


class ApiCacheEntry(Base):
    """Last-known payload for panel routes backed by slow external APIs —
    served instantly after a restart while a background refresh runs
    (lib/api_cache.py). One row per route key."""
    __tablename__ = "api_cache"
    key        = Column(String, primary_key=True)
    payload    = Column(Text, nullable=False)
    fetched_at = Column(String, nullable=False)


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

class FocusProfile(Base):
    """Accumulated behavioural knowledge for a focus symbol — see
    lib/focus_profile.py. stats_json holds MEASURED numbers from real bars;
    narrative holds the LLM's character sketch, which is interpretation and
    is labelled as such wherever it is shown."""
    __tablename__ = "focus_profiles"
    symbol      = Column(String, primary_key=True)
    stats_json  = Column(Text)
    summary     = Column(Text)
    narrative   = Column(Text)
    updated_at  = Column(String, default=now_iso)


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
    # ── Focus list ("coins to watch") ────────────────────────────────────
    # A deliberately tiny set the desk watches CONTINUOUSLY and patiently:
    # analysed every cycle regardless of budget, across the full timeframe
    # ladder, and allowed to emit a signal ONLY when conviction clears a
    # much higher bar than the normal floor. Distinct from the watchlist,
    # which gets ordinary treatment.
    is_focus      = Column(Boolean, default=False)
    focus_note    = Column(String)      # why the operator is watching it
    focus_added   = Column(String)
    created_date  = Column(String, default=now_iso)
    updated_date  = Column(String, default=now_iso)

class SnapshotCache(Base):
    """Last good result of an expensive derivation, surviving restarts.

    The Morning Brief takes 172 seconds to derive and returns 7.7 KB. That
    cost was paid on every request and again from zero after every restart,
    which is what left the panel on "assembling…" and the desk looking dead
    after a crash. See lib/snapshot_cache.py for the serving rules; the
    short version is that a reading labelled four minutes old beats a blank
    panel, and it beats a three-minute block by more.
    """
    __tablename__ = "snapshot_cache"
    id          = Column(String, primary_key=True, default=new_id)
    key         = Column(String, unique=True, index=True)
    payload     = Column(Text)
    computed_at = Column(Float, default=0.0)
    compute_ms  = Column(Integer, default=0)
    last_error  = Column(String)


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
    # Which generation of the engine produced this outcome. Calibration reads
    # ONLY the current epoch: 93.6% of pre-epoch outcomes were closed by an
    # exit rule that no longer exists, so their win/loss labels describe a
    # machine that is gone. Quarantined, not deleted.
    engine_epoch     = Column(String)
    # "live" (observed) or "replay" (simulated under current rules against
    # real historical bars). A replayed fill is perfect — no slippage, no
    # partial fills, both the bar's high and low assumed reachable — so it
    # is systematically optimistic and must be weighted below live evidence
    # rather than pooled with it.
    outcome_source   = Column(String, default="live")
    # ── Path labels ──────────────────────────────────────────────────────
    # Entry and exit alone cannot distinguish a trade that ran straight to
    # target from one that sat two bars from being stopped out first. Same
    # P&L, entirely different risk, and only one of them is repeatable.
    # Produced by lib/signal_replay.py, which already walks the bars.
    mfe_r            = Column(Float)    # best excursion in favour, in R
    mae_r            = Column(Float)    # worst excursion against, in R
    mfe_bar          = Column(Integer)  # bars until MFE
    mae_bar          = Column(Integer)  # bars until MAE
    # STOP | TARGET | AMBIGUOUS | None. AMBIGUOUS means both levels sat
    # inside one OHLC bar: intrabar ordering is unknowable, and choosing the
    # profitable one is how a backtest manufactures an edge.
    first_touch      = Column(String)
    # REPLAY_OHLC | LIVE_OBSERVED. Replay assumes perfect fills and that a
    # bar's high AND low were both reachable, so it is systematically
    # optimistic and must never be pooled with observed paths.
    path_source      = Column(String)
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
    # Live (Alpaca paper) auto-execution criteria — an approved signal must
    # clear ALL of these to be placed on the broker account. Auto Sim takes
    # every approved signal regardless, so the two books can be compared.
    live_min_score      = Column(Float, default=55.0)
    live_min_rr         = Column(Float, default=0.0)   # 0 = no R:R gate
    live_min_confidence = Column(Float, default=0.0)   # 0 = no confidence gate
    updated_at          = Column(String, default=now_iso)


# ── Wallet intelligence registry (§141) ──────────────────────────────────
# The wallet universe used to be an environment variable, which made
# `HELIUS_WATCH_WALLETS` the database and left the whole subsystem reporting
# configured:false whenever it was blank. Wallets live here now; the env var
# becomes optional seeds and pinned overrides.
#
# Lifecycle, per §141:
#   DISCOVERED -> CANDIDATE -> ANALYZING -> WATCH -> SMART_MONEY
#                                                 -> HIGH_CONVICTION
#   and back down through DEGRADED -> ARCHIVED as edge decays.
#
# EXCLUDED_ENTITY is a terminal state, not a demotion. An exchange hot
# wallet or a router program is not a bad trader — it is not a trader. The
# largest BONK holder on the first live query was a Binance hot wallet, so
# without this the system's first "discovery" is a CEX moving customer
# funds, announced as a whale.
WALLET_STATES = (
    "DISCOVERED", "CANDIDATE", "ANALYZING", "WATCH",
    "SMART_MONEY", "HIGH_CONVICTION", "DEGRADED", "ARCHIVED",
    "EXCLUDED_ENTITY",
)


class WalletRegistry(Base):
    __tablename__ = "wallet_registry"

    address     = Column(String, primary_key=True)
    # Human name where one is known. Truncated base58 is unreadable, and an
    # operator who supplied a wallet because they know whose it is should
    # see that name back. A label is not evidence of quality.
    label       = Column(String)
    # How it got here: manual_seed | token_holders | counterparty |
    # funding_graph | co_trading. Kept so a bad discovery source can be
    # measured and switched off rather than guessed at.
    source          = Column(String, default="manual_seed")
    source_wallet   = Column(String)
    discovery_reason = Column(Text)

    status      = Column(String, default="DISCOVERED", index=True)
    # Pinned wallets survive automatic archival. A seed the operator chose
    # is never silently dropped because it went quiet for a week.
    pinned              = Column(Boolean, default=False)
    monitoring_enabled  = Column(Boolean, default=False)

    # Entity classification (§13). Decided BEFORE scoring, because scoring
    # infrastructure produces confident nonsense.
    entity_type = Column(String)          # exchange | program | pool | bridge…
    entity_name = Column(String)
    is_trader   = Column(Boolean)
    is_protocol = Column(Boolean, default=False)

    first_discovered_at = Column(String, default=now_iso)
    last_seen_at        = Column(String)
    last_analyzed_at    = Column(String)
    last_score_update   = Column(String)

    total_transactions = Column(Integer, default=0)
    total_swaps        = Column(Integer, default=0)
    qualified_trades   = Column(Integer, default=0)
    winning_trades     = Column(Integer, default=0)
    losing_trades      = Column(Integer, default=0)

    win_rate       = Column(Float)
    realized_pnl   = Column(Float)
    average_return = Column(Float)
    median_return  = Column(Float)
    profit_factor  = Column(Float)
    max_drawdown   = Column(Float)

    average_trade_size    = Column(Float)
    median_trade_size     = Column(Float)
    largest_trade         = Column(Float)
    average_holding_period = Column(Float)

    # What happened AFTER this wallet entered — the actual alpha question.
    entry_alpha_5m  = Column(Float)
    entry_alpha_15m = Column(Float)
    entry_alpha_1h  = Column(Float)
    entry_alpha_4h  = Column(Float)
    entry_alpha_24h = Column(Float)

    # Four SEPARATE scores. Collapsing them is the mistake §10 exists to
    # prevent: a wallet can be genuinely skilled and completely uncopyable.
    whale_score        = Column(Float)
    smart_money_score  = Column(Float)
    # POST-ENTRY MARKET ALPHA — what happened to the token after this wallet
    # entered. Populated from entry_alpha_* observations; NULL until enough
    # of them exist. It previously held the wallet's own realized round-trip
    # return under a comment claiming it was post-entry alpha, which is a
    # different metric entirely.
    alpha_score        = Column(Float)
    # The old number, preserved under its true name so a historical row
    # stays interpretable and the two semantics never share a column.
    legacy_alpha_score = Column(Float)
    coordination_score = Column(Float)
    copy_score         = Column(Float)
    # Which scoring engine produced this row. Pre-USD-normalization scores
    # were computed by summing mixed quote units and are not comparable
    # with anything after it.
    wallet_score_version = Column(String)

    # ── Analysis diagnostics ─────────────────────────────────────────────
    # ZERO, UNKNOWN, INSUFFICIENT and PROVIDER_FAILURE are FOUR different
    # states and must never collapse into one another. "we measured zero
    # trades" and "we could not measure this run" are different facts, and
    # a scorer that overwrites a known qualified_trades=12 with 0 because
    # Helius timed out has destroyed evidence and called it a measurement.
    #
    # analysis_status:  MEASURED | INSUFFICIENT | NO_VERIFIED_TRADES
    #                   | DEGRADED | FAILED
    analysis_status       = Column(String)
    # Why a score is absent, in the vocabulary the UI explains it with.
    measurability_reason  = Column(String)
    # False means "the sample does not support a score", NOT "bad wallet".
    measurable            = Column(Boolean)
    # What we had, and what we needed — so "3 of 15" is expressible and
    # never has to be rendered as "0 of 15".
    sample_count          = Column(Integer)
    required_sample_count = Column(Integer)
    # Round trips that could not be valued in USD, kept separate from
    # trades that simply did not happen.
    unpriced_trades       = Column(Integer)
    # When analysis last RAN, as distinct from when a score last CHANGED —
    # a failed run updates this and leaves last_score_update alone, so
    # stale evidence is visible as stale rather than reading as current.
    last_analysis_at      = Column(String)
    analysis_error        = Column(String)
    # Confidence is separate from score so a 100% win rate over 2 trades
    # cannot outrank 71% over 167. §29's sample-size discipline.
    confidence_score   = Column(Float)

    cluster_id         = Column(String, index=True)
    funded_by          = Column(String)
    funding_cluster_id = Column(String)

    # §116: WALLET_ALPHA records must never enter CRYPTO_MAJORS training,
    # calibration, expectancy or Gate. Stamped at birth so the guard can
    # assert on it rather than infer.
    population = Column(String, default="WALLET_ALPHA")

    notes      = Column(Text)
    updated_at = Column(String, default=now_iso, onupdate=now_iso)


class WalletTrade(Base):
    """One reconstructed trade. Raw transfers are NOT trades (§9) — a token
    account being created, a wrapped-SOL unwrap and a genuine swap all look
    alike until they are reconstructed, and counting transfers as trades is
    how a wallet gets a fabricated win rate."""
    __tablename__ = "wallet_trades"
    __table_args__ = (
        # One signature moves several mints between several counterparties,
        # so the signature alone is NOT unique. Measured on live data.
        UniqueConstraint("signature", "mint", "counterparty", "direction",
                         name="uq_wallet_trade_identity"),
    )

    id        = Column(String, primary_key=True, default=new_id)
    address   = Column(String, index=True, nullable=False)
    signature = Column(String, nullable=False)
    mint      = Column(String, nullable=False)
    counterparty = Column(String, default="")
    direction = Column(String, nullable=False)      # buy | sell

    token_symbol = Column(String)
    quantity     = Column(Float)
    # `amount` from /v1/transfers, never decimals/amountRaw — those are
    # unreliable per-token on live data (a USDT row reported decimals 0
    # when USDT genuinely has 6).
    value_usd    = Column(Float)
    price        = Column(Float)
    price_source = Column(String)
    dex          = Column(String)
    fees_usd     = Column(Float)

    opened_at        = Column(String, index=True)
    closed_at        = Column(String)
    holding_period_s = Column(Float)
    realized_pnl     = Column(Float)
    return_pct       = Column(Float)

    # Post-entry price path — the alpha measurement. Null where no price
    # history exists for that mint; abstaining beats inventing a return.
    price_5m_after  = Column(Float)
    price_15m_after = Column(Float)
    price_1h_after  = Column(Float)
    price_4h_after  = Column(Float)
    price_24h_after = Column(Float)
    mfe = Column(Float)
    mae = Column(Float)

    population = Column(String, default="WALLET_ALPHA")
    created_at = Column(String, default=now_iso)


class WalletObservation(Base):
    """One sighting of one wallet entering one token. APPEND-ONLY.

    The registry holds ONE row per wallet — its identity. This holds MANY
    rows per wallet — its evidence. Discovery used to see an existing
    registry row and immediately `continue`, throwing away the sighting.
    If wallet A appears before ten independent token surges, that recurrence
    is among the most valuable evidence in the system, and it was being
    discarded because the wallet was already "known".

    It also carries POST-ENTRY MARKET ALPHA, which is a different question
    from whether the wallet traded well:

        realized return   did the WALLET make money?
        post-entry alpha  what did the TOKEN do after the wallet entered?

    A wallet can buy at $10, watch the token run to $15 within the hour,
    hold too long and exit at $9. Its realized return is negative and its
    1h post-entry alpha is strongly positive — a follower who copied the
    entry and took the hour would have done well. Those two numbers must
    never be collapsed, which is exactly what the old `alpha_score` did.

    Horizons are resolved independently and late: `return_1h` is filled an
    hour after the entry, `return_24h` a day after. A NULL horizon means
    not-yet-resolved, never zero.
    """
    __tablename__ = "wallet_observations"

    id = Column(String, primary_key=True, default=new_id)

    wallet_address = Column(String, index=True, nullable=False)
    mint           = Column(String, index=True, nullable=False)
    pool           = Column(String)
    token_symbol   = Column(String)

    # Where this sighting came from, and what it was near.
    discovery_source = Column(String)      # token_holders | pool_traders | ...
    surge_event_id   = Column(String, index=True)
    surge_started_at = Column(String)
    # NEGATIVE means the wallet was EARLY — it entered before the surge
    # crossed its threshold, which is the population worth finding.
    seconds_before_surge = Column(Float)

    signature      = Column(String, index=True)
    entry_timestamp = Column(String, index=True)
    entry_amount   = Column(Float)
    entry_notional_usd = Column(Float)
    entry_price_usd = Column(Float)

    # Forward prices, resolved as each horizon elapses. NULL = pending.
    price_5m  = Column(Float)
    price_15m = Column(Float)
    price_1h  = Column(Float)
    price_4h  = Column(Float)
    price_24h = Column(Float)

    return_5m  = Column(Float)
    return_15m = Column(Float)
    return_1h  = Column(Float)
    return_4h  = Column(Float)
    return_24h = Column(Float)

    # Which horizons have actually been resolved, so a pending observation
    # is never mistaken for a flat one.
    horizons_resolved = Column(String, default="")
    fully_resolved    = Column(Integer, default=0, index=True)

    price_source  = Column(String)
    price_quality = Column(String)

    observed_at = Column(String, default=now_iso, index=True)
    updated_at  = Column(String, default=now_iso, onupdate=now_iso)

    __table_args__ = (
        # One observation per wallet per entry. A re-scan of the same
        # signature is the same sighting, not new evidence.
        UniqueConstraint("wallet_address", "signature", "mint",
                         name="uq_wallet_observation"),
        Index("ix_obs_wallet_time", "wallet_address", "entry_timestamp"),
    )


class WalletRelationship(Base):
    """Edges in the wallet graph. Shared funding is NOT proof of shared
    ownership and co-trading is NOT proof of collusion (§141 safety rules),
    so every edge carries a confidence and a kind rather than a verdict."""
    __tablename__ = "wallet_relationships"
    __table_args__ = (
        UniqueConstraint("from_address", "to_address", "kind",
                         name="uq_wallet_relationship"),
    )

    id           = Column(String, primary_key=True, default=new_id)
    from_address = Column(String, index=True, nullable=False)
    to_address   = Column(String, index=True, nullable=False)
    # funded_by | transferred_to | traded_same_token | coordinated_with
    kind       = Column(String, nullable=False)
    confidence = Column(Float, default=0.0)
    evidence   = Column(Text)
    observations = Column(Integer, default=1)
    first_seen_at = Column(String, default=now_iso)
    last_seen_at  = Column(String, default=now_iso)


class WalletCapitalEvent(Base):
    """One normalised capital action — the layer above BUY/SELL/TRANSFER.

    WalletTrade can only describe a swap: mint, counterparty, direction,
    quantity, price. It has nowhere to put a collateral deposit, a borrow,
    a stake delegation or a liquidation, so a wallet that pledges JitoSOL,
    borrows USDC and buys a token reads as one unexplained purchase with
    the financing invisible.

    `confidence` and `unparsed_programs` are first-class because the honest
    answer is often partial. A transaction touching an unregistered program
    is recorded as UNPARSEABLE with the program listed, never guessed into
    the nearest familiar shape.
    """
    __tablename__ = "wallet_capital_events"
    __table_args__ = (
        # One signature legitimately produces several events (a Jupiter
        # route through three pools), so identity includes the leg.
        UniqueConstraint("signature", "event_index", name="uq_capital_event"),
    )

    id        = Column(String, primary_key=True, default=new_id)
    address   = Column(String, index=True, nullable=False)
    signature = Column(String, index=True, nullable=False)
    event_index = Column(Integer, default=0)
    block_time  = Column(String, index=True)
    slot        = Column(Integer)

    # SWAP_BUY | STAKE | LIQUID_STAKE | LENDING_DEPOSIT | BORROW | REPAY |
    # COLLATERAL_DEPOSIT | LP_ADD | BRIDGE_IN | LIQUIDATION | UNPARSEABLE ...
    event_type    = Column(String, index=True, nullable=False)
    protocol      = Column(String, index=True)
    protocol_type = Column(String)
    program_id    = Column(String)

    asset      = Column(String, index=True)
    asset_symbol = Column(String)
    amount     = Column(Float)
    value_usd  = Column(Float)

    # Populated for lending/leverage events only.
    collateral_asset     = Column(String)
    collateral_amount    = Column(Float)
    collateral_value_usd = Column(Float)
    borrow_asset     = Column(String)
    borrow_amount    = Column(Float)
    borrow_value_usd = Column(Float)
    position_id      = Column(String, index=True)

    health_factor        = Column(Float)
    ltv                  = Column(Float)
    liquidation_threshold = Column(Float)
    liquidation_price    = Column(Float)

    # Does the wallet still hold SOL exposure after this? SOL -> JitoSOL
    # lowers the raw SOL balance and changes nothing about the position;
    # without this the conversion is indistinguishable from a sale.
    retains_sol_exposure = Column(Boolean)

    strategy_chain_id = Column(String, index=True)
    confidence        = Column(Float, default=0.0)
    unparsed_programs = Column(Text)
    raw_note          = Column(Text)
    created_at        = Column(String, default=now_iso)


class WalletStrategyChain(Base):
    """Several capital events that are one intent.

    stake -> collateral -> borrow -> swap is not four unrelated
    transactions; it is one leveraged deployment, and the difference
    decides whether the buy reads as ordinary conviction or as borrowed
    conviction with a liquidation price attached.
    """
    __tablename__ = "wallet_strategy_chains"

    id       = Column(String, primary_key=True, default=new_id)
    address  = Column(String, index=True, nullable=False)
    # LEVERAGED_LONG | SPOT_ACCUMULATION | DELEVERAGING | CAPITAL_PREP |
    # LEVERAGED_STAKING | LIQUIDITY_PROVIDING | CAPITAL_ROTATION ...
    strategy_type = Column(String, index=True)
    status        = Column(String, default="open", index=True)

    started_at = Column(String, default=now_iso, index=True)
    updated_at = Column(String, default=now_iso)
    closed_at  = Column(String)

    capital_usd      = Column(Float)
    borrowed_usd     = Column(Float)
    leverage_estimate = Column(Float)
    primary_asset    = Column(String)
    secondary_asset  = Column(String)
    protocols        = Column(Text)
    event_count      = Column(Integer, default=0)
    confidence       = Column(Float, default=0.0)
    narrative        = Column(Text)


class WalletLiquidationRisk(Base):
    """A leveraged position's distance to forced selling.

    Recalculated when PRICES move, not only when the wallet transacts — a
    position walks toward liquidation while its owner does nothing, and a
    risk engine that only wakes on transactions learns about the cascade
    from the liquidation itself.
    """
    __tablename__ = "wallet_liquidation_risk"
    __table_args__ = (
        UniqueConstraint("address", "protocol", "position_id",
                         name="uq_liquidation_position"),
    )

    id       = Column(String, primary_key=True, default=new_id)
    address  = Column(String, index=True, nullable=False)
    protocol = Column(String, index=True)
    position_id = Column(String)

    collateral_asset     = Column(String)
    collateral_amount    = Column(Float)
    collateral_value_usd = Column(Float)
    debt_asset     = Column(String)
    debt_amount    = Column(Float)
    debt_value_usd = Column(Float)

    ltv            = Column(Float)
    health_factor  = Column(Float)
    liquidation_threshold = Column(Float)
    estimated_liquidation_price = Column(Float)
    distance_to_liquidation_pct = Column(Float, index=True)

    # SAFE | ELEVATED | HIGH | CRITICAL | LIQUIDATION_IN_PROGRESS | LIQUIDATED
    risk_state = Column(String, default="SAFE", index=True)
    risk_score = Column(Float, default=0.0, index=True)
    potential_forced_sale_usd = Column(Float)
    # Health factor trend, so a deteriorating position outranks a static one.
    health_trend = Column(String)
    previous_health_factor = Column(Float)

    wallet_alpha_score = Column(Float)
    last_updated = Column(String, default=now_iso, index=True)
    last_price_check = Column(String)


class TokenActivitySnapshot(Base):
    """One observation of a pool's activity, kept so acceleration can be
    measured against THIS token's own history.

    Without stored snapshots the only available baseline is whatever
    buckets the market API happens to return in the same response, which
    answers "is h1 busier than h24/24" — a fixed, coarse comparison that
    cannot see a token whose normal 5m volume is $1,200 suddenly doing
    $85,000. A median over the token's own recent snapshots can.

    Deliberately append-only and cheap: one row per pool per scan, pruned
    by age, never updated in place. A baseline that gets rewritten is not a
    baseline.
    """
    __tablename__ = "token_activity_snapshots"

    id           = Column(String, primary_key=True, default=new_id)
    mint         = Column(String, index=True, nullable=False)
    pool_address = Column(String, index=True)
    symbol       = Column(String)
    network      = Column(String, default="solana")
    captured_at  = Column(String, default=now_iso, index=True)

    price_usd     = Column(Float)
    liquidity_usd = Column(Float)

    volume_m5   = Column(Float)
    volume_m15  = Column(Float)
    volume_m30  = Column(Float)
    volume_h1   = Column(Float)
    volume_h6   = Column(Float)
    volume_h24  = Column(Float)

    buys_m5    = Column(Integer)
    sells_m5   = Column(Integer)
    buyers_m5  = Column(Integer)
    sellers_m5 = Column(Integer)
    buys_h1    = Column(Integer)
    sells_h1   = Column(Integer)
    buyers_h1  = Column(Integer)
    sellers_h1 = Column(Integer)

    price_change_m5  = Column(Float)
    price_change_h1  = Column(Float)
    price_change_h6  = Column(Float)
    price_change_h24 = Column(Float)


class TokenSurgeState(Base):
    """Current surge standing per token, with hysteresis.

    Exists so a token scoring 85 for ten consecutive scans emits ONE event
    rather than ten identical ones — the difference between an alert and a
    stuck alarm nobody reads.
    """
    __tablename__ = "token_surge_state"

    mint          = Column(String, primary_key=True)
    pool_address  = Column(String)
    symbol        = Column(String)
    # NORMAL | SURGING | INVESTIGATING | MONITORED | COOLDOWN
    state         = Column(String, default="NORMAL", index=True)
    surge_score   = Column(Float, default=0.0, index=True)
    peak_score    = Column(Float, default=0.0)
    bias          = Column(String)            # bullish | bearish | mixed | unknown
    baseline_quality = Column(String)         # measured | insufficient | new_token
    metrics_json  = Column(Text)
    first_seen_at = Column(String, default=now_iso)
    last_scan_at  = Column(String, default=now_iso)
    last_event_at = Column(String)
    last_event_score = Column(Float)
    scans         = Column(Integer, default=0)
    cooldown_until = Column(String)
    # WHEN the surge began — the T0 that pre-surge wallet discovery searches
    # backwards from. Distinct from first_seen_at, which is only when this
    # token was first scanned. Cleared on return to NORMAL.
    surge_started_at = Column(String)


class DexPosition(Base):
    """A simulated on-chain spot position. Deliberately NOT PaperPosition.

    An AMM swap and a broker fill are different events, and the fields that
    matter barely overlap. There is no leverage here and no short side: you
    cannot borrow from a constant-product pool, so a book that offers 5x on
    a memecoin is describing a venue that does not exist. Size is bounded
    by POOL DEPTH rather than by account equity — the binding constraint
    on-chain is how much the pool can absorb, which is why entry impact is
    stored on the row instead of being recomputed later from a mid price
    that has since moved.
    """
    __tablename__ = "dex_positions"

    id          = Column(String, primary_key=True, default=new_id)
    user_id     = Column(String, default=DEFAULT_USER_ID, index=True)
    mint        = Column(String, index=True, nullable=False)
    symbol      = Column(String)
    pool_address = Column(String, index=True)
    dex         = Column(String)
    network     = Column(String, default="solana")

    status      = Column(String, default="Open", index=True)
    qty_tokens  = Column(Float)          # tokens actually received
    entry_price_usd = Column(Float)      # AVERAGE achieved, not the quote
    quoted_price_usd = Column(Float)     # spot at decision time
    notional_usd = Column(Float)         # USD committed

    # Entry costs, kept separate — one is the pool's price, one is your own
    # size, and they call for different remedies.
    entry_pool_fee_usd  = Column(Float, default=0.0)
    entry_impact_usd    = Column(Float, default=0.0)
    entry_impact_pct    = Column(Float, default=0.0)
    entry_network_fee_usd = Column(Float, default=0.0)
    pool_reserve_usd_at_entry = Column(Float)

    stop_price_usd   = Column(Float)
    target_price_usd = Column(Float)
    current_price_usd = Column(Float)
    unrealized_pnl_usd = Column(Float, default=0.0)

    signal_id  = Column(String, index=True)
    opened_at  = Column(String, default=now_iso, index=True)
    updated_at = Column(String, default=now_iso)
    notes      = Column(Text)


class DexTrade(Base):
    """A closed simulated swap round trip, with every cost itemised."""
    __tablename__ = "dex_trades"

    id          = Column(String, primary_key=True, default=new_id)
    user_id     = Column(String, default=DEFAULT_USER_ID, index=True)
    position_id = Column(String, index=True)
    mint        = Column(String, index=True)
    symbol      = Column(String)
    pool_address = Column(String)
    dex         = Column(String)

    qty_tokens  = Column(Float)
    notional_usd = Column(Float)
    entry_price_usd = Column(Float)
    exit_price_usd  = Column(Float)

    # Gross is the price move; net is what the book actually keeps. On a
    # thin pool the gap between them can exceed the move itself.
    gross_pnl_usd = Column(Float)
    total_costs_usd = Column(Float)
    net_pnl_usd  = Column(Float)
    net_pnl_pct  = Column(Float)

    entry_impact_pct = Column(Float)
    exit_impact_pct  = Column(Float)
    pool_fees_usd    = Column(Float)
    network_fees_usd = Column(Float)

    reason     = Column(String)
    opened_at  = Column(String, index=True)
    closed_at  = Column(String, default=now_iso, index=True)
    hold_minutes = Column(Float)


class DexPortfolio(Base):
    """The virtual on-chain wallet. `reset_at` is the epoch watermark —
    equity derives from trades AFTER it, so a reset cannot be undone by a
    stale row and history is never deleted to move a number."""
    __tablename__ = "dex_portfolio"

    id            = Column(String, primary_key=True, default=new_id)
    user_id       = Column(String, default=DEFAULT_USER_ID, unique=True)
    starting_usd  = Column(Float, default=10_000.0)
    cash_usd      = Column(Float, default=10_000.0)
    realized_pnl_usd = Column(Float, default=0.0)
    total_trades  = Column(Integer, default=0)
    wins          = Column(Integer, default=0)
    losses        = Column(Integer, default=0)
    reset_at      = Column(String)
    updated_at    = Column(String, default=now_iso)


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
    # Exposure as solved at open (qty x unit value), the quantity the
    # concentration guard bounds. Auto Sim stored only margin, so its book
    # reported zero exposure to anything that asked — and margin says
    # nothing about exposure once leverage is involved. Derived rows
    # (pre-migration) fall back to qty x entry in lib/concentration.
    notional          = Column(Float)
    fees              = Column(Float, default=0.0)   # round-trip venue cost, charged at open
    fee_basis         = Column(String)
    entry_slippage_pct = Column(Float, default=0.0)
    unrealized_pnl    = Column(Float, default=0.0)   # NET of fees
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
    gross_pnl    = Column(Float, default=0.0)   # price move only
    fees         = Column(Float, default=0.0)   # venue round trip
    fee_basis    = Column(String)
    realized_pnl = Column(Float, default=0.0)   # NET = gross - fees
    pnl_pct      = Column(Float, default=0.0)
    close_reason = Column(String)
    opened_at    = Column(String)
    closed_at    = Column(String, default=now_iso)


class AutoSimPortfolio(Base):
    __tablename__ = "auto_sim_portfolios"
    user_id      = Column(String, primary_key=True, default=DEFAULT_USER_ID)
    starting_cash= Column(Float, default=100000.0)
    # These four are a CACHE of what the trades table already records. They
    # are incremented on every close, which makes them vulnerable to any
    # concurrent session holding a stale copy — a soft reset zeroed them and
    # a background job's in-flight portfolio object wrote the old totals
    # straight back, so the book read insolvent again minutes later with
    # zero trades in between. `reset_at` is the durable fix: realized P&L is
    # DERIVED from trades closed after the watermark, so no stale object can
    # resurrect a cleared book.
    realized_pnl = Column(Float, default=0.0)
    total_trades = Column(Integer, default=0)
    wins         = Column(Integer, default=0)
    losses       = Column(Integer, default=0)
    # Trades closed at or before this instant belong to a previous book.
    # They stay in the table — they are learning data — but they no longer
    # count toward this book's equity.
    reset_at     = Column(String)
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
            ("strategy", "TEXT"),
            ("strategy_score", "REAL"),
            ("invalidation", "TEXT"),
            ("signal_version", "TEXT DEFAULT 'v7.2'"),
            ("llm_model", "TEXT"),
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
            ("verification_json", "TEXT"),
            ("entry_source", "TEXT"),
            ("stop_source", "TEXT"),
            ("target_source", "TEXT"),
            ("verified_at", "TEXT"),
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
        # The paper book computed venue fees in size_position for DISPLAY but
        # never charged them: `cash += margin + pnl`. Same omission Auto Sim
        # had — a book whose costs are optional cannot be compared against
        # one whose costs are real.
        "paper_positions": [
            ("fees",      "REAL DEFAULT 0.0"),
            ("fee_basis", "TEXT"),
            ("initial_stop_loss", "REAL"),
        ],
        "paper_trades": [
            ("gross_pnl", "REAL DEFAULT 0.0"),
            ("fees",      "REAL DEFAULT 0.0"),
            ("fee_basis", "TEXT"),
        ],
        "telegram_deliveries": [
            ("setup_key", "TEXT"),
            ("setup_state", "TEXT"),
            ("message_id", "TEXT"),
            ("updated_at", "TEXT"),
        ],
        "market_assets": [
            ("is_focus", "INTEGER DEFAULT 0"),
            ("focus_note", "TEXT"),
            ("focus_added", "TEXT"),
        ],
        # Which generation of the engine produced this outcome. Everything
        # recorded before 2026-08-13 came from a system where fees went
        # uncharged, stops fired at 0.12%, positions closed when their ENTRY
        # signal expired, futures P&L was off by the contract multiplier and
        # sub-cent prices were rounded to nothing. 93.6% of those outcomes
        # were closed by an exit rule that no longer exists, so they measure
        # a machine that is gone.
        "trade_outcomes": [
            ("engine_epoch", "TEXT"),
            ("outcome_source", "TEXT DEFAULT 'live'"),
            # Path labels — see the TradeOutcome model. Nullable on purpose:
            # rows written before this existed genuinely have no path, and a
            # zero would train as "never moved", which is a claim.
            ("mfe_r", "REAL"),
            ("mae_r", "REAL"),
            ("mfe_bar", "INTEGER"),
            ("mae_bar", "INTEGER"),
            ("first_touch", "TEXT"),
            ("path_source", "TEXT"),
        ],
        # The gate experiment: both arms' verdicts, recorded at candidate
        # birth. Nullable — rows from before the experiment simply have no
        # verdicts, and the scoreboard only compares rows that carry both.
        "execution_samples": [
            ("initial_stop_loss", "REAL"),
            ("approved_risk_usd", "REAL"),
            ("approved_notional", "REAL"),
        ],
        "candidate_signals": [
            ("gate_legacy_take", "INTEGER"),
            ("gate_v8_decision", "TEXT"),
            ("gate_v8_take",     "INTEGER"),
            ("gate_v8_reason",   "TEXT"),
            ("gate_v8_net_r",    "REAL"),
            # Point-in-time macro context at the moment of judgment
            # (funding, COT percentile, curve, short ratio) — the join
            # the 4C ablation needs. NULL = nothing was known, honestly.
            ("market_context",   "TEXT"),
            # Only generate_signals.py ever wrote candidates, so every
            # pre-existing row is truthfully 'generator'.
            ("source", "TEXT DEFAULT 'generator'"),
        ],
        "user_preferences": [
            ("paper_auto_trade_enabled", "INTEGER DEFAULT 1"),
            ("live_min_score", "REAL DEFAULT 55.0"),
            ("live_min_rr", "REAL DEFAULT 0.0"),
            ("live_min_confidence", "REAL DEFAULT 0.0"),
        ],
        # Second derivatives venue. Every pre-existing row was OKX, so the
        # default backfills them truthfully.
        "crypto_derivatives_snapshots": [
            ("venue", "TEXT DEFAULT 'okx'"),
        ],
        # Auto Sim priced every trade as free: no fee, no spread. A book that
        # cannot lose money to costs will always look profitable, so its P&L
        # could not be compared against the paper book (which does charge
        # venue fees). These columns carry the cost side of the ledger.
        # The registry is young and will gain columns as §141 lands; every
        # one goes here so an existing database picks it up without a drop.
        "wallet_registry": [
            ("label", "TEXT"),
        ],
        "auto_sim_portfolios": [
            ("reset_at", "TEXT"),
        ],
        "paper_portfolio": [
            ("reset_at", "TEXT"),
        ],
        "auto_sim_positions": [
            ("fees",       "REAL DEFAULT 0.0"),   # round trip, reserved at open
            ("fee_basis",  "TEXT"),
            ("entry_slippage_pct", "REAL DEFAULT 0.0"),
            # Exposure at open. Without it the concentration guard reads
            # every Auto Sim row as zero exposure; existing rows stay NULL
            # and fall back to qty x entry.
            ("notional",   "REAL"),
        ],
        "auto_sim_trades": [
            ("gross_pnl",  "REAL DEFAULT 0.0"),   # before costs
            ("fees",       "REAL DEFAULT 0.0"),
            ("fee_basis",  "TEXT"),
        ],
        "wallet_registry": [
            # W4/W5. Existing alpha_score values were the wallet's own
            # realized return, not post-entry alpha; they move to
            # legacy_alpha_score so the two semantics never share a column.
            ("legacy_alpha_score", "REAL"),
            ("wallet_score_version", "TEXT"),
            # ZERO / UNKNOWN / INSUFFICIENT / PROVIDER_FAILURE are four
            # states, not one. Without these the scorer could only say
            # "no score" and the UI had to guess which of the four it meant.
            ("analysis_status", "TEXT"),
            ("measurability_reason", "TEXT"),
            ("measurable", "INTEGER"),
            ("sample_count", "INTEGER"),
            ("required_sample_count", "INTEGER"),
            ("unpriced_trades", "INTEGER"),
            ("last_analysis_at", "TEXT"),
            ("analysis_error", "TEXT"),
        ],
        "token_surge_state": [
            # WHEN the surge began, not merely when the row was created.
            # Pre-surge wallet discovery asks "who entered before T0", and
            # without a stored T0 there is no window to search backwards
            # from. Cleared when the token returns to NORMAL.
            ("surge_started_at", "TEXT"),
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
            # LLM routing telemetry — see lib/llm_router.py. signal_id is
            # what lets this table join to trade_outcomes, which is the only
            # way to answer whether thinking mode pays for itself.
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id TEXT PRIMARY KEY,
                    task TEXT,
                    mode_requested TEXT,
                    thinking INTEGER,
                    reason TEXT,
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    latency_ms REAL,
                    response_chars INTEGER,
                    ok INTEGER DEFAULT 1,
                    error TEXT,
                    signal_id TEXT,
                    symbol TEXT,
                    created_at TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS execution_samples (
                    id TEXT PRIMARY KEY,
                    signal_id TEXT, symbol TEXT, asset_class TEXT, venue TEXT,
                    side TEXT, order_type TEXT,
                    intended_price REAL, qty REAL, stop_loss REAL,
                    broker_order_id TEXT, status TEXT DEFAULT 'PENDING',
                    microstructure TEXT,
                    spread_pct_at_submit REAL, book_imbalance_at_submit REAL,
                    fill_price REAL, filled_qty REAL, fill_ratio REAL,
                    fill_delay_ms REAL, slippage_pct REAL, slippage_bps REAL,
                    submitted_at TEXT, resolved_at TEXT
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_exec_samples_symbol "
                "ON execution_samples(symbol, submitted_at)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task, thinking)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_signal ON llm_calls(signal_id)"))
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
    # The stop AS PLACED at open, never mutated afterward. stop_loss above
    # is trailed by the position manager, so by close it often sits at
    # breakeven — and anything that divides by |entry - stop| (R multiples)
    # then divides by pennies. A ^VIX short that lost $19.29 reported
    # R = -1,063,462 because its trailed stop differed from entry by
    # $0.00000004. Initial risk is a fact about the trade at birth; it
    # must be recorded at birth.
    initial_stop_loss = Column(Float)
    notional      = Column(Float)           # total exposure = qty * entry_price * leverage
    margin_used   = Column(Float)           # cash reserved = notional / leverage
    fees          = Column(Float, default=0.0)   # venue round trip, reserved at open
    fee_basis     = Column(String)
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
    gross_pnl     = Column(Float, default=0.0)   # price move only
    fees          = Column(Float, default=0.0)   # venue round trip
    fee_basis     = Column(String)
    realized_pnl  = Column(Float)                # NET = gross - fees
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
    # Same watermark, same reason — see AutoSimPortfolio.reset_at.
    reset_at       = Column(String)
    updated_at     = Column(String, default=now_iso)

class ExecutionSample(Base):
    """One order, the book it was sent into, and what it actually cost.

    Phase 4 failed for want of data: 4 of 39,821 signals carried a measured
    slippage and nothing persisted the order book at all. No later
    cleverness recovers a measurement nobody took, so this starts
    collecting now.

    The columns that matter are the *_at_submit ones. Spread and imbalance
    read after the fill are contaminated by the fill itself — the order
    moved the book it would be measured against.

    PENDING rows are kept. An order that never filled is a real observation
    about liquidity, and dropping those biases the dataset toward moments
    when trading happened to be easy.
    """
    __tablename__ = "execution_samples"
    id                = Column(String, primary_key=True, default=new_id)
    signal_id         = Column(String)
    symbol            = Column(String)
    asset_class       = Column(String)
    venue             = Column(String)     # behaviour differs per venue
    side              = Column(String)     # buy | sell
    order_type        = Column(String)     # market | limit
    intended_price    = Column(Float)
    qty               = Column(Float)
    stop_loss         = Column(Float)
    # ── Immutable approved facts at trade birth (P0.12) ────────────────
    # The stop AS APPROVED for submission, the risk the approval implied,
    # and the notional it authorized. Written once, never trailed, never
    # recomputed — every later R and learning read uses THESE, because the
    # signal's stop and the live stop can both diverge from what was
    # actually placed.
    initial_stop_loss = Column(Float)
    approved_risk_usd = Column(Float)
    approved_notional = Column(Float)
    broker_order_id   = Column(String)
    status            = Column(String, default="PENDING")
    # ── market state AT SUBMIT ────────────────────────────────────────
    microstructure    = Column(Text)       # full JSON snapshot
    spread_pct_at_submit     = Column(Float)
    book_imbalance_at_submit = Column(Float)
    # ── what happened ─────────────────────────────────────────────────
    fill_price        = Column(Float)
    filled_qty        = Column(Float)
    fill_ratio        = Column(Float)      # partial fills are data
    fill_delay_ms     = Column(Float)
    # Signed so POSITIVE is always worse than intended, for both sides.
    # Unsigned, longs and shorts average into a comfortable zero.
    slippage_pct      = Column(Float)
    slippage_bps      = Column(Float)
    submitted_at      = Column(String, default=now_iso)
    resolved_at       = Column(String)


class KrakenTrade(Base):
    """One REAL fill from the operator's Kraken account, synced read-only.

    Ground truth for the execution model: actual price, actual fee, actual
    size — including manual trades Jarvis never placed. The Kraken trade id
    is the primary key, so re-syncing an overlapping window is idempotent
    and resume-after-downtime needs no bookkeeping.
    """
    __tablename__ = "kraken_trades"
    trade_id         = Column(String, primary_key=True)   # Kraken's own id
    order_id         = Column(String)
    pair             = Column(String, index=True)         # Kraken pair name
    side             = Column(String)                     # buy | sell
    order_type       = Column(String)                     # market | limit | ...
    price            = Column(Float)
    cost             = Column(Float)                      # quote-ccy notional
    fee              = Column(Float)
    volume           = Column(Float)
    margin           = Column(Float)
    executed_at_unix = Column(Float, index=True)
    executed_at      = Column(String)
    synced_at        = Column(String, default=now_iso)


class CandidateSignal(Base):
    """Every setup the system CONSIDERED — including the ones it refused.

    Until now only surviving signals were persisted; anything under
    MIN_PERSIST_SCORE or below the focus bar vanished. That means the
    filters could never be evaluated: the system only learned from trades
    its own filters already approved, which is selection bias by
    construction. The question "would the rejected ones have won?" was
    unanswerable — and with the composite score measured INVERTED, it is
    exactly the question that matters.

    Rows are immutable once written except for the resolution fields, which
    are filled in later by counterfactual replay. The original judgment —
    score, breakdown, verdict, rejection reason — is never rewritten after
    the fact; hindsight editing its own paper trail is how a learning
    system lies to itself.
    """
    __tablename__ = "candidate_signals"
    id               = Column(String, primary_key=True, default=new_id)
    created_at       = Column(String, default=now_iso)
    engine_epoch     = Column(String)
    # identity — also the dedup key, since generators re-emit the same
    # setup for many cycles while it remains valid
    dedup_hash       = Column(String, index=True)
    symbol           = Column(String, index=True)
    asset_class      = Column(String)
    timeframe        = Column(String)
    direction        = Column(String)
    strategy         = Column(String)
    entry_price      = Column(Float)
    stop_loss        = Column(Float)
    target_price     = Column(Float)
    # the judgment as it stood at creation
    composite_score  = Column(Float)
    score_breakdown  = Column(Text)      # JSON, same shape as trading_signals
    shadow_variants  = Column(Text)      # JSON {schema, B, C} — logged, never acted on
    verdict          = Column(String)    # persisted | rejected
    rejection_reason = Column(String)    # below_min_persist | below_focus_bar | ...
    signal_id        = Column(String)    # link when verdict == persisted
    # WHICH writer considered this setup. Added 2026-08-16 when an audit
    # found the scanner — more than half the desk's signal output — never
    # recorded candidates at all, so its setups carried no gate verdict and
    # every card read UNMEASURED. Existing rows backfill to 'generator'
    # because only generate_signals.py ever wrote here. The gate
    # experiment's running window is evaluated generator-only so widening
    # the population cannot move its goalposts mid-flight.
    source           = Column(String, default="generator", index=True)
    paper_mode       = Column(Boolean, default=False)
    # ── The gate experiment (HARDENING_PLAN: legacy vs v8, side by side) ──
    # Both verdicts recorded at birth, immutable, judged against the same
    # counterfactual outcomes. gate_legacy_take is what the retired
    # composite>=threshold query WOULD have done; gate_v8_* is the arm
    # that actually executes.
    gate_legacy_take = Column(Boolean)
    gate_v8_decision = Column(String)    # TRADE | TENTATIVE | NO_TRADE | UNKNOWN
    gate_v8_take     = Column(Boolean)
    gate_v8_reason   = Column(String)
    gate_v8_net_r    = Column(Float)
    market_context   = Column(Text)      # ctx_v1: macro state at judgment
    # counterfactual resolution — the only fields ever updated
    resolved         = Column(Boolean, default=False)
    resolved_at      = Column(String)
    outcome          = Column(String)    # WIN | LOSS | BREAKEVEN
    pnl_pct          = Column(Float)
    mfe_r            = Column(Float)
    mae_r            = Column(Float)
    first_touch      = Column(String)    # STOP | TARGET | AMBIGUOUS
    exit_reason      = Column(String)


class FeatureSnapshot(Base):
    """One feature vector as it stood at one moment — immutable (P4 §52).

    Clock-driven rows are the scientifically important ones: taken on a
    fixed cadence whether or not anything looked interesting, so the corpus
    is free of pick-the-moment selection bias. The vector is stored under
    its schema hash; a model trained later can verify it is reading the
    contract it was trained on, and nothing ever recomputes these values
    from newer code against older markets.
    """
    __tablename__ = "feature_snapshots"
    id               = Column(String, primary_key=True, default=new_id)
    created_at       = Column(String, default=now_iso, index=True)
    symbol           = Column(String, index=True)
    timeframe        = Column(String)
    trigger          = Column(String)     # clock | signal
    signal_id        = Column(String)     # when trigger == signal
    schema_version   = Column(String)
    schema_hash      = Column(String)
    values_json      = Column(Text)       # list[float], clipped+scaled
    mask_json        = Column(Text)       # list[float], 1.0 = observed
    missing_fraction = Column(Float)
    quality          = Column(String)     # ok | degraded  (§43 flag, not a filter)
    bar_time         = Column(String)     # anchor bar the features describe
    anchor_price     = Column(Float)      # close at anchor — forward returns


class FeatureLabel(Base):
    """One horizon's outcome for one snapshot — resolved independently (§57).

    A snapshot schedules several of these at birth (1h/4h/1d). Each becomes
    due on its own clock and resolves on its own evidence: the 1h label can
    be RESOLVED while the 1d label is still pending, and a horizon without
    enough forward bars ABSTAINS with a reason instead of fabricating a
    return from partial coverage.
    """
    __tablename__ = "feature_labels"
    id              = Column(String, primary_key=True, default=new_id)
    snapshot_id     = Column(String, index=True)
    horizon_min     = Column(Integer)
    due_at          = Column(String, index=True)
    status          = Column(String, default="pending")  # pending | resolved | abstained
    resolved_at     = Column(String)
    forward_ret_pct = Column(Float)
    max_up_pct      = Column(Float)
    max_down_pct    = Column(Float)
    abstain_reason  = Column(String)


class ScoreChampion(Base):
    """The champion artifact ledger (§4.3) — append-only, never updated.

    One row per promotion event; the current champion is the latest row.
    The evidence that justified each promotion is frozen INTO the row, so
    "why did we believe this?" is answerable years later even after the
    underlying candidate data ages out. Rewriting or deleting a row here
    would be rewriting the system's own scientific record — nothing in the
    codebase does it, and nothing should.
    """
    __tablename__ = "score_champions"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    promoted_at    = Column(String, default=now_iso)
    variant        = Column(String)   # A | B | C | MS | ...
    schema_version = Column(String)   # lib/score_variants.VARIANT_SCHEMA_VERSION at promotion
    evidence       = Column(Text)     # JSON: the full evaluation that justified this
    note           = Column(String)


class LlmCall(Base):
    """One row per LLM call, so "does thinking mode help?" is a query.

    Nobody currently knows whether chain-of-thought improves trading
    outcomes for any given task — the parameter defaulted to True and
    eleven of fourteen call sites simply inherited it. `signal_id` joins
    this row to trade_outcomes, which turns the question into a measurement:
    win rate and average P&L, grouped by task and thinking flag.
    """
    __tablename__ = "llm_calls"
    id                = Column(String, primary_key=True, default=new_id)
    task              = Column(String)    # lib/llm_router.TASKS
    mode_requested    = Column(String)    # FAST | AUTO | DEEP as the caller asked
    thinking          = Column(Boolean)   # what was actually sent
    reason            = Column(String)    # which trigger fired, verbatim
    model             = Column(String)
    prompt_tokens     = Column(Integer)
    completion_tokens = Column(Integer)
    latency_ms        = Column(Float)
    response_chars    = Column(Integer)
    ok                = Column(Boolean, default=True)
    error             = Column(String)
    signal_id         = Column(String)    # FK → trading_signals.id, joins to outcomes
    symbol            = Column(String)
    created_at        = Column(String, default=now_iso)


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
