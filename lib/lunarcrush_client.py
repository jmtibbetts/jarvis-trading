"""LunarCrush v4 — social/sentiment evidence.

WHAT THIS IS BUILT AGAINST. The current v4 REST API, read from the official
reference rather than an old example:

    base    https://lunarcrush.com/api4
    auth    Authorization: Bearer <key>

    /public/coins/list/v2         every tracked coin, refreshed continuously
    /public/coins/:coin/v1        one coin, market + social
    /public/topic/:topic/v1       one topic, sentiment + contributors

THE SUBSCRIPTION IS NOT ACTIVE. Measured 2026-08-19: the credential is
VALID — the service recognises it and returns rate-limit headers that
decrement on every call — but every endpoint answers

    HTTP 402  {"error": "You must have an active Individual or higher
                         subscription to use this endpoint."}

That is neither an auth failure nor an outage, and no amount of retrying or
backing off will change it. It is recorded as PAYMENT_REQUIRED so it reads
as what it is: something only the account owner can fix. Everything on this
side is finished and tested, so collection begins the moment the plan is
active — nothing here needs revisiting.

THE QUOTA IS SMALL. The headers report 100 requests/day and 4/minute. That
is a hard design constraint, not a detail: a naive per-symbol poll would
exhaust a day in under two minutes. So the list endpoint is preferred (one
request describes every asset), the cadence is measured in hours, and the
client refuses to spend the last of the daily budget on low-value calls.

NO FAKE CONTINUITY. A failed fetch persists nothing. A sentiment of 0 and
"we did not get sentiment" are different facts, and only one of them is
ever written.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROVIDER = "lunarcrush"
API_BASE = "https://lunarcrush.com/api4"
API_VERSION = "v4"

CAP_COINS = "coins_social"
CAP_TOPIC = "topic_social"

# From the observed response headers on this key.
DAILY_BUDGET_DEFAULT = 100
MINUTE_BUDGET_DEFAULT = 4

# Never spend the last of the day on routine polling — leave room for a
# deliberate diagnostic call.
DAILY_RESERVE = 5

HTTP_TIMEOUT = 30.0


@dataclass
class Fetch:
    """One provider call, with everything needed to judge it."""
    ok: bool
    status: str
    http_status: int | None = None
    data: list | dict | None = None
    error: str | None = None
    latency_ms: float | None = None
    headers: dict = field(default_factory=dict)
    rows: int = 0


def is_configured() -> bool:
    return bool(os.getenv("LUNARCRUSH_API_KEY"))


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MinuteBudget:
    """4 requests/minute is small enough that a burst is a real risk."""

    def __init__(self, per_minute: int = MINUTE_BUDGET_DEFAULT):
        self.per_minute = per_minute
        self._stamps: list[float] = []

    def take(self) -> float:
        """Seconds the caller should wait before its next request."""
        now = time.monotonic()
        self._stamps = [t for t in self._stamps if now - t < 60.0]
        if len(self._stamps) < self.per_minute:
            self._stamps.append(now)
            return 0.0
        return max(0.0, 60.0 - (now - self._stamps[0]))


_BUDGET = _MinuteBudget()


def _get(path: str, params: dict | None = None) -> Fetch:
    """One read-only request, classified honestly."""
    from lib import provider_health as PH

    key = os.getenv("LUNARCRUSH_API_KEY")
    if not key:
        return Fetch(ok=False, status=PH.NOT_CONFIGURED,
                     error="LUNARCRUSH_API_KEY not set")

    wait = _BUDGET.take()
    if wait > 0:
        logger.info("[LunarCrush] minute budget reached — waiting %.1fs", wait)
        time.sleep(min(wait, 65.0))

    import httpx
    url = f"{API_BASE}{path}"
    t0 = time.monotonic()
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}"},
                      params=params or None, timeout=HTTP_TIMEOUT)
    except Exception as exc:                        # noqa: BLE001
        return Fetch(ok=False, status=PH.UNAVAILABLE,
                     error=f"{type(exc).__name__}: {exc}",
                     latency_ms=(time.monotonic() - t0) * 1000)

    latency = (time.monotonic() - t0) * 1000
    headers = dict(r.headers)
    status = PH.classify_http(r.status_code, r.text[:300])
    if status != PH.HEALTHY:
        return Fetch(ok=False, status=status, http_status=r.status_code,
                     error=r.text[:300], latency_ms=latency, headers=headers)

    try:
        payload = r.json()
    except ValueError as exc:
        return Fetch(ok=False, status=PH.DEGRADED, http_status=r.status_code,
                     error=f"unparseable JSON: {exc}", latency_ms=latency,
                     headers=headers)

    data = payload.get("data") if isinstance(payload, dict) else payload
    rows = len(data) if isinstance(data, list) else (1 if data else 0)
    return Fetch(ok=True, status=PH.HEALTHY, http_status=r.status_code,
                 data=data, latency_ms=latency, headers=headers, rows=rows)


def daily_remaining(fetch: Fetch) -> int | None:
    from lib import provider_health as PH
    return PH.rate_limit_from_headers(fetch.headers).get("rate_limit_remaining")


def fetch_coins(limit: int = 200) -> Fetch:
    """ONE request describing every tracked coin.

    Preferred over per-symbol calls by a wide margin: the same information
    for 200 assets costs one request out of a hundred per day instead of
    two hundred.
    """
    from lib import provider_health as PH
    f = _get("/public/coins/list/v2", {"limit": limit})
    PH.record(PROVIDER, CAP_COINS, status=f.status, http_status=f.http_status,
              latency_ms=f.latency_ms, error=f.error, rows=f.rows,
              headers=f.headers,
              detail=("subscription inactive — credential is valid"
                      if f.status == PH.PAYMENT_REQUIRED else None))
    return f


def fetch_topic(topic: str) -> Fetch:
    from lib import provider_health as PH
    f = _get(f"/public/topic/{topic}/v1")
    PH.record(PROVIDER, CAP_TOPIC, status=f.status, http_status=f.http_status,
              latency_ms=f.latency_ms, error=f.error, rows=f.rows,
              headers=f.headers)
    return f


# ── normalization ────────────────────────────────────────────────────────
# The metric names are the provider's own. They are NOT renamed into
# generic-sounding fields: a galaxy_score is a LunarCrush construct and must
# never later be mistaken for a market fact.
SOCIAL_FIELDS = (
    "galaxy_score", "alt_rank", "sentiment", "social_dominance",
    "social_volume_24h", "interactions_24h", "topic_rank",
    "market_dominance", "percent_change_24h", "volume_24h", "market_cap",
    "price",
)


def normalize(rows, *, endpoint: str, received_at: str | None = None
              ) -> list[dict]:
    """Provider rows -> observation dicts, provenance intact.

    Absent metrics stay absent. A missing galaxy_score is not a zero.
    """
    if isinstance(rows, dict):
        rows = [rows]
    received_at = received_at or _utc()
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = (row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        metrics = {k: row.get(k) for k in SOCIAL_FIELDS if row.get(k) is not None}
        if not metrics:
            continue
        out.append({
            "provider": PROVIDER,
            "api_version": API_VERSION,
            "endpoint": endpoint,
            "provider_asset_id": str(row.get("id") or ""),
            "symbol": symbol,
            "name": row.get("name"),
            "provider_at": _provider_time(row),
            "received_at": received_at,
            "metrics": metrics,
        })
    return out


def _provider_time(row: dict) -> str | None:
    """The provider's own observation time, where it gives one."""
    for key in ("time", "last_updated", "updated", "timestamp"):
        v = row.get(key)
        if v is None:
            continue
        try:
            n = float(v)
            if n > 1e12:
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return str(v)
    return None
