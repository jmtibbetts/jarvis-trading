"""What the Solana network currently indicates a transaction costs.

THIS MODULE MEASURES. IT DOES NOT DECIDE. `lib.solana_fee_policy` holds what
the operator is willing to pay, and `authorize_fee` below is the only place
the two meet. The separation is the point:

    NetworkFeeEstimate   what current network evidence indicates
    FeeAuthorization     what JARVIS is allowed and willing to pay

A capped number is NOT an estimate. Earlier versions applied the operator
ceiling inside `estimate_network_fee` and returned the clamped figure as
`priority_fee_lamports`, which destroyed the only evidence that the market
had moved beyond what we authorised: a 0.004 SOL market and a 0.002 SOL
ceiling both came back as "0.002", so nothing downstream could tell a cheap
transaction from a refused one. The estimate now carries the MEASUREMENT,
untouched, and the authorization carries the bid.

ESTIMATE -> AUTHORIZED BID -> ACTUAL CHARGE are three different lifecycle
facts and are never collapsed. Only the ACTUAL charge is economic truth;
the other two are evidence about a decision.

AUTHORITY HIERARCHY, measured against what lib/helius_client exposes:

    priority fee   1. Helius getPriorityFeeEstimate  (per-account fee
                      market, per-priority-level)
                   2. getRecentPrioritizationFees    (standard Solana RPC,
                      percentile over recent slots — ACCOUNT-AWARE when
                      writable accounts are known)
                   3. nothing -> UNKNOWN. Never a constant, never zero,
                      never a caller's opinion.
    base fee       getFeeForMessage when a compiled message exists; the
                   protocol's per-signature constant otherwise, labelled
                   SINGLE_SIGNATURE_PROTOCOL_ASSUMPTION because a
                   multi-signature transaction pays a multiple of it.
    compute units  simulateTransaction unitsConsumed when a transaction is
                   available; the declared default budget otherwise,
                   labelled DEFAULT_BUDGET_ASSUMPTION.

CONTEXT QUALITY IS PART OF THE ANSWER. A global estimate and a per-account
estimate are both measurements, of different things. Solana prices inclusion
around the specific accounts a transaction writes, so a global figure is
evidence about the network, not about this transaction. Every estimate says
which one it is, and a global zero is never reported as "this transaction
needs no priority fee".

UNITS ARE PART OF THE TYPE. Exactly one function per conversion:

    compute_unit_price_micro_lamports   what BOTH estimators return:
                                        MICRO-LAMPORTS PER COMPUTE UNIT.
    priority_fee_lamports()             micro-lamports/CU x CU -> lamports
                                        (divide by 1e6, ROUND UP)
    total_network_fee_lamports          base + priority
    lamports_to_sol()                   lamports -> SOL (divide by 1e9)

CANONICAL UNITS ARE LAMPORTS. SOL and USD are derived display values.

THE CONVERSION IS EXACT, NOT FLOATING POINT. A live unsafeMax quote of
160,361,842,105 micro-lamports/CU times a 400,000 CU budget is 6.4e16
micro-lamports — past the 9.007e15 boundary where float64 stops being able
to represent consecutive integers. Decimal keeps it exact, and the rounding
is CEILING because a fee rounded down is a fee that was never offered.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# A DIVISOR, not a unit: there are one million micro-lamports in a lamport.
MICRO_LAMPORTS_PER_LAMPORT = 1_000_000

# Solana's documented per-signature base fee. The protocol constant IS the
# protocol constant — but a TRANSACTION's base fee is that times the number
# of signatures, which is why using it is an ASSUMPTION about signature
# count and is labelled as one.
PROTOCOL_BASE_FEE_LAMPORTS = 5_000

BASE_FEE_ASSUMED = "SINGLE_SIGNATURE_PROTOCOL_ASSUMPTION"
BASE_FEE_MEASURED = "RPC_GET_FEE_FOR_MESSAGE"

# Default compute budget when no simulation is available. Solana's default
# per-instruction budget is 200k CU; a Jupiter-style multi-hop swap commonly
# lands higher, so the DEFAULT is conservative and LABELLED an assumption.
# It must never harden into a measurement: real measurement needs
# simulateTransaction unitsConsumed, which needs a canonical transaction.
DEFAULT_SWAP_COMPUTE_UNITS = 400_000
DEFAULT_BUDGET_ASSUMPTION = "DEFAULT_BUDGET_ASSUMPTION"
MEASURED_UNITS_CONSUMED = "SIMULATE_TRANSACTION_UNITS_CONSUMED"

# ── Priority levels: HOW AGGRESSIVELY TO BID ────────────────────────────
# Orthogonal to action policy below. A priority level selects a place in
# the fee market; it does NOT decide which economic rules apply.
ECONOMY = "ECONOMY"
NORMAL = "NORMAL"
HIGH = "HIGH"
VERY_HIGH = "VERY_HIGH"
MAX_ACCEPTANCE = "MAX_ACCEPTANCE"

PRIORITY_LEVELS = (ECONOMY, NORMAL, HIGH, VERY_HIGH, MAX_ACCEPTANCE)
POLICIES = PRIORITY_LEVELS          # legacy name, same tuple

# Helius priority levels. CAPITALISED, because the API rejects anything
# else. Measured against live Helius: priorityLevel="high" returns -32602
# Invalid params while "High" succeeds, so the lowercase spelling silently
# disabled the primary authority and every estimate quietly came from the
# RPC fallback.
#
# MAX_ACCEPTANCE maps to VeryHigh, NOT UnsafeMax, and unsafeMax is refused
# outright below. Measured live, unsafeMax quoted 160,361,842,105
# micro-lamports/CU. On the 400k-CU default budget that is:
#
#     160,361,842,105 x 400,000       = 6.4144736842e16 micro-lamports
#                       / 1e6         = 64,144,736,842 lamports
#                       / 1e9         = 64.144736842 SOL of priority fee
#
# ~64 SOL, NOT ~64,000 SOL. THE PRECISE UNIT FAILURE behind the old
# "~64,000 SOL" note: the second division used the MICRO-LAMPORTS-PER-LAMPORT
# divisor (1e6) a second time where LAMPORTS-PER-SOL (1e9) belonged, and
# 1e9/1e6 = 1000. Reading the lamport total as SOL outright would have been
# a 1e9x error instead. The mistake was reusing a right-looking constant in
# the wrong step, which is the failure mode that survives a re-read.
_HELIUS_LEVEL = {
    ECONOMY: "Low",
    NORMAL: "Medium",
    HIGH: "High",
    VERY_HIGH: "VeryHigh",
    MAX_ACCEPTANCE: "VeryHigh",
}

# UnsafeMax is NON-EXECUTABLE — refused at the call site, so a future edit
# to the table above cannot quietly enable a 64 SOL bid. Compared
# case-insensitively, because a case bug is what broke the primary.
NON_EXECUTABLE_HELIUS_LEVELS = frozenset({"unsafemax"})


class NonExecutablePriorityLevel(ValueError):
    """A priority level that exists to win auctions, not to price them."""


def assert_executable_level(level: str) -> str:
    """Refuse a priority level no policy may ever bid at."""
    if str(level).strip().lower() in NON_EXECUTABLE_HELIUS_LEVELS:
        raise NonExecutablePriorityLevel(
            f"priority level {level!r} is NON-EXECUTABLE: it is named unsafe "
            f"by its own provider and exists to win auctions, not to price "
            f"them. Measured live it quoted 160,361,842,105 micro-lamports/CU "
            f"— 64.144736842 SOL on a 400k-CU transaction.")
    return level


# getRecentPrioritizationFees fallback: percentile over recent slots.
_FALLBACK_PERCENTILE = {
    ECONOMY: 0.25,
    NORMAL: 0.50,
    HIGH: 0.75,
    VERY_HIGH: 0.90,
    MAX_ACCEPTANCE: 0.95,
}

# ── Action policies: WHAT ECONOMIC RULES APPLY ──────────────────────────
# Orthogonal to priority level. Risk reduction may rationally outbid risk
# creation; that is a property of the ACTION, not of how hard we are bidding.
NORMAL_ENTRY = "NORMAL_ENTRY"
NORMAL_EXIT = "NORMAL_EXIT"
URGENT_EXIT = "URGENT_EXIT"
SEVERE_RISK_EXIT = "SEVERE_RISK_EXIT"

ACTION_POLICIES = (NORMAL_ENTRY, NORMAL_EXIT, URGENT_EXIT, SEVERE_RISK_EXIT)

# Older callers name the action rather than the policy. Same thing, so they
# resolve to it rather than to a second set of rules.
ENTRY = "ENTRY"
PROFIT_EXIT = "PROFIT_EXIT"
URGENT_RISK_REDUCTION = "URGENT_RISK_REDUCTION"

_ACTION_ALIAS = {
    ENTRY: NORMAL_ENTRY,
    PROFIT_EXIT: NORMAL_EXIT,
    URGENT_RISK_REDUCTION: URGENT_EXIT,
}

# WHICH PRIORITY LEVELS AN ACTION MAY SELECT. This is the ONLY coupling
# between the two dimensions, and it runs the correct way: the action
# constrains how hard we may bid. It does NOT let the priority level
# choose which economic caps apply.
#
# The removed defect: a table mapped HIGH -> NORMAL_EXIT, so an ENTRY that
# selected HIGH priority silently inherited EXIT economics. It looked
# coherent only because both absolute ceilings happened to be 0.002 SOL —
# raising the exit ceiling would have quietly raised what an entry could
# spend, which is precisely the coupling that must not exist.
ALLOWED_PRIORITY_LEVELS = {
    NORMAL_ENTRY: (ECONOMY, NORMAL, HIGH),
    NORMAL_EXIT: (ECONOMY, NORMAL, HIGH, VERY_HIGH),
    URGENT_EXIT: PRIORITY_LEVELS,
    SEVERE_RISK_EXIT: PRIORITY_LEVELS,
}
ALLOWED_POLICIES = ALLOWED_PRIORITY_LEVELS          # legacy name

# Actions that are shedding risk rather than creating it. They may proceed
# on a bounded emergency allowance when the estimator is blind; an entry
# may not.
RISK_REDUCING_ACTIONS = frozenset({URGENT_EXIT, SEVERE_RISK_EXIT})


def resolve_action_policy(action: str) -> str | None:
    """Map any accepted action name onto its economic policy."""
    if action in ACTION_POLICIES:
        return action
    return _ACTION_ALIAS.get(action)


def action_caps(action: str) -> dict:
    """The operator's economic caps for an ACTION. Never for a priority."""
    from lib.solana_fee_policy import caps_for
    policy = resolve_action_policy(action) or NORMAL_ENTRY
    return caps_for(policy)


def total_cap_lamports(action: str) -> tuple[int, str]:
    """The absolute total-network-fee ceiling for an ACTION, with source."""
    from lib.solana_fee_policy import total_cap_lamports as _policy_total
    return _policy_total(resolve_action_policy(action) or NORMAL_ENTRY)


def priority_cap_lamports(action: str) -> tuple[int, str]:
    """The priority-fee ceiling for an ACTION, with source."""
    caps = action_caps(action)
    return (int(caps["max_priority_fee_lamports"]),
            f"{caps['policy_version']}/{caps['action']}")


MAX_ESTIMATE_AGE_MS = 30_000

# Quality of the MEASUREMENT.
MEASURED_HELIUS = "MEASURED_HELIUS_PRIORITY_ESTIMATE"
MEASURED_RPC_FALLBACK = "MEASURED_RECENT_PRIORITIZATION_FEES"
UNKNOWN = "UNKNOWN"

# Quality of the CONTEXT the measurement was made in. Solana prices
# inclusion around the accounts a transaction writes, so these are
# measurements of progressively different things.
CONTEXT_TRANSACTION = "TRANSACTION_SPECIFIC"
CONTEXT_LOCAL_ACCOUNTS = "LOCAL_ACCOUNT_SET"
CONTEXT_GLOBAL = "GLOBAL_ESTIMATE"
CONTEXT_NONE = "NO_CONTEXT"

# Estimator identities, distinguishing the account-aware fallback from the
# coarse global one. "It answered" and "it answered about THIS transaction"
# are different claims.
EST_HELIUS = "helius.getPriorityFeeEstimate"
EST_RPC_LOCAL = "rpc.getRecentPrioritizationFees.LOCAL_ACCOUNT_FALLBACK"
EST_RPC_GLOBAL = "rpc.getRecentPrioritizationFees.GLOBAL_FALLBACK"

# ── Provider identity, so fallback health lands on the EXISTING surface ──
PRIMARY_PROVIDER = "helius"
PRIMARY_CAPABILITY = "solana_priority_fee_estimate"
FALLBACK_PROVIDER = "solana_rpc"
FALLBACK_CAPABILITY = "recent_prioritization_fees"

# Primary error classes. The distinction that matters is TRANSIENT vs NOT:
# a 500 may fix itself, a malformed request never will.
ERR_MALFORMED_REQUEST = "MALFORMED_REQUEST"
ERR_NOT_CONFIGURED = "NOT_CONFIGURED"
ERR_AUTH = "AUTH_FAILED"
ERR_RATE_LIMITED = "RATE_LIMITED"
ERR_PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
ERR_EMPTY_RESPONSE = "EMPTY_RESPONSE"
ERR_TRANSPORT = "TRANSPORT_FAILURE"

ERROR_CLASSES = (ERR_MALFORMED_REQUEST, ERR_NOT_CONFIGURED, ERR_AUTH,
                 ERR_RATE_LIMITED, ERR_PAYMENT_REQUIRED, ERR_EMPTY_RESPONSE,
                 ERR_TRANSPORT)

NEVER_TRANSIENT = frozenset({ERR_MALFORMED_REQUEST, ERR_NOT_CONFIGURED,
                             ERR_AUTH, ERR_PAYMENT_REQUIRED})

PRIMARY_FAILURE_ESCALATION = 3

_JSONRPC_MALFORMED = frozenset({-32700, -32600, -32601, -32602})

# Refusal reasons, each naming a distinct economic lesson.
FEE_ESTIMATE_UNKNOWN = "FEE_ESTIMATE_UNKNOWN"
FEE_EXCEEDS_AUTHORISED_POLICY = "FEE_EXCEEDS_AUTHORISED_POLICY"
FEE_DESTROYS_EDGE = "FEE_DESTROYS_EDGE"
FEE_EXCEEDS_NOTIONAL_CAP = "FEE_EXCEEDS_NOTIONAL_CAP"
FEE_USD_UNPRICEABLE = "FEE_USD_UNPRICEABLE"
PRIORITY_NOT_PERMITTED_FOR_ACTION = "POLICY_NOT_PERMITTED_FOR_ACTION"
UNKNOWN_ACTION = "UNKNOWN_ACTION"

# Binding constraints, so a refusal or a clamp says which rule bit.
BINDING_TOTAL_CEILING = "TOTAL_NETWORK_FEE_CEILING"
BINDING_PRIORITY_CEILING = "PRIORITY_FEE_CEILING"
BINDING_NONE = "NONE_MEASURED_FEE_WITHIN_POLICY"


# ── The conversions. One function each; nothing else may do this math. ──
def executable_compute_unit_price_micro_lamports(raw_estimate: float) -> int:
    """The provider's estimate -> the u64 that can actually be executed.

    Helius returns priorityFeeEstimate as a FLOATING-POINT number, but the
    instruction it feeds — SetComputeUnitPrice(micro_lamports: u64) — takes
    an integer. Something has to quantize, and doing it in two places is
    how the derived fee and the persisted price came to disagree: an
    earlier version derived the fee from the raw float and then persisted
    int(raw), so recomputing from the record gave a different, lower number.

    Quantizing FIRST makes the estimate self-consistent: this integer is
    the value that goes on the wire, the value the fee is derived from, and
    the value that is persisted. CEILING, conservatively — bidding a
    fraction less than indicated is the one rounding direction that can
    lose an inclusion.
    """
    if raw_estimate < 0:
        raise ValueError(
            f"negative compute unit price: {raw_estimate!r} micro-lamports/CU")
    return int(Decimal(str(raw_estimate)).to_integral_value(
        rounding=ROUND_CEILING))


def priority_fee_lamports(*, compute_unit_price_micro_lamports: int,
                          compute_unit_limit: int) -> int:
    """micro-lamports/CU x CU -> LAMPORTS. Exact, and rounded UP.

        priority_fee_lamports = ceil(compute_unit_price_micro_lamports
                                     * compute_unit_limit / 1_000_000)

    Decimal rather than float because the products are large enough to lose
    integer precision in float64. CEILING rather than truncation because a
    fee rounded down is a fee that was never offered to the chain.

    THE PRICE MUST ALREADY BE THE EXECUTABLE INTEGER. A fractional price is
    refused rather than quietly quantized here, because quantizing in two
    places is the defect described above. Callers quantize once, with
    executable_compute_unit_price_micro_lamports().
    """
    if compute_unit_price_micro_lamports < 0 or compute_unit_limit < 0:
        raise ValueError(
            f"negative fee input: {compute_unit_price_micro_lamports!r} "
            f"micro-lamports/CU x {compute_unit_limit!r} CU")
    price = Decimal(str(compute_unit_price_micro_lamports))
    if price != price.to_integral_value():
        raise ValueError(
            f"compute_unit_price_micro_lamports must be the exact executable "
            f"integer (SetComputeUnitPrice takes a u64), not "
            f"{compute_unit_price_micro_lamports!r}. Quantize once with "
            f"executable_compute_unit_price_micro_lamports().")
    micro_lamports = price * Decimal(int(compute_unit_limit))
    return int((micro_lamports / MICRO_LAMPORTS_PER_LAMPORT)
               .to_integral_value(rounding=ROUND_CEILING))


def lamports_to_sol(lamports: int) -> float:
    """lamports -> SOL. Display only; lamports stay canonical."""
    return int(lamports) / LAMPORTS_PER_SOL


def sol_to_lamports(sol: float) -> int:
    """SOL -> lamports."""
    return int(round(float(sol) * LAMPORTS_PER_SOL))


@dataclass(frozen=True)
class NetworkFeeEstimate:
    """WHAT DOES CURRENT NETWORK EVIDENCE INDICATE?

    Measurement only. No operator policy has touched any number here — a
    ceiling applied to a measurement produces a figure that is neither.
    """
    ok: bool
    priority_level: str
    raw_provider_price: float | None = None
    executable_compute_unit_price_micro_lamports: int = 0
    compute_unit_limit: int = DEFAULT_SWAP_COMPUTE_UNITS
    compute_unit_limit_source: str = DEFAULT_BUDGET_ASSUMPTION
    measured_units_consumed: int | None = None
    measured_priority_fee_lamports: int = 0
    measured_base_fee_lamports: int = PROTOCOL_BASE_FEE_LAMPORTS
    base_fee_source: str = BASE_FEE_ASSUMED
    estimator: str = UNKNOWN
    context_quality: str = CONTEXT_NONE
    quality: str = UNKNOWN
    observed_at: str = ""
    estimate_age_ms: float = 0.0
    reason: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def measured_total_network_fee_lamports(self) -> int:
        return (int(self.measured_base_fee_lamports)
                + int(self.measured_priority_fee_lamports))

    @property
    def measured_total_network_fee_sol(self) -> float:
        return lamports_to_sol(self.measured_total_network_fee_lamports)

    def measured_total_usd(self, sol_price_usd: float) -> float | None:
        if not sol_price_usd or sol_price_usd <= 0:
            return None
        return self.measured_total_network_fee_sol * float(sol_price_usd)

    # ── Names older callers use. Same measured numbers, no policy. ──
    @property
    def priority_policy(self) -> str:
        return self.priority_level

    @property
    def compute_unit_price_micro_lamports(self) -> int:
        return self.executable_compute_unit_price_micro_lamports

    @property
    def base_fee_lamports(self) -> int:
        return int(self.measured_base_fee_lamports)

    @property
    def priority_estimate_source(self) -> str:
        return self.estimator

    @property
    def total_network_fee_lamports(self) -> int:
        return self.measured_total_network_fee_lamports

    @property
    def total_network_fee_sol(self) -> float:
        return self.measured_total_network_fee_sol

    @property
    def total_lamports(self) -> int:
        return self.measured_total_network_fee_lamports

    @property
    def total_sol(self) -> float:
        return self.measured_total_network_fee_sol

    def total_usd(self, sol_price_usd: float) -> float | None:
        return self.measured_total_usd(sol_price_usd)


# The old name, kept so type checks and isinstance calls elsewhere keep
# working. It is the same class: there is one estimate type, not two.
FeeEstimate = NetworkFeeEstimate


@dataclass(frozen=True)
class FeeAuthorization:
    """WHAT IS JARVIS ALLOWED AND WILLING TO PAY?

    Never a measurement. `authorized_bid_lamports` is a decision, and when
    it sits below what the network indicated,
    `bid_below_measured_requirement` says so out loud rather than letting a
    clamped number pass itself off as an estimate.
    """
    allowed: bool
    action_policy: str
    priority_level: str
    policy_version: str
    measured_total_network_fee_lamports: int | None = None
    operator_total_fee_limit_lamports: int | None = None
    authorized_bid_lamports: int = 0
    bid_below_measured_requirement: bool = False
    binding_constraint: str | None = None
    fee_usd: float | None = None
    expected_edge_usd: float | None = None
    notional_usd: float | None = None
    quality: str = UNKNOWN
    context_quality: str = CONTEXT_NONE
    refusal_reason: str | None = None
    detail: str | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def authorized_bid_sol(self) -> float:
        return lamports_to_sol(self.authorized_bid_lamports)

    @property
    def ok(self) -> bool:
        return self.allowed

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action_policy": self.action_policy,
            "priority_level": self.priority_level,
            "policy_version": self.policy_version,
            "measured_total_network_fee_lamports":
                self.measured_total_network_fee_lamports,
            "operator_total_fee_limit_lamports":
                self.operator_total_fee_limit_lamports,
            "authorized_bid_lamports": self.authorized_bid_lamports,
            "authorized_bid_sol": self.authorized_bid_sol,
            "bid_below_measured_requirement":
                self.bid_below_measured_requirement,
            "binding_constraint": self.binding_constraint,
            "fee_usd": self.fee_usd,
            "quality": self.quality,
            "context_quality": self.context_quality,
            "refusal_reason": self.refusal_reason,
            "detail": self.detail,
            "provenance": self.provenance,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_fetch(method: str, params):
    """Live RPC through the EXISTING Helius client. Injectable for tests.

    HERMETIC BY CONSTRUCTION, the same way `lib.venues` is. Now that
    canonical DEX execution actually calls the estimator, any test that
    exercises that path without injecting a `fetch` would reach real Helius
    — slowly, non-deterministically, and against a release gate that
    ci.yml promises is hermetic. The refusal is immediate and named, so an
    accidental network call announces itself instead of passing as "the
    provider had nothing to say".

    Tests that genuinely check the live service opt out with
    JARVIS_REAL_PROVIDER_TESTS=1, the existing convention.
    """
    if (os.getenv("JARVIS_UNDER_PYTEST") == "1"
            and os.getenv("JARVIS_REAL_PROVIDER_TESTS") != "1"):
        raise RuntimeError(
            "hermetic test: the Solana fee estimator was called without an "
            "injected fetch. Canonical DEX execution now measures the live "
            "fee market, so pass fetch=/fee_fetch= (see "
            "tests/test_dex_canonical_fee_integration.py), or set "
            "JARVIS_REAL_PROVIDER_TESTS=1 for a deliberate live probe.")
    from lib import helius_client as HC
    return HC.rpc(method, params)


def classify_primary_error(exc: BaseException) -> str:
    """Name the failure so the operator knows whether retrying can help.

    Duck-typed on `.status` (lib.helius_client.HeliusError carries the
    JSON-RPC code or the HTTP status there) so this module never has to
    import the client merely to describe a failure.
    """
    text = str(exc)
    status = getattr(exc, "status", None)
    if "HELIUS_API_KEY is not set" in text:
        return ERR_NOT_CONFIGURED
    if isinstance(status, int):
        if status in _JSONRPC_MALFORMED:
            return ERR_MALFORMED_REQUEST
        if status == 402:
            return ERR_PAYMENT_REQUIRED
        if status == 429:
            return ERR_RATE_LIMITED
        if status in (401, 403):
            return ERR_AUTH
    if "no priorityFeeEstimate in response" in text:
        return ERR_EMPTY_RESPONSE
    return ERR_TRANSPORT


_HEALTH_STATUS_FOR = {
    ERR_MALFORMED_REQUEST: "DEGRADED",
    ERR_NOT_CONFIGURED: "NOT_CONFIGURED",
    ERR_AUTH: "AUTH_FAILED",
    ERR_RATE_LIMITED: "RATE_LIMITED",
    ERR_PAYMENT_REQUIRED: "PAYMENT_REQUIRED",
    ERR_EMPTY_RESPONSE: "DEGRADED",
    ERR_TRANSPORT: "UNAVAILABLE",
}


def _record_health(provider: str, capability: str, *, status: str,
                   latency_ms: float, error: str | None = None,
                   detail: str | None = None, rows: int | None = None) -> None:
    """Health telemetry must never be able to break a fee estimate."""
    try:
        from lib import provider_health as PH
        PH.record(provider, capability, status=status, latency_ms=latency_ms,
                  error=error, detail=detail, rows=rows)
    except Exception as exc:                                 # noqa: BLE001
        logger.debug("[SolanaFees] could not record %s/%s health: %s",
                     provider, capability, exc)


def _normalise_accounts(writable_account_keys) -> list[str]:
    """Writable accounts, de-duplicated, order preserved, capped at 32."""
    seen: list[str] = []
    for key in (writable_account_keys or []):
        text = str(key).strip()
        if text and text not in seen:
            seen.append(text)
    return seen[:32]


def _helius_priority(level: str, *, account_keys, transaction_b64, fetch):
    """Helius getPriorityFeeEstimate — the per-account fee market.

    USES THE BEST CONTEXT AVAILABLE, and says which it used. A serialized
    transaction prices the exact accounts the transaction writes; an
    account list prices that local market; a global estimate prices the
    network. All three are measurements, of narrowing relevance.
    """
    helius_level = assert_executable_level(_HELIUS_LEVEL[level])
    options = {"priorityLevel": helius_level}
    params: dict = {"options": options}
    if transaction_b64:
        params["transaction"] = transaction_b64
        context = CONTEXT_TRANSACTION
    elif account_keys:
        params["accountKeys"] = list(account_keys)
        context = CONTEXT_LOCAL_ACCOUNTS
    else:
        options["includeAllPriorityFeeLevels"] = False
        context = CONTEXT_GLOBAL
    result = fetch("getPriorityFeeEstimate", [params])
    est = result.get("priorityFeeEstimate") if isinstance(result, dict) else None
    if est is None:
        raise ValueError(f"no priorityFeeEstimate in response: {result!r}")
    return float(est), context     # micro-lamports per compute unit


def _rpc_fallback_priority(level: str, *, fetch, account_keys=None):
    """getRecentPrioritizationFees percentile — standard Solana RPC.

    ACCOUNT-AWARE WHEN IT CAN BE. `prioritizationFee` is documented in the
    SAME unit as the Helius estimate (micro-lamports per compute unit), so
    the two are interchangeable at this boundary — but the ACCOUNTLESS call
    reports the global market, and a live probe of it returned 150 slots of
    exactly 0.0. That is a fact about the network, not proof that a
    transaction touching a hot pool needs no priority fee, so the two
    variants are labelled differently and never conflated.
    """
    params = [list(account_keys)] if account_keys else []
    rows = fetch("getRecentPrioritizationFees", params)
    fees = sorted(float(r.get("prioritizationFee") or 0.0)
                  for r in (rows or []) if isinstance(r, dict))
    if not fees:
        raise ValueError("getRecentPrioritizationFees returned no rows")
    idx = min(len(fees) - 1, int(len(fees) * _FALLBACK_PERCENTILE[level]))
    if account_keys:
        return fees[idx], EST_RPC_LOCAL, CONTEXT_LOCAL_ACCOUNTS
    return fees[idx], EST_RPC_GLOBAL, CONTEXT_GLOBAL


def estimate_network_fee(priority_level: str, *,
                         writable_account_keys=None,
                         account_keys=None,
                         transaction_b64: str | None = None,
                         compute_unit_limit: int | None = None,
                         compute_units_source: str | None = None,
                         measured_units_consumed: int | None = None,
                         base_fee_lamports: int | None = None,
                         base_fee_source: str | None = None,
                         fetch=None,
                         record_health: bool = True) -> NetworkFeeEstimate:
    """MEASURE this transaction's network fee. Apply no policy whatsoever.

    Tries Helius first (per-account fee market), the standard RPC second,
    and refuses to invent a number third. A dead estimator returns
    quality=UNKNOWN — what that MEANS for a trade is authorize_fee's
    decision, because an entry and an emergency exit answer it differently.

    EVERY PRIMARY ATTEMPT IS RECORDED, win or lose. The fallback once
    worked so well that a malformed primary request survived undetected:
    the numbers looked reasonable because they WERE reasonable, they were
    simply coming from the second authority every single time. Falling back
    is correct; falling back silently and permanently is not.
    """
    if priority_level not in PRIORITY_LEVELS:
        return NetworkFeeEstimate(
            ok=False, priority_level=str(priority_level),
            reason=f"unknown priority level {priority_level!r}")

    fetch = fetch or _default_fetch
    accounts = _normalise_accounts(writable_account_keys or account_keys)
    cu_limit = int(compute_unit_limit or DEFAULT_SWAP_COMPUTE_UNITS)
    cu_source = (compute_units_source
                 or (MEASURED_UNITS_CONSUMED if measured_units_consumed
                     else ("CALLER_SIMULATION" if compute_unit_limit
                           else DEFAULT_BUDGET_ASSUMPTION)))
    base_fee = int(base_fee_lamports if base_fee_lamports is not None
                   else PROTOCOL_BASE_FEE_LAMPORTS)
    base_source = base_fee_source or (BASE_FEE_MEASURED
                                      if base_fee_lamports is not None
                                      else BASE_FEE_ASSUMED)
    started = time.perf_counter()

    def _elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    micro_per_cu = None
    estimator = UNKNOWN
    context = CONTEXT_NONE
    quality = UNKNOWN
    detail = None
    primary_error_class = None
    try:
        micro_per_cu, context = _helius_priority(
            priority_level, account_keys=accounts,
            transaction_b64=transaction_b64, fetch=fetch)
        estimator = EST_HELIUS
        quality = MEASURED_HELIUS
        if record_health:
            _record_health(PRIMARY_PROVIDER, PRIMARY_CAPABILITY,
                           status="HEALTHY", latency_ms=_elapsed_ms(), rows=1,
                           detail=f"priorityLevel="
                                  f"{_HELIUS_LEVEL[priority_level]} "
                                  f"context={context}")
    except Exception as first:                               # noqa: BLE001
        primary_error_class = classify_primary_error(first)
        if record_health:
            _record_health(
                PRIMARY_PROVIDER, PRIMARY_CAPABILITY,
                status=_HEALTH_STATUS_FOR.get(primary_error_class, "DEGRADED"),
                latency_ms=_elapsed_ms(), error=str(first),
                detail=f"{primary_error_class} "
                       f"priorityLevel={_HELIUS_LEVEL[priority_level]}")
        try:
            micro_per_cu, estimator, context = _rpc_fallback_priority(
                priority_level, fetch=fetch, account_keys=accounts)
            quality = MEASURED_RPC_FALLBACK
            detail = f"helius estimate unavailable ({first}); used fallback"
            if record_health:
                _record_health(FALLBACK_PROVIDER, FALLBACK_CAPABILITY,
                               status="HEALTHY", latency_ms=_elapsed_ms(),
                               rows=1,
                               detail=f"fallback activated after "
                                      f"{primary_error_class} on "
                                      f"{PRIMARY_PROVIDER} context={context}")
        except Exception as second:                          # noqa: BLE001
            if record_health:
                _record_health(FALLBACK_PROVIDER, FALLBACK_CAPABILITY,
                               status="UNAVAILABLE", latency_ms=_elapsed_ms(),
                               error=str(second))
            return NetworkFeeEstimate(
                ok=False, priority_level=priority_level,
                compute_unit_limit=cu_limit,
                compute_unit_limit_source=cu_source,
                measured_base_fee_lamports=base_fee,
                base_fee_source=base_source,
                quality=UNKNOWN, context_quality=CONTEXT_NONE,
                observed_at=_now_iso(), estimate_age_ms=_elapsed_ms(),
                reason=(f"no trustworthy priority estimate: helius=({first}) "
                        f"fallback=({second})"),
                provenance={"helius_error": str(first)[:200],
                            "helius_error_class": primary_error_class,
                            "fallback_error": str(second)[:200],
                            "writable_account_keys": accounts})

    # QUANTIZE ONCE, THEN DERIVE, so the persisted price is the price the
    # fee was computed from and the price that would go on the wire. The
    # provider's exact value survives in provenance as evidence.
    executable_price = executable_compute_unit_price_micro_lamports(
        micro_per_cu)
    measured_priority = priority_fee_lamports(
        compute_unit_price_micro_lamports=executable_price,
        compute_unit_limit=cu_limit)

    return NetworkFeeEstimate(
        ok=True, priority_level=priority_level,
        raw_provider_price=micro_per_cu,
        executable_compute_unit_price_micro_lamports=executable_price,
        compute_unit_limit=cu_limit,
        compute_unit_limit_source=cu_source,
        measured_units_consumed=measured_units_consumed,
        measured_priority_fee_lamports=measured_priority,
        measured_base_fee_lamports=base_fee,
        base_fee_source=base_source,
        estimator=estimator, context_quality=context, quality=quality,
        observed_at=_now_iso(), estimate_age_ms=_elapsed_ms(),
        reason=detail,
        provenance={"micro_lamports_per_cu_raw": micro_per_cu,
                    "executable_compute_unit_price_micro_lamports":
                        executable_price,
                    "price_quantization": "CEILING_TO_EXECUTABLE_U64",
                    "measured_priority_fee_lamports": measured_priority,
                    "writable_account_keys": accounts,
                    "transaction_context": bool(transaction_b64),
                    "primary_error_class": primary_error_class,
                    "no_policy_applied": True})


# ── Authorization: what may we pay for THIS action? ────────────────────
def authorize_fee(estimate: NetworkFeeEstimate, *, action: str,
                  sol_price_usd: float = 0.0,
                  expected_edge_usd: float | None = None,
                  notional_usd: float | None = None) -> FeeAuthorization:
    """Turn a MEASUREMENT plus operator policy into a BID. Never the reverse.

    THE ASYMMETRY, STATED: risk reduction may rationally tolerate a larger
    fee than risk creation — paying up to escape is different from paying up
    to enter. But asymmetry is not immunity: even the emergency path is
    bounded, because an unbounded 'emergency' is a wallet drain wearing a
    siren.

    A LIVE ESTIMATE ABOVE THE AUTHORISED POLICY IS A REFUSAL FOR AN ENTRY,
    NOT A REASON TO RAISE THE POLICY. Paying the ceiling instead of the
    market price does not buy the inclusion the estimate said was needed; it
    merely spends the ceiling. An exit may still bid the ceiling — getting
    out slowly beats not getting out — and it is told plainly that its bid
    is below what the network indicated.
    """
    from lib.solana_fee_policy import FEE_POLICY_VERSION

    policy = resolve_action_policy(action)
    if policy is None:
        return FeeAuthorization(
            allowed=False, action_policy=str(action),
            priority_level=getattr(estimate, "priority_level", UNKNOWN),
            policy_version=FEE_POLICY_VERSION,
            refusal_reason=UNKNOWN_ACTION,
            detail=f"unknown action {action!r}")

    caps = action_caps(policy)
    total_limit = int(caps["max_total_network_fee_lamports"])
    priority_limit = int(caps["max_priority_fee_lamports"])
    level = estimate.priority_level

    base = {
        "action_policy": policy,
        "priority_level": level,
        "policy_version": caps["policy_version"],
        "operator_total_fee_limit_lamports": total_limit,
        "expected_edge_usd": expected_edge_usd,
        "notional_usd": notional_usd,
        "context_quality": getattr(estimate, "context_quality", CONTEXT_NONE),
    }
    provenance = {
        "operator_priority_fee_limit_lamports": priority_limit,
        "caps_source": f"{caps['policy_version']}/{policy}",
        "estimator": getattr(estimate, "estimator", UNKNOWN),
        "cap_is_policy_not_network_truth": True,
        "estimate_untouched_by_policy": True,
    }

    # ── No trustworthy measurement ──────────────────────────────────────
    if not estimate.ok or estimate.quality == UNKNOWN:
        if policy in RISK_REDUCING_ACTIONS:
            from lib.solana_fee_policy import emergency_fallback_lamports
            fallback, fallback_source = emergency_fallback_lamports()
            return FeeAuthorization(
                allowed=True, **base, quality=UNKNOWN,
                measured_total_network_fee_lamports=None,
                authorized_bid_lamports=fallback,
                bid_below_measured_requirement=False,
                binding_constraint="EMERGENCY_FALLBACK",
                detail=("no live estimate; the separately configured "
                        "emergency cap applies. This buys bounded "
                        "aggression, not certainty."),
                provenance={**provenance,
                            "fallback_source": fallback_source,
                            "policy": "EMERGENCY_FALLBACK"})
        return FeeAuthorization(
            allowed=False, **base, quality=UNKNOWN,
            refusal_reason=FEE_ESTIMATE_UNKNOWN,
            detail=("no trustworthy network fee estimate — a normal action "
                    "does not proceed on a guessed cost. "
                    + (estimate.reason or "")),
            provenance=provenance)

    # ── The action constrains how hard we may bid ───────────────────────
    if level not in ALLOWED_PRIORITY_LEVELS[policy]:
        return FeeAuthorization(
            allowed=False, **base, quality=estimate.quality,
            measured_total_network_fee_lamports=(
                estimate.measured_total_network_fee_lamports),
            refusal_reason=PRIORITY_NOT_PERMITTED_FOR_ACTION,
            detail=(f"{policy} may not select priority level {level}; "
                    f"permitted: {ALLOWED_PRIORITY_LEVELS[policy]}"),
            provenance=provenance)

    measured_total = estimate.measured_total_network_fee_lamports
    measured_priority = estimate.measured_priority_fee_lamports

    # Both operator bounds apply and the tighter one wins. The total is the
    # authored number; the priority ceiling is derived from it, so they
    # normally coincide — an env override of one alone cannot escape the
    # other.
    priority_allowed_by_total = max(
        0, total_limit - int(estimate.measured_base_fee_lamports))
    effective_priority_cap = min(priority_limit, priority_allowed_by_total)
    binding = (BINDING_TOTAL_CEILING
               if priority_allowed_by_total <= priority_limit
               else BINDING_PRIORITY_CEILING)

    over_policy = measured_priority > effective_priority_cap
    authorized_bid = (int(estimate.measured_base_fee_lamports)
                      + min(measured_priority, effective_priority_cap))

    fee_usd = estimate.measured_total_usd(sol_price_usd)
    base = {**base, "quality": estimate.quality,
            "measured_total_network_fee_lamports": measured_total,
            "fee_usd": fee_usd}

    if over_policy:
        detail = (f"live estimate {measured_total} lamports "
                  f"({lamports_to_sol(measured_total):.9f} SOL) exceeds the "
                  f"authorised {total_limit} lamports "
                  f"({lamports_to_sol(total_limit):.9f} SOL) for {policy}. "
                  f"The policy ceiling is not raised to meet the market.")
        if policy == NORMAL_ENTRY:
            # Creating risk at a price we did not authorise: refuse.
            return FeeAuthorization(
                allowed=False, **base,
                authorized_bid_lamports=0,
                bid_below_measured_requirement=True,
                binding_constraint=binding,
                refusal_reason=FEE_EXCEEDS_AUTHORISED_POLICY,
                detail=detail, provenance=provenance)
        # Shedding risk: bid the ceiling, and say plainly that the bid is
        # below what the network indicated. A capped exit bid is a real
        # decision, not a measurement, and pretending otherwise is how a
        # partial-inclusion risk becomes invisible.
        return FeeAuthorization(
            allowed=True, **base,
            authorized_bid_lamports=authorized_bid,
            bid_below_measured_requirement=True,
            binding_constraint=binding,
            detail=detail + " The exit bids the ceiling.",
            provenance=provenance)

    # ── Within the absolute ceiling. Percentage caps still apply. ───────
    if policy == NORMAL_ENTRY:
        edge_cap = caps["max_fee_pct_expected_edge"]
        notional_cap = caps["max_fee_pct_notional"]
        # A USD PRICE IS REQUIRED ONLY WHEN A PERCENTAGE CAP IS ACTUALLY
        # EVALUABLE. The absolute lamport ceiling above is the primary
        # authorization and needs no price at all; the percentage caps are
        # additional protections, and an entry must not proceed while a
        # protection its policy declares cannot be checked. Demanding a
        # price when neither an edge nor a notional was supplied would
        # refuse trades for failing a test nobody asked for.
        edge_applies = (expected_edge_usd is not None
                        and float(expected_edge_usd) > 0
                        and edge_cap is not None)
        notional_applies = (notional_usd is not None
                            and float(notional_usd or 0) > 0
                            and notional_cap is not None)
        if (edge_applies or notional_applies) and fee_usd is None:
            return FeeAuthorization(
                allowed=False, **base, authorized_bid_lamports=0,
                binding_constraint=binding,
                refusal_reason=FEE_USD_UNPRICEABLE,
                detail=("no SOL price, so the fee cannot be valued against "
                        "the expected edge or the trade notional — an entry "
                        "does not skip a protection it cannot evaluate"),
                provenance=provenance)
        if fee_usd is not None and expected_edge_usd is not None and expected_edge_usd > 0:
            pct_of_edge = 100.0 * fee_usd / float(expected_edge_usd)
            if edge_cap is not None and pct_of_edge > edge_cap:
                return FeeAuthorization(
                    allowed=False, **base, authorized_bid_lamports=0,
                    binding_constraint="EXPECTED_EDGE_CAP",
                    refusal_reason=FEE_DESTROYS_EDGE,
                    detail=(f"network fee ${fee_usd:.4f} is "
                            f"{pct_of_edge:.1f}% of the "
                            f"${float(expected_edge_usd):.2f} expected edge "
                            f"(policy cap {edge_cap}%)"),
                    provenance=provenance)
        if fee_usd is not None and notional_usd and float(notional_usd) > 0:
            pct_of_notional = 100.0 * fee_usd / float(notional_usd)
            if notional_cap is not None and pct_of_notional > notional_cap:
                return FeeAuthorization(
                    allowed=False, **base, authorized_bid_lamports=0,
                    binding_constraint="NOTIONAL_CAP",
                    refusal_reason=FEE_EXCEEDS_NOTIONAL_CAP,
                    detail=(f"network fee ${fee_usd:.4f} is "
                            f"{pct_of_notional:.2f}% of the "
                            f"${float(notional_usd):.2f} trade "
                            f"(policy cap {notional_cap}%)"),
                    provenance=provenance)

    return FeeAuthorization(
        allowed=True, **base,
        authorized_bid_lamports=authorized_bid,
        bid_below_measured_requirement=False,
        binding_constraint=BINDING_NONE,
        provenance=provenance)


# ── Is the PRIMARY authority actually working? ─────────────────────────
def fee_estimator_health() -> dict:
    """Whether the fallback is covering for a primary that never works.

    THE FAILURE THIS EXISTS FOR. Phase 6 found `priorityLevel="high"`
    returning -32602 Invalid params while "High" succeeded. Nothing broke:
    getRecentPrioritizationFees answered every time, the estimates were
    sane, and the primary authority had — as far as anyone could tell —
    never once returned a number. A fallback that good is indistinguishable
    from a working system until somebody goes looking.

    Reads the EXISTING provider-health table rather than keeping counters of
    its own, so this is the same answer the Ops panel shows.
    """
    try:
        from lib import provider_health as PH
        rows = {(r["provider"], r["capability"]): r for r in PH.snapshot()}
    except Exception as exc:                                 # noqa: BLE001
        return {"available": False,
                "reason": f"health surface unreadable: {exc}"}

    primary = rows.get((PRIMARY_PROVIDER, PRIMARY_CAPABILITY))
    fallback = rows.get((FALLBACK_PROVIDER, FALLBACK_CAPABILITY))

    def _n(row, key) -> int:
        return int((row or {}).get(key) or 0)

    primary_successes = _n(primary, "success_count")
    primary_failures = _n(primary, "failure_count")
    primary_attempts = primary_successes + primary_failures
    consecutive = _n(primary, "consecutive_failures")
    fallback_activations = _n(fallback, "success_count")

    last_error_class = None
    if primary and primary.get("detail"):
        head = str(primary["detail"]).split(" ", 1)[0]
        if head in ERROR_CLASSES:
            last_error_class = head

    never_succeeded = primary_attempts > 0 and primary_successes == 0
    persistently_failing = consecutive >= PRIMARY_FAILURE_ESCALATION
    non_transient = bool(last_error_class in NEVER_TRANSIENT
                         and consecutive > 0)

    # A SINGLE TRANSIENT FAILURE IS NOT AN ALARM. One 500 on one call is a
    # fact worth recording and nothing worth waking anyone for, so "never
    # succeeded" only escalates once there have been enough attempts for
    # the silence to mean something. A never-transient class escalates
    # immediately instead, because no number of retries will change it.
    standing = never_succeeded and primary_attempts >= PRIMARY_FAILURE_ESCALATION
    masked = standing and fallback_activations > 0
    actionable = bool(standing or persistently_failing or non_transient)

    stamps = []
    if primary and primary.get("last_success_at"):
        stamps.append((str(primary["last_success_at"]), EST_HELIUS))
    if fallback and fallback.get("last_success_at"):
        stamps.append((str(fallback["last_success_at"]),
                       "rpc.getRecentPrioritizationFees"))
    last_successful_source = max(stamps)[1] if stamps else None

    note = None
    if masked:
        note = ("the primary authority has NEVER succeeded while the "
                "fallback serves every call — the estimates are sane but "
                "they are not coming from the per-account fee market")
    elif persistently_failing:
        note = (f"{consecutive} consecutive primary failures "
                f"(>= {PRIMARY_FAILURE_ESCALATION})")
    elif non_transient:
        note = (f"the last primary failure was {last_error_class}, which "
                f"retrying cannot fix")

    return {
        "available": True,
        "primary": {
            "provider": PRIMARY_PROVIDER, "capability": PRIMARY_CAPABILITY,
            "status": (primary or {}).get("status"),
            "attempts": primary_attempts,
            "successes": primary_successes,
            "failures": primary_failures,
            "consecutive_failures": consecutive,
            "last_success_at": (primary or {}).get("last_success_at"),
            "last_failure_at": (primary or {}).get("last_failure_at"),
            "last_error": (primary or {}).get("error"),
            "last_error_class": last_error_class,
        },
        "fallback": {
            "provider": FALLBACK_PROVIDER, "capability": FALLBACK_CAPABILITY,
            "status": (fallback or {}).get("status"),
            "activations": fallback_activations,
            "last_success_at": (fallback or {}).get("last_success_at"),
        },
        "last_successful_source": last_successful_source,
        "primary_never_succeeded": never_succeeded,
        "primary_persistently_failing": persistently_failing,
        "primary_failure_is_non_transient": non_transient,
        "fallback_is_masking_a_dead_primary": masked,
        "actionable": actionable,
        "escalation_threshold": PRIMARY_FAILURE_ESCALATION,
        "note": note,
    }


# ── Estimated vs actual: the calibration loop. Python owns this. ────────
_reconciliations: list[dict] = []
_RECON_WINDOW = 200


def reconcile_fee(estimate: NetworkFeeEstimate, *,
                  actual_total_lamports: int, actual_fee_source: str,
                  authorization: FeeAuthorization | None = None,
                  context: dict | None = None) -> dict:
    """Compare what was MEASURED, what was AUTHORIZED, and what was CHARGED.

    All three are preserved. Replacing any one with another would destroy
    the only evidence of whether a policy overpays, underestimates, or is
    priced about right — and whether a capped bid actually got included.
    """
    est = int(estimate.measured_total_network_fee_lamports)
    act = int(actual_total_lamports)
    err = act - est
    row = {
        "estimated_total_lamports": est,
        "authorized_bid_lamports": (int(authorization.authorized_bid_lamports)
                                    if authorization is not None else None),
        "bid_below_measured_requirement": (
            bool(authorization.bid_below_measured_requirement)
            if authorization is not None else None),
        "actual_total_lamports": act,
        "estimation_error_lamports": err,
        "estimation_error_pct": (100.0 * err / est) if est else None,
        "priority_level": estimate.priority_level,
        "priority_policy": estimate.priority_level,
        "action_policy": (authorization.action_policy
                          if authorization is not None else None),
        "estimator": estimate.estimator,
        "context_quality": estimate.context_quality,
        "quality": estimate.quality,
        "actual_fee_source": actual_fee_source,
        "context": context or {},
        "at": _now_iso(),
    }
    _reconciliations.append(row)
    del _reconciliations[:-_RECON_WINDOW]
    if est and abs(row["estimation_error_pct"]) > 50.0:
        logger.warning("[SolanaFees] estimate off by %.0f%% (%s, %s): "
                       "est %d vs actual %d lamports",
                       row["estimation_error_pct"], estimate.priority_level,
                       estimate.estimator, est, act)
    return row


def reconciliation_summary() -> dict:
    """Bounded rollup for Ops. Per (level, estimator) error statistics."""
    out: dict = {}
    for row in _reconciliations:
        key = f"{row['priority_level']}/{row['estimator']}"
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
