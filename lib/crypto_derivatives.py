"""
Crypto perpetual-futures intelligence — funding rate, open interest, long/short
account ratio, and liquidations.

Source: OKX's public REST API (api.okx.com), free and unauthenticated. Verified
live while building this: Binance's futures API (fapi.binance.com) is fully
geo-blocked from this deployment ("Service unavailable from a restricted
location"), and Binance.US does not offer derivatives products at all. Bybit's
public REST is blocked at the CloudFront layer for this deployment's region.
OKX was the only one of the three that actually returned real data, so it's
the sole source here — no other vendor, no fabricated numbers for the blocked
exchanges.

What this deliberately does NOT claim: OI-vs-price classification (long/short
buildup/unwinding) is a standard, mechanical futures-market convention, not an
invented signal — see classify_oi_price_action's docstring. It is reported as
a classification of what happened, not a prediction of what happens next.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = float(os.getenv("CRYPTO_API_TIMEOUT", "8"))
OKX_BASE = "https://www.okx.com"

# Only majors with genuinely liquid OKX perpetuals — thin-book contracts would
# make funding/OI/liquidation numbers noisy to the point of being misleading.
DEFAULT_DERIVATIVES_WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


def _get(path: str, params: dict[str, Any]) -> dict:
    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": "JarvisTradingAI/6.8 (+local)"}) as client:
        resp = client.get(f"{OKX_BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def to_okx_inst_id(base_symbol: str) -> str:
    """'BTC' or 'BTC/USD' -> 'BTC-USDT-SWAP' (OKX's USDT-margined perpetual)."""
    base = base_symbol.upper().split("/")[0].strip()
    return f"{base}-USDT-SWAP"


def parse_funding_rate(data: dict) -> dict | None:
    rows = data.get("data") or []
    if not rows:
        return None
    row = rows[0]
    try:
        return {
            "funding_rate": float(row["fundingRate"]),
            "funding_time": _ms_to_iso(row.get("fundingTime")),
            "next_funding_time": _ms_to_iso(row.get("nextFundingTime")) if row.get("nextFundingTime") else None,
        }
    except (KeyError, ValueError, TypeError):
        return None


def parse_open_interest(data: dict) -> dict | None:
    rows = data.get("data") or []
    if not rows:
        return None
    row = rows[0]
    try:
        return {
            "open_interest_contracts": float(row["oi"]),
            "open_interest_usd": float(row["oiUsd"]),
        }
    except (KeyError, ValueError, TypeError):
        return None


def parse_long_short_ratio(data: dict) -> dict | None:
    rows = data.get("data") or []
    if not rows:
        return None
    ts, ratio = rows[0]
    try:
        return {"ratio": float(ratio), "ts": _ms_to_iso(ts)}
    except (ValueError, TypeError):
        return None


def parse_liquidations(data: dict, symbol: str, inst_id: str, contract_value: float = 1.0) -> list[dict]:
    """Flatten OKX liquidation-orders into rows.

    contract_value (OKX "ctVal") is REQUIRED for a correct notional. OKX quotes
    liquidation size in CONTRACTS, not coins, and the contract size differs per
    instrument — verified live against OKX's instruments endpoint:

        BTC-USDT-SWAP   ctVal = 0.01  BTC
        ETH-USDT-SWAP   ctVal = 0.1   ETH
        SOL-USDT-SWAP   ctVal = 1     SOL
        XRP-USDT-SWAP   ctVal = 100   XRP
        DOGE-USDT-SWAP  ctVal = 1000  DOGE

    Treating sz as coins (the original bug here) overstated BTC notionals 100x
    and ETH 10x, while UNDERSTATING XRP 100x and DOGE 1000x. size_coins is
    stored alongside the raw contract count so the two are never confused again.
    """
    groups = data.get("data") or []
    ct_val = contract_value if contract_value and contract_value > 0 else 1.0
    out = []
    for group in groups:
        for d in group.get("details") or []:
            try:
                price = float(d["bkPx"])
                contracts = float(d["sz"])
                size_coins = contracts * ct_val
                out.append({
                    "symbol": symbol,
                    "inst_id": inst_id,
                    "side": d.get("side"),
                    "pos_side": d.get("posSide"),
                    "price": price,
                    "size": contracts,          # raw contract count, as reported
                    "size_coins": size_coins,   # contracts x ctVal
                    "contract_value": ct_val,
                    "notional_usd": round(price * size_coins, 2),
                    "liquidated_at": _ms_to_iso(d.get("ts")),
                })
            except (KeyError, ValueError, TypeError):
                continue
    return out


_contract_value_cache: dict[str, float] = {}


def fetch_contract_value(inst_id: str) -> float | None:
    """OKX contract size (ctVal) for a SWAP instrument, cached in-process.

    Contract specs are effectively static, so this is fetched once per
    instrument per process. Returns None on failure — callers must NOT fall
    back to 1.0 silently, because that reintroduces the exact unit error this
    exists to prevent."""
    if inst_id in _contract_value_cache:
        return _contract_value_cache[inst_id]
    try:
        data = _get("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})
        rows = data.get("data") or []
        if not rows:
            return None
        ct_val = float(rows[0]["ctVal"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.warning(f"[CryptoDerivatives] ctVal lookup failed for {inst_id}: {e}")
        return None
    _contract_value_cache[inst_id] = ct_val
    return ct_val


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def fetch_derivatives_snapshot(symbol: str) -> dict | None:
    """One live pull of funding rate + OI + long/short ratio for a symbol.
    Returns None if OKX doesn't have a listed perpetual for it (not every
    symbol in the app's crypto universe has an OKX-USDT-SWAP contract)."""
    inst_id = to_okx_inst_id(symbol)
    try:
        funding = parse_funding_rate(_get("/api/v5/public/funding-rate", {"instId": inst_id}))
        oi = parse_open_interest(_get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst_id}))
        ls = parse_long_short_ratio(_get(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
            {"instId": inst_id, "period": "5m", "limit": "1"},
        ))
    except httpx.HTTPError as e:
        logger.debug(f"[CryptoDerivatives] Fetch failed for {symbol}: {e}")
        return None
    if not funding and not oi:
        return None
    return {
        "symbol": symbol.upper().split("/")[0],
        "inst_id": inst_id,
        "funding_rate": funding["funding_rate"] if funding else None,
        "next_funding_time": funding.get("next_funding_time") if funding else None,
        "open_interest_usd": oi["open_interest_usd"] if oi else None,
        "long_short_ratio": ls["ratio"] if ls else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_recent_liquidations(symbol: str, limit: int = 100) -> list[dict]:
    base = symbol.upper().split("/")[0]
    inst_id = to_okx_inst_id(symbol)
    # Without ctVal the notional would be wrong by orders of magnitude in either
    # direction depending on the instrument, so skip rather than emit bad numbers.
    ct_val = fetch_contract_value(inst_id)
    if ct_val is None:
        logger.warning(f"[CryptoDerivatives] Skipping {symbol} liquidations — contract value unavailable")
        return []
    try:
        data = _get("/api/v5/public/liquidation-orders", {
            "instType": "SWAP", "instFamily": f"{base}-USDT", "state": "filled", "limit": str(limit),
        })
    except httpx.HTTPError as e:
        logger.debug(f"[CryptoDerivatives] Liquidations fetch failed for {symbol}: {e}")
        return []
    return parse_liquidations(data, base, inst_id, contract_value=ct_val)


def classify_oi_price_action(oi_change_pct: float | None, price_change_pct: float | None) -> str | None:
    """Standard futures-market OI/price convention (not an invented signal):
    OI up + price up = Long Buildup; OI up + price down = Short Buildup;
    OI down + price up = Short Covering; OI down + price down = Long Unwinding.
    Reports what happened, not a directional prediction."""
    if oi_change_pct is None or price_change_pct is None:
        return None
    oi_up = oi_change_pct > 0
    price_up = price_change_pct > 0
    if oi_up and price_up:
        return "long_buildup"
    if oi_up and not price_up:
        return "short_buildup"
    if not oi_up and price_up:
        return "short_covering"
    return "long_unwinding"


def summarize_liquidations(liquidations: list[dict]) -> dict:
    """Pure aggregation over a liquidation list: total notional by side, so the
    caller can see whether longs or shorts are being blown out more heavily."""
    long_notional = sum(l["notional_usd"] for l in liquidations if l.get("pos_side") == "long")
    short_notional = sum(l["notional_usd"] for l in liquidations if l.get("pos_side") == "short")
    total = long_notional + short_notional
    return {
        "count": len(liquidations),
        "long_liquidated_usd": round(long_notional, 2),
        "short_liquidated_usd": round(short_notional, 2),
        "total_liquidated_usd": round(total, 2),
        "long_liquidation_share": round(long_notional / total, 4) if total else None,
    }
