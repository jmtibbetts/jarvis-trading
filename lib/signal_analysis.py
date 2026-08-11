"""On-demand evidence package for a trading signal."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from lib.ta_engine import TIMEFRAME_LADDER, analyze_symbol

logger = logging.getLogger(__name__)


def _is_crypto(signal: dict) -> bool:
    symbol = (signal.get("asset_symbol") or "").upper()
    asset_class = (signal.get("asset_class") or "").lower()
    return asset_class == "crypto" or "/" in symbol or symbol.endswith("-USD")


def _is_futures_or_forex(signal: dict) -> bool:
    symbol = (signal.get("asset_symbol") or "").upper()
    asset_class = (signal.get("asset_class") or "").lower()
    return asset_class in ("futures", "forex") or symbol.endswith(("=F", "=X")) or symbol.startswith("^")


def _fallback_bars(signal: dict, timeframe: str) -> Optional[pd.DataFrame]:
    symbol = signal.get("asset_symbol") or ""
    if _is_crypto(signal):
        from lib.crypto_market_data import fetch_crypto_ohlcv
        return fetch_crypto_ohlcv(symbol, timeframe, limit=240)
    if _is_futures_or_forex(signal):
        from lib.futures_data import fetch_futures_ohlcv
        return fetch_futures_ohlcv(symbol, timeframe)

    from lib.ohlcv_cache import TF_CONFIG, _yf_fetch
    cfg = TF_CONFIG[timeframe]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=cfg["lookback_days"])
    return _yf_fetch(symbol, timeframe, start, end)


def _fetch_all_bars(signal: dict) -> dict[str, Optional[pd.DataFrame]]:
    symbol = signal.get("asset_symbol") or ""
    bars: dict[str, Optional[pd.DataFrame]] = {}

    if not _is_futures_or_forex(signal):
        try:
            from lib.ohlcv import fetch_multi_timeframe
            bars.update(fetch_multi_timeframe(symbol, TIMEFRAME_LADDER))
        except Exception as exc:
            logger.debug("Signal analysis primary fetch failed for %s: %s", symbol, exc)

    for timeframe in TIMEFRAME_LADDER:
        frame = bars.get(timeframe)
        if frame is not None and not frame.empty:
            continue
        try:
            bars[timeframe] = _fallback_bars(signal, timeframe)
        except Exception as exc:
            logger.debug("Signal analysis fallback failed for %s/%s: %s", symbol, timeframe, exc)
            bars[timeframe] = None
    return bars


def _serialize_candles(frame: Optional[pd.DataFrame], limit: int = 140) -> list[dict]:
    if frame is None or frame.empty:
        return []
    candles = []
    clean = frame.tail(limit).copy()
    clean.columns = [str(c).lower() for c in clean.columns]
    for ts, row in clean.iterrows():
        candles.append({
            "time": pd.Timestamp(ts).isoformat(),
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "volume": float(row.get("volume", 0) or 0),
        })
    return candles


def _confluence(ta: dict, signal: dict) -> dict:
    valid = [(tf, ta.get(tf) or {}) for tf in TIMEFRAME_LADDER if not (ta.get(tf) or {}).get("error")]
    bullish = [tf for tf, data in valid if data.get("bias") == "bullish"]
    bearish = [tf for tf, data in valid if data.get("bias") == "bearish"]
    neutral = [tf for tf, data in valid if data.get("bias") not in ("bullish", "bearish")]
    direction = (signal.get("direction") or "Long").lower()
    expected = "bearish" if "short" in direction else "bullish"
    aligned = bearish if expected == "bearish" else bullish
    score = round(len(aligned) / len(valid) * 100) if valid else 0

    risk_flags = []
    short_ta = [data for tf, data in valid if tf.endswith("m")]
    if any((data.get("volume") or {}).get("dry") for data in short_ta):
        risk_flags.append("Dry volume on at least one scalp timeframe")
    if expected == "bullish" and any((data.get("rsi") or 0) >= 70 for data in short_ta):
        risk_flags.append("A short timeframe is overbought")
    if expected == "bearish" and any((data.get("rsi") or 100) <= 30 for data in short_ta):
        risk_flags.append("A short timeframe is oversold")
    higher = [ta.get(tf) or {} for tf in ("1H", "4H", "1D")]
    opposing = "bullish" if expected == "bearish" else "bearish"
    if sum(data.get("bias") == opposing for data in higher) >= 2:
        risk_flags.append("The higher-timeframe trend opposes this trade")

    label = "strong" if score >= 70 else "mixed" if score >= 40 else "weak"
    return {
        "expected_bias": expected,
        "score": score,
        "label": label,
        "bullish_timeframes": bullish,
        "bearish_timeframes": bearish,
        "neutral_timeframes": neutral,
        "risk_flags": risk_flags,
    }


def build_signal_analysis(signal: dict, news: list[dict], threats: list[dict]) -> dict:
    bars = _fetch_all_bars(signal)
    ta = analyze_symbol(bars)
    return {
        "signal": signal,
        "analysis_generated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes": TIMEFRAME_LADDER,
        "ta": ta,
        "candles": {tf: _serialize_candles(bars.get(tf)) for tf in TIMEFRAME_LADDER},
        "sources": {
            tf: (bars.get(tf).attrs.get("source", "market data") if bars.get(tf) is not None else None)
            for tf in TIMEFRAME_LADDER
        },
        "confluence": _confluence(ta, signal),
        "news": news,
        "threats": threats,
    }
