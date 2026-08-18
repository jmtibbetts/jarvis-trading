"""Write down what the decision actually saw — traded or refused.

THE PROBLEM THIS SOLVES. A rejected candidate used to leave nothing behind:

    candidate -> NO_TRADE -> forgotten

Measured cost of that: 11,952 historical cost-gate rejections, 11,775 of
them with no usable forward evidence. The honest answer to "how much
opportunity did the broken cost model suppress?" turned out to be UNKNOWN,
and it was unknown for a reason that had nothing to do with the market —
the evidence was discarded at the moment of refusal.

THE RECORD IS AN AUDIT TRAIL, NOT A SECOND OPINION. Everything here is
lifted from the artifacts the decision ACTUALLY used — the
`ExecutionReadiness`, the `ExecutionMarketSnapshot`, the
`EntryAuthorization`, the `FeeQuote`. Nothing is recomputed, because a
recomputed record would diverge from the decision the moment either side
changed and would be worse than no record at all: it would look like
evidence.

ONE MARKET EVENT, ONE OBSERVATION. `observation_id` is derived
deterministically from the thesis and the decision instant, so a retried
scheduler cycle updates the same row rather than creating a second sample.
One event must never vote twice in learning.

A REFUSAL IS NOT A LOSS. `venue_data_failure` and `binding_constraint`
carry that distinction into the record: a stale quote is a DATA constraint,
and it must never be read later as evidence that the thesis was wrong.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

OBSERVATION_MODEL_VERSION = "decision_observation_v1"

# ── Evidence sources. Provenance is explicit from the start, so nothing has
# to be guessed retrospectively later. ───────────────────────────────────
FORWARD_CANONICAL = "FORWARD_CANONICAL"
FORWARD_REJECTED_OBSERVATION = "FORWARD_REJECTED_OBSERVATION"
LEGACY_FORWARD_VIRTUAL = "LEGACY_FORWARD_VIRTUAL"
HISTORICAL_BACKTEST = "HISTORICAL_BACKTEST"
# Reserved namespace. The full replay engine is deliberately NOT built: the
# historical sample's honest answer is UNKNOWN, and infrastructure for an
# unanswerable question is just infrastructure.
COUNTERFACTUAL_REPLAY = "COUNTERFACTUAL_REPLAY"

ALL_SOURCES = frozenset({FORWARD_CANONICAL, FORWARD_REJECTED_OBSERVATION,
                         LEGACY_FORWARD_VIRTUAL, HISTORICAL_BACKTEST,
                         COUNTERFACTUAL_REPLAY})

# Sources that describe trades this system actually took forward. Only
# these may inform execution calibration or portfolio performance.
FORWARD_EXECUTED_SOURCES = frozenset({FORWARD_CANONICAL, LEGACY_FORWARD_VIRTUAL})

TRADE, NO_TRADE, ABSTAIN = "TRADE", "NO_TRADE", "ABSTAIN"

# APPEND-ONLY LIFECYCLE. These are facts that cannot be known at T0 —
# what the decision went on to produce. Completing them is causal linkage,
# not hindsight; everything NOT on this list is frozen at the decision.
_LATE_LIFECYCLE_FIELDS = ("execution_id", "position_id", "settlement_at",
                          "settlement_failure_reason")

# ── Which KIND of thing stopped the trade. ───────────────────────────────
EDGE = "EDGE"                 # the setup did not promise enough
COST = "COST"                 # edge cleared, costs ate it
RISK = "RISK"                 # the account could not carry it
CAPABILITY = "CAPABILITY"     # the product/venue cannot execute it at all
DATA = "DATA"                 # we could not see the market well enough
ACCOUNT_STATE = "ACCOUNT_STATE"   # book full, no cash, duplicate open
NONE_BINDING = "NONE"         # it traded

# Named refusals mapped to the constraint they represent. Anything unmapped
# is recorded as-is rather than forced into a bucket — an unclassified
# reason is a prompt to classify it, not licence to guess.
_CONSTRAINT_BY_REASON = {
    "UNSUPPORTED_VIRTUAL_VENUE": CAPABILITY,
    "NO_EXECUTABLE_QUOTE": CAPABILITY,
    "NO_EXECUTABLE_PERP_QUOTE": CAPABILITY,
    "NO_EXECUTABLE_PRODUCT_QUOTE": CAPABILITY,
    "UNKNOWN_PRODUCT": CAPABILITY,
    "NO_BITNOMIAL_PRODUCT": CAPABILITY,
    "MISSING_CONTRACT_SPEC": CAPABILITY,
    "UNVERIFIED_PRICE_SCALE": DATA,
    "EXECUTION_DATA_UNAVAILABLE": DATA,
    "STALE_EXECUTION_DATA": DATA,
    "CROSSED_BOOK": DATA,
    "ONE_SIDED_BOOK": DATA,
    "BOOK_DESYNCED": DATA,
    "MARKET_NOT_OPEN": DATA,
    "MARKET_HALTED": DATA,
    "FEE_AUTHORITY_UNAVAILABLE": COST,
    "FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL": COST,
    "REFUSED_EXCEEDS_RISK": RISK,
    "REJECTED_BY_EXECUTION": RISK,
    "UNFILLED": DATA,
}


def constraint_for(reason: str | None) -> str:
    """The KIND of constraint a named refusal represents.

    Kept as a mapping rather than a heuristic on the string, because
    "which sort of problem was this?" is exactly the question the historical
    data could not answer, and a regex over prose would reproduce that.
    """
    if not reason:
        return NONE_BINDING
    # UNCLASSIFIED is deliberate: an unmapped reason is a prompt to classify
    # it, not licence to file it under whichever bucket looks closest.
    return _CONSTRAINT_BY_REASON.get(str(reason), "UNCLASSIFIED")


# How trustworthy the event identity is. A retry-stable identity is the
# difference between "one event, observed twice" and "two events".
IDENTITY_EXPLICIT = "EXPLICIT_EVENT_ID"
IDENTITY_CANDIDATE = "CANDIDATE_ID"
IDENTITY_SIGNAL_EVENT_TIME = "SIGNAL_EVENT_TIME"
IDENTITY_VENUE_EVENT_TIME = "VENUE_EVENT_TIME"
IDENTITY_UNSTABLE = "UNSTABLE_WALL_CLOCK"

_SIGNAL_TIME_KEYS = ("event_at", "created_at", "generated_at", "signal_at",
                     "timestamp")


def event_identity(signal: dict, snap=None) -> tuple[str, str]:
    """(the stable anchor for this market event, how it was established).

    THE DEFECT THIS FIXES. The anchor used to be `_now()`, taken at the
    moment the Python call happened to run. A scheduler cycle at 12:00:00
    and its retry at 12:00:01 then hashed differently, so the retry wrote a
    SECOND observation of the same market event — exactly the duplicate the
    unique index was supposed to prevent, and exactly the way one event
    comes to vote twice in learning.

    The anchor must be a fact about the EVENT, never about the invocation.
    In descending order of strength:

        1. an explicit market/event id, when something upstream minted one
        2. the candidate id — one scored setup is one event
        3. the signal id plus the signal's OWN timestamp
        4. the signal id plus the VENUE's event time from the book snapshot,
           which is a real market instant rather than our clock

    Only if none of those exists does this fall back to wall-clock, and it
    says so: an UNSTABLE identity is recorded as such rather than quietly
    pretending to be retry-safe.

    A genuinely NEW evaluation of the same signal against a new book gets a
    new venue event time, and therefore a new identity — which is correct.
    Re-evaluation is a new observation; a retry is not.
    """
    explicit = signal.get("market_event_id") or signal.get("event_id")
    if explicit:
        return str(explicit), IDENTITY_EXPLICIT
    if signal.get("candidate_id"):
        return str(signal["candidate_id"]), IDENTITY_CANDIDATE
    for key in _SIGNAL_TIME_KEYS:
        if signal.get(key):
            return str(signal[key]), IDENTITY_SIGNAL_EVENT_TIME
    venue_at = getattr(snap, "venue_event_at", None) if snap is not None else None
    if venue_at:
        return str(venue_at), IDENTITY_VENUE_EVENT_TIME
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(), IDENTITY_UNSTABLE


def observation_id_for(*, signal_id=None, symbol=None, event_at=None,
                       thesis_id=None) -> str:
    """A deterministic identity for ONE market event.

    `event_at` is the STABLE anchor from `event_identity` — a fact about the
    event, not the wall-clock of this invocation. The ARM that observed the
    event (traded, refused, shadow, control) is deliberately absent: arms of
    one event must share an identity, or each becomes an independent market
    sample to the learner.
    """
    parts = [str(thesis_id or ""), str(signal_id or ""), str(symbol or ""),
             str(event_at or "")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# ── Execution lifecycle. The DECISION and what became of it are different
# facts, and conflating them is how a failed settlement could have counted
# as an executed trade. ──────────────────────────────────────────────────
EXEC_NOT_APPLICABLE = "NOT_APPLICABLE"      # nothing was ever sent
EXEC_SIMULATED_FILLED = "SIMULATED_FILLED"  # the venue produced a fill
EXEC_SETTLED = "SETTLED"                    # and the account recorded it
EXEC_SETTLEMENT_FAILED = "SETTLEMENT_FAILED"


def is_execution_calibration_eligible(obs) -> bool:
    """May this row inform fill/slippage calibration or portfolio results?

    A PREDICATE, NOT A SET MEMBERSHIP TEST. `source == FORWARD_CANONICAL`
    was never sufficient: the source is written at T0, before settlement is
    known, so a trade JARVIS decided to take and then failed to settle would
    have carried an executed-evidence label while no position existed.

    Every one of these must hold — the decision was to trade, from a forward
    executed source, an execution really happened, settlement really
    succeeded, and a position exists to point at:
    """
    def _g(name):
        return obs.get(name) if isinstance(obs, dict) else getattr(obs, name, None)

    return bool(
        _g("source") in FORWARD_EXECUTED_SOURCES
        and _g("final_decision") == TRADE
        and _g("execution_id")
        and _g("execution_state") == EXEC_SETTLED
        and _g("position_id"))


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _depth_summary(snap) -> dict | None:
    """A COMPACT read of the book, never the book itself.

    Full depth on every decision would be the largest table in the system
    within a month, for data almost none of which is ever read. Best levels,
    sizes and level counts reconstruct the spread and top-of-book liquidity,
    which is what the analysis actually asks for.
    """
    prov = getattr(snap, "provenance", None) or {}
    if not any(k in prov for k in ("depth_bids", "depth_asks")):
        return None
    bids = prov.get("depth_bids") or []
    asks = prov.get("depth_asks") or []
    return {
        "bid_levels": prov.get("bid_levels"),
        "ask_levels": prov.get("ask_levels"),
        "top_bids": bids[:3],
        "top_asks": asks[:3],
        "note": "compact summary; the full book is not persisted per decision",
    }


def build(*, signal, ready=None, authorization=None, fee_quote=None,
          decision, binding_reason=None, source=None, decision_price=None,
          execution_id=None, position_id=None, gates=None,
          edge_threshold_r=None, gross_expected_r=None,
          estimated_cost_r=None, venue_data_failure=False,
          decision_at=None, execution_state=None) -> dict:
    """Assemble the row from the artifacts the decision used. No re-deriving.

    Every argument is an object the decision already produced. Where one is
    absent — because the decision stopped before reaching it — the fields it
    would have filled stay NULL. A refusal at the readiness gate genuinely
    has no authorized quantity, and writing a plausible one would invent
    evidence.
    """
    from lib.trade_side import SHORT, parse_side_strict

    snap = getattr(ready, "snapshot", None)
    prov = (getattr(snap, "provenance", None) or {}) if snap is not None else {}
    sym = (signal.get("asset_symbol") or signal.get("symbol") or "").upper().strip()
    raw_dir = signal.get("paper_direction") or signal.get("direction")
    side = parse_side_strict(raw_dir)
    at = decision_at or _now()
    # THE EVENT ANCHOR IS NOT `at`. `decision_at` records WHEN JARVIS
    # decided and is persisted as such; the identity comes from a fact about
    # the event, so a retry resolves to the same row.
    anchor, identity_quality = event_identity(signal, snap)

    row = {
        "observation_id": observation_id_for(
            signal_id=signal.get("id") or signal.get("signal_id"),
            symbol=sym, event_at=anchor, thesis_id=signal.get("thesis_id")),
        "identity_quality": identity_quality,
        "execution_state": execution_state or (
            EXEC_NOT_APPLICABLE if decision != TRADE else None),
        "execution_id": execution_id,
        "thesis_id": signal.get("thesis_id"),
        "signal_id": signal.get("id") or signal.get("signal_id"),
        "candidate_id": signal.get("candidate_id"),
        "decision_at": at,
        "source": source or (FORWARD_CANONICAL if decision == TRADE
                             else FORWARD_REJECTED_OBSERVATION),
        "strategy": signal.get("strategy"),
        "timeframe": signal.get("timeframe"),

        "symbol": sym,
        "asset_class": getattr(ready, "asset_class", None),
        "product": getattr(ready, "product", None),
        "venue": getattr(ready, "venue", None),
        "market_data_source": (getattr(snap, "source", None) if snap is not None
                               else None),
        "instrument_id": getattr(ready, "instrument", None),
        "provider_product_code": prov.get("product_code"),
        "contract_size": _f(prov.get("contract_size")),

        "side": ("short" if side == SHORT else "long") if side else None,
        "order_type": "market",
        "decision_price": _f(decision_price),
        "intended_stop": _f(signal.get("stop_loss")),
        "intended_target": _f(signal.get("target_price")),

        "bid": _f(getattr(snap, "bid", None)),
        "ask": _f(getattr(snap, "ask", None)),
        "bid_size": _f(getattr(snap, "bid_size", None)),
        "ask_size": _f(getattr(snap, "ask_size", None)),
        "quote_age_ms": _f(getattr(snap, "age_ms", None)),
        "quote_status": getattr(snap, "status", None),
        "book_ack_id": (str(prov["ack_id"]) if prov.get("ack_id") is not None
                        else None),
        "market_state": prov.get("market_state"),
        "gross_expected_r": _f(gross_expected_r),
        "estimated_cost_r": _f(estimated_cost_r),
        "edge_threshold_r": _f(edge_threshold_r),
        "final_decision": decision,
        "binding_reason": binding_reason,
        "binding_constraint": (NONE_BINDING if decision == TRADE
                               else constraint_for(binding_reason)),
        "venue_data_failure": bool(venue_data_failure),
        "position_id": position_id,
        "engine_epoch": _epoch(),
    }

    if gross_expected_r is not None and estimated_cost_r is not None:
        row["expected_net_r"] = float(gross_expected_r) - float(estimated_cost_r)
        if edge_threshold_r is not None:
            row["distance_to_threshold_r"] = (row["expected_net_r"]
                                              - float(edge_threshold_r))

    # PRICE SCALE QUALITY travels with the row, because a perpetual whose
    # scale is unverified must never be read later as an exact economic
    # observation. SHIB is the live example.
    if getattr(ready, "product", None) == "CRYPTO_PERP":
        row["price_scale_quality"] = ("VERIFIED" if prov.get("price_increment")
                                      else "UNVERIFIED_PRICE_SCALE")

    depth = _depth_summary(snap) if snap is not None else None
    if depth:
        row["depth_summary"] = json.dumps(depth)

    if authorization is not None:
        row.update({
            "authorized_qty": _f(authorization.qty),
            "authorized_notional": _f(authorization.notional),
            "committed_margin_usd": _f(authorization.margin),
            "leverage": _f(authorization.leverage),
            "intended_leverage": _f(authorization.leverage),
            "authorized_risk_usd": _f(authorization.loss_at_stop),
            "loss_at_stop_usd": _f(authorization.loss_at_stop),
            # risk_budget_usd IS DELIBERATELY NULL. `sizing["margin"]` looks
            # like a budget and is not: `size_position` seeds it with the
            # equity slice, hands it to `solve_position` as risk_budget_usd,
            # then REASSIGNS it to `decision.margin` — the committed margin.
            # Storing that under "risk budget" would put the same number in
            # two columns with different meanings, which is the field-name
            # ambiguity this whole table exists to remove. The pre-solve
            # budget is not returned by any authority, so the honest value
            # is absent. `authorized_risk_usd` carries the real approved
            # money-at-stop.
            "quantity_unit": authorization.sizing.get("quantity_unit"),
            "multiplier": _f(authorization.sizing.get("multiplier")),
        })

    if fee_quote is not None and getattr(fee_quote, "ok", False):
        row.update({
            "entry_fee_usd": _f(fee_quote.fee_usd),
            "fee_basis": fee_quote.fee_basis,
            "fee_contract_count": _f(fee_quote.contract_count),
            "cost_provenance": json.dumps({
                "fee_quality": fee_quote.quality,
                "fee_source": fee_quote.source,
                "fee_is_measured": fee_quote.is_measured,
                "fee_authority_version": fee_quote.version,
            }),
        })

    if gates:
        row["gate_results"] = json.dumps(gates)

    row["provenance"] = json.dumps({
        "observation_model": OBSERVATION_MODEL_VERSION,
        "quote_provenance": {k: prov.get(k) for k in
                             ("market_data_source", "execution_venue",
                              "bitnomial_symbol", "price_increment",
                              "snapshot_count", "feed_products")
                             if prov.get(k) is not None},
    })
    return row


def _epoch() -> str:
    try:
        from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
        return CANONICAL_ENGINE_EPOCH
    except Exception:
        return "unknown"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def record(row: dict) -> str | None:
    """Persist ONE observation, idempotently, in a short transaction.

    The write is deliberately tiny and holds no provider call: market data
    is gathered before this is entered, so a slow venue can never widen a
    database transaction.

    A failure to persist is LOUD. Losing the audit trail silently is the
    exact failure this module exists to prevent — but it must not take a
    trade down with it either, so the caller decides. For an accepted trade
    the caller writes the observation BEFORE settling, so a canonical
    position cannot exist without its decision record.
    """
    from app.database import DecisionObservation, get_db

    try:
        with get_db() as db:
            existing = db.query(DecisionObservation).filter(
                DecisionObservation.observation_id == row["observation_id"]
            ).first()
            if existing is not None:
                # SAME EVENT, SEEN AGAIN. Only APPEND-ONLY LIFECYCLE facts
                # may complete — what became of the decision, which is not
                # known at T0 and is not hindsight. The judgment itself
                # (decision, binding reason, book, economics, threshold) is
                # never rewritten: a paper trail its own subject can edit is
                # not a paper trail.
                for late in _LATE_LIFECYCLE_FIELDS:
                    if row.get(late) and not getattr(existing, late, None):
                        setattr(existing, late, row[late])
                # execution_state is the one lifecycle field that legitimately
                # ADVANCES rather than being filled in once.
                new_state = row.get("execution_state")
                if new_state and new_state != getattr(existing, "execution_state", None):
                    existing.execution_state = new_state
                return existing.observation_id
            db.add(DecisionObservation(**row))
            return row["observation_id"]
    except Exception as e:
        logger.error("[DecisionObservation] FAILED to persist %s for %s: %s",
                     row.get("final_decision"), row.get("symbol"), e,
                     exc_info=True)
        return None
