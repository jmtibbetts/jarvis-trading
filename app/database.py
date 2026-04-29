"""
Jarvis Trading AI — SQLAlchemy + SQLite database layer.
v6.1: Added earnings_risk column to TradingSignal. Better migration coverage.
"""
import os, uuid, json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (create_engine, Column, String, Float, Boolean, Text, event, text)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from contextlib import contextmanager

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "jarvis.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, echo=False)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

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
    created_date= Column(String, default=now_iso)
    updated_date= Column(String, default=now_iso)

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

def init_db():
    Base.metadata.create_all(bind=engine)
    # Run migrations for any missing columns
    _migrate_columns()
    # Seed paper portfolio if missing
    _seed_paper_portfolio()
    print("[DB] Schema initialized")

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
        ],
        "paper_positions": [
            ("asset_class",     "TEXT"),
            ("direction",       "TEXT"),
            ("side",            "TEXT"),
            ("leverage",        "REAL DEFAULT 1.0"),
            ("notional",        "REAL"),
            ("margin_used",     "REAL"),
            ("unrealized_pnl",  "REAL DEFAULT 0.0"),
            ("unrealized_pct",  "REAL DEFAULT 0.0"),
            ("signal_id",       "TEXT"),
        ],
        "paper_trades": [
            ("asset_class",     "TEXT"),
            ("direction",       "TEXT"),
            ("side",            "TEXT"),
            ("leverage",        "REAL DEFAULT 1.0"),
            ("notional",        "REAL"),
            ("signal_id",       "TEXT"),
            ("position_id",     "TEXT"),
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

    # ── Learning engine tables ───────────────────────────────────────────────
    try:
        with engine.begin() as conn:
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
            print("[DB] Learning engine tables ready")
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
    opened_at     = Column(String, default=now_iso)
    updated_at    = Column(String, default=now_iso)


class PaperTrade(Base):
    """Completed paper trades — the historical ledger."""
    __tablename__ = "paper_trades"
    id            = Column(String, primary_key=True, default=new_id)
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
