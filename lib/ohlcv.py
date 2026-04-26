"""
Multi-timeframe OHLCV fetcher using alpaca-py SDK.
Handles rate limiting, retries, crypto vs equity routing.
"""
import asyncio, logging, time
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from lib.alpaca_client import get_alpaca_creds, is_crypto, normalize_symbol

logger = logging.getLogger(__name__)

# Timeframe config: label → (alpaca TimeFrame, bar_count, extra_days_lookback)
TF_CONFIG = {
    '1H':  (TimeFrame(1,  TimeFrameUnit.Hour),  72,  5),
    '2H':  (TimeFrame(2,  TimeFrameUnit.Hour),  60, 10),
    '4H':  (TimeFrame(4,  TimeFrameUnit.Hour),  60, 20),
    '1D':  (TimeFrame(1,  TimeFrameUnit.Day),  200, 400),
}

RATE_LIMIT_DELAY = 0.8   # seconds between requests
MAX_RETRIES = 3

def _get_clients():
    key, secret, _ = get_alpaca_creds()
    if not key:
        raise ValueError("No Alpaca credentials")
    stock  = StockHistoricalDataClient(api_key=key, secret_key=secret)
    crypto = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    return stock, crypto

def _fetch_bars_single(symbol: str, tf_label: str, stock_client, crypto_client) -> Optional[pd.DataFrame]:
    """Fetch bars for one symbol/timeframe with retry logic."""
    sym, crypto = normalize_symbol(symbol)
    tf, bar_count, lookback_days = TF_CONFIG[tf_label]
    
    # Use extended lookback to handle weekends/holidays
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    
    for attempt in range(MAX_RETRIES):
        try:
            if crypto:
                req = CryptoBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=tf,
                    start=start,
                    end=end,
                    limit=bar_count
                )
                bars = crypto_client.get_crypto_bars(req)
            else:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=tf,
                    start=start,
                    end=end,
                    limit=bar_count,
                    adjustment='split'
                )
                bars = stock_client.get_stock_bars(req)
            
            # Convert to DataFrame
            df = bars.df
            if df is None or df.empty:
                return None
            
            # Handle multi-index (symbol, timestamp)
            if isinstance(df.index, pd.MultiIndex):
                if sym in df.index.get_level_values(0):
                    df = df.loc[sym]
                else:
                    df = df.reset_index(level=0, drop=True)
            
            df.index = pd.to_datetime(df.index, utc=True)
            df = df.rename(columns={'open':'open','high':'high','low':'low',
                                     'close':'close','volume':'volume',
                                     'trade_count':'trade_count','vwap':'vwap_raw'})
            df = df[['open','high','low','close','volume']].copy()
            df = df.dropna(subset=['close']).tail(bar_count)
            return df
            
        except Exception as e:
            err = str(e)
            if '429' in err or 'rate' in err.lower():
                wait = RATE_LIMIT_DELAY * (2 ** attempt) * 3
                logger.warning(f"[OHLCV] 429 on {sym}/{tf_label} — waiting {wait:.1f}s")
                time.sleep(wait)
            elif attempt < MAX_RETRIES - 1:
                time.sleep(RATE_LIMIT_DELAY * (attempt + 1))
                logger.debug(f"[OHLCV] Retry {attempt+1} for {sym}/{tf_label}: {err[:80]}")
            else:
                logger.warning(f"[OHLCV] Failed {sym}/{tf_label}: {err[:80]}")
                return None
    return None


def fetch_multi_timeframe(symbol: str, timeframes: list = None) -> dict:
    """
    Fetch multiple timeframes for a single symbol.
    Returns { '1H': DataFrame, '4H': DataFrame, '1D': DataFrame, ... }
    """
    if timeframes is None:
        timeframes = ['1H', '4H', '1D']
    
    stock_client, crypto_client = _get_clients()
    result = {}
    for tf in timeframes:
        result[tf] = _fetch_bars_single(symbol, tf, stock_client, crypto_client)
        time.sleep(RATE_LIMIT_DELAY)
    return result


def fetch_batch(symbols: list, timeframes: list = None, max_workers: int = 2) -> dict:
    """
    Fetch multi-timeframe bars for a batch of symbols.
    Returns { symbol: { '1H': df, '4H': df, '1D': df } }
    Uses a simple sequential loop with rate limiting to avoid 429s.
    """
    if timeframes is None:
        timeframes = ['1H', '4H', '1D']
    
    stock_client, crypto_client = _get_clients()
    results = {}
    
    for i, symbol in enumerate(symbols):
        sym_data = {}
        for tf in timeframes:
            sym_data[tf] = _fetch_bars_single(symbol, tf, stock_client, crypto_client)
            time.sleep(RATE_LIMIT_DELAY)
        results[symbol] = sym_data
        if (i + 1) % 5 == 0:
            logger.info(f"[OHLCV] Progress: {i+1}/{len(symbols)} symbols fetched")
    
    return results
