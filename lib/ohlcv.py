"""
Multi-timeframe OHLCV fetcher using alpaca-py SDK.
Handles rate limiting, retries, crypto vs equity routing.
Uses IEX feed for equities (works on free/paper tier).
"""
import logging, time
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from lib.alpaca_client import get_alpaca_creds, is_crypto, normalize_symbol

logger = logging.getLogger(__name__)

# Timeframe config: label → (alpaca TimeFrame, bar_count, lookback_days)
TF_CONFIG = {
    '1H':  (TimeFrame(1,  TimeFrameUnit.Hour),  72,   5),
    '2H':  (TimeFrame(2,  TimeFrameUnit.Hour),  60,  10),
    '4H':  (TimeFrame(4,  TimeFrameUnit.Hour),  60,  20),
    '1D':  (TimeFrame(1,  TimeFrameUnit.Day),  200, 400),
}

RATE_LIMIT_DELAY = 0.8
MAX_RETRIES = 3


def _get_clients():
    key, secret, _ = get_alpaca_creds()
    if not key:
        raise ValueError("No Alpaca credentials")
    stock  = StockHistoricalDataClient(api_key=key, secret_key=secret)
    crypto = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    return stock, crypto


def _fetch_bars_single(symbol: str, tf_label: str, stock_client, crypto_client) -> Optional[pd.DataFrame]:
    sym, crypto = normalize_symbol(symbol)
    tf, bar_count, lookback_days = TF_CONFIG[tf_label]

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
                # IEX feed = free tier, no SIP subscription required
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=tf,
                    start=start,
                    end=end,
                    limit=bar_count,
                    adjustment='split',
                    feed=DataFeed.IEX
                )
                bars = stock_client.get_stock_bars(req)

            df = bars.df
            if df is None or df.empty:
                return None

            if isinstance(df.index, pd.MultiIndex):
                if sym in df.index.get_level_values(0):
                    df = df.loc[sym]
                else:
                    df = df.reset_index(level=0, drop=True)

            df.index = pd.to_datetime(df.index, utc=True)
            df = df.rename(columns={'open':'open','high':'high','low':'low',
                                     'close':'close','volume':'volume'})
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
                logger.warning(f"[OHLCV] Failed {sym}/{tf_label}: {err[:120]}")
                return None
    return None


def fetch_multi_timeframe(symbol: str, timeframes: list = None) -> dict:
    """Fetch multiple timeframes for one symbol. Returns {tf_label: DataFrame}."""
    if timeframes is None:
        timeframes = list(TF_CONFIG.keys())

    results = {}
    try:
        stock_client, crypto_client = _get_clients()
    except Exception as e:
        logger.error(f"[OHLCV] Client init failed: {e}")
        return results

    for tf in timeframes:
        if tf not in TF_CONFIG:
            continue
        df = _fetch_bars_single(symbol, tf, stock_client, crypto_client)
        if df is not None and not df.empty:
            results[tf] = df
        time.sleep(RATE_LIMIT_DELAY)

    return results


def fetch_batch(symbols: list, timeframes: list = None, max_workers: int = 2) -> dict:
    """
    Fetch multiple symbols sequentially with rate limiting.
    Returns {symbol: {tf: DataFrame}}.
    """
    if timeframes is None:
        timeframes = list(TF_CONFIG.keys())

    all_results = {}
    try:
        stock_client, crypto_client = _get_clients()
    except Exception as e:
        logger.error(f"[OHLCV] Client init failed: {e}")
        return all_results

    for symbol in symbols:
        sym_results = {}
        for tf in timeframes:
            if tf not in TF_CONFIG:
                continue
            df = _fetch_bars_single(symbol, tf, stock_client, crypto_client)
            if df is not None and not df.empty:
                sym_results[tf] = df
            time.sleep(RATE_LIMIT_DELAY)
        if sym_results:
            all_results[symbol] = sym_results

    return all_results
