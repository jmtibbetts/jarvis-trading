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

v7.1 changes:
- Every equity and crypto track evaluates both bullish and bearish setups
- Short and leveraged directions are always routed to paper execution
- Short targets, stops, and composite scoring remain direction-aware
"""
import json, logging, os, re, time, uuid, threading
from datetime import datetime, timezone, timedelta
from app.routes import log_decision
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset, SignalAccuracy
from lib.lmstudio import call_lm_studio, parse_json, get_llm_config
from lib.ta_engine import TIMEFRAME_LADDER, analyze_symbol, build_ta_prompt_block
from lib.learning_engine import get_accuracy_context, get_pattern_context, get_regime_context, get_lessons_context, get_lessons_context_for_track, get_confidence_adjustment
from lib.futures_data import PAPER_FUTURES, get_futures_news_context, fetch_futures_multi_tf, FUTURES_UNIVERSE
from lib.signal_identity import signal_identity
from lib.trading_preferences import get_user_preference, horizon_for_timeframe, timeframe_allowed

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


def direction_requires_paper(direction: str | None) -> bool:
    """Return True for directions the Alpaca live-long path must never execute."""
    normalized = (direction or "").replace(" ", "_").replace("-", "_").lower()
    return (
        normalized.startswith("short")
        or "leveraged" in normalized
        or bool(re.search(r"_(?:2|5|10|20)x$", normalized))
    )


def _read_ta_from_cache(symbols: list, timeframes=None) -> dict:
    """
    Read OHLCV bars directly from the SQLite cache (no live API calls).
    fetch_market_data already keeps the cache warm — this is instant.
    """
    if timeframes is None:
        timeframes = TIMEFRAME_LADDER
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
    entry = float(s.get("entry_price") or 0)
    last_price = (
        ((ta.get("4H") or {}).get("price") or {}).get("last") or
        ((ta.get("1D") or {}).get("price") or {}).get("last") or
        asset_map.get(sym, {}).get("price") or
        entry
    )
    if not last_price:
        return s

    if not entry or abs(entry - last_price) / last_price > 0.15:
        entry = round(last_price, 4 if last_price < 1 else 2)
    s["entry_price"] = entry

    atr_pct = (((ta.get("4H") or {}).get("atr") or {}).get("pct")) or 2.0

    if is_paper and s["direction"].lower().startswith("short"):
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

    from lib.signal_levels import validate_signal_levels, clamp_stop_to_atr
    s, atr_clamped, atr_reason = clamp_stop_to_atr(s, atr_pct)
    if atr_clamped:
        logger.debug(f"[Signals] {sym} stop clamped: {atr_reason}")

    # No stop can be both economically viable and structurally sane for this
    # symbol at this volatility — the spread and fees exceed the move on
    # offer. Drop it rather than repair it into a trade that cannot pay for
    # itself; the ATR repair block below would otherwise manufacture levels
    # that look fine and lose money by construction.
    if s.get("untradeable_reason"):
        logger.info(f"[Signals] {sym} dropped — {s['untradeable_reason']}")
        return None

    levels_ok, _ = validate_signal_levels(s)
    if not levels_ok:
        distance_pct = max(0.5, min(float(atr_pct or 2.0), 10.0)) / 100.0
        precision = 6 if entry < 1 else 2
        if is_paper and s["direction"].lower().startswith("short"):
            s["stop_loss"] = round(entry * (1 + distance_pct * 1.5), precision)
            s["target_price"] = round(entry * (1 - distance_pct * 2.5), precision)
        else:
            s["stop_loss"] = round(entry * (1 - distance_pct * 1.5), precision)
            s["target_price"] = round(entry * (1 + distance_pct * 2.5), precision)
        levels_ok, _ = validate_signal_levels(s)
        if not levels_ok:
            return None
    return s


_web_headlines_cache = {"at": None, "text": ""}


def _mcp_market_headlines() -> str:
    """One MCP web search per ~30 min across ALL generation runs — fresh
    market-moving news injected into every LLM prompt. Tavily first (keyed,
    structured JSON with title+content snippets); exa fallback (keyless).
    Empty string on any failure; signal generation never depends on it."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    at = _web_headlines_cache["at"]
    if at is not None and (now - at).total_seconds() < 1800:
        return _web_headlines_cache["text"]
    _web_headlines_cache["at"] = now
    _web_headlines_cache["text"] = ""
    from lib.mcp_client import call_tool
    query = "biggest stock and crypto market moving news today"
    # Tavily returns structured JSON: title + a content snippet per result —
    # inject BOTH so the model sees the substance, not just headlines.
    try:
        import json as _json
        # NOTE: tavily's hosted MCP only accepts topic='general' (verified
        # live — 'news' fails schema validation, unlike their REST API).
        raw = call_tool("tavily", "tavily_search", {
            "query": query, "max_results": 5,
        })
        if raw:
            data = _json.loads(raw) if isinstance(raw, str) else raw
            lines = []
            for r in (data.get("results") or [])[:5]:
                title = str(r.get("title") or "").strip()[:110]
                content = str(r.get("content") or "").strip()[:220]
                if title:
                    lines.append(f"- {title}: {content}" if content else f"- {title}")
            if lines:
                _web_headlines_cache["text"] = "\n".join(lines)[:1400]
                return _web_headlines_cache["text"]
    except Exception as e:
        logger.debug(f"[Signals] Tavily headlines unavailable: {e}")
    try:
        raw = call_tool("exa", "web_search_exa", {"query": query, "numResults": 4})
        if raw:
            titles = [ln.split(":", 1)[1].strip() for ln in raw.splitlines()
                      if ln.lower().startswith("title:")][:4]
            if not titles:
                titles = [ln.strip() for ln in raw.splitlines() if ln.strip()][:3]
            _web_headlines_cache["text"] = " | ".join(t[:80] for t in titles)
    except Exception as e:
        logger.debug(f"[Signals] MCP headlines unavailable: {e}")
    return _web_headlines_cache["text"]


_postmortem_cache = {"loaded_at": None, "rows": []}


def _recent_postmortems():
    """Loaded once per ~10 min across a generation run — the failure memory
    is small and changes slowly; per-signal DB reads would be waste."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    loaded = _postmortem_cache["loaded_at"]
    if loaded is None or (now - loaded).total_seconds() > 600:
        try:
            from lib.postmortem import load_recent_postmortems
            _postmortem_cache["rows"] = load_recent_postmortems()
        except Exception as e:
            logger.debug(f"[Signals] Postmortem load failed: {e}")
            _postmortem_cache["rows"] = []
        _postmortem_cache["loaded_at"] = now
    return _postmortem_cache["rows"]


def score_safe(signal, ta_profiles, regime, earnings_set, accuracy_map=None, news_confidence=50.0):
    try:
        from lib.signal_scorer import score_signal
        from lib.postmortem import get_failure_adjustment
        sym = signal.get("asset_symbol", "")
        failure_adj = get_failure_adjustment(sym, signal.get("setup_type"), _recent_postmortems())
        return score_signal(signal, ta_profiles.get(sym, {}), regime,
                            earnings_risk=sym.replace("/USD", "") in earnings_set,
                            historical=(accuracy_map or {}).get(sym),
                            news_confidence=news_confidence,
                            failure_adjustment=failure_adj)
    except Exception as e:
        logger.warning(f"[Signals] Scorer failed for {signal.get('asset_symbol')}: {e} — failing closed")
        # Fail toward NOT trading: a scorer exception means we couldn't verify data
        # quality/freshness/confluence, so this signal should not silently clear the
        # composite-score and quality/freshness gates in execute_signals.py.
        signal["composite_score"] = 0.0
        signal["data_quality_score"] = 0.0
        signal["freshness_score"] = 0.0
        return signal


def build_ta_fallback_signals(symbols, ta_profiles, asset_map, trade_mode="all", is_paper=False,
                                reason="Local LLM unavailable"):
    """Build conservative signals from cached TA when an LLM batch didn't run.
    `reason` should describe WHY the LLM wasn't used for this batch — the LLM
    being genuinely down is only one of several causes (time-budget cutoff,
    an unparseable response, a batch never selected this cycle); the caller
    knows which one applies and should pass an accurate reason so the signal's
    key_risks field doesn't claim the LLM is down when it isn't."""
    timeframe_sets = {
        "scalp": ["1m", "3m", "5m", "15m"],
        "longer": ["30m", "1H", "2H", "4H", "1D"],
        "all": list(TIMEFRAME_LADDER),
    }
    allowed = timeframe_sets.get(trade_mode, timeframe_sets["all"])
    results = []

    for sym in symbols:
        profile = ta_profiles.get(sym) or {}
        valid = [
            (tf, profile.get(tf)) for tf in allowed
            if profile.get(tf) and not profile[tf].get("error")
        ]
        if len(valid) < 2:
            continue

        bullish = sum(1 for _, data in valid if data.get("bias") == "bullish")
        bearish = sum(1 for _, data in valid if data.get("bias") == "bearish")
        dominant = max(bullish, bearish)
        if dominant < 2 or dominant / len(valid) < 0.5 or bullish == bearish:
            continue

        direction = "Long" if bullish > bearish else "Short"
        if is_paper and direction == "Long" and sym not in FUTURES_UNIVERSE:
            continue
        expected_bias = "bullish" if direction == "Long" else "bearish"
        aligned = [(tf, data) for tf, data in valid if data.get("bias") == expected_bias]
        if not aligned:
            continue

        def evidence(item):
            _, data = item
            trend_pct = float((data.get("trend") or {}).get("pct") or 50)
            strength = trend_pct if direction == "Long" else 100 - trend_pct
            return strength + (8 if (data.get("volume") or {}).get("surge") else 0)

        timeframe, selected = max(aligned, key=evidence)
        entry = float((selected.get("price") or {}).get("last") or 0)
        if entry <= 0:
            entry = float((asset_map.get(sym) or {}).get("price") or 0)
        if entry <= 0:
            continue

        atr_pct = max(0.5, min(float((selected.get("atr") or {}).get("pct") or 2.0), 8.0))
        precision = 6 if entry < 1 else 2
        if direction == "Short":
            stop = round(entry * (1 + atr_pct * 1.5 / 100), precision)
            target = round(entry * (1 - atr_pct * 2.5 / 100), precision)
        else:
            stop = round(entry * (1 - atr_pct * 1.5 / 100), precision)
            target = round(entry * (1 + atr_pct * 2.5 / 100), precision)

        agreement = dominant / len(valid)
        trend_pct = float((selected.get("trend") or {}).get("pct") or 50)
        strength = trend_pct if direction == "Long" else 100 - trend_pct
        confidence = round(min(88, 58 + agreement * 22 + max(0, strength - 50) * 0.2))
        macd = (selected.get("macd") or {}).get("trend") or "unknown"
        rsi = selected.get("rsi")

        if sym in FUTURES_UNIVERSE:
            category = FUTURES_UNIVERSE[sym].get("category")
            asset_class = "Forex" if category == "Forex" else "Futures"
            asset_name = FUTURES_UNIVERSE[sym].get("name") or sym
        elif _is_crypto_signal(sym):
            asset_class = "Crypto"
            asset_name = (asset_map.get(sym) or {}).get("name") or sym.split("/")[0]
        else:
            asset_class = "Equity"
            asset_name = (asset_map.get(sym) or {}).get("name") or sym

        results.append({
            "asset_symbol": sym,
            "asset_name": asset_name,
            "asset_class": asset_class,
            "direction": direction,
            "confidence": confidence,
            "timeframe": timeframe,
            "entry_price": entry,
            "target_price": target,
            "stop_loss": stop,
            "reasoning": (
                f"TA fallback: {dominant}/{len(valid)} timeframes {expected_bias}; "
                f"{timeframe} trend={strength:.0f}%, RSI={rsi}, MACD={macd}."
            ),
            "key_risks": f"{reason}; deterministic TA only. Invalidate at stop or bias reversal.",
            "momentum": "Bullish" if direction == "Long" else "Bearish",
            "signal_source": "ta_fallback",
            "setup_type": "scalp" if timeframe in {"1m", "3m", "5m"} else "swing",
        })

    return results


def _news_confidence_for_symbol(symbol: str, news: list[dict]) -> float:
    base = (symbol or "").upper().replace("-USD", "").split("/")[0]
    relevant = []
    for item in news:
        assets = {str(asset).upper().replace("-USD", "").split("/")[0] for asset in item.get("affected_assets", [])}
        if base in assets:
            relevant.append(float(item.get("claim_confidence") or 50))
    return max(relevant, default=50.0)


# ── TA block builder — TA only, no per-symbol learning context ────────────────
def _is_crypto_signal(symbol: str, asset_class: str = "") -> bool:
    cls = (asset_class or "").strip().lower()
    if cls == "crypto":
        return True
    sym = (symbol or "").upper().strip()
    if not sym or sym.endswith(("=F", "=X")):
        return False
    if "/" in sym or sym.endswith("-USD"):
        return True
    crypto_bases = CRYPTO_BASES | {"BTC", "ETH"}
    return sym.endswith("USD") and sym[:-3] in crypto_bases


def _target_status_for_signal(signal: dict, is_paper: bool, market_open: bool) -> str:
    asset_class = signal.get("asset_class") or ""
    if is_paper:
        return "Active"
    if _is_crypto_signal(signal.get("asset_symbol"), asset_class):
        return "Active"
    return "Active" if market_open else "PendingApproval"


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
        f"Task: {task_hint} In reasoning, cite timeframe-specific RSI, MACD, EMA, VWAP, volume, volatility, support/resistance, and any conflicting timeframe. {rule}"
        f"Return ONLY the JSON array starting with '[' and ending with ']'.\n"
        f"Schema: {schema}"
    )
    tok_est = len(prompt) // 4
    logger.info(f"[Signals] Batch '{track_label}' {batch_syms}: ~{tok_est} tok in | {TRACK_MAX_TOKENS} max out")
    return prompt


def select_llm_batches(all_batches, max_batches):
    """Choose a bounded, round-robin sample so every asset track gets review time."""
    max_batches = max(0, int(max_batches))
    if max_batches <= 0 or max_batches >= len(all_batches):
        max_batches = len(all_batches)

    track_order = ("A", "B", "C", "E", "F", "D")
    grouped = {track: [] for track in track_order}
    for batch in all_batches:
        grouped.setdefault(batch[0][:1], []).append(batch)

    selected = []
    while len(selected) < max_batches:
        added = False
        for track in track_order:
            if grouped.get(track):
                selected.append(grouped[track].pop(0))
                added = True
                if len(selected) >= max_batches:
                    break
        if not added:
            break
    return selected


FOCUS_MIN_SCORE = 75.0   # focus setups must be genuinely strong, not merely valid
FOCUS_MAX_SYMBOLS = 5    # a focus list is small by definition


def focus_symbols() -> list:
    """The tiny "coins to watch" set, watched patiently and continuously.

    Different from the watchlist in three ways, all deliberate:
      - analysed EVERY cycle and placed first, exempt from the batch cap,
        so a focus name is never skipped because the budget ran out;
      - given the full timeframe ladder rather than the standard slice;
      - held to FOCUS_MIN_SCORE before a signal is emitted at all, so the
        output is "this setup is ready" rather than "here is today's best
        guess". Silence on a focus symbol is a real answer.
    """
    from app.database import get_db, MarketAsset
    try:
        with get_db() as db:
            rows = db.query(MarketAsset).filter(MarketAsset.is_focus == True).all()  # noqa: E712
            syms = [(r.symbol or "").upper().strip() for r in rows if r.symbol and float(r.price or 0) > 0]
    except Exception as e:
        logger.warning(f"[Signals] Focus list load failed: {e}")
        return []
    if syms:
        logger.info(f"[Signals] FOCUS watch ({len(syms)}): {', '.join(syms)} "
                    f"— every cycle, full ladder, only emits at score >= {FOCUS_MIN_SCORE:.0f}")
    return syms[:FOCUS_MAX_SYMBOLS]


def watchlist_symbols(limit: int = 25) -> list:
    """Symbols the OPERATOR tracks, which the hardcoded tracks never covered.

    TRACK_A/B/C are fixed lists written into this file; the watchlist in the
    database was never consulted, so 246 of 307 tracked assets produced no
    signals at all. Anything the user deliberately added is at least as
    interesting as a hardcoded ticker, so these are analysed FIRST and the
    standard universe runs afterwards with whatever budget remains.

    Ordered by absolute 24h move: a symbol swinging hard is where setups
    actually exist, and it keeps the batch budget on live names rather than
    stablecoins parked at 1.00.
    """
    from app.database import get_db, MarketAsset
    fixed = set(TRACK_A) | set(TRACK_B) | set(TRACK_C) | set(TRACK_E_PAPER)
    fixed |= set(focus_symbols())   # focus names have their own dedicated track
    rows = []
    try:
        with get_db() as db:
            for a in db.query(MarketAsset).all():
                sym = (a.symbol or "").upper().strip()
                if not sym or sym in fixed:
                    continue
                price = float(a.price or 0)
                if price <= 0:
                    continue
                # Stablecoins never produce a directional setup worth an LLM call.
                base = sym.split("/")[0]
                if base in {"USDT", "USDC", "USD1", "USDG", "DAI", "FDUSD", "TUSD", "PYUSD"}:
                    continue
                move = abs(float(getattr(a, "change_percent", 0) or 0))
                rows.append((move, sym))
    except Exception as e:
        logger.warning(f"[Signals] Watchlist load failed: {e}")
        return []
    rows.sort(reverse=True)
    picked = [sym for _, sym in rows[:limit]]
    if picked:
        logger.info(f"[Signals] Watchlist track: {len(picked)} operator-tracked symbols "
                    f"(top movers first) — {', '.join(picked[:6])}...")
    return picked


def run():
    global ta_profiles_global, asset_map_global

    logger.info("[Signals] Starting signal generation v7.0 (batch architecture)...")
    preference = get_user_preference()
    trade_mode = preference["trade_mode"]
    horizon_rule = {
        "scalp": "Generate only scalp setups using 1m, 3m, 5m, or 15m as the selected timeframe.",
        "longer": "Generate only longer-duration setups using 30m, 1H, 2H, 4H, or 1D.",
        "all": "Generate the strongest setup at any supported timeframe.",
    }[trade_mode]
    logger.info("[Signals] User trade mode: %s", trade_mode)

    cfg = {}
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
             "claim_confidence": getattr(n, "claim_confidence", None),
             "confirmation_status": getattr(n, "confirmation_status", None),
             "affected_assets": n.affected_assets.split(",") if n.affected_assets else []}
            for n in db.query(NewsItem).order_by(NewsItem.published_at.desc()).limit(30).all()
        ]
        asset_map = {a.symbol: {"name": a.name, "price": a.price}
                     for a in db.query(MarketAsset).all()}
        accuracy_map = {
            row.symbol: {
                "total_trades": row.total_trades or 0,
                "wins": row.wins or 0,
                "losses": row.losses or 0,
                "win_rate": row.win_rate or 0,
            }
            for row in db.query(SignalAccuracy).all()
        }

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

    # Fresh web headlines via MCP — ONE bounded search per generation run
    # (cached 30 min in-process), so up-to-the-hour market context reaches
    # the signal LLM without per-signal web calls. Keyless exa first; the
    # block is prefixed as EXTERNAL reporting so the model treats it as
    # context, not verified system data.
    web_ctx = _mcp_market_headlines()
    if web_ctx:
        news_ctx = (news_ctx + "\n\nFRESH WEB NEWS (unverified, live search — weigh against TA):\n" + web_ctx)

    sys_p = "You are an expert quantitative trader. Output only valid JSON arrays. No commentary, no markdown — start with '[' end with ']'."
    directional_rule = horizon_rule + " " + (
        " Evaluate bullish and bearish TA equally. direction='Long', 'Bounce', or 'Short'. "
        "Long/Bounce: stop BELOW entry and target ABOVE. Short: stop ABOVE entry and target BELOW. "
        "R:R>=2. Return only the strongest 1-2 setups; do not force a long when bearish evidence is stronger.\n"
    )
    paper_rule  = horizon_rule + " direction='Short','Short_Leveraged', or 'Long_Leveraged'. Short: stop ABOVE entry, target BELOW. LongLev: stop BELOW, target ABOVE. R:R>=2. Generate 1-2 best signals only.\n"
    futures_rule = horizon_rule + " direction: Long/Long_5x/Long_10x/Long_20x/Short/Short_5x/Short_10x/Short_20x. asset_class='Futures' or 'Forex'. Generate 1-2 best signals only.\n"

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

    # Track FOCUS — "coins to watch": first, always, and held to a higher bar.
    focus = focus_symbols()
    _focus_set = set(focus)
    for i, batch in enumerate(_chunk(focus, BATCH_SIZE)):
        focus_rule = (
            " These symbols are under CONTINUOUS focused watch. Do not force a setup: "
            "return an EMPTY array unless the confluence is genuinely strong, because "
            "silence is an acceptable and expected answer here. When you do return one, "
            "it must be a high-conviction setup with confidence >= 80. "
            "Evaluate bullish and bearish equally. direction='Long', 'Bounce', or 'Short'. "
            "Long/Bounce: stop BELOW entry, target ABOVE. Short: stop ABOVE entry, target BELOW. "
            "Size stops from ATR - these are volatile names. R:R>=2.\n"
        )
        # Each focus symbol carries an accumulated behavioural profile:
        # measured volatility/range/swing statistics plus a written sketch of
        # how it trades. Especially valuable for newly listed names that have
        # no win-rate history but plenty of observable behaviour.
        profiles = []
        for fsym in batch:
            try:
                from lib.focus_profile import profile_prompt_block
                blk = profile_prompt_block(fsym)
                if blk:
                    profiles.append(blk)
            except Exception as e:
                logger.debug(f"[Signals] Focus profile unavailable for {fsym}: {e}")
        focus_ctx = ("\n\n" + "\n".join(profiles)) if profiles else ""
        prompt = make_batch_prompt(
            batch, "FOCUS WATCH",
            "Continuously monitored focus list. Patience is correct: only a strong, "
            "ready setup should produce a signal. Analyze TA for each symbol." + focus_ctx,
            threat_ctx, news_ctx, regime, held_ctx, focus_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"F{i}", batch, prompt, False))

    # Track W — the operator's own watchlist, analysed BEFORE the fixed
    # tracks so their budget is never consumed by hardcoded tickers.
    wl = watchlist_symbols()
    wl_crypto = [s for s in wl if "/" in s]
    wl_equity = [s for s in wl if "/" not in s]
    for i, batch in enumerate(_chunk(wl_crypto, BATCH_SIZE)):
        crypto_rule_wl = (
            " Evaluate bullish and bearish TA equally. direction='Long', 'Bounce', or 'Short'. "
            "Long/Bounce: stop BELOW entry and target ABOVE. Short: stop ABOVE entry and target BELOW. "
            "These are high-volatility names: size stops from ATR, not a fixed percentage. "
            "R:R>=2. Return only the strongest 1-2 setups.\n"
        )
        prompt = make_batch_prompt(
            batch, "WATCHLIST/CRYPTO",
            "Operator-tracked crypto, 24/7. Often high volatility. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, crypto_rule_wl, SIGNAL_SCHEMA
        )
        all_batches.append((f"W{i}", batch, prompt, False))
    for i, batch in enumerate(_chunk(wl_equity, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "WATCHLIST/EQUITY",
            "Operator-tracked equities. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, directional_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"WE{i}", batch, prompt, False))

    # Track A — macro / defense / energy / commodities
    for i, batch in enumerate(_chunk(TRACK_A, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "MACRO/GEO/COMMODITIES",
            "Defense/energy/commodity/rates setup. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, directional_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"A{i}", batch, prompt, False))

    # Track B — tech / AI / growth
    for i, batch in enumerate(_chunk(TRACK_B, BATCH_SIZE)):
        prompt = make_batch_prompt(
            batch, "TECH/AI/GROWTH",
            "AI/semi/software/high-beta setup. Analyze TA for each symbol.",
            threat_ctx, news_ctx, regime, held_ctx, directional_rule, SIGNAL_SCHEMA
        )
        all_batches.append((f"B{i}", batch, prompt, False))

    # Track C — crypto
    for i, batch in enumerate(_chunk(TRACK_C, BATCH_SIZE)):
        crypto_rule = (
            " Evaluate bullish and bearish TA equally. direction='Long', 'Bounce', or 'Short'. "
            "Long/Bounce: stop BELOW entry and target ABOVE. Short: stop ABOVE entry and target BELOW. "
            "Wider ATR-based stops are acceptable. R:R>=2. Return only the strongest 1-2 setups.\n"
        )
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
        # Live interbank FX rates for the forex symbols in this batch —
        # fresher than the (delayed) bar data the TA profiles come from.
        fx_ctx = ""
        try:
            from lib.allrates_data import fx_summary_block
            fx_lines = [b for b in (fx_summary_block(s) for s in batch) if b]
            if fx_lines:
                fx_ctx = "\n\n" + "\n".join(fx_lines)
        except Exception as _fx_e:
            logger.debug(f"[Signals] FX context unavailable: {_fx_e}")
        prompt = make_batch_prompt(
            batch, "FUTURES/FOREX/COMMODITIES",
            f"Macro/commodity/forex setup. Symbols: {_fut_asset_ref}. Use 5x moderate/10x high/20x very high conviction.",
            threat_ctx, (futures_news_ctx or news_ctx) + fx_ctx, regime, "",
            futures_rule, FUTURES_PAPER_SCHEMA, futures_profiles=futures_ta_profiles
        )
        all_batches.append((f"F{i}", batch, prompt, True))

    # Track D — opportunistic
    if opp_syms:
        for i, batch in enumerate(_chunk(opp_syms, BATCH_SIZE)):
            prompt = make_batch_prompt(
                batch, "OPPORTUNISTIC",
                f"These appeared in threat/news: {batch}. Best setup only.",
                threat_ctx, news_ctx, regime, held_ctx, directional_rule, SIGNAL_SCHEMA
            )
            all_batches.append((f"D{i}", batch, prompt, False))

    logger.info(f"[Signals] {len(all_batches)} total batches across all tracks")

    # ── Run all batches in parallel (capped by LM Studio semaphore) ──────────
    all_raw      = []  # (signal_dict, is_paper)
    all_raw_lock = threading.Lock()
    fallback_profiles = {**ta_profiles, **futures_ta_profiles}

    def _append_fallback(batch_id, batch_syms, is_paper, reason="Local LLM unavailable"):
        fallback = build_ta_fallback_signals(
            batch_syms, fallback_profiles, asset_map, trade_mode, is_paper=is_paper,
            reason=reason,
        )
        if fallback:
            with all_raw_lock:
                all_raw.extend((signal, is_paper) for signal in fallback)
            logger.warning(
                f"[Signals] Batch {batch_id} used TA fallback ({reason}) -> {len(fallback)} signals"
            )
        return len(fallback)

    # 90s was too tight for local "thinking" models (Qwen3 etc. burn tokens reasoning
    # before answering) — most batches were hitting the fallback path well before the
    # LLM was actually unavailable. 240s still leaves headroom inside the 30-min cycle.
    llm_budget = max(10.0, float(os.getenv("SIGNAL_LLM_TIME_BUDGET_SECONDS", "420")))
    # 30s was tuned for a 9B model; the user runs larger local models
    # (observed live: a 26B swap made every request time out, tripping the
    # circuit breaker so ALL batches instantly fell back to TA — 628
    # "RuntimeError" fallbacks in one log). 90s absorbs big-model latency.
    llm_request_timeout = max(5.0, float(os.getenv("SIGNAL_LLM_REQUEST_TIMEOUT_SECONDS", "90")))
    llm_queue_timeout = max(1.0, float(os.getenv("SIGNAL_LLM_QUEUE_TIMEOUT_SECONDS", "45")))
    llm_deadline = time.monotonic() + llm_budget

    def _run_batch(batch_id, batch_syms, prompt, is_paper):
        try:
            remaining = llm_deadline - time.monotonic()
            if remaining <= 1.0:
                logger.warning(f"[Signals] Batch {batch_id} skipped after LLM time budget expired")
                _append_fallback(
                    batch_id, batch_syms, is_paper,
                    reason=f"LLM time budget ({llm_budget:.0f}s) exhausted before this batch ran",
                )
                return
            # One provider failure trips a 15s circuit cooldown; without this
            # wait, every remaining batch in the run failed INSTANTLY inside
            # that window (observed live: 8 batches dead in the same second).
            # A batch that can afford it sleeps the cooldown out instead.
            from lib.lmstudio import get_llm_cooldown
            cooldown = get_llm_cooldown()
            if cooldown > 0 and (llm_deadline - time.monotonic()) > cooldown + 5:
                logger.info(f"[Signals] Batch {batch_id} waiting out LLM cooldown ({cooldown:.1f}s)")
                time.sleep(cooldown + 0.5)
            logger.info(f"[Signals] LLM call batch {batch_id} ({len(batch_syms)} syms)...")
            r = call_lm_studio(prompt, system=sys_p, max_tokens=TRACK_MAX_TOKENS,
                               temperature=0.15, thinking=False,
                               queue_timeout=min(llm_queue_timeout, remaining),
                               request_timeout=min(llm_request_timeout, remaining))
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
            if results:
                with all_raw_lock:
                    all_raw.extend(results)
            else:
                _append_fallback(
                    batch_id, batch_syms, is_paper,
                    reason="LLM response was empty or unparseable for this batch",
                )
        except Exception as e:
            logger.error(f"[Signals] Batch {batch_id} FAILED: {type(e).__name__}: {e}")
            is_conn_err = "running at" in str(e) or "LLM call failed" in str(e)
            reason = (
                f"Local LLM unreachable ({e})" if is_conn_err
                else f"LLM call failed for this batch ({type(e).__name__}: {str(e)[:80]})"
            )
            _append_fallback(batch_id, batch_syms, is_paper, reason=reason)

    # ── Watchlist priority WITHOUT starving discovery ────────────────────
    # The LLM time budget is fixed, and batches that miss the deadline fall
    # back to TA. Simply front-loading the watchlist would therefore push the
    # discovery tracks (A/B/C, opportunistic scanner, futures) past the
    # deadline on a slow model — trading one blind spot for another.
    # Interleaving gives the watchlist the FIRST slot of every pair while
    # guaranteeing discovery batches run early enough to matter.
    _focus = [b for b in all_batches if b[0].startswith("F") and not b[0].startswith("FUT")]
    _wl = [b for b in all_batches if b[0].startswith("W")]
    _rest = [b for b in all_batches if b not in _focus and not b[0].startswith("W")]
    if _wl and _rest:
        interleaved = []
        wi = ri = 0
        while wi < len(_wl) or ri < len(_rest):
            if wi < len(_wl):
                interleaved.append(_wl[wi]); wi += 1
            if ri < len(_rest):
                interleaved.append(_rest[ri]); ri += 1
        all_batches = _focus + interleaved   # focus always leads
        logger.info(
            f"[Signals] Batch order interleaved: {len(_wl)} watchlist + {len(_rest)} discovery "
            f"— watchlist first in each pair, neither track starves the other"
        )

    max_llm_batches = int(os.getenv("SIGNAL_LLM_MAX_BATCHES", "0"))
    llm_workers = max(1, min(4, int(os.getenv("SIGNAL_LLM_WORKERS", "1"))))
    selected_batches = select_llm_batches(all_batches, max_llm_batches)
    selected_ids = {batch[0] for batch in selected_batches}
    logger.info(
        f"[Signals] Local LLM review: {len(selected_batches)}/{len(all_batches)} batches, "
        f"workers={llm_workers}, budget={llm_budget:.0f}s"
    )

    for bid, bsyms, _, bpaper in all_batches:
        if bid not in selected_ids:
            _append_fallback(
                bid, bsyms, bpaper,
                reason=f"Batch not selected this cycle (SIGNAL_LLM_MAX_BATCHES cap: {max_llm_batches})",
            )

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=llm_workers, thread_name_prefix="batch") as pool:
        futures_exec = {
            pool.submit(_run_batch, bid, bsyms, bprompt, bpaper): bid
            for bid, bsyms, bprompt, bpaper in selected_batches
        }
        for fut in as_completed(futures_exec):
            fut.result()

    deduped_raw = {}
    for signal, is_paper in all_raw:
        key = signal_identity(signal.get("asset_symbol"), signal.get("direction"))
        deduped_raw[key] = (signal, is_paper)
    all_raw = list(deduped_raw.values())

    logger.info(f"[Signals] {len(all_raw)} raw signals after local LLM review and TA fallback")

    if not all_raw:
        emergency = build_ta_fallback_signals(
            all_syms_dedup, fallback_profiles, asset_map, trade_mode, is_paper=False,
            reason="Local LLM produced no usable signals this cycle",
        )
        all_raw.extend(
            (signal, direction_requires_paper(signal.get("direction")))
            for signal in emergency
        )
        logger.warning(
            f"[Signals] LLM produced no output; emergency TA fallback created {len(emergency)} candidates"
        )

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
            k = signal_identity(rec.asset_symbol, rec.direction, getattr(rec, "user_id", "local"))
            existing_map[k] = rec

        for raw, batch_is_paper in all_raw:
            try:
                is_paper = bool(batch_is_paper or direction_requires_paper(raw.get("direction")))
                n = normalize_signal(raw, ta_profiles, asset_map, is_paper=is_paper)
                if not n:
                    skipped += 1
                    continue
                sym = n.get("asset_symbol")
                scored = score_safe(
                    n, ta_profiles, regime, earnings_set, accuracy_map,
                    _news_confidence_for_symbol(sym, news),
                )
                if not timeframe_allowed(scored.get("timeframe"), trade_mode):
                    skipped += 1
                    continue
                scored["trade_horizon"] = horizon_for_timeframe(scored.get("timeframe"))

                # Postmortem-driven floor: 226 signals expired in one 48h window
                # and NONE had ever reached a broker order — most sat below or
                # near the execution gate (55) and could never fire. Signals
                # scoring under MIN_PERSIST_SCORE are noise that only churns
                # Expired rows and pollutes the failure stats; drop them here
                # instead of letting them die slowly. Floor sits well under
                # both the live gate (55) and paper auto-trade behavior.
                MIN_PERSIST_SCORE = 45.0
                score_now = float(scored.get("composite_score") or 0)
                if score_now < MIN_PERSIST_SCORE:
                    skipped += 1
                    continue

                # Focus names are held to a much higher bar, enforced HERE in
                # code rather than trusted to the prompt: the instruction to
                # stay silent is guidance, this is the guarantee. A focus
                # symbol producing nothing is the system working, not failing.
                if sym in _focus_set and score_now < FOCUS_MIN_SCORE:
                    logger.info(
                        f"[Signals] FOCUS {sym}: setup scored {score_now:.0f}, below the "
                        f"{FOCUS_MIN_SCORE:.0f} focus bar — holding watch, no signal"
                    )
                    skipped += 1
                    continue
                if sym in _focus_set:
                    scored["signal_source"] = "focus"

                target_status = _target_status_for_signal(scored, is_paper, market_open)

                rec_key = signal_identity(sym, scored.get("direction"))
                prior = existing_map.get(rec_key)
                if prior and prior.status in ("Active", "PendingApproval"):
                    # COMPARE before superseding: a new signal only replaces a
                    # live one for the same symbol+direction when it's at least
                    # as strong (small tolerance) — otherwise the fresh-but-
                    # weaker take is dropped and the standing signal survives
                    # with a refreshed updated_date. Prevents score churn where
                    # each run replaces a good setup with a mediocre rescoring.
                    prior_score = float(prior.composite_score or 0)
                    new_score = float(scored.get("composite_score") or 0)
                    if new_score < prior_score - 5.0:
                        prior.updated_date = now_iso
                        logger.debug(
                            f"[Signals] Kept standing {sym} {scored.get('direction')} "
                            f"({prior_score:.0f} > new {new_score:.0f}) — new take dropped"
                        )
                        skipped += 1
                        continue
                    prior.status = "Superseded"
                    prior.updated_date = now_iso
                    updated += 1

                new_rec = TradingSignal(
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
                        calibrated_confidence=scored.get("calibrated_confidence"),
                        score_breakdown=json.dumps(scored.get("score_breakdown", {}), sort_keys=True),
                        data_quality_score=scored.get("data_quality_score"),
                        freshness_score=scored.get("freshness_score"),
                        news_confidence=scored.get("news_confidence"),
                        setup_type=scored.get("setup_type"),
                        invalidation=scored.get("invalidation"),
                        signal_version=scored.get("signal_version", "v7.2"),
                        market_data_at=scored.get("market_data_at"),
                        expires_at=scored.get("expires_at"),
                        trade_horizon=scored.get("trade_horizon"),
                        status=target_status,
                        generated_at=now_iso,
                        paper_mode=is_paper,
                        paper_direction=scored.get("paper_direction") if is_paper else None,
                        trigger_event=f"Regime:{regime.get('label','?')}",
                )
                db.add(new_rec)
                existing_map[rec_key] = new_rec
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
    if saved:
        try:
            from app.ws import manager as ws_manager
            ws_manager.broadcast_from_thread("new_signals", {"count": saved, "regime": regime.get("label")})
        except Exception:
            pass
    return {"saved": saved, "updated": updated, "skipped": skipped,
            "regime": regime.get("label"), "batches": len(all_batches)}
