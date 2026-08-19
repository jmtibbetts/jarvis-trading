"""The moment a virtual fill stops being provisional.

THE DEFECT THIS REMOVES. Between `ExecutionVenue.submit()` returning a fill
and settlement persisting it, the execution existed only in RAM. A process
death anywhere in that window — fee lookup, carry lookup, settlement
transaction — erased it completely, and the next management cycle re-decided
against whatever the market had become.

That was previously argued to be safe because losers and winners were erased
alike. Symmetry is the weaker claim. It rules out DIRECTIONAL bias and says
nothing about PROCESS-TIMING bias, and a simulator whose history depends on
whether Python happened to die is still wrong. A stop that triggered at
61,000 and then vanished because the process crashed, leaving a position
that later closed at 70,000, has had its history rewritten by an operating
system event.

THE BOUNDARY, STATED ONCE. A virtual execution is COMMITTED the moment the
venue returns a usable fill. Before that moment the attempt may vanish and
nothing economic has happened. After it, the fill is an immutable fact:
quantity, price, model and market snapshot are settled from the persisted
row and are never re-derived from a later market.

    not committed  ->  no economic fact exists, crash is a clean no-op
    committed      ->  the fact survives, and settlement finishes it

ONE RULE FOR BOTH SIDES OF A TRADE. This was first built for exits, while
entries still committed at settlement -- and two different definitions of
"committed" is not a boundary, it is a coin toss about which crashes count.
The asymmetry favoured the bot: a crash between an entry fill and its
settlement erased the entry, and the next cycle re-decided against whatever
the market had become, so a fall in price bought JARVIS a better entry than
the order actually paid. Entry now commits at its fill and carries the
authorization that approved it, so recovery HONOURS that approval rather
than re-deciding size after the fact.

ONLY A CRASH MAY LEAVE A FILL PENDING. Any path that RETURNS has told its
caller whether the position opened, so it resolves the commitment before it
goes -- SETTLED or ABANDONED with a reason. A refusal that left a fill
pending would let recovery later open a position the decision path had
already declined, while the caller, told "not opened", may have re-decided
the same signal. Two positions, one intention.

WHY THIS IS ENOUGH HERE, AND WOULD NOT BE AGAINST A REAL VENUE. JARVIS owns
the virtual execution model, so it can declare its own commit point and be
certain nothing exists on the other side of it. An external broker can
accept an order in the gap between submit and persist, so that case needs an
exchange order id and a fill lookup on recovery. That is deliberately NOT
built here — FULL_VIRTUAL does not need it and pretending otherwise would be
inventing reconciliation for a counterparty that does not exist.

COSTS BIND TO THE COMMITTED FILL. Fees, funding and carry are computed from
the persisted execution and its timestamp, never from a fresh market read.
A retry tomorrow must not price yesterday's funding interval from today's
book.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ENTRY = "ENTRY"
EXIT = "EXIT"
PARTIAL_EXIT = "PARTIAL_EXIT"

COMMITTED_PENDING_SETTLEMENT = "COMMITTED_PENDING_SETTLEMENT"
SETTLED = "SETTLED"
ABANDONED = "ABANDONED"

# What the recovery sweep looks for.
UNFINISHED = (COMMITTED_PENDING_SETTLEMENT,)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_commitment(*, execution_id: str, intent_kind: str, symbol: str,
           product: str, venue: str, instrument_id: str | None,
           side: str, requested_qty: float, filled_qty: float,
           fill_price: float, quantity_unit: str | None,
           multiplier: float | None, fill_model: str,
           fill_model_version: str,
           position_id: str | None = None,
           expected_revision: int | None = None,
           observation_id: str | None = None,
           market_snapshot: dict | None = None,
           plan_facts: dict | None = None,
           capability: dict | None = None,
           fee_context: dict | None = None) -> dict:
    """Record that this virtual fill HAPPENED, before anything derives from it.

    Keyed by `execution_id`, which is also settlement's idempotency key, so
    a replay of the same execution cannot produce a second commitment.

    Its own short transaction, holding no provider call: the whole point is
    that it lands before the fee and carry lookups that follow it.
    """
    from app.database import VirtualExecutionCommitment, get_db

    row = dict(
        execution_id=execution_id, intent_kind=intent_kind,
        position_id=position_id, symbol=symbol, product=product,
        venue=venue, instrument_id=instrument_id, side=side,
        requested_qty=float(requested_qty), filled_qty=float(filled_qty),
        fill_price=float(fill_price), quantity_unit=quantity_unit,
        multiplier=(float(multiplier) if multiplier is not None else None),
        fill_model=fill_model, fill_model_version=fill_model_version,
        expected_revision=expected_revision, observation_id=observation_id,
        market_snapshot_json=json.dumps(market_snapshot or {}, default=str),
        plan_facts_json=json.dumps(plan_facts or {}, default=str),
        capability_json=json.dumps(capability or {}, default=str),
        fee_context_json=json.dumps(fee_context or {}, default=str),
        state=COMMITTED_PENDING_SETTLEMENT, committed_at=_utc(),
    )
    with get_db() as db:
        existing = db.query(VirtualExecutionCommitment).filter(
            VirtualExecutionCommitment.execution_id == execution_id).first()
        if existing is not None:
            # A REPLAY, not a second fill. Return what was committed; the
            # caller must settle THAT, not re-market.
            return _as_dict(existing)
        db.add(VirtualExecutionCommitment(**row))
        db.commit()
    logger.info("[Commit] %s %s %s %s %.8f @ %.8f (%s)", intent_kind, symbol,
                instrument_id or "-", side, row["filled_qty"],
                row["fill_price"], fill_model)
    return row


def attach_observation(execution_id: str, observation_id: str) -> bool:
    """Link the decision observation once it exists.

    The commitment lands at the fill, which is BEFORE the observation is
    minted. If the process dies after the observation and before
    settlement, recovery must reuse it rather than mint a second one for
    the same execution -- one fill, one decision record.
    """
    from app.database import VirtualExecutionCommitment, get_db
    with get_db() as db:
        row = db.query(VirtualExecutionCommitment).filter(
            VirtualExecutionCommitment.execution_id == execution_id).first()
        if row is None:
            return False
        row.observation_id = observation_id
        db.commit()
    return True


def mark_settled(execution_id: str, *, detail: str | None = None) -> bool:
    """Settlement finished. Idempotent."""
    from app.database import VirtualExecutionCommitment, get_db
    with get_db() as db:
        row = db.query(VirtualExecutionCommitment).filter(
            VirtualExecutionCommitment.execution_id == execution_id).first()
        if row is None:
            return False
        if row.state != SETTLED:
            row.state = SETTLED
            row.settled_at = _utc()
            row.detail = detail
            db.commit()
    return True


def mark_abandoned(execution_id: str, *, reason: str) -> bool:
    """Settlement refused for a reason that will never resolve.

    Used only where re-attempting is provably pointless — a stale revision
    means another settlement already won, so this fill describes a position
    state that no longer exists. It is NOT used for transient failures,
    which must stay pending so recovery finishes them.
    """
    from app.database import VirtualExecutionCommitment, get_db
    with get_db() as db:
        row = db.query(VirtualExecutionCommitment).filter(
            VirtualExecutionCommitment.execution_id == execution_id).first()
        if row is None:
            return False
        row.state = ABANDONED
        row.detail = reason
        row.settled_at = _utc()
        db.commit()
    return True


def pending(limit: int = 100) -> list[dict]:
    """Commitments that survived a crash and still owe a settlement."""
    from app.database import VirtualExecutionCommitment, get_db
    with get_db() as db:
        rows = (db.query(VirtualExecutionCommitment)
                .filter(VirtualExecutionCommitment.state
                        .in_(list(UNFINISHED)))
                .order_by(VirtualExecutionCommitment.committed_at)
                .limit(limit).all())
        return [_as_dict(r) for r in rows]


def get(execution_id: str) -> dict | None:
    from app.database import VirtualExecutionCommitment, get_db
    with get_db() as db:
        row = db.query(VirtualExecutionCommitment).filter(
            VirtualExecutionCommitment.execution_id == execution_id).first()
        return _as_dict(row) if row is not None else None


def _as_dict(row) -> dict:
    out = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for key, target in (("market_snapshot_json", "market_snapshot"),
                        ("plan_facts_json", "plan_facts"),
                        ("capability_json", "capability"),
                        ("fee_context_json", "fee_context")):
        try:
            out[target] = json.loads(out.get(key) or "{}")
        except (TypeError, ValueError):
            out[target] = {}
    return out
