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
