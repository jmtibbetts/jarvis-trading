"""ONE door for every paper-book exit. Pass B's last joint.

Ten production callers wanted to close positions, and all ten reached the
legacy leaves — `close_paper_position` / `partial_close_paper_position` —
which settle at whatever price the caller handed them. The permanent guard
kept canonical fills out of those leaves, which is why canonical positions
had no exit at all. This module is the door above them.

WHAT A CALLER MAY DECIDE. That a position should be reduced; a mark it
observed; a threshold it believes fired; why it is asking. That is all.

WHAT A CALLER MAY NEVER DECIDE. The product, the venue, the contract, the
executable side, the fill, the fee, the carry, or the settlement
arithmetic. A canonical position already owns every one of those, frozen at
entry, and `canonical_exit` reads them from the ledger rather than from the
caller.

THREE ROUTES, CLASSIFIED ONCE:

    LEGACY      no canonical fill and no settlement ledger. Goes to the
                legacy leaf, mark-as-fill and all — those 667 operator rows
                belong to the old economy and are not half-migrated.
    CANONICAL   canonical fill AND is_canonical AND a valid B1 header.
                Goes to canonical_exit -> B2A -> B2C.
    HYBRID      any mixed state. REFUSES.

THERE IS NO FALLBACK. Not on exception, not on refusal, not on venue
outage. A canonical exit that cannot execute leaves the position open; it
never drops through to the old economy, because "canonical failed, settle
at the mark" would reintroduce exactly the defect this whole pass removed.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from lib.realized_outcome import (ADMINISTRATIVE_RESET,  # noqa: E402
                                  FORCED_LIQUIDATION, MARGIN_CALL,
                                  STOP_EXIT, TARGET_EXIT, VOLUNTARY_EXIT)

# ── Routes ───────────────────────────────────────────────────────────────
LEGACY = "LEGACY"
CANONICAL = "CANONICAL"
HYBRID = "HYBRID"

HYBRID_POSITION_EXIT_REFUSED = "HYBRID_POSITION_EXIT_REFUSED"
UNKNOWN_EXIT_REASON = "UNKNOWN_EXIT_REASON"
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
POSITION_NOT_OPEN = "POSITION_NOT_OPEN"

# ── Canonical semantics: the SOURCE decides, the reason is provenance ────
#
# Real callers do not pass tokens. The tier caller passes its own label —
# ">=10% — take profit", "<=-4% — cut loss" — and the risk guard passes a
# whole sentence: "catastrophic backstop — lost $350.00 of $1,000.00 margin
# (35% cap)". Teaching a table to parse human prose would be a losing race
# with every future edit to a log message.
#
# So the STRUCTURED caller_source carries the canonical meaning, and the
# reason text is kept verbatim as provenance. That preserves
# VOLUNTARY_EXIT/">=10% — take profit" as a distinct record from
# VOLUNTARY_EXIT/"AI EXIT: ..." without pretending either sentence is
# settlement vocabulary.

# Sources whose semantics are fixed regardless of what they say.
_SOURCE_REASON = {
    "SOFT_RESET": ADMINISTRATIVE_RESET,
    "PAPER_TP1": "SCALE_OUT",
    "RISK_GUARD": VOLUNTARY_EXIT,
    "TIER_EXIT": VOLUNTARY_EXIT,
    "AI_EXIT": VOLUNTARY_EXIT,
    "TELEGRAM_MANUAL": VOLUNTARY_EXIT,
    "TELEGRAM_TAKE_PROFIT": VOLUNTARY_EXIT,
    "API_MANUAL": VOLUNTARY_EXIT,
    "API_FLATTEN": VOLUNTARY_EXIT,
}

# The one source whose reason MUST be read: it names WHICH price threshold
# fired, and that choice drives the exact-book confirmation downstream. It
# does not get a blanket VOLUNTARY_EXIT.
_TRIGGER_REASONS = {
    "stop_loss": STOP_EXIT,
    "stop": STOP_EXIT,
    "hard_stop": STOP_EXIT,
    "take_profit": TARGET_EXIT,
    "target": TARGET_EXIT,
    "margin_call": MARGIN_CALL,
    "liquidation": FORCED_LIQUIDATION,
    "forced_liquidation": FORCED_LIQUIDATION,
}
_REASON_SOURCES = {"MARK_TO_MARKET"}

KNOWN_CALLER_SOURCES = frozenset(_SOURCE_REASON) | _REASON_SOURCES

# Bare reason tokens, for direct/test callers that state no source. Kept
# deliberately small: production goes through the source table above.
_REASON_MAP = {
    **_TRIGGER_REASONS,
    "scale_out": "SCALE_OUT",
    "scale_out_tp1": "SCALE_OUT",
    "tp1": "SCALE_OUT",
    "reset": ADMINISTRATIVE_RESET,
    "manual": VOLUNTARY_EXIT,
    "manual flatten": VOLUNTARY_EXIT,
    "flatten": VOLUNTARY_EXIT,
    "api_manual": VOLUNTARY_EXIT,
    "telegram_manual": VOLUNTARY_EXIT,
    "ai_exit": VOLUNTARY_EXIT,
    "risk_guard": VOLUNTARY_EXIT,
    "tier_exit": VOLUNTARY_EXIT,
    "timeout": VOLUNTARY_EXIT,
    "llm_exit": VOLUNTARY_EXIT,
}


def canonical_reason_for(caller_reason: str | None,
                         caller_source: str | None = None) -> str | None:
    """Canonical semantics for one exit request, or None if unmappable.

    An unknown source with an unmappable reason REFUSES. The fix for prose
    was a structured source, not a looser parser — "every unknown string is
    VOLUNTARY_EXIT" would launder anything into the settlement vocabulary.
    """
    source = str(caller_source or "").strip().upper()
    raw = str(caller_reason or "").strip()
    low = raw.lower()

    if source in _REASON_SOURCES:
        # A price-threshold caller must say WHICH threshold.
        return _TRIGGER_REASONS.get(low)
    if source in _SOURCE_REASON:
        return _SOURCE_REASON[source]
    if source:
        return None          # a source we do not know is not a free pass

    if not raw:
        return VOLUNTARY_EXIT           # an explicit close with no stated why
    return _REASON_MAP.get(low)


def _canonical_ledger_available() -> bool:
    """Does this database HAVE a canonical settlement ledger at all?

    The operator book has not been migrated: `paper_position_settlements`
    does not exist there, and a dispatcher that queries it unconditionally
    cannot exit a legacy trade on the very schema it has to support today.

    Schema introspection, deliberately — not a broad `except
    OperationalError`. "The table is absent" and "the database is broken"
    are different facts, and treating a failed connection as proof that a
    position is legacy would settle a canonical fill through the old
    economy. This raises on a real failure; only a genuinely missing table
    returns False. It never creates anything.
    """
    from sqlalchemy import inspect
    from app.database import engine
    return inspect(engine).has_table("paper_position_settlements")


def classify_position(position, header) -> str:
    """LEGACY | CANONICAL | HYBRID, from the two existing concepts plus the
    ledger. Independently testable on purpose.

    `has_canonical_fill` is the WIDER claim (the fill crossed a venue book);
    `is_canonical` is the narrow one (venue book AND per-leg costs AND this
    epoch AND a named execution). A position that satisfies one but not the
    other, or carries a ledger without a canonical fill, is a hybrid — and a
    hybrid is refused, never adjudicated.
    """
    from lib.canonical_entry import has_canonical_fill, is_canonical
    from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                      EXECUTION_MODEL_CANONICAL,
                                      SETTLEMENT_VERSION)

    filled = has_canonical_fill(position)
    canonical = is_canonical(position)

    # THE WIDE FILL CLAIM DECIDES FIRST, and it is answered from the
    # position's own provenance — no table required. That is what makes the
    # still-unmigrated operator book exitable: a position with no venue-book
    # fill is legacy whether or not this database has ever heard of a
    # settlement ledger.
    if not filled:
        # ...unless a ledger names it anyway, which is a corrupted pairing
        # rather than an old trade.
        return LEGACY if header is None else HYBRID
    if header is None:
        # A venue-book fill with no ledger — because the row is missing, or
        # because the schema cannot hold one — is the half-migrated state
        # that neither economy may settle.
        return HYBRID
    if not canonical:
        return HYBRID
    if (header.settlement_version != SETTLEMENT_VERSION
            or header.cost_model != COST_MODEL_CANONICAL
            or header.execution_model != EXECUTION_MODEL_CANONICAL):
        return HYBRID
    from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
    if header.engine_epoch != CANONICAL_ENGINE_EPOCH:
        return HYBRID
    return CANONICAL


def _load(position_id: str):
    """One short read: the position row, and its header IF one can exist.

    THE POSITION COMES FIRST, AND THE LEDGER IS ONLY CONSULTED WHEN THERE
    IS A LEDGER. On the unmigrated operator schema the settlement table is
    absent, and asking for it would break the legacy exit path on the only
    database that currently matters.
    """
    from app.database import PaperPosition, PaperPositionSettlement, get_db
    with get_db() as db:
        pos = db.query(PaperPosition).filter(
            PaperPosition.id == position_id).first()
        if pos is None:
            return None, None, True
        ledger = _canonical_ledger_available()
        header = None
        if ledger:
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).first()
        db.expunge_all()
    return pos, header, ledger


def _hybrid_refusal(position_id: str) -> dict:
    return {"ok": False, "route": HYBRID,
            "error": HYBRID_POSITION_EXIT_REFUSED,
            "position_id": position_id,
            "detail": ("this position mixes canonical and legacy facts — a "
                       "venue-book fill without a valid canonical ledger, or "
                       "a ledger under superseded models. Neither economy "
                       "may settle it, and picking whichever route happens "
                       "to work is how half-honest economics get written")}


def _apply_learning(result: dict) -> dict:
    """Best-effort post-settlement handoff (§11). ONE attempt, outside the
    financial transaction.

    A learning failure NEVER turns a correct financial close into a failed
    one: the trade closed, the cash moved, the ledger is right. Learning is
    a secondary subsystem with its own recovery sweep.
    """
    outcome_id = result.get("realized_outcome_id")
    if not outcome_id:
        return result
    try:
        from lib import canonical_learning as CL
        learning = CL.apply_realized_outcome(outcome_id)
    except Exception as e:                       # never take a close down
        logger.error("[ExitDispatch] learning handoff crashed for %s: %s",
                     outcome_id, e, exc_info=True)
        learning = {"ok": False, "error": "LEARNING_HANDOFF_CRASHED",
                    "detail": str(e)}
    if not learning.get("ok"):
        logger.warning("[ExitDispatch] %s settled correctly but learning "
                       "did not apply: %s", outcome_id, learning.get("error"))
    result["learning"] = learning
    result["learning_status"] = learning.get("result") or learning.get("error")
    return result


def _canonical_result(raw: dict, position_id: str, symbol: str,
                      canonical_reason: str, *, partial: bool) -> dict:
    """Normalize B2A/B2B output into the shape callers already expect.

    `close_price` is the ACTUAL fill, never the caller's mark. `pnl` is
    economics, never `cash_delta_usd` — that carries the released margin,
    which was always ours and is not profit.
    """
    if not raw.get("ok"):
        out = {"ok": False, "route": CANONICAL, "position_id": position_id,
               "symbol": symbol, "reason": canonical_reason,
               "error": raw.get("error"), "detail": raw.get("detail")}
        for passthrough in ("venue_failure", "reprepare_required",
                            "threshold", "canonical_bid", "canonical_ask",
                            "executable_reference", "caller_reference"):
            if passthrough in raw:
                out[passthrough] = raw[passthrough]
        if raw.get("error") == "EXIT_TRIGGER_NOT_CONFIRMED":
            out["trigger_not_confirmed"] = True
        return out

    out = {"ok": True, "route": CANONICAL, "position_id": position_id,
           "symbol": symbol, "reason": canonical_reason,
           "close_price": raw.get("fill_price"),
           "kind": raw.get("kind"),
           "revision": raw.get("revision")}
    if partial or raw.get("kind") == "PARTIAL_EXIT":
        out["closed_qty"] = raw.get("filled_qty")
        out["remaining_qty"] = raw.get("remaining_qty")
        # THIS LEG's economics — not the cash delta.
        out["pnl"] = raw.get("leg_net_pnl_usd")
    else:
        out["pnl"] = raw.get("net_pnl_usd")
        out["outcome"] = raw.get("outcome")
        out["realized_outcome_id"] = raw.get("realized_outcome_id")
        out["closed_qty"] = raw.get("filled_qty")
    return out


def request_position_exit(position_id: str, *,
                          caller_price: float | None = None,
                          caller_reason: str | None = None,
                          caller_source: str | None = None,
                          trigger_price: float | None = None,
                          max_age_s: float | None = None) -> dict:
    """Close a paper position in FULL, by whichever economy owns it."""
    pos, header, _ledger = _load(position_id)
    if pos is None:
        return {"ok": False, "error": POSITION_NOT_FOUND,
                "position_id": position_id}
    if pos.status != "Open":
        return {"ok": False, "error": POSITION_NOT_OPEN,
                "position_id": position_id, "status": pos.status,
                "detail": f"position is {pos.status!r}, not Open"}

    route = classify_position(pos, header)
    if route == HYBRID:
        return _hybrid_refusal(position_id)

    if route == LEGACY:
        from lib.paper_engine import close_paper_position
        raw = close_paper_position(position_id, float(caller_price or 0),
                                   reason=caller_reason or "manual")
        raw = dict(raw)
        raw["route"] = LEGACY
        raw.setdefault("position_id", position_id)
        return raw

    canonical_reason = canonical_reason_for(caller_reason,
                                          caller_source=caller_source)
    if canonical_reason is None:
        return {"ok": False, "route": CANONICAL, "error": UNKNOWN_EXIT_REASON,
                "position_id": position_id,
                "detail": (f"caller reason {caller_reason!r} has no canonical "
                           f"meaning; refusing to guess one into the "
                           f"settlement vocabulary")}

    from lib import canonical_exit as CX
    raw = CX.close_canonical_position(
        position_id, exit_reason=canonical_reason,
        decision_price=caller_price, trigger_price=trigger_price,
        max_age_s=max_age_s, caller_source=caller_source,
        caller_reason=caller_reason)
    out = _canonical_result(raw, position_id, pos.symbol, canonical_reason,
                            partial=False)
    if out.get("ok"):
        out = _apply_learning(out)
    return out


def request_position_partial_exit(position_id: str, *,
                                  fraction: float | None = None,
                                  requested_qty: float | None = None,
                                  caller_price: float | None = None,
                                  caller_reason: str | None = None,
                                  caller_source: str | None = None,
                                  max_age_s: float | None = None) -> dict:
    """Reduce a paper position, by whichever economy owns it."""
    pos, header, _ledger = _load(position_id)
    if pos is None:
        return {"ok": False, "error": POSITION_NOT_FOUND,
                "position_id": position_id}
    if pos.status != "Open":
        return {"ok": False, "error": POSITION_NOT_OPEN,
                "position_id": position_id, "status": pos.status}

    route = classify_position(pos, header)
    if route == HYBRID:
        return _hybrid_refusal(position_id)

    if route == LEGACY:
        from lib.paper_engine import partial_close_paper_position
        raw = partial_close_paper_position(
            position_id, float(fraction or 0), float(caller_price or 0),
            reason=caller_reason or "scale_out")
        raw = dict(raw)
        raw["route"] = LEGACY
        raw.setdefault("position_id", position_id)
        return raw

    canonical_reason = canonical_reason_for(caller_reason,
                                          caller_source=caller_source)
    if canonical_reason is None:
        return {"ok": False, "route": CANONICAL, "error": UNKNOWN_EXIT_REASON,
                "position_id": position_id,
                "detail": f"caller reason {caller_reason!r} has no canonical "
                          f"meaning"}

    from lib import canonical_exit as CX
    raw = CX.close_canonical_position(
        position_id, close_fraction=fraction, requested_qty=requested_qty,
        exit_reason=canonical_reason, decision_price=caller_price,
        max_age_s=max_age_s, caller_source=caller_source,
        caller_reason=caller_reason)
    # A partial creates no realized outcome, so there is nothing to learn
    # from — one position votes once, at its final exit.
    return _canonical_result(raw, position_id, pos.symbol, canonical_reason,
                             partial=True)
