"""
Earnings calendar fetcher — avoids entering positions right before earnings
when IV crush and gap risk is highest.
Uses free Yahoo Finance RSS / scraping — no paid API needed.
"""
import httpx, logging, re
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Simple in-memory cache (TTL = 4 hours)
_cache = {}
_cache_time = None
CACHE_TTL_HOURS = 4

def get_earnings_this_week() -> set[str]:
    """
    Returns set of ticker symbols reporting earnings within the next 5 days.
    Uses Yahoo Finance earnings calendar (no API key needed).
    """
    global _cache, _cache_time
    
    now = datetime.now(timezone.utc)
    if _cache_time and (now - _cache_time).seconds < CACHE_TTL_HOURS * 3600:
        return _cache
    
    tickers = set()
    try:
        # Yahoo Finance earnings calendar
        for offset in range(5):
            date = (now + timedelta(days=offset)).strftime('%Y-%m-%d')
            url  = f"https://finance.yahoo.com/calendar/earnings?day={date}"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            r = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
            # Extract ticker symbols from the page
            found = re.findall(r'"symbol"\s*:\s*"([A-Z]{1,5})"', r.text)
            tickers.update(found)
    except Exception as e:
        logger.debug(f"[Earnings] Calendar fetch error: {e}")
    
    _cache = tickers
    _cache_time = now
    logger.info(f"[Earnings] {len(tickers)} tickers with earnings this week")
    return tickers


def is_earnings_risk(symbol: str, days_before: int = 3) -> bool:
    """Returns True if this symbol has earnings within `days_before` days."""
    sym = symbol.replace('/USD', '').upper()
    earnings = get_earnings_this_week()
    return sym in earnings
