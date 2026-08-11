"""
Market Regime Detection — auto-detects TA-Lib or ta fallback.

Regime FLAGS (added alongside the original label, not replacing it — the
learning engine keys regime_performance off `label`, so the label set must
stay stable): PANIC / EUPHORIA / VOLATILITY_EXPANSION / VOLATILITY_COMPRESSION
/ RISK_ON / RISK_OFF, each computed deterministically from measured inputs
(SPY ATR trend, VIX percentile vs its own trailing year, breadth). Flags are
descriptive add-ons; thresholds are stated conventions documented inline.
"""
import logging
import pandas as pd
from lib.ohlcv import fetch_multi_timeframe

logger = logging.getLogger(__name__)

try:
    import talib as _talib
    _BACKEND = "talib"
except ImportError:
    _BACKEND = "ta"


def _ema(close, period):
    if _BACKEND == "talib":
        return float(_talib.EMA(close, timeperiod=period).iloc[-1])
    import ta.trend as tat
    return float(tat.EMAIndicator(close=close, window=period).ema_indicator().iloc[-1])

def _rsi(close, period=14):
    if _BACKEND == "talib":
        return float(_talib.RSI(close, timeperiod=period).iloc[-1])
    import ta.momentum as tam
    return float(tam.RSIIndicator(close=close, window=period).rsi().iloc[-1])

def _adx(high, low, close, period=14):
    if _BACKEND == "talib":
        return float(_talib.ADX(high, low, close, timeperiod=period).iloc[-1])
    import ta.trend as tat
    return float(tat.ADXIndicator(high=high, low=low, close=close, window=period).adx().iloc[-1])


def compute_regime_flags(atr_series: pd.Series | None = None,
                         vix_current: float | None = None,
                         vix_history: list[float] | None = None,
                         breadth_pct_advancing: float | None = None) -> list[str]:
    """Deterministic add-on flags from whatever inputs are available. An
    absent input simply contributes no flags — never a guessed one.

    Thresholds (stated conventions):
      VOLATILITY_EXPANSION / COMPRESSION — current ATR vs its own 50-bar
        median, ±25%. Measured against the instrument's own history, not an
        absolute level.
      PANIC    — VIX above its trailing-year 90th percentile AND breadth
        below 25% advancing. Both legs required: high VIX alone is elevated
        volatility, not panic.
      EUPHORIA — VIX below its trailing-year 10th percentile AND breadth
        above 75% advancing.
      RISK_ON / RISK_OFF — breadth above 60% / below 40% advancing.
    """
    flags: list[str] = []

    if atr_series is not None and len(atr_series) >= 50:
        current = float(atr_series.iloc[-1])
        median = float(atr_series.tail(50).median())
        if median > 0:
            if current > median * 1.25:
                flags.append("VOLATILITY_EXPANSION")
            elif current < median * 0.75:
                flags.append("VOLATILITY_COMPRESSION")

    vix_pct = None
    if vix_current is not None and vix_history and len(vix_history) >= 60:
        vix_pct = sum(1 for v in vix_history if v < vix_current) / len(vix_history) * 100

    if vix_pct is not None and breadth_pct_advancing is not None:
        if vix_pct >= 90 and breadth_pct_advancing <= 25:
            flags.append("PANIC")
        elif vix_pct <= 10 and breadth_pct_advancing >= 75:
            flags.append("EUPHORIA")

    if breadth_pct_advancing is not None:
        if breadth_pct_advancing >= 60:
            flags.append("RISK_ON")
        elif breadth_pct_advancing <= 40:
            flags.append("RISK_OFF")

    return flags


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def get_regime() -> dict:
    regime = {
        "label": "Unknown", "risk": "medium",
        "spy_trend": "unknown", "recommendation": "Standard position sizing"
    }
    try:
        spy_bars = fetch_multi_timeframe("SPY", ["1D"])
        spy_1d   = spy_bars.get("1D")
        if spy_1d is None or len(spy_1d) < 50:
            return regime

        close = spy_1d["close"]
        high  = spy_1d["high"]
        low   = spy_1d["low"]

        ema21  = _ema(close, 21)
        ema50  = _ema(close, 50)
        ema200 = _ema(close, 200)
        rsi    = _rsi(close)
        adx    = _adx(high, low, close)
        last   = float(close.iloc[-1])

        high_52w     = float(close.tail(252).max())
        drawdown_pct = (last - high_52w) / high_52w * 100

        if last > ema21 > ema50 > ema200:   spy_trend = "strong_uptrend"
        elif last > ema50 > ema200:          spy_trend = "uptrend"
        elif last < ema21 < ema50:           spy_trend = "downtrend"
        else:                                spy_trend = "choppy"

        regime.update({
            "spy_trend": spy_trend, "spy_last": round(last, 2),
            "spy_ema21": round(ema21, 2), "spy_ema50": round(ema50, 2), "spy_ema200": round(ema200, 2),
            "spy_rsi": round(rsi, 1), "spy_adx": round(adx, 1), "spy_drawdown_pct": round(drawdown_pct, 1),
            "backend": _BACKEND
        })

        # Supplementary flags — each input independently optional; a failed
        # fetch just means fewer flags, never a wrong one.
        vix_current = vix_history = breadth = None
        try:
            from lib.futures_data import fetch_futures_ohlcv
            vix_df = fetch_futures_ohlcv("^VIX", "1D")
            if vix_df is not None and not vix_df.empty:
                vix_history = [float(v) for v in vix_df["close"].tolist()]
                vix_current = vix_history[-1]
        except Exception:
            pass
        try:
            from app.database import get_db, MarketAsset
            # Equities only: this regime is SPY-centric, and the tracked
            # universe is crypto-dominated (~85%) — whole-universe breadth
            # produced a RISK_OFF flag alongside a Risk-On Bull label
            # (observed live), which measured two different markets. The
            # cross-market breadth reading lives in the psychology index,
            # where mixing asset classes is the point.
            with get_db() as db:
                changes = [
                    r[0] for r in db.query(MarketAsset.change_percent)
                    .filter(MarketAsset.asset_class == "Equity").all()
                    if r[0] is not None
                ]
            if len(changes) >= 5:
                breadth = sum(1 for c in changes if c > 0) / len(changes) * 100
        except Exception:
            pass

        regime["flags"] = compute_regime_flags(
            atr_series=_atr_series(high, low, close),
            vix_current=vix_current, vix_history=vix_history,
            breadth_pct_advancing=breadth,
        )
        if breadth is not None:
            regime["breadth_pct_advancing"] = round(breadth, 1)

        if spy_trend in ("strong_uptrend", "uptrend") and rsi < 75 and adx > 20:
            regime.update({"label": "Risk-On Bull", "risk": "low",
                           "recommendation": "Full position sizing. Favor momentum longs."})
        elif spy_trend == "choppy" and adx < 20:
            regime.update({"label": "Range-Bound", "risk": "medium",
                           "recommendation": "Reduce size 30%. Favor mean-reversion Bounce signals."})
        elif spy_trend == "downtrend" or drawdown_pct < -15:
            regime.update({"label": "Bear / Risk-Off", "risk": "high",
                           "recommendation": "Reduce size 50-70%. Only highest confidence bounces."})
        elif rsi > 72:
            regime.update({"label": "Overbought Bull", "risk": "medium-high",
                           "recommendation": "Reduce size 20%. Wait for pullbacks."})
        else:
            regime.update({"label": "Neutral", "risk": "medium", "recommendation": "Standard sizing."})

    except Exception as e:
        logger.error(f"[Regime] SPY analysis failed: {e}")

    return regime
