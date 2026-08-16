"""Product-specific execution mechanics — what differs once the order fills.

Every product shares the order lifecycle, the position ledger and the
realized-outcome contract. What they do NOT share is what one unit means,
what it costs to carry, and how it can be taken away from you:

    EQUITY        a short must be BORROWED, and borrow is neither free nor
                  always available
    FUTURES       whole contracts only, and a multiplier that dominates
                  every dollar figure
    FX            units and pips, where a JPY cross pips at the 2nd decimal
                  and everything else at the 4th
    CRYPTO SPOT   no funding, no liquidation — you own the coin
    CRYPTO PERP   funding accrues with a SIGN, and the position can be
                  taken from you before your stop is reached

The last one is the one a simulator most often gets wrong. A stop loss does
not protect a leveraged position if the market crosses the liquidation
price first: the exchange closes it, at its price, and the stop never
fills. A model that always honours the stop teaches the desk that leverage
is free downside protection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Exit classifications. A liquidation is not a stop ────────────────────
VOLUNTARY_EXIT = "VOLUNTARY_EXIT"
STOP_EXIT = "STOP_EXIT"
TARGET_EXIT = "TARGET_EXIT"
MARGIN_CALL = "MARGIN_CALL"
FORCED_LIQUIDATION = "FORCED_LIQUIDATION"

# ── Equity borrow ────────────────────────────────────────────────────────
GENERAL_COLLATERAL_RATE = 0.0050     # 50 bps annual, easy to borrow
HARD_TO_BORROW_RATE = 0.30           # 30% annual, a genuinely tight name
BORROW_DAYS_PER_YEAR = 365           # borrow accrues on calendar days

# ── Perpetuals ───────────────────────────────────────────────────────────
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005   # 0.5% of notional
DEFAULT_FUNDING_RATE_8H = 0.0001          # published baseline, 0.01%/8h


class BorrowUnavailable(RuntimeError):
    """A short that cannot be borrowed is not a free short — it is no
    trade. Modelling it as free is how a strategy learns to short
    impossible names."""


# ═════════════════════════════════════════════════════════════════════════
# EQUITY
# ═════════════════════════════════════════════════════════════════════════

def equity_borrow_cost(notional_usd: float, hold_hours: float, *,
                       hard_to_borrow: bool = False,
                       borrow_rate_annual: float | None = None,
                       available: bool = True) -> dict:
    """Cost of borrowing stock to short it. Longs never pay this.

    Availability is checked FIRST and refuses, because a short-interest
    signal surfaces exactly the names that are hardest to borrow — the
    setup and the constraint are correlated, so treating borrow as always
    available biases the whole strategy toward trades it could not have
    put on.
    """
    if not available:
        raise BorrowUnavailable(
            "ORDER_REJECTED_BORROW_UNAVAILABLE — a short that cannot be "
            "borrowed is not a free short, it is no trade")
    rate = (float(borrow_rate_annual) if borrow_rate_annual is not None
            else (HARD_TO_BORROW_RATE if hard_to_borrow
                  else GENERAL_COLLATERAL_RATE))
    days = max(0.0, float(hold_hours)) / 24.0
    cost = float(notional_usd) * rate * (days / BORROW_DAYS_PER_YEAR)
    return {
        "borrow_cost_usd": cost,
        "borrow_rate_annual": rate,
        "hard_to_borrow": bool(hard_to_borrow),
        "days_held": days,
        "provenance": ("measured" if borrow_rate_annual is not None else
                       ("default_hard_to_borrow" if hard_to_borrow
                        else "default_general_collateral")),
    }


# ═════════════════════════════════════════════════════════════════════════
# FUTURES
# ═════════════════════════════════════════════════════════════════════════

def futures_size(symbol: str, risk_budget_usd: float, entry: float,
                 stop: float) -> dict:
    """Whole contracts only, sized on RISK PER CONTRACT.

    Risk per contract is price distance TIMES the multiplier. Sizing on
    price distance alone understates risk by the multiplier — a 10-point
    stop on ES is $500 of risk, not $10 — so a "1% risk" position would
    actually be a 50% one.

    A budget too small for one contract returns zero. Rounding up to one
    would silently take more risk than the caller authorised, and there is
    no such thing as a fractional contract.
    """
    from lib.instruments import resolve

    inst = resolve(symbol).require_executable()
    mult = float(inst.multiplier or 1.0)
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        return {"contracts": 0, "reason": "no risk distance"}

    risk_per_contract = distance * mult
    contracts = int(float(risk_budget_usd) // risk_per_contract)
    return {
        "contracts": contracts,
        "quantity_unit": inst.quantity_unit,
        "multiplier": mult,
        "risk_per_contract_usd": risk_per_contract,
        "risk_usd": contracts * risk_per_contract,
        "notional_usd": contracts * float(entry) * mult,
        "margin_usd": (contracts * float(inst.initial_margin)
                       if inst.initial_margin else None),
        "reason": (None if contracts else
                   f"budget ${risk_budget_usd:,.0f} is below the "
                   f"${risk_per_contract:,.0f} risk of one contract"),
    }


# ═════════════════════════════════════════════════════════════════════════
# FOREX
# ═════════════════════════════════════════════════════════════════════════

def fx_pip_value(symbol: str, units: float, *,
                 quote_to_usd: float = 1.0) -> dict:
    """Dollar value of one pip on `units` of base currency.

    The pip SIZE is the trap: a JPY cross pips at the 2nd decimal and
    everything else at the 4th, so using 0.0001 everywhere overstates every
    JPY pip value by 100x — and FX costs are quoted in pips, so the error
    lands directly on the cost model.
    """
    from lib.instruments import resolve

    inst = resolve(symbol)
    if inst.asset_class != "FOREX":
        return {"pip_value_usd": None, "reason": f"{symbol} is not FX"}
    pip = float(inst.pip_size or 0.0001)
    return {
        "pip_size": pip,
        "pip_value_usd": float(units) * pip * float(quote_to_usd),
        "units": float(units),
        "quantity_unit": inst.quantity_unit,
        "lots": float(units) / 100_000.0,
    }


# ═════════════════════════════════════════════════════════════════════════
# CRYPTO PERPETUALS
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class PerpPosition:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    leverage: float = 1.0
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE

    @property
    def notional(self) -> float:
        return abs(float(self.quantity) * float(self.entry_price))

    @property
    def margin(self) -> float:
        return self.notional / max(1.0, float(self.leverage))


def liquidation_price(pos: PerpPosition) -> float | None:
    """The price at which the exchange takes the position away.

    A stop does NOT protect a leveraged position that reaches this first.
    The exchange closes it, at its own price, and the stop never fills —
    so a simulator that always honours the stop teaches the desk that
    leverage is free downside protection.
    """
    from lib.trade_side import SHORT, parse_side_strict

    side = parse_side_strict(pos.side)
    if side is None or not pos.entry_price or pos.leverage <= 0:
        return None
    # Margin is exhausted once loss per unit reaches (1/L - mmr) of entry.
    move_frac = (1.0 / float(pos.leverage)) - float(pos.maintenance_margin_rate)
    if move_frac <= 0:
        return float(pos.entry_price)      # already unmaintainable
    e = float(pos.entry_price)
    return e * (1.0 + move_frac) if side == SHORT else e * (1.0 - move_frac)


def resolve_exit(pos: PerpPosition, *, stop_price: float | None,
                 bar_high: float, bar_low: float) -> dict:
    """Which came first — the stop, or the liquidation?

    THE case a simulator must not get wrong. If price crosses the
    liquidation boundary within the same bar, the position is gone at the
    exchange's price whether or not the stop was also touched. Reporting
    that as a clean STOP_EXIT understates the tail of every leveraged
    strategy in the book.
    """
    from lib.trade_side import SHORT, parse_side_strict

    side = parse_side_strict(pos.side)
    if side is None:
        return {"exit_reason": None, "reason": "unreadable side"}

    liq = liquidation_price(pos)
    if side == SHORT:
        liq_hit = liq is not None and float(bar_high) >= liq
        stop_hit = stop_price is not None and float(bar_high) >= float(stop_price)
        liq_first = liq_hit and (not stop_hit or liq <= float(stop_price))
    else:
        liq_hit = liq is not None and float(bar_low) <= liq
        stop_hit = stop_price is not None and float(bar_low) <= float(stop_price)
        liq_first = liq_hit and (not stop_hit or liq >= float(stop_price))

    if liq_first:
        return {"exit_reason": FORCED_LIQUIDATION, "exit_price": liq,
                "liquidation_price": liq, "stop_would_have_filled": stop_hit,
                "detail": ("liquidation was crossed before the stop — the "
                           "exchange closed this, the stop never filled")}
    if stop_hit:
        return {"exit_reason": STOP_EXIT, "exit_price": float(stop_price),
                "liquidation_price": liq, "stop_would_have_filled": True}
    return {"exit_reason": None, "liquidation_price": liq,
            "stop_would_have_filled": False}


def funding_payment(pos: PerpPosition, hold_hours: float, *,
                    rate_8h: float | None = None) -> dict:
    """Funding is a TRANSFER, and its sign depends on the side.

    Longs pay shorts when the rate is positive; shorts pay longs when it is
    negative. A short with positive funding RECEIVES it, so this can
    legitimately return a negative cost — and a model that always charges
    funding as a cost systematically understates every short.
    """
    from lib.trade_side import SHORT, parse_side_strict

    side = parse_side_strict(pos.side)
    if side is None:
        return {"funding_usd": None, "reason": "unreadable side"}
    rate = DEFAULT_FUNDING_RATE_8H if rate_8h is None else float(rate_8h)
    periods = max(0.0, float(hold_hours)) / 8.0
    paid = pos.notional * rate * periods
    return {
        "funding_usd": -paid if side == SHORT else paid,
        "rate_8h": rate, "periods": periods,
        "receives": (side == SHORT and rate > 0) or (side != SHORT and rate < 0),
        "provenance": "measured" if rate_8h is not None else "default_baseline",
    }


def spot_has_no_funding(symbol: str) -> bool:
    """Spot is owned, not financed. Charging it funding is a pure
    simulator error that penalises spot against perps."""
    from lib.instruments import CRYPTO_SPOT, resolve
    return resolve(symbol).product == CRYPTO_SPOT
