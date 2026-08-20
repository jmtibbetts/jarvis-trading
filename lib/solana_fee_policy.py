"""How much JARVIS is WILLING to pay. Not what the network costs.

TWO DIFFERENT QUESTIONS, DELIBERATELY IN DIFFERENT MODULES.

    lib.solana_fees        what the network currently indicates a
                           transaction may require -- measured, live,
                           from Helius or the standard RPC.
    lib.solana_fee_policy  what the operator authorises spending on
                           inclusion -- a policy, configurable, versioned.

The caps used to live as module constants inside the estimator, which read
as though 0.05 SOL were a property of Solana. It is not.

A MAXIMUM FEE IS A POLICY LIMIT, NOT A GAS PRICE. Nothing here may be read
as an estimate: these numbers answer "would we pay that?", never "what does
it cost?".

THE CEILING IS ON THE TOTAL NETWORK FEE, NOT ON THE PRIORITY FEE ALONE.
v1 authored a `max_priority_fee_lamports` per action and a
`max_total_fee_lamports` that NOTHING EVER READ, so the operator's actual
question -- "what is the most this transaction may cost me?" -- had no
enforced answer anywhere in the system. The total is now the authored
number and the priority ceiling is DERIVED from it, so the two cannot
disagree:

    total_network_fee_lamports = base_fee_lamports + priority_fee_lamports

CALIBRATION, AND WHERE THESE NUMBERS COME FROM. They are OPERATOR
WILLINGNESS-TO-PAY DEFAULTS, calibrated against what the operator actually
chooses to pay -- not against what Solana charges:

    0.002  SOL   the effective normal maximum the operator chooses, for
                 both entering and exiting in ordinary conditions.
    0.0035 SOL   the extreme the operator has historically OBSERVED
                 (~0.003-0.0035) without ever having needed to deliberately
                 pay it. Reserved for urgent and severe risk reduction,
                 because paying up to escape is a different decision from
                 paying up to enter.

None of them is a gas price, an expected cost, a Solana constant, or a
promise of inclusion. v1 carried 0.01 / 0.03 / 0.05 SOL -- an order of
magnitude above anything the operator would authorise. Those were a safety
ceiling picked from a single aggressive observation, and they had quietly
drifted into reading as normal.

Every cap is named, per-action, environment-overridable and carries a
policy version, so a change is visible in provenance rather than silently
reinterpreting past decisions.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

FEE_POLICY_VERSION = "solana_fee_policy_v2"

LAMPORTS_PER_SOL = 1_000_000_000

# Solana's per-signature base fee. MIRRORED rather than imported, so this
# module keeps the property the suite asserts about it -- that it contains
# no reference to the estimator at all. The copy cannot rot: a test asserts
# it still equals lib.solana_fees.PROTOCOL_BASE_FEE_LAMPORTS.
_PROTOCOL_BASE_FEE_LAMPORTS = 5_000

# Action classes. Risk reduction may rationally outbid risk creation.
NORMAL_ENTRY = "NORMAL_ENTRY"
NORMAL_EXIT = "NORMAL_EXIT"
URGENT_EXIT = "URGENT_EXIT"
SEVERE_RISK_EXIT = "SEVERE_RISK_EXIT"

ACTIONS = (NORMAL_ENTRY, NORMAL_EXIT, URGENT_EXIT, SEVERE_RISK_EXIT)

# -- INITIAL OPERATOR DEFAULTS. Policy, not network truth. ---------------
# The AUTHORED number is the absolute total-network-fee ceiling, in SOL.
NORMAL_TOTAL_CEILING_SOL = 0.002
EMERGENCY_TOTAL_CEILING_SOL = 0.0035

_TOTAL_CEILING_SOL = {
    NORMAL_ENTRY: NORMAL_TOTAL_CEILING_SOL,
    NORMAL_EXIT: NORMAL_TOTAL_CEILING_SOL,
    URGENT_EXIT: EMERGENCY_TOTAL_CEILING_SOL,
    SEVERE_RISK_EXIT: EMERGENCY_TOTAL_CEILING_SOL,
}

# Percentage caps are a SECOND, independent bound: a fee well inside the
# absolute ceiling can still be economically wrong for a small trade.
_PCT_CAPS = {
    NORMAL_ENTRY:     {"max_fee_pct_notional": 1.0,
                       "max_fee_pct_expected_edge": 25.0},
    NORMAL_EXIT:      {"max_fee_pct_notional": 2.0,
                       "max_fee_pct_expected_edge": None},  # not sized on edge
    URGENT_EXIT:      {"max_fee_pct_notional": 5.0,
                       "max_fee_pct_expected_edge": None},
    SEVERE_RISK_EXIT: {"max_fee_pct_notional": 10.0,
                       "max_fee_pct_expected_edge": None},
}


def sol_to_lamports(sol: float) -> int:
    """SOL -> lamports. One conversion, so there is one place to be wrong."""
    return int(round(float(sol) * LAMPORTS_PER_SOL))


def lamports_to_sol(lamports: int) -> float:
    """lamports -> SOL. Display only; lamports stay canonical."""
    return int(lamports) / LAMPORTS_PER_SOL


def _defaults_for(action: str) -> dict:
    """The authored total ceiling, plus the priority ceiling DERIVED from it.

    Derived, never authored twice: a priority ceiling written independently
    of the total is how the two drift apart, and the pair that disagrees is
    exactly the one that lets a transaction cost more than was authorised.
    """
    total = sol_to_lamports(_TOTAL_CEILING_SOL[action])
    return {
        "max_total_network_fee_lamports": total,
        "max_priority_fee_lamports": total - _PROTOCOL_BASE_FEE_LAMPORTS,
        **_PCT_CAPS[action],
    }


_DEFAULTS = {a: _defaults_for(a) for a in ACTIONS}

# Bounded fallback when NO live estimate exists and the action reduces
# risk. Also policy: it buys bounded aggression, never certainty, and never
# "whatever it takes". It DEFAULTS TO, and is CLAMPED BY, the severe-exit
# total ceiling rather than being an independent number -- an "emergency"
# allowance permitted to exceed the emergency ceiling is not a bound, and
# v1's 0.02 SOL default was 5.7x the ceiling it was meant to respect.
EMERGENCY_FALLBACK_ENV = "JARVIS_SOL_FEE_EMERGENCY_FALLBACK_LAMPORTS"


def _env_key(action: str, field: str) -> str:
    return f"JARVIS_SOL_FEE_{action}_{field.upper()}"


def _override(action: str, field: str, default):
    raw = os.getenv(_env_key(action, field))
    if raw is None or not raw.strip():
        return default, "DEFAULT"
    try:
        value = float(raw) if "pct" in field else int(float(raw))
    except ValueError:
        logger.warning("[SolFeePolicy] %s=%r is not numeric; using default",
                       _env_key(action, field), raw)
        return default, "DEFAULT_INVALID_OVERRIDE"
    return value, "ENV_OVERRIDE"


def caps_for(action: str) -> dict:
    """The configured caps for one action class, with provenance.

    An unrecognised action returns the STRICTEST policy rather than a
    permissive default -- an unnamed action has not been authorised to
    spend anything in particular.
    """
    if action not in _DEFAULTS:
        strict = dict(_DEFAULTS[NORMAL_ENTRY])
        return {**strict, "action": action,
                "policy_version": FEE_POLICY_VERSION,
                "source": "STRICTEST_FALLBACK_UNKNOWN_ACTION",
                "max_total_network_fee_sol": lamports_to_sol(
                    strict["max_total_network_fee_lamports"]),
                "note": "unrecognised action; the tightest policy applies"}
    out: dict = {"action": action, "policy_version": FEE_POLICY_VERSION,
                 "sources": {}}
    for field, default in _DEFAULTS[action].items():
        value, source = _override(action, field, default)
        out[field] = value
        out["sources"][field] = source
    out["max_total_network_fee_sol"] = lamports_to_sol(
        out["max_total_network_fee_lamports"])
    return out


def total_cap_lamports(action: str) -> tuple[int, str]:
    """The absolute total-network-fee ceiling for one action, with source."""
    caps = caps_for(action)
    return (int(caps["max_total_network_fee_lamports"]),
            f"{caps['policy_version']}/{caps['action']}")


def emergency_fallback_lamports() -> tuple[int, str]:
    """Bounded TOTAL fee for a risk-reducing action with no live estimate.

    CLAMPED to the severe-risk-exit ceiling. An operator who wants to
    authorise more raises that ceiling, where the change is visible as a
    policy decision -- rather than routing around it through a fallback
    that only applies when the estimator is blind, which is the worst
    possible moment to be spending more than was authorised.
    """
    ceiling, ceiling_source = total_cap_lamports(SEVERE_RISK_EXIT)
    raw = os.getenv(EMERGENCY_FALLBACK_ENV)
    if raw and raw.strip():
        try:
            requested = int(float(raw))
        except ValueError:
            logger.warning("[SolFeePolicy] %s=%r is not numeric",
                           EMERGENCY_FALLBACK_ENV, raw)
        else:
            if requested > ceiling:
                logger.warning(
                    "[SolFeePolicy] %s=%d exceeds the %s severe-risk-exit "
                    "ceiling of %d lamports; clamped. Raise the ceiling "
                    "itself to authorise more.",
                    EMERGENCY_FALLBACK_ENV, requested, ceiling_source, ceiling)
                return ceiling, "CLAMPED_TO_SEVERE_RISK_EXIT_CEILING"
            return max(0, requested), "ENV_OVERRIDE"
    return ceiling, f"DEFAULT_SEVERE_RISK_EXIT_CEILING/{ceiling_source}"


def describe() -> dict:
    """The whole policy surface, for Ops. Contains no network estimates."""
    fallback, fallback_source = emergency_fallback_lamports()
    return {
        "policy_version": FEE_POLICY_VERSION,
        "meaning": "operator willingness to pay for inclusion -- NOT a fee "
                   "estimate and NOT a Solana network constant",
        "ceiling_is_on": "total_network_fee_lamports (base + priority)",
        "normal_total_ceiling_sol": NORMAL_TOTAL_CEILING_SOL,
        "emergency_total_ceiling_sol": EMERGENCY_TOTAL_CEILING_SOL,
        "actions": {a: caps_for(a) for a in ACTIONS},
        "emergency_fallback_lamports": fallback,
        "emergency_fallback_sol": lamports_to_sol(fallback),
        "emergency_fallback_source": fallback_source,
    }
