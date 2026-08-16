"""health(t, sol_shock, depeg_shock) — liquidation risk with three axes.

A single "distance to liquidation" number answers one question badly. The
boundary moves, and it moves for three independent reasons:

    1. SOL/USD shock     systemic, hits every SOL-derived asset at once
    2. LST/SOL depeg     asset-specific basis risk, small but not zero
    3. time and carry    collateral yield vs debt interest, either sign

Collapsing them loses the distinctions that matter. "SOL -20%, all LSTs
hold their peg" and "SOL flat, bSOL depegs 7%" are different events with
different cascades, and a position can be safe under one and liquidated
under the other.

Reserve parameters are used EXACTLY as the protocol sets them and never
averaged into a family factor. Kamino gives SOL a 75% liquidation
threshold and bSOL only 55% — it already prices the LST as the riskier
collateral, and flattening that would discard the protocol's own judgement.

Carry is not assumed to be a tailwind. LST yield helps, borrow interest
hurts, and on a stablecoin loan at 89% utilization the borrow side is
larger. The adverse scenario re-reads Kamino's own curve at stressed
utilization rather than inventing a number, because a deleveraging wave is
precisely what drives utilization — and therefore borrow APR — upward.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_SOL_SHOCKS = (0.0, 5.0, 10.0, 15.0, 20.0, 30.0)
DEFAULT_DEPEG_SHOCKS = (0.0, 1.0, 2.0, 5.0, 10.0)
DEFAULT_HORIZONS_DAYS = (0, 1, 7, 30, 90)


class LSTStressProfile:
    """Basis-risk assumptions for ONE liquid staking token.

    Per asset on purpose: mSOL, bSOL and JitoSOL have different validator
    sets, different withdrawal mechanics and very different liquidity, so
    inheriting one basis assumption across them would be the same mistake
    as sharing a collateral factor.

    Every number here is an ASSUMPTION and is labelled as such wherever it
    reaches output. None of it is decoded from the chain.
    """

    def __init__(self, symbol: str, normal_basis_vol_pct: float,
                 stress_depeg_pct: float, severe_depeg_pct: float,
                 catastrophic_depeg_pct: float, recovery_half_life_days: float,
                 note: str = ""):
        self.symbol = symbol
        self.normal_basis_vol_pct = normal_basis_vol_pct
        self.stress_depeg_pct = stress_depeg_pct
        self.severe_depeg_pct = severe_depeg_pct
        self.catastrophic_depeg_pct = catastrophic_depeg_pct
        self.recovery_half_life_days = recovery_half_life_days
        self.note = note

    def as_dict(self) -> dict:
        return {"symbol": self.symbol,
                "normal_basis_vol_pct": self.normal_basis_vol_pct,
                "stress_depeg_pct": self.stress_depeg_pct,
                "severe_depeg_pct": self.severe_depeg_pct,
                "catastrophic_depeg_pct": self.catastrophic_depeg_pct,
                "recovery_half_life_days": self.recovery_half_life_days,
                "basis": "ASSUMED — not decoded from chain data",
                "note": self.note}


# KEYED BY MINT, not by ticker. "BSOL" is also the Bitwise Solana Staking
# ETF, a US-listed equity, and JARVIS trades both asset classes — so a
# symbol-keyed profile would eventually apply liquid-staking depeg
# assumptions to an ETF share, or miss a real LST because the ticker
# arrived cased differently. Today "bSOL" and "BSOL" happen not to collide
# because nothing uppercases, which is luck rather than design. The mint is
# the authoritative identity, exactly as it is in capital_reserves.
#
# Ordered roughly by liquidity depth. Kamino's own liquidation thresholds
# rank them the same way (mSOL 60%, bSOL 55%), which is corroboration from
# an independent source rather than agreement by construction.
LST_PROFILES: dict[str, LSTStressProfile] = {
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":
        LSTStressProfile("mSOL", 0.3, 1.5, 4.0, 12.0, 3.0,
                         "deep secondary liquidity; Kamino liq threshold 60%"),
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn":
        LSTStressProfile("JitoSOL", 0.3, 1.5, 4.0, 12.0, 3.0,
                         "largest LST by TVL"),
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1":
        LSTStressProfile("bSOL", 0.5, 2.5, 6.0, 18.0, 5.0,
                         "thinner liquidity; Kamino liq threshold 55%, the "
                         "lowest of the SOL family. NOTE: ticker collides "
                         "with the Bitwise Solana Staking ETF — this profile "
                         "is the MINT, never the ticker"),
}

WSOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DERIVED_MINTS = set(LST_PROFILES) | {WSOL_MINT}

# Display-name fallback ONLY, for positions that arrive without a mint.
# Never used to select a stress profile.
_SOL_SYMBOLS = {"SOL", "wSOL", "mSOL", "bSOL", "JitoSOL", "jupSOL"}


def _mint_of(leg: dict | None) -> str | None:
    return (leg or {}).get("asset") or (leg or {}).get("mint")


def is_lst(leg: dict | str | None) -> bool:
    """True for a liquid staking token, decided by MINT.

    Accepts a position leg (preferred) or a bare symbol (legacy). A bare
    symbol cannot distinguish bSOL-the-LST from BSOL-the-ETF, so that path
    is deliberately conservative and matches nothing on its own.
    """
    if isinstance(leg, dict):
        return _mint_of(leg) in LST_PROFILES
    return False


def is_sol_derived(leg: dict | str | None) -> bool:
    if isinstance(leg, dict):
        mint = _mint_of(leg)
        if mint:
            return mint in SOL_DERIVED_MINTS
        # No mint on the leg — fall back to the symbol, which is all that
        # is available, and say so at the call site.
        return (leg.get("symbol") or "") in _SOL_SYMBOLS
    return (leg or "") in _SOL_SYMBOLS


def profile_for(leg: dict | str | None) -> LSTStressProfile | None:
    if isinstance(leg, dict):
        return LST_PROFILES.get(_mint_of(leg) or "")
    return None


def _grow(annual_pct: float, days: int) -> float:
    """Compounding factor over `days` at an annual percentage."""
    if days <= 0:
        return 1.0
    return (1.0 + annual_pct / 100.0) ** (days / 365.0)


def project_health(position: dict, *, days: int = 0, sol_shock_pct: float = 0.0,
                   depeg_shock_pct: float = 0.0,
                   carry: dict | None = None) -> dict | None:
    """Health factor under one (time, SOL shock, depeg) combination.

    Each collateral leg is repriced with the terms that actually apply to
    it: a plain SOL deposit takes the SOL shock only, an LST takes the SOL
    shock AND the depeg, and an unrelated asset takes neither. Its own
    reserve liquidation threshold is applied — never a family average.
    """
    assets = position.get("assets") or {}
    deposits = assets.get("deposits") or []
    borrows = assets.get("borrows") or []
    if not deposits or not borrows:
        return None

    sol_factor = 1.0 - (sol_shock_pct / 100.0)
    depeg_factor = 1.0 - (depeg_shock_pct / 100.0)
    carry = carry or {}
    col_growth = _grow(float(carry.get("collateral_growth_apy_pct") or 0.0), days)
    debt_growth = _grow(float(carry.get("debt_growth_apy_pct") or 0.0), days)

    weighted_collateral = 0.0
    raw_collateral = 0.0
    legs = []
    for d in deposits:
        sym = d.get("symbol")
        value = float(d.get("value_usd") or 0)
        thr = float(d.get("liquidation_threshold_pct") or 0) / 100.0
        f = 1.0
        # Identity by MINT — see LST_PROFILES on the BSOL ticker collision.
        if is_sol_derived(d):
            f *= sol_factor
            if is_lst(d):
                f *= depeg_factor
        shocked = value * f * col_growth
        raw_collateral += shocked
        # THE protocol parameter, per reserve, never averaged.
        weighted_collateral += shocked * thr
        legs.append({"symbol": sym, "value_usd": round(value, 2),
                     "shocked_value_usd": round(shocked, 2),
                     "liquidation_threshold_pct": d.get("liquidation_threshold_pct"),
                     "mint": _mint_of(d),
                     "identified_by": "mint" if _mint_of(d) else "symbol (no mint on leg)",
                     "took_sol_shock": is_sol_derived(d),
                     "took_depeg": is_lst(d)})

    debt = sum(float(b.get("value_usd") or 0) for b in borrows) * debt_growth
    if debt <= 0:
        return None
    return {
        "health_factor": round(weighted_collateral / debt, 4),
        "collateral_value_usd": round(raw_collateral, 2),
        "weighted_collateral_usd": round(weighted_collateral, 2),
        "debt_value_usd": round(debt, 2),
        "days": days, "sol_shock_pct": sol_shock_pct,
        "depeg_shock_pct": depeg_shock_pct,
        "legs": legs,
        "liquidatable": (weighted_collateral / debt) <= 1.0,
    }


def stress_matrix(position: dict, *, sol_shocks=DEFAULT_SOL_SHOCKS,
                  depeg_shocks=DEFAULT_DEPEG_SHOCKS, days: int = 0,
                  carry: dict | None = None) -> dict:
    """The SOL-shock x depeg grid at one horizon."""
    rows = []
    for s in sol_shocks:
        cells = []
        for d in depeg_shocks:
            r = project_health(position, days=days, sol_shock_pct=s,
                               depeg_shock_pct=d, carry=carry)
            cells.append({"depeg_pct": d,
                          "health_factor": (r or {}).get("health_factor"),
                          "liquidatable": (r or {}).get("liquidatable")})
        rows.append({"sol_shock_pct": s, "cells": cells})
    return {"days": days, "sol_shocks": list(sol_shocks),
            "depeg_shocks": list(depeg_shocks), "rows": rows,
            "carry_scenario": (carry or {}).get("scenario", "none")}


def liquidation_boundary(position: dict, *, days: int = 0,
                         depeg_shock_pct: float = 0.0,
                         carry: dict | None = None,
                         precision: float = 0.1) -> float | None:
    """The SOL decline that puts this position exactly at health 1.0.

    Solved by bisection rather than read off a ladder, so the boundary can
    be tracked as it MOVES with time and carry — which is the thing a fixed
    ladder cannot show.
    """
    base = project_health(position, days=days, sol_shock_pct=0.0,
                          depeg_shock_pct=depeg_shock_pct, carry=carry)
    if not base:
        return None
    if base["liquidatable"]:
        return 0.0
    lo, hi = 0.0, 99.0
    worst = project_health(position, days=days, sol_shock_pct=hi,
                           depeg_shock_pct=depeg_shock_pct, carry=carry)
    if not worst or not worst["liquidatable"]:
        return None                       # survives even a 99% decline
    while hi - lo > precision:
        mid = (lo + hi) / 2.0
        r = project_health(position, days=days, sol_shock_pct=mid,
                           depeg_shock_pct=depeg_shock_pct, carry=carry)
        if r and r["liquidatable"]:
            hi = mid
        else:
            lo = mid
    return round(hi, 2)


def position_risk_report(position: dict, *, carry_by_scenario: dict,
                         horizons=DEFAULT_HORIZONS_DAYS) -> dict:
    """The decomposed view — never one opaque score.

    Reports which risk dominates, because the remedy differs: SOL beta is
    hedged by reducing size or adding unrelated collateral, basis risk by
    swapping the LST, and negative carry by repaying rather than waiting.
    """
    current = project_health(position, days=0)
    if not current:
        return {"available": False,
                "reason": "position has no priced collateral or debt legs"}

    lst_legs = [l for l in current["legs"] if l["took_depeg"]]
    out = {
        "available": True,
        "current_health_factor": current["health_factor"],
        "static_sol_liquidation_pct": liquidation_boundary(position),
        "has_lst_collateral": bool(lst_legs),
        "lst_profiles": [profile_for(l).as_dict()
                         for l in lst_legs if profile_for(l)],
        "legs": current["legs"],
        "carry": {}, "boundary_over_time": {}, "matrix": {},
        "provenance": {
            "position_values": "VERIFIED — canonical Kamino decode",
            "liquidation_thresholds": "VERIFIED — per reserve, never averaged",
            "borrow_rates": "VERIFIED — Kamino's own utilization curve",
            "staking_yield": "ASSUMED — no LST rate history kept yet",
            "depeg_shocks": "ASSUMED — per-asset stress profiles",
            "projections": "MODELLED — scenarios, not forecasts",
        },
    }

    for scenario, carry in (carry_by_scenario or {}).items():
        out["carry"][scenario] = carry
        out["boundary_over_time"][scenario] = {
            f"T+{d}d": liquidation_boundary(position, days=d, carry=carry)
            for d in horizons
        }

    base_carry = (carry_by_scenario or {}).get("current")
    out["matrix"] = {f"T+{d}d": stress_matrix(position, days=d, carry=base_carry)
                     for d in (0, 30)}

    # Which risk dominates, decided by comparing like-for-like moves.
    sol_10 = project_health(position, sol_shock_pct=10.0)
    depeg_10 = project_health(position, depeg_shock_pct=10.0)
    if sol_10 and depeg_10:
        out["primary_risk"] = ("SOL beta" if sol_10["health_factor"] <= depeg_10["health_factor"]
                               else "LST basis")
        out["secondary_risk"] = ("LST basis" if out["primary_risk"] == "SOL beta"
                                 else "SOL beta")
    net = (carry_by_scenario or {}).get("current", {}).get("net_carry_apy_pct")
    out["carry_direction"] = ("positive" if (net or 0) > 0 else
                              "negative" if (net or 0) < 0 else "flat")
    return out
