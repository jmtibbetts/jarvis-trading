"""What one leg of one trade actually costs, in dollars, and how we know.

THE DEFECT THIS EXISTS TO REMOVE. `transaction_costs.fee_pct(leveraged=True)`
asked `venues.futures_fee_for()` for a perpetual rate. Under VENUE_REGION=us
that function returns None — correctly, because US perpetuals list through
Bitnomial and are priced PER CONTRACT, which a percentage-shaped caller
cannot express. Execution then fell through to
`fee_for(asset_class="crypto")`: THE SPOT SCHEDULE.

So a CRYPTO_PERP was silently billed as spot. Not approximately — a
different product's schedule entirely, 0.80%/side against 0.05%/side, and
in the direction that makes trades look unaffordable. A cost model that
rejects above 0.50R will refuse setups that are comfortably viable, and the
refusals look like risk discipline rather than a units bug.

A SPOT RATE IS NOT A CONSERVATIVE PERP ESTIMATE. It is a measurement of a
different instrument. The fix is structural: the perp branch never reaches
the spot branch, at all, for any reason. When the perp basis is genuinely
unknown this returns FEE_AUTHORITY_UNAVAILABLE, or a rate explicitly
labelled ESTIMATED_PERP — either of which is visible downstream. A wrong
number that looks measured is worse than a missing one.

THE API IS NOT PERCENTAGE-SHAPED, and that is the other half of the fix.
Percentage is the wrong primitive because it cannot represent a flat
per-contract fee without knowing the size, which is precisely the
information the old signature threw away:

    percentage products   fee_usd = notional * rate
    per-contract products fee_usd = filled_contracts * fee_per_contract

Dollars are what both have in common, so dollars is what this returns. A
display percentage is DERIVED afterwards, from the dollars, and never fed
back in as an input.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

FEE_AUTHORITY_VERSION = "fee_authority_v1"

# ── How the number is arrived at ─────────────────────────────────────────
PER_CONTRACT = "PER_CONTRACT"
PERCENT_OF_NOTIONAL = "PERCENT_OF_NOTIONAL"
REGULATORY_PER_SHARE = "REGULATORY_PER_SHARE"

# ── How much the number can be trusted. This travels with it. ────────────
# EXCHANGE_SCHEDULE  a published rulebook or live venue schedule
# VENUE_SCHEDULE     this venue's own configured maker/taker ladder
# ESTIMATED_PERP     a PERPETUAL rate standing in for one we cannot resolve;
#                    still the right product, explicitly not a measurement
# UNAVAILABLE        no honest basis exists — priced by nobody
EXCHANGE_SCHEDULE = "EXCHANGE_SCHEDULE"
VENUE_SCHEDULE = "VENUE_SCHEDULE"
ESTIMATED_PERP = "ESTIMATED_PERP"
UNAVAILABLE = "UNAVAILABLE"

FEE_AUTHORITY_UNAVAILABLE = "FEE_AUTHORITY_UNAVAILABLE"

# Qualities that are measurements rather than stand-ins. Anything outside
# this set must never be pooled into calibration as though it were observed.
MEASURED_QUALITIES = frozenset({EXCHANGE_SCHEDULE, VENUE_SCHEDULE,
                                REGULATORY_PER_SHARE})


@dataclass(frozen=True)
class FeeQuote:
    """One leg's cost, with everything needed to audit it.

    `ok=False` is a RESULT, not an absence: it names why no honest number
    exists so the caller can refuse the trade instead of assuming zero.
    """
    ok: bool
    fee_usd: float | None = None
    fee_basis: str | None = None
    rate: float | None = None              # fraction of notional, when applicable
    contract_count: float | None = None
    contract_size: float | None = None
    notional_usd: float | None = None
    venue: str | None = None
    product: str | None = None
    region: str | None = None
    maker: bool = False
    source: str | None = None
    quality: str = UNAVAILABLE
    reason: str | None = None
    detail: str | None = None
    version: str = FEE_AUTHORITY_VERSION

    @property
    def pct_of_notional(self) -> float | None:
        """DERIVED, for display only.

        Never an input. A flat per-contract fee expressed as a percentage is
        a number that changes with position size, so feeding it back into a
        percentage model reintroduces exactly the error this replaced.
        """
        if not self.ok or not self.notional_usd or self.fee_usd is None:
            return None
        return float(self.fee_usd) / abs(float(self.notional_usd))

    @property
    def is_measured(self) -> bool:
        return self.ok and self.quality in MEASURED_QUALITIES

    def as_dict(self) -> dict:
        from dataclasses import asdict
        d = asdict(self)
        d["pct_of_notional"] = self.pct_of_notional
        return d


def _unavailable(reason: str, detail: str, **kw) -> FeeQuote:
    return FeeQuote(ok=False, reason=reason, detail=detail,
                    quality=UNAVAILABLE, **kw)


def _region(explicit: str | None = None) -> str:
    return str(explicit or os.getenv("VENUE_REGION") or "international").lower()


def leg_fee(symbol: str, *, notional: float, price: float, product: str,
            venue: str | None = None, maker: bool = False,
            side: str | None = None, region: str | None = None) -> FeeQuote:
    """The cost of ONE leg, in dollars, for THIS product.

    Dispatch is on PRODUCT and nothing else. Not on leverage — a perpetual
    at 1x is still a perpetual — and never by falling through from one
    product's schedule into another's.
    """
    from lib import product_router as PR

    reg = _region(region)
    prod = str(product or "").upper()
    common = dict(venue=venue, product=prod, region=reg, maker=maker,
                  notional_usd=abs(float(notional or 0.0)))

    if not prod:
        return _unavailable(
            FEE_AUTHORITY_UNAVAILABLE,
            "no product was established, so no schedule can be selected; "
            "picking one would be a guess recorded as a measurement",
            **common)

    if prod == PR.CRYPTO_PERP:
        return _perp_leg_fee(symbol, price=price, **common)
    if prod == PR.CRYPTO_SPOT:
        return _crypto_spot_leg_fee(symbol, **common)
    if prod in (PR.EQUITY_SPOT, PR.ETF_SPOT, PR.EQUITY_SHORT):
        return _equity_leg_fee(symbol, price=price, side=side, **common)

    return _unavailable(
        FEE_AUTHORITY_UNAVAILABLE,
        f"no fee schedule is wired up for {prod}; this desk has not "
        f"characterised its costs and will not invent them",
        **common)


def _perp_leg_fee(symbol: str, *, price: float, notional_usd: float,
                  venue: str | None, product: str, region: str,
                  maker: bool) -> FeeQuote:
    """A PERPETUAL, priced as a perpetual. Never as spot, under any failure.

    Three bases, in descending order of authority, and the spot schedule is
    not among them at any level:

        1. the US per-contract rulebook figure, when the contract size is
           on file — an exact measurement, not a rate
        2. the international perpetual ladder, LABELLED as an estimate when
           it stands in for a US account whose contract size is unknown
        3. Kraken's published perpetual base tier, also labelled

    If none of those resolves, the answer is "unavailable" and the caller
    must refuse the trade.
    """
    from lib import venues as V

    common = dict(venue=venue, product=product, region=region, maker=maker,
                  notional_usd=notional_usd)

    if region == "us" and V.us_perp_venue_applies(venue):
        # PLANNING COUNT. us_perp_contracts rounds UP, which overstates the
        # fee — the safe direction for a cost estimate, and the wrong one
        # for an executable quantity. See executable_contracts().
        contracts, why = V.us_perp_contracts(symbol, notional_usd, price)
        if contracts is not None:
            spec = V.us_perp_spec(symbol) or {}
            per_side = float(spec.get("fee_per_contract_per_side",
                                      V.US_PERPETUAL_FEE_PER_SIDE))
            return FeeQuote(
                ok=True, fee_usd=contracts * per_side,
                fee_basis=PER_CONTRACT, rate=None,
                contract_count=contracts,
                contract_size=float(spec["contract_size"]) if spec.get("contract_size") else None,
                source=why, quality=EXCHANGE_SCHEDULE, **common)

        # THE FALLTHROUGH THAT USED TO REACH SPOT. The contract SIZE is
        # missing; the product is not in doubt. Everything on this venue in
        # this region is a perpetual, so it is priced on a perpetual ladder
        # and the substitution is stated rather than hidden inside a
        # plausible-looking number.
        rate, why = V.futures_fee_for(symbol, maker=maker,
                                      region="international")
        source = why
        if rate is None:
            rate = V.KRAKEN_PERP_BASE_MAKER if maker else V.KRAKEN_PERP_BASE_TAKER
            source = "Kraken published perpetual base tier"
        return FeeQuote(
            ok=True, fee_usd=notional_usd * float(rate),
            fee_basis=PERCENT_OF_NOTIONAL, rate=float(rate),
            source=source, quality=ESTIMATED_PERP,
            detail=("US perpetual contract size is not on file, so this leg "
                    "is priced on the INTERNATIONAL perpetual ladder and "
                    "labelled an estimate. It is still a perpetual rate — "
                    "the spot schedule is a different product and is never "
                    "substituted here"),
            **common)

    rate, why = V.futures_fee_for(symbol, maker=maker, region=region)
    if rate is not None:
        return FeeQuote(ok=True, fee_usd=notional_usd * float(rate),
                        fee_basis=PERCENT_OF_NOTIONAL, rate=float(rate),
                        source=why, quality=EXCHANGE_SCHEDULE, **common)

    rate = V.KRAKEN_PERP_BASE_MAKER if maker else V.KRAKEN_PERP_BASE_TAKER
    return FeeQuote(
        ok=True, fee_usd=notional_usd * float(rate),
        fee_basis=PERCENT_OF_NOTIONAL, rate=float(rate),
        source="Kraken published perpetual base tier",
        quality=ESTIMATED_PERP,
        detail=(f"{symbol} has no resolvable perpetual schedule ({why}); "
                f"priced at the published perpetual base tier and labelled "
                f"an estimate rather than billed as spot"),
        **common)


def _crypto_spot_leg_fee(symbol: str, *, notional_usd: float,
                         venue: str | None, product: str, region: str,
                         maker: bool) -> FeeQuote:
    from lib import venues as V
    try:
        rate, why = V.fee_for(venue or V.DEFAULT_VENUE, maker=maker,
                              asset_class="crypto")
    except Exception as e:
        return _unavailable(
            FEE_AUTHORITY_UNAVAILABLE,
            f"the venue fee registry could not price {symbol} spot: {e}",
            venue=venue, product=product, region=region, maker=maker,
            notional_usd=notional_usd)
    return FeeQuote(ok=True, fee_usd=notional_usd * float(rate),
                    fee_basis=PERCENT_OF_NOTIONAL, rate=float(rate),
                    source=why, quality=VENUE_SCHEDULE,
                    venue=venue, product=product, region=region, maker=maker,
                    notional_usd=notional_usd)


def _equity_leg_fee(symbol: str, *, price: float, side: str | None,
                    notional_usd: float, venue: str | None, product: str,
                    region: str, maker: bool) -> FeeQuote:
    """US equity regulatory charges apply to the SELL side only.

    A commission-free buy really does cost zero here, and saying so is not
    the same as ignoring the cost — the sell leg pays, and the exit will.
    """
    from lib import venues as V
    from lib.trade_side import SHORT, parse_side_strict

    common = dict(venue=venue, product=product, region=region, maker=maker,
                  notional_usd=notional_usd)
    selling = parse_side_strict(side) == SHORT if side is not None else False
    if not selling:
        return FeeQuote(ok=True, fee_usd=0.0, fee_basis=REGULATORY_PER_SHARE,
                        rate=0.0, quality=EXCHANGE_SCHEDULE,
                        source="US equity regulatory fees are sell-side only",
                        **common)
    shares = abs(notional_usd) / float(price) if price else 0.0
    fee, why = V.equity_regulatory_fee(abs(notional_usd), shares)
    return FeeQuote(ok=True, fee_usd=float(fee),
                    fee_basis=REGULATORY_PER_SHARE,
                    rate=(float(fee) / abs(notional_usd)) if notional_usd else None,
                    source=why, quality=EXCHANGE_SCHEDULE, **common)


def executable_contracts(symbol: str, notional: float,
                         price: float) -> tuple[float | None, str]:
    """Whole contracts that may actually be SENT. FLOOR, never ceil.

    `venues.us_perp_contracts` rounds UP, and that is correct where it is
    used: overstating a cost ESTIMATE is the safe direction, since a trade
    that clears a fee bar it was overcharged against genuinely clears it.

    It is the wrong direction for a quantity. An authorization is a MAXIMUM,
    so 92.5 contracts of room means 92 contracts may be sent, not 93 — and
    a request below one whole contract is refused rather than rounded up to
    one, which would take size nobody approved.
    """
    import math

    from lib import venues as V

    spec = V.us_perp_spec(symbol)
    if not spec:
        return None, (f"{symbol}: contract size not on file, so an executable "
                      f"contract count cannot be established")
    if not spec.get("verified"):
        return None, f"{symbol}: {spec['product_code']} is unverified"
    if price <= 0:
        return None, f"{symbol}: cannot count contracts without a price"

    size = float(spec["contract_size"])
    exact = abs(float(notional)) / float(price) / size
    contracts = float(math.floor(exact))
    if contracts < 1:
        return None, (
            f"{symbol}: {exact:.4f} contracts of room is below one whole "
            f"{spec['product_code']} ({size:g} {spec['underlying']}/contract) "
            f"— refusing rather than rounding up to size nobody authorized")
    return contracts, (
        f"{contracts:g} x {spec['product_code']} (floored from {exact:.4f}; "
        f"authorization is a maximum)")
