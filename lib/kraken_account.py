"""Read-only account access on Kraken Pro.

Deliberately READ-ONLY. This module can query balances, open orders, open
positions, trade history and the operator's own fee tier. It contains no
order placement, no cancellation, and no withdrawal — not because the API
lacks them, but because there is no Kraken sandbox: every authenticated
write hits the live account with real money. Execution, if it is ever
built, belongs in a separate module added deliberately, not reachable by
accident from a data path.

Kraken's private endpoints are signed, not bearer-authenticated:

    signature = HMAC-SHA512(
        key    = base64_decode(API_SECRET),
        message= URI_PATH + SHA256(nonce + urlencoded_POST_body)
    )

The nonce must strictly increase across calls, so it is derived from the
clock in milliseconds. A clock that jumps backwards will produce "Invalid
nonce" errors — that is Kraken's guard against replay, not a bug here.

Credentials come from the environment (KRAKEN_API_KEY / KRAKEN_API_SECRET)
and are never logged, echoed, or persisted. If they are absent, every
function returns a stated reason rather than raising, so the desk degrades
to "not connected" instead of crashing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

KRAKEN_API = "https://api.kraken.com"

# Endpoints this module is permitted to call. Anything that could alter the
# account is absent by construction — an allowlist is harder to defeat by
# accident than a blocklist.
READ_ONLY_ENDPOINTS = {
    "/0/private/Balance",
    "/0/private/TradeBalance",
    "/0/private/OpenOrders",
    "/0/private/ClosedOrders",
    "/0/private/OpenPositions",
    "/0/private/TradesHistory",
    "/0/private/TradeVolume",
    "/0/private/Ledgers",
}


def credentials() -> tuple[str | None, str | None]:
    return os.getenv("KRAKEN_API_KEY"), os.getenv("KRAKEN_API_SECRET")


def is_configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def _sign(uri_path: str, data: dict, secret: str) -> str:
    """Kraken's API-Sign header. Wrong ordering here is the usual cause of
    'Invalid key' — the nonce must appear in BOTH the body and the hash."""
    post_data = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + post_data).encode()
    message = uri_path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _private(endpoint: str, params: dict | None = None) -> dict:
    """One signed read. Returns {"ok": bool, ...} and never raises."""
    if endpoint not in READ_ONLY_ENDPOINTS:
        # A programming error, not a runtime condition: fail loudly.
        raise ValueError(
            f"{endpoint} is not in the read-only allowlist. This module does "
            f"not place, cancel, or modify orders by design."
        )

    key, secret = credentials()
    if not (key and secret):
        return {"ok": False, "reason": "KRAKEN_API_KEY / KRAKEN_API_SECRET not set"}

    data = dict(params or {})
    data["nonce"] = int(time.time() * 1000)
    try:
        r = httpx.post(
            f"{KRAKEN_API}{endpoint}",
            data=data,
            headers={
                "API-Key": key,
                "API-Sign": _sign(endpoint, data, secret),
                "User-Agent": "jarvis-trading/7.0 (read-only)",
            },
            timeout=25,
        )
    except Exception as e:
        return {"ok": False, "reason": f"request failed: {str(e)[:80]}"}

    if r.status_code != 200:
        return {"ok": False, "reason": f"HTTP {r.status_code}"}
    body = r.json()
    errors = body.get("error") or []
    if errors:
        # Never echo the key back, even on an auth failure.
        return {"ok": False, "reason": "; ".join(str(e) for e in errors)}
    return {"ok": True, "result": body.get("result") or {}}


def check_connection() -> dict:
    """Is the key valid, and is it genuinely read-only?

    Verifying the PERMISSION is the point: a key with trading rights would
    work here identically, so the operator should confirm what they issued.
    This reports what the key can read and reminds that write scopes cannot
    be detected from a successful read.
    """
    if not is_configured():
        return {"connected": False, "reason": "no credentials in .env"}
    out = _private("/0/private/Balance")
    if not out["ok"]:
        return {"connected": False, "reason": out["reason"]}
    balances = {k: float(v) for k, v in (out["result"] or {}).items() if float(v) != 0}
    return {
        "connected": True,
        "assets_with_balance": len(balances),
        "note": ("A successful read does NOT prove the key lacks trade permission. "
                 "Confirm in Kraken that only 'Query' scopes were granted."),
    }


def balances() -> dict:
    out = _private("/0/private/Balance")
    if not out["ok"]:
        return out
    return {"ok": True, "balances": {k: float(v) for k, v in out["result"].items()
                                     if float(v) != 0}}


def open_positions() -> dict:
    """Leveraged spot positions (Kraken margin), not futures."""
    out = _private("/0/private/OpenPositions", {"docalcs": "true"})
    if not out["ok"]:
        return out
    rows = []
    for pos_id, p in (out["result"] or {}).items():
        rows.append({
            "id": pos_id,
            "pair": p.get("pair"),
            "side": p.get("type"),
            "volume": float(p.get("vol") or 0),
            "cost": float(p.get("cost") or 0),
            "fee": float(p.get("fee") or 0),
            "unrealized_pnl": float(p.get("net") or 0),
            "leverage": p.get("terms"),
        })
    return {"ok": True, "positions": rows}


def open_orders() -> dict:
    out = _private("/0/private/OpenOrders")
    if not out["ok"]:
        return out
    orders = (out["result"] or {}).get("open") or {}
    rows = []
    for oid, o in orders.items():
        d = o.get("descr") or {}
        rows.append({
            "id": oid,
            "pair": d.get("pair"),
            "side": d.get("type"),
            "order_type": d.get("ordertype"),
            "price": d.get("price"),
            "volume": float(o.get("vol") or 0),
            "executed": float(o.get("vol_exec") or 0),
            "status": o.get("status"),
        })
    return {"ok": True, "orders": rows}


def fee_tier(pair: str = "XXBTZUSD") -> dict:
    """The operator's ACTUAL fee tier and 30-day volume.

    This replaces a guess with a fact: lib/venues defaults to the most
    expensive tier because assuming a discount understates cost. Once this
    reports real volume, VENUE_30D_VOLUME_USD can be set from measurement
    instead of assumption.
    """
    out = _private("/0/private/TradeVolume", {"pair": pair})
    if not out["ok"]:
        return out
    res = out["result"] or {}
    fees = (res.get("fees") or {}).get(pair) or {}
    fees_maker = (res.get("fees_maker") or {}).get(pair) or {}
    return {
        "ok": True,
        "volume_30d_usd": float(res.get("volume") or 0),
        "taker_pct": float(fees.get("fee") or 0),
        "maker_pct": float(fees_maker.get("fee") or 0),
        "next_tier_volume": float(fees.get("nextvolume") or 0) or None,
        "next_tier_fee": float(fees.get("nextfee") or 0) or None,
    }
