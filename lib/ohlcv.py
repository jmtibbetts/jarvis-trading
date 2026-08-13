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
    '1m':  (TimeFrame(1,  TimeFrameUnit.Minute), 240,   1),
    '3m':  (TimeFrame(3,  TimeFrameUnit.Minute), 240,   2),
    '5m':  (TimeFrame(5,  TimeFrameUnit.Minute), 240,   3),
    '15m': (TimeFrame(15, TimeFrameUnit.Minute), 200,   7),
    '30m': (TimeFrame(30, TimeFrameUnit.Minute), 160,  10),
    '1H':  (TimeFrame(1,  TimeFrameUnit.Hour),  72,   5),
    '2H':  (TimeFrame(2,  TimeFrameUnit.Hour),  60,  10),
    '4H':  (TimeFrame(4,  TimeFrameUnit.Hour),  60,  20),
    # 520, not 252: the daily series is also the SOURCE for 2D and 1W, and
    # crypto trades seven days a week, so 252 daily bars is only 36 weekly
    # ones — too thin to compute a weekly trend against. 520 gives ~74 weeks
    # on crypto and ~104 on equities. Nothing reading 1D directly is affected;
    # the 52-week high in market_regime already tails its own 252.
    '1D':  (TimeFrame(1,  TimeFrameUnit.Day),  520, 900),
    # Longer horizons. A 1D chart shows a month; a 1W chart shows the year
    # that decides whether that month means anything. Both use Day bars
    # resampled locally, because Alpaca has no native 2-day interval and
    # weekly bars need a consistent week boundary anyway.
    '2D':  (TimeFrame(1,  TimeFrameUnit.Day),  252, 900),
    '1W':  (TimeFrame(1,  TimeFrameUnit.Day),  260, 2200),
}

# Timeframes with no native exchange interval, built by resampling daily
# bars. Kept explicit so nothing silently requests a bar size a venue does
# not actually publish and gets something subtly different back.
#
# (pandas rule, calendar days the bucket spans). The span is stated rather
# than inferred from index spacing, because the last bucket has no successor
# to measure against and that is exactly the bucket whose completeness
# decides whether an unfinished bar reaches the engine.
#
# Weekly is "W" (= W-SUN), NOT "W-MON". W-MON closes buckets ON Monday, so
# a bar labelled Mon Aug 10 actually spans Tue Aug 4 -> Mon Aug 10: it
# straddles two trading weeks, mixing the end of one with the start of the
# next. "W" buckets Mon->Sun, one whole trading week; the label is then
# moved from the Sunday right edge back to the bucket's Monday.
RESAMPLE_FROM_DAILY = {"2D": ("2D", 2), "1W": ("W", 7)}
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
            # No `limit`. Alpaca applies it from the START of the window, so
            # asking for 252 daily bars over a 450-day lookback returned the
            # OLDEST 252 — every equity series ended 2026-05-21 and had done
            # for months, while looking like a complete, healthy 252-bar
            # history. The window bounds the request; the tail below takes
            # the most recent bars, which is what was wanted all along.
            if crypto:
                req = CryptoBarsRequest(symbol_or_symbols=sym, timeframe=tf, start=start, end=end)
                bars = crypto_client.get_crypto_bars(req)
            else:
                req = StockBarsRequest(symbol_or_symbols=sym, timeframe=tf, start=start, end=end,
                                       adjustment='split', feed=DataFeed.IEX)
                bars = stock_client.get_stock_bars(req)
            df = bars.df
            if df is None or df.empty:
                return None
            if isinstance(df.index, pd.MultiIndex):
                df = df.loc[sym] if sym in df.index.get_level_values(0) else df.reset_index(level=0, drop=True)
            df.index = pd.to_datetime(df.index, utc=True)
            cols = [c for c in ['open','high','low','close','volume'] if c in df.columns]
            df = df[cols].copy()
            df = df[~df.index.duplicated(keep='last')].sort_index().tail(bar_count)
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
        timeframes = ['1m', '3m', '5m', '15m', '30m', '1H', '4H', '1D']
    
    try:
        from lib.ohlcv_cache import fetch_with_cache, init_cache_db
        init_cache_db()
        stock_client, crypto_client = _get_clients()
    except Exception as e:
        logger.error(f"[OHLCV] fetch_multi_timeframe({symbol}) setup error: {e}")
        return {tf: None for tf in timeframes}

    def alpaca_fn(sym, tf):
        return _fetch_alpaca_single(sym, tf, stock_client, crypto_client)

    result = {}
    daily = None
    for tf in timeframes:
        if tf not in TF_CONFIG:
            continue
        try:
            if tf in RESAMPLE_FROM_DAILY:
                # No venue publishes a 2-day bar, and weekly bars need a
                # consistent week boundary. Both are built from daily bars
                # rather than trusting an exchange interval that may differ
                # from what was asked for.
                if daily is None:
                    daily = fetch_with_cache(symbol, "1D", alpaca_fetch_fn=alpaca_fn)
                result[tf] = resample_daily(daily, RESAMPLE_FROM_DAILY[tf])
            else:
                result[tf] = fetch_with_cache(symbol, tf, alpaca_fetch_fn=alpaca_fn)
        except Exception as e:
            # Don't let one bad timeframe discard the rest of the batch.
            logger.error(f"[OHLCV] fetch_multi_timeframe({symbol}/{tf}) error: {e}")
            result[tf] = None
    return result


def resample_daily(daily, spec, now=None):
    """Build a longer bar from daily bars.

    `spec` is (pandas rule, calendar days per bucket) from
    RESAMPLE_FROM_DAILY. A bare rule string is accepted and its span
    inferred, for callers that predate the tuple.

    OHLCV does not aggregate uniformly: open is the FIRST open, close the
    LAST close, high the max, low the min, volume the sum. Averaging any of
    them would produce a chart that never existed.

    The final bucket is dropped while it is still forming. A partial week
    shown as a completed weekly bar reports a high and low the week has not
    finished making — every level derived from it (breakout, channel edge,
    ATR) would be measured against a bar that does not exist yet, and would
    change under the engine's feet on the next tick. Costing up to a week of
    recency on the 1W timeframe is the price of only ever reading closed
    bars, and it is the right trade.
    """
    if daily is None or len(daily) == 0:
        return None
    rule, span_days = spec if isinstance(spec, (tuple, list)) else (spec, None)
    if span_days is None:
        span_days = 7 if str(rule).upper().startswith("W") else 2
    try:
        df = daily.copy()
        df.columns = [c.lower() for c in df.columns]
        out = df.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        if len(out) == 0:
            return None

        # "W" labels each bucket with its Sunday right edge. The bar spans
        # Mon->Sun, so name it by the Monday the week actually opened.
        if str(rule).upper().startswith("W"):
            out.index = out.index - pd.Timedelta(days=span_days - 1)

        # The label is now the bucket's first calendar day for both rules,
        # so its exclusive right edge is start + span.
        end = out.index[-1] + pd.Timedelta(days=span_days)
        cutoff = pd.Timestamp(now) if now is not None else pd.Timestamp.now(
            tz=out.index.tz) if out.index.tz is not None else pd.Timestamp.now()
        if cutoff.tzinfo is not None and out.index.tz is None:
            cutoff = cutoff.tz_localize(None)
        elif cutoff.tzinfo is None and out.index.tz is not None:
            cutoff = cutoff.tz_localize(out.index.tz)
        if cutoff < end:
            out = out.iloc[:-1]

        out.attrs["source"] = f"resampled_from_1D({rule})"
        return out if len(out) else None
    except Exception as e:
        logger.debug(f"[OHLCV] resample to {rule} failed: {e}")
        return None


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
    except Exception as e:
        logger.error(f"[OHLCV] fetch_batch setup error: {e}")
        return {s: {tf: None for tf in timeframes} for s in symbols}

    def alpaca_fn(sym, tf):
        return _fetch_alpaca_single(sym, tf, stock_client, crypto_client)

    result = {}
    for sym in symbols:
        sym_bars = {}
        for tf in timeframes:
            if tf not in TF_CONFIG:
                sym_bars[tf] = None
                continue
            try:
                sym_bars[tf] = fetch_with_cache(sym, tf, alpaca_fetch_fn=alpaca_fn)
            except Exception as e:
                # Don't let one bad symbol/timeframe discard the rest of the batch.
                logger.error(f"[OHLCV] fetch_batch({sym}/{tf}) error: {e}")
                sym_bars[tf] = None
            time.sleep(RATE_LIMIT_DELAY)
        result[sym] = sym_bars
    return result
