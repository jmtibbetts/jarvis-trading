"""Persistence and lifecycle for manually executed trades.

THE BOUNDARY THIS MODULE ENFORCES. A manual trade is EVIDENCE ABOUT AN
EXTERNAL ACCOUNT. Recording one must not move virtual cash, open a paper
position, touch a settlement ledger or fund a DEX wallet — and this module
is where that promise is either kept or broken, so it imports none of those
writers and `tests/test_manual_operator_execution.py` pins that by AST as
well as by measuring the tables before and after.

Why it matters more than it sounds: the virtual book is a TRAINING
INSTRUMENT. Its whole value is that its balance was produced by the
simulator and nothing else, so a divergence between simulated and real
results is attributable. Fund it with real-world evidence, even correctly,
and that comparison is gone — the simulator would be measured against a
balance it never produced, and nobody afterwards could separate the two.

LIFECYCLE, AND WHY LEG KINDS ARE ENFORCED

    DRAFT --ENTRY-->            OPEN
    OPEN  --PARTIAL_EXIT-->     PARTIALLY_CLOSED   (quantity MUST remain)
    *     --FINAL_EXIT-->       CLOSED             (quantity MUST reach flat)

A PARTIAL_EXIT that would flatten the book is refused, and so is a
FINAL_EXIT that would leave quantity on. Without that, the leg kinds become
decorative and the ledger stops being able to say whether a position is
still live — which is the one question a manual desk exists to answer
between entry and exit.

THE FINAL EXIT AND THE REALIZED OUTCOME COMMIT TOGETHER. A closed trade
with no final truth is not an acceptable state, exactly as it is not for a
canonical paper position. Learning is decoupled from that transaction and
may be blocked afterwards without unwinding it.

LEARNING IS BLOCKED, NOT FAKED, WHEN COSTS ARE UNKNOWN. `RealizedOutcome`
stores floats, so an unevidenced fee necessarily lands as 0.0 in the
canonical row — and a net P&L computed over a missing cost is FLATTERING.
The outcome is still written, because it is real financial history worth
keeping and inspecting; it is marked `BLOCKED_INCOMPLETE_COSTS` and does
not vote. THE BOT MUST NEVER LEARN THAT IT MADE MONEY BECAUSE A COST WAS
NEVER ENTERED.
"""
from __future__ import annotations

import json
import logging

from lib import manual_execution as mx
from lib.execution_mode import MANUAL_OPERATOR, assert_executable

logger = logging.getLogger(__name__)

MANUAL_STORE_VERSION = "manual_trade_store_v1"

# Learning projection states, mirroring lib/canonical_learning.py's
# vocabulary rather than inventing a second one.
PENDING = "PENDING"
APPLIED = "APPLIED"
# TERMINAL AND DELIBERATELY NOT A FAILURE. The trade is real; its cost
# record is incomplete, so its net result is not trustworthy enough to
# teach from. Completing the evidence and re-deriving is the remedy.
BLOCKED_INCOMPLETE_COSTS = "BLOCKED_INCOMPLETE_COSTS"

CORRECTION_TARGET_TRADE = "TRADE"
CORRECTION_TARGET_LEG = "LEG"
CORRECTION_TARGET_COST_EVENT = "COST_EVENT"
CORRECTION_TARGETS = frozenset({CORRECTION_TARGET_TRADE,
                                CORRECTION_TARGET_LEG,
                                CORRECTION_TARGET_COST_EVENT})

# Trade fields a correction may touch. The RECOMMENDATION IS NOT AMONG
# THEM: what JARVIS said at time T is history, and "correcting" it to match
# what happened would delete the calibration baseline the manual desk exists
# to produce.
CORRECTABLE_TRADE_FIELDS = frozenset({
    "opened_at", "closed_at", "leverage", "margin_mode", "collateral_usd",
    "collateral_capital_kind", "stop_used", "initial_risk_usd",
    "operator_reported_realized_pnl_usd", "operator_reported_evidence_type",
    "evidence_type", "evidence_source", "notes", "instrument_id",
})
CORRECTABLE_LEG_FIELDS = frozenset({
    "quantity", "fill_price", "at", "fee_usd", "liquidity_role",
    "decision_price", "venue_order_ref", "exit_reason", "evidence_type",
    "evidence_source", "notes",
})
CORRECTABLE_COST_EVENT_FIELDS = frozenset({
    "kind", "amount_usd", "at", "evidence_type", "evidence_source", "notes",
})


class ManualTradeNotFound(LookupError):
    """No such manual trade."""


def _json(value):
    return None if value is None else json.dumps(value)


def _unjson(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ── Read ─────────────────────────────────────────────────────────────────
def _hydrate(row, legs, events) -> mx.ManualTrade:
    """Rebuild the semantic trade from persisted rows.

    The dataclasses re-validate on construction, so a row that became
    invalid — by a bad migration, a stray writer, a hand edit — surfaces as
    a refusal at read time rather than as a quietly wrong number.
    """
    rec = None
    rec_json = _unjson(row.recommendation_json)
    if rec_json:
        fields = mx.RecommendationSnapshot.__dataclass_fields__
        rec = mx.RecommendationSnapshot(**{
            k: (tuple(v) if k == "targets" and isinstance(v, list) else v)
            for k, v in rec_json.items() if k in fields})

    trade = mx.ManualTrade(
        trade_id=row.id,
        venue=row.venue, product=row.product, symbol=row.symbol,
        direction=row.direction, quantity_unit=row.quantity_unit,
        account_label=row.account_label, instrument_id=row.instrument_id,
        multiplier=row.multiplier, state=row.state,
        opened_at=row.opened_at, closed_at=row.closed_at,
        leverage=row.leverage, margin_mode=row.margin_mode or mx.UNKNOWN,
        collateral_usd=row.collateral_usd,
        collateral_capital_kind=row.collateral_capital_kind,
        stop_used=row.stop_used,
        targets_used=tuple(_unjson(row.targets_used_json, []) or ()),
        initial_risk_usd=row.initial_risk_usd,
        recommendation=rec,
        declared_absent_costs=tuple(
            _unjson(row.declared_absent_costs_json, []) or ()),
        operator_reported_realized_pnl_usd=(
            row.operator_reported_realized_pnl_usd),
        operator_reported_evidence_type=row.operator_reported_evidence_type,
        evidence_type=row.evidence_type, evidence_source=row.evidence_source,
        notes=row.notes or "", engine_epoch=row.engine_epoch,
    )
    for l in legs:
        trade.legs.append(mx.ManualExecutionLeg(
            kind=l.kind, quantity=l.quantity, fill_price=l.fill_price,
            at=l.at, fee_usd=l.fee_usd,
            liquidity_role=l.liquidity_role or mx.UNKNOWN,
            decision_price=l.decision_price, leg_id=l.id,
            venue_order_ref=l.venue_order_ref, exit_reason=l.exit_reason,
            evidence_type=l.evidence_type, evidence_source=l.evidence_source,
            notes=l.notes or ""))
    for e in events:
        trade.cost_events.append(mx.ManualCostEvent(
            kind=e.kind, amount_usd=e.amount_usd, at=e.at, event_id=e.id,
            evidence_type=e.evidence_type, evidence_source=e.evidence_source,
            notes=e.notes or ""))
    return trade


def _load(db, trade_id: str):
    from app.database import (ManualTradeCostEvent, ManualTradeLeg,
                              ManualTradeRecord)

    row = db.query(ManualTradeRecord).filter(
        ManualTradeRecord.id == trade_id).first()
    if row is None:
        raise ManualTradeNotFound(f"no manual trade {trade_id!r}")
    legs = db.query(ManualTradeLeg).filter(
        ManualTradeLeg.trade_id == trade_id).order_by(
        ManualTradeLeg.sequence).all()
    events = db.query(ManualTradeCostEvent).filter(
        ManualTradeCostEvent.trade_id == trade_id).order_by(
        ManualTradeCostEvent.at).all()
    return row, legs, events


def get(trade_id: str) -> mx.ManualTrade:
    from app.database import get_db

    with get_db() as db:
        row, legs, events = _load(db, trade_id)
        return _hydrate(row, legs, events)


def get_record(trade_id: str) -> dict:
    """The trade plus its persisted projection state, for the API."""
    from app.database import get_db

    with get_db() as db:
        row, legs, events = _load(db, trade_id)
        trade = _hydrate(row, legs, events)
        out = trade.as_dict()
        out["learning_state"] = row.learning_state
        out["learning_error"] = row.learning_error
        out["revision"] = row.revision
        out["realized_outcome"] = _unjson(row.realized_outcome_json)
        out["created_at"] = row.created_at
        out["updated_at"] = row.updated_at
        return out


def list_trades(*, state: str | None = None, venue: str | None = None,
                account_label: str | None = None,
                thesis_id: str | None = None,
                linked: bool | None = None, limit: int = 100) -> list:
    from app.database import ManualTradeRecord, get_db

    with get_db() as db:
        q = db.query(ManualTradeRecord)
        if state:
            q = q.filter(ManualTradeRecord.state == state)
        if venue:
            q = q.filter(ManualTradeRecord.venue == venue)
        if account_label:
            q = q.filter(ManualTradeRecord.account_label == account_label)
        if thesis_id:
            q = q.filter(ManualTradeRecord.thesis_id == thesis_id)
        if linked is True:
            q = q.filter(ManualTradeRecord.thesis_id.isnot(None))
        elif linked is False:
            q = q.filter(ManualTradeRecord.thesis_id.is_(None))
        rows = q.order_by(ManualTradeRecord.created_at.desc()).limit(
            max(1, min(int(limit), 500))).all()
        ids = [r.id for r in rows]

    return [get_record(i) for i in ids]


# ── Write ────────────────────────────────────────────────────────────────
def create(*, venue: str, product: str, symbol: str, direction: str,
           quantity_unit: str, account_label: str = "default",
           multiplier: float = 1.0, instrument_id: str | None = None,
           leverage: float | None = None, margin_mode: str = mx.UNKNOWN,
           collateral_usd: float | None = None,
           collateral_capital_kind: str = "CAPITAL_UNKNOWN",
           stop_used: float | None = None, targets_used=(),
           initial_risk_usd: float | None = None,
           recommendation: mx.RecommendationSnapshot | None = None,
           declared_absent_costs=(),
           operator_reported_realized_pnl_usd: float | None = None,
           operator_reported_evidence_type: str | None = None,
           evidence_type: str | None = None,
           evidence_source: str | None = None,
           notes: str = "", opened_at: str | None = None,
           trade_id: str | None = None) -> str:
    """Record a manual trade in DRAFT. No legs yet, so no economics yet.

    `recommendation=None` is the INDEPENDENT-OPERATOR case and is fully
    supported. Nothing here manufactures a thesis to fill the gap.
    """
    from app.database import ManualTradeRecord, get_db, new_id, now_iso
    from lib.engine_epoch import ENGINE_EPOCH as CANONICAL_ENGINE_EPOCH

    # The mode is asserted, not assumed. LIVE_AUTONOMOUS cannot reach this
    # path even if a caller names it.
    assert_executable(MANUAL_OPERATOR)

    # Construct the semantic object FIRST: it validates everything, so an
    # invalid trade never reaches the database at all.
    tid = trade_id or new_id()
    trade = mx.ManualTrade(
        trade_id=tid, venue=venue, product=product, symbol=symbol,
        direction=direction, quantity_unit=quantity_unit,
        account_label=account_label, instrument_id=instrument_id,
        multiplier=multiplier, state=mx.DRAFT, opened_at=opened_at,
        leverage=leverage, margin_mode=margin_mode,
        collateral_usd=collateral_usd,
        collateral_capital_kind=collateral_capital_kind,
        stop_used=stop_used, targets_used=tuple(targets_used or ()),
        initial_risk_usd=initial_risk_usd, recommendation=recommendation,
        declared_absent_costs=tuple(declared_absent_costs or ()),
        operator_reported_realized_pnl_usd=operator_reported_realized_pnl_usd,
        operator_reported_evidence_type=operator_reported_evidence_type,
        evidence_type=evidence_type, evidence_source=evidence_source,
        notes=notes, engine_epoch=CANONICAL_ENGINE_EPOCH)

    now = now_iso()
    with get_db() as db:
        db.add(ManualTradeRecord(
            id=tid, execution_mode=MANUAL_OPERATOR,
            account_label=trade.account_label, venue=trade.venue,
            product=trade.product, symbol=trade.symbol,
            instrument_id=trade.instrument_id, direction=trade.direction,
            quantity_unit=trade.quantity_unit, multiplier=trade.multiplier,
            state=mx.DRAFT, revision=0, opened_at=trade.opened_at,
            leverage=trade.leverage, margin_mode=trade.margin_mode,
            collateral_usd=trade.collateral_usd,
            collateral_capital_kind=trade.collateral_capital_kind,
            stop_used=trade.stop_used,
            targets_used_json=_json(list(trade.targets_used)),
            initial_risk_usd=trade.initial_risk_usd,
            thesis_id=trade.thesis_id,
            signal_id=(recommendation.signal_id if recommendation else None),
            decision_id=(recommendation.decision_id if recommendation
                         else None),
            recommendation_json=(_json(recommendation.as_dict())
                                 if recommendation else None),
            declared_absent_costs_json=_json(
                list(trade.declared_absent_costs)),
            operator_reported_realized_pnl_usd=(
                trade.operator_reported_realized_pnl_usd),
            operator_reported_evidence_type=(
                trade.operator_reported_evidence_type),
            evidence_type=trade.evidence_type,
            evidence_source=trade.evidence_source, notes=trade.notes,
            learning_state=PENDING, engine_epoch=trade.engine_epoch,
            version=mx.MANUAL_EXECUTION_VERSION,
            created_at=now, updated_at=now))
    return tid


def append_leg(trade_id: str, *, kind: str, quantity: float,
               fill_price: float, at: str, fee_usd: float | None = None,
               liquidity_role: str = mx.UNKNOWN,
               decision_price: float | None = None,
               venue_order_ref: str | None = None,
               exit_reason: str | None = None,
               evidence_type: str | None = None,
               evidence_source: str | None = None,
               notes: str = "") -> dict:
    """Append one fill and advance the lifecycle. ONE TRANSACTION.

    A FINAL_EXIT that flattens the book also builds and persists the
    canonical RealizedOutcome here, inside the same transaction — a closed
    trade must not be able to exist without its final truth.
    """
    from app.database import ManualTradeLeg, get_db, new_id, now_iso

    with get_db() as db:
        row, legs, events = _load(db, trade_id)
        if row.state in mx.TERMINAL_STATES:
            raise mx.ManualExecutionError(
                f"trade {trade_id} is {row.state} and takes no further "
                f"legs. Amending a settled trade is a CORRECTION, which is "
                f"appended with its provenance, not a new fill")

        trade = _hydrate(row, legs, events)
        leg = mx.ManualExecutionLeg(
            kind=kind, quantity=quantity, fill_price=fill_price, at=at,
            fee_usd=fee_usd, liquidity_role=liquidity_role,
            decision_price=decision_price, venue_order_ref=venue_order_ref,
            exit_reason=exit_reason, evidence_type=evidence_type,
            evidence_source=evidence_source, notes=notes)

        # Validate against the WHOLE trade before writing anything: the
        # walk raises if this leg closes more than is open, and costs_usd
        # raises if the leg's fee contradicts a declared-absent category.
        # Both must run HERE — checking them only on read would let a
        # contradictory ledger commit and surface as a refusal later, on
        # a request that changed nothing.
        trade.legs.append(leg)
        walk = trade._walk()
        trade.costs_usd()
        open_qty = walk["open_quantity"]

        if kind == mx.LEG_PARTIAL_EXIT and open_qty <= mx.QTY_TOL:
            raise mx.ManualExecutionError(
                f"a PARTIAL_EXIT of {leg.quantity:g} would flatten the "
                f"position. A partial exit that leaves nothing on is a "
                f"final exit; record it as {mx.LEG_FINAL_EXIT} so the "
                f"ledger can still say whether the trade is live")
        if kind == mx.LEG_FINAL_EXIT and open_qty > mx.QTY_TOL:
            raise mx.ManualExecutionError(
                f"a FINAL_EXIT would leave {open_qty:g} "
                f"{trade.quantity_unit} still open. Record it as "
                f"{mx.LEG_PARTIAL_EXIT}")

        if kind == mx.LEG_ENTRY:
            target = (mx.OPEN if row.state in (mx.DRAFT, mx.OPEN)
                      else mx.PARTIALLY_CLOSED)
        elif kind == mx.LEG_PARTIAL_EXIT:
            target = mx.PARTIALLY_CLOSED
        else:
            target = mx.CLOSED
        # Refuses DRAFT -> PARTIALLY_CLOSED, CLOSED -> anything, and so on.
        new_state = (row.state if target == row.state
                     else mx.parse_state_transition(row.state, target))

        seq = len(legs)
        db.add(ManualTradeLeg(
            id=new_id(), trade_id=trade_id, sequence=seq, kind=leg.kind,
            quantity=leg.quantity, fill_price=leg.fill_price, at=leg.at,
            fee_usd=leg.fee_usd, liquidity_role=leg.liquidity_role,
            decision_price=leg.decision_price,
            venue_order_ref=leg.venue_order_ref,
            exit_reason=leg.exit_reason, evidence_type=leg.evidence_type,
            evidence_source=leg.evidence_source, notes=leg.notes,
            created_at=now_iso()))

        row.state = new_state
        row.revision = int(row.revision or 0) + 1
        row.updated_at = now_iso()
        if row.opened_at is None and kind == mx.LEG_ENTRY:
            row.opened_at = leg.at
        trade.state = new_state
        trade.opened_at = row.opened_at

        result = {"trade_id": trade_id, "state": new_state,
                  "sequence": seq, "revision": row.revision,
                  "open_quantity": open_qty}

        if new_state == mx.CLOSED:
            row.closed_at = leg.at
            trade.closed_at = leg.at
            outcome = mx.realized_outcome(trade)
            row.realized_outcome_json = _json(outcome.as_dict())
            complete = bool(outcome.provenance.get("net_pnl_is_complete"))
            row.learning_state = PENDING if complete else BLOCKED_INCOMPLETE_COSTS
            if not complete:
                row.learning_error = (
                    "net P&L is not trustworthy: no evidence for "
                    + ", ".join(trade.unknown_cost_categories())
                    + ". The outcome is preserved as financial history and "
                      "is barred from learning until the cost record is "
                      "completed — teaching from a result that omits a real "
                      "cost would teach that the trade was better than it "
                      "was")
            result["realized_outcome"] = outcome.as_dict()
            result["learning_state"] = row.learning_state

        return result


def append_cost_event(trade_id: str, *, kind: str, amount_usd: float,
                      at: str, evidence_type: str | None = None,
                      evidence_source: str | None = None,
                      notes: str = "") -> dict:
    """Append funding, a rebate, gas or a penalty.

    ALLOWED AFTER CLOSE. Funding settles late and statements arrive late;
    refusing a real post-close settlement would force the operator to either
    lose it or falsify its timestamp. It re-derives the realized outcome,
    because a cost that arrives after the outcome was built must change the
    outcome rather than sit beside it.
    """
    from app.database import ManualTradeCostEvent, get_db, new_id, now_iso

    with get_db() as db:
        row, legs, events = _load(db, trade_id)
        if row.state in (mx.CANCELLED,):
            raise mx.ManualExecutionError(
                f"trade {trade_id} was CANCELLED and never executed, so it "
                f"incurred no costs")
        event = mx.ManualCostEvent(
            kind=kind, amount_usd=amount_usd, at=at,
            evidence_type=evidence_type, evidence_source=evidence_source,
            notes=notes)
        db.add(ManualTradeCostEvent(
            id=new_id(), trade_id=trade_id, kind=event.kind,
            amount_usd=event.amount_usd, at=event.at,
            evidence_type=event.evidence_type,
            evidence_source=event.evidence_source, notes=event.notes,
            created_at=now_iso()))

        trade = _hydrate(row, legs, events)
        trade.cost_events.append(event)
        # Raises on a declaration/charge contradiction before committing.
        trade.costs_usd()

        row.revision = int(row.revision or 0) + 1
        row.updated_at = now_iso()
        out = {"trade_id": trade_id, "revision": row.revision,
               "costs_usd": trade.costs_usd()}
        if row.state == mx.CLOSED:
            out["realized_outcome"] = _rederive(row, trade)
        return out


def _rederive(row, trade: mx.ManualTrade) -> dict:
    """Rebuild the persisted outcome after late evidence changed the facts."""
    outcome = mx.realized_outcome(trade)
    row.realized_outcome_json = _json(outcome.as_dict())
    complete = bool(outcome.provenance.get("net_pnl_is_complete"))
    if row.learning_state != APPLIED:
        row.learning_state = PENDING if complete else BLOCKED_INCOMPLETE_COSTS
        row.learning_error = (None if complete else
                              "net P&L is not trustworthy: no evidence for "
                              + ", ".join(trade.unknown_cost_categories()))
    return outcome.as_dict()


def terminate(trade_id: str, *, state: str, reason: str) -> dict:
    """CANCELLED (never executed) or ABANDONED (opened, end not evidenced).

    Kept apart deliberately: a trade that never happened and a trade whose
    ending nobody recorded are different facts, and collapsing them would
    let an unfinished position disappear as though it had been called off.
    Neither produces a realized outcome — there is nothing realized to
    report.
    """
    from app.database import get_db, now_iso

    if state not in (mx.CANCELLED, mx.ABANDONED):
        raise mx.ManualExecutionError(
            f"{state!r} is not a termination state; use {mx.CANCELLED} or "
            f"{mx.ABANDONED}")
    if not str(reason or "").strip():
        raise mx.ManualExecutionError(
            "a termination needs a stated reason; an unexplained "
            "disappearance is not a record")

    with get_db() as db:
        row, _legs, _events = _load(db, trade_id)
        mx.parse_state_transition(row.state, state)
        row.state = state
        row.revision = int(row.revision or 0) + 1
        row.notes = ((row.notes or "") + f"\n[{state}] {reason}").strip()
        row.updated_at = now_iso()
        return {"trade_id": trade_id, "state": state,
                "revision": row.revision}


# ── Correction ───────────────────────────────────────────────────────────
def correct(trade_id: str, *, target_kind: str, target_id: str, field: str,
            new_value, reason: str, corrected_by: str,
            evidence_type: str | None = None,
            evidence_source: str | None = None) -> dict:
    """Amend a recorded fact, KEEPING what it used to say.

    The previous value is written to `manual_trade_corrections` in the same
    transaction that changes it, so history is appended to rather than
    overwritten. A reconciliation run later against the corrected value can
    still explain why the original disagreed; without the old value it
    could only report that it does.

    THE RECOMMENDATION IS NOT CORRECTABLE. See CORRECTABLE_TRADE_FIELDS.
    """
    from app.database import (ManualTradeCorrection, ManualTradeCostEvent,
                              ManualTradeLeg, get_db, new_id, now_iso)

    if target_kind not in CORRECTION_TARGETS:
        raise mx.ManualExecutionError(
            f"{target_kind!r} is not a correction target. Known: "
            f"{', '.join(sorted(CORRECTION_TARGETS))}")
    if not str(reason or "").strip():
        raise mx.ManualExecutionError(
            "a correction needs a stated reason: a changed number with no "
            "explanation is indistinguishable from a mistake")
    if not str(corrected_by or "").strip():
        raise mx.ManualExecutionError("a correction needs an author")

    allowed = {CORRECTION_TARGET_TRADE: CORRECTABLE_TRADE_FIELDS,
               CORRECTION_TARGET_LEG: CORRECTABLE_LEG_FIELDS,
               CORRECTION_TARGET_COST_EVENT: CORRECTABLE_COST_EVENT_FIELDS
               }[target_kind]
    if field not in allowed:
        extra = ""
        if target_kind == CORRECTION_TARGET_TRADE and field.startswith(
                ("recommend", "thesis", "signal", "decision")):
            extra = (" The recommendation is frozen at recommendation time: "
                     "changing it to match the execution would delete the "
                     "calibration baseline this desk exists to produce.")
        raise mx.ManualExecutionError(
            f"{field!r} is not correctable on a {target_kind}. Correctable: "
            f"{', '.join(sorted(allowed))}.{extra}")

    with get_db() as db:
        row, legs, events = _load(db, trade_id)

        if target_kind == CORRECTION_TARGET_TRADE:
            target = row
            if target_id != trade_id:
                raise mx.ManualExecutionError(
                    f"target_id {target_id!r} is not trade {trade_id!r}")
        elif target_kind == CORRECTION_TARGET_LEG:
            target = db.query(ManualTradeLeg).filter(
                ManualTradeLeg.id == target_id,
                ManualTradeLeg.trade_id == trade_id).first()
        else:
            target = db.query(ManualTradeCostEvent).filter(
                ManualTradeCostEvent.id == target_id,
                ManualTradeCostEvent.trade_id == trade_id).first()
        if target is None:
            raise ManualTradeNotFound(
                f"no {target_kind} {target_id!r} on trade {trade_id!r}")

        previous = getattr(target, field)
        setattr(target, field, new_value)
        db.flush()

        # Re-validate the WHOLE trade against the corrected value. A
        # correction that produces an impossible book is refused, and the
        # transaction unwinds with the original intact.
        row2, legs2, events2 = _load(db, trade_id)
        trade = _hydrate(row2, legs2, events2)
        trade._walk()
        trade.costs_usd()

        db.add(ManualTradeCorrection(
            id=new_id(), trade_id=trade_id, target_kind=target_kind,
            target_id=target_id, field=field,
            previous_value_json=_json(previous),
            new_value_json=_json(new_value), reason=reason,
            corrected_by=corrected_by, evidence_type=evidence_type,
            evidence_source=evidence_source, corrected_at=now_iso()))

        row.revision = int(row.revision or 0) + 1
        row.updated_at = now_iso()
        out = {"trade_id": trade_id, "target_kind": target_kind,
               "target_id": target_id, "field": field,
               "previous_value": previous, "new_value": new_value,
               "revision": row.revision}
        if row.state == mx.CLOSED:
            out["realized_outcome"] = _rederive(row, trade)
        return out


def corrections(trade_id: str) -> list:
    """The amendment history. What it used to say, and who changed it."""
    from app.database import ManualTradeCorrection, get_db

    with get_db() as db:
        rows = db.query(ManualTradeCorrection).filter(
            ManualTradeCorrection.trade_id == trade_id).order_by(
            ManualTradeCorrection.corrected_at).all()
        return [{
            "id": r.id, "target_kind": r.target_kind,
            "target_id": r.target_id, "field": r.field,
            "previous_value": _unjson(r.previous_value_json),
            "new_value": _unjson(r.new_value_json),
            "reason": r.reason, "corrected_by": r.corrected_by,
            "evidence_type": r.evidence_type,
            "evidence_source": r.evidence_source,
            "corrected_at": r.corrected_at,
        } for r in rows]


def realized_outcome(trade_id: str) -> dict:
    """The canonical outcome of a closed trade, as persisted."""
    from app.database import get_db

    with get_db() as db:
        row, legs, events = _load(db, trade_id)
        if row.state != mx.CLOSED:
            raise mx.IncompleteManualTrade(
                f"trade {trade_id} is {row.state}; only a CLOSED trade has "
                f"a realized outcome")
        stored = _unjson(row.realized_outcome_json)
        return {"outcome": stored, "learning_state": row.learning_state,
                "learning_error": row.learning_error}


def arm_result(trade_id: str):
    """This trade as an OPERATOR arm on its thesis, or None if unlinked."""
    return mx.arm_result(get(trade_id))
