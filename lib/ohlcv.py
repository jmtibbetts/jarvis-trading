"""
Multi-timeframe OHLCV fetcher using alpaca-py SDK.
v6.0: Added fetch_multi_timeframe() for single-symbol fetching.
      Integrated ohlcv_cache for yfinance fallback + SQLite persistence.
      Uses IEX feed for equities (free/paper tier).
"""
import logging, time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from lib.alpaca_client import get_alpaca_creds, is_crypto, normalize_symbol

logger = logging.getLogger(__name__)

TF_CONFIG = {
    '1H':  (TimeFrame(1,  TimeFrameUnit.Hour),  72,   5),
    '2H':  (TimeFrame(2,  TimeFrameUnit.Hour),  60,  10),
    '4H':  (TimeFrame(4,  TimeFrameUnit.Hour),  60,  20),
    '1D':  (TimeFrame(1,  TimeFrameUnit.Day),  252, 450),
}
RATE_LIMIT_DELAY = 0.8
MAX_RETRIES = 3


def _get_clients():
    key, secret, _ = get_alpaca_creds()
    if not key:
        raise ValueError("No Alpaca credentials")
    return (StockHistoricalDataClient(api_key=key, secret_key=secret),
            CryptoHistoricalDataClient(api_key=key, secret_key=secret))


def _fetch_alpaca_single(symbol: str, tf_label: str, stock_client, crypto_client) -> Optional[pd.DataFrame]:
    sym, crypto = normalize_symbol(symbol)
    tf, bar_count, lookback_days = TF_CONFIG[tf_label]
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    for attempt in range(MAX_RETRIES):
        try:
            if crypto:
                req = CryptoBarsRequest(symbol_or_symbols=sym, timeframe=tf, start=start, end=end, limit=bar_count)
                bars = crypto_client.get_crypto_bars(req)
            else:
                req = StockBarsRequest(symbol_or_symbols=sym, timeframe=tf, start=start, end=end,
                                       limit=bar_count, adjustment='split', feed=DataFeed.IEX)
                bars = stock_client.get_stock_bars(req)
            df = bars.df
            if df is None or df.empty:
                return None
            if isinstance(df.index, pd.MultiIndex):
                df = df.loc[sym] if sym in df.index.get_level_values(0) else df.reset_index(level=0, drop=True)
            df.index = pd.to_datetime(df.index, utc=True)
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df = df[cols].copy()
            df = df[~df.index.duplicated(keep='last')].sort_index()
            return df if len(df) >= 5 else None
        except Exception as e:
            err = str(e)
            if '429' in err or 'rate' in err.lower():
                time.sleep(RATE_LIMIT_DELAY * (2 ** attempt))
            elif attempt < MAX_RETRIES - 1:
                time.sleep(RATE_LIMIT_DELAY)
            else:
                logger.debug(f"[OHLCV] Alpaca failed {sym}/{tf_label}: {err[:80]}")
                return None
    return None


def fetch_multi_timeframe(symbol: str, timeframes: list = None) -> Dict[str, Optional[pd.DataFrame]]:
    """
    Fetch multiple timeframes for ONE symbol.
    Uses cache+yfinance fallback via ohlcv_cache module.
    Returns { '1H': df, '4H': df, '1D': df }
    """
    if timeframes is None:
        timeframes = ['1H', '4H', '1D']
    
    try:
        from lib.ohlcv_cache import fetch_with_cache, init_cache_db
        init_cache_db()
        stock_client, crypto_client = _get_clients()
        
        def alpaca_fn(sym, tf):
            return _fetch_alpaca_single(sym, tf, stock_client, crypto_client)
        
        return {tf: fetch_with_cache(symbol, tf, alpaca_fetch_fn=alpaca_fn)
                for tf in timeframes if tf in TF_CONFIG}
    except Exception as e:
        logger.error(f"[OHLCV] fetch_multi_timeframe({symbol}) error: {e}")
        return {tf: None for tf in timeframes}


def fetch_batch(symbols: list, timeframes: list = None) -> dict:
    """
    Fetch multiple timeframes for multiple symbols.
    Returns { symbol: { tf: DataFrame } }
    """
    if timeframes is None:
        timeframes = ['1H', '4H', '1D']
    
    try:
        from lib.ohlcv_cache import fetch_with_cache, init_cache_db
        init_cache_db()
        stock_client, crypto_client = _get_clients()
        
        def alpaca_fn(sym, tf):
            return _fetch_alpaca_single(sym, tf, stock_client, crypto_client)
        
        result = {}
        for sym in symbols:
            sym_bars = {}
            for tf in timeframes:
                if tf not in TF_CONFIG:
                    sym_bars[tf] = None
                    continue
                df = fetch_with_cache(sym, tf, alpaca_fetch_fn=alpaca_fn)
                sym_bars[tf] = df
                time.sleep(RATE_LIMIT_DELAY)
            result[sym] = sym_bars
        return result
    except Exception as e:
        logger.error(f"[OHLCV] fetch_batch error: {e}")
        return {s: {tf: None for tf in timeframes} for s in symbols}
