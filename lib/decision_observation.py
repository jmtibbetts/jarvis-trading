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


def observation_id_for(*, signal_id=None, symbol=None, decision_at=None,
                       thesis_id=None) -> str:
    """A deterministic identity for ONE market event.

    Deterministic rather than random so a retried cycle resolves to the same
    row. The thesis and the decision instant are what make the event unique;
    the ARM that observed it (traded, refused, shadow) deliberately is not,
    because arms of one event must share an identity or they will each look
    like an independent market sample to the learner.
    """
    parts = [str(thesis_id or ""), str(signal_id or ""), str(symbol or ""),
             str(decision_at or "")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


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
          execution=None, position_id=None, gates=None,
          edge_threshold_r=None, gross_expected_r=None,
          estimated_cost_r=None, venue_data_failure=False,
          decision_at=None) -> dict:
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

    row = {
        "observation_id": observation_id_for(
            signal_id=signal.get("id") or signal.get("signal_id"),
            symbol=sym, decision_at=at, thesis_id=signal.get("thesis_id")),
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
            "risk_budget_usd": _f(authorization.sizing.get("margin")),
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

    if execution is not None:
        row["execution_id"] = getattr(execution, "execution_id", None)

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
                # SAME EVENT, SEEN AGAIN. Late-arriving linkage is filled in;
                # the judgment itself is never rewritten.
                for late in ("execution_id", "position_id"):
                    if row.get(late) and not getattr(existing, late, None):
                        setattr(existing, late, row[late])
                return existing.observation_id
            db.add(DecisionObservation(**row))
            return row["observation_id"]
    except Exception as e:
        logger.error("[DecisionObservation] FAILED to persist %s for %s: %s",
                     row.get("final_decision"), row.get("symbol"), e,
                     exc_info=True)
        return None
