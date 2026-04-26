"""
Jarvis Trading AI — SQLAlchemy + SQLite database layer (async-compatible).
Uses synchronous SQLAlchemy with a thread pool for simplicity on Windows.
"""
import os, uuid, json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (
    create_engine, Column, String, Float, Boolean, Text, DateTime,
    event, text
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from contextlib import contextmanager

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "jarvis.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False
)

# Enable WAL mode for concurrent reads
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

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def new_id():
    return str(uuid.uuid4())

# ─── Models ───────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass

class TradingSignal(Base):
    __tablename__ = "trading_signals"
    id              = Column(String, primary_key=True, default=new_id)
    asset_symbol    = Column(String, nullable=False)
    asset_name      = Column(String)
    asset_class     = Column(String)
    direction       = Column(String)
    confidence      = Column(Float)
    timeframe       = Column(String)
    reasoning       = Column(Text)
    trigger_event   = Column(Text)
    trigger_event_id= Column(String)
    entry_price     = Column(Float)
    target_price    = Column(Float)
    stop_loss       = Column(Float)
    status          = Column(String, default="Active")
    generated_at    = Column(String)
    momentum        = Column(String)
    key_risks       = Column(Text)
    signal_source   = Column(String, default="watchlist")
    created_date    = Column(String, default=now_iso)
    updated_date    = Column(String, default=now_iso)

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
    affected_assets = Column(Text)  # JSON array stored as string
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
    symbol       = Column(String, primary_key=True)
    qty          = Column(Float)
    avg_entry    = Column(Float)
    market_value = Column(Float)
    unrealized_pl= Column(Float)
    unrealized_plpc = Column(Float)
    side         = Column(String)
    asset_class  = Column(String)
    updated_at   = Column(String, default=now_iso)

def init_db():
    Base.metadata.create_all(bind=engine)
    print("[DB] Schema initialized")

