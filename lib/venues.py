"""Where a trade executes, and what that venue actually charges.

The cost model assumed one crypto fee for everything — 0.25% taker, 0.15%
maker, which are Alpaca's numbers. Measured live against Kraken's public
AssetPairs endpoint, Kraken's base tier is 0.40% taker and 0.25% maker:
60% more expensive than the model believed. Since P0 made the whole system
reject trades whose costs exceed 0.50R, a fee assumption that is wrong by
60% silently moves the line between "tradeable" and "not".

Fees are also not a constant per venue. Kraken's schedule steps down with
30-day volume (0.40% -> 0.24% by $50k), so the same trade costs different
amounts depending on activity. The tier is configurable and defaults to the
base — the expensive end — because assuming a discount you have not earned
makes trades look cheaper than they are.

Kraken publishes per-pair order minimums and tick sizes too, which is the
same instrument-spec problem already solved for futures in lib/instruments:
a price off-tick or a size below the minimum cannot fill, so simulating one
produces results that are unreachable live.

This module deliberately holds NO credentials and places NO orders. It
answers "what would this cost and is it valid here" so the cost model and
sizing can be venue-correct before any execution integration exists.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

KRAKEN_PUBLIC = "https://api.kraken.com/0/public"
_SPEC_TTL = timedelta(hours=12)      # contract specs change rarely
_pair_cache: dict[str, tuple[datetime, dict]] = {}


# ── Venue fee schedules ──────────────────────────────────────────────────
# Percentages are FRACTIONS of notional, per side. Volume tiers are
# (30-day USD volume, fee) pairs, ascending, exactly as the venue lists them.
VENUE_FEES = {
    "alpaca": {
        "crypto": {"taker": [(0, 0.0025)], "maker": [(0, 0.0015)]},
        "equity": {"taker": [(0, 0.0)], "maker": [(0, 0.0)]},
    },
    # Verified live from Kraken's AssetPairs endpoint (XXBTZUSD), 2026-08.
    "kraken": {
        "crypto": {
            "taker": [(0, 0.0040), (10_000, 0.0035), (50_000, 0.0024),
                      (100_000, 0.0022), (250_000, 0.0020), (500_000, 0.0018),
                      (1_000_000, 0.0016)],
            "maker": [(0, 0.0025), (10_000, 0.0020), (50_000, 0.0014),
                      (100_000, 0.0012), (250_000, 0.0010), (500_000, 0.0008),
                      (1_000_000, 0.0006)],
        },
    },
}

DEFAULT_VENUE = os.getenv("DEFAULT_CRYPTO_VENUE", "alpaca").lower()


def configured_volume_tier() -> float:
    """The operator's 30-day traded volume, used to pick a fee tier.

    Defaults to 0 — the most expensive tier — because claiming a discount
    that has not been earned understates cost, and understated cost is what
    lets an unprofitable trade through the gate.
    """
    try:
        return max(0.0, float(os.getenv("VENUE_30D_VOLUME_USD", "0")))
    except (TypeError, ValueError):
        return 0.0


def fee_for(venue: str, *, maker: bool = False, asset_class: str = "crypto",
            volume_30d: float | None = None) -> tuple[float, str]:
    """(fee as a fraction of notional, explanation) for one side."""
    v = str(venue or DEFAULT_VENUE).lower()
    schedule = VENUE_FEES.get(v, {}).get(asset_class)
    if not schedule:
        # An unknown venue must not silently become free.
        fallback = VENUE_FEES["alpaca"][asset_class]["maker" if maker else "taker"][0][1]
        return fallback, f"unknown venue {v!r} — using alpaca {asset_class} rate as a conservative stand-in"

    tiers = schedule["maker" if maker else "taker"]
    vol = configured_volume_tier() if volume_30d is None else float(volume_30d)
    fee = tiers[0][1]
    hit = tiers[0][0]
    for threshold, rate in tiers:
        if vol >= threshold:
            fee, hit = rate, threshold
        else:
            break
    side = "maker" if maker else "taker"
    return fee, f"{v} {side} {fee * 100:.3g}% at ${hit:,.0f}+ 30d volume"


# ── Kraken instrument specs (live, keyless) ──────────────────────────────
def _to_kraken_pair(symbol: str) -> str:
    """'BTC/USD' -> 'XBTUSD'. Kraken uses XBT for bitcoin and its own
    alt-names; the AssetPairs response is searched by altname so this only
    needs to get close."""
    s = str(symbol or "").upper().replace("/", "").replace("-", "")
    return s.replace("BTC", "XBT", 1) if s.startswith("BTC") else s


def kraken_pair_specs(symbol: str) -> dict | None:
    """Order minimum, tick size and precision for one pair, from Kraken.

    Returns None when the pair is not listed — which is itself useful: a
    signal for a symbol Kraken does not trade cannot be executed there.
    """
    want = _to_kraken_pair(symbol)
    now = datetime.now(timezone.utc)
    cached = _pair_cache.get("all")
    if not cached or (now - cached[0]) > _SPEC_TTL:
        try:
            r = httpx.get(f"{KRAKEN_PUBLIC}/AssetPairs", timeout=25)
            if r.status_code != 200:
                logger.info(f"[Venues] Kraken AssetPairs -> {r.status_code}")
                return None
            data = r.json().get("result") or {}
            _pair_cache["all"] = (now, data)
            cached = _pair_cache["all"]
        except Exception as e:
            logger.debug(f"[Venues] Kraken AssetPairs failed: {e}")
            return None

    for key, spec in cached[1].items():
        if str(spec.get("altname", "")).upper() == want or key.upper() == want:
            return {
                "venue": "kraken",
                "pair": spec.get("altname"),
                "order_min": float(spec.get("ordermin") or 0),
                "tick_size": float(spec.get("tick_size") or 0),
                "price_decimals": int(spec.get("pair_decimals") or 8),
                "qty_decimals": int(spec.get("lot_decimals") or 8),
                "max_leverage": max([int(x) for x in (spec.get("leverage_buy") or [1])] or [1]),
                "taker_tiers": [(int(v), f / 100) for v, f in (spec.get("fees") or [])],
                "maker_tiers": [(int(v), f / 100) for v, f in (spec.get("fees_maker") or [])],
            }
    return None


def is_tradeable_on(venue: str, symbol: str) -> tuple[bool, str]:
    """Whether a venue lists the symbol at all."""
    if str(venue).lower() != "kraken":
        return True, f"{venue}: tradeability not checked"
    spec = kraken_pair_specs(symbol)
    if spec:
        return True, f"kraken lists {spec['pair']}"
    return False, f"kraken does not list {symbol}"


def validate_order(venue: str, symbol: str, qty: float, price: float) -> dict:
    """Would this order be ACCEPTED? Checks size minimum and price tick.

    A simulated fill below the order minimum, or at a price off the tick
    grid, is a fill that could never happen — so paper results built on one
    do not transfer to live trading.
    """
    if str(venue).lower() != "kraken":
        return {"ok": True, "reason": f"{venue}: no venue constraints modelled"}
    spec = kraken_pair_specs(symbol)
    if not spec:
        return {"ok": False, "reason": f"kraken does not list {symbol}"}

    problems = []
    if spec["order_min"] and abs(qty) < spec["order_min"]:
        problems.append(f"size {abs(qty):g} below kraken minimum {spec['order_min']:g}")
    tick = spec["tick_size"]
    if tick and price:
        remainder = abs(round(price / tick) * tick - price)
        if remainder > tick * 1e-6:
            problems.append(
                f"price {price!r} is off the {tick:g} tick grid "
                f"(nearest valid: {round(price / tick) * tick:.{spec['price_decimals']}f})"
            )
    if problems:
        return {"ok": False, "reason": "; ".join(problems), "spec": spec}
    return {"ok": True, "reason": f"valid on kraken ({spec['pair']})", "spec": spec}


# ── Measured spreads ─────────────────────────────────────────────────────
# The cost model assumed 0.10% for crypto. Kraken's real median on BTC is
# 0.0064% — the assumption was 16x too wide, and since spread feeds the
# minimum-viable-stop calculation, it was inflating that floor and
# rejecting trades that were fine.
#
# A conservative default is correct when nothing is known. It is NOT
# correct when a free, keyless, live measurement exists. This fetches the
# real thing and falls back to the default only on failure, so a network
# problem can never make a trade look cheaper than it is.
_SPREAD_TTL = timedelta(minutes=5)     # spreads move fast; cache briefly
_spread_cache: dict[str, tuple[datetime, float | None]] = {}


def measured_spread_pct(symbol: str, venue: str = "kraken") -> tuple[float | None, str]:
    """(median recent spread as a fraction of price, source) or (None, why).

    Uses the MEDIAN of Kraken's recent quotes rather than the mean: a
    momentary blowout during a print should not permanently widen the
    estimate, and the median is what a normal fill actually faces.
    """
    if str(venue).lower() != "kraken":
        return None, f"{venue}: no live spread source wired"

    key = f"{venue}:{symbol}"
    now = datetime.now(timezone.utc)
    hit = _spread_cache.get(key)
    if hit and (now - hit[0]) < _SPREAD_TTL:
        return hit[1], "measured_cached" if hit[1] is not None else "unavailable_cached"

    spec = kraken_pair_specs(symbol)
    if not spec:
        _spread_cache[key] = (now, None)
        return None, f"kraken does not list {symbol}"

    try:
        r = httpx.get(f"{KRAKEN_PUBLIC}/Spread", params={"pair": spec["pair"]}, timeout=20)
        if r.status_code != 200:
            _spread_cache[key] = (now, None)
            return None, f"kraken Spread -> {r.status_code}"
        result = (r.json().get("result") or {})
        quotes = next((v for k, v in result.items() if k != "last"), [])
        rates = []
        for row in quotes:
            try:
                _, bid, ask = row[0], float(row[1]), float(row[2])
                if bid > 0 and ask >= bid:
                    rates.append((ask - bid) / bid)
            except (TypeError, ValueError, IndexError):
                continue
        if not rates:
            _spread_cache[key] = (now, None)
            return None, "no usable quotes returned"
        rates.sort()
        median = rates[len(rates) // 2]
        _spread_cache[key] = (now, median)
        return median, f"measured from {len(rates)} kraken quotes"
    except Exception as e:
        logger.debug(f"[Venues] spread fetch failed for {symbol}: {e}")
        _spread_cache[key] = (now, None)
        return None, f"fetch failed: {str(e)[:60]}"
