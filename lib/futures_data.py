"""
lib/futures_data.py
────────────────────
Fetches price data and news for futures, commodities, and currency pairs
using yfinance (free, no API key required) with an RSS news layer.

Supported categories:
  - Energy:     Crude Oil (CL=F), Brent (BZ=F), Natural Gas (NG=F), RBOB Gasoline (RB=F)
  - Metals:     Gold (GC=F), Silver (SI=F), Platinum (PL=F), Copper (HG=F), Palladium (PA=F)
  - Grains:     Corn (ZC=F), Wheat (ZW=F), Soybeans (ZS=F)
  - Currencies: EUR/USD (EURUSD=X), GBP/USD (GBPUSD=X), JPY/USD (JPY=X),
                AUD/USD (AUDUSD=X), USD/CHF (CHF=X), USD/CAD (CAD=X),
                DXY Dollar Index (DX-Y.NYB)
  - Indices:    VIX (^VIX), S&P Futures (ES=F), Nasdaq Futures (NQ=F), Dow Futures (YM=F)
  - Softs:      Sugar (SB=F), Coffee (KC=F), Cocoa (CC=F)
  - Bonds:      10Y Treasury (^TNX), 30Y Treasury (^TYX), 2Y Treasury (^IRX)

All return standardised OHLCV DataFrames compatible with the existing ta_engine.
"""

import logging
import time
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
import pandas as pd

logger = logging.getLogger(__name__)

# ─── Symbol registry ──────────────────────────────────────────────────────────

FUTURES_UNIVERSE = {
    # ── Energy ──
    "CL=F":  {"name": "Crude Oil (WTI)",    "category": "Energy",    "unit": "barrel",   "paper": True},
    "BZ=F":  {"name": "Brent Crude Oil",    "category": "Energy",    "unit": "barrel",   "paper": True},
    "NG=F":  {"name": "Natural Gas",        "category": "Energy",    "unit": "mmBtu",    "paper": True},
    "RB=F":  {"name": "RBOB Gasoline",      "category": "Energy",    "unit": "gallon",   "paper": False},
    "HO=F":  {"name": "Heating Oil",        "category": "Energy",    "unit": "gallon",   "paper": False},
    # ── Metals ──
    "GC=F":  {"name": "Gold Futures",       "category": "Metals",    "unit": "troy oz",  "paper": True},
    "SI=F":  {"name": "Silver Futures",     "category": "Metals",    "unit": "troy oz",  "paper": True},
    "PL=F":  {"name": "Platinum",           "category": "Metals",    "unit": "troy oz",  "paper": True},
    "PA=F":  {"name": "Palladium",          "category": "Metals",    "unit": "troy oz",  "paper": False},
    "HG=F":  {"name": "Copper Futures",     "category": "Metals",    "unit": "lb",       "paper": True},
    # ── Grains ──
    "ZC=F":  {"name": "Corn Futures",       "category": "Grains",    "unit": "bushel",   "paper": True},
    "ZW=F":  {"name": "Wheat Futures",      "category": "Grains",    "unit": "bushel",   "paper": True},
    "ZS=F":  {"name": "Soybeans",           "category": "Grains",    "unit": "bushel",   "paper": False},
    # ── Currencies ──
    "EURUSD=X": {"name": "EUR/USD",         "category": "Forex",     "unit": "pair",     "paper": True},
    "GBPUSD=X": {"name": "GBP/USD",         "category": "Forex",     "unit": "pair",     "paper": True},
    "JPY=X":    {"name": "USD/JPY",         "category": "Forex",     "unit": "pair",     "paper": True},
    "AUDUSD=X": {"name": "AUD/USD",         "category": "Forex",     "unit": "pair",     "paper": True},
    "CHF=X":    {"name": "USD/CHF",         "category": "Forex",     "unit": "pair",     "paper": True},
    "CAD=X":    {"name": "USD/CAD",         "category": "Forex",     "unit": "pair",     "paper": False},
    "DX-Y.NYB": {"name": "US Dollar Index", "category": "Forex",     "unit": "index",    "paper": True},
    "USDMXN=X": {"name": "USD/MXN",        "category": "Forex",     "unit": "pair",     "paper": False},
    # ── Index Futures ──
    "ES=F":  {"name": "S&P 500 Futures",    "category": "Index",     "unit": "contract", "paper": True},
    "NQ=F":  {"name": "Nasdaq 100 Futures", "category": "Index",     "unit": "contract", "paper": True},
    "YM=F":  {"name": "Dow Futures",        "category": "Index",     "unit": "contract", "paper": False},
    "RTY=F": {"name": "Russell 2000 Fut",   "category": "Index",     "unit": "contract", "paper": False},
    "^VIX":  {"name": "CBOE VIX",           "category": "Volatility","unit": "index",    "paper": False},
    # ── Softs ──
    "SB=F":  {"name": "Sugar #11",          "category": "Softs",     "unit": "lb",       "paper": False},
    "KC=F":  {"name": "Coffee",             "category": "Softs",     "unit": "lb",       "paper": False},
    "CC=F":  {"name": "Cocoa",              "category": "Softs",     "unit": "MT",       "paper": False},
    # ── Treasuries (reference) ──
    "^TNX":  {"name": "10Y Treasury Yield", "category": "Bonds",     "unit": "%",        "paper": False},
    "^TYX":  {"name": "30Y Treasury Yield", "category": "Bonds",     "unit": "%",        "paper": False},
}

# Symbols eligible for paper trading
PAPER_FUTURES = [sym for sym, meta in FUTURES_UNIVERSE.items() if meta.get("paper")]

# Category → search keywords for news scraping
FUTURES_NEWS_KEYWORDS = {
    "Energy":     ["crude oil price", "OPEC production", "brent crude", "natural gas supply",
                   "energy commodities", "oil demand", "US oil inventory"],
    "Metals":     ["gold price", "silver market", "precious metals", "gold futures",
                   "Federal Reserve gold", "inflation gold hedge", "copper demand China"],
    "Grains":     ["corn futures", "wheat price", "grain markets", "USDA crop report",
                   "agricultural commodities", "drought impact crops"],
    "Forex":      ["dollar index", "EUR USD", "forex market", "Federal Reserve dollar",
                   "currency markets", "USD strength", "GBP USD Brexit"],
    "Index":      ["S&P 500 futures", "Nasdaq futures", "stock index futures",
                   "equity futures market", "futures trading"],
}

# Display-friendly labels
CATEGORY_ICONS = {
    "Energy":     "🛢️",
    "Metals":     "🥇",
    "Grains":     "🌾",
    "Forex":      "💱",
    "Index":      "📊",
    "Volatility": "⚡",
    "Softs":      "☕",
    "Bonds":      "📈",
}

# ─── OHLCV data via yfinance ──────────────────────────────────────────────────

# Map internal timeframe labels to yfinance period/interval combos
_YF_TF_MAP = {
    "1H":  {"interval": "1h",  "period": "7d"},
    "2H":  {"interval": "2h",  "period": "14d"},
    "4H":  {"interval": "4h",  "period": "30d"},
    "1D":  {"interval": "1d",  "period": "1y"},
}


def fetch_futures_ohlcv(symbol: str, timeframe: str = "1D") -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data for a futures/forex symbol via yfinance.
    Returns a DataFrame with columns [open, high, low, close, volume]
    or None on failure.
    """
    try:
        import yfinance as yf
        cfg = _YF_TF_MAP.get(timeframe, _YF_TF_MAP["1D"])
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=cfg["period"], interval=cfg["interval"], auto_adjust=True)
        if df is None or df.empty:
            logger.debug(f"[Futures] No data for {symbol}/{timeframe}")
            return None
        # Normalize column names
        df = df.rename(columns={"Open": "open", "High": "high",
                                 "Low": "low",  "Close": "close", "Volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        # Drop rows with all-zero prices (bad data)
        df = df[df["close"] > 0]
        return df if len(df) >= 5 else None
    except Exception as e:
        logger.warning(f"[Futures] fetch_futures_ohlcv({symbol}, {timeframe}) failed: {e}")
        return None


def fetch_futures_multi_tf(symbol: str, timeframes: list = None) -> Dict[str, Optional[pd.DataFrame]]:
    """
    Fetch multiple timeframes for a single futures symbol.
    Returns { '1H': df, '4H': df, '1D': df } — same structure as ohlcv.fetch_multi_timeframe.
    """
    if timeframes is None:
        timeframes = ["1H", "4H", "1D"]
    result = {}
    for tf in timeframes:
        df = fetch_futures_ohlcv(symbol, tf)
        result[tf] = df
        time.sleep(0.2)   # gentle rate limit — yfinance is free but don't hammer
    return result


def fetch_futures_latest_price(symbol: str) -> Optional[float]:
    """
    Fetch the latest close price for a single futures symbol.
    """
    try:
        import yfinance as yf
        tick = yf.Ticker(symbol)
        info = tick.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        return float(price) if price else None
    except Exception as e:
        logger.debug(f"[Futures] price fetch failed {symbol}: {e}")
        return None


def fetch_all_futures_prices(symbols: list = None) -> dict:
    """
    Fetch latest prices for a list of futures/forex symbols.
    Returns { symbol: { price, name, category, change_pct } }
    """
    if symbols is None:
        symbols = list(FUTURES_UNIVERSE.keys())
    try:
        import yfinance as yf
        tickers = yf.Tickers(" ".join(symbols))
        result = {}
        for sym in symbols:
            meta = FUTURES_UNIVERSE.get(sym, {})
            try:
                tick = tickers.tickers.get(sym)
                if not tick:
                    continue
                info = tick.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                prev  = getattr(info, "previous_close", None)
                if not price:
                    continue
                chg_pct = round(((price - prev) / prev * 100), 3) if prev and prev != 0 else 0.0
                result[sym] = {
                    "symbol":       sym,
                    "name":         meta.get("name", sym),
                    "category":     meta.get("category", "Other"),
                    "unit":         meta.get("unit", ""),
                    "price":        round(float(price), 4),
                    "change_pct":   chg_pct,
                    "paper_eligible": meta.get("paper", False),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as se:
                logger.debug(f"[Futures] {sym} price error: {se}")
        logger.info(f"[Futures] Fetched {len(result)}/{len(symbols)} prices")
        return result
    except Exception as e:
        logger.error(f"[Futures] fetch_all_futures_prices failed: {e}")
        return {}


# ─── News via RSS feeds ───────────────────────────────────────────────────────

# Free commodity/forex RSS sources (no API key needed)
FUTURES_RSS_FEEDS = [
    # Energy / Commodity
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/technologyNews",
    "https://rss.cnn.com/rss/money_markets.rss",
    "https://www.fxstreet.com/rss",
    "https://www.kitco.com/rss/news.rss",           # Gold/metals
    "https://oilprice.com/rss/main",                 # Oil/energy
    "https://www.forexlive.com/feed/news",           # Forex
    "https://finance.yahoo.com/rss/topfinstories",   # General finance
    "https://feeds.feedburner.com/zerohedge/feed",   # Macro
]

# Keywords for filtering articles relevant to futures
_FUTURES_KEYWORDS = [
    "crude oil", "brent", "opec", "natural gas", "lng",
    "gold", "silver", "platinum", "precious metals", "copper",
    "wheat", "corn", "grain", "usda",
    "eur/usd", "gbp/usd", "dollar index", "dxy", "forex",
    "s&p futures", "nasdaq futures", "vix",
    "inflation", "federal reserve", "fed rate", "treasury yield",
    "commodity", "futures market", "hedging",
]


def _parse_rss(url: str, timeout: int = 8) -> list:
    """Parse an RSS feed URL and return list of article dicts."""
    try:
        import urllib.request
        import xml.etree.ElementTree as ET
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        items = []
        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        for item in root.iter("item"):
            title   = (item.findtext("title") or "").strip()
            link    = (item.findtext("link") or "").strip()
            desc    = (item.findtext("description") or "").strip()
            pubdate = (item.findtext("pubDate") or "").strip()
            if title and link:
                items.append({"title": title, "url": link,
                               "summary": desc[:300], "published": pubdate})
        return items
    except Exception as e:
        logger.debug(f"[Futures/RSS] {url} failed: {e}")
        return []


def _is_futures_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in _FUTURES_KEYWORDS)


def fetch_futures_news(max_per_feed: int = 10, max_total: int = 60) -> list:
    """
    Scrape futures/commodity/forex news from free RSS feeds.
    Returns list of { title, url, summary, source, published, category }.
    """
    all_articles = []
    seen_urls = set()

    for feed_url in FUTURES_RSS_FEEDS:
        articles = _parse_rss(feed_url, timeout=8)
        source_name = feed_url.split("/")[2].replace("www.", "").replace("feeds.", "")
        count = 0
        for art in articles:
            if count >= max_per_feed:
                break
            url = art.get("url", "")
            if url in seen_urls:
                continue
            full_text = (art.get("title", "") + " " + art.get("summary", "")).lower()
            if not _is_futures_relevant(full_text):
                continue
            seen_urls.add(url)
            # Tag category
            category = "General"
            for cat, keywords in FUTURES_NEWS_KEYWORDS.items():
                if any(kw.lower() in full_text for kw in keywords):
                    category = cat
                    break
            all_articles.append({
                **art,
                "source":   source_name,
                "category": category,
            })
            count += 1
        if len(all_articles) >= max_total:
            break

    logger.info(f"[Futures/News] Fetched {len(all_articles)} futures-relevant articles")
    return all_articles[:max_total]


def get_futures_news_context(max_items: int = 15) -> str:
    """
    Returns a text block suitable for LLM prompt injection.
    Summarises the most recent futures/commodity/forex headlines.
    """
    try:
        articles = fetch_futures_news(max_total=max_items)
        if not articles:
            return ""
        lines = ["\n=== FUTURES / COMMODITY / FOREX NEWS ==="]
        for a in articles:
            icon = CATEGORY_ICONS.get(a.get("category", ""), "📰")
            lines.append(f"  {icon} [{a['category']}] {a['title']} ({a['source']})")
        lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[Futures/News] context build failed: {e}")
        return ""


# ─── Cache layer — reuse last fetch within N minutes ─────────────────────────

_price_cache: dict = {}       # { symbol: (price_data, fetched_at) }
_CACHE_TTL_SEC = 300          # 5 min


def get_cached_futures_price(symbol: str) -> Optional[dict]:
    entry = _price_cache.get(symbol)
    if entry:
        data, ts = entry
        if (time.time() - ts) < _CACHE_TTL_SEC:
            return data
    price = fetch_futures_latest_price(symbol)
    if price is None:
        return None
    meta = FUTURES_UNIVERSE.get(symbol, {})
    result = {
        "symbol": symbol, "price": price,
        "name": meta.get("name", symbol),
        "category": meta.get("category", "Other"),
        "unit": meta.get("unit", ""),
        "paper_eligible": meta.get("paper", False),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    _price_cache[symbol] = (result, time.time())
    return result


def get_all_paper_futures_prices() -> list:
    """
    Return price data for all paper-eligible futures.
    Used by the paper engine to look up current prices.
    """
    results = []
    for sym in PAPER_FUTURES:
        data = get_cached_futures_price(sym)
        if data:
            results.append(data)
    return results
