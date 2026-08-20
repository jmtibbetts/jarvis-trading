"""How much JARVIS is WILLING to pay. Not what the network costs.

TWO DIFFERENT QUESTIONS, DELIBERATELY IN DIFFERENT MODULES.

    lib.solana_fees        what the network currently indicates a
                           transaction may require — measured, live,
                           from Helius or the standard RPC.
    lib.solana_fee_policy  what the operator authorises spending on
                           inclusion — a policy, configurable, versioned.

The caps used to live as module constants inside the estimator, which read
as though 0.05 SOL were a property of Solana. It is not. It was an initial
operator ceiling chosen because the operator observed that aggressive
inclusion can sometimes approach ~0.03 SOL — evidence for picking a safety
ceiling, never a fee estimate and never a network constant.

A MAXIMUM FEE IS A POLICY LIMIT, NOT A GAS PRICE. Nothing here may be read
as an estimate: these numbers answer "would we pay that?", never "what
does it cost?".

Every cap is named, per-action, environment-overridable and carries a
policy version, so a change is visible in provenance rather than silently
reinterpreting past decisions.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

FEE_POLICY_VERSION = "solana_fee_policy_v1"

LAMPORTS_PER_SOL = 1_000_000_000

# Action classes. Risk reduction may rationally outbid risk creation.
NORMAL_ENTRY = "NORMAL_ENTRY"
NORMAL_EXIT = "NORMAL_EXIT"
URGENT_EXIT = "URGENT_EXIT"
SEVERE_RISK_EXIT = "SEVERE_RISK_EXIT"

ACTIONS = (NORMAL_ENTRY, NORMAL_EXIT, URGENT_EXIT, SEVERE_RISK_EXIT)

# ── INITIAL OPERATOR DEFAULTS. Policy, not network truth. ───────────────
# Chosen conservatively; the ~0.03 SOL observation informs the ceiling for
# the most aggressive class. Every value is overridable per action via
# JARVIS_SOL_FEE_<ACTION>_<FIELD>.
_DEFAULTS = {
    NORMAL_ENTRY: {
        "max_priority_fee_lamports": int(0.002 * LAMPORTS_PER_SOL),
        "max_total_fee_lamports": int(0.0025 * LAMPORTS_PER_SOL),
        "max_fee_pct_notional": 1.0,
        "max_fee_pct_expected_edge": 25.0,
    },
    NORMAL_EXIT: {
        "max_priority_fee_lamports": int(0.01 * LAMPORTS_PER_SOL),
        "max_total_fee_lamports": int(0.011 * LAMPORTS_PER_SOL),
        "max_fee_pct_notional": 2.0,
        "max_fee_pct_expected_edge": None,   # an exit is not sized on edge
    },
    URGENT_EXIT: {
        "max_priority_fee_lamports": int(0.03 * LAMPORTS_PER_SOL),
        "max_total_fee_lamports": int(0.031 * LAMPORTS_PER_SOL),
        "max_fee_pct_notional": 5.0,
        "max_fee_pct_expected_edge": None,
    },
    SEVERE_RISK_EXIT: {
        "max_priority_fee_lamports": int(0.05 * LAMPORTS_PER_SOL),
        "max_total_fee_lamports": int(0.051 * LAMPORTS_PER_SOL),
        "max_fee_pct_notional": 10.0,
        "max_fee_pct_expected_edge": None,
    },
}

# Bounded fallback when NO live estimate exists and the action reduces
# risk. Also policy: it buys bounded aggression, never certainty, and
# never "whatever it takes".
_EMERGENCY_FALLBACK_DEFAULT_LAMPORTS = int(0.02 * LAMPORTS_PER_SOL)
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
    permissive default — an unnamed action has not been authorised to
    spend anything in particular.
    """
    if action not in _DEFAULTS:
        strict = dict(_DEFAULTS[NORMAL_ENTRY])
        return {**strict, "action": action, "policy_version": FEE_POLICY_VERSION,
                "source": "STRICTEST_FALLBACK_UNKNOWN_ACTION",
                "note": "unrecognised action; the tightest policy applies"}
    out: dict = {"action": action, "policy_version": FEE_POLICY_VERSION,
                 "sources": {}}
    for field, default in _DEFAULTS[action].items():
        value, source = _override(action, field, default)
        out[field] = value
        out["sources"][field] = source
    return out


def emergency_fallback_lamports() -> tuple[int, str]:
    """Bounded fee for a risk-reducing action with no live estimate."""
    raw = os.getenv(EMERGENCY_FALLBACK_ENV)
    if raw and raw.strip():
        try:
            return int(float(raw)), "ENV_OVERRIDE"
        except ValueError:
            logger.warning("[SolFeePolicy] %s=%r is not numeric",
                           EMERGENCY_FALLBACK_ENV, raw)
    return _EMERGENCY_FALLBACK_DEFAULT_LAMPORTS, "DEFAULT"


def describe() -> dict:
    """The whole policy surface, for Ops. Contains no network estimates."""
    fallback, fallback_source = emergency_fallback_lamports()
    return {
        "policy_version": FEE_POLICY_VERSION,
        "meaning": "operator willingness to pay for inclusion — NOT a fee "
                   "estimate and NOT a Solana network constant",
        "actions": {a: caps_for(a) for a in ACTIONS},
        "emergency_fallback_lamports": fallback,
        "emergency_fallback_source": fallback_source,
    }
