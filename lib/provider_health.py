"""Is this provider working, and how would anyone know?

WHAT WAS MISSING. `intelligence_source_health` tracks the 43 RSS news feeds
and nothing else. Every paid API — TwelveData, Massive, Helius, LunarCrush,
CoinGecko, FRED, EIA, AllRates, OpenFIGI, Tavily, Bitnomial — had no health
record at all. The only way to answer "is TwelveData authenticating?" or "is
Massive rate-limited?" was to SSH in and hand-probe, which is how the 429 on
Massive and the 402 on LunarCrush went unnoticed while the subscriptions
were being paid for.

HEALTH IS PER CAPABILITY, NOT PER PROVIDER. Kraken's public market data can
be perfectly healthy while its private trading capability is deliberately
disabled, and calling "Kraken" healthy or unhealthy would be false either
way. The key is (provider, capability).

FAILURES ARE NOT INTERCHANGEABLE. Collapsing everything into ERROR loses the
only thing the operator needs to know — what to DO about it:

    RATE_LIMITED     back off; the plan is being outrun
    AUTH_FAILED      the credential is wrong or revoked
    PAYMENT_REQUIRED the credential is fine; the subscription is not active
    STALE            it answers, but the data is too old to use
    DEGRADED         partial answers, or intermittent failure
    UNAVAILABLE      the service cannot be reached
    DISABLED         deliberately off — not a fault
    NOT_CONFIGURED   no credential present
    HEALTHY          answering, fresh

`PAYMENT_REQUIRED` earns its own status because it was a real, live finding:
a valid key returning HTTP 402 looks like an auth failure and is not one.
Retrying harder cannot fix it and neither can any code — only the account
owner can.

NEVER STORE A SECRET. Errors are sanitised before they are persisted: keys
appear in query strings, in Authorization headers echoed back by chatty
services, and in exception messages that quote the request URL. Anything
that looks like a credential is redacted before it reaches the database.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
STALE = "STALE"
RATE_LIMITED = "RATE_LIMITED"
AUTH_FAILED = "AUTH_FAILED"
PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
UNAVAILABLE = "UNAVAILABLE"
DISABLED = "DISABLED"
NOT_CONFIGURED = "NOT_CONFIGURED"

STATUSES = (HEALTHY, DEGRADED, STALE, RATE_LIMITED, AUTH_FAILED,
            PAYMENT_REQUIRED, UNAVAILABLE, DISABLED, NOT_CONFIGURED)

# A status the operator must act on, as opposed to one that is merely a fact.
ACTIONABLE = frozenset({AUTH_FAILED, PAYMENT_REQUIRED, RATE_LIMITED,
                        UNAVAILABLE, STALE, DEGRADED})

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|bearer)"
               r"([=:\s]+)([A-Za-z0-9\-_\.]{8,})"),
    re.compile(r"(?i)\b([A-Za-z0-9]{32,})\b"),      # bare long opaque strings
)


# ── Dropped writes. TELEMETRY MAY BE BEST-EFFORT; ITS LOSS MAY NOT BE
# INVISIBLE. `record` deliberately gives up after 250ms rather than holding
# up economic execution behind SQLite's write lock — but a health row that
# silently vanishes is how a dead primary provider comes to look healthy,
# which is the exact failure this module exists to prevent.
#
# In-memory and bounded: this is a counter, not a second telemetry system,
# and it is surfaced through the existing provider-health API rather than a
# new one.
_dropped: dict = {"count": 0, "last_at": None, "last_error": None,
                  "by_provider": {}}


def record_drop(provider: str, capability: str, error: str) -> None:
    """A health write that did not land. Counted, never swallowed silently."""
    _dropped["count"] += 1
    _dropped["last_at"] = _utc()
    _dropped["last_error"] = sanitize(str(error), 200)
    key = f"{provider}/{capability}"
    _dropped["by_provider"][key] = _dropped["by_provider"].get(key, 0) + 1
    logger.warning("[ProviderHealth] DROPPED health write for %s/%s (%s) — "
                   "telemetry lost, execution unaffected", provider,
                   capability, str(error)[:120])


def dropped_writes() -> dict:
    """How much health telemetry was lost, and when. For the Ops surface."""
    return {
        "count": _dropped["count"],
        "last_at": _dropped["last_at"],
        "last_error": _dropped["last_error"],
        "by_provider": dict(_dropped["by_provider"]),
        "any_dropped": _dropped["count"] > 0,
        "meaning": ("health writes give up after 250ms rather than blocking "
                    "economic execution; a non-zero count means some health "
                    "observations are missing and a provider's status may be "
                    "staler than it looks"),
    }


def reset_dropped_writes() -> None:
    """Test seam. Never called by runtime."""
    _dropped["count"] = 0
    _dropped["last_at"] = None
    _dropped["last_error"] = None
    _dropped["by_provider"] = {}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize(text: str | None, limit: int = 400) -> str | None:
    """Strip anything that could be a credential.

    Deliberately aggressive. A redacted diagnostic is inconvenient; a
    persisted API key is an incident, and provider errors quote request
    URLs — which is exactly where keys live.
    """
    if not text:
        return None
    out = str(text)
    out = _SECRET_PATTERNS[0].sub(r"\1\2<redacted>", out)
    out = _SECRET_PATTERNS[1].sub("<redacted>", out)
    return out[:limit]


def classify_http(status_code: int | None, body: str | None = None) -> str:
    """Turn a transport result into a status the operator can act on."""
    if status_code is None:
        return UNAVAILABLE
    if status_code == 402:
        return PAYMENT_REQUIRED
    if status_code == 429:
        return RATE_LIMITED
    if status_code in (401, 403):
        # 403 is ambiguous — some providers use it for quota. Let the body
        # break the tie when it says so plainly.
        low = (body or "").lower()
        if "subscription" in low or "payment" in low or "plan" in low:
            return PAYMENT_REQUIRED
        if "rate" in low and "limit" in low:
            return RATE_LIMITED
        return AUTH_FAILED
    if 200 <= status_code < 300:
        return HEALTHY
    if 500 <= status_code:
        return UNAVAILABLE
    return DEGRADED


def rate_limit_from_headers(headers) -> dict:
    """Pull quota out of whatever shape the provider chose.

    There is no standard. LunarCrush sends x-rate-limit-day-remaining,
    others send X-RateLimit-Remaining, others Retry-After and nothing else.
    Read what is there; invent nothing.
    """
    if not headers:
        return {}
    low = {str(k).lower(): v for k, v in dict(headers).items()}
    out: dict = {}

    def first(*names):
        for n in names:
            if n in low:
                return low[n]
        return None

    remaining = first("x-rate-limit-day-remaining", "x-ratelimit-remaining",
                      "x-rate-limit-remaining", "ratelimit-remaining")
    limit = first("x-rate-limit-day", "x-ratelimit-limit",
                  "x-rate-limit-limit", "ratelimit-limit")
    reset = first("x-rate-limit-day-reset", "x-ratelimit-reset",
                  "x-rate-limit-reset", "ratelimit-reset", "retry-after")
    for key, val in (("rate_limit_remaining", remaining),
                     ("rate_limit_limit", limit)):
        if val is not None:
            try:
                out[key] = int(float(val))
            except (TypeError, ValueError):
                pass
    if reset is not None:
        out["rate_limit_reset_at"] = str(reset)
    minute_remaining = first("x-rate-limit-minute-remaining")
    if minute_remaining is not None:
        out["minute_remaining"] = str(minute_remaining)
    return out


def record(provider: str, capability: str, *, status: str,
           http_status: int | None = None, latency_ms: float | None = None,
           error: str | None = None, provider_data_at: str | None = None,
           rows: int | None = None, headers=None, detail: str | None = None
           ) -> dict:
    """Persist one health observation. Never raises into a caller.

    Health telemetry that can break a collection job is worse than no
    telemetry, so every failure here is swallowed and logged.
    """
    from app.database import ProviderHealth, get_db

    now = _utc()
    fields = dict(
        provider=provider, capability=capability, status=status,
        last_attempt_at=now,
        last_http_status=http_status,
        latency_ms=float(latency_ms) if latency_ms is not None else None,
        error_detail=sanitize(error),
        provider_data_at=provider_data_at,
        last_rows=rows,
        detail=sanitize(detail, 200),
        updated_at=now,
    )
    fields.update(rate_limit_from_headers(headers))

    try:
        from sqlalchemy import text
        with get_db() as db:
            # TELEMETRY MUST NEVER BLOCK THE CALLER, and on SQLite it can:
            # this opens a SECOND connection, so a caller already holding
            # the write lock makes this wait out the engine's 30s busy
            # timeout. That is not a deadlock anybody notices — it is a
            # process that mysteriously takes half a minute per call, which
            # is exactly how it was found, twice.
            #
            # A quarter second, then give up. Losing a health row is a small
            # honest cost; stalling a fee estimate to record one is not, and
            # the surrounding try/except already treats failure as
            # acceptable.
            db.execute(text("PRAGMA busy_timeout = 250"))
            row = db.query(ProviderHealth).filter(
                ProviderHealth.provider == provider,
                ProviderHealth.capability == capability).first()
            if row is None:
                row = ProviderHealth(provider=provider, capability=capability,
                                     success_count=0, failure_count=0,
                                     consecutive_failures=0)
                db.add(row)
            for k, v in fields.items():
                if v is not None or k in ("error_detail", "detail"):
                    setattr(row, k, v)
            if status == HEALTHY:
                row.success_count = (row.success_count or 0) + 1
                row.consecutive_failures = 0
                row.last_success_at = now
                if rows:
                    row.last_data_at = now
            else:
                row.failure_count = (row.failure_count or 0) + 1
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                row.last_failure_at = now
            db.commit()
    except Exception as exc:                        # noqa: BLE001
        # NOT SILENT. The write is abandoned so execution never waits on
        # telemetry, and the abandonment is counted so a missing health
        # record cannot be mistaken for a healthy provider.
        record_drop(provider, capability, exc)
    return fields


def snapshot() -> list[dict]:
    """Everything known, for the API and the UI."""
    from app.database import ProviderHealth, get_db

    out: list[dict] = []
    try:
        with get_db() as db:
            rows = db.query(ProviderHealth).order_by(
                ProviderHealth.provider, ProviderHealth.capability).all()
            now = datetime.now(timezone.utc)
            for r in rows:
                age = None
                stamp = r.last_data_at or r.last_success_at
                if stamp:
                    try:
                        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=timezone.utc)
                        age = round((now - d).total_seconds(), 1)
                    except ValueError:
                        pass
                out.append({
                    "provider": r.provider, "capability": r.capability,
                    "status": r.status,
                    "actionable": r.status in ACTIONABLE,
                    "last_success_at": r.last_success_at,
                    "last_failure_at": r.last_failure_at,
                    "last_data_at": r.last_data_at,
                    "provider_data_at": r.provider_data_at,
                    "age_seconds": age,
                    "latency_ms": r.latency_ms,
                    "http_status": r.last_http_status,
                    "consecutive_failures": r.consecutive_failures,
                    "success_count": r.success_count,
                    "failure_count": r.failure_count,
                    "rate_limit_remaining": r.rate_limit_remaining,
                    "rate_limit_limit": r.rate_limit_limit,
                    "rate_limit_reset_at": r.rate_limit_reset_at,
                    "last_rows": r.last_rows,
                    "error": r.error_detail,
                    "detail": r.detail,
                    "updated_at": r.updated_at,
                })
    except Exception as exc:                        # noqa: BLE001
        logger.debug("[ProviderHealth] snapshot failed: %s", exc)
    return out
