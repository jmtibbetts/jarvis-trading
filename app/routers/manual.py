"""Manual Trade Desk routes — trades the OPERATOR placed, recorded as evidence.

THE SMALLEST SURFACE THAT SUPPORTS THE WORKFLOW, and nothing that implies
JARVIS can execute. There is no submit route here and there will not be one:
these venues are reachable by a human and not by this program, which is a
fact about the venue rather than a gap to be filled.

Every response reports `execution_mode` and `submitted_by_jarvis: false`,
because a client that has to infer who placed an order will eventually infer
it wrong.

VALIDATION LIVES IN THE DOMAIN, NOT HERE. Pydantic checks shapes; the
economics, units, lifecycle and cost semantics are checked by
`lib/manual_execution.py` and surfaced as 400s. Duplicating those rules at
the edge is how the two copies drift and the weaker one wins.
"""
from fastapi import APIRouter, Body, HTTPException

from app.routers.common import *  # noqa: F401,F403

router = APIRouter()


def _domain_error(exc: Exception) -> HTTPException:
    """Refusals are 400s WITH THE REASON. A manual desk that says only
    'invalid' forces the operator to guess which of fifteen fields it
    meant, and guessing is what put the bad value there."""
    return HTTPException(400, str(exc))


# ── Taxonomy ─────────────────────────────────────────────────────────────
@router.get("/manual/execution-modes")
def manual_execution_modes():
    """The canonical execution-mode taxonomy, including the reserved one.

    LIVE_AUTONOMOUS appears with `executable_today: false`. It is listed so
    the vocabulary is honest about what exists, and refused everywhere it
    could be acted on.
    """
    from lib import execution_mode

    return execution_mode.as_dict()


@router.get("/manual/vocabulary")
def manual_vocabulary():
    """Everything a client must choose from, so it never invents a value."""
    from lib import manual_execution as mx
    from lib.account_economics import (CAPITAL_KINDS, COST_CATEGORIES,
                                       ENTITLEMENT_KINDS)
    from lib.venue_reconciliation import EVIDENCE_RANK

    return {
        "states": list(mx.STATES),
        "leg_kinds": sorted(mx.LEG_KINDS),
        "cost_event_kinds": sorted(mx.COST_EVENT_KINDS),
        "products": sorted(mx.PRODUCTS),
        "quantity_units": sorted(mx.QUANTITY_UNITS),
        "margin_modes": sorted(mx.MARGIN_MODES),
        "liquidity_roles": sorted(mx.LIQUIDITY_ROLES),
        "capital_kinds": list(CAPITAL_KINDS),
        "cost_categories": list(COST_CATEGORIES),
        "entitlement_kinds": list(ENTITLEMENT_KINDS),
        # Provenance ranks are `lib/venue_reconciliation.py`'s, strongest
        # first — not a second vocabulary invented for this desk.
        "evidence_types": list(EVIDENCE_RANK),
        "pnl_basis": mx.PNL_BASIS,
        "version": mx.MANUAL_EXECUTION_VERSION,
        "note": ("options are absent because the instrument taxonomy "
                 "carries no option contract identity; recording one as a "
                 "near-miss product would price it with the wrong economics"),
    }


# ── Read ─────────────────────────────────────────────────────────────────
@router.get("/manual/trades")
def manual_trades(state: str = None, venue: str = None,
                  account_label: str = None, thesis_id: str = None,
                  linked: bool = None, limit: int = 100):
    """Manual trades, newest first.

    `linked=false` selects trades the operator took INDEPENDENTLY of any
    JARVIS thesis. Those are first-class records, not incomplete ones.
    """
    from lib import manual_trade_store as store

    rows = store.list_trades(state=state, venue=venue,
                             account_label=account_label,
                             thesis_id=thesis_id, linked=linked, limit=limit)
    return {"trades": rows, "count": len(rows),
            "execution_mode": "MANUAL_OPERATOR",
            "submitted_by_jarvis": False}


@router.get("/manual/trades/{trade_id}")
def manual_trade(trade_id: str):
    from lib import manual_trade_store as store

    try:
        return store.get_record(trade_id)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))


@router.get("/manual/trades/{trade_id}/outcome")
def manual_trade_outcome(trade_id: str):
    """The canonical RealizedOutcome, and whether it may teach anything."""
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    try:
        return store.realized_outcome(trade_id)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    except mx.IncompleteManualTrade as e:
        raise _domain_error(e)


@router.get("/manual/trades/{trade_id}/corrections")
def manual_trade_corrections(trade_id: str):
    from lib import manual_trade_store as store

    try:
        store.get(trade_id)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    return {"corrections": store.corrections(trade_id)}


# ── Write ────────────────────────────────────────────────────────────────
class ManualRecommendation(BaseModel):
    """The JARVIS side, as it stood WHEN IT WAS MADE.

    Supplied once, at creation, and never accepted again — there is no
    route that updates it. If the operator entered later at a worse price,
    both numbers survive; that gap is the measurement.
    """
    thesis_id: Optional[str] = None
    signal_id: Optional[str] = None
    decision_id: Optional[str] = None
    recommended_at: Optional[str] = None
    direction: Optional[str] = None
    venue: Optional[str] = None
    product: Optional[str] = None
    symbol: Optional[str] = None
    entry: Optional[float] = None
    stop: Optional[float] = None
    targets: list = []
    leverage: Optional[float] = None
    expected_fee_usd: Optional[float] = None
    expected_funding_usd: Optional[float] = None
    expected_cost_usd: Optional[float] = None
    expected_r: Optional[float] = None
    confidence: Optional[float] = None
    evidence_ref: dict = {}


class ManualTradeCreate(BaseModel):
    venue: str
    product: str
    symbol: str
    direction: str
    quantity_unit: str
    account_label: str = "default"
    multiplier: float = 1.0
    instrument_id: Optional[str] = None
    leverage: Optional[float] = None
    margin_mode: str = "UNKNOWN"
    collateral_usd: Optional[float] = None
    collateral_capital_kind: str = "CAPITAL_UNKNOWN"
    stop_used: Optional[float] = None
    targets_used: list = []
    initial_risk_usd: Optional[float] = None
    declared_absent_costs: list = []
    operator_reported_realized_pnl_usd: Optional[float] = None
    operator_reported_evidence_type: Optional[str] = None
    evidence_type: Optional[str] = None
    evidence_source: Optional[str] = None
    notes: str = ""
    opened_at: Optional[str] = None
    # OPTIONAL BY DESIGN. Absent means the operator traded independently,
    # and no thesis is fabricated to make the record look complete.
    recommendation: Optional[ManualRecommendation] = None


@router.post("/manual/trades")
def manual_trade_create(body: ManualTradeCreate):
    """Record a manual trade in DRAFT. No legs yet, so no economics yet."""
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    rec = None
    if body.recommendation is not None:
        r = body.recommendation
        rec = mx.RecommendationSnapshot(
            thesis_id=r.thesis_id, signal_id=r.signal_id,
            decision_id=r.decision_id, recommended_at=r.recommended_at,
            direction=r.direction, venue=r.venue, product=r.product,
            symbol=r.symbol, entry=r.entry, stop=r.stop,
            targets=tuple(r.targets or ()), leverage=r.leverage,
            expected_fee_usd=r.expected_fee_usd,
            expected_funding_usd=r.expected_funding_usd,
            expected_cost_usd=r.expected_cost_usd, expected_r=r.expected_r,
            confidence=r.confidence, evidence_ref=dict(r.evidence_ref or {}))
    try:
        trade_id = store.create(
            venue=body.venue, product=body.product, symbol=body.symbol,
            direction=body.direction, quantity_unit=body.quantity_unit,
            account_label=body.account_label, multiplier=body.multiplier,
            instrument_id=body.instrument_id, leverage=body.leverage,
            margin_mode=body.margin_mode,
            collateral_usd=body.collateral_usd,
            collateral_capital_kind=body.collateral_capital_kind,
            stop_used=body.stop_used,
            targets_used=tuple(body.targets_used or ()),
            initial_risk_usd=body.initial_risk_usd, recommendation=rec,
            declared_absent_costs=tuple(body.declared_absent_costs or ()),
            operator_reported_realized_pnl_usd=(
                body.operator_reported_realized_pnl_usd),
            operator_reported_evidence_type=(
                body.operator_reported_evidence_type),
            evidence_type=body.evidence_type,
            evidence_source=body.evidence_source, notes=body.notes,
            opened_at=body.opened_at)
    except mx.ManualExecutionError as e:
        raise _domain_error(e)
    return store.get_record(trade_id)


class ManualLegAppend(BaseModel):
    kind: str
    quantity: float
    fill_price: float
    at: str
    # NULLABLE ON PURPOSE. An unevidenced fee is None and makes net P&L
    # UNKNOWN; a zero would claim the venue charged nothing.
    fee_usd: Optional[float] = None
    liquidity_role: str = "UNKNOWN"
    decision_price: Optional[float] = None
    venue_order_ref: Optional[str] = None
    exit_reason: Optional[str] = None
    evidence_type: Optional[str] = None
    evidence_source: Optional[str] = None
    notes: str = ""


@router.post("/manual/trades/{trade_id}/legs")
def manual_trade_append_leg(trade_id: str, body: ManualLegAppend):
    """Append one fill: an entry, a scale-in, a partial exit, a final exit.

    A FINAL_EXIT that flattens the position also produces the canonical
    RealizedOutcome, in the same transaction.
    """
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    try:
        return store.append_leg(
            trade_id, kind=body.kind, quantity=body.quantity,
            fill_price=body.fill_price, at=body.at, fee_usd=body.fee_usd,
            liquidity_role=body.liquidity_role,
            decision_price=body.decision_price,
            venue_order_ref=body.venue_order_ref,
            exit_reason=body.exit_reason, evidence_type=body.evidence_type,
            evidence_source=body.evidence_source, notes=body.notes)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    except mx.ManualExecutionError as e:
        raise _domain_error(e)


class ManualCostEventAppend(BaseModel):
    kind: str
    # A MAGNITUDE. Direction is the kind's job — FUNDING_PAID and
    # FUNDING_RECEIVED are different events, not one with a sign.
    amount_usd: float
    at: str
    evidence_type: Optional[str] = None
    evidence_source: Optional[str] = None
    notes: str = ""


@router.post("/manual/trades/{trade_id}/cost-events")
def manual_trade_append_cost_event(trade_id: str,
                                   body: ManualCostEventAppend):
    """Append funding, a rebate, gas or a penalty — including after close."""
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    try:
        return store.append_cost_event(
            trade_id, kind=body.kind, amount_usd=body.amount_usd,
            at=body.at, evidence_type=body.evidence_type,
            evidence_source=body.evidence_source, notes=body.notes)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    except mx.ManualExecutionError as e:
        raise _domain_error(e)


class ManualTerminate(BaseModel):
    state: str          # CANCELLED | ABANDONED
    reason: str


@router.post("/manual/trades/{trade_id}/terminate")
def manual_trade_terminate(trade_id: str, body: ManualTerminate):
    """End a trade that produced no realized result.

    CANCELLED means it never executed. ABANDONED means it opened and its
    ending was never evidenced. Neither is a loss and neither votes.
    """
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    try:
        return store.terminate(trade_id, state=body.state,
                               reason=body.reason)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    except mx.ManualExecutionError as e:
        raise _domain_error(e)


class ManualCorrection(BaseModel):
    target_kind: str          # TRADE | LEG | COST_EVENT
    target_id: str
    field: str
    new_value: Union[float, str, None] = None
    reason: str
    corrected_by: str
    evidence_type: Optional[str] = None
    evidence_source: Optional[str] = None


@router.post("/manual/trades/{trade_id}/corrections")
def manual_trade_correct(trade_id: str, body: ManualCorrection):
    """Amend a recorded fact, keeping what it used to say.

    The recommendation is deliberately NOT correctable — see
    `lib/manual_trade_store.CORRECTABLE_TRADE_FIELDS`.
    """
    from lib import manual_execution as mx
    from lib import manual_trade_store as store

    try:
        return store.correct(
            trade_id, target_kind=body.target_kind,
            target_id=body.target_id, field=body.field,
            new_value=body.new_value, reason=body.reason,
            corrected_by=body.corrected_by,
            evidence_type=body.evidence_type,
            evidence_source=body.evidence_source)
    except store.ManualTradeNotFound as e:
        raise HTTPException(404, str(e))
    except mx.ManualExecutionError as e:
        raise _domain_error(e)
