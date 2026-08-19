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

from lib.realized_outcome import (FORCED_LIQUIDATION,  # noqa: E402
                                  MARGIN_CALL, STOP_EXIT, TARGET_EXIT,
                                  VOLUNTARY_EXIT)

# ── Refusal vocabulary (§36). Named, never "could not close". ────────────
NOT_CANONICAL_POSITION = "NOT_CANONICAL_POSITION"
# An automated PRICE-THRESHOLD claim that the contract's own executable
# book does not support. Not a failure — the trigger simply is not true.
EXIT_TRIGGER_NOT_CONFIRMED = "EXIT_TRIGGER_NOT_CONFIRMED"

# Exits whose justification IS a price threshold, and which therefore must
# be re-confirmed against the exact book. Everything else — a manual close,
# an AI decision, an administrative reset — is an INSTRUCTION to exit, not
# a claim about price, and is never second-guessed here.
AUTOMATED_TRIGGER_REASONS = frozenset({STOP_EXIT, TARGET_EXIT, MARGIN_CALL,
                                       FORCED_LIQUIDATION})
EXIT_MARKET_DATA_UNAVAILABLE = "EXIT_MARKET_DATA_UNAVAILABLE"
EXIT_EXACT_INSTRUMENT_UNAVAILABLE = "EXIT_EXACT_INSTRUMENT_UNAVAILABLE"
EXIT_INVALID_QUANTITY = "EXIT_INVALID_QUANTITY"
EXIT_REDUCTION_RISK_REFUSED = "EXIT_REDUCTION_RISK_REFUSED"
EXIT_EXECUTION_REFUSED = "EXIT_EXECUTION_REFUSED"
EXIT_EXECUTION_CONTRADICTION = "EXIT_EXECUTION_CONTRADICTION"
EXIT_FEE_UNAVAILABLE = "EXIT_FEE_UNAVAILABLE"
EXIT_FEE_SCHEDULE_CHANGED = "EXIT_FEE_SCHEDULE_CHANGED"
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
                           "header models are not the current canonical "
                           "set; a hybrid position gets no canonical exit")
        # P0 (B2C): never prepare an order from a projection that disagrees
        # with its ledger. Settlement re-validates independently — two
        # boundaries, neither trusting the other.
        from lib.settlement_ledger import validate_position_projection
        drift = validate_position_projection(db, pos, header)
        if drift:
            return _refuse("CANONICAL_POSITION_PROJECTION_MISMATCH", drift)
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


def confirm_exit_trigger(snap: CanonicalExitSnapshot, ready, exit_reason: str,
                         *, trigger_price: float | None,
                         caller_price: float | None) -> dict | None:
    """Is this automated price trigger still true on the contract's OWN book?

    A generic cross-market mark is EVIDENCE that something may have
    happened. It is not authority to liquidate a contract whose executable
    book has not reached the level — a spot print, another venue's tape, or
    a stale cache can all cross a threshold the perpetual never did.

    THE EXECUTABLE SIDE IS THE REFERENCE, because that is the side the
    position must actually leave through. A LONG exits by SELLING, so its
    stop and target are judged on the BID; a SHORT exits by BUYING and is
    judged on the ASK. A midpoint that touches the target while the
    executable side has not is not an executable target.

    Returns a refusal dict, or None when the trigger is confirmed.
    """
    if exit_reason not in AUTOMATED_TRIGGER_REASONS:
        return None                      # an instruction, not a claim

    bid = float(ready.snapshot.bid)
    ask = float(ready.snapshot.ask)
    long_side = snap.position_side == "long"
    reference = bid if long_side else ask

    def _no(detail: str) -> dict:
        return _refuse(
            EXIT_TRIGGER_NOT_CONFIRMED, detail,
            reason=exit_reason, threshold=trigger_price,
            caller_reference=caller_price, canonical_bid=bid,
            canonical_ask=ask, executable_reference=reference,
            position_id=snap.position_id)

    if exit_reason in (STOP_EXIT, TARGET_EXIT):
        if trigger_price is None:
            return _no(f"{exit_reason} names no threshold, so there is "
                       f"nothing to confirm against the book")
        threshold = float(trigger_price)
        if exit_reason == STOP_EXIT:
            fired = reference <= threshold if long_side else \
                reference >= threshold
        else:
            fired = reference >= threshold if long_side else \
                reference <= threshold
        if not fired:
            side_word = "bid" if long_side else "ask"
            return _no(
                f"{exit_reason} at {threshold:g} is not confirmed: the "
                f"executable {side_word} is {reference:g}. A caller "
                f"reference of {caller_price!r} is evidence, not the "
                f"contract's own book")
        return None

    # MARGIN_CALL / FORCED_LIQUIDATION — re-derive the condition from the
    # exact book, at the SAME threshold the legacy path uses. One constant,
    # imported, never a second literal.
    from lib.paper_engine import MARGIN_CALL_THRESHOLD
    sign = 1.0 if long_side else -1.0
    gross = ((reference - snap.actual_entry_fill) * sign
             * snap.remaining_qty * snap.multiplier)
    equity_in_position = snap.remaining_margin + gross
    if snap.remaining_margin > 0 and equity_in_position >= \
            snap.remaining_margin * MARGIN_CALL_THRESHOLD:
        return _no(
            f"{exit_reason} is not confirmed: at the executable "
            f"{reference:g} this position still holds "
            f"${equity_in_position:,.2f} of its ${snap.remaining_margin:,.2f} "
            f"margin, above the {MARGIN_CALL_THRESHOLD:.0%} floor")
    return None


def close_canonical_position(position_id: str, *,
                             requested_qty: float | None = None,
                             close_fraction: float | None = None,
                             exit_reason: str = VOLUNTARY_EXIT,
                             trigger_price: float | None = None,
                             decision_price: float | None = None,
                             max_age_s: float | None = None,
                             caller_source: str | None = None,
                             caller_reason: str | None = None,
                             request_metadata: dict | None = None) -> dict:
    """Close (some of) one canonical position against its own frozen book.

    NOT wired to the ten production callers — direct invocation only, until
    the learning projection exists and dispatch is deliberately routed.
    """
    from lib import canonical_settlement as CS
    from lib import execution_policy as POL
    from lib import execution_venue as EV
    from lib import execution_commitment as EC
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

    # ── 2b. An automated PRICE TRIGGER is re-confirmed on this contract's
    # own executable book, before any order exists. A caller's mark got us
    # here; it does not get to liquidate.
    unconfirmed = confirm_exit_trigger(snap, ready, exit_reason,
                                       trigger_price=trigger_price,
                                       caller_price=decision_price)
    if unconfirmed:
        return unconfirmed

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

    # ── 6b. THE COMMIT BOUNDARY. ────────────────────────────────────────
    # This fill has HAPPENED. Persist it before anything derives from it,
    # because everything below — the fee lookup, the carry lookup, the
    # settlement transaction — is a place the process can die, and until
    # this row exists such a death erases the execution entirely and lets
    # the next cycle re-decide against a different market. A stop that
    # triggered at 61,000 and vanished because Python crashed, leaving a
    # position that later closed at 70,000, has had its history rewritten
    # by an operating-system event.
    #
    # The id is minted HERE rather than at step 9, so the thing that is
    # committed and the thing that settles are the same identity.
    exit_execution_id = _execution_event_id()
    EC.record_commitment(
        execution_id=exit_execution_id,
        intent_kind=(EC.PARTIAL_EXIT if filled < remaining - 1e-12
                     else EC.EXIT),
        position_id=snap.position_id, symbol=snap.symbol,
        product=snap.product, venue=snap.venue,
        instrument_id=snap.instrument_id, side=plan.side,
        requested_qty=float(plan.qty), filled_qty=filled, fill_price=fill,
        quantity_unit=execution.quantity_unit,
        multiplier=float(execution.multiplier or 1.0),
        fill_model=getattr(execution, "fill_model", None) or "UNKNOWN",
        fill_model_version=getattr(execution, "fill_model_version", None)
        or VO.FILL_MODEL_VERSION,
        expected_revision=snap.settlement_revision,
        market_snapshot={"bid_at_submit": ready.snapshot.bid,
                         "ask_at_submit": ready.snapshot.ask,
                         "observed_at": getattr(ready.snapshot, "observed_at",
                                                None)},
        plan_facts={"qty": float(plan.qty), "side": plan.side,
                    "instrument_id": plan.instrument_id},
        fee_context=FA.pricing_context())

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

    # ── 9. The execution id was minted at the COMMIT BOUNDARY above, so
    #      the fill that was committed and the fill that settles are the
    #      same identity — settlement's idempotency key IS the commitment.

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
                # WHO ASKED, AND IN WHAT WORDS. Provenance only — these
                # never touch economics. The canonical reason is normalized
                # semantics; the caller's own spelling is kept beside it so
                # VOLUNTARY_EXIT/"telegram_manual" stays distinguishable
                # from VOLUNTARY_EXIT/"ai_exit".
                "caller_source": caller_source,
                "caller_reason": caller_reason,
                "request_metadata": request_metadata or None,
            })
    except CS.ExitValidationError as e:
        return _refuse(CS.EXIT_VALIDATION_FAILED, str(e))

    # ── 10. B2A is the only economic mutation ────────────────────────────
    result = CS.settle_prepared_exit(facts)
    # The committed fill is now durable economics. Mark it so recovery does
    # not try to settle it twice; a stale revision means another settlement
    # already won and this fill describes a state that no longer exists.
    if result.get("ok"):
        EC.mark_settled(exit_execution_id)
    elif result.get("error") == CS.STALE_SETTLEMENT_REVISION:
        EC.mark_abandoned(exit_execution_id,
                          reason=CS.STALE_SETTLEMENT_REVISION)
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


def settle_committed_exit(commitment: dict, snap) -> dict:
    """Settle a fill that was COMMITTED before the process died.

    THE ORIGINAL FILL, NOT THE CURRENT MARKET. Quantity, price, instrument
    and model all come from the persisted commitment. No venue is consulted
    and no trigger is re-evaluated: a stop that fired at 61,000 must not
    settle at 70,000 because a restart happened in between.

    Costs are still computed here rather than stored, but they are computed
    FROM the committed fill — the fee from its notional and side, the carry
    over entry -> now. Carry legitimately grows while the position remains
    open, because the position genuinely was open for that time; the FILL is
    what must not move.
    """
    from datetime import datetime

    from lib import canonical_settlement as CS
    from lib import execution_commitment as EC
    from lib import fee_authority as FA
    from lib import holding_cost_authority as HCA
    from lib import instruments as INST

    filled = float(commitment["filled_qty"])
    fill = float(commitment["fill_price"])
    multiplier = float(commitment.get("multiplier") or snap.multiplier or 1.0)
    exit_side = "sell" if snap.position_side == "long" else "buy"

    instrument = None
    try:
        instrument = INST.resolve_for_execution(
            snap.symbol, snap.asset_class, snap.product)
    except Exception:                                   # noqa: BLE001
        instrument = None

    # PRICE IT UNDER THE SCHEDULE THAT WAS IN FORCE AT THE FILL. The fill's
    # own facts are persisted and immutable, but the fee authority's version
    # and its region come from process state, and a restart can change both.
    # Recomputing under today's schedule would charge yesterday's execution
    # a price it never faced -- so if the schedule moved, this refuses
    # instead of inventing a number, and the commitment stays PENDING so the
    # owed settlement is neither lost nor guessed at.
    committed_ctx = commitment.get("fee_context") or {}
    current_ctx = FA.pricing_context()
    if committed_ctx and committed_ctx != current_ctx:
        return {"ok": False, "error": EXIT_FEE_SCHEDULE_CHANGED,
                "detail": f"the fill was committed under {committed_ctx} but "
                          f"this process prices under {current_ctx}; a "
                          f"recovered fill is not repriced"}

    executed_notional = filled * fill * multiplier
    in_contracts = commitment.get("quantity_unit") == "CONTRACTS"
    fee_quote = FA.leg_fee(
        snap.symbol, notional=executed_notional, price=fill,
        product=snap.product, venue=snap.venue, side=exit_side, maker=False,
        exact_contract_count=filled if in_contracts else None,
        execution_instrument=instrument, actual_fill_price=fill)
    if not fee_quote.ok or fee_quote.fee_usd is None:
        return {"ok": False, "error": EXIT_FEE_UNAVAILABLE,
                "detail": f"{fee_quote.reason}: {fee_quote.detail}"}

    # THE CARRY STOPS WHEN THE FILL HAPPENED, NOT WHEN THE PROCESS CAME
    # BACK. This quantity left the position at `committed_at`. If the stop
    # committed at 12:00:00 and the process was down until 12:45, those 45
    # minutes are an OPERATIONAL fact, not an economic one — charging carry
    # for them would let downtime create cost, and the same trade would cost
    # a different amount depending on how long a restart took.
    #
    # `settled_at` is therefore the committed instant too: it is the moment
    # the economics are dated to, and the recovery wall-clock appears only
    # in provenance.
    settled_at = commitment.get("committed_at") or _now_iso()
    try:
        t0 = datetime.fromisoformat(snap.opened_at)
        t1 = datetime.fromisoformat(settled_at)
        hours = (t1 - t0).total_seconds() / 3600.0
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": EXIT_HOLDING_COST_UNAVAILABLE,
                "detail": f"cannot establish the holding interval: {e}"}
    if hours < 0:
        return {"ok": False, "error": EXIT_HOLDING_COST_UNAVAILABLE,
                "detail": f"negative holding interval ({hours:.6f}h)"}

    holding_notional = filled * snap.actual_entry_fill * snap.multiplier
    holding_quote = HCA.holding_cost(
        snap.symbol, product=snap.product, notional_usd=holding_notional,
        hours_held=hours, is_short=(snap.position_side == "short"))
    if not holding_quote.ok or holding_quote.amount_usd is None:
        return {"ok": False, "error": EXIT_HOLDING_COST_UNAVAILABLE,
                "detail": f"{holding_quote.reason}: {holding_quote.detail}"}

    plan_facts = commitment.get("plan_facts") or {}
    try:
        facts = CS.exit_facts(
            position_id=snap.position_id,
            expected_revision=snap.settlement_revision,
            execution_id=commitment["execution_id"],
            symbol=snap.symbol, product=snap.product, venue=snap.venue,
            instrument_id=snap.instrument_id,
            position_side=snap.position_side, execution_side=exit_side,
            requested_qty=float(commitment.get("requested_qty") or filled),
            filled_qty=filled,
            quantity_unit=snap.quantity_unit, multiplier=snap.multiplier,
            fill_price=fill, fee_quote=fee_quote,
            holding_quote=holding_quote, settled_at=settled_at,
            exit_reason=(commitment.get("exit_reason")
                         or plan_facts.get("exit_reason")
                         or VOLUNTARY_EXIT),
            trigger_price=plan_facts.get("trigger_price"),
            decision_exit_price=plan_facts.get("decision_price"),
            spread_attribution_usd=0.0,
            slippage_attribution_usd=0.0,
            impact_attribution_usd=0.0,
            fill_model=commitment.get("fill_model") or "UNKNOWN",
            provenance={
                "exit_execution_id": commitment["execution_id"],
                "identity_source": PERSISTED_CANONICAL_ENTRY,
                # THIS SETTLEMENT IS A RECOVERY. Recorded so nothing later
                # mistakes an original settlement for a replayed one: the
                # fill is the committed fill, and the costs were computed
                # when the process came back.
                "recovered_from_commitment": True,
                "committed_at": commitment.get("committed_at"),
                # Operational only. The ECONOMICS are dated to committed_at
                # above; this says when the process got round to finishing
                # them, and must never be used as a cost boundary.
                "recovered_at": _now_iso(),
                "market_snapshot_at_commit": commitment.get(
                    "market_snapshot"),
                "fill_model_version": commitment.get("fill_model_version"),
            })
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "EXIT_FACTS_INVALID", "detail": str(e)}

    result = CS.settle_prepared_exit(facts)
    if result.get("ok"):
        EC.mark_settled(commitment["execution_id"], detail="recovered")
    return result
