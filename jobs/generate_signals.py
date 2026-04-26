"""
Job: Generate Trading Signals
Runs every 30 minutes. 4 parallel LLM tracks: Macro/Geo, Tech/AI, Crypto, Opportunistic.
"""
import logging, re, uuid
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset
from lib.lmstudio import call_lm_studio, parse_json
from lib.ohlcv import fetch_batch
from lib.ta_engine import analyze_symbol, build_ta_prompt_block

logger = logging.getLogger(__name__)

# ── Watchlists ─────────────────────────────────────────────────────────────────
TRACK_A = ['RTX','LMT','NOC','GD','BA','XOM','CVX','COP','FANG','CEG',
           'GLD','SLV','TLT','SPY','IWM','USO','UNG','GDX','GDXJ']
TRACK_B = ['NVDA','AMD','MSFT','GOOGL','AAPL','META','AMZN','AVGO','TSM',
           'ANET','INTC','QCOM','SMCI','VRT','SOXX','QQQ','CRWV','NBIS',
           'PLTR','TSLA','COIN','MSTR','ARM','HOOD']
TRACK_C = ['BTC/USD','ETH/USD','SOL/USD','XRP/USD','BNB/USD','AVAX/USD',
           'LINK/USD','DOGE/USD','ADA/USD','AAVE/USD','DOT/USD','ATOM/USD',
           'SUI/USD','RENDER/USD','INJ/USD','NEAR/USD','OP/USD','ARB/USD']

ALL_SYMBOLS = list(dict.fromkeys(TRACK_A + TRACK_B + TRACK_C))

COMMON_TICKERS = {
    'AAPL','MSFT','GOOGL','AMZN','META','NVDA','TSLA','AMD','INTC','QCOM',
    'AVGO','TSM','ARM','SMCI','PLTR','COIN','MSTR','HOOD','RBLX','SNAP',
    'UBER','ABNB','SQ','PYPL','SHOP','NET','CRWD','PANW','ZS','DDOG',
    'SNOW','MDB','AI','SOUN','IONQ','RXRX','ACHR','JOBY','RKLB','ASTS',
    'XOM','CVX','COP','OXY','SLB','HAL','RTX','LMT','NOC','GD','BA',
    'GLD','SLV','GDX','GDXJ','USO','UNG','SPY','QQQ','IWM','DIA',
    'XLK','XLF','XLE','XLV','TLT','IEF','HYG','JPM','BAC','GS',
    'BTC','ETH','SOL','XRP','BNB','AVAX','LINK','DOGE','ADA','AAVE',
    'DOT','ATOM','SUI','RENDER','INJ','NEAR','OP','ARB','MATIC','UNI',
}

CRYPTO_SIGNAL_BASES = {
    'SOL','XRP','BNB','AVAX','LINK','DOGE','ADA','AAVE','DOT','ATOM',
    'SUI','RENDER','INJ','NEAR','OP','ARB','MATIC','UNI','PEPE','LTC',
}

SIGNAL_SCHEMA = '''[{
  "asset_symbol": "NVDA",
  "asset_name": "NVIDIA Corporation",
  "asset_class": "Equity",
  "direction": "Long",
  "confidence": 78,
  "timeframe": "4H",
  "entry_price": 875.00,
  "target_price": 920.00,
  "stop_loss": 850.00,
  "reasoning": "Detailed reasoning referencing actual TA numbers",
  "key_risks": "Key risk factors",
  "momentum": "Bullish"
}]'''

def extract_opportunistic(threats, news, fixed_symbols):
    """Extract tickers from news/threats not already in watchlist."""
    fixed = {s.replace('/USD','') for s in fixed_symbols}
    found = {}
    texts = [
        *[f"{t.get('title','')} {t.get('description','')}" for t in threats[:20]],
        *[f"{n.get('title','')} {n.get('summary','')} {' '.join(n.get('affected_assets',[]))}" for n in news[:30]],
    ]
    for text in texts:
        for m in re.finditer(r'\$([A-Z]{1,5})|\b([A-Z]{2,5})\b', text):
            ticker = (m.group(1) or m.group(2) or '').upper()
            if ticker and ticker in COMMON_TICKERS and ticker not in fixed:
                found[ticker] = found.get(ticker, 0) + 1
    return [
        {'symbol': f"{t}/USD" if t in CRYPTO_SIGNAL_BASES else t,
         'is_crypto': t in CRYPTO_SIGNAL_BASES}
        for t, cnt in sorted(found.items(), key=lambda x: -x[1])
        if cnt >= 1
    ][:15]


def normalize_signal(s: dict, ta_profiles: dict, asset_map: dict) -> dict | None:
    """Validate and normalize a raw LLM signal dict."""
    # Resolve symbol
    sym = s.get('asset_symbol') or s.get('symbol') or s.get('ticker')
    if not sym:
        return None
    sym = sym.upper().strip()
    
    # Crypto normalization
    if sym in CRYPTO_SIGNAL_BASES:
        sym = f"{sym}/USD"
        s['asset_class'] = 'Crypto'
    if '/' in sym and not s.get('asset_class'):
        s['asset_class'] = 'Crypto'
    
    s['asset_symbol'] = sym
    s['asset_name'] = s.get('asset_name') or sym
    
    # Direction
    direction = (s.get('direction') or 'Long').capitalize()
    if direction not in ('Long', 'Bounce'):
        direction = 'Bounce' if direction == 'Short' else 'Long'
    s['direction'] = direction
    
    # Get current price from TA or asset map
    ta = ta_profiles.get(sym, {})
    last_price = (ta.get('4H', {}) or {}).get('price', {}).get('last') or \
                 (ta.get('1D', {}) or {}).get('price', {}).get('last') or \
                 asset_map.get(sym, {}).get('price')
    
    if not last_price:
        return s  # keep as-is if no price data
    
    # Snap entry price
    entry = float(s.get('entry_price') or 0)
    if not entry or abs(entry - last_price) / last_price > 0.15:
        entry = round(last_price, 4 if last_price < 1 else 2)
    s['entry_price'] = entry
    
    # ATR-based fallback for stop/target
    atr_pct = (ta.get('4H', {}) or {}).get('atr', {})
    atr_pct = (atr_pct.get('pct') if isinstance(atr_pct, dict) else None) or 2.0
    
    stop = float(s.get('stop_loss') or 0)
    if not stop or stop >= entry:
        stop = round(entry * (1 - max(atr_pct, 1.5) / 100 * 1.5), 4 if entry < 1 else 2)
    s['stop_loss'] = stop
    
    target = float(s.get('target_price') or 0)
    if not target or target <= entry:
        target = round(entry * (1 + atr_pct / 100 * 2.5), 4 if entry < 1 else 2)
    s['target_price'] = target
    
    # Confidence clamp
    s['confidence'] = max(1, min(100, int(s.get('confidence') or 65)))
    s['timeframe']  = s.get('timeframe') or '4H'
    s['asset_class']= s.get('asset_class') or 'Equity'
    s['momentum']   = s.get('momentum') or ''
    s['key_risks']  = s.get('key_risks') or ''
    
    return s


def run():
    logger.info("[Signals] Starting signal generation job...")
    
    with get_db() as db:
        # 1. Fetch context
        threats = [{'title': t.title, 'description': t.description,
                    'severity': t.severity, 'country': t.country}
                   for t in db.query(ThreatEvent).filter(
                       ThreatEvent.status == 'Active'
                   ).order_by(ThreatEvent.published_at.desc()).limit(30).all()]
        
        news = [{'title': n.title, 'summary': n.summary, 'source': n.source,
                 'sentiment': n.sentiment,
                 'affected_assets': n.affected_assets.split(',') if n.affected_assets else []}
                for n in db.query(NewsItem).order_by(
                    NewsItem.published_at.desc()).limit(50).all()]
        
        asset_map = {a.symbol: {'name': a.name, 'price': a.price, 'change_pct': a.change_percent}
                     for a in db.query(MarketAsset).all()}
    
    # 2. Opportunistic tickers
    opp = extract_opportunistic(threats, news, ALL_SYMBOLS)
    opp_syms = [o['symbol'] for o in opp]
    logger.info(f"[Signals] Opportunistic tickers: {opp_syms}")
    
    # 3. Fetch OHLCV + TA in batches
    all_syms = ALL_SYMBOLS + [s for s in opp_syms if s not in ALL_SYMBOLS]
    logger.info(f"[Signals] Fetching OHLCV for {len(all_syms)} symbols...")
    
    bars = fetch_batch(all_syms, timeframes=['1H', '4H', '1D'])
    ta_profiles = {}
    for sym, sym_bars in bars.items():
        ta_profiles[sym] = analyze_symbol(sym_bars)
    
    # 4. Build context strings
    def build_threat_ctx():
        lines = []
        for t in threats[:15]:
            sev = t.get('severity','Unknown')
            lines.append(f"[{sev}] {t.get('country','?')}: {t.get('title','')}")
        return '\n'.join(lines) or "No active threat intelligence."
    
    def build_news_ctx():
        lines = []
        for n in news[:20]:
            sent = n.get('sentiment','neutral')
            lines.append(f"[{sent.upper()}] {n.get('title','')} ({n.get('source','')})")
        return '\n'.join(lines) or "No recent news."
    
    def build_ta_block(symbols):
        blocks = []
        for sym in symbols:
            ta = ta_profiles.get(sym)
            if ta:
                name = asset_map.get(sym, {}).get('name', sym)
                blocks.append(build_ta_prompt_block(sym, ta, name))
        return '\n'.join(blocks) if blocks else "No TA data available."
    
    threat_ctx = build_threat_ctx()
    news_ctx   = build_news_ctx()
    
    bounce_note = """
IMPORTANT — LONG-ONLY STRATEGY:
- direction must be "Long" (trend-following) or "Bounce" (mean-reversion oversold bounce)
- NEVER output "Short" — if bearish, skip it
- All stop_loss MUST be BELOW entry_price
- All target_price MUST be ABOVE entry_price
"""
    
    sys_prompt = (
        "You are an expert quantitative trader and geopolitical risk analyst. "
        "You specialize in multi-timeframe technical analysis, combining TA signals with "
        "macro and geopolitical intelligence to generate high-probability trade setups. "
        "You only output valid JSON arrays — no commentary, no markdown outside the JSON."
    )
    
    # 5. Build 4 prompts
    def make_prompt(track_label, track_symbols, task_description):
        ta_block = build_ta_block(track_symbols)
        return f"""=== GEOPOLITICAL & MACRO INTELLIGENCE ===
{threat_ctx}

=== MARKET NEWS ===
{news_ctx}

=== TECHNICAL ANALYSIS — {track_label} ===
{ta_block}

=== YOUR TASK ===
{task_description}

{bounce_note}

Output format:
{SIGNAL_SCHEMA}

Return ONLY the JSON array."""

    prompt_a = make_prompt("MACRO/GEO/COMMODITIES", TRACK_A, """
You are analyzing: Defense (RTX, LMT, NOC, GD, BA), Energy (XOM, CVX, COP, FANG, CEG),
Commodities (GLD, SLV, GDX, GDXJ, USO, UNG), Rates/Macro (TLT, SPY, IWM).

1. What is the macro regime? (risk-on / risk-off / stagflation?)
2. Defense: any conflict escalation → defense spending catalyst?
3. Energy: oil/gas supply shocks, OPEC moves, geopolitical disruption?
4. Gold/Silver: inflation hedge demand, USD weakness?
5. Use 4H/1D for trend, 1H for entry timing.
6. Generate 5-7 LONG or BOUNCE signals with specific TA-referenced reasoning.
Reference actual prices and indicator values.""")

    prompt_b = make_prompt("TECH/AI/SEMIS", TRACK_B, """
You are analyzing: Semiconductors (NVDA, AMD, AVGO, TSM, ANET, INTC, QCOM, SMCI),
AI Infrastructure (VRT, CRWV, NBIS), Big Tech (MSFT, GOOGL, AAPL, META, AMZN),
ETFs (SOXX, QQQ), FinTech (COIN, MSTR, HOOD, PLTR).

1. Semiconductor cycle — bull or bear? Sector leader?
2. AI capex — any earnings/news catalysts?
3. EMA stack analysis per symbol — where is price relative to 21/50/200?
4. 4H MACD crossovers + RSI recovery from oversold = best entries
5. Generate 5-7 LONG or BOUNCE signals referencing actual TA numbers.""")

    prompt_c = make_prompt("CRYPTO", TRACK_C, """
You are analyzing: BTC/USD, ETH/USD, SOL/USD, XRP/USD, BNB/USD, AVAX/USD,
LINK/USD, DOGE/USD, ADA/USD, AAVE/USD, DOT/USD, ATOM/USD, SUI/USD, NEAR/USD, OP/USD, ARB/USD.

1. BTC 1D structure — HH/HL uptrend or broken?
2. ETH/BTC ratio trend
3. Per-alt: 4H S/R pivots, RSI, MACD crossovers, BB squeezes
4. Volume surges on 1H/2H carry more weight in crypto (24/7)
5. Regulatory/macro news impact on crypto
6. Generate 5-7 LONG or BOUNCE signals.""")
    
    opp_ta_block = build_ta_block(opp_syms) if opp_syms else "No opportunistic tickers this cycle."
    prompt_d = f"""=== GEOPOLITICAL & MACRO INTELLIGENCE ===
{threat_ctx}

=== MARKET NEWS ===
{news_ctx}

=== TECHNICAL ANALYSIS — NEWS-DRIVEN OPPORTUNITIES ===
{opp_ta_block}

=== YOUR TASK ===
You are an opportunistic scanner. Find ANY stock or crypto with a strong catalyst RIGHT NOW:
- News-driven moves (earnings, product launches, regulatory wins, M&A)
- Technical breakouts not in our watchlist
- Sector rotation opportunities
- Crypto momentum plays

You are NOT limited to the symbols above. Use news context and typical technical patterns.
Generate 3-5 high-conviction LONG or BOUNCE signals.

{bounce_note}

Output format:
{SIGNAL_SCHEMA}

Return ONLY the JSON array."""

    # 6. Fire 4 tracks in parallel
    logger.info("[Signals] Firing 4 parallel LLM tracks...")
    prompts = [
        ('TrackA', prompt_a),
        ('TrackB', prompt_b),
        ('TrackC', prompt_c),
        ('TrackD', prompt_d),
    ]
    
    raw_signals = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(call_lm_studio, p, sys_prompt, 3000, 0.15): name
            for name, p in prompts
        }
        for future in as_completed(futures):
            track = futures[future]
            try:
                result = future.result()
                parsed = parse_json(result)
                if isinstance(parsed, list):
                    logger.info(f"[Signals] {track}: {len(parsed)} signals")
                    raw_signals.extend(parsed)
                elif isinstance(parsed, dict):
                    raw_signals.append(parsed)
            except Exception as e:
                logger.error(f"[Signals] {track} failed: {e}")
    
    logger.info(f"[Signals] Total raw signals: {len(raw_signals)}")
    
    # 7. Normalize + validate
    now_iso = datetime.now(timezone.utc).isoformat()
    saved = 0
    seen_symbols = set()
    
    with get_db() as db:
        # Dedup: mark old Active signals as Expired
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        old = db.query(TradingSignal).filter(
            TradingSignal.status == 'Active',
            TradingSignal.generated_at < cutoff
        ).all()
        for s in old:
            s.status = 'Expired'
        
        for raw in raw_signals:
            norm = normalize_signal(raw, ta_profiles, asset_map)
            if not norm:
                continue
            sym = norm['asset_symbol']
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)
            
            # Check if we already have an active signal for this symbol
            existing = db.query(TradingSignal).filter(
                TradingSignal.asset_symbol == sym,
                TradingSignal.status == 'Active'
            ).first()
            if existing:
                # Update existing
                for k, v in norm.items():
                    if hasattr(existing, k):
                        setattr(existing, k, v)
                existing.generated_at = now_iso
                existing.updated_date = now_iso
            else:
                signal = TradingSignal(
                    id=str(uuid.uuid4()),
                    asset_symbol=sym,
                    asset_name=norm.get('asset_name', sym),
                    asset_class=norm.get('asset_class', 'Equity'),
                    direction=norm.get('direction', 'Long'),
                    confidence=norm.get('confidence', 65),
                    timeframe=norm.get('timeframe', '4H'),
                    reasoning=norm.get('reasoning', ''),
                    entry_price=norm.get('entry_price'),
                    target_price=norm.get('target_price'),
                    stop_loss=norm.get('stop_loss'),
                    key_risks=norm.get('key_risks', ''),
                    momentum=norm.get('momentum', ''),
                    status='Active',
                    generated_at=now_iso,
                    created_date=now_iso,
                    updated_date=now_iso
                )
                db.add(signal)
            saved += 1
    
    logger.info(f"[Signals] Done — {saved} signals saved/updated")
    return {'signals_saved': saved}

# ── Regime-Aware Signal Generation (called at top of run()) ───────────────────
def get_regime_context():
    """Fetch regime + format for prompt injection."""
    try:
        from lib.market_regime import get_regime, regime_to_prompt_block
        regime = get_regime()
        block  = regime_to_prompt_block(regime)
        return regime, block
    except Exception as e:
        logger.warning(f"[Signals] Regime fetch failed: {e}")
        return {}, "=== MARKET REGIME ===\nRegime data unavailable."

def score_and_rank(signals: list[dict], ta_profiles: dict, regime: dict) -> list[dict]:
    """Score all signals and sort by composite score."""
    try:
        from lib.signal_scorer import score_signal
        from lib.earnings_calendar import is_earnings_risk
        scored = []
        for sig in signals:
            sym = sig.get('asset_symbol', '')
            ta  = ta_profiles.get(sym, {})
            er  = is_earnings_risk(sym)
            scored.append(score_signal(sig, ta, regime, er))
        return sorted(scored, key=lambda x: x.get('composite_score', 0), reverse=True)
    except Exception as e:
        logger.warning(f"[Signals] Scoring failed: {e}")
        return signals
