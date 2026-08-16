"""Utilization, borrow rates and carry — decoded from Kamino's own curve.

The carry side of liquidation risk. A leveraged position does not sit
still: LST collateral accrues staking yield while the debt accrues
interest, and which of those wins decides whether health drifts up, stays
flat, or quietly deteriorates while nobody touches the position.

The adverse case does not have to be invented, which is the point of
reading the real curve. USDC's own configuration:

    util   0%  ->  borrow APR   0.00%
    util  95%  ->  borrow APR   4.61%
    util 100%  ->  borrow APR  30.40%

It sits at 89% today, so the live rate is about 4.3%. A deleveraging event
pushes utilization toward 100% — borrowers repay, suppliers withdraw — and
the same curve then charges 30%. Borrow cost spikes at exactly the moment a
stress test matters, and the protocol tells us by how much.

Offsets continue from `capital_reserves`, all derived from the official
klend-sdk ReserveConfig layout:

    config              @ 4856
      protocolTakeRatePct @ 4870
      borrowRateCurve     @ 4920   (11 x CurvePoint, each u32+u32)
    liquidity.totalAvailableAmount @ 224
    liquidity.borrowedAmountSf     @ 232
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SCALE_SF = 2 ** 60
_OFF_CONFIG = 4856
OFF_TAKE_RATE_PCT = _OFF_CONFIG + 14
OFF_BORROW_CURVE = _OFF_CONFIG + 64
OFF_TOTAL_AVAILABLE = 128 + 96
OFF_BORROWED_SF = 128 + 104
CURVE_POINTS = 11

# Solana staking yield. This is an ASSUMPTION, not decoded from anywhere —
# the LST/SOL exchange rate is observable but its growth rate needs history
# this system does not yet keep. Every projection that uses it says so.
ASSUMED_STAKING_APY_PCT = 7.0

# Carry scenarios. "adverse" is not a guess either: it re-reads the SAME
# curve at stressed utilization, because a deleveraging wave is what drives
# utilization up in the first place.
CARRY_SCENARIOS = ("optimistic", "current", "zero", "adverse")
ADVERSE_UTILIZATION = 0.99


def _u32(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 4], "little")


def _u64(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 8], "little")


def _u128(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 16], "little")


def borrow_curve(raw: bytes) -> list[tuple[float, float]]:
    """The 11-point utilization -> borrow APR curve, trailing slots trimmed.

    Trimming matters: unused slots repeat the terminal point, and an
    earlier version dropped every (0,0) pair instead — which deleted the
    curve's ORIGIN. USDC's curve then started at 95% utilization, 89%
    found no bracket, and the interpolation fell through to the terminal
    30.40% instead of the real 4.33%. A 7x error in borrow cost, from
    filtering a legitimate point.
    """
    pts = [(_u32(raw, OFF_BORROW_CURVE + i * 8) / 100.0,
            _u32(raw, OFF_BORROW_CURVE + i * 8 + 4) / 100.0)
           for i in range(CURVE_POINTS)]
    trimmed = [pts[0]]
    for u, r in pts[1:]:
        # Stop once utilization stops increasing — the rest are padding
        # repeating the last real point.
        if u <= trimmed[-1][0]:
            if u >= 100.0 and trimmed[-1][0] < 100.0:
                trimmed.append((u, r))
            break
        trimmed.append((u, r))
    return trimmed


def borrow_apr_at(curve: list[tuple[float, float]], utilization_pct: float) -> float:
    """Linear interpolation along Kamino's curve. Clamped at both ends."""
    if not curve:
        return 0.0
    u = max(0.0, min(float(utilization_pct), 100.0))
    if u <= curve[0][0]:
        return curve[0][1]
    for i in range(len(curve) - 1):
        lo, hi = curve[i], curve[i + 1]
        if lo[0] <= u <= hi[0]:
            span = (hi[0] - lo[0]) or 1.0
            return lo[1] + (hi[1] - lo[1]) * ((u - lo[0]) / span)
    return curve[-1][1]


def reserve_economics(raw: bytes, mint_decimals: int) -> dict | None:
    """Utilization and the rates that follow from it."""
    if not raw or len(raw) < OFF_BORROW_CURVE + CURVE_POINTS * 8:
        return None
    scale = 10 ** int(mint_decimals or 0)
    available = _u64(raw, OFF_TOTAL_AVAILABLE) / scale
    borrowed = (_u128(raw, OFF_BORROWED_SF) / SCALE_SF) / scale
    supplied = available + borrowed
    if supplied <= 0:
        return {"utilization_pct": 0.0, "borrow_apr_pct": 0.0,
                "supply_apr_pct": 0.0, "curve": borrow_curve(raw),
                "protocol_take_pct": raw[OFF_TAKE_RATE_PCT],
                "available": available, "borrowed": borrowed}

    util = borrowed / supplied
    curve = borrow_curve(raw)
    borrow_apr = borrow_apr_at(curve, util * 100.0)
    take = raw[OFF_TAKE_RATE_PCT]
    # Suppliers earn the borrow interest, shared across all supply and net
    # of the protocol's cut.
    supply_apr = borrow_apr * util * (1.0 - take / 100.0)
    return {
        "utilization_pct": round(util * 100.0, 4),
        "borrow_apr_pct": round(borrow_apr, 4),
        "supply_apr_pct": round(supply_apr, 4),
        "protocol_take_pct": take,
        "available": available,
        "borrowed": borrowed,
        "curve": curve,
        # What the SAME curve charges if a deleveraging wave drives
        # utilization to 99%. Not a guess — the protocol's own number.
        "stressed_borrow_apr_pct": round(
            borrow_apr_at(curve, ADVERSE_UTILIZATION * 100.0), 4),
        "source": "kamino_reserve_config",
    }


def carry_rates(collateral_econ: list[dict], debt_econ: list[dict],
                scenario: str = "current",
                staking_apy_pct: float = ASSUMED_STAKING_APY_PCT,
                collateral_is_lst: bool = False) -> dict:
    """Annual collateral growth and debt growth under one carry scenario.

    Returns both sides separately rather than a net figure. "LST yield
    makes health improve" is only half the sentence — the borrow APR is
    climbing at the same time, and on a stablecoin loan at 89% utilization
    it is climbing faster.
    """
    supply = sum(e.get("supply_apr_pct", 0.0) for e in collateral_econ) / max(len(collateral_econ), 1)
    if scenario == "adverse":
        borrow = sum(e.get("stressed_borrow_apr_pct", 0.0) for e in debt_econ) / max(len(debt_econ), 1)
        staking = staking_apy_pct * 0.5      # yield also compresses in stress
    elif scenario == "zero":
        borrow = sum(e.get("borrow_apr_pct", 0.0) for e in debt_econ) / max(len(debt_econ), 1)
        staking = 0.0
    elif scenario == "optimistic":
        borrow = sum(e.get("borrow_apr_pct", 0.0) for e in debt_econ) / max(len(debt_econ), 1) * 0.75
        staking = staking_apy_pct * 1.15
    else:
        borrow = sum(e.get("borrow_apr_pct", 0.0) for e in debt_econ) / max(len(debt_econ), 1)
        staking = staking_apy_pct

    collateral_growth = (staking if collateral_is_lst else 0.0) + supply
    return {
        "scenario": scenario,
        "collateral_growth_apy_pct": round(collateral_growth, 4),
        "debt_growth_apy_pct": round(borrow, 4),
        "net_carry_apy_pct": round(collateral_growth - borrow, 4),
        "staking_apy_pct": round(staking, 4) if collateral_is_lst else 0.0,
        "assumptions": {
            "staking_apy": ("ASSUMED — the LST/SOL rate is observable but "
                            "its growth rate needs history not yet kept"
                            if collateral_is_lst else "not applicable"),
            "borrow_apr": ("VERIFIED — Kamino's own curve at "
                           + ("99% stressed utilization" if scenario == "adverse"
                              else "current utilization")),
        },
    }
