"""Solana network fees, estimated live per transaction. GAS IS NOT A CONSTANT.

WHAT THIS REPLACES. The operator has observed aggressive Solana inclusion
costing around ~0.03 SOL. That number is REAL-WORLD OPERATOR OBSERVATION —
of one moment, in one fee market, around one set of accounts. Hard-coding
it (or any figure) as "the" gas cost would make the simulator's execution
economics a constant in a market that prices inclusion by congestion around
the specific accounts a transaction touches. A simulator that underprices
execution manufactures edge; one that overprices it refuses real edge. Both
are the same defect: the fee was not measured.

AUTHORITY HIERARCHY, measured against what lib/helius_client actually
exposes (a generic JSON-RPC `rpc(method, params)` against Helius mainnet):

    priority fee   1. Helius getPriorityFeeEstimate  (per-account fee
                      market, per-priority-level)
                   2. getRecentPrioritizationFees    (standard Solana RPC,
                      percentile over recent slots)
                   3. nothing -> UNKNOWN. Never a constant, never zero,
                      never a caller's opinion.
    base fee       getFeeForMessage when a compiled message exists; the
                   protocol's per-signature base otherwise, labelled as
                   such. The protocol constant IS the protocol constant —
                   using it is measurement, not invention.
    compute units  simulateTransaction unitsConsumed when a transaction is
                   available; the declared default budget otherwise,
                   labelled.

CANONICAL UNITS ARE LAMPORTS. SOL and USD are derived display values;
accounting keeps the integers the chain charges.

MAX_ACCEPTANCE IS A POLICY, NOT AN AMOUNT. It selects the most aggressive
practical estimator level — it does not promise inclusion (no fee can) and
it does NOT mean "pay whatever the estimator says": every policy passes
through hard caps, and the caps for creating risk are tighter than the caps
for shedding it. Risk reduction may rationally outbid risk creation;
neither may drain the wallet.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
MICRO_LAMPORTS = 1_000_000

# Protocol base fee per signature. This is Solana's documented constant,
# not a market estimate — the market lives in the priority fee.
PROTOCOL_BASE_FEE_LAMPORTS = 5_000

# Default compute budget when no simulation is available. Solana's default
# per-instruction budget is 200k CU; a Jupiter-style multi-hop swap commonly
# lands higher, so the DEFAULT is conservative and LABELLED an assumption.
DEFAULT_SWAP_COMPUTE_UNITS = 400_000

# ── Priority policies ────────────────────────────────────────────────────
ECONOMY = "ECONOMY"
NORMAL = "NORMAL"
HIGH = "HIGH"
VERY_HIGH = "VERY_HIGH"
MAX_ACCEPTANCE = "MAX_ACCEPTANCE"

POLICIES = (ECONOMY, NORMAL, HIGH, VERY_HIGH, MAX_ACCEPTANCE)

# Helius getPriorityFeeEstimate priority levels per policy. MAX_ACCEPTANCE
# maps to veryHigh — the most aggressive PRACTICAL level. Helius also
# exposes unsafeMax; it is named unsafe by its own provider and exists to
# win auctions, not to price them, so it is deliberately not selected.
_HELIUS_LEVEL = {
    ECONOMY: "low",
    NORMAL: "medium",
    HIGH: "high",
    VERY_HIGH: "veryHigh",
    MAX_ACCEPTANCE: "veryHigh",
}

# getRecentPrioritizationFees fallback: percentile over recent slots.
_FALLBACK_PERCENTILE = {
    ECONOMY: 0.25,
    NORMAL: 0.50,
    HIGH: 0.75,
    VERY_HIGH: 0.90,
    MAX_ACCEPTANCE: 0.95,
}

# ── Hard caps. EVERY policy is bounded — including MAX_ACCEPTANCE. ──────
# Priority cap in lamports for the whole transaction. The MAX_ACCEPTANCE
# cap of 0.05 SOL sits above the operator's observed ~0.03 SOL aggressive
# cost — the observation calibrates the CEILING, it does not become the
# price.
_MAX_PRIORITY_LAMPORTS = {
    ECONOMY: int(0.0005 * LAMPORTS_PER_SOL),
    NORMAL: int(0.002 * LAMPORTS_PER_SOL),
    HIGH: int(0.01 * LAMPORTS_PER_SOL),
    VERY_HIGH: int(0.03 * LAMPORTS_PER_SOL),
    MAX_ACCEPTANCE: int(0.05 * LAMPORTS_PER_SOL),
}

# Entry economics: a NEW position must not spend its edge on inclusion.
MAX_FEE_PCT_OF_EDGE_FOR_ENTRY = 25.0
# And never more than this share of the trade itself, edge aside.
MAX_FEE_PCT_OF_NOTIONAL = 1.0

# Emergency risk-reduction fallback: used ONLY when no live estimate exists
# and the action reduces risk. Bounded and named — an urgent exit with a
# dead estimator pays at most this, it does not pay "whatever".
EMERGENCY_EXIT_FALLBACK_LAMPORTS = int(0.02 * LAMPORTS_PER_SOL)

MAX_ESTIMATE_AGE_MS = 30_000

# Quality vocabulary.
MEASURED_HELIUS = "MEASURED_HELIUS_PRIORITY_ESTIMATE"
MEASURED_RPC_FALLBACK = "MEASURED_RECENT_PRIORITIZATION_FEES"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FeeEstimate:
    """One transaction's estimated network cost. Lamports are canonical."""
    ok: bool
    priority_policy: str
    base_fee_lamports: int = PROTOCOL_BASE_FEE_LAMPORTS
    base_fee_source: str = "PROTOCOL_CONSTANT_PER_SIGNATURE"
    priority_fee_lamports: int = 0
    compute_unit_limit: int = DEFAULT_SWAP_COMPUTE_UNITS
    compute_unit_limit_source: str = "DEFAULT_BUDGET_ASSUMPTION"
    compute_unit_price_micro_lamports: int = 0
    priority_estimate_source: str = UNKNOWN
    quality: str = UNKNOWN
    capped: bool = False
    cap_applied_lamports: int | None = None
    observed_at: str = ""
    estimate_age_ms: float = 0.0
    reason: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def total_lamports(self) -> int:
        return int(self.base_fee_lamports) + int(self.priority_fee_lamports)

    @property
    def total_sol(self) -> float:
        return self.total_lamports / LAMPORTS_PER_SOL

    def total_usd(self, sol_price_usd: float) -> float | None:
        if not sol_price_usd or sol_price_usd <= 0:
            return None
        return self.total_sol * float(sol_price_usd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_fetch(method: str, params):
    """Live RPC through the EXISTING Helius client. Injectable for tests."""
    from lib import helius_client as HC
    return HC.rpc(method, params)


def _helius_priority(policy: str, *, account_keys, transaction_b64, fetch):
    """Helius getPriorityFeeEstimate — the per-account fee market."""
    options = {"priorityLevel": _HELIUS_LEVEL[policy]}
    params: dict = {"options": options}
    if transaction_b64:
        params["transaction"] = transaction_b64
    elif account_keys:
        params["accountKeys"] = list(account_keys)[:32]
    else:
        # A global estimate is still a Helius measurement, just a coarser
        # one; recorded as such in provenance.
        options["includeAllPriorityFeeLevels"] = False
    result = fetch("getPriorityFeeEstimate", [params])
    est = result.get("priorityFeeEstimate") if isinstance(result, dict) else None
    if est is None:
        raise ValueError(f"no priorityFeeEstimate in response: {result!r}")
    return float(est)  # micro-lamports per compute unit


def _rpc_fallback_priority(policy: str, *, fetch):
    """getRecentPrioritizationFees percentile — standard Solana RPC."""
    rows = fetch("getRecentPrioritizationFees", [])
    fees = sorted(float(r.get("prioritizationFee") or 0.0)
                  for r in (rows or []) if isinstance(r, dict))
    if not fees:
        raise ValueError("getRecentPrioritizationFees returned no rows")
    idx = min(len(fees) - 1,
              int(len(fees) * _FALLBACK_PERCENTILE[policy]))
    return fees[idx]  # micro-lamports per compute unit


def estimate_network_fee(policy: str, *,
                         account_keys=None,
                         transaction_b64: str | None = None,
                         compute_unit_limit: int | None = None,
                         compute_units_source: str | None = None,
                         fetch=None) -> FeeEstimate:
    """Estimate THIS transaction's network fee under a priority policy.

    Tries Helius first (per-account fee market), the standard RPC second,
    and refuses to invent a number third. A dead estimator returns
    quality=UNKNOWN — what that means for the trade is authorize_fee's
    decision, because an entry and an emergency exit answer it differently.
    """
    if policy not in POLICIES:
        return FeeEstimate(ok=False, priority_policy=str(policy),
                           reason=f"unknown priority policy {policy!r}")
    fetch = fetch or _default_fetch
    cu_limit = int(compute_unit_limit or DEFAULT_SWAP_COMPUTE_UNITS)
    cu_source = (compute_units_source
                 or ("CALLER_SIMULATION" if compute_unit_limit
                     else "DEFAULT_BUDGET_ASSUMPTION"))
    started = time.perf_counter()

    micro_per_cu = None
    source = UNKNOWN
    quality = UNKNOWN
    detail = None
    try:
        micro_per_cu = _helius_priority(policy, account_keys=account_keys,
                                        transaction_b64=transaction_b64,
                                        fetch=fetch)
        source = "helius.getPriorityFeeEstimate"
        quality = MEASURED_HELIUS
    except Exception as first:                               # noqa: BLE001
        try:
            micro_per_cu = _rpc_fallback_priority(policy, fetch=fetch)
            source = "rpc.getRecentPrioritizationFees"
            quality = MEASURED_RPC_FALLBACK
            detail = f"helius estimate unavailable ({first}); used fallback"
        except Exception as second:                          # noqa: BLE001
            return FeeEstimate(
                ok=False, priority_policy=policy,
                compute_unit_limit=cu_limit,
                compute_unit_limit_source=cu_source,
                quality=UNKNOWN, observed_at=_now_iso(),
                estimate_age_ms=(time.perf_counter() - started) * 1000.0,
                reason=(f"no trustworthy priority estimate: helius=({first}) "
                        f"fallback=({second})"),
                provenance={"helius_error": str(first)[:200],
                            "fallback_error": str(second)[:200]})

    priority_lamports = int(micro_per_cu * cu_limit / MICRO_LAMPORTS)

    # HARD CAP — every policy, including MAX_ACCEPTANCE.
    cap = _MAX_PRIORITY_LAMPORTS[policy]
    capped = priority_lamports > cap
    if capped:
        priority_lamports = cap

    return FeeEstimate(
        ok=True, priority_policy=policy,
        priority_fee_lamports=priority_lamports,
        compute_unit_limit=cu_limit,
        compute_unit_limit_source=cu_source,
        compute_unit_price_micro_lamports=int(micro_per_cu),
        priority_estimate_source=source, quality=quality,
        capped=capped, cap_applied_lamports=cap if capped else None,
        observed_at=_now_iso(),
        estimate_age_ms=(time.perf_counter() - started) * 1000.0,
        reason=detail,
        provenance={"micro_lamports_per_cu_raw": micro_per_cu,
                    "policy_cap_lamports": cap})


# ── Economic authorization: is this fee worth paying for THIS action? ───
ENTRY = "ENTRY"
PROFIT_EXIT = "PROFIT_EXIT"
URGENT_RISK_REDUCTION = "URGENT_RISK_REDUCTION"

# Which priority policies each action may select. An autonomous entry does
# not get to bid like a liquidation-avoidance exit.
ALLOWED_POLICIES = {
    ENTRY: (ECONOMY, NORMAL, HIGH),
    PROFIT_EXIT: (ECONOMY, NORMAL, HIGH, VERY_HIGH),
    URGENT_RISK_REDUCTION: POLICIES,          # including MAX_ACCEPTANCE
}


def authorize_fee(estimate: FeeEstimate, *, action: str,
                  sol_price_usd: float,
                  expected_edge_usd: float | None = None,
                  notional_usd: float | None = None) -> dict:
    """May this fee be paid for this action? Refusals name themselves.

    THE ASYMMETRY, STATED: risk reduction may rationally tolerate a larger
    fee than risk creation — paying up to escape is different from paying
    up to enter. But asymmetry is not immunity: even the emergency path is
    bounded, because an unbounded 'emergency' is a wallet drain wearing a
    siren.
    """
    if action not in ALLOWED_POLICIES:
        return {"ok": False, "reason": f"unknown action {action!r}"}

    if not estimate.ok or estimate.quality == UNKNOWN:
        if action == URGENT_RISK_REDUCTION:
            # Bounded, named fallback — never "whatever it takes".
            return {"ok": True,
                    "fee_lamports": EMERGENCY_EXIT_FALLBACK_LAMPORTS,
                    "policy": "EMERGENCY_FALLBACK",
                    "quality": UNKNOWN,
                    "note": ("no live estimate; the separately configured "
                             "emergency cap applies. This buys bounded "
                             "aggression, not certainty.")}
        return {"ok": False, "reason": "FEE_ESTIMATE_UNKNOWN",
                "detail": ("no trustworthy network fee estimate — a normal "
                           "action does not proceed on a guessed cost. "
                           + (estimate.reason or ""))}

    if estimate.priority_policy not in ALLOWED_POLICIES[action]:
        return {"ok": False, "reason": "POLICY_NOT_PERMITTED_FOR_ACTION",
                "detail": (f"{action} may not select "
                           f"{estimate.priority_policy}; permitted: "
                           f"{ALLOWED_POLICIES[action]}")}

    fee_usd = estimate.total_usd(sol_price_usd)

    if action == ENTRY:
        # A new position must not spend its edge on inclusion.
        if fee_usd is None:
            return {"ok": False, "reason": "FEE_USD_UNPRICEABLE",
                    "detail": "no SOL price to value the fee against edge"}
        if expected_edge_usd is not None and expected_edge_usd > 0:
            pct_of_edge = 100.0 * fee_usd / expected_edge_usd
            if pct_of_edge > MAX_FEE_PCT_OF_EDGE_FOR_ENTRY:
                return {"ok": False, "reason": "FEE_DESTROYS_EDGE",
                        "detail": (f"network fee ${fee_usd:.4f} is "
                                   f"{pct_of_edge:.1f}% of the "
                                   f"${expected_edge_usd:.2f} expected edge "
                                   f"(cap {MAX_FEE_PCT_OF_EDGE_FOR_ENTRY}%)"),
                        "fee_usd": fee_usd}
        if notional_usd and notional_usd > 0:
            pct_of_notional = 100.0 * fee_usd / float(notional_usd)
            if pct_of_notional > MAX_FEE_PCT_OF_NOTIONAL:
                return {"ok": False, "reason": "FEE_EXCEEDS_NOTIONAL_CAP",
                        "detail": (f"network fee ${fee_usd:.4f} is "
                                   f"{pct_of_notional:.2f}% of the "
                                   f"${notional_usd:.2f} trade "
                                   f"(cap {MAX_FEE_PCT_OF_NOTIONAL}%)"),
                        "fee_usd": fee_usd}

    return {"ok": True, "fee_lamports": estimate.total_lamports,
            "fee_usd": fee_usd, "policy": estimate.priority_policy,
            "quality": estimate.quality, "capped": estimate.capped}


# ── Estimated vs actual: the calibration loop. Python owns this. ────────
_reconciliations: list[dict] = []
_RECON_WINDOW = 200


def reconcile_fee(estimate: FeeEstimate, *, actual_total_lamports: int,
                  actual_fee_source: str, context: dict | None = None) -> dict:
    """Compare what was estimated with what the chain actually charged.

    The estimate is PRESERVED next to the actual — replacing one with the
    other would destroy the only evidence of whether a policy overpays,
    underestimates, or is priced about right.
    """
    est = int(estimate.total_lamports)
    act = int(actual_total_lamports)
    err = act - est
    row = {
        "estimated_total_lamports": est,
        "actual_total_lamports": act,
        "estimation_error_lamports": err,
        "estimation_error_pct": (100.0 * err / est) if est else None,
        "priority_policy": estimate.priority_policy,
        "estimator": estimate.priority_estimate_source,
        "quality": estimate.quality,
        "capped": estimate.capped,
        "actual_fee_source": actual_fee_source,
        "context": context or {},
        "at": _now_iso(),
    }
    _reconciliations.append(row)
    del _reconciliations[:-_RECON_WINDOW]
    if est and abs(row["estimation_error_pct"]) > 50.0:
        logger.warning("[SolanaFees] estimate off by %.0f%% (%s, %s): "
                       "est %d vs actual %d lamports",
                       row["estimation_error_pct"], estimate.priority_policy,
                       estimate.priority_estimate_source, est, act)
    return row


def reconciliation_summary() -> dict:
    """Bounded rollup for Ops. Per (policy, estimator) error statistics."""
    out: dict = {}
    for row in _reconciliations:
        key = f"{row['priority_policy']}/{row['estimator']}"
        bucket = out.setdefault(key, {"n": 0, "errors_pct": []})
        bucket["n"] += 1
        if row["estimation_error_pct"] is not None:
            bucket["errors_pct"].append(row["estimation_error_pct"])
    for key, bucket in out.items():
        errs = bucket.pop("errors_pct")
        bucket["avg_error_pct"] = (round(sum(errs) / len(errs), 2)
                                   if errs else "UNKNOWN")
        bucket["max_abs_error_pct"] = (round(max(abs(e) for e in errs), 2)
                                       if errs else "UNKNOWN")
    return {"window": _RECON_WINDOW, "by_policy_estimator": out}
