"""
Job: Generate Trading Signals v6.4
Architecture fix: read TA from cache only (no live OHLCV fetch during signal gen).
The fetch_market_data job already runs every 15 min and populates ohlcv_cache.
Signal gen just reads that cache → builds prompts → calls LLM. Fast and lock-friendly.

v6.4 changes:
- Paper signals (Short, Short_Leveraged, Long_Leveraged) now generated via a dedicated Track E
- normalize_signal accepts an is_paper flag to skip the Long/Bounce enforcement
- Paper signals are saved with paper_mode=True and paper_direction set
"""
import logging, re, uuid
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset
from lib.lmstudio import call_lm_studio, parse_json, get_llm_config
from lib.ta_engine import analyze_symbol, build_ta_prompt_block

logger = logging.getLogger(__name__)

TRACK_A = ["RTX","LMT","NOC","GD","BA","XOM","CVX","COP","FANG","CEG","GLD","SLV","TLT","SPY","IWM","USO","UNG","GDX","GDXJ"]
TRACK_B = ["NVDA","AMD","MSFT","GOOGL","AAPL","META","AMZN","AVGO","TSM","ANET","INTC","QCOM","SMCI","VRT","SOXX","QQQ","CRWV","NBIS","PLTR","TSLA","COIN","MSTR","ARM","HOOD"]
TRACK_C = ["BTC/USD","ETH/USD","SOL/USD","XRP/USD","BNB/USD","AVAX/USD","LINK/USD","DOGE/USD","ADA/USD","AAVE/USD","DOT/USD","ATOM/USD","SUI/USD","RENDER/USD","INJ/USD","NEAR/USD","OP/USD","ARB/USD"]
# Track E: paper-only universe — best candidates for leveraged/short plays
TRACK_E_PAPER = ["NVDA","AMD","TSLA","COIN","MSTR","PLTR","SOXS","SQQQ","TQQQ","SPXU","BTC/USD","ETH/USD","SOL/USD","QQQ","SPY","SMCI","META","GOOGL","AMZN","MSFT"]
ALL_SYMBOLS = list(dict.fromkeys(TRACK_A + TRACK_B + TRACK_C))

COMMON_TICKERS = {"AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AMD","INTC","QCOM","AVGO","TSM","ARM","SMCI","PLTR","COIN","MSTR","HOOD","RBLX","SNAP","UBER","ABNB","SQ","PYPL","SHOP","NET","CRWD","PANW","ZS","DDOG","SNOW","MDB","AI","SOUN","IONQ","RXRX","ACHR","JOBY","RKLB","ASTS","XOM","CVX","COP","OXY","SLB","HAL","RTX","LMT","NOC","GD","BA","GLD","SLV","GDX","GDXJ","USO","UNG","SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","TLT","IEF","HYG","JPM","BAC","GS","BTC","ETH","SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI"}
CRYPTO_BASES = {"SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI","PEPE","LTC"}
SIGNAL_SCHEMA = """[{"asset_symbol":"NVDA","asset_name":"NVIDIA","asset_class":"Equity","direction":"Long","confidence":78,"timeframe":"4H","entry_price":875.00,"target_price":920.00,"stop_loss":850.00,"reasoning":"detailed reasoning","key_risks":"risks","momentum":"Bullish"}]"""

# Paper signal schema — direction can be Short, Short_Leveraged, Long_Leveraged
PAPER_SIGNAL_SCHEMA = """[{"asset_symbol":"NVDA","asset_name":"NVIDIA","asset_class":"Equity","direction":"Short_Leveraged","confidence":72,"timeframe":"4H","entry_price":875.00,"target_price":820.00,"stop_loss":900.00,"reasoning":"detailed reasoning","key_risks":"risks","momentum":"Bearish"}]"""

PAPER_DIRECTIONS = {"Short", "Short_Leveraged", "Long_Leveraged", "Long", "Bounce"}


def _read_ta_from_cache(symbols: list, timeframes=None) -> dict:
    """
    Read OHLCV bars directly from the SQLite cache (no live API calls).
    fetch_market_data already keeps the cache warm — this is instant.
    """
    if timeframes is None:
        timeframes = ["1H", "4H", "1D"]
    try:
        from lib.ohlcv_cache import _get_cached_bars, TF_CONFIG
        from datetime import datetime, timedelta, timezone
        result = {}
        end = datetime.now(timezone.utc)
        for sym in symbols:
            sym_bars = {}
            for tf in timeframes:
                cfg = TF_CONFIG.get(tf, TF_CONFIG['1D'])
                start = end - timedelta(days=cfg['lookback_days'])
                df = _get_cached_bars(sym, tf, start, end)
                sym_bars[tf] = df
            result[sym] = sym_bars
        return result
    except Exception as e:
        logger.error(f"[Signals] Cache read failed: {e} — falling back to live fetch")
        try:
            from lib.ohlcv import fetch_batch
            return fetch_batch(symbols[:10], timeframes)
        except:
            return {s: {tf: None for tf in timeframes} for s in symbols}


def extract_opportunistic(threats, news, fixed_symbols):
    fixed = {s.replace("/USD", "") for s in fixed_symbols}
    found = {}
    texts = (
        [f"{t.get('title','')} {t.get('description','')}" for t in threats[:20]] +
        [f"{n.get('title','')} {n.get('summary','')} {' '.join(n.get('affected_assets',[]))}" for n in news[:30]]
    )
    for text in texts:
        for m in re.finditer(r'\$([A-Z]{1,5})|\b([A-Z]{2,5})\b', text):
            t2 = (m.group(1) or m.group(2) or '').upper()
            if t2 and t2 in COMMON_TICKERS and t2 not in fixed:
                found[t2] = found.get(t2, 0) + 1
    return [
        {"symbol": f"{t}/USD" if t in CRYPTO_BASES else t, "is_crypto": t in CRYPTO_BASES}
        for t, cnt in sorted(found.items(), key=lambda x: -x[1]) if cnt >= 1
    ][:10]


def normalize_signal(s, ta_profiles, asset_map, is_paper=False):
    sym = (s.get("asset_symbol") or s.get("symbol") or s.get("ticker") or "").upper().strip()
    if not sym:
        return None
    if sym in CRYPTO_BASES:
        sym = f"{sym}/USD"
        s["asset_class"] = "Crypto"
    if "/" in sym:
        s["asset_class"] = "Crypto"
    s["asset_symbol"] = sym
    s["asset_name"] = s.get("asset_name") or sym

    direction = (s.get("direction") or "Long").replace(" ", "_").replace("-", "_")

    if is_paper:
        # Paper mode: allow all direction types — just normalize the key
        # Map common variants
        dir_map = {
            "Long":            "Long",
            "Bounce":          "Bounce",
            "Short":           "Short",
            "Short_Leveraged": "Short_Leveraged",
            "Shortleveraged":  "Short_Leveraged",
            "Long_Leveraged":  "Long_Leveraged",
            "Longleveraged":   "Long_Leveraged",
        }
        direction = dir_map.get(direction.capitalize(), direction)
        if direction not in PAPER_DIRECTIONS:
            direction = "Long"
        s["direction"] = direction
        s["paper_mode"] = True
        s["paper_direction"] = direction
    else:
        # Live mode: only Long or Bounce
        d_cap = direction.capitalize()
        s["direction"] = "Long" if d_cap not in ("Bounce", "Long") else d_cap

    ta = ta_profiles.get(sym, {})
    last_price = (
        ((ta.get("4H") or {}).get("price") or {}).get("last") or
        ((ta.get("1D") or {}).get("price") or {}).get("last") or
        asset_map.get(sym, {}).get("price")
    )
    if not last_price:
        return s

    entry = float(s.get("entry_price") or 0)
    if not entry or abs(entry - last_price) / last_price > 0.15:
        entry = round(last_price, 4 if last_price < 1 else 2)
    s["entry_price"] = entry

    atr_pct = (((ta.get("4H") or {}).get("atr") or {}).get("pct")) or 2.0

    if is_paper and s["direction"] in ("Short", "Short_Leveraged"):
        # Short: stop ABOVE entry, target BELOW entry
        stop = float(s.get("stop_loss") or 0)
        if not stop or stop <= entry:
            stop = round(entry * (1 + max(atr_pct, 1.5) / 100 * 1.5), 4 if entry < 1 else 2)
        s["stop_loss"] = stop
        target = float(s.get("target_price") or 0)
        if not target or target >= entry:
            target = round(entry * (1 - atr_pct / 100 * 2.5), 4 if entry < 1 else 2)
        s["target_price"] = target
    else:
        # Long / Long_Leveraged / Bounce: stop BELOW entry, target ABOVE entry
        stop = float(s.get("stop_loss") or 0)
        if not stop or stop >= entry:
            stop = round(entry * (1 - max(atr_pct, 1.5) / 100 * 1.5), 4 if entry < 1 else 2)
        s["stop_loss"] = stop
        target = float(s.get("target_price") or 0)
        if not target or target <= entry:
            target = round(entry * (1 + atr_pct / 100 * 2.5), 4 if entry < 1 else 2)
        s["target_price"] = target

    s["confidence"] = max(1, min(100, int(s.get("confidence") or 65)))
    s["timeframe"] = s.get("timeframe") or "4H"
    s["asset_class"] = s.get("asset_class") or "Equity"
    s["momentum"] = s.get("momentum") or ""
    s["key_risks"] = s.get("key_risks") or ""
    return s


def score_safe(signal, ta_profiles, regime, earnings_set):
    try:
        from lib.signal_scorer import score_signal
        sym = signal.get("asset_symbol", "")
        return score_signal(signal, ta_profiles.get(sym, {}), regime,
                            earnings_risk=sym.replace("/USD", "") in earnings_set)
    except Exception as e:
        logger.debug(f"[Signals] Scorer failed for {signal.get('asset_symbol')}: {e}")
        signal["composite_score"] = signal.get("confidence", 65)
        return signal


def run():
    logger.info("[Signals] Starting signal generation v6.4 (paper-enabled, position-aware)...")

    try:
        cfg = get_llm_config()
        logger.info(f"[Signals] LLM → platform={cfg.get('platform')} url={cfg.get('url')} model={cfg.get('model')}")
    except Exception as e:
        logger.error(f"[Signals] LLM config error: {e}")

    with get_db() as db:
        threats = [
            {"title": t.title, "description": t.description, "severity": t.severity, "country": t.country}
            for t in db.query(ThreatEvent).filter(ThreatEvent.status == "Active")
                       .order_by(ThreatEvent.published_at.desc()).limit(20).all()
        ]
        news = [
            {"title": n.title, "summary": n.summary, "source": n.source,
             "sentiment": n.sentiment,
             "affected_assets": n.affected_assets.split(",") if n.affected_assets else []}
            for n in db.query(NewsItem).order_by(NewsItem.published_at.desc()).limit(30).all()
        ]
        asset_map = {a.symbol: {"name": a.name, "price": a.price}
                     for a in db.query(MarketAsset).all()}

    held_positions = []
    held_symbols   = set()
    try:
        from lib.alpaca_client import get_positions
        raw_positions = get_positions()
        for p in raw_positions:
            sym  = str(p.symbol).upper()
            plpc = float(p.unrealized_plpc or 0) * 100
            mv   = float(p.market_value or 0)
            held_symbols.add(sym)
            held_symbols.add(sym.replace("USD", "/USD"))
            held_positions.append({
                "symbol": sym,
                "pnl_pct": round(plpc, 2),
                "market_value": round(mv, 2),
                "avg_entry": float(p.avg_entry_price or 0),
                "current_price": float(p.current_price or 0),
            })
    except Exception as e:
        logger.warning(f"[Signals] Could not fetch positions for context: {e}")

    logger.info(f"[Signals] Context: {len(threats)} threats, {len(news)} news, {len(asset_map)} assets | holding {len(held_positions)} positions")

    opp = extract_opportunistic(threats, news, ALL_SYMBOLS)
    opp_syms = [o["symbol"] for o in opp if o["symbol"] not in ALL_SYMBOLS]
    all_syms = ALL_SYMBOLS + opp_syms + TRACK_E_PAPER
    all_syms_dedup = list(dict.fromkeys(all_syms))
    logger.info(f"[Signals] {len(opp_syms)} opportunistic tickers: {opp_syms}")

    logger.info(f"[Signals] Reading TA from cache for {len(all_syms_dedup)} symbols...")
    bars = _read_ta_from_cache(all_syms_dedup)
    ta_profiles = {sym: analyze_symbol(sym_bars) for sym, sym_bars in bars.items()}
    has_ta = sum(1 for v in ta_profiles.values() if any(tf_data for tf_data in v.values() if tf_data is not None))
    logger.info(f"[Signals] TA profiles: {has_ta}/{len(all_syms_dedup)} have data")

    regime = {"label": "Unknown", "risk": "medium"}
    try:
        from lib.market_regime import get_regime
        regime = get_regime()
        logger.info(f"[Signals] Regime: {regime.get('label')} risk={regime.get('risk')}")
    except Exception as e:
        logger.warning(f"[Signals] Regime check failed: {e}")

    earnings_set = set()
    try:
        from lib.earnings_calendar import get_earnings_this_week
        earnings_set = get_earnings_this_week()
    except:
        pass

    threat_ctx = "\n".join([
        f"[{t.get('severity','?')}] {t.get('country','?')}: {t.get('title','')}"
        for t in threats[:10]
    ]) or "No active threats."

    news_ctx = "\n".join([
        f"[{n.get('sentiment','neutral').upper()}] {n.get('title','')} ({n.get('source','')})"
        for n in news[:15]
    ]) or "No recent news."

    sys_p = "You are an expert quantitative trader. Output only valid JSON arrays, no commentary, no markdown."
    bounce_rule = "\nRULES: direction must be 'Long' or 'Bounce' only. stop_loss BELOW entry. target ABOVE entry. R:R >= 2.\n"
    paper_rule  = ("\nRULES: direction must be 'Short', 'Short_Leveraged', or 'Long_Leveraged' ONLY. "
                   "For Short/Short_Leveraged: stop_loss ABOVE entry, target_price BELOW entry. "
                   "For Long_Leveraged: stop_loss BELOW entry, target ABOVE entry. R:R >= 2.\n")

    def ta_block(syms):
        blocks = [
            build_ta_prompt_block(s, ta_profiles.get(s, {}), asset_map.get(s, {}).get("name", s))
            for s in syms if ta_profiles.get(s)
        ]
        return "\n".join(blocks) or "No TA data available."

    held_ctx = ""
    if held_positions:
        held_lines = [
            f"  {p['symbol']}: {p['pnl_pct']:+.1f}% | entry=${p['avg_entry']:.4f} | current=${p['current_price']:.4f} | MV=${p['market_value']:,.0f}"
            for p in held_positions
        ]
        held_ctx = "=== CURRENT OPEN POSITIONS (DO NOT generate signals for these unless adding to a winner >+5%) ===\n" + "\n".join(held_lines) + "\n\n"

    def make_prompt(label, syms, task, rule=None):
        r = rule if rule is not None else bounce_rule
        schema = PAPER_SIGNAL_SCHEMA if rule == paper_rule else SIGNAL_SCHEMA
        prompt = (
            f"=== GEOPOLITICAL / MACRO INTEL ===\n{threat_ctx}\n\n"
            f"=== MARKET NEWS ===\n{news_ctx}\n\n"
            f"{held_ctx}"
            f"=== TECHNICAL ANALYSIS — {label} ===\n{ta_block(syms)}\n\n"
            f"=== TASK ===\n{task}{r}"
            f"Output format (return ONLY this JSON array):\n{schema}"
        )
        return prompt

    tracks = [
        ("A_macro", TRACK_A,
         make_prompt("MACRO/GEO/COMMODITIES", TRACK_A,
                     "Analyze defense (RTX/LMT/NOC/GD/BA), energy (XOM/CVX/COP), "
                     "commodities (GLD/SLV/GDX), rates (TLT), broad market (SPY/IWM). "
                     "Generate 4-6 high-conviction LONG or BOUNCE signals with TA references."), False),
        ("B_tech", TRACK_B,
         make_prompt("TECH/AI/GROWTH", TRACK_B,
                     "Analyze AI/semis (NVDA/AMD/AVGO/TSM/ARM/SMCI), "
                     "software (MSFT/GOOGL/META/AMZN), high-beta (PLTR/COIN/MSTR/TSLA). "
                     "Generate 4-7 high-conviction LONG or BOUNCE signals."), False),
        ("C_crypto", TRACK_C,
         make_prompt("CRYPTO", TRACK_C,
                     "Analyze BTC/ETH macro, L1s (SOL/XRP/BNB/AVAX), DeFi (LINK/AAVE). "
                     "24/7 market. Wider stops (8-12% ATR ok). "
                     "Generate 3-5 LONG or BOUNCE signals."), False),
        ("E_paper", TRACK_E_PAPER,
         make_prompt("PAPER TRADING — LEVERAGED/SHORT", TRACK_E_PAPER,
                     "Identify the BEST candidates for SHORT, SHORT_LEVERAGED, or LONG_LEVERAGED paper trades. "
                     "Use macro/news to find overextended longs (short candidates) or confirmed breakouts for leveraged longs. "
                     "Generate 3-5 paper trading signals only.", rule=paper_rule), True),
    ]
    if opp_syms:
        tracks.append((
            "D_opp", opp_syms,
            make_prompt("OPPORTUNISTIC", opp_syms,
                        f"These tickers appeared in threat/news: {opp_syms[:8]}. "
                        "Generate 2-4 signals for the strongest setups only."), False
        ))

    for name, syms, prompt, _ in tracks:
        logger.info(f"[Signals] Track {name}: {len(syms)} syms | ~{len(prompt)//4} tokens")

    all_raw        = []  # (signal_dict, is_paper)
    for name, syms, prompt, is_paper in tracks:
        try:
            logger.info(f"[Signals] Calling LLM for track {name}...")
            r = call_lm_studio(prompt, system=sys_p, max_tokens=2500, temperature=0.15)
            logger.info(f"[Signals] Track {name} → {len(r)} chars returned")
            sigs = parse_json(r)
            if isinstance(sigs, list):
                logger.info(f"[Signals] Track {name} → parsed {len(sigs)} signals")
                all_raw.extend((s, is_paper) for s in sigs)
            elif isinstance(sigs, dict):
                for k in ["signals", "trades", "setups", "results"]:
                    if sigs.get(k):
                        logger.info(f"[Signals] Track {name} → {len(sigs[k])} signals from key '{k}'")
                        all_raw.extend((s, is_paper) for s in sigs[k])
                        break
            else:
                logger.warning(f"[Signals] Track {name} → unexpected response type {type(sigs)}: {r[:200]}")
        except Exception as e:
            logger.error(f"[Signals] Track {name} FAILED: {type(e).__name__}: {e}")

    logger.info(f"[Signals] {len(all_raw)} raw signals from LLM across all tracks")

    if not all_raw:
        logger.warning("[Signals] No signals generated — check LLM connection and logs above")
        return {"saved": 0, "skipped": 0, "regime": regime.get("label"), "error": "no_llm_output"}

    now_utc  = datetime.now(timezone.utc)
    weekday  = now_utc.weekday()
    market_open = weekday < 5 and (now_utc.hour > 13 or (now_utc.hour == 13 and now_utc.minute >= 30)) and now_utc.hour < 20

    now_iso = now_utc.isoformat()
    saved = updated = skipped = 0

    with get_db() as db:
        stale = (now_utc - timedelta(hours=6)).isoformat()
        expired = db.query(TradingSignal).filter(
            TradingSignal.status == "Active",
            TradingSignal.generated_at.isnot(None),
            TradingSignal.generated_at < stale
        ).all()
        for s in expired:
            s.status = "Expired"
            s.updated_date = now_iso
        if expired:
            logger.info(f"[Signals] Expired {len(expired)} stale Active signals")

        # Exclude PaperExecuted so each signal cycle can generate fresh paper entries
        live_records = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"])
        ).all()
        # Key: (symbol, is_paper) to prevent collisions between live and paper signals
        existing_map = {}
        for rec in live_records:
            k = (rec.asset_symbol, bool(getattr(rec, 'paper_mode', False)))
            existing_map[k] = rec

        for raw, is_paper in all_raw:
            try:
                n = normalize_signal(raw, ta_profiles, asset_map, is_paper=is_paper)
                if not n:
                    skipped += 1
                    continue
                sym = n.get("asset_symbol")
                scored = score_safe(n, ta_profiles, regime, earnings_set)

                is_crypto_sym = "/" in sym or sym.upper().endswith("USD")
                if is_crypto_sym:
                    target_status = "Active"
                else:
                    target_status = "Active" if market_open else "PendingApproval"

                # Paper signals are always "Active" — they don't go to Alpaca
                if is_paper:
                    target_status = "Active"

                rec_key = (sym, is_paper)
                if rec_key in existing_map:
                    rec = existing_map[rec_key]
                    rec.asset_name       = scored.get("asset_name", rec.asset_name)
                    rec.direction        = scored.get("direction", rec.direction)
                    rec.confidence       = scored.get("confidence", rec.confidence)
                    rec.composite_score  = scored.get("composite_score", rec.composite_score)
                    rec.timeframe        = scored.get("timeframe", rec.timeframe)
                    rec.entry_price      = scored.get("entry_price", rec.entry_price)
                    rec.target_price     = scored.get("target_price", rec.target_price)
                    rec.stop_loss        = scored.get("stop_loss", rec.stop_loss)
                    rec.reasoning        = scored.get("reasoning", rec.reasoning)
                    rec.key_risks        = scored.get("key_risks", rec.key_risks)
                    rec.momentum         = scored.get("momentum", rec.momentum)
                    rec.signal_source    = scored.get("signal_source", rec.signal_source)
                    rec.earnings_risk    = bool(scored.get("earnings_risk", False))
                    rec.rr_ratio         = scored.get("rr_ratio", rec.rr_ratio)
                    rec.generated_at     = now_iso
                    rec.updated_date     = now_iso
                    if is_paper:
                        rec.paper_mode      = True
                        rec.paper_direction = scored.get("paper_direction") or scored.get("direction")
                    if rec.status not in ("Executed", "Closed", "Rejected", "PaperExecuted"):
                        rec.status = target_status
                    logger.debug(f"[Signals] Upsert ↺ {sym} (paper={is_paper}) → {target_status}")
                    updated += 1
                else:
                    db.add(TradingSignal(
                        id=str(uuid.uuid4()),
                        asset_symbol=scored.get("asset_symbol"),
                        asset_name=scored.get("asset_name"),
                        asset_class=scored.get("asset_class", "Equity"),
                        direction=scored.get("direction", "Long"),
                        confidence=scored.get("confidence", 65),
                        composite_score=scored.get("composite_score"),
                        timeframe=scored.get("timeframe", "4H"),
                        entry_price=scored.get("entry_price"),
                        target_price=scored.get("target_price"),
                        stop_loss=scored.get("stop_loss"),
                        reasoning=scored.get("reasoning", ""),
                        key_risks=scored.get("key_risks", ""),
                        momentum=scored.get("momentum", ""),
                        signal_source=scored.get("signal_source", "watchlist"),
                        earnings_risk=bool(scored.get("earnings_risk", False)),
                        rr_ratio=scored.get("rr_ratio"),
                        status=target_status,
                        paper_mode=True if is_paper else False,
                        paper_direction=scored.get("paper_direction") if is_paper else None,
                        generated_at=now_iso,
                        created_date=now_iso,
                        updated_date=now_iso,
                    ))
                    existing_map[rec_key] = True
                    saved += 1
                    logger.debug(f"[Signals] New {'PAPER' if is_paper else 'LIVE'} signal: {sym} {scored.get('direction')} → {target_status}")
            except Exception as e:
                logger.error(f"[Signals] Save error: {e} | raw={raw}")
                skipped += 1

    logger.info(
        f"[Signals] Done — {saved} new | {updated} updated | {skipped} skipped | "
        f"market={'OPEN' if market_open else 'CLOSED'} | regime={regime.get('label')}"
    )
    return {"saved": saved, "updated": updated, "skipped": skipped, "regime": regime.get("label"), "market_open": market_open}
