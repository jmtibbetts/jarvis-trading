"""AllRatesToday — live + historical FX rates via their REST API (the same
API the user's LM Studio MCP stdio server wraps; Jarvis calls REST directly
since lib/mcp_client speaks streamable HTTP only).

Verified live: /rate returns interbank rates ({"rate":0.866514,
"source":"interbank"}), /historical-rates returns daily closes.

Auth: Bearer ALLRATES_API_KEY. Quota unknown → 15-min cache per pair and
None on any failure; callers never depend on this source.

Symbol handling: Jarvis forex symbols are Yahoo-style ("EURUSD=X",
"JPY=X" — the bare form means USD/JPY). fx_pair_from_symbol() maps them to
(source, target) or returns None for anything that isn't FX.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://allratestoday.com/api"
CACHE_TTL_SECONDS = 900

_cache: dict[str, tuple[datetime, dict | None]] = {}

_FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY", "HKD",
    "SGD", "SEK", "NOK", "DKK", "INR", "MXN", "BRL", "ZAR", "KRW", "TRY",
    "PLN", "THB",
}


def fx_pair_from_symbol(symbol: str) -> tuple[str, str] | None:
    """'EURUSD=X' -> ('EUR','USD'); 'JPY=X' -> ('USD','JPY'); 'EUR/USD' ->
    ('EUR','USD'); None for non-FX symbols."""
    s = (symbol or "").upper().strip()
    if s.endswith("=X"):
        s = s[:-2]
        if len(s) == 6 and s[:3] in _FIAT and s[3:] in _FIAT:
            return s[:3], s[3:]
        if len(s) == 3 and s in _FIAT:
            return "USD", s
        return None
    if "/" in s:
        a, _, b = s.partition("/")
        if a in _FIAT and b in _FIAT:
            return a, b
    return None


def _get(path: str, params: dict) -> dict | None:
    key = os.getenv("ALLRATES_API_KEY")
    if not key:
        return None
    cache_key = f"{path}?{sorted(params.items())}"
    now = datetime.now(timezone.utc)
    hit = _cache.get(cache_key)
    if hit and (now - hit[0]).total_seconds() < CACHE_TTL_SECONDS:
        return hit[1]
    try:
        r = httpx.get(
            f"{BASE_URL}{path}", params=params, timeout=20,
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
        )
        body = r.json() if r.status_code == 200 else None
        if r.status_code != 200:
            logger.info(f"[AllRates] {path} -> {r.status_code}: {str(r.text)[:120]}")
    except Exception as e:
        logger.debug(f"[AllRates] {path} failed: {e}")
        body = None
    _cache[cache_key] = (now, body)
    return body


def get_fx_rate(source: str, target: str) -> dict | None:
    """{'rate': 0.866514, 'source': 'interbank'} or None."""
    return _get("/rate", {"source": source, "target": target})


def get_fx_history(source: str, target: str, period: str = "7d") -> dict | None:
    """{'source','target','data':[{'date','rate','timestamp'},...]} or None."""
    return _get("/historical-rates", {"source": source, "target": target, "period": period})


def fx_summary_block(symbol: str) -> str | None:
    """Compact FX context block for LLM prompts: live rate + 7d trend.
    None when the symbol isn't FX or the API is unavailable."""
    pair = fx_pair_from_symbol(symbol)
    if not pair:
        return None
    src, tgt = pair
    rate = get_fx_rate(src, tgt)
    if not rate or rate.get("rate") is None:
        return None
    lines = [f"{src}/{tgt} live interbank rate: {rate['rate']}"]
    hist = get_fx_history(src, tgt, "7d")
    points = (hist or {}).get("data") or []
    if len(points) >= 2:
        first, last = points[0], points[-1]
        try:
            chg = (float(last["rate"]) - float(first["rate"])) / float(first["rate"]) * 100
            daily = ", ".join(f"{p['date'][5:]}:{p['rate']}" for p in points[-7:])
            lines.append(f"7d change: {chg:+.2f}% ({daily})")
        except Exception:
            pass
    return "FX RATES (AllRatesToday, live interbank):\n" + "\n".join(lines)
