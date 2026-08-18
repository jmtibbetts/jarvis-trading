"""What holding this position cost — typed, signed, and honest about quality.

WHY funding_for_interval() COULD NOT BE THE SETTLEMENT AUTHORITY. It returns
a bare float, and on any exception it returns 0.0. At settlement grade, zero
is six different facts wearing one number:

    genuinely no holding cost            (a spot position)
    product not applicable               (no carry mechanism exists)
    measured funding happened to be zero (a real measurement)
    latest rate extrapolated to zero     (an estimate)
    conservative default of zero         (a stand-in)
    the provider failed                  (NO fact at all)

Only the last one is a refusal, and a settlement layer that cannot tell it
apart from the others will eventually record free carry for a position that
paid it — money the simulator invented. So the canonical exit path uses THIS
authority, which returns a typed quote that either establishes an amount
with its provenance, or says plainly that it could not.

SIGN IS PRESERVED, BECAUSE FUNDING IS A TRANSFER. Positive funding: longs
pay (positive holding cost), shorts receive (negative). Negative funding
reverses both. Settlement subtracts the holding cost, so subtracting a
negative transfer correctly credits the account. Nothing here calls abs().

QUALITY IS NOT A FORMALITY. The perp path extrapolates the LATEST measured
rate over the position's interval — even when the underlying snapshot says
"measured", that is a rate measurement, not a measurement of the funding
events this position actually paid. It is labelled LATEST_RATE_EXTRAPOLATED,
and nothing may call it MEASURED_INTERVAL until actual historical funding
events across the interval are collected.

This authority runs BEFORE the settlement transaction (it may read the
market-data tables); the settlement mutation layer receives the finished
quote and never calls back into it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

HOLDING_COST_VERSION = "holding_cost_v1"

# ── Kinds — what mechanism charged the carry ─────────────────────────────
KIND_FUNDING = "FUNDING"
KIND_BORROW = "BORROW"
KIND_ROLLOVER = "ROLLOVER"
KIND_NOT_APPLICABLE = "NOT_APPLICABLE"

# ── Qualities — how much the number can be trusted ───────────────────────
LATEST_RATE_EXTRAPOLATED = "LATEST_RATE_EXTRAPOLATED"
DEFAULT_BASELINE = "DEFAULT_BASELINE"
MEASURED_BORROW_RATE = "MEASURED_BORROW_RATE"
DEFAULT_GENERAL_COLLATERAL = "DEFAULT_GENERAL_COLLATERAL"
DEFAULT_HARD_TO_BORROW = "DEFAULT_HARD_TO_BORROW"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNAVAILABLE = "UNAVAILABLE"

HOLDING_COST_UNAVAILABLE = "HOLDING_COST_UNAVAILABLE"


@dataclass(frozen=True)
class HoldingCostQuote:
    """One position-interval's carry, with everything needed to audit it.

    `ok=False` means NO honest number exists — amount_usd is None, and the
    caller must refuse settlement rather than assume free carry. It is
    never a synonym for zero.
    """
    ok: bool
    amount_usd: float | None = None
    kind: str = KIND_NOT_APPLICABLE
    source: str | None = None
    quality: str = UNAVAILABLE
    product: str | None = None
    symbol: str | None = None
    notional_usd: float | None = None
    hours_held: float | None = None
    rate: float | None = None            # per-period or annual, per `kind`
    reason: str | None = None
    detail: str | None = None
    version: str = HOLDING_COST_VERSION


def _unavailable(reason: str, detail: str, **kw) -> HoldingCostQuote:
    return HoldingCostQuote(ok=False, amount_usd=None, quality=UNAVAILABLE,
                            reason=reason, detail=detail, **kw)


def _not_applicable(symbol: str, product: str, notional: float,
                    hours: float) -> HoldingCostQuote:
    """An ESTABLISHED zero — the product has no carry mechanism. This is a
    fact, not a missing measurement."""
    return HoldingCostQuote(ok=True, amount_usd=0.0,
                            kind=KIND_NOT_APPLICABLE, quality=NOT_APPLICABLE,
                            source="no carry mechanism for this product",
                            product=product, symbol=symbol,
                            notional_usd=notional, hours_held=hours)


def holding_cost(symbol: str, *, product: str, notional_usd: float,
                 hours_held: float, is_short: bool,
                 funding_rate_8h: float | None = None,
                 borrow_rate_annual: float | None = None,
                 hard_to_borrow: bool = False) -> HoldingCostQuote:
    """The carry for ONE quantity over ONE explicit historical interval.

    The caller states the interval and the notional of the quantity being
    charged — per-leg, so a 10-lot that exits 4 at hour 8 and 6 at hour 24
    charges 4 contracts for 8 hours and 6 for 24, never all 10 twice and
    never the first 4 for free.
    """
    prod = str(product or "").upper().strip()
    sym = str(symbol or "").upper().strip()
    try:
        notional = float(notional_usd)
        hours = float(hours_held)
    except (TypeError, ValueError):
        return _unavailable(HOLDING_COST_UNAVAILABLE,
                            f"non-numeric notional/hours "
                            f"({notional_usd!r}, {hours_held!r})",
                            product=prod, symbol=sym)
    if not math.isfinite(notional) or notional < 0:
        return _unavailable(HOLDING_COST_UNAVAILABLE,
                            f"notional {notional!r} is not a finite "
                            f"non-negative number",
                            product=prod, symbol=sym, hours_held=hours)
    if not math.isfinite(hours) or hours < 0:
        return _unavailable(HOLDING_COST_UNAVAILABLE,
                            f"hours_held {hours!r} is not a finite "
                            f"non-negative number",
                            product=prod, symbol=sym, notional_usd=notional)

    if prod == "CRYPTO_PERP":
        return _perp_funding(sym, prod, notional, hours, is_short,
                             funding_rate_8h)
    if prod == "EQUITY_SHORT":
        return _equity_borrow(sym, prod, notional, hours, is_short,
                              borrow_rate_annual, hard_to_borrow)
    if prod in ("CRYPTO_SPOT", "EQUITY_SPOT", "ETF_SPOT"):
        return _not_applicable(sym, prod, notional, hours)

    # An unknown product's carry is not zero — it is unknown. Recording
    # free carry for a product nobody characterised is the exact failure
    # this module exists to prevent.
    return _unavailable(
        HOLDING_COST_UNAVAILABLE,
        f"no holding-cost model is wired up for {prod!r}; refusing to "
        f"assume free carry", product=prod, symbol=sym,
        notional_usd=notional, hours_held=hours)


def _perp_funding(sym: str, prod: str, notional: float, hours: float,
                  is_short: bool,
                  funding_rate_8h: float | None) -> HoldingCostQuote:
    """Perpetual funding: the LATEST rate, extrapolated over the interval.

    HONEST LABELLING. The snapshot table calls its rate "measured", and it
    is — a measured RATE, at one instant. Applying it across the whole
    interval is extrapolation, so the quality here says so. A measured-
    interval quality can only exist once actual funding events across the
    interval are collected, which nothing does yet.
    """
    source_note = None
    if funding_rate_8h is None:
        try:
            from lib.transaction_costs import _latest_funding_rate
            funding_rate_8h, source_note = _latest_funding_rate(sym)
        except Exception as e:
            return _unavailable(
                HOLDING_COST_UNAVAILABLE,
                f"funding rate lookup failed: {e}", kind=KIND_FUNDING,
                product=prod, symbol=sym, notional_usd=notional,
                hours_held=hours)
        if funding_rate_8h is None:
            return _unavailable(
                HOLDING_COST_UNAVAILABLE,
                "no funding rate could be established", kind=KIND_FUNDING,
                product=prod, symbol=sym, notional_usd=notional,
                hours_held=hours)
        quality = (DEFAULT_BASELINE if source_note == "default_baseline"
                   else LATEST_RATE_EXTRAPOLATED)
        source = ("published baseline perpetual funding rate"
                  if quality == DEFAULT_BASELINE
                  else "latest OKX funding snapshot, extrapolated over the "
                       "interval")
    else:
        quality = LATEST_RATE_EXTRAPOLATED
        source = "caller-supplied 8h funding rate, extrapolated"

    rate = float(funding_rate_8h)
    if not math.isfinite(rate):
        return _unavailable(HOLDING_COST_UNAVAILABLE,
                            f"funding rate {funding_rate_8h!r} is not finite",
                            kind=KIND_FUNDING, product=prod, symbol=sym,
                            notional_usd=notional, hours_held=hours)

    # A TRANSFER, signed. Longs pay positive funding; shorts receive it.
    paid_pct = rate * (hours / 8.0)
    signed_pct = -paid_pct if is_short else paid_pct
    return HoldingCostQuote(
        ok=True, amount_usd=signed_pct * notional, kind=KIND_FUNDING,
        source=source, quality=quality, product=prod, symbol=sym,
        notional_usd=notional, hours_held=hours, rate=rate)


def _equity_borrow(sym: str, prod: str, notional: float, hours: float,
                   is_short: bool, borrow_rate_annual: float | None,
                   hard_to_borrow: bool) -> HoldingCostQuote:
    """Stock borrow: the lender's annualised fee, accrued on calendar days.

    Only a SHORT borrows. A default rate is labelled a default — claiming
    general collateral is a measurement would launder a guess into the
    ledger."""
    if not is_short:
        return _not_applicable(sym, prod, notional, hours)
    if borrow_rate_annual is not None:
        rate = float(borrow_rate_annual)
        quality, source = MEASURED_BORROW_RATE, "measured borrow rate"
    else:
        from lib.transaction_costs import (DEFAULT_BORROW_RATE_ANNUAL,
                                           HARD_TO_BORROW_RATE_ANNUAL)
        if hard_to_borrow:
            rate = float(HARD_TO_BORROW_RATE_ANNUAL)
            quality, source = (DEFAULT_HARD_TO_BORROW,
                               "hard-to-borrow default rate")
        else:
            rate = float(DEFAULT_BORROW_RATE_ANNUAL)
            quality, source = (DEFAULT_GENERAL_COLLATERAL,
                               "general-collateral default rate")
    if not math.isfinite(rate) or rate < 0:
        return _unavailable(HOLDING_COST_UNAVAILABLE,
                            f"borrow rate {borrow_rate_annual!r} is not a "
                            f"finite non-negative number", kind=KIND_BORROW,
                            product=prod, symbol=sym, notional_usd=notional,
                            hours_held=hours)
    amount = notional * rate * (hours / 24.0) / 365.0
    return HoldingCostQuote(
        ok=True, amount_usd=amount, kind=KIND_BORROW, source=source,
        quality=quality, product=prod, symbol=sym, notional_usd=notional,
        hours_held=hours, rate=rate)
