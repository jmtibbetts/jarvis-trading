"""Twelve Data REST client — the deep-history source.

Exists for one measured reason: the path model was rejected with 9,246
labels spanning ONE usable day, and no other connected source carries
intraday depth. Twelve Data verified 2026-08-13: NVDA 15min back to
2019-09-16, BTC/USD 15min back to 2020-02-19, split-adjusted correctly
(checked against NVDA's 40x combined split factor).

Plan limits are enforced HERE, not hoped about: basic tier is 8 credits
per minute and 800 per day. One time_series call = 1 credit and returns up
to 5000 bars, so three years of 15-minute bars is ~4 credits per symbol —
a 50-symbol universe backfills inside a single day's budget.

The MCP connector proved the account works; this client exists because the
Jarvis backend cannot call MCP tools, and shipping 250KB CSV responses
through a chat context is not a data pipeline.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"

# Jarvis timeframe -> Twelve Data interval. Deliberately absent: 2D/1W are
# resampled locally from 1D (lib/ohlcv.resample_daily) so their week
# boundary stays under our control, and 3m does not exist at the vendor
# (their intraday ladder is 1/5/15/30/45min) — a 3m backfill request must
# fail loudly rather than fetch something adjacent.
TD_INTERVALS = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1H": "1h", "2H": "2h", "4H": "4h", "1D": "1day",
}

MAX_BARS_PER_CALL = 5000

# Per-minute pacing follows the PLAN, which only the operator knows:
# TWELVEDATA_RPM in .env (8 = free Basic, 55 = Grow, 610 = Pro). The
# margin keeps a long backfill just under the limit instead of tripping
# 429s and retry loops that spend credits on nothing. Setting an RPM the
# plan doesn't actually allow earns exactly those 429s back.
def _plan_rpm() -> int:
    try:
        return max(1, int(os.getenv("TWELVEDATA_RPM", "8")))
    except ValueError:
        return 8


def _min_call_spacing_s() -> float:
    return 60.0 / _plan_rpm() + 0.1

# The backfill must never starve the rest of the system: signals, focus
# scans and anything else that later leans on this client share the same
# daily pool ON THE FREE PLAN. Paid plans have no daily cap, and
# credits_remaining() reports UNLIMITED there, which keeps this floor
# logic inert without a second code path.
DAILY_CREDIT_FLOOR = 50
UNLIMITED_CREDITS = 10**9

_lock = threading.Lock()
_last_call_ts = 0.0


class TwelveDataError(RuntimeError):
    pass


class CreditFloorReached(TwelveDataError):
    """Raised when continuing would eat into the reserved daily credits."""


def _api_key() -> str:
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        raise TwelveDataError("TWELVE_DATA_API_KEY is not set in the environment")
    return key


def _throttled_get(path: str, params: dict) -> dict:
    """One rate-limited request. Serialized across threads because the
    per-minute limit is account-wide, not per-caller."""
    global _last_call_ts
    with _lock:
        wait = _min_call_spacing_s() - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()
    r = httpx.get(f"{BASE_URL}/{path}", params={**params, "apikey": _api_key()},
                  timeout=30.0)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        # Twelve Data reports errors in-band with HTTP 200.
        raise TwelveDataError(f"{path}: {data.get('message', 'unknown error')}")
    return data


def api_usage() -> dict:
    return _throttled_get("api_usage", {})


def credits_remaining() -> int:
    """Daily credits left, or UNLIMITED_CREDITS on plans with no daily cap.

    Paid plans report no meaningful plan_daily_limit; treating that as
    the old 800 default would make the floor trip on day one of a paid
    account — the exact opposite of what the upgrade bought.
    """
    u = api_usage()
    limit = int(u.get("plan_daily_limit") or 0)
    if limit <= 0:
        return UNLIMITED_CREDITS
    return limit - int(u.get("daily_usage", 0))


def earliest_timestamp(symbol: str, timeframe: str) -> datetime | None:
    """How far back this symbol's history goes at this interval."""
    interval = TD_INTERVALS.get(timeframe)
    if not interval:
        return None
    try:
        d = _throttled_get("earliest_timestamp",
                           {"symbol": symbol, "interval": interval})
        return datetime.fromtimestamp(int(d["unix_time"]), tz=timezone.utc)
    except (TwelveDataError, KeyError, ValueError) as e:
        logger.debug(f"[TwelveData] earliest_timestamp {symbol}/{timeframe}: {e}")
        return None


def fetch_series(symbol: str, timeframe: str,
                 start_date: str | None = None,
                 end_date: str | None = None,
                 outputsize: int = MAX_BARS_PER_CALL) -> pd.DataFrame | None:
    """One page of OHLCV, ascending UTC index — the cache's expected shape."""
    interval = TD_INTERVALS.get(timeframe)
    if not interval:
        raise TwelveDataError(f"no Twelve Data interval for timeframe {timeframe!r}")
    params = {"symbol": symbol, "interval": interval,
              "outputsize": min(outputsize, MAX_BARS_PER_CALL),
              "timezone": "UTC"}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    data = _throttled_get("time_series", params)
    values = data.get("values") or []
    if not values:
        return None
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0     # FX pairs come without volume
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    df.attrs["source"] = "twelvedata"
    return df if len(df) else None


def backfill_symbol(symbol: str, timeframe: str, years: float = 3.0,
                    cache_symbol: str | None = None) -> dict:
    """Pull `years` of history for one symbol into the OHLCV cache.

    Pages BACKWARD from now in 5000-bar chunks until the requested span or
    the vendor's earliest bar is reached. Each page is upserted through the
    cache's own _store_bars, so priority rules apply — a backfilled bar
    never clobbers a live Alpaca bar for the same timestamp.

    Idempotent: re-running skips nothing but rewrites identical rows, so a
    backfill interrupted by the credit floor resumes by just running again.
    """
    from lib.ohlcv_cache import _store_bars

    target_start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=365.25 * years)
    floor = earliest_timestamp(symbol, timeframe)
    if floor is not None and pd.Timestamp(floor) > target_start:
        target_start = pd.Timestamp(floor)

    # Resume cheaply: if the cache already reaches the effective target
    # (vendor floor included — a coin listed in 2023 can never satisfy a
    # 2021 ask), this series is done for one local query plus the floor
    # call above, instead of re-paying every page it already fetched.
    # Week of tolerance so slight vendor drift can't force eternal top-ups.
    try:
        from lib.ohlcv_cache import cached_earliest_ts
        have = cached_earliest_ts(cache_symbol or symbol, timeframe)
        if have is not None:
            have_ts = pd.Timestamp(have)
            have_ts = (have_ts.tz_localize("UTC") if have_ts.tzinfo is None
                       else have_ts.tz_convert("UTC"))
            if have_ts <= target_start + pd.Timedelta(days=7):
                return {"bars_stored": 0, "pages": 0,
                        "earliest": str(have_ts),
                        "skipped": "cache already at target depth"}
    except Exception:
        pass  # a failed check costs a re-fetch, never a lost series

    stored = calls = 0
    end_cursor: str | None = None
    earliest_seen = None

    while True:
        if credits_remaining() <= DAILY_CREDIT_FLOOR:
            raise CreditFloorReached(
                f"stopping {symbol}/{timeframe} backfill: daily credits at the "
                f"{DAILY_CREDIT_FLOOR}-credit reserve floor. Re-run tomorrow — "
                f"the backfill resumes where it left off.")
        df = fetch_series(symbol, timeframe, end_date=end_cursor)
        calls += 1
        if df is None or len(df) == 0:
            break
        stored += _store_bars(cache_symbol or symbol, timeframe, df,
                              source="twelvedata") or 0
        earliest_seen = df.index[0]
        logger.info(f"[TwelveData] {symbol}/{timeframe}: page {calls}, "
                    f"{len(df)} bars back to {earliest_seen.date()}")
        if earliest_seen <= target_start or len(df) < MAX_BARS_PER_CALL:
            break
        # Next page ends where this one began. The boundary bar is fetched
        # twice and upserted twice — one duplicate row per 5000 is cheaper
        # than an off-by-one that silently skips a bar.
        end_cursor = earliest_seen.strftime("%Y-%m-%d %H:%M:%S")

    return {"symbol": symbol, "timeframe": timeframe, "pages": calls,
            "bars_stored": stored,
            "earliest": str(earliest_seen) if earliest_seen is not None else None,
            "target_start": str(target_start)}
