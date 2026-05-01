"""
Job: Generate Trading Signals v7.0
Batch-per-symbol architecture: each track is split into batches of BATCH_SIZE symbols.
Each batch gets its own focused LLM call, guaranteed to fit within any token cap.

v7.0 changes:
- run_track() replaced with run_track_batched() — splits any track into N×BATCH_SIZE sub-calls
- BATCH_SIZE = 5 symbols per LLM call (5 signals × ~160 tokens/signal ≈ 800 tokens output)
- Each batch gets minimal but complete context: macro/news header + only its own TA blocks
- Batch prompts stripped of per-track learning context bloat to keep input tokens low
- TRACK_MAX_TOKENS set to 1800 — safe floor for any LM Studio token cap
- Parallel ThreadPoolExecutor still used, now across all batches from all tracks
- Track F (futures) also batched with FUTURES_BATCH_SIZE = 4

v6.9.2 changes:
- Smart Tier 5 lesson injection — get_lessons_context() now track-aware + category-deduplicated

v6.9 changes:
- ta_block() now builds TA-only text (no per-symbol learning context calls)
- Global learning context (lessons + regime perf) injected once per make_prompt()
- Accuracy context condensed: only symbols with >=3 trades get a summary line

v6.8 changes:
- thinking=True on all LLM track calls for full chain-of-thought reasoning

v6.4 changes:
- Paper signals (Short, Short_Leveraged, Long_Leveraged) now generated via a dedicated Track E
- normalize_signal accepts an is_paper flag to skip the Long/Bounce enforcement
- Paper signals are saved with paper_mode=True and paper_direction set
"""
import logging, re, uuid, threading
from datetime import datetime, timezone, timedelta
from app.routes import log_decision
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset
from lib.lmstudio import call_lm_studio, parse_json, get_llm_config
from lib.ta_engine import analyze_symbol, build_ta_prompt_block
from lib.learning_engine import get_accuracy_context, get_pattern_context, get_regime_context, get_lessons_context, get_lessons_context_for_track, get_confidence_adjustment
from lib.futures_data import PAPER_FUTURES, get_futures_news_context, fetch_futures_multi_tf, FUTURES_UNIVERSE

logger = logging.getLogger(__name__)

TRACK_A = ["RTX","LMT","NOC","GD","BA","XOM","CVX","COP","FANG","CEG","GLD","SLV","TLT","SPY","IWM","USO","UNG","GDX","GDXJ"]
TRACK_B = ["NVDA","AMD","MSFT","GOOGL","AAPL","META","AMZN","AVGO","TSM","ANET","INTC","QCOM","SMCI","VRT","SOXX","QQQ","CRWV","NBIS","PLTR","TSLA","COIN","MSTR","ARM","HOOD"]
TRACK_C = ["BTC/USD","ETH/USD","SOL/USD","XRP/USD","BNB/USD","AVAX/USD","LINK/USD","DOGE/USD","ADA/USD","AAVE/USD","DOT/USD","ATOM/USD","SUI/USD","RENDER/USD","INJ/USD","NEAR/USD","OP/USD","ARB/USD"]
# Track E: paper-only universe — best candidates for leveraged/short plays
TRACK_E_PAPER = ["NVDA","AMD","TSLA","COIN","MSTR","PLTR","SOXS","SQQQ","TQQQ","SPXU","BTC/USD","ETH/USD","SOL/USD","QQQ","SPY","SMCI","META","GOOGL","AMZN","MSFT"]

# Track F: paper-only futures / forex / commodities
TRACK_F_FUTURES = [sym for sym in PAPER_FUTURES
                   if sym not in ["^VIX","^TNX","^TYX"]]  # exclude pure reference indices

FUTURES_PAPER_DIRECTIONS = {"Long", "Short", "Long_Leveraged", "Short_Leveraged",
                             "Long_5x", "Short_5x", "Long_10x", "Short_10x",
                             "Long_20x", "Short_20x"}
ALL_SYMBOLS = list(dict.fromkeys(TRACK_A + TRACK_B + TRACK_C))

COMMON_TICKERS = {"AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AMD","INTC","QCOM","AVGO","TSM","ARM","SMCI","PLTR","COIN","MSTR","HOOD","RBLX","SNAP","UBER","ABNB","SQ","PYPL","SHOP","NET","CRWD","PANW","ZS","DDOG","SNOW","MDB","AI","SOUN","IONQ","RXRX","ACHR","JOBY","RKLB","ASTS","XOM","CVX","COP","OXY","SLB","HAL","RTX","LMT","NOC","GD","BA","GLD","SLV","GDX","GDXJ","USO","UNG","SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","TLT","IEF","HYG","JPM","BAC","GS","BTC","ETH","SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI"}
CRYPTO_BASES = {"SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI","PEPE","LTC"}
SIGNAL_SCHEMA = """[{"asset_symbol":"NVDA","asset_name":"NVIDIA","asset_class":"Equity","direction":"Long","confidence":78,"timeframe":"4H","entry_price":875.00,"target_price":920.00,"stop_loss":850.00,"reasoning":"brief TA rationale","key_risks":"key risk","momentum":"Bullish"}]"""

# Paper signal schema — direction can be Short, Short_Leveraged, Long_Leveraged
PAPER_SIGNAL_SCHEMA = """[{"asset_symbol":"NVDA","asset_name":"NVIDIA","asset_class":"Equity","direction":"Short_Leveraged","confidence":72,"timeframe":"4H","entry_price":875.00,"target_price":820.00,"stop_loss":900.00,"reasoning":"brief TA rationale","key_risks":"key risk","momentum":"Bearish"}]"""
FUTURES_PAPER_SCHEMA = """[{"asset_symbol":"GC=F","asset_name":"Gold Futures","asset_class":"Futures","direction":"Long_5x","confidence":75,"timeframe":"4H","entry_price":2310.50,"target_price":2380.00,"stop_loss":2265.00,"reasoning":"brief TA rationale","key_risks":"key risk","momentum":"Bullish"}]"""

PAPER_DIRECTIONS = {"Short", "Short_Leveraged", "Long_Leveraged", "Long", "Bounce", "Long_5x", "Short_5x", "Long_10x", "Short_10x", "Long_20x", "Short_20x"}

# Batch sizes — tuned so output stays under LM Studio's 2000-token hard cap
# 5 symbols × ~160 tokens/signal = ~800 tokens → safe margin for thinking tokens
BATCH_SIZE         = 5   # equity / crypto tracks (A, B, C, E)
FUTURES_BATCH_SIZE = 4   # futures / forex (Track F) — larger per-symbol TA blocks
TRACK_MAX_TOKENS   = 1800  # conservative ceiling that works even on 2000-cap servers


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
    # Futures universe check must come BEFORE crypto detection
    if sym in FUTURES_UNIVERSE:
        fu_meta = FUTURES_UNIVERSE[sym]
        s["asset_class"] = "Forex" if fu_meta.get("category") == "Forex" else "Futures"
    elif sym in CRYPTO_BASES:
        sym = f"{sym}/USD"
        s["asset_class"] = "Crypto"
    elif "/" in sym and not sym.endswith("=X") and not sym.endswith("=F"):
        s["asset_class"] = "Crypto"
    s["asset_symbol"] = sym
    s["asset_name"] = s.get("asset_name") or sym

    direction = (s.get("direction") or "Long").replace(" ", "_").replace("-", "_")

    if is_paper:
        dir_map = {
            "Long":             "Long",
            "Bounce":           "Bounce",
            "Short":            "Short",
            "Short_Leveraged":  "Short_Leveraged",
            "Shortleveraged":   "Short_Leveraged",
            "Long_Leveraged":   "Long_Leveraged",
            "Longleveraged":    "Long_Leveraged",
            "Long_2X":          "Long_Leveraged",
            "Long_5X":          "Long_5x",
            "Long_10X":         "Long_10x",
            "Long_20X":         "Long_20x",
            "Long5X":           "Long_5x",
            "Long10X":          "Long_10x",
            "Long20X":          "Long_20x",
            "Short_2X":         "Short_Leveraged",
            "Short_5X":         "Short_5x",
            "Short_10X":        "Short_10x",
            "Short_20X":        "Short_20x",
            "Short5X":          "Short_5x",
            "Short10X":         "Short_10x",
            "Short20X":         "Short_20x",
        }
        direction = dir_map.get(direction, dir_map.get(direction.capitalize(), direction))
        if direction not in PAPER_DIRECTIONS:
            direction = "Long"
        s["direction"] = direction
        s["paper_mode"] = True
        s["paper_direction"] = direction
    else:
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
        stop = float(s.get("stop_loss") or 0)
        if not stop or stop <= entry:
            stop = round(entry * (1 + max(atr_pct, 1.5) / 100 * 1.5), 4 if entry < 1 else 2)
        s["stop_loss"] = stop
        target = float(s.get("target_price") or 0)
        if not target or target >= entry:
            target = round(entry * (1 - atr_pct / 100 * 2.5), 4 if entry < 1 else 2)
        s["target_price"] = target
    else:
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


# ── TA block builder — TA only, no per-symbol learning context ────────────────
def ta_block(syms, futures_profiles=None):
    blocks = []
    for s in syms:
        profile = ta_profiles_global.get(s)
        if not profile and futures_profiles:
            profile = futures_profiles.get(s)
        if profile:
            fu_meta = FUTURES_UNIVERSE.get(s, {})
            name = asset_map_global.get(s, {}).get("name") or fu_meta.get("name") or s
            ta_txt = build_ta_prompt_block(s, profile, name)
            blocks.append(ta_txt)
    return "\n".join(blocks) or "No TA data available."


# ── Condensed accuracy summary across a list of symbols ─────────────────────
def build_accuracy_summary(syms: list) -> str:
    """Build a compact accuracy block — symbols with ≥3 trades only."""
    lines = []
    for sym in syms:
        try:
            txt = get_accuracy_context(sym, lookback_days=30)
            if txt and txt.strip():
                for line in txt.splitlines():
                    if "Overall:" in line:
                        lines.append(f"  {sym}: {line.strip()}")
                        break
        except Exception:
            pass
    if not lines:
        return ""
    return "\n📊 ACCURACY:\n" + "\n".join(lines) + "\n"


# Module-level references so ta_block() can access them without closure issues
ta_profiles_global = {}
asset_map_global   = {}


# ── Batch prompt builder ──────────────────────────────────────────────────────
def make_batch_prompt(batch_syms: list, track_label: str, task_hint: str,
                      threat_ctx: str, news_ctx: str, regime: dict,
                      held_ctx: str, rule: str, schema: str,
                      futures_profiles: dict = None) -> str:
    """
    Build a compact prompt for a single batch of symbols.
    Input token budget target: ~600-800 tokens so output has full room within a 2000-token cap.
    """
    ta_txt = ta_block(batch_syms, futures_profiles=futures_profiles)
    acc    = build_accuracy_summary(batch_syms)
    regime_label = regime.get("label", "Unknown")
    regime_risk  = regime.get("risk", "medium")

    prompt = (
        f"Regime: {regime_label} | Risk: {regime_risk}\n"
        f"Threats: {threat_ctx}\n"
        f"News: {news_ctx}\n"
        f"{held_ctx}"
        f"{acc}"
        f"=== TA — {track_label} ===\n{ta_txt}\n\n"
        f"Task: {task_hint}{rule}"
        f"Return ONLY the JSON array starting with '[' and ending with ']'.\n"
        f"Schema: {schema}"
    )
    tok_est = len(prompt) // 4
    logger.info(f"[Signals] Batch '{track_label}' {batch_syms}: ~{tok_est} tok in | {TRACK_MAX_TOKENS} max out")
    return prompt


def run():
    global ta_profiles_global, asset_map_global

    logger.info("[Signals] Starting signal generation v7.0 (batch architecture)...")

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

    asset_map_global = asset_map

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
    ta_profiles_global = ta_profiles
    has_ta = sum(1 for v in ta_profiles.values() if any(tf_data for tf_data in v.values() if tf_data is not None))
    logger.info(f"[Signals] TA profiles: {has_ta}/{len(all_syms_dedup)} have data")

    regime = {"label": "Unknown", "risk": "medium"}
    try:
        from lib.market_regime import get_regime
        regime = get_regime()
        logger.info(f"[Signals] Regime: {regime.get('label')} risk={regime.get('risk')}")
    except Exception as e:
        logger.warning(f"[Signals] Regime check failed: {e}")

    futures_news_ctx = ""
    try:
        futures_news_ctx = get_futures_news_context(max_items=4)
        if futures_news_ctx:
            logger.info("[Signals] Futures news context loaded")
    except Exception as _fn:
        logger.debug(f"[Signals] Futures news unavailable: {_fn}")

    earnings_set = set()
    try:
        from lib.earnings_calendar import get_earnings_this_week
        earnings_set = get_earnings_this_week()
    except:
        pass

    # Compact single-line context strings — keep input tokens minimal
    threat_ctx = " | ".join([
        f"[{t.get('severity','?')}] {t.get('country','?')}: {t.get('title','')[:60]}"
        for t in threats[:4]
    ]) or "No active threats."

    news_ctx = " | ".join([
        f"[{n.get('sentiment','?').upper()}] {n.get('title','')[:60]}"
        for n in news[:5]
    ]) or "No recent news."

    sys_p = "You are an expert quantitative trader. Output only valid JSON arrays. No commentary, no markdown — start with '[' end with ']'."
    bounce_rule = " direction='Long' or 'Bounce'. stop_loss BELOW entry. target ABOVE entry. R:R>=2. Generate 1-2 best signals only.\n"
    paper_rule  = " direction='Short','Short_Leveraged', or 'Long_Leveraged'. Short: stop ABOVE entry, target BELOW. LongLev: stop BELOW, target ABOVE. R:R>=2. Generate 1-2 best signals only.\n"
    futures_rule = " direction: Long/Long_5x/Long_10x/Long_20x/Short/Short_5x/Short_10x/Short_20x. asset_class='Futures' or 'Forex'. Generate 1-2 best signals only.\n"

    held_ctx = ""
    if held_positions:
        held_lines = [
            f"{p['symbol']}:{p['pnl_pct']:+.1f}%"
            for p in held_positions
        ]
        held_ctx = f"Open positions (skip unless adding to winner>+5%): {', '.join(held_lines)}\n"

    # ── Build batch list across all tracks ──────────────────────────────────
    # Each entry: (batch_id, batch_syms, prompt, is_paper)
    all_batches = []

    def _chunk(lst, n):
        """Yield successive n-sized chunks from list."""
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    # Track A — macro / defense / energy / commodities
    for i, batch in enumerate(_chunk(TRACK_A, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "MACRO/GEO/COMMODITIES",
            "Defense/energy/commodity/rates setup. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, bounce_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"A{i}", batch, prompt, False))

    # Track B — tech / AI / growth
    for i, batch in enumerate(_chunk(TRACK_B, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "TECH/AI/GROWTH",
            "AI/semi/software/high-beta setup. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, bounce_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"B{i}", batch, prompt, False))

    # Track C — crypto
    for i, batch in enumerate(_chunk(TRACK_C, BATCH_SIZE)):
        crypto_rule = " direction='Long' or 'Bounce'. stop_loss BELOW entry (8-12% ATR ok). target ABOVE. R:R>=2. Generate 1-2 best signals only.\n"
        prompt = make_batch_prompt(
            batch, "CRYPTO",
            "24/7 market. Wider stops acceptable. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, crypto_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"C{i}", batch, prompt, False))

    # Track E — paper leveraged/short
    for i, batch in enumerate(_chunk(TRACK_E_PAPER, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "PAPER LEVERAGED/SHORT",
            "Find overextended longs (short) or breakout longs (leveraged long).",
            threat_ctx, news_ctx, regime, held_ctx, paper_rule, PAPER_SIGNAL_SCHEMA
        )
        all_batches.append((f"E{i}", batch, prompt, True))

    # Track F — futures / forex (fetch live TA)
    futures_ta_profiles = {}
    try:
        futures_syms_to_analyze = TRACK_F_FUTURES[:10]
        logger.info(f"[Signals] Fetching futures TA for {len(futures_syms_to_analyze)} symbols...")
        for fsym in futures_syms_to_analyze:
            bars_f = fetch_futures_multi_tf(fsym, ["1H", "4H", "1D"])
            valid_bars = {tf: df for tf, df in bars_f.items() if df is not None and len(df) >= 10}
            if valid_bars:
                futures_ta_profiles[fsym] = analyze_symbol(valid_bars)
        logger.info(f"[Signals] Futures TA ready: {len(futures_ta_profiles)} symbols")
    except Exception as _fe:
        logger.warning(f"[Signals] Futures TA fetch failed: {_fe}")

    for i, batch in enumerate(_chunk(TRACK_F_FUTURES[:12], FUTURES_BATCH_SIZE)):
        _fut_asset_ref = ", ".join(
            f"{s}={FUTURES_UNIVERSE.get(s,{}).get('name',s)}"
            for s in batch
        )
        prompt = make_batch_prompt(
            batch, "FUTURES/FOREX/COMMODITIES",
            f"Macro/commodity/forex setup. Symbols: {_fut_asset_ref}. Use 5x moderate/10x high/20x very high conviction.",
            threat_ctx, futures_news_ctx or news_ctx, regime, "",
            futures_rule, FUTURES_PAPER_SCHEMA, futures_profiles=futures_ta_profiles
        )
        all_batches.append((f"F{i}", batch, prompt, True))

    # Track D — opportunistic
    if opp_syms:
        for i, batch in enumerate(_chunk(opp_syms, BATCH_SIZE)):
            prompt = make_batch_prompt(
                batch, "OPPORTUNISTIC",
                f"These appeared in threat/news: {batch}. Best setup only.",
                threat_ctx, news_ctx, regime, held_ctx, bounce_rule, SIGNAL_SCHEMA
            )
            all_batches.append((f"D{i}", batch, prompt, False))

    logger.info(f"[Signals] {len(all_batches)} total batches across all tracks")

    # ── Run all batches in parallel (capped by LM Studio semaphore) ──────────
    all_raw      = []  # (signal_dict, is_paper)
    all_raw_lock = threading.Lock()

    def _run_batch(batch_id, batch_syms, prompt, is_paper):
        try:
            logger.info(f"[Signals] LLM call batch {batch_id} ({len(batch_syms)} syms)...")
            r = call_lm_studio(prompt, system=sys_p, max_tokens=TRACK_MAX_TOKENS,
                               temperature=0.15, thinking=False)
            logger.info(f"[Signals] Batch {batch_id} → {len(r)} chars")
            sigs = parse_json(r)
            results = []
            if isinstance(sigs, list):
                logger.info(f"[Signals] Batch {batch_id} → {len(sigs)} signals")
                results = [(s, is_paper) for s in sigs]
            elif isinstance(sigs, dict):
                for k in ["signals", "trades", "setups", "results"]:
                    if sigs.get(k):
                        results = [(s, is_paper) for s in sigs[k]]
                        break
            else:
                logger.warning(f"[Signals] Batch {batch_id} unexpected type {type(sigs)}: {r[:200]}")
            with all_raw_lock:
                all_raw.extend(results)
        except Exception as e:
            logger.error(f"[Signals] Batch {batch_id} FAILED: {type(e).__name__}: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="batch") as pool:
        futures_exec = {
            pool.submit(_run_batch, bid, bsyms, bprompt, bpaper): bid
            for bid, bsyms, bprompt, bpaper in all_batches
        }
        for fut in as_completed(futures_exec):
            pass  # results written to all_raw via _run_batch

    logger.info(f"[Signals] {len(all_raw)} raw signals from LLM across all batches")

    if not all_raw:
        logger.warning("[Signals] No signals generated — check LLM connection and logs above")
        log_decision("signals", "NO_OUTPUT", "No signals generated — check LLM connection", score=0, thinking=False)
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

        live_records = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"])
        ).all()
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
                        reasoning=scored.get("reasoning"),
                        key_risks=scored.get("key_risks"),
                        momentum=scored.get("momentum"),
                        signal_source=scored.get("signal_source"),
                        earnings_risk=bool(scored.get("earnings_risk", False)),
                        rr_ratio=scored.get("rr_ratio"),
                        status=target_status,
                        generated_at=now_iso,
                        paper_mode=is_paper,
                        paper_direction=scored.get("paper_direction") if is_paper else None,
                        trigger_event=f"Regime:{regime.get('label','?')}",
                        asset_class_raw=scored.get("asset_class"),
                    ))
                    logger.debug(f"[Signals] New signal: {sym} (paper={is_paper}) → {target_status}")
                    saved += 1
            except Exception as e:
                logger.error(f"[Signals] Error processing signal {raw}: {e}")
                skipped += 1

        db.commit()

    total = saved + updated
    logger.info(f"[Signals] Done v7.0 — {saved} new, {updated} updated, {skipped} skipped | regime={regime.get('label')}")
    log_decision("signals", "GENERATED",
                 f"v7.0 batch run: {total} signals ({saved} new + {updated} updated) | regime={regime.get('label')} | batches={len(all_batches)}",
                 score=total, thinking=False)
    return {"saved": saved, "updated": updated, "skipped": skipped,
            "regime": regime.get("label"), "batches": len(all_batches)}
