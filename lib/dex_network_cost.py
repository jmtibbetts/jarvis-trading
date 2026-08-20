"""The ONE place canonical DEX execution learns what a transaction costs.

WHAT THIS CLOSES. `lib.solana_fees` and `lib.solana_fee_policy` were built,
tested and correct — and had no canonical caller. Every DEX path priced its
network fee from `dex_swap_math.DEFAULT_PRIORITY_LAMPORTS`, a 100,000-lamport
constant, so the live fee market reached the simulator's economics nowhere at
all. A helper with no canonical caller is not implemented, and an execution
model disconnected from execution cost is exactly how a simulator invents
edge.

THE SEQUENCE, and why each step is where it is:

    1. identify the ECONOMIC ACTION        entry and exit are not the same
                                           decision and never share caps
    2. select an allowed PRIORITY LEVEL    how hard to bid, orthogonally
    3. gather the best REAL CONTEXT        writable accounts of this swap
    4. MEASURE                             estimate_network_fee -> evidence
    5. AUTHORIZE                           authorize_fee -> a bid
    6. only then may a caller price or     a refusal here stops the trade
       submit a swap

MEASURED, AUTHORIZED and ACTUAL stay three separate facts. This module
produces the first two. Only settlement produces the third, and only the
third is economic truth.

CONTEXT IS REAL OR IT IS ABSENT. A Solana swap writes the pool account and
the token mint, and those are known here — so the estimate is made against
the LOCAL fee market rather than a global average. What is NOT available is a
serialized transaction: there is no Solana transaction builder in this
repository (no `solders`, no `solana-py`, no Jupiter swap-transaction
integration) and no signer. So `simulateTransaction` and `getFeeForMessage`
have nothing to act on, compute units remain DEFAULT_BUDGET_ASSUMPTION and
the base fee remains SINGLE_SIGNATURE_PROTOCOL_ASSUMPTION. Those stay
labelled assumptions rather than being quietly promoted, and no transaction
is fabricated to make the labels look better.
"""
from __future__ import annotations

import logging

from lib import solana_fees as SF

logger = logging.getLogger(__name__)

# Wrapped SOL, written by essentially every swap that pays fees in SOL.
SOL_MINT = "So11111111111111111111111111111111111111112"

# Refusals this module can produce. Each names a distinct lesson rather
# than collapsing into "skipped".
NETWORK_FEE_UNKNOWN = "NETWORK_FEE_UNKNOWN"
NETWORK_FEE_REFUSED = "NETWORK_FEE_REFUSED"


def writable_accounts_for_swap(*, mint: str | None = None,
                               pool_address: str | None = None,
                               extra=None) -> list[str]:
    """The accounts a swap actually writes, as far as we honestly know.

    Solana prices inclusion around the accounts a transaction writes, so
    this is what makes an estimate about THIS transaction rather than about
    the network. The pool account is the contended one — it is what every
    other trader in the same token is also writing.

    Returns only what is genuinely known. An empty list is an honest
    answer, and the estimate is then labelled GLOBAL_ESTIMATE rather than
    being dressed up as transaction-specific.
    """
    keys: list[str] = []
    for candidate in (pool_address, mint, SOL_MINT, *(extra or [])):
        text = str(candidate).strip() if candidate else ""
        if text and text not in keys:
            keys.append(text)
    return keys


def compute_unit_authority(*, transaction_b64: str | None = None,
                           fetch=None) -> dict:
    """Measure compute units if a transaction exists; say UNKNOWN if not.

    THE HEADROOM IS A POLICY, NOT A MEASUREMENT. `unitsConsumed` is what
    one simulation used; a transaction that requests exactly that reverts
    the moment the pool state shifts under it. So headroom is a named,
    configurable percentage applied to a measured number, and both survive
    separately — never a blind multiplier baked into a constant.
    """
    from lib.solana_fee_policy import compute_headroom_pct

    headroom_pct = compute_headroom_pct()
    if not transaction_b64:
        return {
            "compute_unit_limit": SF.DEFAULT_SWAP_COMPUTE_UNITS,
            "compute_unit_limit_source": SF.DEFAULT_BUDGET_ASSUMPTION,
            "measured_units_consumed": None,
            "compute_headroom_pct": headroom_pct,
            "compute_headroom_policy": "NOT_APPLIED_NO_SIMULATION",
            "detail": ("no serialized transaction exists to simulate — "
                       "there is no Solana transaction builder in this "
                       "repository, so the compute budget is an assumption"),
        }
    fetch = fetch or SF._default_fetch
    try:
        result = fetch("simulateTransaction",
                       [transaction_b64,
                        {"sigVerify": False, "replaceRecentBlockhash": True,
                         "encoding": "base64"}])
        value = (result or {}).get("value") or {}
        units = value.get("unitsConsumed")
        if units is None:
            raise ValueError(f"no unitsConsumed in simulation: {value!r}")
        measured = int(units)
        requested = int(round(measured * (1.0 + headroom_pct / 100.0)))
        return {
            "compute_unit_limit": requested,
            "compute_unit_limit_source": SF.MEASURED_UNITS_CONSUMED,
            "measured_units_consumed": measured,
            "compute_headroom_pct": headroom_pct,
            "compute_headroom_policy": "MEASURED_PLUS_CONFIGURED_HEADROOM",
            "detail": None,
        }
    except Exception as exc:                                 # noqa: BLE001
        logger.debug("[DexNetworkCost] simulateTransaction unavailable: %s",
                     exc)
        return {
            "compute_unit_limit": SF.DEFAULT_SWAP_COMPUTE_UNITS,
            "compute_unit_limit_source": SF.DEFAULT_BUDGET_ASSUMPTION,
            "measured_units_consumed": None,
            "compute_headroom_pct": headroom_pct,
            "compute_headroom_policy": "NOT_APPLIED_SIMULATION_FAILED",
            "detail": f"simulateTransaction failed: {exc}",
        }


def base_fee_authority(*, message_b64: str | None = None, fetch=None) -> dict:
    """Measure the base fee if a compiled message exists; label it if not.

    5,000 lamports is the protocol constant PER SIGNATURE, which is not the
    same claim as "this transaction's base fee". A two-signature transaction
    pays twice it. Without a compiled message the number stays an explicitly
    labelled single-signature assumption.
    """
    if not message_b64:
        return {
            "base_fee_lamports": None,
            "base_fee_source": SF.BASE_FEE_ASSUMED,
            "detail": ("no compiled message exists for getFeeForMessage — "
                       "the protocol per-signature constant is assumed to "
                       "apply once"),
        }
    fetch = fetch or SF._default_fetch
    try:
        result = fetch("getFeeForMessage", [message_b64, {"commitment": "processed"}])
        value = (result or {}).get("value")
        if value is None:
            raise ValueError(f"getFeeForMessage returned no value: {result!r}")
        return {"base_fee_lamports": int(value),
                "base_fee_source": SF.BASE_FEE_MEASURED, "detail": None}
    except Exception as exc:                                 # noqa: BLE001
        logger.debug("[DexNetworkCost] getFeeForMessage unavailable: %s", exc)
        return {"base_fee_lamports": None,
                "base_fee_source": SF.BASE_FEE_ASSUMED,
                "detail": f"getFeeForMessage failed: {exc}"}


def price_transaction(*, action: str,
                      priority_level: str = SF.NORMAL,
                      mint: str | None = None,
                      pool_address: str | None = None,
                      writable_account_keys=None,
                      transaction_b64: str | None = None,
                      message_b64: str | None = None,
                      sol_price_usd: float = 0.0,
                      expected_edge_usd: float | None = None,
                      notional_usd: float | None = None,
                      fetch=None,
                      record_health: bool = True) -> dict:
    """MEASURE then AUTHORIZE one DEX transaction's network cost.

    Returns both objects, never a single collapsed number. `ok` is the
    authorization's verdict — a caller that proceeds on a False here is
    executing at a price the operator did not authorise.

    `priority_lamports_for_quote` is the ONE number the swap-math layer
    should be handed, and it is derived from the AUTHORIZED BID rather than
    from the measurement: what a quote should model is what we would
    actually pay.
    """
    accounts = (list(writable_account_keys)
                if writable_account_keys is not None
                else writable_accounts_for_swap(mint=mint,
                                                pool_address=pool_address))

    compute = compute_unit_authority(transaction_b64=transaction_b64,
                                     fetch=fetch)
    base = base_fee_authority(message_b64=message_b64, fetch=fetch)

    estimate = SF.estimate_network_fee(
        priority_level,
        writable_account_keys=accounts,
        transaction_b64=transaction_b64,
        compute_unit_limit=compute["compute_unit_limit"],
        compute_units_source=compute["compute_unit_limit_source"],
        measured_units_consumed=compute["measured_units_consumed"],
        base_fee_lamports=base["base_fee_lamports"],
        base_fee_source=base["base_fee_source"],
        fetch=fetch, record_health=record_health)

    authorization = SF.authorize_fee(
        estimate, action=action, sol_price_usd=sol_price_usd,
        expected_edge_usd=expected_edge_usd, notional_usd=notional_usd)

    # What a quote should model is what we would actually pay. quote_swap
    # adds the base fee itself, so it is handed the PRIORITY component of
    # the authorized bid.
    priority_for_quote = max(
        0, int(authorization.authorized_bid_lamports)
        - int(estimate.measured_base_fee_lamports))

    return {
        "ok": bool(authorization.allowed),
        "estimate": estimate,
        "authorization": authorization,
        "refusal_reason": authorization.refusal_reason,
        "detail": authorization.detail,
        "priority_lamports_for_quote": priority_for_quote,
        "authorized_bid_lamports": int(authorization.authorized_bid_lamports),
        "authorized_bid_sol": authorization.authorized_bid_sol,
        "measured_total_network_fee_lamports": (
            estimate.measured_total_network_fee_lamports if estimate.ok
            else None),
        "measured_total_network_fee_sol": (
            estimate.measured_total_network_fee_sol if estimate.ok else None),
        "context_quality": estimate.context_quality,
        "estimator": estimate.estimator,
        "quality": estimate.quality,
        "writable_account_keys": accounts,
        "compute_unit_authority": compute,
        "base_fee_authority": base,
    }


def fee_provenance(priced: dict) -> dict:
    """A flat, storable record of how a network cost was arrived at.

    Deliberately preserves MEASURED, AUTHORIZED and the labels on every
    assumption. Absent evidence stays None — turning it into zero would
    claim a measurement nobody made.
    """
    estimate = priced.get("estimate")
    auth = priced.get("authorization")
    compute = priced.get("compute_unit_authority") or {}
    base = priced.get("base_fee_authority") or {}
    return {
        "measured_total_network_fee_lamports":
            priced.get("measured_total_network_fee_lamports"),
        "authorized_network_fee_lamports": priced.get(
            "authorized_bid_lamports"),
        "priority_level": getattr(estimate, "priority_level", None),
        "action_policy": getattr(auth, "action_policy", None),
        "estimator": getattr(estimate, "estimator", None),
        "estimate_quality": getattr(estimate, "quality", None),
        "estimate_context_quality": getattr(estimate, "context_quality", None),
        "compute_unit_price_micro_lamports": getattr(
            estimate, "executable_compute_unit_price_micro_lamports", None),
        "compute_unit_limit": getattr(estimate, "compute_unit_limit", None),
        "compute_unit_limit_source": getattr(
            estimate, "compute_unit_limit_source", None),
        "measured_units_consumed": compute.get("measured_units_consumed"),
        "compute_headroom_pct": compute.get("compute_headroom_pct"),
        "compute_headroom_policy": compute.get("compute_headroom_policy"),
        "base_fee_lamports": getattr(estimate, "measured_base_fee_lamports",
                                     None),
        "base_fee_source": base.get("base_fee_source"),
        "fee_policy_version": getattr(auth, "policy_version", None),
        "binding_fee_constraint": getattr(auth, "binding_constraint", None),
        "bid_below_measured_requirement": getattr(
            auth, "bid_below_measured_requirement", None),
        "fee_refusal_reason": getattr(auth, "refusal_reason", None),
        "writable_account_keys": priced.get("writable_account_keys"),
    }
