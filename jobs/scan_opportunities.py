"""
Job: Opportunity Scanner v1.0
Proactive market scanner — finds high-probability setups across equities, crypto,
futures, and forex WITHOUT relying on a fixed symbol universe.

Scan modes:
  1. PRE_MARKET  — runs 6:30 AM PT (13:30 UTC), top movers + TA ranking for the day
  2. INTRADAY    — runs every 30 min during market hours, catches breakouts live
  3. CRYPTO      — runs 24/7 every 60 min, wide crypto universe scan

Sources:
  - yfinance: top gainers/losers/volume from Yahoo Finance screener
  - Alpaca: existing positions for context (skip duplicates)
  - SQLite OHLCV cache: TA on any symbol with cached bars

Signal routing:
  - Equity / ETF setups → Alpaca live paper (status=PendingApproval or Active)
  - Leveraged / short / futures / crypto setups → virtual paper engine (paper_mode=True)
  - High-conviction breakouts → Telegram alert immediately
"""
import json, logging, uuid, time, os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
from lib.crypto_market_data import (
    DEFAULT_CRYPTO_UNIVERSE,
    discover_crypto_universe,
    fetch_crypto_ohlcv,
    is_crypto_symbol as is_crypto_data_symbol,
    normalize_crypto_symbol,
)
from lib.signal_identity import signal_identity
from lib.trading_preferences import get_user_preference, horizon_for_timeframe, timeframe_allowed

# ── Universe definitions ──────────────────────────────────────────────────────

# Dynamic: pulled from yfinance screener each run (top gainers/losers/volume)
EQUITY_SCREENER_COUNT = 60   # how many movers to pull per screener

# Static extended universe — scanned every run alongside dynamic movers
EXTENDED_EQUITY = [
    # High-beta momentum
    "NVDA","AMD","TSLA","PLTR","COIN","MSTR","HOOD","SMCI","ARM","CRWV","NBIS","VRT",
    # Defense / energy
    "RTX","LMT","NOC","GD","BA","XOM","CVX","COP","OXY","SLB",
    # Semis
    "AVGO","TSM","INTC","QCOM","ANET","SOXX",
    # Mag7
    "MSFT","GOOGL","META","AMZN","AAPL",
    # Commodities / rates
    "GLD","SLV","GDX","GDXJ","USO","UNG","TLT","TBT",
    # Broad market
    "SPY","QQQ","IWM","DIA","XLK","XLE","XLF","XLV",
    # High-vol favorites
    "SOXS","SQQQ","TQQQ","SPXU","UVXY","SVXY",
    # Emerging momentum
    "RKLB","ASTS","ACHR","JOBY","IONQ","RXRX","AI","SOUN",
]

CRYPTO_UNIVERSE = list(DEFAULT_CRYPTO_UNIVERSE)
CRYPTO_SCAN_LIMIT = int(os.getenv("CRYPTO_SCAN_LIMIT", "150"))
ALLOW_YFINANCE_CRYPTO_FALLBACK = os.getenv("ALLOW_YFINANCE_CRYPTO_FALLBACK", "false").lower() in ("1", "true", "yes")

FUTURES_UNIVERSE_SCAN = [
    "GC=F",    # Gold
    "SI=F",    # Silver
    "CL=F",    # Crude Oil WTI
    "BZ=F",    # Brent Crude
    "NG=F",    # Natural Gas
    "HG=F",    # Copper
    "ZW=F",    # Wheat
    "ZC=F",    # Corn
    "ES=F",    # S&P 500 Futures
    "NQ=F",    # Nasdaq Futures
    "RTY=F",   # Russell 2000 Futures
    "YM=F",    # Dow Futures
    "GE=F",    # Eurodollar
    "6E=F",    # Euro FX
    "6J=F",    # Japanese Yen
    "6B=F",    # British Pound
    "^VIX",    # VIX (reference)
    "^TNX",    # 10Y Yield
]

FOREX_UNIVERSE = [
    "EURUSD=X","GBPUSD=X","USDJPY=X","AUDUSD=X",
    "USDCAD=X","USDCHF=X","NZDUSD=X","USDMXN=X",
]

# ── Scoring thresholds ────────────────────────────────────────────────────────
MIN_CONFIDENCE     = 60    # below this → skip signal
HIGH_CONFIDENCE    = 80    # above this → send Telegram alert
MIN_VOLUME_RATIO   = 1.5   # volume vs 20-day avg — below this → deprioritize
BREAKOUT_RSI_MIN   = 45    # RSI must be above this for a breakout (not overbought chasing)
BREAKOUT_RSI_MAX   = 72    # RSI must be below this (not already exhausted)
OVERSOLD_RSI_MAX   = 38    # RSI below this = oversold bounce candidate
MIN_ATR_PCT        = 0.8   # min ATR% for a trade to be worth taking
SCALP_TIMEFRAMES   = ("1m", "3m", "5m", "15m", "30m")


def _resample_intraday(df_1m):
    """Build the scalp timeframe ladder from one provider request."""
    if df_1m is None or df_1m.empty:
        return {}
    frames = {"1m": df_1m.tail(240)}
    rules = {"3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min"}
    for label, rule in rules.items():
        frame = df_1m.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        frames[label] = frame.tail(240)
    return frames


def _score_scalp_timeframes(tf_data: dict) -> dict | None:
    """Score short-term confluence without allowing a lone 1m trigger to win."""
    weights = {"1m": 0.75, "3m": 1.0, "5m": 1.4, "15m": 1.8, "30m": 2.0}
    directional_total = 0.0
    available_weight = 0.0
    bullish_frames, bearish_frames = [], []
    volume_surges, crossovers, overbought, oversold = 0, 0, 0, 0

    for tf in SCALP_TIMEFRAMES:
        data = tf_data.get(tf) or {}
        if data.get("error") or not data.get("price"):
            continue
        weight = weights[tf]
        score = 0
        bias = data.get("bias")
        macd = data.get("macd") or {}
        vwap = data.get("vwap") or {}
        emas = data.get("emas") or {}
        price = (data.get("price") or {}).get("last")

        score += 2 if bias == "bullish" else -2 if bias == "bearish" else 0
        score += 2 if macd.get("trend") == "bullish" else -2 if macd.get("trend") == "bearish" else 0
        score += 1 if vwap.get("position") == "above" else -1 if vwap.get("position") == "below" else 0
        if emas.get("ema9") and emas.get("ema21"):
            score += 1 if emas["ema9"] > emas["ema21"] else -1
        if price and emas.get("ema50"):
            score += 1 if price > emas["ema50"] else -1

        if score > 1:
            bullish_frames.append(tf)
        elif score < -1:
            bearish_frames.append(tf)
        directional_total += weight * score
        available_weight += weight * 7
        volume_surges += int(bool((data.get("volume") or {}).get("surge")))
        crossovers += int((macd.get("crossover") or "none") != "none")
        overbought += int((data.get("rsi") or 0) >= 70)
        oversold += int((data.get("rsi") or 100) <= 30)

    if available_weight == 0 or len(bullish_frames) + len(bearish_frames) < 3:
        return None

    direction = "Long" if directional_total > 0 else "Short"
    aligned = bullish_frames if direction == "Long" else bearish_frames
    if len(aligned) < 3:
        return None

    strength = min(1.0, abs(directional_total) / available_weight)
    confidence = 54 + strength * 30 + min(6, len(aligned)) + min(4, volume_surges * 2) + min(2, crossovers)
    confidence -= (overbought if direction == "Long" else oversold) * 2

    higher_biases = [
        (tf_data.get(tf) or {}).get("bias") for tf in ("1H", "4H")
        if (tf_data.get(tf) or {}).get("bias")
    ]
    opposing = "bearish" if direction == "Long" else "bullish"
    if higher_biases.count(opposing) >= 2:
        confidence -= 10
    elif opposing in higher_biases:
        confidence -= 4

    primary_tf = next((tf for tf in ("5m", "15m", "3m", "30m", "1m") if tf in aligned), aligned[0])
    return {
        "confidence": round(confidence),
        "direction": direction,
        "timeframe": primary_tf,
        "primary": tf_data[primary_tf],
        "volume_surges": volume_surges,
        "reasoning": [
            f"{direction} alignment on {', '.join(aligned)}",
            f"short-frame confluence {strength * 100:.0f}%",
            f"volume surges on {volume_surges} frames",
            f"higher-timeframe bias {', '.join(higher_biases) or 'unavailable'}",
        ],
    }


def _yf_fetch_ohlcv(symbol: str, period: str = "5d", interval: str = "1h"):
    """Fetch OHLCV from yfinance. Returns DataFrame or None."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        logger.debug(f"[Scanner] yf fetch failed {symbol}: {e}")
        return None


def _yf_fetch_screener(screener_type: str = "day_gainers", count: int = 50):
    """
    Pull top movers from Yahoo Finance screener.
    screener_type: day_gainers | day_losers | most_actives
    Returns list of ticker dicts with symbol, name, price, change_pct, volume.
    """
    try:
        import yfinance as yf
        screener = yf.Screener()
        screener.set_predefined_body(screener_type)
        screener.set_fields("symbol", "shortName", "regularMarketPrice",
                            "regularMarketChangePercent", "regularMarketVolume",
                            "averageDailyVolume3Month")
        result = screener.response
        if not result or "quotes" not in result:
            return []
        quotes = result["quotes"][:count]
        tickers = []
        for q in quotes:
            sym = q.get("symbol", "").upper()
            if not sym or "." in sym:  # skip OTC / warrants
                continue
            tickers.append({
                "symbol":      sym,
                "name":        q.get("shortName", sym),
                "price":       q.get("regularMarketPrice", 0),
                "change_pct":  q.get("regularMarketChangePercent", 0),
                "volume":      q.get("regularMarketVolume", 0),
                "avg_volume":  q.get("averageDailyVolume3Month", 1),
            })
        return tickers
    except Exception as e:
        logger.warning(f"[Scanner] Screener '{screener_type}' failed: {e}")
        return []


def _score_setup(sym: str, df_1h, df_4h, df_1d, meta: dict = None,
                 intraday_bars: dict = None) -> dict | None:
    """
    Run TA on the symbol's OHLCV data and produce a scored setup.
    Returns a signal dict if setup quality >= MIN_CONFIDENCE, else None.
    """
    try:
        from lib.ta_engine import compute_timeframe
        import numpy as np

        # Need at least 1H for intraday, 4H for swing, 1D for regime
        tf_data = {}
        all_frames = list((intraday_bars or {}).items()) + [("1H", df_1h), ("4H", df_4h), ("1D", df_1d)]
        for label, df in all_frames:
            if df is not None and len(df) >= 10:
                tf_data[label] = compute_timeframe(df, label)

        if not tf_data:
            return None

        # Use best available timeframe for primary signal
        primary = tf_data.get("4H") or tf_data.get("1H") or tf_data.get("1D")
        if not primary or primary.get("error"):
            return None

        price      = primary.get("price", {})
        emas       = primary.get("emas", {})
        rsi        = primary.get("rsi")
        macd_data  = primary.get("macd", {})
        bb         = primary.get("bollinger_bands", {})
        atr_data   = primary.get("atr", {})
        vol_data   = primary.get("volume", {})
        adx_data   = primary.get("adx", {})
        vwap_data  = primary.get("vwap", {})
        stoch_data = primary.get("stochastic", {})

        last_price = price.get("last", 0)
        if not last_price or last_price <= 0:
            return None

        atr_pct = atr_data.get("pct", 0) if atr_data else 0
        if atr_pct < MIN_ATR_PCT and not intraday_bars:
            return None  # too low volatility — not worth trading

        ema9  = emas.get("ema9")
        ema21 = emas.get("ema21")
        ema50 = emas.get("ema50")

        macd_val   = macd_data.get("macd") if macd_data else None
        macd_sig   = macd_data.get("signal") if macd_data else None
        macd_hist  = macd_data.get("histogram") if macd_data else None
        macd_cross = macd_data.get("crossover") if macd_data else None

        bb_upper = bb.get("upper") if bb else None
        bb_lower = bb.get("lower") if bb else None
        bb_mid   = bb.get("mid") if bb else None

        vol_ratio = 1.0
        if vol_data:
            avg_vol = vol_data.get("avg_20") or 1
            cur_vol = vol_data.get("current") or 0
            if avg_vol > 0:
                vol_ratio = cur_vol / avg_vol

        # Also use meta volume if available (from screener)
        if meta and meta.get("avg_volume"):
            meta_vol_ratio = meta.get("volume", 0) / max(meta["avg_volume"], 1)
            vol_ratio = max(vol_ratio, meta_vol_ratio)
        elif meta and meta.get("volume"):
            vol_ratio = max(vol_ratio, 1.25)

        confidence = 50
        direction  = "Long"
        setup_type = "neutral"
        signals_hit = []
        selected_timeframe = "4H" if "4H" in tf_data else "1H" if "1H" in tf_data else "1D"

        # ── BREAKOUT setup ──────────────────────────────────────────────────────
        ema_bull = ema9 and ema21 and ema9 > ema21
        if ema50:
            ema_bull = ema_bull and last_price > ema50

        if (rsi and BREAKOUT_RSI_MIN <= rsi <= BREAKOUT_RSI_MAX and ema_bull):
            confidence += 12
            setup_type  = "breakout"
            signals_hit.append(f"RSI={rsi:.0f} bullish zone")

        if macd_hist and macd_hist > 0 and macd_cross == "bullish":
            confidence += 10
            signals_hit.append("MACD bullish cross")
        elif macd_hist and macd_hist > 0:
            confidence += 5
            signals_hit.append("MACD positive")

        if vol_ratio >= MIN_VOLUME_RATIO:
            confidence += 8
            signals_hit.append(f"Vol {vol_ratio:.1f}x avg")

        if bb_mid and last_price > bb_mid:
            confidence += 5
            signals_hit.append("Above BB mid")

        if vwap_data and vwap_data.get("position") == "above" and direction in ("Long", "Bounce"):
            confidence += 3
            signals_hit.append("Above VWAP")

        if adx_data and adx_data.get("strong") and ema_bull:
            confidence += 4
            signals_hit.append(f"ADX trend {adx_data.get('value')}")

        # ── OVERSOLD BOUNCE setup ───────────────────────────────────────────────
        if rsi and rsi <= OVERSOLD_RSI_MAX:
            confidence += 15
            direction   = "Bounce"
            setup_type  = "oversold"
            signals_hit.append(f"RSI oversold={rsi:.0f}")
            if bb_lower and last_price <= bb_lower * 1.02:
                confidence += 8
                signals_hit.append("At BB lower")

        # ── MOMENTUM / CONTINUATION ─────────────────────────────────────────────
        daily = tf_data.get("1D", {})
        daily_rsi = daily.get("rsi") if daily else None
        daily_emas = daily.get("emas", {}) if daily else {}
        if daily_emas.get("ema50") and last_price > daily_emas["ema50"]:
            confidence += 6
            signals_hit.append("Above daily EMA50")
        if daily_rsi and 50 <= daily_rsi <= 68:
            confidence += 4
            signals_hit.append(f"Daily RSI={daily_rsi:.0f} bullish")

        # Paper-only short/breakdown setup. Save routing forces all short/leverage ideas to paper.
        ema_bear = ema9 and ema21 and ema9 < ema21
        if ema50:
            ema_bear = ema_bear and last_price < ema50

        bearish_points = 0
        if rsi and 30 <= rsi <= 58 and ema_bear:
            bearish_points += 12
            signals_hit.append(f"RSI={rsi:.0f} bearish zone")
        if macd_hist and macd_hist < 0 and macd_cross == "bearish":
            bearish_points += 12
            signals_hit.append("MACD bearish cross")
        elif macd_hist and macd_hist < 0:
            bearish_points += 6
            signals_hit.append("MACD negative")
        if bb_mid and last_price < bb_mid:
            bearish_points += 5
            signals_hit.append("Below BB mid")
        if vwap_data and vwap_data.get("position") == "below":
            bearish_points += 4
            signals_hit.append("Below VWAP")
        if adx_data and adx_data.get("strong") and ema_bear:
            bearish_points += 4
            signals_hit.append(f"ADX downtrend {adx_data.get('value')}")
        if rsi and rsi >= 70 and bb_upper and last_price >= bb_upper * 0.98 and macd_hist and macd_hist < 0:
            bearish_points += 15
            signals_hit.append(f"Overbought reversal RSI={rsi:.0f}")
        if daily_emas.get("ema50") and last_price < daily_emas["ema50"]:
            bearish_points += 5
            signals_hit.append("Below daily EMA50")
        if daily_rsi and 32 <= daily_rsi <= 50:
            bearish_points += 4
            signals_hit.append(f"Daily RSI={daily_rsi:.0f} bearish")
        if stoch_data and stoch_data.get("signal") == "overbought" and macd_hist and macd_hist < 0:
            bearish_points += 4
            signals_hit.append("Stoch overbought fade")
        if primary.get("obv_trend") == "falling":
            bearish_points += 3
            signals_hit.append("OBV falling")

        if bearish_points >= 14 and bearish_points > (confidence - 50):
            confidence = 50 + bearish_points
            direction = "Short"
            setup_type = "breakdown" if ema_bear else "reversal_short"

        # ── Penalty factors ─────────────────────────────────────────────────────
        if rsi and rsi > 75 and direction in ("Long", "Bounce"):
            confidence -= 15
            signals_hit.append(f"⚠ RSI overbought={rsi:.0f}")
        if vol_ratio < 0.7:
            confidence -= 8  # low vol = weak setup

        scalp = _score_scalp_timeframes(tf_data)
        if scalp and scalp["confidence"] >= MIN_CONFIDENCE and scalp["confidence"] > confidence:
            confidence = scalp["confidence"]
            direction = scalp["direction"]
            setup_type = "scalp_momentum" if direction == "Long" else "scalp_breakdown"
            selected_timeframe = scalp["timeframe"]
            primary = scalp["primary"]
            price = primary.get("price") or price
            last_price = price.get("last", last_price)
            atr_data = primary.get("atr") or atr_data
            atr_pct = (atr_data or {}).get("pct", atr_pct)
            scalp_volume = primary.get("volume") or {}
            if scalp_volume.get("surge_ratio"):
                vol_ratio = max(vol_ratio, scalp_volume["surge_ratio"])
            signals_hit = scalp["reasoning"]

        if confidence >= 82 and vol_ratio >= 1.8 and 1.2 <= atr_pct <= 8:
            if direction == "Long":
                direction = "Long_Leveraged"
                setup_type = f"{setup_type}_leveraged"
                signals_hit.append("Paper leverage candidate")
            elif direction == "Short":
                direction = "Short_Leveraged"
                setup_type = f"{setup_type}_leveraged"
                signals_hit.append("Paper short leverage candidate")

        # Clamp
        confidence = max(10, min(98, confidence))

        if confidence < MIN_CONFIDENCE:
            return None

        # ── Price targets ────────────────────────────────────────────────────────
        atr_val = atr_data.get("value", last_price * atr_pct / 100) if atr_data else last_price * 0.02

        is_long_side = direction.startswith("Long") or direction == "Bounce"
        if is_long_side:
            stop_loss    = round(last_price - atr_val * 1.5, 6 if last_price < 1 else 2)
            target_price = round(last_price + atr_val * 2.5, 6 if last_price < 1 else 2)
        else:
            stop_loss    = round(last_price + atr_val * 1.5, 6 if last_price < 1 else 2)
            target_price = round(last_price - atr_val * 2.5, 6 if last_price < 1 else 2)

        rr = abs(target_price - last_price) / max(abs(last_price - stop_loss), 0.0001)

        tf_snapshot = []
        for tf in ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D"):
            data = tf_data.get(tf) or {}
            if data.get("error") or not data:
                continue
            macd_trend = ((data.get("macd") or {}).get("trend") or "?")
            tf_snapshot.append(f"{tf}:{data.get('bias','?')}/RSI {data.get('rsi','?')}/MACD {macd_trend}")
        reasoning = (
            f"{setup_type.upper()} ({selected_timeframe}) | {' | '.join(signals_hit)} | "
            f"R:R={rr:.1f}x | ATR={atr_pct:.1f}% | TA: {'; '.join(tf_snapshot)}"
        )

        signal = {
            "asset_symbol": sym,
            "asset_name":   (meta or {}).get("name", sym),
            "direction":    direction,
            "confidence":   confidence,
            "setup_type":   setup_type,
            "entry_price":  round(last_price, 6 if last_price < 1 else 2),
            "target_price": target_price,
            "stop_loss":    stop_loss,
            "rr_ratio":     round(rr, 2),
            "timeframe":    selected_timeframe,
            "momentum":     "Bullish" if is_long_side else "Bearish",
            "reasoning":    reasoning,
            "key_risks":    f"Vol ratio={vol_ratio:.1f}x | ATR={atr_pct:.1f}%",
            "vol_ratio":    round(vol_ratio, 2),
            "paper_mode":   direction not in ("Long", "Bounce"),
            "paper_direction": direction if direction not in ("Long", "Bounce") else None,
        }
        from lib.signal_scorer import score_signal
        return score_signal(signal, tf_data, {"risk": "unknown"}, news_confidence=50.0)
    except Exception as e:
        logger.debug(f"[Scanner] Score failed {sym}: {e}")
        return None


def _classify_symbol(sym: str) -> tuple[str, bool]:
    """Returns (asset_class, is_paper)"""
    sym_up = sym.upper()
    # Futures
    if sym_up.endswith("=F") or sym_up.endswith("=X") or sym_up.startswith("^"):
        return "Futures", True
    # Crypto
    if is_crypto_data_symbol(sym_up):
        return "Crypto", False  # live crypto via Alpaca
    # Leveraged ETFs → paper
    if sym_up in {"SOXS","SQQQ","TQQQ","SPXU","UVXY","SVXY","TBT","LABD","LABU","TECL","TECS"}:
        return "Equity", True
    return "Equity", False


def _requires_paper_execution(sig: dict, asset_cls: str) -> bool:
    """Keep unsupported or higher-risk ideas out of live order execution."""
    direction = (sig.get("direction") or sig.get("paper_direction") or "").upper()
    if "SHORT" in direction or "LEVER" in direction or "5X" in direction:
        return True
    if asset_cls in ("Futures", "Forex"):
        return True
    if asset_cls == "Crypto":
        try:
            from lib.alpaca_client import is_crypto as is_alpaca_crypto
            if not is_alpaca_crypto(sig.get("asset_symbol") or ""):
                return True
        except Exception:
            return True
    return bool(sig.get("paper_mode"))


def _save_signals(signals: list, scan_mode: str):
    """Persist scored signals to DB, routing to live vs paper appropriately."""
    from app.database import get_db, TradingSignal
    from app.routes import log_decision

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    weekday = now_utc.weekday()
    market_open = (weekday < 5 and
                   (now_utc.hour > 13 or (now_utc.hour == 13 and now_utc.minute >= 30))
                   and now_utc.hour < 20)

    saved = updated = skipped = 0
    trade_mode = get_user_preference()["trade_mode"]

    with get_db() as db:
        # Fetch existing active signals to avoid duplicates
        from app.database import TradingSignal
        live_sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"])
        ).all()
        existing = {}
        for rec in live_sigs:
            k = signal_identity(rec.asset_symbol, rec.direction, getattr(rec, "user_id", "local"))
            existing[k] = rec

        for sig in signals:
            try:
                sym        = sig["asset_symbol"]
                asset_cls, is_paper = _classify_symbol(sym)

                # Normalize crypto symbol for Alpaca (BTC-USD → BTC/USD)
                if asset_cls == "Crypto":
                    sym = normalize_crypto_symbol(sym)
                    sig["asset_symbol"] = sym

                is_paper = bool(is_paper or _requires_paper_execution(sig, asset_cls))
                if not timeframe_allowed(sig.get("timeframe"), trade_mode):
                    skipped += 1
                    continue
                sig["trade_horizon"] = horizon_for_timeframe(sig.get("timeframe"))
                from lib.signal_levels import validate_signal_levels
                levels_ok, level_error = validate_signal_levels(sig)
                if not levels_ok:
                    logger.warning(f"[Scanner] Rejected {sym}: {level_error}")
                    skipped += 1
                    continue

                # Paper routing overrides
                if is_paper:
                    sig["paper_mode"]      = True
                    sig["paper_direction"] = sig.get("direction", "Long")
                    target_status = "Active"
                else:
                    sig["paper_mode"] = False
                    if asset_cls == "Crypto":
                        target_status = "Active"
                    else:
                        target_status = "Active" if market_open else "PendingApproval"

                rec_key = signal_identity(sym, sig.get("direction"))

                prior = existing.get(rec_key)
                if prior and prior.status in ("Active", "PendingApproval"):
                    prior.status = "Superseded"
                    prior.updated_date = now_iso
                    updated += 1

                new_record = TradingSignal(
                    id=str(uuid.uuid4()),
                    asset_symbol  = sym,
                    asset_name    = sig.get("asset_name", sym),
                    asset_class   = asset_cls,
                    direction     = sig.get("direction", "Long"),
                    confidence    = sig.get("confidence", 65),
                    composite_score = sig.get("composite_score"),
                    calibrated_confidence = sig.get("calibrated_confidence"),
                    score_breakdown = json.dumps(sig.get("score_breakdown", {}), sort_keys=True),
                    data_quality_score = sig.get("data_quality_score"),
                    freshness_score = sig.get("freshness_score"),
                    news_confidence = sig.get("news_confidence"),
                    setup_type = sig.get("setup_type"),
                    invalidation = sig.get("invalidation"),
                    signal_version = sig.get("signal_version", "v7.2"),
                    market_data_at = sig.get("market_data_at"),
                    expires_at = sig.get("expires_at"),
                    trade_horizon = sig.get("trade_horizon"),
                    timeframe     = sig.get("timeframe", "4H"),
                    entry_price   = sig.get("entry_price"),
                    target_price  = sig.get("target_price"),
                    stop_loss     = sig.get("stop_loss"),
                    reasoning     = f"[SCANNER:{scan_mode}] {sig.get('reasoning','')}",
                    key_risks     = sig.get("key_risks", ""),
                    momentum      = sig.get("momentum", "Neutral"),
                    rr_ratio      = sig.get("rr_ratio"),
                    status        = target_status,
                    generated_at  = now_iso,
                    paper_mode    = is_paper,
                    paper_direction = sig.get("paper_direction") if is_paper else None,
                    trigger_event = f"SCANNER:{scan_mode}",
                )
                db.add(new_record)
                existing[rec_key] = new_record
                saved += 1
            except Exception as e:
                logger.error(f"[Scanner] Save failed {sig.get('asset_symbol')}: {e}")
                skipped += 1

        db.commit()

    logger.info(f"[Scanner:{scan_mode}] Saved {saved} new, {updated} updated, {skipped} skipped")
    return saved, updated


def _send_telegram_alerts(high_conf_signals: list, scan_mode: str):
    """Send Telegram alerts for high-confidence scanner hits."""
    if not high_conf_signals:
        return
    try:
        from jobs.telegram_bot import send_message
        for sig in high_conf_signals[:5]:  # max 5 alerts per scan
            emoji = "🚀" if sig["direction"] == "Long" else "🔄" if sig["direction"] == "Bounce" else "⚡"
            msg = (
                f"{emoji} *SCANNER ALERT [{scan_mode}]*\n"
                f"*{sig['asset_symbol']}* — {sig['direction']}\n"
                f"Entry: ${sig['entry_price']:,.4f} | Target: ${sig['target_price']:,.4f} | Stop: ${sig['stop_loss']:,.4f}\n"
                f"Confidence: {sig['confidence']}% | R:R {sig.get('rr_ratio','?')}x\n"
                f"_{sig.get('reasoning','')[:120]}_"
            )
            send_message(msg)
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"[Scanner] Telegram alert failed: {e}")


def _scan_symbols(symbols: list, scan_mode: str, meta_map: dict = None):
    """
    Core scan loop: fetch OHLCV + score each symbol.
    Returns list of scored signal dicts sorted by confidence desc.
    """
    import concurrent.futures as cf
    meta_map = meta_map or {}
    results  = []
    lock     = __import__("threading").Lock()

    def _process(sym):
        try:
            scan_sym = normalize_crypto_symbol(sym) if is_crypto_data_symbol(sym) else sym
            intraday_bars = {}
            if is_crypto_data_symbol(scan_sym):
                df_1m = fetch_crypto_ohlcv(scan_sym, "1m", limit=300)
                intraday_bars = _resample_intraday(df_1m)
                df_1h = fetch_crypto_ohlcv(scan_sym, "1H", limit=120)
                if (df_1h is None or df_1h.empty) and ALLOW_YFINANCE_CRYPTO_FALLBACK:
                    df_1h = _yf_fetch_ohlcv(scan_sym, period="5d", interval="1h")
                df_4h = fetch_crypto_ohlcv(scan_sym, "4H", limit=120) if df_1h is not None else None
                df_1d = fetch_crypto_ohlcv(scan_sym, "1D", limit=180) if df_1h is not None else None
                if (df_4h is None or df_4h.empty) and df_1h is not None and ALLOW_YFINANCE_CRYPTO_FALLBACK:
                    df_4h = _yf_fetch_ohlcv(scan_sym, period="30d", interval="4h")
                if (df_1d is None or df_1d.empty) and df_1h is not None and ALLOW_YFINANCE_CRYPTO_FALLBACK:
                    df_1d = _yf_fetch_ohlcv(scan_sym, period="180d", interval="1d")
            else:
                df_1m = _yf_fetch_ohlcv(scan_sym, period="5d", interval="1m")
                intraday_bars = _resample_intraday(df_1m)
                df_1h = _yf_fetch_ohlcv(scan_sym, period="5d",  interval="1h")
                df_4h = _yf_fetch_ohlcv(scan_sym, period="30d", interval="4h") if df_1h is not None else None
                df_1d = _yf_fetch_ohlcv(scan_sym, period="90d", interval="1d") if df_1h is not None else None
            sig = _score_setup(
                scan_sym, df_1h, df_4h, df_1d,
                meta=meta_map.get(scan_sym) or meta_map.get(sym),
                intraday_bars=intraday_bars,
            )
            if sig:
                with lock:
                    results.append(sig)
        except Exception as e:
            logger.debug(f"[Scanner] {sym} failed: {e}")

    # Limit concurrency to avoid yfinance rate limits.
    # NOTE: deliberately not using the pool as a context manager — `with` calls
    # shutdown(wait=True) on exit, which blocks until every still-running worker
    # thread finishes even after cf.wait(..., timeout=...) has already given up,
    # negating the timeout. Shut down with wait=False so stragglers don't stall us.
    pool = cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="scanner")
    try:
        futs = [pool.submit(_process, sym) for sym in symbols]
        cf.wait(futs, timeout=120)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda x: x["confidence"], reverse=True)
    logger.info(f"[Scanner:{scan_mode}] {len(results)}/{len(symbols)} symbols produced signals")
    return results


def run_pre_market():
    """
    Pre-market scan — 6:30 AM PT (13:30 UTC).
    Pull top movers from screener + extended universe. Score all. Save top setups.
    """
    logger.info("[Scanner] 🌅 PRE-MARKET scan starting...")

    # Pull dynamic movers from Yahoo screener
    gainers  = _yf_fetch_screener("day_gainers",    EQUITY_SCREENER_COUNT)
    actives  = _yf_fetch_screener("most_actives",   EQUITY_SCREENER_COUNT)
    losers   = _yf_fetch_screener("day_losers",     EQUITY_SCREENER_COUNT // 2)

    all_mover_data = {m["symbol"]: m for m in gainers + actives + losers}
    dynamic_syms   = list(all_mover_data.keys())

    # Merge with extended static universe (deduplicated)
    all_syms = list(dict.fromkeys(dynamic_syms + EXTENDED_EQUITY))
    logger.info(f"[Scanner:PRE_MARKET] {len(all_syms)} equity symbols ({len(dynamic_syms)} dynamic + {len(EXTENDED_EQUITY)} static)")

    signals = _scan_symbols(all_syms, "PRE_MARKET", meta_map=all_mover_data)
    saved, updated = _save_signals(signals, "PRE_MARKET")

    high_conf = [s for s in signals if s["confidence"] >= HIGH_CONFIDENCE]
    _send_telegram_alerts(high_conf, "PRE_MARKET")

    logger.info(f"[Scanner:PRE_MARKET] Done — {saved} new signals | {len(high_conf)} high-confidence alerts")
    return {"saved": saved, "updated": updated, "signals": len(signals)}


def run_intraday():
    """
    Intraday scan — every 30 min during market hours.
    Focuses on breakouts + volume surges happening RIGHT NOW.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()
    market_open = (weekday < 5 and
                   (now_utc.hour > 13 or (now_utc.hour == 13 and now_utc.minute >= 30))
                   and now_utc.hour < 20)

    if not market_open:
        logger.debug("[Scanner:INTRADAY] Market closed — skipping")
        return {"skipped": True, "reason": "market_closed"}

    logger.info("[Scanner] 📊 INTRADAY scan starting...")

    # Focus on highest volume / biggest moves right now
    actives = _yf_fetch_screener("most_actives",   40)
    gainers = _yf_fetch_screener("day_gainers",    30)
    meta_map = {m["symbol"]: m for m in actives + gainers}
    syms = list(meta_map.keys())

    # Add our high-beta watchlist
    syms = list(dict.fromkeys(syms + [
        "NVDA","AMD","TSLA","PLTR","COIN","MSTR","HOOD","SMCI","ARM",
        "SPY","QQQ","IWM","SOXS","SQQQ","TQQQ","SPXU",
    ]))

    signals = _scan_symbols(syms, "INTRADAY", meta_map=meta_map)
    # Intraday: only save high-conviction setups to avoid noise
    top_signals = [s for s in signals if s["confidence"] >= 65]
    saved, updated = _save_signals(top_signals, "INTRADAY")

    high_conf = [s for s in top_signals if s["confidence"] >= HIGH_CONFIDENCE]
    _send_telegram_alerts(high_conf, "INTRADAY")

    logger.info(f"[Scanner:INTRADAY] Done — {saved} new | {len(high_conf)} alerts sent")
    return {"saved": saved, "updated": updated, "signals": len(top_signals)}


def run_crypto():
    """
    Crypto scan — runs every 60 min, 24/7.
    Wide crypto universe, routes to live Alpaca paper positions.
    """
    logger.info("[Scanner] 🪙 CRYPTO scan starting...")
    meta_map = {}
    try:
        for item in discover_crypto_universe(limit=CRYPTO_SCAN_LIMIT):
            sym = normalize_crypto_symbol(item.get("symbol", ""))
            meta_map[sym] = {
                "symbol": sym,
                "name": item.get("name") or sym,
                "price": item.get("price", 0),
                "change_pct": item.get("change_pct", 0),
                "volume": item.get("volume", 0),
                "source": item.get("source", "crypto_api"),
            }
    except Exception as e:
        logger.warning(f"[Scanner:CRYPTO] Dynamic crypto discovery failed: {e}")

    dynamic_syms = list(meta_map.keys())
    all_syms = list(dict.fromkeys(CRYPTO_UNIVERSE + dynamic_syms))
    logger.info(f"[Scanner:CRYPTO] Scanning {len(all_syms)} symbols ({len(dynamic_syms)} dynamic)")

    signals = _scan_symbols(all_syms, "CRYPTO", meta_map=meta_map)
    saved, updated = _save_signals(signals, "CRYPTO")

    high_conf = [s for s in signals if s["confidence"] >= HIGH_CONFIDENCE]
    _send_telegram_alerts(high_conf, "CRYPTO")

    logger.info(f"[Scanner:CRYPTO] Done — {saved} new | {len(high_conf)} alerts sent")
    return {"saved": saved, "updated": updated, "signals": len(signals)}


def run_futures():
    """
    Futures + Forex scan — every 4 hours.
    All routed to virtual paper engine with leverage.
    """
    logger.info("[Scanner] 📈 FUTURES/FOREX scan starting...")
    all_syms = FUTURES_UNIVERSE_SCAN + FOREX_UNIVERSE
    signals = _scan_symbols(all_syms, "FUTURES")

    # Mark all futures/forex as paper + leverage
    for sig in signals:
        sym = sig["asset_symbol"]
        if sig["direction"] == "Long" and sig["confidence"] >= 72:
            sig["direction"]       = "Long_5x"
            sig["paper_direction"] = "Long_5x"
        elif sig["direction"] in ("Long", "Bounce") and sig["confidence"] >= 65:
            sig["direction"]       = "Long"
            sig["paper_direction"] = "Long"
        elif sig["direction"] == "Bounce":
            sig["direction"]       = "Long"
            sig["paper_direction"] = "Long"
        sig["paper_mode"] = True

    saved, updated = _save_signals(signals, "FUTURES")

    high_conf = [s for s in signals if s["confidence"] >= HIGH_CONFIDENCE]
    _send_telegram_alerts(high_conf, "FUTURES")

    logger.info(f"[Scanner:FUTURES] Done — {saved} new | {len(high_conf)} alerts sent")
    return {"saved": saved, "updated": updated, "signals": len(signals)}


def run(mode: str = "all"):
    """
    Main entry point called by scheduler.
    mode: 'pre_market' | 'intraday' | 'crypto' | 'futures' | 'all'
    """
    from app.routes import log_decision
    results = {}

    try:
        if mode in ("pre_market", "all"):
            results["pre_market"] = run_pre_market()
        if mode in ("intraday", "all"):
            results["intraday"] = run_intraday()
        if mode in ("crypto", "all"):
            results["crypto"] = run_crypto()
        if mode in ("futures", "all"):
            results["futures"] = run_futures()

        total_saved = sum(r.get("saved", 0) for r in results.values() if isinstance(r, dict))
        log_decision("scanner", "SCAN_COMPLETE",
                     f"Opportunity scan [{mode}]: {total_saved} new signals across {list(results.keys())}",
                     score=total_saved, thinking=False)
    except Exception as e:
        logger.error(f"[Scanner] run({mode}) failed: {e}", exc_info=True)

    return results
