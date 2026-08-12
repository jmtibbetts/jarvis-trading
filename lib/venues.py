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
    # Alpaca crypto, full 30-day volume schedule. The base tier was already
    # correct here but the ladder was flat, so any volume discount was
    # invisible to the cost model.
    "alpaca": {
        "crypto": {
            "taker": [(0, 0.0025), (100_000, 0.0022), (500_000, 0.0020),
                      (1_000_000, 0.0018), (10_000_000, 0.0015),
                      (25_000_000, 0.0013), (50_000_000, 0.0012),
                      (100_000_000, 0.0010)],
            "maker": [(0, 0.0015), (100_000, 0.0012), (500_000, 0.0010),
                      (1_000_000, 0.0008), (10_000_000, 0.0005),
                      (25_000_000, 0.0002), (50_000_000, 0.0002),
                      (100_000_000, 0.0000)],
        },
        # Commission-free, but commission is not the whole cost — see
        # equity_regulatory_fee() for the SEC/FINRA charges on the sell side.
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


# The operator's OWN fee, read from their account. Cached because it is a
# signed call and the tier moves slowly.
_account_fee_cache: dict[str, tuple[datetime, dict | None]] = {}
_ACCOUNT_FEE_TTL = timedelta(hours=6)


def account_fee(venue: str = "kraken") -> dict | None:
    """The fee this ACCOUNT actually pays, or None if unreadable.

    Published schedules are a starting point, not the answer. Measured
    against a live Kraken account the real tier was 0.8% taker / 0.4% maker
    where the public BTC schedule reads 0.40% / 0.25% — so pricing from the
    published table understated true cost by half, in the direction that
    lets unprofitable trades through.
    """
    if str(venue).lower() != "kraken":
        return None
    now = datetime.now(timezone.utc)
    hit = _account_fee_cache.get(venue)
    if hit and (now - hit[0]) < _ACCOUNT_FEE_TTL:
        return hit[1]
    try:
        from lib.kraken_account import fee_tier, is_configured
        if not is_configured():
            _account_fee_cache[venue] = (now, None)
            return None
        out = fee_tier()
        if not out.get("ok"):
            _account_fee_cache[venue] = (now, None)
            return None
        measured = {
            "taker": float(out["taker_pct"]) / 100.0,
            "maker": float(out["maker_pct"]) / 100.0,
            "volume_30d": float(out.get("volume_30d_usd") or 0),
        }
        _account_fee_cache[venue] = (now, measured)
        return measured
    except Exception as e:
        logger.debug(f"[Venues] account fee lookup failed: {e}")
        _account_fee_cache[venue] = (now, None)
        return None


def fee_for(venue: str, *, maker: bool = False, asset_class: str = "crypto",
            volume_30d: float | None = None,
            use_account: bool = True) -> tuple[float, str]:
    """(fee as a fraction of notional, explanation) for one side.

    Preference: the account's MEASURED fee, then the published schedule at
    the configured volume tier. A measurement that fails falls back to the
    table rather than to optimism.
    """
    v = str(venue or DEFAULT_VENUE).lower()

    if use_account and volume_30d is None and asset_class == "crypto":
        measured = account_fee(v)
        if measured:
            rate = measured["maker" if maker else "taker"]
            side = "maker" if maker else "taker"
            return rate, (f"{v} {side} {rate * 100:.3g}% — MEASURED from the account "
                          f"(${measured['volume_30d']:,.0f} 30d volume)")

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


# ── Kraken Futures instrument specs (live, keyless) ──────────────────────
# lib/instruments.py holds CME futures specs I typed in from published
# contract documents. This is the same data for crypto derivatives, except
# fetched from the venue itself, so it cannot silently go stale.
#
# The important structure here is TIERED MARGIN: Kraken requires more margin
# as a position grows, so maximum leverage is a function of size, not a
# constant. PF_XBTUSD allows 100x on a small position and 2x above $150m.
# A model that treats max leverage as one number is wrong at both ends.
KRAKEN_FUTURES = "https://futures.kraken.com/derivatives/api/v3"
_futures_cache: dict[str, tuple[datetime, dict]] = {}


def kraken_futures_instruments() -> dict:
    """All tradeable futures keyed by symbol, cached 12h."""
    now = datetime.now(timezone.utc)
    hit = _futures_cache.get("all")
    if hit and (now - hit[0]) < _SPEC_TTL:
        return hit[1]
    try:
        r = httpx.get(f"{KRAKEN_FUTURES}/instruments", timeout=25)
        if r.status_code != 200:
            logger.info(f"[Venues] Kraken Futures instruments -> {r.status_code}")
            return (hit[1] if hit else {})
        data = {i["symbol"]: i for i in (r.json().get("instruments") or [])
                if i.get("tradeable")}
        _futures_cache["all"] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[Venues] Kraken Futures fetch failed: {e}")
        return (hit[1] if hit else {})


def _to_kraken_futures_symbol(symbol: str) -> str:
    """'BTC/USD' -> 'PF_XBTUSD' (perpetual). Kraken prefixes perpetuals with
    PF_ and still calls bitcoin XBT."""
    base = str(symbol or "").upper().split("/")[0].replace("USD", "")
    if base == "BTC":
        base = "XBT"
    return f"PF_{base}USD"


def kraken_futures_spec(symbol: str) -> dict | None:
    """Contract spec for the perpetual matching a spot symbol, or None."""
    want = _to_kraken_futures_symbol(symbol)
    inst = kraken_futures_instruments().get(want)
    if not inst:
        return None
    tiers = [
        {"from_units": float(t.get("numNonContractUnits") or 0),
         "initial_margin": float(t.get("initialMargin") or 0),
         "maintenance_margin": float(t.get("maintenanceMargin") or 0)}
        for t in (inst.get("marginLevels") or [])
    ]
    tiers.sort(key=lambda t: t["from_units"])
    first = tiers[0]["initial_margin"] if tiers else 0
    return {
        "venue": "kraken_futures",
        "symbol": want,
        "type": inst.get("type"),
        "tick_size": float(inst.get("tickSize") or 0),
        "contract_size": float(inst.get("contractSize") or 1),
        "qty_precision": int(inst.get("contractValueTradePrecision") or 0),
        "max_position": float(inst.get("maxPositionSize") or 0),
        "funding_periods_per_day": float(inst.get("fundingRateCoefficient") or 0),
        "margin_tiers": tiers,
        "max_leverage_small": (1.0 / first) if first else 1.0,
    }


def max_leverage_at_size(symbol: str, notional_usd: float) -> tuple[float, str]:
    """Maximum leverage Kraken permits for a position of THIS size.

    Leverage is not a property of the instrument alone — a $500 position on
    PF_XBTUSD may use 100x while a $10m position is capped at 10x. Sizing
    that assumes the headline number would be rejected by the venue at
    scale, so the tier is resolved against the actual notional.
    """
    spec = kraken_futures_spec(symbol)
    if not spec or not spec["margin_tiers"]:
        return 1.0, f"no kraken futures listing for {symbol}"
    tier = spec["margin_tiers"][0]
    for t in spec["margin_tiers"]:
        if notional_usd >= t["from_units"]:
            tier = t
        else:
            break
    im = tier["initial_margin"]
    lev = (1.0 / im) if im else 1.0
    return lev, (f"{spec['symbol']} at ${notional_usd:,.0f}: {im:.1%} initial margin "
                 f"-> {lev:.0f}x max")


# ── Futures fee schedules (live, keyless) ────────────────────────────────
# Derivatives are priced on a completely different scale from spot. Verified
# against Kraken's own /feeschedules endpoint:
#
#   spot  (this account, measured)   taker 0.80%   maker 0.40%
#   futures tier 1                   taker 0.05%   maker 0.02%
#
# Sixteen times cheaper on the taker side. Pricing a leveraged perpetual at
# the spot fee overstates its cost by that factor, which in a system that
# rejects trades above 0.50R means refusing setups that are comfortably
# viable. Each instrument names its own schedule via feeScheduleUid, so this
# resolves the real one rather than assuming a tier.
#
# Note the top tiers carry NEGATIVE maker fees — a rebate for providing
# liquidity. The model must be able to represent being PAID to trade.
_fee_schedule_cache: dict[str, tuple[datetime, dict]] = {}


def kraken_futures_fee_schedules() -> dict:
    """All schedules keyed by uid, cached 12h."""
    now = datetime.now(timezone.utc)
    hit = _fee_schedule_cache.get("all")
    if hit and (now - hit[0]) < _SPEC_TTL:
        return hit[1]
    try:
        r = httpx.get(f"{KRAKEN_FUTURES}/feeschedules", timeout=25)
        if r.status_code != 200:
            return hit[1] if hit else {}
        data = {s["uid"]: s for s in (r.json().get("feeSchedules") or []) if s.get("uid")}
        _fee_schedule_cache["all"] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[Venues] futures fee schedules failed: {e}")
        return hit[1] if hit else {}


def futures_fee_for(symbol: str, *, maker: bool = False,
                    volume_30d: float | None = None,
                    region: str | None = None) -> tuple[float | None, str]:
    """(fee as a fraction of notional, explanation) for a perpetual.

    REGION MATTERS. Kraken states that the cross-platform Futures tiers
    (0.020% maker / 0.050% taker) do NOT apply to US customers, whose
    perpetuals list through Bitnomial at a flat $0.15/contract/side. Using
    the international ladder for a US account understates cost on small
    positions and overstates it on large ones, because the two schedules
    have opposite shapes. Set VENUE_REGION=us to price per contract.

    Resolves the instrument's OWN schedule by uid, then the tier for the
    given 30-day volume. Returns None when the symbol is not a listed
    future, so callers fall back to spot pricing rather than silently
    applying derivative fees to a spot trade.
    """
    import os
    reg = (region or os.getenv("VENUE_REGION") or "international").lower()
    if reg == "us":
        # Per-contract, not per-cent: the caller needs notional and contract
        # count to convert, which percentage-shaped callers do not have.
        return None, ("US account: perpetuals are priced PER CONTRACT "
                      "($0.15/side all-in) — use us_perpetual_fee(), not a "
                      "percentage schedule")

    want = _to_kraken_futures_symbol(symbol)
    inst = kraken_futures_instruments().get(want)
    if not inst:
        return None, f"{symbol} is not a listed kraken perpetual"

    uid = inst.get("feeScheduleUid")
    sched = kraken_futures_fee_schedules().get(uid)
    if not sched or not sched.get("tiers"):
        return None, f"no fee schedule for {want}"

    vol = configured_volume_tier() if volume_30d is None else float(volume_30d)
    tiers = sorted(sched["tiers"], key=lambda t: float(t.get("usdVolume") or 0))
    chosen = tiers[0]
    for t in tiers:
        if vol >= float(t.get("usdVolume") or 0):
            chosen = t
        else:
            break
    pct = float(chosen.get("makerFee" if maker else "takerFee") or 0) / 100.0
    side = "maker" if maker else "taker"
    rebate = " (REBATE — paid to provide liquidity)" if pct < 0 else ""
    return pct, (f"{want} {sched.get('name')} {side} {pct * 100:.4g}% "
                 f"at ${float(chosen.get('usdVolume') or 0):,.0f}+ volume{rebate}")


# ── Kraken Pro US: PER-CONTRACT pricing ──────────────────────────────────
# US customers are explicitly excluded from the international Futures
# maker/taker tiers (0.020%/0.050%). US perpetuals list through Bitnomial
# and CME products clear as real futures, both priced PER CONTRACT rather
# than as a percentage of notional.
#
# That inverts the optimisation. A percentage fee is scale-neutral — 0.05%
# costs the same proportion whether the position is $100 or $100,000. A
# flat per-contract fee is REGRESSIVE: $0.30 round trip is 0.30% of a $100
# position and 0.003% of a $10,000 one. Small positions are punished and
# large ones are nearly free, which is the opposite of what the percentage
# model assumed.
#
# Source: Kraken Pro US fee documentation as supplied by the operator.
# Exchange, NFA and clearing components are included where Kraken publishes
# them all-in; for CME products Kraken publishes only its own commission
# and shows the full estimate in the order form, so the values here are
# LOWER BOUNDS and marked as such.
US_PERPETUAL_FEE_PER_SIDE = 0.15      # all-in: 0.03 Kraken + 0.10 exchange/clearing + 0.02 NFA

# Kraken's published cross-platform perpetual base tier, used ONLY when a
# symbol has no live schedule of its own. A leveraged crypto position is a
# perpetual, which is a different PRODUCT from spot with its own schedule —
# not a variant of it. Spot taker measures 0.80%/side on this account; the
# perp taker is 0.05%/side, so falling back to spot bills 16x the real cost.
KRAKEN_PERP_BASE_TAKER = 0.0005
KRAKEN_PERP_BASE_MAKER = 0.0002

US_FUTURES_COMMISSION = {             # Kraken commission per side; NOT all-in
    "MES=F": 0.39, "MNQ=F": 0.39,     # CME micros
    "ES=F": 1.29,  "NQ=F": 1.29,      # CME e-minis
}


# ── US perpetuals: Bitnomial-listed contracts ───────────────────────────────
# Kraken's US fee schedule (updated 2026-06-15) prices Bitnomial-listed US
# perpetual futures at a flat $0.15 per contract PER SIDE, all-in — $0.30 the
# round trip. The rate was never the problem; the CONTRACT COUNT was.
#
# The count must come from the BITNOMIAL contract, not from Kraken's
# international flexible futures. Those are different instruments: PF_XBTUSD
# uses contractSize 1 meaning one TOKEN, so deriving contracts that way made
# a $0.089 coin need 20,157 "contracts" and billed $6,047 to trade $1,800.
#
# Bitnomial PERPETUAL contracts, sized in units of the UNDERLYING, from the
# exchange rulebook. Note these are the P-prefixed perpetual products, NOT
# the BUI/BUS Bitcoin futures — those are a different instrument and using
# them put the BTC contract out by 10x.
#
# Everything listed on the Kraken US exchange is a US perpetual and is
# priced this way. What varies per instrument — and what this table exists
# to hold — is the CONTRACT SIZE, the only input the fee formula needs
# beyond the rate.
#
# Sizes are deliberately spread so a contract is economically comparable
# across a five-order-of-magnitude price range: 0.01 BTC and 100,000 SHIB
# are both roughly the same notional. That is exactly why assuming "one
# contract = one token" was so destructive — it made contract count a
# function of unit price, and billed $6,047 to trade $1,800 of a $0.089
# coin.
#
# Only sizes confirmed from a published spec belong here. A symbol absent
# from the table has an UNKNOWN contract size, not an ineligible one:
# us_perp_contracts() returns None so the caller prices it some other way
# and labels it, rather than inventing a size.
US_PERP_CONTRACTS = {
    "BTC":  [("PBTCUC", 0.01)],
    "ETH":  [("PETHUI", 0.5)],
    "SOL":  [("PSOLUS", 5.0)],
    "XRP":  [("PXRPUH", 500.0)],
    "AAVE": [("PAVEUS", 5.0)],
    "AVAX": [("PAVXUD", 50.0)],
    "BCH":  [("PBCHUS", 1.0)],
    "ADA":  [("PADAUK", 5_000.0)],
    "LINK": [("PLNKUD", 50.0)],
    "DOGE": [("PDOGUK", 5_000.0)],
    "HBAR": [("PHBRUK", 5_000.0)],
    "LTC":  [("PLTCUS", 5.0)],
    "DOT":  [("PDOTUH", 500.0)],
    "SHIB": [("PSHBUN", 100_000.0)],
    "XLM":  [("PXLMUK", 5_000.0)],
    "XTZ":  [("PXTZUK", 1_000.0)],
    "TRX":  [("PTRXUK", 1_000.0)],
}

US_PERP_FEE_PER_SIDE = 0.15           # all-in, per contract, per side
US_PERP_SCHEDULE_VINTAGE = "Kraken US schedule, updated 2026-06-15"

# No exchange lists a contract worth less than this. A configured size that
# implies one is wrong by orders of magnitude — the arithmetic below turns a
# bad size straight into a bad fee, so it is checked rather than trusted.
MIN_PLAUSIBLE_CONTRACT_NOTIONAL = 50.0


def _underlying(symbol: str) -> str:
    return str(symbol or "").upper().split("/")[0].replace("XBT", "BTC").strip()


def us_perp_contracts(symbol: str, notional: float,
                      price: float) -> tuple[float | None, str]:
    """(whole contracts needed, explanation) for a US perpetual, or None when
    the symbol has no listed Bitnomial contract.

    Futures trade in WHOLE contracts — you cannot buy 0.0937 of one — so the
    count rounds UP, and the smallest listed contract that can express the
    position is chosen. Returning None rather than guessing a contract size
    is the whole point: a fabricated size is what produced every absurd fee
    this model has emitted.
    """
    listed = US_PERP_CONTRACTS.get(_underlying(symbol))
    if not listed:
        return None, (f"{symbol}: contract size not on file, so contracts "
                      f"cannot be counted — add it to US_PERP_CONTRACTS to "
                      f"price this per contract")
    if price <= 0:
        return None, f"{symbol}: cannot count contracts without a price"
    import math
    base = _underlying(symbol)
    for name, size in listed:
        # A contract's notional is the real check on whether the size is
        # right. Bitnomial sizes them so one contract is a few hundred to a
        # few thousand dollars across a five-order-of-magnitude price range
        # (0.01 BTC and 5,000 ADA are both ~$1,000). A size that implies a
        # sub-$50 contract is off by orders of magnitude, and passing it
        # through would recreate the original bug from the opposite side:
        # SHIB at 100,000/contract is $0.45 at $0.00000447, which would need
        # 19,910 contracts and bill $5,973 to trade $8,900.
        one = price * size
        if one < MIN_PLAUSIBLE_CONTRACT_NOTIONAL:
            return None, (
                f"{symbol}: {name} at {size:g} {base}/contract is only "
                f"${one:,.2f} of notional at ${price:,.8g} — implausible for a "
                f"listed contract, so the size on file is wrong by orders of "
                f"magnitude and is not being used")
        contracts = math.ceil(abs(notional) / one)
        if contracts >= 1:
            return float(contracts), (
                f"{contracts:g} x {name} ({size:g} {base}/contract) at "
                f"${US_PERP_FEE_PER_SIDE:.2f}/side all-in "
                f"({US_PERP_SCHEDULE_VINTAGE})")
    return None, f"{symbol}: no listed contract can express ${abs(notional):,.0f}"


def us_perp_fee(symbol: str, notional: float, price: float) -> tuple[float | None, str]:
    """Round-trip cost: contracts x per-contract all-in fee x 2 sides."""
    contracts, why = us_perp_contracts(symbol, notional, price)
    if contracts is None:
        return None, why
    return contracts * US_PERP_FEE_PER_SIDE * 2.0, why


# ── US equity regulatory fees ───────────────────────────────────────────────
# "Commission-free" is not "free". Both charges below apply to the SELL side
# only, so a round trip pays them once. They are small, but a cost model that
# rounds them to zero is making the same class of error as one that ignores
# the spread — and on a small, frequently churned position they are not
# negligible relative to the edge.
#
# RATES CHANGE. The SEC sets its Section 31 rate annually (and sometimes
# mid-year); FINRA adjusts the TAF. These are the rates effective 2024-05-22,
# carried forward. Treat them as a documented vintage, not a live feed — the
# explanation string says so, so a stale rate is visible rather than silent.
SEC_SECTION_31_RATE = 0.0000278       # of sell PROCEEDS ($27.80 per $1M)
FINRA_TAF_PER_SHARE = 0.000166        # per share sold
FINRA_TAF_CAP       = 8.30            # per trade
EQUITY_FEE_VINTAGE  = "rates effective 2024-05-22"


def equity_regulatory_fee(proceeds: float, shares: float) -> tuple[float, str]:
    """(dollars, explanation) of SEC + FINRA charges on an equity SELL.

    Applies once per round trip — you are not charged to buy.
    """
    sec = abs(float(proceeds or 0)) * SEC_SECTION_31_RATE
    taf = min(abs(float(shares or 0)) * FINRA_TAF_PER_SHARE, FINRA_TAF_CAP)
    total = sec + taf
    return total, (f"$0 commission + SEC ${sec:,.2f} + FINRA TAF ${taf:,.2f} "
                   f"on the sell side ({EQUITY_FEE_VINTAGE})")


def us_perpetual_fee(contracts: float = 1.0) -> tuple[float, str]:
    """Round-trip cost of a US perpetual, in dollars.

    No maker/taker distinction exists in this schedule. Funding is separate
    and settles as a daily cash adjustment at 15:00 CT for open positions,
    so it is NOT included here — see funding_cost_pct.
    """
    total = abs(float(contracts)) * US_PERPETUAL_FEE_PER_SIDE * 2.0
    return total, (f"US perpetual: ${US_PERPETUAL_FEE_PER_SIDE:.2f}/contract/side all-in "
                   f"(Kraken 0.03 + exchange/clearing 0.10 + NFA 0.02), "
                   f"${total:.2f} round trip on {contracts:g} contract(s)")


def us_futures_fee(symbol: str, contracts: float = 1.0) -> tuple[float | None, str]:
    """Round-trip Kraken commission for a CME product, in dollars.

    This is a LOWER BOUND: exchange, NFA and clearing charges are added on
    top and Kraken publishes the complete figure only in the order form.
    Treating this as the full cost would understate it, so callers should
    label it as a floor rather than an estimate.
    """
    sym = str(symbol or "").upper()
    rate = US_FUTURES_COMMISSION.get(sym)
    if rate is None:
        return None, f"no published Kraken commission for {sym}"
    total = abs(float(contracts)) * rate * 2.0
    return total, (f"{sym}: ${rate:.2f}/side Kraken commission, ${total:.2f} round trip "
                   f"— EXCLUDES exchange/NFA/clearing, so this is a lower bound")


def us_fee_as_pct_of_notional(notional: float, fee_dollars: float) -> float:
    """Convert a per-contract fee to the percentage the cost model speaks.

    This is where the regressiveness becomes visible: the same dollar fee
    is a large percentage on a small position and a negligible one on a
    large position.
    """
    return (fee_dollars / abs(notional)) if notional else 0.0
