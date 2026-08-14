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


# ── Canonical-event emission (Phase 3 P3) ────────────────────────────────────
# Until now every funding/OI number was fetched, served, and discarded —
# history over these observations is exactly what the platform doc's derived
# features (funding-vs-price divergence, cross-venue dispersion drift) need
# and could never have. Emission rides the EXISTING fetch paths: whatever
# polls the snapshot (UI, scanner, analyst) feeds the log as a side effect,
# throttled per (venue, symbol, metric) so a hot dashboard can't multiply
# a slow-moving observation into duplicate rows. Tier-1 symbols only, same
# gate as every other raw persist.
_obs_marks: dict[tuple, float] = {}
OBS_PERSIST_INTERVAL_SEC = 300.0


def _emit_observation(venue: str, symbol: str, metric: str, value,
                      obs_iso: str | None = None) -> None:
    try:
        import time as _time

        from lib.event_store import tier_of
        from lib.market_events import (DerivativesObservation, get_queue,
                                       make_meta, parse_iso_ts)

        if value is None or tier_of(symbol) != 1:
            return
        key = (venue, str(symbol).upper(), metric)
        now = _time.time()
        if now - _obs_marks.get(key, 0.0) < OBS_PERSIST_INTERVAL_SEC:
            return
        _obs_marks[key] = now
        get_queue("derivatives_obs").push(DerivativesObservation(
            meta=make_meta(venue, f"{venue}_rest_v1", parse_iso_ts(obs_iso)),
            symbol=str(symbol).upper().split("/")[0],
            metric=metric, value=float(value)))
    except Exception as e:
        # Serving the caller must survive any persistence problem.
        logger.debug(f"[CryptoDerivatives] observation event skipped: {e}")


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
    # OKX's funding/OI payloads carry no observation time in the fields we
    # parse — None, not fetch time masquerading as the venue's clock. The
    # long/short ratio row does carry its own timestamp.
    _emit_observation("okx", symbol, "funding_rate",
                      funding["funding_rate"] if funding else None)
    _emit_observation("okx", symbol, "open_interest_usd",
                      oi["open_interest_usd"] if oi else None)
    _emit_observation("okx", symbol, "long_short_ratio",
                      ls["ratio"] if ls else None,
                      obs_iso=ls.get("ts") if ls else None)
    return {
        "symbol": symbol.upper().split("/")[0],
        "inst_id": inst_id,
        "funding_rate": funding["funding_rate"] if funding else None,
        "next_funding_time": funding.get("next_funding_time") if funding else None,
        "open_interest_usd": oi["open_interest_usd"] if oi else None,
        "long_short_ratio": ls["ratio"] if ls else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Crypto.com — second venue ────────────────────────────────────────────────
# Public REST, no key, verified live 2026-08-13. A single-venue funding rate
# cannot show DISAGREEMENT between venues, and cross-exchange funding/OI
# dispersion is one of the platform doc's named derived features. Rows are
# tagged venue='cryptocom'; nothing that consumed the OKX-only table sees
# them unless it asks.
#
# Semantics note (the §49 trap): Crypto.com pays funding HOURLY on these
# perps while OKX pays 8-hourly, so the raw rates differ by nature, not by
# market view. Comparisons must normalize; funding_dispersion() below does,
# with the intervals stated.
CRYPTOCOM_BASE = "https://api.crypto.com/exchange/v1"
CRYPTOCOM_FUNDING_INTERVAL_H = 1.0
OKX_FUNDING_INTERVAL_H = 8.0


def to_cryptocom_inst(base_symbol: str) -> str:
    """'BTC' or 'BTC/USD' -> 'BTCUSD-PERP'."""
    return f"{base_symbol.upper().split('/')[0]}USD-PERP"


def parse_cryptocom_ticker(payload: dict) -> dict | None:
    """Crypto.com tickers use one-letter keys: a=last, oi=open interest in
    BASE units (verified: BTC oi 6059 x $63,645 = $385M, a sane number for
    that venue), b/k=bid/ask, t=ms timestamp."""
    try:
        rows = (payload.get("result") or {}).get("data") or []
        if not rows:
            return None
        t = rows[0]
        last = float(t.get("a") or 0)
        oi_base = float(t.get("oi") or 0)
        if last <= 0:
            return None
        return {
            "last": last,
            "open_interest_base": oi_base,
            "open_interest_usd": round(oi_base * last, 2),
            "ts": _ms_to_iso(t.get("t")),
        }
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def parse_cryptocom_funding(payload: dict) -> dict | None:
    """Valuations rows are {v: rate, t: ms}, newest first."""
    try:
        rows = (payload.get("result") or {}).get("data") or []
        if not rows:
            return None
        return {"funding_rate": float(rows[0]["v"]),
                "ts": _ms_to_iso(rows[0].get("t"))}
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def fetch_cryptocom_snapshot(symbol: str) -> dict | None:
    """Funding + OI from Crypto.com for one symbol; None when the venue has
    no such perp or the fetch fails — never a partial guess."""
    inst = to_cryptocom_inst(symbol)
    try:
        with httpx.Client(timeout=10.0) as client:
            tick = parse_cryptocom_ticker(client.get(
                f"{CRYPTOCOM_BASE}/public/get-tickers",
                params={"instrument_name": inst}).json())
            funding = parse_cryptocom_funding(client.get(
                f"{CRYPTOCOM_BASE}/public/get-valuations",
                params={"instrument_name": inst,
                        "valuation_type": "funding_rate", "count": 1}).json())
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"[CryptoDerivatives] cryptocom fetch failed for {symbol}: {e}")
        return None
    if not tick:
        return None
    # Crypto.com stamps both payloads with the venue's own ms clock.
    _emit_observation("cryptocom", symbol, "funding_rate",
                      funding["funding_rate"] if funding else None,
                      obs_iso=funding.get("ts") if funding else None)
    _emit_observation("cryptocom", symbol, "open_interest_usd",
                      tick["open_interest_usd"], obs_iso=tick.get("ts"))
    return {
        "symbol": symbol.upper().split("/")[0],
        "inst_id": inst,
        "venue": "cryptocom",
        "funding_rate": funding["funding_rate"] if funding else None,
        "open_interest_usd": tick["open_interest_usd"],
        "price": tick["last"],
        # Crypto.com's public API has no long/short account ratio; None is
        # the honest value, not a copy of OKX's.
        "long_short_ratio": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def funding_dispersion(okx_rate: float | None, cryptocom_rate: float | None) -> dict | None:
    """Cross-venue funding disagreement, normalized to per-hour rates.

    Raw rates are NOT comparable — OKX pays every 8h, Crypto.com every 1h —
    so each is divided by its own interval first. The sign of the spread
    says which venue's longs are paying more dearly; a large spread means
    the venues disagree about positioning, which single-venue data cannot
    express at all.
    """
    if okx_rate is None or cryptocom_rate is None:
        return None
    okx_hourly = okx_rate / OKX_FUNDING_INTERVAL_H
    cdc_hourly = cryptocom_rate / CRYPTOCOM_FUNDING_INTERVAL_H
    return {
        "okx_hourly": okx_hourly,
        "cryptocom_hourly": cdc_hourly,
        "spread_hourly": okx_hourly - cdc_hourly,
        "intervals_h": {"okx": OKX_FUNDING_INTERVAL_H,
                        "cryptocom": CRYPTOCOM_FUNDING_INTERVAL_H},
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
