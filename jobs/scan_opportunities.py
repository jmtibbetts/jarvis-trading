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
import logging, uuid, time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

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

CRYPTO_UNIVERSE = [
    "BTC-USD","ETH-USD","SOL-USD","XRP-USD","BNB-USD","AVAX-USD",
    "LINK-USD","DOGE-USD","ADA-USD","AAVE-USD","DOT-USD","ATOM-USD",
    "SUI20947-USD","RENDER-USD","INJ-USD","NEAR-USD","OP-USD","ARB11841-USD",
    "LTC-USD","UNI7083-USD","PEPE24478-USD","WIF-USD","BONK-USD","JUP-USD",
]

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


def _score_setup(sym: str, df_1h, df_4h, df_1d, meta: dict = None) -> dict | None:
    """
    Run TA on the symbol's OHLCV data and produce a scored setup.
    Returns a signal dict if setup quality >= MIN_CONFIDENCE, else None.
    """
    try:
        from lib.ta_engine import compute_timeframe
        import numpy as np

        # Need at least 1H for intraday, 4H for swing, 1D for regime
        tf_data = {}
        for label, df in [("1H", df_1h), ("4H", df_4h), ("1D", df_1d)]:
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
        bb         = primary.get("bbands", {})
        atr_data   = primary.get("atr", {})
        vol_data   = primary.get("volume", {})

        last_price = price.get("last", 0)
        if not last_price or last_price <= 0:
            return None

        atr_pct = atr_data.get("pct", 0) if atr_data else 0
        if atr_pct < MIN_ATR_PCT:
            return None  # too low volatility — not worth trading

        ema9  = emas.get("ema9")
        ema21 = emas.get("ema21")
        ema50 = emas.get("ema50")

        macd_val   = macd_data.get("macd") if macd_data else None
        macd_sig   = macd_data.get("signal") if macd_data else None
        macd_hist  = macd_data.get("histogram") if macd_data else None
        macd_cross = macd_data.get("cross") if macd_data else None

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

        confidence = 50
        direction  = "Long"
        setup_type = "neutral"
        signals_hit = []

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

        # ── Penalty factors ─────────────────────────────────────────────────────
        if rsi and rsi > 75:
            confidence -= 15
            signals_hit.append(f"⚠ RSI overbought={rsi:.0f}")
        if vol_ratio < 0.7:
            confidence -= 8  # low vol = weak setup

        # Clamp
        confidence = max(10, min(98, confidence))

        if confidence < MIN_CONFIDENCE:
            return None

        # ── Price targets ────────────────────────────────────────────────────────
        atr_val = atr_data.get("value", last_price * atr_pct / 100) if atr_data else last_price * 0.02

        if direction in ("Long", "Bounce"):
            stop_loss    = round(last_price - atr_val * 1.5, 6 if last_price < 1 else 2)
            target_price = round(last_price + atr_val * 2.5, 6 if last_price < 1 else 2)
        else:
            stop_loss    = round(last_price + atr_val * 1.5, 6 if last_price < 1 else 2)
            target_price = round(last_price - atr_val * 2.5, 6 if last_price < 1 else 2)

        rr = abs(target_price - last_price) / max(abs(last_price - stop_loss), 0.0001)

        reasoning = f"{setup_type.upper()} | {' | '.join(signals_hit)} | R:R={rr:.1f}x | ATR={atr_pct:.1f}%"

        return {
            "asset_symbol": sym,
            "asset_name":   (meta or {}).get("name", sym),
            "direction":    direction,
            "confidence":   confidence,
            "setup_type":   setup_type,
            "entry_price":  round(last_price, 6 if last_price < 1 else 2),
            "target_price": target_price,
            "stop_loss":    stop_loss,
            "rr_ratio":     round(rr, 2),
            "timeframe":    "4H",
            "momentum":     "Bullish" if direction in ("Long", "Bounce") else "Bearish",
            "reasoning":    reasoning,
            "key_risks":    f"Vol ratio={vol_ratio:.1f}x | ATR={atr_pct:.1f}%",
            "vol_ratio":    round(vol_ratio, 2),
        }
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
    if sym_up.endswith("-USD") or "/" in sym_up:
        return "Crypto", False  # live crypto via Alpaca
    # Leveraged ETFs → paper
    if sym_up in {"SOXS","SQQQ","TQQQ","SPXU","UVXY","SVXY","TBT","LABD","LABU","TECL","TECS"}:
        return "Equity", True
    return "Equity", False


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

    with get_db() as db:
        # Fetch existing active signals to avoid duplicates
        from app.database import TradingSignal
        live_sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"])
        ).all()
        existing = {}
        for rec in live_sigs:
            k = (rec.asset_symbol, bool(getattr(rec, "paper_mode", False)))
            existing[k] = rec

        for sig in signals:
            try:
                sym        = sig["asset_symbol"]
                asset_cls, is_paper = _classify_symbol(sym)

                # Normalize crypto symbol for Alpaca (BTC-USD → BTC/USD)
                if asset_cls == "Crypto":
                    sym = sym.replace("-USD", "/USD")
                    sig["asset_symbol"] = sym

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

                rec_key = (sym, is_paper)

                if rec_key in existing:
                    rec = existing[rec_key]
                    if rec.status in ("Executed", "Closed", "Rejected", "PaperExecuted"):
                        skipped += 1
                        continue
                    # Update if new confidence is higher
                    new_conf = sig.get("confidence", 0)
                    if new_conf > (rec.confidence or 0):
                        rec.confidence   = new_conf
                        rec.entry_price  = sig.get("entry_price", rec.entry_price)
                        rec.target_price = sig.get("target_price", rec.target_price)
                        rec.stop_loss    = sig.get("stop_loss", rec.stop_loss)
                        rec.reasoning    = f"[SCANNER:{scan_mode}] {sig.get('reasoning','')}"
                        rec.momentum     = sig.get("momentum", rec.momentum)
                        rec.rr_ratio     = sig.get("rr_ratio", rec.rr_ratio)
                        rec.updated_date = now_iso
                        rec.status       = target_status
                        updated += 1
                    else:
                        skipped += 1
                    continue

                db.add(TradingSignal(
                    id=str(uuid.uuid4()),
                    asset_symbol  = sym,
                    asset_name    = sig.get("asset_name", sym),
                    asset_class   = asset_cls,
                    direction     = sig.get("direction", "Long"),
                    confidence    = sig.get("confidence", 65),
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
                    asset_class_raw = asset_cls,
                ))
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
            df_1h = _yf_fetch_ohlcv(sym, period="5d",  interval="1h")
            df_4h = _yf_fetch_ohlcv(sym, period="30d", interval="4h") if df_1h is not None else None
            df_1d = _yf_fetch_ohlcv(sym, period="90d", interval="1d") if df_1h is not None else None
            sig = _score_setup(sym, df_1h, df_4h, df_1d, meta=meta_map.get(sym))
            if sig:
                with lock:
                    results.append(sig)
        except Exception as e:
            logger.debug(f"[Scanner] {sym} failed: {e}")

    # Limit concurrency to avoid yfinance rate limits
    with cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="scanner") as pool:
        futs = [pool.submit(_process, sym) for sym in symbols]
        cf.wait(futs, timeout=120)

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
    signals = _scan_symbols(CRYPTO_UNIVERSE, "CRYPTO")
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
