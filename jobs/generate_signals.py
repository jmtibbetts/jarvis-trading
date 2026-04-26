"""
Job: Generate Trading Signals v6.0
Wired composite scorer + earnings risk. 4 parallel LLM tracks.
"""
import logging, re, uuid
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import get_db, TradingSignal, ThreatEvent, NewsItem, MarketAsset
from lib.lmstudio import call_lm_studio, parse_json
from lib.ohlcv import fetch_batch
from lib.ta_engine import analyze_symbol, build_ta_prompt_block

logger = logging.getLogger(__name__)

TRACK_A = ["RTX","LMT","NOC","GD","BA","XOM","CVX","COP","FANG","CEG","GLD","SLV","TLT","SPY","IWM","USO","UNG","GDX","GDXJ"]
TRACK_B = ["NVDA","AMD","MSFT","GOOGL","AAPL","META","AMZN","AVGO","TSM","ANET","INTC","QCOM","SMCI","VRT","SOXX","QQQ","CRWV","NBIS","PLTR","TSLA","COIN","MSTR","ARM","HOOD"]
TRACK_C = ["BTC/USD","ETH/USD","SOL/USD","XRP/USD","BNB/USD","AVAX/USD","LINK/USD","DOGE/USD","ADA/USD","AAVE/USD","DOT/USD","ATOM/USD","SUI/USD","RENDER/USD","INJ/USD","NEAR/USD","OP/USD","ARB/USD"]
ALL_SYMBOLS = list(dict.fromkeys(TRACK_A + TRACK_B + TRACK_C))
COMMON_TICKERS = {"AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AMD","INTC","QCOM","AVGO","TSM","ARM","SMCI","PLTR","COIN","MSTR","HOOD","RBLX","SNAP","UBER","ABNB","SQ","PYPL","SHOP","NET","CRWD","PANW","ZS","DDOG","SNOW","MDB","AI","SOUN","IONQ","RXRX","ACHR","JOBY","RKLB","ASTS","XOM","CVX","COP","OXY","SLB","HAL","RTX","LMT","NOC","GD","BA","GLD","SLV","GDX","GDXJ","USO","UNG","SPY","QQQ","IWM","DIA","XLK","XLF","XLE","XLV","TLT","IEF","HYG","JPM","BAC","GS","BTC","ETH","SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI"}
CRYPTO_BASES = {"SOL","XRP","BNB","AVAX","LINK","DOGE","ADA","AAVE","DOT","ATOM","SUI","RENDER","INJ","NEAR","OP","ARB","MATIC","UNI","PEPE","LTC"}
SIGNAL_SCHEMA = """[{"asset_symbol":"NVDA","asset_name":"NVIDIA","asset_class":"Equity","direction":"Long","confidence":78,"timeframe":"4H","entry_price":875.00,"target_price":920.00,"stop_loss":850.00,"reasoning":"detailed reasoning","key_risks":"risks","momentum":"Bullish"}]"""

def extract_opportunistic(threats, news, fixed_symbols):
    fixed = {s.replace("/USD","") for s in fixed_symbols}
    found = {}
    texts = [f"{t.get('title','')} {t.get('description','')}" for t in threats[:20]] + [f"{n.get('title','')} {n.get('summary','')} {chr(32).join(n.get('affected_assets',[]))}" for n in news[:30]]
    for text in texts:
        for m in re.finditer(r'\$([A-Z]{1,5})|\b([A-Z]{2,5})\b', text):
            t2 = (m.group(1) or m.group(2) or '').upper()
            if t2 and t2 in COMMON_TICKERS and t2 not in fixed:
                found[t2] = found.get(t2,0) + 1
    return [{"symbol":f"{t}/USD" if t in CRYPTO_BASES else t,"is_crypto":t in CRYPTO_BASES} for t,cnt in sorted(found.items(),key=lambda x:-x[1]) if cnt>=1][:15]

def normalize_signal(s, ta_profiles, asset_map):
    sym = (s.get("asset_symbol") or s.get("symbol") or s.get("ticker") or "").upper().strip()
    if not sym: return None
    if sym in CRYPTO_BASES: sym=f"{sym}/USD"; s["asset_class"]="Crypto"
    if "/" in sym: s["asset_class"]="Crypto"
    s["asset_symbol"]=sym; s["asset_name"]=s.get("asset_name") or sym
    direction=(s.get("direction") or "Long").capitalize()
    s["direction"]="Bounce" if direction=="Short" else ("Bounce" if direction=="Bounce" else "Long")
    ta=ta_profiles.get(sym,{})
    last_price=(ta.get("4H",{}) or {}).get("price",{}).get("last") or (ta.get("1D",{}) or {}).get("price",{}).get("last") or asset_map.get(sym,{}).get("price")
    if not last_price: return s
    entry=float(s.get("entry_price") or 0)
    if not entry or abs(entry-last_price)/last_price>0.15: entry=round(last_price,4 if last_price<1 else 2)
    s["entry_price"]=entry
    atr_pct=((ta.get("4H",{}) or {}).get("atr",{}) or {}).get("pct") or 2.0
    stop=float(s.get("stop_loss") or 0)
    if not stop or stop>=entry: stop=round(entry*(1-max(atr_pct,1.5)/100*1.5),4 if entry<1 else 2)
    s["stop_loss"]=stop
    target=float(s.get("target_price") or 0)
    if not target or target<=entry: target=round(entry*(1+atr_pct/100*2.5),4 if entry<1 else 2)
    s["target_price"]=target
    s["confidence"]=max(1,min(100,int(s.get("confidence") or 65)))
    s["timeframe"]=s.get("timeframe") or "4H"
    s["asset_class"]=s.get("asset_class") or "Equity"
    s["momentum"]=s.get("momentum") or ""
    s["key_risks"]=s.get("key_risks") or ""
    return s

def score_safe(signal, ta_profiles, regime, earnings_set):
    try:
        from lib.signal_scorer import score_signal
        sym=signal.get("asset_symbol","")
        return score_signal(signal, ta_profiles.get(sym,{}), regime, earnings_risk=sym.replace("/USD","") in earnings_set)
    except:
        signal["composite_score"]=signal.get("confidence",65)
        return signal

def run():
    logger.info("[Signals] Starting signal generation...")
    with get_db() as db:
        threats=[{"title":t.title,"description":t.description,"severity":t.severity,"country":t.country} for t in db.query(ThreatEvent).filter(ThreatEvent.status=="Active").order_by(ThreatEvent.published_at.desc()).limit(30).all()]
        news=[{"title":n.title,"summary":n.summary,"source":n.source,"sentiment":n.sentiment,"affected_assets":n.affected_assets.split(",") if n.affected_assets else []} for n in db.query(NewsItem).order_by(NewsItem.published_at.desc()).limit(50).all()]
        asset_map={a.symbol:{"name":a.name,"price":a.price} for a in db.query(MarketAsset).all()}
    opp=extract_opportunistic(threats,news,ALL_SYMBOLS)
    all_syms=ALL_SYMBOLS+[o["symbol"] for o in opp if o["symbol"] not in ALL_SYMBOLS]
    logger.info(f"[Signals] Fetching OHLCV for {len(all_syms)} symbols...")
    bars=fetch_batch(all_syms,timeframes=["1H","4H","1D"])
    ta_profiles={sym:analyze_symbol(sym_bars) for sym,sym_bars in bars.items()}
    earnings_set=set()
    try:
        from lib.earnings_calendar import get_earnings_this_week
        earnings_set=get_earnings_this_week()
    except: pass
    regime={"label":"Unknown","risk":"medium"}
    try:
        from lib.market_regime import get_regime
        regime=get_regime()
        logger.info(f"[Signals] Regime: {regime.get('label')} | Risk: {regime.get('risk')}")
    except Exception as e:
        logger.warning(f"[Signals] Regime check failed: {e}")
    threat_ctx="\n".join([f"[{t.get('severity','?')}] {t.get('country','?')}:{t.get('title','')}" for t in threats[:15]]) or "No threats."
    news_ctx="\n".join([f"[{n.get('sentiment','neutral').upper()}] {n.get('title','')} ({n.get('source','')})" for n in news[:20]]) or "No news."
    bounce="\nIMPORTANT: direction must be Long or Bounce only. NEVER Short. stop_loss BELOW entry. target ABOVE entry. R:R>=2.\n"
    sys_p="You are an expert quantitative trader. Output only valid JSON arrays, no commentary."
    def ta_block(syms): return "\n".join([build_ta_prompt_block(s,ta_profiles.get(s),asset_map.get(s,{}).get("name",s)) for s in syms if ta_profiles.get(s)]) or "No TA data."
    def make_p(label,syms,task):
        return f"=== GEO/MACRO INTEL ===\n{threat_ctx}\n\n=== MARKET NEWS ===\n{news_ctx}\n\n=== TA — {label} ===\n{ta_block(syms)}\n\n=== TASK ===\n{task}{bounce}\nFormat:\n{SIGNAL_SCHEMA}\nReturn ONLY the JSON array."
    tracks=[
        ("A_macro",TRACK_A,make_p("MACRO/GEO/COMMODITIES",TRACK_A,"Analyze defense RTX/LMT/NOC, energy XOM/CVX, commodities GLD/SLV, rates TLT. Generate 5-7 LONG or BOUNCE signals with TA references.")),
        ("B_tech", TRACK_B,make_p("TECH/AI/GROWTH",TRACK_B,"Analyze AI/semis NVDA/AMD/AVGO/TSM/ARM, software MSFT/GOOGL/META, high-beta PLTR/COIN/TSLA. Generate 5-8 LONG or BOUNCE signals.")),
        ("C_crypto",TRACK_C,make_p("CRYPTO",TRACK_C,"Analyze BTC/ETH macro, L1s SOL/XRP/BNB/AVAX, DeFi LINK/AAVE/DOT. 24/7 market. Generate 4-6 signals. Crypto: wider stops 8-12% ATR ok.")),
    ]
    if opp:
        tracks.append(("D_opp",[o["symbol"] for o in opp],make_p("OPPORTUNISTIC",[o["symbol"] for o in opp],f"These tickers appeared in threat/news feeds: {[o['symbol'] for o in opp[:10]]}. Generate 2-5 signals for strongest setups.")))
    def run_track(name,syms,prompt):
        try:
            r=call_lm_studio(prompt,system=sys_p,max_tokens=3000,temperature=0.15)
            sigs=parse_json(r)
            if isinstance(sigs,list): return sigs
            if isinstance(sigs,dict):
                for k in ["signals","trades","setups"]:
                    if sigs.get(k): return sigs[k]
        except Exception as e:
            logger.error(f"[Signals] Track {name} failed: {e}")
        return []
    all_raw=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs={ex.submit(run_track,n,s,p):n for n,s,p in tracks}
        for fut in as_completed(futs): all_raw.extend(fut.result())
    logger.info(f"[Signals] {len(all_raw)} raw signals from LLM")
    now_iso=datetime.now(timezone.utc).isoformat()
    saved=skipped=0
    with get_db() as db:
        stale=(datetime.now(timezone.utc)-timedelta(hours=6)).isoformat()
        for s in db.query(TradingSignal).filter(TradingSignal.status=="Active",TradingSignal.generated_at<stale).all():
            s.status="Expired"; s.updated_date=now_iso
        existing={s.asset_symbol for s in db.query(TradingSignal).filter(TradingSignal.status=="Active").all()}
        for raw in all_raw:
            n=normalize_signal(raw,ta_profiles,asset_map)
            if not n or n.get("asset_symbol") in existing: skipped+=1; continue
            scored=score_safe(n,ta_profiles,regime,earnings_set)
            db.add(TradingSignal(id=str(uuid.uuid4()),asset_symbol=scored.get("asset_symbol"),asset_name=scored.get("asset_name"),asset_class=scored.get("asset_class","Equity"),direction=scored.get("direction","Long"),confidence=scored.get("confidence",65),composite_score=scored.get("composite_score"),timeframe=scored.get("timeframe","4H"),entry_price=scored.get("entry_price"),target_price=scored.get("target_price"),stop_loss=scored.get("stop_loss"),reasoning=scored.get("reasoning",""),key_risks=scored.get("key_risks",""),momentum=scored.get("momentum",""),signal_source="watchlist",status="Active",generated_at=now_iso,created_date=now_iso,updated_date=now_iso))
            existing.add(scored.get("asset_symbol")); saved+=1
    logger.info(f"[Signals] Done — {saved} saved, {skipped} skipped")
    return {"saved":saved,"skipped":skipped,"regime":regime.get("label")}
