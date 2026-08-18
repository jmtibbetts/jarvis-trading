"""The market-facing canonical exit — B2B. It prepares; B2A settles.

WHAT THIS MODULE IS NOT. It is not an accountant: it never mutates
PaperPosition, cash, legs, outcomes, trades or counters — every economic
mutation belongs to `canonical_settlement.settle_prepared_exit()`, which
revalidates everything inside its own short transaction. And it is not a
resolver: the position already knows what it is. Identity comes from the B1
settlement header, frozen at entry, and today's router/config/env are never
asked.

THE CHAIN, in order, with the session CLOSED before any provider work:

    immutable exit snapshot (position + header, one short read session)
        -> frozen RoutingIdentity, provenance PERSISTED_CANONICAL_ENTRY
        -> execution_readiness with the frozen identity
        -> exact execution instrument (resolve_for_execution, no bare resolve)
        -> reduction authorization (normalize DOWN; whole contracts)
        -> REDUCE RiskDecision + reduce-only OrderPlan
        -> ExecutionVenue.submit (never virtual_orders directly)
        -> execution agreement (the shared B0 definition)
        -> exact exit FeeQuote at the ACTUAL fill and EXECUTION side
        -> exact HoldingCostQuote for THIS leg's exposure and interval
        -> ExitSettlementFacts
        -> settle_prepared_exit()

PERMANENT INVARIANTS:

    MARK/TRIGGER AUTHORITY IS NOT FILL AUTHORITY. `trigger_price` and
    `decision_price` are references; the fill comes only from the venue
    book. A long exits by SELLING into the BID; a short by BUYING the ASK.

    NO FALLBACK ON EXIT. A stale, halted, desynced, closed, one-sided or
    drifted book leaves the position OPEN with a named refusal. Exit
    pending is better than fictional money, and a venue refusal is never
    taught to the strategy as a loss.

    A STALE REVISION IS NOT AUTO-RETRIED. The fill was prepared for
    revision N; revision N+1 is a different economic state, and a new
    attempt means a new snapshot, order, execution, fee, carry and
    execution id.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from lib.realized_outcome import VOLUNTARY_EXIT  # noqa: E402

# ── Refusal vocabulary (§36). Named, never "could not close". ────────────
NOT_CANONICAL_POSITION = "NOT_CANONICAL_POSITION"
EXIT_MARKET_DATA_UNAVAILABLE = "EXIT_MARKET_DATA_UNAVAILABLE"
EXIT_EXACT_INSTRUMENT_UNAVAILABLE = "EXIT_EXACT_INSTRUMENT_UNAVAILABLE"
EXIT_INVALID_QUANTITY = "EXIT_INVALID_QUANTITY"
EXIT_REDUCTION_RISK_REFUSED = "EXIT_REDUCTION_RISK_REFUSED"
EXIT_EXECUTION_REFUSED = "EXIT_EXECUTION_REFUSED"
EXIT_EXECUTION_CONTRADICTION = "EXIT_EXECUTION_CONTRADICTION"
EXIT_FEE_UNAVAILABLE = "EXIT_FEE_UNAVAILABLE"
EXIT_HOLDING_COST_UNAVAILABLE = "EXIT_HOLDING_COST_UNAVAILABLE"

# How the frozen identity was established — persisted at entry, not chosen
# by today's configuration.
PERSISTED_CANONICAL_ENTRY = "PERSISTED_CANONICAL_ENTRY"


@dataclass(frozen=True)
class CanonicalExitSnapshot:
    """The immutable facts an exit is prepared against. Read in one short
    session, then the session closes — B2A reloads and revision-checks."""
    position_id: str
    remaining_qty: float
    remaining_margin: float
    leverage: float
    stop_loss: float | None
    symbol: str
    asset_class: str | None
    product: str
    venue: str
    instrument_id: str
    position_side: str
    quantity_unit: str
    multiplier: float
    original_quantity: float
    actual_entry_fill: float
    opened_at: str
    settlement_revision: int


def _refuse(error: str, detail: str, **extra) -> dict:
    out = {"ok": False, "error": error, "detail": detail}
    out.update(extra)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_exit_snapshot(position_id: str) -> CanonicalExitSnapshot | dict:
    """One short read session; identity from the B1 HEADER, quantity from
    the position row. PaperPosition provenance is not the authority — the
    header is the frozen settlement identity."""
    from app.database import (PaperPosition, PaperPositionSettlement, get_db)
    from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
    from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                      EXECUTION_MODEL_CANONICAL,
                                      SETTLEMENT_VERSION)

    with get_db() as db:
        pos = db.query(PaperPosition).filter(
            PaperPosition.id == position_id).first()
        header = db.query(PaperPositionSettlement).filter(
            PaperPositionSettlement.position_id == position_id).first()
        if pos is None or header is None:
            return _refuse(NOT_CANONICAL_POSITION,
                           f"position {position_id} has no canonical "
                           f"settlement ledger; the legacy close paths own "
                           f"legacy positions")
        if pos.status != "Open" or header.status != "OPEN":
            return _refuse(NOT_CANONICAL_POSITION,
                           f"position is {pos.status!r} / header "
                           f"{header.status!r}; only an OPEN canonical "
                           f"position can be exited")
        if (header.settlement_version != SETTLEMENT_VERSION
                or header.cost_model != COST_MODEL_CANONICAL
                or header.execution_model != EXECUTION_MODEL_CANONICAL
                or header.engine_epoch != CANONICAL_ENGINE_EPOCH):
            return _refuse(NOT_CANONICAL_POSITION,
                           f"header models are not the current canonical "
                           f"set; a hybrid position gets no canonical exit")
        snap = CanonicalExitSnapshot(
            position_id=position_id,
            remaining_qty=float(pos.qty),
            remaining_margin=float(pos.margin_used or 0.0),
            leverage=float(pos.leverage or 1.0),
            stop_loss=float(pos.stop_loss) if pos.stop_loss else None,
            symbol=header.symbol, asset_class=header.asset_class,
            product=header.product, venue=header.venue,
            instrument_id=header.instrument_id,
            position_side=header.position_side,
            quantity_unit=header.quantity_unit,
            multiplier=float(header.multiplier),
            original_quantity=float(header.original_quantity),
            actual_entry_fill=float(header.actual_entry_fill),
            opened_at=str(header.opened_at),
            settlement_revision=int(header.settlement_revision),
        )
    return snap


def close_canonical_position(position_id: str, *,
                             requested_qty: float | None = None,
                             close_fraction: float | None = None,
                             exit_reason: str = VOLUNTARY_EXIT,
                             trigger_price: float | None = None,
                             decision_price: float | None = None,
                             max_age_s: float | None = None) -> dict:
    """Close (some of) one canonical position against its own frozen book.

    NOT wired to the ten production callers — direct invocation only, until
    the learning projection exists and dispatch is deliberately routed.
    """
    from lib import canonical_settlement as CS
    from lib import execution_policy as POL
    from lib import execution_venue as EV
    from lib import fee_authority as FA
    from lib import holding_cost_authority as HCA
    from lib import instruments as INST
    from lib import virtual_orders as VO
    from lib.canonical_entry import _execution_id as _execution_event_id
    from lib.decision_types import OrderPlan, RiskDecision
    from lib.execution_consistency import execution_disagreement
    from lib.routing_identity import RESOLVED, RoutingIdentity

    if requested_qty is not None and close_fraction is not None:
        return _refuse(EXIT_INVALID_QUANTITY,
                       "requested_qty and close_fraction are two different "
                       "instructions; refusing to guess which one governs")

    # ── 1. Immutable snapshot, then the session is CLOSED ────────────────
    snap = read_exit_snapshot(position_id)
    if isinstance(snap, dict):
        return snap

    # ── 2. The frozen identity, from persisted facts — not today's config ─
    identity = RoutingIdentity(
        symbol=snap.symbol, asset_class=snap.asset_class,
        product=snap.product, venue=snap.venue,
        instrument_id=snap.instrument_id,
        identity_status=RESOLVED,
        product_identity_source=PERSISTED_CANONICAL_ENTRY,
        provenance={"source": PERSISTED_CANONICAL_ENTRY,
                    "position_id": snap.position_id})

    ready = POL.execution_readiness(
        snap.symbol, snap.asset_class, routing_identity=identity,
        **({} if max_age_s is None else {"max_age_s": max_age_s}))
    if not ready.ok:
        return _refuse(EXIT_MARKET_DATA_UNAVAILABLE,
                       f"{ready.reason}: {ready.detail}",
                       reason=ready.reason,
                       venue_failure=POL.is_venue_data_failure(ready.reason))
    if (ready.venue != snap.venue or ready.product != snap.product
            or (ready.instrument and ready.instrument != snap.instrument_id)):
        return _refuse(EXIT_MARKET_DATA_UNAVAILABLE,
                       f"readiness answered for "
                       f"{ready.venue}/{ready.product}/{ready.instrument!r} "
                       f"but the frozen identity is "
                       f"{snap.venue}/{snap.product}/{snap.instrument_id!r}")
    from lib import product_router as _PR
    if (snap.product == _PR.CRYPTO_PERP and ready.snapshot.instrument_id
            and ready.snapshot.instrument_id != snap.instrument_id):
        return _refuse(EXIT_MARKET_DATA_UNAVAILABLE,
                       f"the book confirmed "
                       f"{ready.snapshot.instrument_id!r}, not the frozen "
                       f"{snap.instrument_id!r}")

    # ── 3. The exact execution instrument. No bare resolve(). ────────────
    try:
        instrument = INST.resolve_for_execution(
            snap.symbol, product=snap.product, venue=snap.venue,
            instrument_id=snap.instrument_id)
        instrument.require_executable()
    except Exception as e:
        return _refuse(EXIT_EXACT_INSTRUMENT_UNAVAILABLE, str(e))
    if (instrument.quantity_unit != snap.quantity_unit
            or abs(float(instrument.multiplier) - snap.multiplier)
            > 1e-12 * max(1.0, snap.multiplier)):
        return _refuse(EXIT_EXACT_INSTRUMENT_UNAVAILABLE,
                       f"the executable spec now says "
                       f"{instrument.quantity_unit!r} x "
                       f"{instrument.multiplier!r} but the position was "
                       f"settled in {snap.quantity_unit!r} x "
                       f"{snap.multiplier!r} — refusing to substitute")

    # ── 4. Reduction quantity: normalize DOWN, never up ──────────────────
    remaining = snap.remaining_qty
    if close_fraction is not None:
        if not (0.0 < float(close_fraction) <= 1.0):
            return _refuse(EXIT_INVALID_QUANTITY,
                           f"close_fraction {close_fraction!r} is not in "
                           f"(0, 1]")
        theoretical = remaining * float(close_fraction)
    elif requested_qty is not None:
        theoretical = float(requested_qty)
        if theoretical > remaining + 1e-9 * max(1.0, remaining):
            theoretical = remaining
    else:
        theoretical = remaining

    full_close_intent = (requested_qty is None and close_fraction is None) \
        or abs(theoretical - remaining) <= 1e-9 * max(1.0, remaining)
    try:
        authorized = INST.normalize_quantity_down(theoretical, instrument)
    except Exception as e:
        return _refuse(EXIT_INVALID_QUANTITY, str(e))
    minimum = instrument.minimum_quantity
    if authorized <= 0 or (minimum is not None
                           and authorized < float(minimum)):
        return _refuse(EXIT_INVALID_QUANTITY,
                       f"{theoretical:g} {snap.quantity_unit} reduces to "
                       f"{authorized:g} executable — below the venue "
                       f"minimum; nothing can be closed at this size")
    if full_close_intent and abs(authorized - remaining) > 1e-9 * max(
            1.0, remaining):
        # A full close that cannot close the full quantity is not a partial
        # request — it is corrupted position state (2.4 CONTRACTS remaining
        # on an indivisible contract). Fail closed; do not leave 0.4 forever.
        return _refuse(EXIT_INVALID_QUANTITY,
                       f"full close of {remaining:g} {snap.quantity_unit} "
                       f"normalizes to {authorized:g} — the remaining "
                       f"quantity itself is not executable, which is "
                       f"corrupted state, not a partial exit")

    # ── 5. REDUCE authorization and reduce-only plan ─────────────────────
    mid = (float(ready.snapshot.bid) + float(ready.snapshot.ask)) / 2.0
    exit_side = "short" if snap.position_side == "long" else "long"
    reduce_risk = RiskDecision(
        allowed_risk_usd=0.0, stop_distance=0.0,
        qty=authorized, notional=authorized * mid * snap.multiplier,
        margin=0.0, leverage=snap.leverage,
        quantity_unit=snap.quantity_unit, multiplier=snap.multiplier,
        intent="REDUCE", position_id=snap.position_id,
        position_side=snap.position_side)
    plan = OrderPlan(
        symbol=snap.symbol, venue=snap.venue, side=exit_side,
        order_type="market", qty=authorized, entry=mid,
        initial_stop=float(snap.stop_loss or 0.0),
        notional=authorized * mid * snap.multiplier,
        leverage=snap.leverage, product=snap.product,
        instrument_id=snap.instrument_id,
        quantity_unit=snap.quantity_unit, multiplier=snap.multiplier,
        intent="REDUCE", reduce_only=True,
        position_id=snap.position_id, position_side=snap.position_side)

    quote = VO.Quote(bid=float(ready.snapshot.bid),
                     ask=float(ready.snapshot.ask),
                     as_of=ready.snapshot.venue_event_at,
                     source=ready.snapshot.source)

    # ── 6. Execution through the boundary — never around it ─────────────
    submission = EV.submit(plan, venue_family=EV.VIRTUAL_CEX,
                           risk=reduce_risk, product=snap.product,
                           venue=snap.venue, quote=quote,
                           instrument=instrument)
    if not submission.accepted:
        error = (EXIT_REDUCTION_RISK_REFUSED
                 if submission.reason == EV.REFUSED_RISK
                 else EXIT_EXECUTION_REFUSED)
        return _refuse(error,
                       f"{submission.reason}: {submission.detail}",
                       reason=submission.reason)
    execution = submission.execution
    if execution is None or not execution.filled or not execution.fill_price:
        return _refuse(EXIT_EXECUTION_REFUSED,
                       f"accepted submission produced no usable execution "
                       f"({getattr(execution, 'state', None)!r})")

    contradiction = execution_disagreement(plan, execution)
    if contradiction:
        return _refuse(EXIT_EXECUTION_CONTRADICTION, contradiction)

    filled = float(execution.filled_quantity)
    fill = float(execution.fill_price)

    # ── 7. Exact exit fee, at the ACTUAL fill and EXECUTION side ─────────
    executed_notional = filled * fill * float(execution.multiplier or 1.0)
    in_contracts = execution.quantity_unit == "CONTRACTS"
    fee_quote = FA.leg_fee(
        snap.symbol, notional=executed_notional, price=fill,
        product=snap.product, venue=snap.venue,
        side=exit_side,                      # execution side, NOT position
        maker=False,
        exact_contract_count=filled if in_contracts else None,
        execution_instrument=instrument, actual_fill_price=fill)
    if not fee_quote.ok or fee_quote.fee_usd is None:
        return _refuse(EXIT_FEE_UNAVAILABLE,
                       f"{fee_quote.reason}: {fee_quote.detail}")

    # ── 8. One settlement timestamp; one explicit holding interval ───────
    settled_at = _now_iso()
    try:
        t0 = datetime.fromisoformat(snap.opened_at)
        t1 = datetime.fromisoformat(settled_at)
        hours = (t1 - t0).total_seconds() / 3600.0
    except (TypeError, ValueError) as e:
        return _refuse(EXIT_HOLDING_COST_UNAVAILABLE,
                       f"cannot establish the holding interval: {e}")
    if hours < 0:
        return _refuse(EXIT_HOLDING_COST_UNAVAILABLE,
                       f"negative holding interval ({hours:.6f}h) — the "
                       f"timestamps cannot both be right")

    # THE ESTABLISHED CARRY MODEL: this leg's quantity, at the ENTRY fill's
    # notional, over entry -> this exit. Changing it is a new version with
    # new tests — not a silent substitution here.
    holding_notional = filled * snap.actual_entry_fill * snap.multiplier
    holding_quote = HCA.holding_cost(
        snap.symbol, product=snap.product, notional_usd=holding_notional,
        hours_held=hours, is_short=(snap.position_side == "short"))
    if not holding_quote.ok or holding_quote.amount_usd is None:
        return _refuse(EXIT_HOLDING_COST_UNAVAILABLE,
                       f"{holding_quote.reason}: {holding_quote.detail}")

    # ── 9. One execution event, one id — minted the same way entry's is ──
    exit_execution_id = _execution_event_id()

    try:
        facts = CS.exit_facts(
            position_id=snap.position_id,
            expected_revision=snap.settlement_revision,
            execution_id=exit_execution_id,
            symbol=snap.symbol, product=snap.product, venue=snap.venue,
            instrument_id=snap.instrument_id,
            position_side=snap.position_side,
            execution_side=("sell" if snap.position_side == "long"
                            else "buy"),
            requested_qty=float(execution.requested_quantity),
            filled_qty=filled,
            quantity_unit=snap.quantity_unit, multiplier=snap.multiplier,
            fill_price=fill, fee_quote=fee_quote,
            holding_quote=holding_quote, settled_at=settled_at,
            exit_reason=exit_reason, trigger_price=trigger_price,
            decision_exit_price=decision_price,
            spread_attribution_usd=float(execution.spread_cost_usd or 0.0),
            slippage_attribution_usd=float(execution.slippage_usd or 0.0),
            impact_attribution_usd=0.0,   # the fill model does not measure it
            fill_model=execution.fill_model,
            provenance={
                "exit_execution_id": exit_execution_id,
                "market_source": ready.snapshot.source,
                "snapshot_instrument_id": ready.snapshot.instrument_id,
                "bid_at_submit": ready.snapshot.bid,
                "ask_at_submit": ready.snapshot.ask,
                "venue_event_at": str(ready.snapshot.venue_event_at),
                "price_model": execution.price_model,
                "identity_source": PERSISTED_CANONICAL_ENTRY,
                "theoretical_qty": theoretical,
                "authorized_qty": authorized,
            })
    except CS.ExitValidationError as e:
        return _refuse(CS.EXIT_VALIDATION_FAILED, str(e))

    # ── 10. B2A is the only economic mutation ────────────────────────────
    result = CS.settle_prepared_exit(facts)
    if result.get("ok"):
        result.setdefault("execution_id", exit_execution_id)
        result["fill_price"] = fill
        result["filled_qty"] = filled
        return result
    if result.get("error") == CS.STALE_SETTLEMENT_REVISION:
        # The fill belonged to revision N; the ledger moved. Preparing again
        # is a NEW authorization — new snapshot, order, execution, fee,
        # carry, id — and it is the CALLER's decision, never an auto-retry
        # that reuses this fill against a state it was not prepared for.
        result["reprepare_required"] = True
    return result
