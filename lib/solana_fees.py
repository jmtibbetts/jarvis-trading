"""Solana network fees, estimated live per transaction. GAS IS NOT A CONSTANT.

WHAT THIS REPLACES. Execution economics used to carry a hard-coded gas
cost. Hard-coding any figure as "the" gas cost makes the simulator's
economics a constant in a market that prices inclusion by congestion around
the specific accounts a transaction touches. A simulator that underprices
execution manufactures edge; one that overprices it refuses real edge. Both
are the same defect: the fee was not measured.

WHAT THE OPERATOR HAS ACTUALLY OBSERVED, and what it is evidence OF. These
are operator observations and willingness-to-pay calibration. They are NOT
network truth, not an expected cost, and nothing here may be read back as
an estimate:

    ~0.002  SOL   the normal maximum the operator chooses to pay.
    ~0.003-0.0035 SOL   the extreme of the observed range — which the
                  operator has never actually needed to deliberately pay.

An earlier version of this docstring cited "~0.03 SOL" as the aggressive
observation. That was wrong by 10x, and it was the number that justified
v1's 0.01/0.03/0.05 SOL policy ceilings. A mis-stated observation becomes a
mis-calibrated ceiling: this is exactly why the two live in different
modules and why the numbers live in lib.solana_fee_policy, not here.

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

UNITS ARE PART OF THE TYPE. Every quantity below is named for the unit it
carries, and there is exactly ONE function per conversion:

    compute_unit_price_micro_lamports   what BOTH estimators return:
                                        MICRO-LAMPORTS PER COMPUTE UNIT.
    priority_fee_lamports()             micro-lamports/CU x CU -> lamports
                                        (divide by 1e6, ROUND UP)
    total_network_fee_lamports          base_fee_lamports + priority
    lamports_to_sol()                   lamports -> SOL (divide by 1e9)

CANONICAL UNITS ARE LAMPORTS. SOL and USD are derived display values;
accounting keeps the integers the chain charges.

THE CONVERSION IS EXACT, NOT FLOATING POINT. A live unsafeMax quote of
160,361,842,105 micro-lamports/CU times a 400,000 CU budget is 6.4e16
micro-lamports — past the 9.007e15 boundary where a float64 stops being
able to represent consecutive integers. Decimal keeps the arithmetic exact,
and the rounding is CEILING because a fee rounded down is a fee that was
never offered to the chain.

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
from decimal import ROUND_CEILING, Decimal

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Renamed from the bare `MICRO_LAMPORTS`, which named a unit rather than a
# ratio and read equally well as "some micro-lamports". It is a DIVISOR:
# there are one million micro-lamports in one lamport.
MICRO_LAMPORTS_PER_LAMPORT = 1_000_000

# Protocol base fee per signature. This is Solana's documented constant,
# not a market estimate — the market lives in the priority fee.
PROTOCOL_BASE_FEE_LAMPORTS = 5_000

# Default compute budget when no simulation is available. Solana's default
# per-instruction budget is 200k CU; a Jupiter-style multi-hop swap commonly
# lands higher, so the DEFAULT is conservative and LABELLED an assumption.
#
# IT IS AN ASSUMPTION AND MUST NOT HARDEN INTO A MEASUREMENT. Every estimate
# built on it carries compute_unit_limit_source=DEFAULT_BUDGET_ASSUMPTION.
# Real measurement needs simulateTransaction unitsConsumed, which needs a
# canonical transaction builder, which does not exist yet.
DEFAULT_SWAP_COMPUTE_UNITS = 400_000
DEFAULT_BUDGET_ASSUMPTION = "DEFAULT_BUDGET_ASSUMPTION"

# ── Priority policies ────────────────────────────────────────────────────
ECONOMY = "ECONOMY"
NORMAL = "NORMAL"
HIGH = "HIGH"
VERY_HIGH = "VERY_HIGH"
MAX_ACCEPTANCE = "MAX_ACCEPTANCE"

POLICIES = (ECONOMY, NORMAL, HIGH, VERY_HIGH, MAX_ACCEPTANCE)

# Helius getPriorityFeeEstimate priority levels per policy. CAPITALISED,
# because the API rejects anything else. Measured against live Helius:
# priorityLevel="high" returns -32602 Invalid params while "High" succeeds,
# so the lowercase spelling silently disabled the primary authority and
# every estimate quietly came from the RPC fallback.
#
# MAX_ACCEPTANCE maps to VeryHigh, NOT UnsafeMax, and unsafeMax is refused
# outright below. Measured on the same live call, unsafeMax quoted
# 160,361,842,105 micro-lamports/CU. On the 400k-CU default budget that is:
#
#     160,361,842,105 x 400,000       = 6.4144736842e16 micro-lamports
#                       / 1e6         = 64,144,736,842 lamports
#                       / 1e9         = 64.144736842 SOL of priority fee
#
# ~64 SOL, NOT ~64,000 SOL. THE PRECISE UNIT FAILURE behind the "~64,000
# SOL" figure that previously stood here: the second division used the
# MICRO-LAMPORTS-PER-LAMPORT divisor (1e6) a second time where the
# LAMPORTS-PER-SOL divisor (1e9) belonged. 6.4144736842e16 / 1e6 / 1e6 =
# 64,144.736842. The ratio of the two divisors is 1e9/1e6 = 1000, which is
# exactly the size of the error.
#
# It is worth being exact about this, because the obvious guess is wrong:
# reading the LAMPORT total as though it were already SOL — dropping the
# 1e9 division entirely — would have been a 1e9x error giving 6.4e10 SOL,
# not 64,000. The mistake was reusing the right-looking constant in the
# wrong step, which is the failure mode that survives a re-read, and the
# reason MICRO_LAMPORTS_PER_LAMPORT and LAMPORTS_PER_SOL are now named for
# the ratio they are rather than for the unit they mention.
#
# The error also ran in the direction that makes the danger look cartoonish
# instead of real: 64 SOL is a plausible-looking transaction that would
# quietly consume a real wallet, which is exactly why the level is
# NON-EXECUTABLE here rather than merely left unselected.
_HELIUS_LEVEL = {
    ECONOMY: "Low",
    NORMAL: "Medium",
    HIGH: "High",
    VERY_HIGH: "VeryHigh",
    MAX_ACCEPTANCE: "VeryHigh",
}

# UnsafeMax is NON-EXECUTABLE. Not "not currently selected" — refused at
# the call site, so a future edit to the table above cannot quietly enable
# it. Compared case-insensitively, because a case bug is precisely what
# made the primary authority fail silently in the first place.
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

# ── Caps come from POLICY, not from this module. ───────────────────────
#
# These used to be a constant table here, which read as though 0.05 SOL
# were a property of Solana. It is not: it is an operator ceiling. A
# MAXIMUM FEE IS A POLICY LIMIT, NOT A GAS PRICE, so the numbers live in
# lib.solana_fee_policy where they are named, per-action, configurable and
# versioned — and this module keeps doing the one thing it is for,
# measuring what the network currently indicates.
#
# Priority policies map to the policy module's ACTION classes: a bid is
# bounded by what the operator authorised for the action being taken.
_POLICY_ACTION_FOR = {
    ECONOMY: "NORMAL_ENTRY",
    NORMAL: "NORMAL_ENTRY",
    HIGH: "NORMAL_EXIT",
    VERY_HIGH: "URGENT_EXIT",
    MAX_ACCEPTANCE: "SEVERE_RISK_EXIT",
}


def priority_cap_lamports(policy: str) -> tuple[int, str]:
    """The configured PRIORITY ceiling for this policy, with provenance."""
    from lib.solana_fee_policy import caps_for
    caps = caps_for(_POLICY_ACTION_FOR.get(policy, "NORMAL_ENTRY"))
    return (int(caps["max_priority_fee_lamports"]),
            f"{caps['policy_version']}/{caps['action']}")


def total_cap_lamports(policy: str) -> tuple[int, str]:
    """The configured TOTAL-NETWORK-FEE ceiling for this policy.

    The operator authors a ceiling on what a transaction may COST, which is
    base + priority. Capping only the priority fee left that authored
    number unenforced — which is how `max_total_fee_lamports` sat in the
    policy table for an entire phase without a single reader.
    """
    from lib.solana_fee_policy import total_cap_lamports as _policy_total
    return _policy_total(_POLICY_ACTION_FOR.get(policy, "NORMAL_ENTRY"))


MAX_ESTIMATE_AGE_MS = 30_000

# Quality vocabulary.
MEASURED_HELIUS = "MEASURED_HELIUS_PRIORITY_ESTIMATE"
MEASURED_RPC_FALLBACK = "MEASURED_RECENT_PRIORITIZATION_FEES"
UNKNOWN = "UNKNOWN"

# ── Provider identity, so fallback health lands on the EXISTING surface ──
# lib.provider_health already answers "is this provider working, and how
# would anyone know?" for every paid API. A fee estimator that silently
# degraded to its fallback is that same question, so it is recorded there
# rather than in a second monitoring architecture of its own.
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

# A request this code built wrongly cannot be retried into working, so it
# is actionable on the FIRST occurrence rather than after a streak.
NEVER_TRANSIENT = frozenset({ERR_MALFORMED_REQUEST, ERR_NOT_CONFIGURED,
                             ERR_AUTH, ERR_PAYMENT_REQUIRED})

# How many consecutive failures turn a transient fault into a standing one.
# One blip must not create noise; a provider that keeps failing must not
# be able to hide behind a working fallback.
PRIMARY_FAILURE_ESCALATION = 3

# JSON-RPC codes that mean "the request was wrong", not "the server broke".
_JSONRPC_MALFORMED = frozenset({-32700, -32600, -32601, -32602})


@dataclass(frozen=True)
class FeeEstimate:
    """One transaction's estimated network cost. Lamports are canonical."""
    ok: bool
    priority_policy: str
    base_fee_lamports: int = PROTOCOL_BASE_FEE_LAMPORTS
    base_fee_source: str = "PROTOCOL_CONSTANT_PER_SIGNATURE"
    priority_fee_lamports: int = 0
    compute_unit_limit: int = DEFAULT_SWAP_COMPUTE_UNITS
    compute_unit_limit_source: str = DEFAULT_BUDGET_ASSUMPTION
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
    def total_network_fee_lamports(self) -> int:
        return int(self.base_fee_lamports) + int(self.priority_fee_lamports)

    @property
    def total_network_fee_sol(self) -> float:
        return lamports_to_sol(self.total_network_fee_lamports)

    # Shorter aliases kept for existing callers. Both names carry their
    # unit, so neither is ambiguous; the longer pair is canonical because
    # it also names WHICH total.
    @property
    def total_lamports(self) -> int:
        return self.total_network_fee_lamports

    @property
    def total_sol(self) -> float:
        return self.total_network_fee_sol

    def total_usd(self, sol_price_usd: float) -> float | None:
        if not sol_price_usd or sol_price_usd <= 0:
            return None
        return self.total_network_fee_sol * float(sol_price_usd)


# ── The conversions. One function each; nothing else may do this math. ──
def executable_compute_unit_price_micro_lamports(raw_estimate: float) -> int:
    """The provider's estimate -> the u64 that can actually be executed.

    THE BOUNDARY THIS EXISTS FOR. Helius returns priorityFeeEstimate as a
    FLOATING-POINT number, but the on-chain instruction it feeds —
    SetComputeUnitPrice(micro_lamports: u64) — takes an integer. Something
    has to quantize, and the version of this module that did not do it here
    did it in the worst possible place: it derived the fee from the raw
    float and then persisted `int(raw)` into
    compute_unit_price_micro_lamports. The stored price was TRUNCATED and
    therefore no longer the price the stored fee was computed from, so
    recomputing the fee from the record gave a different, lower number, and
    reconciliation would have been comparing two different transactions.

    Quantizing FIRST makes the estimate self-consistent: the integer here is
    the value that goes on the wire, the value the fee is derived from, and
    the value that is persisted — one number doing all three jobs.

    CEILING, conservatively, for the same reason the fee conversion rounds
    up: bidding a fraction less than the estimator indicated is the one
    rounding direction that can lose an inclusion, and the cost of rounding
    up is at most one micro-lamport per compute unit.
    """
    if raw_estimate < 0:
        raise ValueError(
            f"negative compute unit price: {raw_estimate!r} micro-lamports/CU")
    return int(Decimal(str(raw_estimate)).to_integral_value(
        rounding=ROUND_CEILING))


def priority_fee_lamports(*, compute_unit_price_micro_lamports: int,
                          compute_unit_limit: int) -> int:
    """micro-lamports/CU x CU -> LAMPORTS. Exact, and rounded UP.

    This is the conversion the whole module exists to get right:

        priority_fee_lamports = ceil(compute_unit_price_micro_lamports
                                     * compute_unit_limit / 1_000_000)

    Decimal rather than float because the products are large enough to lose
    integer precision in float64 (see the module docstring). CEILING rather
    than truncation because a fee rounded down is a fee that was never
    offered to the chain — the sub-lamport difference is economically
    nothing, but "we paid what we said we would" is not nothing.

    THE PRICE MUST ALREADY BE THE EXECUTABLE INTEGER. A fractional price is
    refused rather than quietly quantized here, because quantizing at two
    different places is how the derived fee and the persisted price came to
    disagree. Callers hand raw provider output to
    executable_compute_unit_price_micro_lamports() first, once.
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_fetch(method: str, params):
    """Live RPC through the EXISTING Helius client. Injectable for tests."""
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


# Error class -> the vocabulary lib.provider_health already speaks.
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


def _helius_priority(policy: str, *, account_keys, transaction_b64, fetch):
    """Helius getPriorityFeeEstimate — the per-account fee market."""
    level = assert_executable_level(_HELIUS_LEVEL[policy])
    options = {"priorityLevel": level}
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
    """getRecentPrioritizationFees percentile — standard Solana RPC.

    `prioritizationFee` is documented in the SAME unit as the Helius
    estimate: micro-lamports per compute unit. The two authorities are
    interchangeable at this boundary precisely because their units match.
    """
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
                         fetch=None,
                         record_health: bool = True) -> FeeEstimate:
    """Estimate THIS transaction's network fee under a priority policy.

    Tries Helius first (per-account fee market), the standard RPC second,
    and refuses to invent a number third. A dead estimator returns
    quality=UNKNOWN — what that means for the trade is authorize_fee's
    decision, because an entry and an emergency exit answer it differently.

    EVERY PRIMARY ATTEMPT IS RECORDED, win or lose. The fallback worked so
    well that a malformed primary request survived undetected: the numbers
    looked reasonable because they WERE reasonable — they were simply
    coming from the second authority every single time. Falling back is
    correct; falling back silently and permanently is not.
    """
    if policy not in POLICIES:
        return FeeEstimate(ok=False, priority_policy=str(policy),
                           reason=f"unknown priority policy {policy!r}")
    fetch = fetch or _default_fetch
    cu_limit = int(compute_unit_limit or DEFAULT_SWAP_COMPUTE_UNITS)
    cu_source = (compute_units_source
                 or ("CALLER_SIMULATION" if compute_unit_limit
                     else DEFAULT_BUDGET_ASSUMPTION))
    started = time.perf_counter()

    def _elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    micro_per_cu = None
    source = UNKNOWN
    quality = UNKNOWN
    detail = None
    primary_error_class = None
    try:
        micro_per_cu = _helius_priority(policy, account_keys=account_keys,
                                        transaction_b64=transaction_b64,
                                        fetch=fetch)
        source = "helius.getPriorityFeeEstimate"
        quality = MEASURED_HELIUS
        if record_health:
            _record_health(PRIMARY_PROVIDER, PRIMARY_CAPABILITY,
                           status="HEALTHY", latency_ms=_elapsed_ms(), rows=1,
                           detail=f"priorityLevel={_HELIUS_LEVEL[policy]}")
    except Exception as first:                               # noqa: BLE001
        primary_error_class = classify_primary_error(first)
        if record_health:
            _record_health(
                PRIMARY_PROVIDER, PRIMARY_CAPABILITY,
                status=_HEALTH_STATUS_FOR.get(primary_error_class, "DEGRADED"),
                latency_ms=_elapsed_ms(), error=str(first),
                detail=f"{primary_error_class} "
                       f"priorityLevel={_HELIUS_LEVEL[policy]}")
        try:
            micro_per_cu = _rpc_fallback_priority(policy, fetch=fetch)
            source = "rpc.getRecentPrioritizationFees"
            quality = MEASURED_RPC_FALLBACK
            detail = f"helius estimate unavailable ({first}); used fallback"
            if record_health:
                _record_health(FALLBACK_PROVIDER, FALLBACK_CAPABILITY,
                               status="HEALTHY", latency_ms=_elapsed_ms(),
                               rows=1,
                               detail=f"fallback activated after "
                                      f"{primary_error_class} on "
                                      f"{PRIMARY_PROVIDER}")
        except Exception as second:                          # noqa: BLE001
            if record_health:
                _record_health(FALLBACK_PROVIDER, FALLBACK_CAPABILITY,
                               status="UNAVAILABLE", latency_ms=_elapsed_ms(),
                               error=str(second))
            return FeeEstimate(
                ok=False, priority_policy=policy,
                compute_unit_limit=cu_limit,
                compute_unit_limit_source=cu_source,
                quality=UNKNOWN, observed_at=_now_iso(),
                estimate_age_ms=_elapsed_ms(),
                reason=(f"no trustworthy priority estimate: helius=({first}) "
                        f"fallback=({second})"),
                provenance={"helius_error": str(first)[:200],
                            "helius_error_class": primary_error_class,
                            "fallback_error": str(second)[:200]})

    # QUANTIZE ONCE, THEN DERIVE. `micro_per_cu` is whatever the provider
    # said — Helius returns a float. Everything downstream uses the
    # executable u64, so the price that is persisted is the price the fee
    # was computed from and the price that would go into
    # SetComputeUnitPrice. The provider's exact value survives in
    # provenance; it is evidence, not an input to arithmetic.
    executable_price = executable_compute_unit_price_micro_lamports(
        micro_per_cu)
    measured_priority_lamports = priority_fee_lamports(
        compute_unit_price_micro_lamports=executable_price,
        compute_unit_limit=cu_limit)

    # HARD CAPS — every policy, including MAX_ACCEPTANCE. The ceilings are
    # operator policy; the estimate above is network measurement. Capping
    # does not change what the network said, only what we will pay.
    #
    # TWO bounds, and the tighter one wins. The operator authors a ceiling
    # on what a transaction may COST (base + priority); enforcing only the
    # priority bound left that authored number unenforced.
    priority_cap, priority_cap_source = priority_cap_lamports(policy)
    total_cap, total_cap_source = total_cap_lamports(policy)
    priority_allowed_by_total = max(0, total_cap - PROTOCOL_BASE_FEE_LAMPORTS)
    effective_cap = min(priority_cap, priority_allowed_by_total)
    binding = ("TOTAL_NETWORK_FEE_CEILING"
               if priority_allowed_by_total < priority_cap
               else "PRIORITY_FEE_CEILING")

    capped = measured_priority_lamports > effective_cap
    final_priority_lamports = (effective_cap if capped
                               else measured_priority_lamports)

    return FeeEstimate(
        ok=True, priority_policy=policy,
        priority_fee_lamports=final_priority_lamports,
        compute_unit_limit=cu_limit,
        compute_unit_limit_source=cu_source,
        compute_unit_price_micro_lamports=executable_price,
        priority_estimate_source=source, quality=quality,
        capped=capped, cap_applied_lamports=effective_cap if capped else None,
        observed_at=_now_iso(),
        estimate_age_ms=_elapsed_ms(),
        reason=detail,
        provenance={"micro_lamports_per_cu_raw": micro_per_cu,
                    "executable_compute_unit_price_micro_lamports":
                        executable_price,
                    "price_quantization": "CEILING_TO_EXECUTABLE_U64",
                    "measured_priority_fee_lamports":
                        measured_priority_lamports,
                    "policy_priority_cap_lamports": priority_cap,
                    "policy_total_network_fee_cap_lamports": total_cap,
                    "effective_priority_cap_lamports": effective_cap,
                    "binding_cap": binding,
                    "policy_cap_source": priority_cap_source,
                    "policy_total_cap_source": total_cap_source,
                    "primary_error_class": primary_error_class,
                    "cap_is_policy_not_network_truth": True})


# ── Is the PRIMARY authority actually working? ─────────────────────────
def fee_estimator_health() -> dict:
    """Whether the fallback is covering for a primary that never works.

    THE FAILURE THIS EXISTS FOR. Phase 6 found `priorityLevel="high"`
    returning -32602 Invalid params while "High" succeeded. Nothing broke:
    getRecentPrioritizationFees answered every time, the estimates were
    sane, and the primary authority had — as far as anyone could tell —
    never once returned a number. A fallback that good is indistinguishable
    from a working system until somebody goes looking.

    Reads the EXISTING provider-health table rather than keeping counters
    of its own, so this is the same answer the Ops panel shows.
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

    # The error class is the first token of the detail this module writes.
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
    # fact worth recording and nothing worth waking anyone for, so
    # "never succeeded" only escalates once there have been enough attempts
    # for the silence to mean something. A never-transient class (a
    # malformed request, a missing key) escalates immediately instead,
    # because no number of retries will change it.
    standing = never_succeeded and primary_attempts >= PRIMARY_FAILURE_ESCALATION

    # The Phase 6 shape exactly: the primary has never once worked, and
    # nobody noticed because the fallback is serving every single call.
    masked = standing and fallback_activations > 0

    actionable = bool(standing or persistently_failing or non_transient)

    stamps = []
    if primary and primary.get("last_success_at"):
        stamps.append((str(primary["last_success_at"]),
                       "helius.getPriorityFeeEstimate"))
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

    A LIVE ESTIMATE ABOVE THE AUTHORISED POLICY IS A REFUSAL, NOT A REASON
    TO RAISE THE POLICY. The ceiling is never widened here to accommodate
    whatever the network happens to be charging; an autonomous entry that
    cannot be included inside the authorised budget simply does not happen.
    """
    if action not in ALLOWED_POLICIES:
        return {"ok": False, "reason": f"unknown action {action!r}"}

    if not estimate.ok or estimate.quality == UNKNOWN:
        if action == URGENT_RISK_REDUCTION:
            # Bounded, named fallback — never "whatever it takes".
            from lib.solana_fee_policy import emergency_fallback_lamports
            fallback, fallback_source = emergency_fallback_lamports()
            return {"ok": True,
                    "fee_lamports": fallback,
                    "fee_sol": lamports_to_sol(fallback),
                    "fallback_source": fallback_source,
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

    # THE ABSOLUTE CEILING, checked against what the network actually
    # indicated rather than against the already-capped number. A capped
    # estimate means the live price EXCEEDED what the operator authorised,
    # and for an autonomous ENTRY that is a refusal: paying the ceiling
    # instead of the market price does not buy the inclusion the estimate
    # said was needed, it merely spends the ceiling.
    total_cap, total_cap_source = total_cap_lamports(estimate.priority_policy)
    measured_priority = int(estimate.provenance.get(
        "measured_priority_fee_lamports", estimate.priority_fee_lamports))
    measured_total = int(estimate.base_fee_lamports) + measured_priority
    if action == ENTRY and measured_total > total_cap:
        return {"ok": False, "reason": "FEE_EXCEEDS_AUTHORISED_POLICY",
                "detail": (f"live estimate {measured_total} lamports "
                           f"({lamports_to_sol(measured_total):.9f} SOL) "
                           f"exceeds the authorised {total_cap} lamports "
                           f"({lamports_to_sol(total_cap):.9f} SOL) for "
                           f"{estimate.priority_policy}. The policy ceiling "
                           f"is not raised to meet the market."),
                "measured_total_network_fee_lamports": measured_total,
                "policy_total_cap_lamports": total_cap,
                "policy_total_cap_source": total_cap_source}

    fee_usd = estimate.total_usd(sol_price_usd)

    if action == ENTRY:
        from lib.solana_fee_policy import caps_for
        entry_caps = caps_for("NORMAL_ENTRY")
        edge_cap = entry_caps["max_fee_pct_expected_edge"]
        notional_cap = entry_caps["max_fee_pct_notional"]
        # A new position must not spend its edge on inclusion.
        if fee_usd is None:
            return {"ok": False, "reason": "FEE_USD_UNPRICEABLE",
                    "detail": "no SOL price to value the fee against edge"}
        if expected_edge_usd is not None and expected_edge_usd > 0:
            pct_of_edge = 100.0 * fee_usd / expected_edge_usd
            if edge_cap is not None and pct_of_edge > edge_cap:
                return {"ok": False, "reason": "FEE_DESTROYS_EDGE",
                        "detail": (f"network fee ${fee_usd:.4f} is "
                                   f"{pct_of_edge:.1f}% of the "
                                   f"${expected_edge_usd:.2f} expected edge "
                                   f"(policy cap {edge_cap}%)"),
                        "fee_usd": fee_usd}
        if notional_usd and notional_usd > 0:
            pct_of_notional = 100.0 * fee_usd / float(notional_usd)
            if notional_cap is not None and pct_of_notional > notional_cap:
                return {"ok": False, "reason": "FEE_EXCEEDS_NOTIONAL_CAP",
                        "detail": (f"network fee ${fee_usd:.4f} is "
                                   f"{pct_of_notional:.2f}% of the "
                                   f"${notional_usd:.2f} trade "
                                   f"(policy cap {notional_cap}%)"),
                        "fee_usd": fee_usd}

    return {"ok": True, "fee_lamports": estimate.total_network_fee_lamports,
            "fee_sol": estimate.total_network_fee_sol,
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
    est = int(estimate.total_network_fee_lamports)
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
