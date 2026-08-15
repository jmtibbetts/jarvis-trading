"""Centralized Helius access — one client, header auth, measured limits.

Every Helius call in JARVIS goes through here. Scattered `httpx.get` calls
against a provider are how a key ends up in a log and how rate limits
become someone else's problem, so the constructor is the only place that
reads the key and the only place that knows an endpoint's shape.

AUTHENTICATION IS THE `X-Api-Key` HEADER, NEVER `?api-key=`. Both work
(verified live 2026-08-17 against the Wallet API and mainnet RPC), and
they are not equally safe: a key in a query string rides inside every URL,
which means it reaches exception messages, retry logs, and metrics labels
by default. The predecessor module needed a string-replace scrub on its
error path for exactly that reason. A header cannot leak that way, so the
scrub is unnecessary rather than merely present.

PACING mirrors lib/twelvedata._throttled_get — the convention already in
this codebase for a paced third-party API — rather than introducing a
second rate-limiting style.

Endpoint facts here were MEASURED, not read from a spec:
  * batch-identity takes {"addresses": [...]}, not {"wallets": [...]},
    and returns a LIST, one entry per address
  * an unclassified wallet returns {"address": ..., "type": "unknown"}
    with no name or category — the common case, not an error
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

WALLET_API_BASE = "https://api.helius.xyz"
RPC_BASE = "https://mainnet.helius-rpc.com/"
DEVNET_RPC_BASE = "https://devnet.helius-rpc.com/"

# Conservative default spacing. Plans differ and this is NOT a hard-coded
# plan limit (§46.8) — it is a floor the operator raises or lowers.
_MIN_SPACING_S = float(os.getenv("HELIUS_MIN_CALL_SPACING", "0.05") or 0.05)
_MAX_RETRIES = int(os.getenv("HELIUS_MAX_RETRIES", "3") or 3)

_lock = threading.Lock()
_last_call_ts = 0.0

# Outbound call accounting (§34). Kept in-process and cheap; the Ops panel
# reads it. Never keyed by URL, because a URL could carry a secret.
_metrics: dict[str, dict] = {}


class HeliusError(RuntimeError):
    """Any Helius failure, normalized. Carries no URL and no key."""

    def __init__(self, endpoint: str, status: int | None, detail: str):
        self.endpoint, self.status = endpoint, status
        super().__init__(f"{endpoint}: {status or 'ERR'} {detail[:200]}")


class HeliusNotConfigured(HeliusError):
    def __init__(self):
        super().__init__("config", None, "HELIUS_API_KEY is not set")


def api_key() -> str:
    return os.getenv("HELIUS_API_KEY", "").strip()


def configured() -> bool:
    return bool(api_key())


def _record(endpoint: str, ms: float, status: int | None, error: bool,
            rate_limited: bool = False) -> None:
    m = _metrics.setdefault(endpoint, {"calls": 0, "errors": 0, "rate_limited": 0,
                                       "total_ms": 0.0, "max_ms": 0.0,
                                       "last_status": None})
    m["calls"] += 1
    m["total_ms"] += ms
    m["max_ms"] = max(m["max_ms"], ms)
    m["last_status"] = status
    if error:
        m["errors"] += 1
    if rate_limited:
        m["rate_limited"] += 1


def metrics() -> dict:
    """Per-endpoint call accounting for the Ops surface."""
    out = {}
    for k, m in _metrics.items():
        calls = m["calls"] or 1
        out[k] = {**m, "avg_ms": round(m["total_ms"] / calls, 1)}
    return out


def reset_metrics() -> None:
    _metrics.clear()


def _pace() -> None:
    global _last_call_ts
    with _lock:
        wait = _MIN_SPACING_S - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def _request(method: str, url: str, endpoint: str, **kw):
    """One paced, retried, metered call. Raises HeliusError, never httpx's.

    Retries only what retrying can fix: 429 and 5xx. A 401 is a wrong key
    and a 400 is a wrong request — repeating either just spends quota.
    """
    import httpx

    if not configured():
        raise HeliusNotConfigured()
    headers = {"X-Api-Key": api_key(), **(kw.pop("headers", None) or {})}

    delay = 0.5
    last = ""
    for attempt in range(_MAX_RETRIES):
        _pace()
        started = time.time()
        try:
            r = httpx.request(method, url, headers=headers, timeout=30.0, **kw)
        except Exception as e:
            ms = (time.time() - started) * 1000
            _record(endpoint, ms, None, error=True)
            last = f"{type(e).__name__}"
            if attempt == _MAX_RETRIES - 1:
                raise HeliusError(endpoint, None, last) from None
            time.sleep(delay)
            delay *= 2
            continue

        ms = (time.time() - started) * 1000
        limited = r.status_code == 429
        retryable = limited or r.status_code >= 500
        _record(endpoint, ms, r.status_code, error=r.status_code >= 400,
                rate_limited=limited)

        if r.status_code < 400:
            return r
        last = r.text[:200]
        if not retryable or attempt == _MAX_RETRIES - 1:
            raise HeliusError(endpoint, r.status_code, last)
        # Honour Retry-After when the server states one.
        try:
            delay = max(delay, float(r.headers.get("Retry-After") or 0))
        except (TypeError, ValueError):
            pass
        time.sleep(delay)
        delay *= 2
    raise HeliusError(endpoint, None, last or "exhausted retries")


# ── Wallet API ───────────────────────────────────────────────────────────

def wallet_identity(address: str) -> dict:
    """Known-entity classification for one address.

    Returns {"address": ..., "type": "unknown"} for anything unlabelled,
    which is most addresses. Callers must treat that as ordinary.
    """
    r = _request("GET", f"{WALLET_API_BASE}/v1/wallet/{address}/identity",
                 "wallet/identity")
    return r.json() or {}


def batch_identity(addresses: list[str]) -> dict[str, dict]:
    """Classify many addresses in one call, keyed by address.

    The body key is `addresses` (measured — `wallets` returns HTTP 400),
    and the response is a list. Batching matters: classifying the
    counterparties of one busy wallet individually would be dozens of
    calls for data one request returns.
    """
    addrs = [a for a in dict.fromkeys(addresses) if a]
    if not addrs:
        return {}
    out: dict[str, dict] = {}
    # Chunked so one enormous wallet cannot build an unbounded request.
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        r = _request("POST", f"{WALLET_API_BASE}/v1/wallet/batch-identity",
                     "wallet/batch-identity", json={"addresses": chunk})
        rows = r.json()
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("address"):
                    out[row["address"]] = row
    return out


def funded_by(address: str) -> dict:
    """The transfer that first funded this wallet, with the funder's own
    identity. The primitive behind cluster detection — and on its own,
    evidence of nothing more than a shared origin.

    Returns {} when Helius knows of no funding transaction. That is a
    404 on the wire and it is an ANSWER, not a failure: plenty of wallets
    have no indexed funder, and raising would make clustering crash on
    ordinary addresses (measured — the first live wallet tried hit it).
    """
    try:
        r = _request("GET", f"{WALLET_API_BASE}/v1/wallet/{address}/funded-by",
                     "wallet/funded-by")
    except HeliusError as e:
        if e.status == 404:
            return {}
        raise
    return r.json() or {}


def balances(address: str) -> dict:
    r = _request("GET", f"{WALLET_API_BASE}/v1/wallet/{address}/balances",
                 "wallet/balances")
    return r.json() or {}


def balance_at(address: str, mint: str, *, time_s: int | None = None,
               slot: int | None = None) -> dict:
    """Historical balance. EXACTLY ONE selector — the API rejects both."""
    if (time_s is None) == (slot is None):
        raise ValueError("balance_at needs exactly one of time_s or slot")
    params = {"mint": mint}
    params["time" if time_s is not None else "slot"] = (
        time_s if time_s is not None else slot)
    r = _request("GET", f"{WALLET_API_BASE}/v1/wallet/{address}/balance-at",
                 "wallet/balance-at", params=params)
    return r.json() or {}


def transfers(address: str, limit: int = 100) -> dict:
    r = _request("GET", f"{WALLET_API_BASE}/v1/wallet/{address}/transfers",
                 "wallet/transfers", params={"limit": limit})
    return r.json() or {}


# ── JSON-RPC ─────────────────────────────────────────────────────────────

def rpc(method: str, params=None, devnet: bool = False):
    """One Helius JSON-RPC call. Returns `result`, raises on `error`."""
    url = DEVNET_RPC_BASE if devnet else RPC_BASE
    r = _request("POST", url, f"rpc/{method}",
                 json={"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params if params is not None else []})
    body = r.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise HeliusError(f"rpc/{method}", err.get("code"),
                          str(err.get("message"))[:200])
    return (body or {}).get("result")


def health() -> dict:
    """Reachability per service (§96). Never raises — a health check that
    can take the desk down is not a health check."""
    out: dict = {"configured": configured()}
    if not configured():
        out["detail"] = "HELIUS_API_KEY not set"
        return out
    for name, fn in (("rpc", lambda: rpc("getHealth")),
                     ("wallet_api", lambda: wallet_identity(
                         "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"))):
        started = time.time()
        try:
            fn()
            out[name] = {"ok": True, "ms": int((time.time() - started) * 1000)}
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)[:160]}
    return out
