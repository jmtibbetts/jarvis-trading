"""
lib/ohlcv_cache.py — SQLite OHLCV bar cache with yfinance fallback.

Architecture:
  1. Try Alpaca IEX (primary — real-time, requires creds)
  2. Try SQLite cache (recent bars we've already fetched)
  3. Try yfinance (fallback — free, works nights/weekends/holidays)
  4. Merge all sources: cache + new bars → deduplicate → store back

Cache rules:
  - Store bars for up to 2 years (1D), 90 days (1H/4H)
  - Never delete bars — merge with UPSERT
  - Mark bars with 'source' (alpaca/yfinance/cache)
  - Fill missing trading days from yfinance on first access
  - Background backfill job can be triggered manually or on startup
"""
import logging, time, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict
import pandas as pd
from sqlalchemy import create_engine, Column, String, Float, Text, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── Cache DB (separate from main DB — can grow large) ─────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DB = DATA_DIR / "ohlcv_cache.db"

cache_engine = create_engine(
    f"sqlite:///{CACHE_DB}",
    connect_args={"check_same_thread": False},
    echo=False
)

@event.listens_for(cache_engine, "connect")
def set_pragma(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

CacheSession = sessionmaker(bind=cache_engine, autoflush=False, autocommit=False)

@contextmanager
def get_cache_db():
    db = CacheSession()
    try:
        yield db
        db.commit()
    except:
        db.rollback()
        raise
    finally:
        db.close()

class CacheBase(DeclarativeBase):
    pass

class OHLCVBar(CacheBase):
    """One OHLCV bar for one symbol/timeframe."""
    __tablename__ = "ohlcv_bars"
    # Composite primary key: symbol + timeframe + timestamp
    symbol     = Column(String, primary_key=True)
    timeframe  = Column(String, primary_key=True)
    ts         = Column(String, primary_key=True)  # ISO timestamp UTC
    open       = Column(Float)
    high       = Column(Float)
    low        = Column(Float)
    close      = Column(Float)
    volume     = Column(Float)
    source     = Column(String, default='unknown')

class BackfillStatus(CacheBase):
    """Track backfill completion per symbol/timeframe."""
    __tablename__ = "backfill_status"
    symbol     = Column(String, primary_key=True)
    timeframe  = Column(String, primary_key=True)
    earliest_ts= Column(String)
    latest_ts  = Column(String)
    bar_count  = Column(Float)
    last_updated = Column(String)

def init_cache_db():
    """Create tables if they don't exist. Safe to call multiple times (create_all is idempotent)."""
    CacheBase.metadata.create_all(bind=cache_engine)
    logger.debug(f"[OHLCVCache] Ready at {CACHE_DB}")

# ── Cache TTL per timeframe ────────────────────────────────────────────────────
CACHE_KEEP_DAYS = {
    '1H':  90,
    '2H':  90,
    '4H':  180,
    '1D':  730,   # 2 years of daily bars
}

# ── yfinance symbol mapping ───────────────────────────────────────────────────
def to_yf_symbol(symbol: str) -> str:
    """Convert internal symbol to yfinance format."""
    s = symbol.upper().strip()
    if '/' in s:
        base = s.split('/')[0]
        return f"{base}-USD"
    return s

# ── yfinance timeframe mapping ─────────────────────────────────────────────────
YF_INTERVALS = {
    '1H': '1h',
    '2H': '2h',
    '4H': '1h',   # yfinance has no 4H — we'll resample from 1H
    '1D': '1d',
}

def _yf_fetch(symbol: str, tf: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """Fetch from yfinance and normalize to standard OHLCV format."""
    try:
        import yfinance as yf
        yf_sym = to_yf_symbol(symbol)
        interval = YF_INTERVALS.get(tf, '1d')
        
        df = yf.download(
            yf_sym,
            start=start.strftime('%Y-%m-%d'),
            end=(end + timedelta(days=1)).strftime('%Y-%m-%d'),
            interval=interval,
            auto_adjust=True,
            progress=False,
            show_errors=False
        )
        
        if df is None or df.empty:
            return None
        
        # Normalize columns
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[['open','high','low','close','volume']].copy()
        df = df.dropna(subset=['close'])
        
        # Resample 1H → 4H if needed
        if tf == '4H':
            df = df.resample('4h').agg({
                'open': 'first', 'high': 'max', 'low': 'min',
                'close': 'last', 'volume': 'sum'
            }).dropna(subset=['close'])
        
        return df if len(df) >= 3 else None
    except Exception as e:
        logger.debug(f"[yfinance] {symbol}/{tf} error: {e}")
        return None


def _cache_to_df(rows: list) -> Optional[pd.DataFrame]:
    """Convert OHLCVBar rows to DataFrame."""
    if not rows:
        return None
    data = [{'ts': r.ts, 'open': r.open, 'high': r.high,
              'low': r.low, 'close': r.close, 'volume': r.volume} for r in rows]
    df = pd.DataFrame(data)
    df.index = pd.to_datetime(df['ts'], utc=True)
    df = df[['open','high','low','close','volume']].copy()
    df = df.sort_index()
    return df


def _store_bars(symbol: str, tf: str, df: pd.DataFrame, source: str = 'alpaca'):
    """Upsert bars into cache DB."""
    if df is None or df.empty:
        return 0
    stored = 0
    with get_cache_db() as db:
        for ts, row in df.iterrows():
            ts_str = ts.isoformat()
            # Check if exists
            existing = db.query(OHLCVBar).filter_by(
                symbol=symbol, timeframe=tf, ts=ts_str
            ).first()
            if existing:
                # Update with better source data
                if source in ('alpaca', 'yfinance') or existing.source == 'cache':
                    existing.open   = float(row.get('open', 0) or 0)
                    existing.high   = float(row.get('high', 0) or 0)
                    existing.low    = float(row.get('low', 0) or 0)
                    existing.close  = float(row.get('close', 0) or 0)
                    existing.volume = float(row.get('volume', 0) or 0)
                    existing.source = source
            else:
                db.add(OHLCVBar(
                    symbol=symbol, timeframe=tf, ts=ts_str,
                    open=float(row.get('open', 0) or 0),
                    high=float(row.get('high', 0) or 0),
                    low=float(row.get('low', 0) or 0),
                    close=float(row.get('close', 0) or 0),
                    volume=float(row.get('volume', 0) or 0),
                    source=source
                ))
                stored += 1
        
        # Update backfill status — always set latest_ts + recount total rows
        all_ts = [ts.isoformat() for ts in df.index]
        now_iso = datetime.now(timezone.utc).isoformat()
        # Real bar count: query the actual row count (not just inserts)
        total_count = db.query(OHLCVBar).filter_by(symbol=symbol, timeframe=tf).count()
        existing_status = db.query(BackfillStatus).filter_by(symbol=symbol, timeframe=tf).first()
        if existing_status:
            existing_status.latest_ts    = max(existing_status.latest_ts or '', max(all_ts))
            existing_status.earliest_ts  = min(existing_status.earliest_ts or min(all_ts), min(all_ts))
            existing_status.bar_count    = total_count   # authoritative count, not just new inserts
            existing_status.last_updated = now_iso
        else:
            db.add(BackfillStatus(
                symbol=symbol, timeframe=tf,
                earliest_ts=min(all_ts), latest_ts=max(all_ts),
                bar_count=stored, last_updated=now_iso
            ))
    return stored


def _get_cached_bars(symbol: str, tf: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """Retrieve bars from cache between start and end."""
    with get_cache_db() as db:
        rows = db.query(OHLCVBar).filter(
            OHLCVBar.symbol == symbol,
            OHLCVBar.timeframe == tf,
            OHLCVBar.ts >= start.isoformat(),
            OHLCVBar.ts <= end.isoformat()
        ).order_by(OHLCVBar.ts.asc()).all()
        return _cache_to_df(rows) if rows else None


def _get_bar_count(symbol: str, tf: str) -> int:
    """Count cached bars for a symbol/tf."""
    with get_cache_db() as db:
        status = db.query(BackfillStatus).filter_by(symbol=symbol, timeframe=tf).first()
        return int(status.bar_count or 0) if status else 0


# ── Public API ────────────────────────────────────────────────────────────────

TF_CONFIG = {
    '1H':  {'bar_count': 72,  'lookback_days': 5,   'hist_days': 90},
    '2H':  {'bar_count': 60,  'lookback_days': 10,  'hist_days': 90},
    '4H':  {'bar_count': 60,  'lookback_days': 20,  'hist_days': 180},
    '1D':  {'bar_count': 252, 'lookback_days': 400, 'hist_days': 730},
}

def fetch_with_cache(symbol: str, tf: str,
                     alpaca_fetch_fn=None) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV bars for symbol/tf using:
    1. Alpaca (primary) → store to cache
    2. Cache (recent bars) → supplement missing
    3. yfinance (fallback) → store to cache

    Returns merged DataFrame ready for TA.
    """
    cfg = TF_CONFIG.get(tf, TF_CONFIG['1D'])
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=cfg['lookback_days'])
    bar_count = cfg['bar_count']

    # ── 1. Try Alpaca ──────────────────────────────────────────────────────────
    alpaca_df = None
    if alpaca_fetch_fn:
        try:
            alpaca_df = alpaca_fetch_fn(symbol, tf)
            if alpaca_df is not None and not alpaca_df.empty:
                _store_bars(symbol, tf, alpaca_df, source='alpaca')
                logger.info(f"[Cache] Stored {symbol}/{tf} → {len(alpaca_df)} bars (alpaca)")
        except Exception as e:
            logger.debug(f"[Cache] Alpaca fetch failed for {symbol}/{tf}: {e}")

    # ── 2. Load from cache ─────────────────────────────────────────────────────
    cached_df = _get_cached_bars(symbol, tf, start, end)

    # ── 3. yfinance fallback if cache is thin ─────────────────────────────────
    use_yf = (
        alpaca_df is None or alpaca_df.empty
    ) and (
        cached_df is None or len(cached_df) < bar_count // 3
    )
    
    yf_df = None
    if use_yf:
        logger.info(f"[Cache] {symbol}/{tf} → falling back to yfinance")
        yf_df = _yf_fetch(symbol, tf, start, end)
        if yf_df is not None and not yf_df.empty:
            _store_bars(symbol, tf, yf_df, source='yfinance')
            logger.info(f"[Cache] {symbol}/{tf} → {len(yf_df)} bars from yfinance")

    # ── 4. Merge all sources ──────────────────────────────────────────────────
    frames = [df for df in [cached_df, alpaca_df, yf_df] if df is not None and not df.empty]
    if not frames:
        return None
    
    if len(frames) == 1:
        merged = frames[0]
    else:
        merged = pd.concat(frames)
        merged = merged[~merged.index.duplicated(keep='last')]
        merged = merged.sort_index()
    
    merged = merged.dropna(subset=['close'])
    return merged.tail(bar_count) if len(merged) > bar_count else merged


def backfill_symbol(symbol: str, tf: str = '1D',
                    days: int = None, force: bool = False) -> int:
    """
    Backfill historical bars for a symbol using yfinance.
    Runs once per symbol unless force=True.
    Returns number of new bars stored.
    """
    cfg = TF_CONFIG.get(tf, TF_CONFIG['1D'])
    days = days or cfg['hist_days']
    
    # Check if already backfilled
    if not force:
        count = _get_bar_count(symbol, tf)
        if tf == '1D' and count >= 200:
            logger.debug(f"[Backfill] {symbol}/{tf} already has {count} bars — skipping")
            return 0
        if tf in ('1H', '4H') and count >= 50:
            logger.debug(f"[Backfill] {symbol}/{tf} already has {count} bars — skipping")
            return 0
    
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    
    logger.info(f"[Backfill] {symbol}/{tf} — fetching {days} days from yfinance...")
    df = _yf_fetch(symbol, tf, start, end)
    if df is None or df.empty:
        logger.warning(f"[Backfill] {symbol}/{tf} — no data from yfinance")
        return 0
    
    stored = _store_bars(symbol, tf, df, source='yfinance')
    logger.info(f"[Backfill] {symbol}/{tf} — stored {stored} new bars ({len(df)} total)")
    return stored


def get_cache_stats() -> dict:
    """Return cache statistics for the API."""
    with get_cache_db() as db:
        statuses = db.query(BackfillStatus).all()
        # Use actual row count as the authoritative total (bar_count may be stale)
        actual_total = db.query(OHLCVBar).count()
        symbols_cached = len(set(s.symbol for s in statuses))
        by_tf = {}
        # Also track freshness: latest bar seen per TF
        latest_by_tf = {}
        last_updated_by_tf = {}
        for s in statuses:
            if s.timeframe not in by_tf:
                by_tf[s.timeframe] = {'symbols': 0, 'bars': 0}
            by_tf[s.timeframe]['symbols'] += 1
            by_tf[s.timeframe]['bars'] += int(s.bar_count or 0)
            # Track newest bar timestamp per TF
            if s.latest_ts:
                prev = latest_by_tf.get(s.timeframe, '')
                if s.latest_ts > prev:
                    latest_by_tf[s.timeframe] = s.latest_ts
            # Track last cache-write timestamp per TF
            if s.last_updated:
                prev = last_updated_by_tf.get(s.timeframe, '')
                if s.last_updated > prev:
                    last_updated_by_tf[s.timeframe] = s.last_updated
        # Enrich by_tf with freshness info
        for tf in by_tf:
            by_tf[tf]['latest_bar_ts'] = latest_by_tf.get(tf, '')
            by_tf[tf]['last_updated']  = last_updated_by_tf.get(tf, '')
        # Overall freshness
        all_latest = [v for v in latest_by_tf.values() if v]
        overall_latest_bar = max(all_latest) if all_latest else ''
        all_updated = [v for v in last_updated_by_tf.values() if v]
        overall_last_updated = max(all_updated) if all_updated else ''
    db_size_mb = CACHE_DB.stat().st_size / 1024 / 1024 if CACHE_DB.exists() else 0
    return {
        'total_bars': actual_total,
        'symbols_cached': symbols_cached,
        'by_timeframe': by_tf,
        'latest_bar_ts': overall_latest_bar,
        'last_updated': overall_last_updated,
        'db_size_mb': round(db_size_mb, 2),
        'db_path': str(CACHE_DB),
    }


def evict_old_bars():
    """Delete bars older than CACHE_KEEP_DAYS cutoff per timeframe."""
    evicted = 0
    with get_cache_db() as db:
        for tf, keep_days in CACHE_KEEP_DAYS.items():
            cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
            n = db.query(OHLCVBar).filter(
                OHLCVBar.timeframe == tf,
                OHLCVBar.ts < cutoff
            ).delete()
            evicted += n
    if evicted:
        logger.info(f"[Cache] Evicted {evicted} stale bars")
    return evicted
