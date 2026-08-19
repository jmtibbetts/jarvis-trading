"""Finish virtual executions that a crash interrupted — at their own prices.

WHAT RECOVERY IS FOR. A commitment row means the fill happened. If the
process died before settlement, the economics are owed and the market has
moved on. Recovery settles the ORIGINAL fill: same quantity, same price,
same model, same instrument. It does not re-mark, does not re-check whether
the trigger still holds, and does not ask the venue for anything.

That last part is the whole point. Re-deciding at the current market would
mean a stop that triggered at 61,000 could be settled at 70,000 simply
because a restart happened in between — the market history rewritten by an
operating-system event.

WHAT IT IS NOT. This is not broker reconciliation. There is no external
order to look up, because JARVIS owns the virtual execution model and
declared the commit boundary itself. Against a real venue the same crash
would need an exchange order id and a fill query, and that is deliberately
absent here rather than faked.

IDEMPOTENT BY CONSTRUCTION. Settlement is keyed on `execution_id`, which is
the commitment's own primary key. Recovering twice settles once.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def recover_pending(limit: int = 50) -> dict:
    """Settle every committed fill that never finished.

    Returns counts rather than raising: recovery runs at startup and inside
    the management cycle, and one unrecoverable row must not stop the
    others or the desk.
    """
    from lib import execution_commitment as EC

    rows = EC.pending(limit=limit)
    out = {"found": len(rows), "settled": 0, "abandoned": 0, "failed": 0,
           "details": []}
    for row in rows:
        try:
            result = _settle_one(row)
        except Exception as exc:                    # noqa: BLE001
            out["failed"] += 1
            out["details"].append({"execution_id": row["execution_id"],
                                   "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("[Recovery] %s could not be settled: %s",
                           row["execution_id"], exc)
            continue
        out[result["bucket"]] += 1
        out["details"].append(result["detail"])
    if rows:
        logger.warning("[Recovery] %d committed execution(s) survived a "
                       "restart: %d settled, %d abandoned, %d failed",
                       out["found"], out["settled"], out["abandoned"],
                       out["failed"])
    return out


def _settle_one(row: dict) -> dict:
    """One committed fill, settled from its own persisted facts."""
    from lib import canonical_settlement as CS
    from lib import execution_commitment as EC

    kind = row.get("intent_kind")
    if kind == EC.ENTRY:
        # Entry commitment recovery is not implemented: entry settlement
        # needs the full authorization context, and inventing it from a
        # commitment row would be reconstructing risk after the fact. The
        # entry boundary is therefore settlement itself — see the module
        # note in lib/execution_commitment — so an ENTRY row here means the
        # boundary moved without this path being updated.
        EC.mark_abandoned(row["execution_id"],
                          reason="ENTRY_RECOVERY_NOT_IMPLEMENTED")
        return {"bucket": "abandoned",
                "detail": {"execution_id": row["execution_id"],
                           "reason": "ENTRY_RECOVERY_NOT_IMPLEMENTED"}}

    position_id = row.get("position_id")
    if not position_id:
        EC.mark_abandoned(row["execution_id"], reason="NO_POSITION_ID")
        return {"bucket": "abandoned",
                "detail": {"execution_id": row["execution_id"],
                           "reason": "NO_POSITION_ID"}}

    # DID THIS EXACT FILL ALREADY SETTLE? Ask BEFORE looking at revisions.
    #
    # There is a seam between `settle_prepared_exit` committing and
    # `mark_settled` running. A crash in that window leaves a PENDING
    # commitment whose economics are already in the ledger and whose
    # position has moved on — which looks exactly like a lost race. Calling
    # that ABANDONED would label a successfully settled fill as one that
    # never became economics, purely because Python died before a status
    # flag changed.
    #
    # Settlement keys its legs on execution_id, so the ledger can answer
    # this directly.
    settled_leg = _existing_leg(row["execution_id"])
    if settled_leg is not None:
        mismatch = _facts_disagree(row, settled_leg)
        if mismatch:
            # The same execution id maps to different economics. That is
            # corruption or an id collision, not a recovery case, and
            # silently marking it SETTLED would bless it.
            raise RuntimeError(
                f"COMMITTED_FACTS_DISAGREE_WITH_SETTLEMENT for "
                f"{row['execution_id']}: {mismatch}")
        EC.mark_settled(row["execution_id"],
                        detail="settlement already existed; only the "
                               "bookkeeping flag was lost")
        return {"bucket": "settled",
                "detail": {"execution_id": row["execution_id"],
                           "position_id": position_id,
                           "already_settled": True}}

    # If the position already moved past this revision, another settlement
    # won the race and this fill describes a state that no longer exists.
    from lib.canonical_exit import read_exit_snapshot
    snap = read_exit_snapshot(position_id)
    if isinstance(snap, dict):
        EC.mark_abandoned(row["execution_id"],
                          reason=str(snap.get("error")))
        return {"bucket": "abandoned",
                "detail": {"execution_id": row["execution_id"],
                           "reason": snap.get("error")}}
    if (row.get("expected_revision") is not None
            and snap.settlement_revision != row["expected_revision"]):
        EC.mark_abandoned(row["execution_id"],
                          reason=CS.STALE_SETTLEMENT_REVISION)
        return {"bucket": "abandoned",
                "detail": {"execution_id": row["execution_id"],
                           "reason": CS.STALE_SETTLEMENT_REVISION}}

    logger.info("[Recovery] settling committed %s for %s at its ORIGINAL "
                "fill %.8f x %.8f (not the current market)",
                kind, position_id, row["filled_qty"], row["fill_price"])
    from lib.canonical_exit import settle_committed_exit
    result = settle_committed_exit(row, snap)
    if result.get("ok"):
        EC.mark_settled(row["execution_id"])
        return {"bucket": "settled",
                "detail": {"execution_id": row["execution_id"],
                           "position_id": position_id,
                           "fill_price": row["fill_price"],
                           "filled_qty": row["filled_qty"]}}
    if result.get("error") == CS.STALE_SETTLEMENT_REVISION:
        EC.mark_abandoned(row["execution_id"],
                          reason=CS.STALE_SETTLEMENT_REVISION)
        return {"bucket": "abandoned",
                "detail": {"execution_id": row["execution_id"],
                           "reason": CS.STALE_SETTLEMENT_REVISION}}
    # Transient: leave it PENDING so the next sweep tries again.
    return {"bucket": "failed",
            "detail": {"execution_id": row["execution_id"],
                       "error": result.get("error"),
                       "detail": result.get("detail")}}


def _existing_leg(execution_id: str):
    """The settlement leg for this exact execution, if one exists."""
    from app.database import PaperSettlementLeg, get_db
    with get_db() as db:
        leg = db.query(PaperSettlementLeg).filter(
            PaperSettlementLeg.execution_id == execution_id).first()
        if leg is None:
            return None
        return {"position_id": leg.position_id, "kind": leg.kind,
                "filled_qty": leg.filled_qty, "fill_price": leg.fill_price,
                "instrument_id": getattr(leg, "instrument_id", None)}


def _facts_disagree(commitment: dict, leg: dict) -> str | None:
    """Do the committed facts and the settled facts describe one trade?

    Checked rather than assumed: an execution id that maps to different
    economics means corruption or a collision, and that must fail loudly
    instead of being tidied away as a successful recovery.
    """
    checks = (
        ("position_id", commitment.get("position_id"), leg.get("position_id")),
        ("filled_qty", commitment.get("filled_qty"), leg.get("filled_qty")),
        ("fill_price", commitment.get("fill_price"), leg.get("fill_price")),
    )
    problems = []
    for name, a, b in checks:
        if a is None or b is None:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) > 1e-9 * max(1.0, abs(float(a))):
                problems.append(f"{name}: committed {a!r} vs settled {b!r}")
        elif str(a) != str(b):
            problems.append(f"{name}: committed {a!r} vs settled {b!r}")
    instrument = commitment.get("instrument_id")
    if instrument and leg.get("instrument_id")             and instrument != leg["instrument_id"]:
        problems.append(f"instrument_id: committed {instrument!r} vs settled "
                        f"{leg['instrument_id']!r}")
    return "; ".join(problems) or None
