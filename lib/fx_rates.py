"""Foreign-exchange rates for Jarvis.

Primary source: Frankfurter (api.frankfurter.dev) — free, keyless, no quota,
serving ECB reference rates. One request returns EVERY pair against a base,
and one more returns the whole historical series, so a 12-pair panel costs
2 calls instead of 24.

Secondary: AllRatesToday, used ONLY when a key is configured AND its quota
is intact. Its free tier is a 300-request LIFETIME cap ("resets_at":"never"),
which the previous per-pair implementation burned through in hours: 12 pairs
x 2 calls every 15 minutes. Once a 429 is seen the provider is disabled for
the rest of the process rather than retried into a wall.

Honesty note carried into the UI: ECB reference rates are published once per
business day, so they are NOT live interbank ticks. The panel labels them as
such rather than implying real-time pricing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"
ALLRATES_BASE = "https://allratestoday.com/api"

CACHE_TTL_SECONDS = 3600          # ECB publishes daily; hourly refresh is generous
_cache: dict[str, tuple[datetime, dict | None]] = {}
_allrates_disabled_reason: str | None = None

# The AllRatesToday free cap is a LIFETIME 300 requests ("resets_at":"never"),
# so an exhausted key stays exhausted. Remember that across restarts instead
# of optimistically reporting the spare healthy every boot — but key the
# memory to the API key itself, so swapping in an upgraded key clears it.
_QUOTA_FLAG_PREFIX = "allrates_exhausted:"


def _key_fingerprint() -> str:
    import hashlib
    key = os.getenv("ALLRATES_API_KEY") or ""
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


def _load_persisted_disable() -> str | None:
    fp = _key_fingerprint()
    if not fp:
        return None
    try:
        from lib.api_cache import _get_row
        payload, _ = _get_row(f"{_QUOTA_FLAG_PREFIX}{fp}")
        if isinstance(payload, dict):
            return payload.get("reason")
    except Exception:
        pass
    return None


def _persist_disable(reason: str) -> None:
    fp = _key_fingerprint()
    if not fp:
        return
    try:
        from lib.api_cache import put_cached
        put_cached(f"{_QUOTA_FLAG_PREFIX}{fp}", {"reason": reason})
    except Exception:
        pass

_FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY", "HKD",
    "SGD", "SEK", "NOK", "DKK", "INR", "MXN", "BRL", "ZAR", "KRW", "TRY",
    "PLN", "THB",
}


def fx_pair_from_symbol(symbol: str) -> tuple[str, str] | None:
    """'EURUSD=X' -> ('EUR','USD'); 'JPY=X' -> ('USD','JPY'); None if not FX."""
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


def _cached(key: str):
    hit = _cache.get(key)
    if hit and (datetime.now(timezone.utc) - hit[0]).total_seconds() < CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _store(key: str, value):
    _cache[key] = (datetime.now(timezone.utc), value)
    return value


def fetch_rates(base: str, symbols: list[str]) -> dict | None:
    """Every requested pair against `base` in ONE request."""
    key = f"latest:{base}:{','.join(sorted(symbols))}"
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        r = httpx.get(f"{FRANKFURTER_BASE}/latest",
                      params={"base": base, "symbols": ",".join(symbols)}, timeout=20)
        if r.status_code == 200:
            return _store(key, r.json())
        logger.info(f"[FX] Frankfurter latest -> {r.status_code}: {r.text[:100]}")
    except Exception as e:
        logger.debug(f"[FX] Frankfurter latest failed: {e}")
    return _store(key, None)


def fetch_series(base: str, symbols: list[str], days: int = 30) -> dict | None:
    """Historical series for every pair in ONE request."""
    key = f"series:{base}:{','.join(sorted(symbols))}:{days}"
    hit = _cached(key)
    if hit is not None:
        return hit
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    try:
        r = httpx.get(f"{FRANKFURTER_BASE}/{start}..{end}",
                      params={"base": base, "symbols": ",".join(symbols)}, timeout=25)
        if r.status_code == 200:
            return _store(key, r.json())
        logger.info(f"[FX] Frankfurter series -> {r.status_code}")
    except Exception as e:
        logger.debug(f"[FX] Frankfurter series failed: {e}")
    return _store(key, None)


def allrates_quota_state() -> dict:
    """Whether the optional AllRatesToday provider is usable, and why not."""
    global _allrates_disabled_reason
    if not os.getenv("ALLRATES_API_KEY"):
        return {"configured": False, "usable": False, "reason": "no ALLRATES_API_KEY set"}
    if _allrates_disabled_reason is None:
        _allrates_disabled_reason = _load_persisted_disable()
    if _allrates_disabled_reason:
        return {"configured": True, "usable": False, "reason": _allrates_disabled_reason}
    return {"configured": True, "usable": True, "reason": None}


def allrates_rate(source: str, target: str) -> dict | None:
    """One AllRatesToday quote. Disables itself permanently on a 429 — the
    free tier's 300-request cap never resets, so retrying only wastes time."""
    global _allrates_disabled_reason
    key = os.getenv("ALLRATES_API_KEY")
    if not key or _allrates_disabled_reason:
        return None
    try:
        r = httpx.get(f"{ALLRATES_BASE}/rate", params={"source": source, "target": target},
                      headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
                      timeout=20)
        if r.status_code == 429:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            _allrates_disabled_reason = (
                f"quota exhausted ({body.get('used', '?')}/{body.get('limit', '?')} on the "
                f"{body.get('plan', 'free')} plan, resets: {body.get('resets_at', 'never')})"
            )
            logger.warning(f"[FX] AllRatesToday disabled — {_allrates_disabled_reason}")
            _persist_disable(_allrates_disabled_reason)
            return None
        if r.status_code == 200:
            return r.json()
        logger.info(f"[FX] AllRatesToday /rate -> {r.status_code}")
    except Exception as e:
        logger.debug(f"[FX] AllRatesToday failed: {e}")
    return None


def fx_summary_block(symbol: str) -> str | None:
    """Compact FX context for LLM prompts, or None when not an FX symbol."""
    pair = fx_pair_from_symbol(symbol)
    if not pair:
        return None
    src, tgt = pair
    latest = fetch_rates(src, [tgt])
    rate = ((latest or {}).get("rates") or {}).get(tgt)
    if rate is None:
        return None
    lines = [f"{src}/{tgt}: {rate} (ECB reference, {(latest or {}).get('date', 'unknown date')})"]
    series = fetch_series(src, [tgt], days=30)
    points = sorted(((series or {}).get("rates") or {}).items())
    if len(points) >= 2:
        first_v = points[0][1].get(tgt)
        last_v = points[-1][1].get(tgt)
        if first_v and last_v:
            lines.append(f"30d change: {(last_v - first_v) / first_v * 100:+.2f}%")
    return "FX RATES (ECB reference via Frankfurter — daily, not live ticks):\n" + "\n".join(lines)
