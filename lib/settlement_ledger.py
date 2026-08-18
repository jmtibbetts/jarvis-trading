"""B1 — the entry settlement ledger's validator and persister.

Three jobs, kept deliberately apart from the transaction that owns them:

    validate_entry_ledger_facts()   PURE. Reads the authorization, the
                                    provenance and the settled figures, and
                                    either returns one immutable fact set or
                                    refuses. It opens no session, calls no
                                    provider, re-prices nothing.

    persist_entry_ledger()          Takes the EXISTING session. Constructs
                                    the header and ENTRY leg rows and adds
                                    them. It may not commit, may not roll
                                    back, may not open a session of its own —
                                    transaction ownership stays with
                                    paper_engine.settle_position_entry().

    load_position_settlement()      Reconstructs the semantic accounting
                                    object (lib.paper_settlement
                                    .PositionSettlement) from persisted rows,
                                    for tests today and B2 tomorrow.

WHY THE VALIDATOR RE-VERIFIES ARITHMETIC IT DID NOT COMPUTE. Settlement
records economic facts; it does not revisit execution decisions. So nothing
here calls the fee authority, the risk engine, or any resolver — the trade
already happened, and a ledger writer that consults today's config is
rewriting history. What it CAN do is prove the frozen facts agree with each
other: the quantity on the authorization, the quantity in the provenance,
the notional the multiplication implies, the fee the fee authority already
produced. Facts that disagree are refused BEFORE any mutation — the answer
to a contradiction is never to pick the plausible side.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                  EXECUTION_MODEL_CANONICAL,
                                  EXECUTION_SIDE_BUY, EXECUTION_SIDE_SELL,
                                  LEG_ENTRY, SETTLEMENT_VERSION)

# Representation tolerance for values that passed through the documented
# 6-decimal quantity rounding; absolute-floored so near-zero values do not
# get an impossible bar.
_REP_TOL = 1e-6


class LedgerValidationError(ValueError):
    """A canonical entry whose facts contradict each other. FAIL CLOSED:
    no position, no ledger, no cash movement."""


def _close(a: float, b: float, tol: float = _REP_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


@dataclass(frozen=True)
class EntryLedgerFacts:
    """The validated, self-consistent facts of ONE canonical entry.

    Immutable on purpose: the ORM constructors read these fields verbatim
    rather than re-interpreting the provenance JSON on their own.
    """
    observation_id: str
    execution_id: str
    signal_id: str | None

    symbol: str
    asset_class: str | None
    product: str
    venue: str
    instrument_id: str
    position_side: str            # long | short
    execution_side: str           # buy | sell

    quantity_unit: str
    multiplier: float

    requested_qty: float
    filled_qty: float
    decision_price: float | None
    fill_price: float
    notional_usd: float
    committed_margin_usd: float

    entry_fee_usd: float
    entry_fee_basis: str | None
    entry_fee_source: str | None
    entry_fee_quality: str | None
    entry_fee_contract_count: float | None
    entry_fee_contract_count_basis: str | None

    initial_stop: float | None
    initial_risk_usd: float | None

    spread_attribution_usd: float
    slippage_attribution_usd: float
    impact_attribution_usd: float

    cost_model: str
    execution_model: str
    engine_epoch: str
    fill_model: str | None

    provenance_json: str


def validate_entry_ledger_facts(auth, *, settled_qty: float,
                                settled_margin: float,
                                fill_price: float,
                                canonical_entry_fee_usd: float,
                                execution_provenance: dict,
                                observation_id: str,
                                execution_id: str) -> EntryLedgerFacts:
    """One validated fact set for one canonical entry, or a refusal.

    `settled_qty` / `settled_margin` are the figures settlement will ACTUALLY
    persist and debit — the rounded ones — not the raw authorization values,
    because the ledger must describe the cash that moves, not the estimate
    that preceded it.
    """
    prov = execution_provenance
    if not isinstance(prov, dict) or not prov:
        raise LedgerValidationError("canonical settlement requires execution "
                                    "provenance; none was supplied")
    if not observation_id or not execution_id:
        raise LedgerValidationError("canonical settlement requires both "
                                    "observation_id and execution_id")

    # ── One execution identity across the whole causal chain (§15) ───────
    prov_exec = prov.get("entry_execution_id")
    if not prov_exec:
        raise LedgerValidationError(
            "provenance carries no entry_execution_id — a canonical entry "
            "with an anonymous execution is not canonical")
    if str(prov_exec) != str(execution_id):
        raise LedgerValidationError(
            f"execution identity disagrees: settlement carries "
            f"{execution_id!r}, provenance names {prov_exec!r}")

    # ── Current canonical models only (§17). A hybrid gets no v1 ledger. ──
    from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
    if prov.get("execution_model") != EXECUTION_MODEL_CANONICAL:
        raise LedgerValidationError(
            f"execution_model {prov.get('execution_model')!r} is not the "
            f"canonical {EXECUTION_MODEL_CANONICAL!r}")
    if prov.get("cost_model") != COST_MODEL_CANONICAL:
        raise LedgerValidationError(
            f"cost_model {prov.get('cost_model')!r} is not "
            f"{COST_MODEL_CANONICAL!r}")
    if prov.get("engine_epoch") != CANONICAL_ENGINE_EPOCH:
        raise LedgerValidationError(
            f"engine_epoch {prov.get('engine_epoch')!r} is not the current "
            f"{CANONICAL_ENGINE_EPOCH!r}")

    # ── Frozen product identity (§18) — persisted, never re-resolved ─────
    symbol = str(prov.get("symbol") or "").strip()
    product = str(prov.get("product") or "").strip()
    venue = str(prov.get("venue") or "").strip()
    instrument_id = str(prov.get("instrument") or "").strip()
    if not symbol or not product or not venue:
        raise LedgerValidationError(
            f"provenance identity is incomplete (symbol={symbol!r}, "
            f"product={product!r}, venue={venue!r})")
    if not instrument_id:
        raise LedgerValidationError(
            f"{symbol}: no instrument identity in provenance — for an exact "
            f"product the contract, not the pair, is what was traded")

    # ── Unit basis (§19) ─────────────────────────────────────────────────
    unit = prov.get("quantity_unit")
    if not (isinstance(unit, str) and unit.strip()):
        raise LedgerValidationError(
            f"provenance quantity_unit {unit!r} is not a non-empty unit")
    try:
        mult = float(prov.get("multiplier"))
    except (TypeError, ValueError):
        mult = float("nan")
    if not math.isfinite(mult) or mult <= 0:
        raise LedgerValidationError(
            f"provenance multiplier {prov.get('multiplier')!r} is not a "
            f"finite positive number")

    # ── Quantities agree, and only relate downward (§19, §20) ────────────
    qty = float(settled_qty)
    if not (qty > 0) or not math.isfinite(qty):
        raise LedgerValidationError(f"settled quantity {settled_qty!r} is "
                                    f"not a positive finite number")
    prov_filled = prov.get("filled_quantity")
    if prov_filled is None or not _close(qty, float(prov_filled)):
        raise LedgerValidationError(
            f"settled quantity {qty!r} is not the provenance filled "
            f"quantity {prov_filled!r}")
    if not _close(qty, float(auth.qty)):
        raise LedgerValidationError(
            f"settled quantity {qty!r} is not the authorized {auth.qty!r}")
    requested = prov.get("requested_quantity")
    if requested is None:
        raise LedgerValidationError("provenance carries no requested_quantity")
    requested = float(requested)
    if requested + _REP_TOL < qty:
        raise LedgerValidationError(
            f"requested {requested!r} is smaller than filled {qty!r} — a "
            f"fill larger than its own order is not a fill")

    # ── One fill price everywhere (§21) ──────────────────────────────────
    fill = float(fill_price)
    prov_fill = prov.get("actual_entry_fill")
    if prov_fill is None or not _close(fill, float(prov_fill), 1e-9):
        raise LedgerValidationError(
            f"settlement fill {fill!r} is not the provenance fill "
            f"{prov_fill!r}")

    # ── Executed notional is one arithmetic truth (§8) ───────────────────
    notional = qty * fill * mult
    prov_notional = prov.get("executed_notional_usd")
    if prov_notional is not None and not _close(notional, float(prov_notional)):
        raise LedgerValidationError(
            f"executed notional disagrees: {qty:g} x {fill:g} x {mult:g} = "
            f"{notional!r} but provenance says {prov_notional!r}")
    if auth.notional and not _close(notional, float(auth.notional)):
        raise LedgerValidationError(
            f"executed notional {notional!r} is not the authorization's "
            f"{auth.notional!r}")

    # ── The fee is the fee that was charged (§22) ────────────────────────
    fee = float(canonical_entry_fee_usd)
    if not math.isfinite(fee) or fee < 0:
        raise LedgerValidationError(f"entry fee {canonical_entry_fee_usd!r} "
                                    f"is not a finite non-negative number")
    prov_fee = prov.get("entry_fee_usd")
    if prov_fee is None or not _close(fee, float(prov_fee), 1e-9):
        raise LedgerValidationError(
            f"settlement fee {fee!r} is not the provenance fee {prov_fee!r}")
    fee_count = prov.get("entry_fee_contract_count")
    count_basis = prov.get("entry_fee_contract_count_basis")
    from lib.fee_authority import EXECUTED_EXACT, PER_CONTRACT
    if (count_basis == EXECUTED_EXACT
            and prov.get("entry_fee_basis") == PER_CONTRACT):
        if fee_count is None or not _close(float(fee_count), qty, 1e-9):
            raise LedgerValidationError(
                f"an EXECUTED_EXACT per-contract fee counted "
                f"{fee_count!r} contracts against a settled fill of {qty!r} "
                f"— the fee describes a different trade")

    # ── Initial risk is one arithmetic truth (§23) ───────────────────────
    initial_stop = float(auth.stop) if auth.stop else None
    initial_risk = float(auth.loss_at_stop) if auth.loss_at_stop is not None \
        else None
    if initial_stop is not None and initial_risk is not None:
        implied = qty * abs(fill - initial_stop) * mult
        if not _close(implied, initial_risk, 1e-9):
            raise LedgerValidationError(
                f"initial risk disagrees: {qty:g} x |{fill:g} - "
                f"{initial_stop:g}| x {mult:g} = {implied!r} but the "
                f"authorization says {initial_risk!r} — refusing to store "
                f"two definitions of R")

    margin = float(settled_margin)
    if not math.isfinite(margin) or margin <= 0:
        raise LedgerValidationError(f"committed margin {settled_margin!r} is "
                                    f"not a positive finite number")

    side_val = getattr(auth, "side", None)
    if side_val not in (1, -1):
        raise LedgerValidationError(f"authorization side {side_val!r} is not "
                                    f"canonical (+1/-1)")
    position_side = "long" if side_val == 1 else "short"
    execution_side = EXECUTION_SIDE_BUY if side_val == 1 else EXECUTION_SIDE_SELL

    dp = prov.get("decision_price")
    return EntryLedgerFacts(
        observation_id=str(observation_id),
        execution_id=str(execution_id),
        signal_id=prov.get("signal_id"),
        symbol=symbol, asset_class=prov.get("asset_class"),
        product=product, venue=venue, instrument_id=instrument_id,
        position_side=position_side, execution_side=execution_side,
        quantity_unit=unit.strip(), multiplier=mult,
        requested_qty=requested, filled_qty=qty,
        decision_price=float(dp) if dp is not None else None,
        fill_price=fill, notional_usd=notional,
        committed_margin_usd=margin,
        entry_fee_usd=fee,
        entry_fee_basis=prov.get("entry_fee_basis"),
        entry_fee_source=prov.get("entry_fee_source"),
        entry_fee_quality=prov.get("cost_model_fee_quality"),
        entry_fee_contract_count=(float(fee_count) if fee_count is not None
                                  else None),
        entry_fee_contract_count_basis=count_basis,
        initial_stop=initial_stop, initial_risk_usd=initial_risk,
        spread_attribution_usd=float(prov.get("spread_attribution_usd") or 0.0),
        slippage_attribution_usd=float(prov.get("slippage_attribution_usd") or 0.0),
        impact_attribution_usd=float(prov.get("impact_attribution_usd") or 0.0),
        cost_model=COST_MODEL_CANONICAL,
        execution_model=EXECUTION_MODEL_CANONICAL,
        engine_epoch=prov.get("engine_epoch"),
        fill_model=prov.get("fill_model"),
        provenance_json=json.dumps(prov),
    )


def persist_entry_ledger(db, *, position, facts: EntryLedgerFacts,
                         settlement_time: str):
    """Add the OPEN header and the ENTRY leg to the EXISTING transaction.

    The header's economic figures must equal the POSITION ROW's — not the
    authorization's, not the provenance's interpretation — because §7's rule
    is that the ledger describes what was actually booked. A disagreement
    here means the caller settled something other than what it validated,
    and the whole transaction deserves to unwind.

    This function must never: open a session, commit, roll back, call a
    provider, the fee authority, the risk engine, or any resolver.
    """
    from app.database import PaperPositionSettlement, PaperSettlementLeg

    if not _close(float(position.qty), facts.filled_qty, 1e-9):
        raise LedgerValidationError(
            f"position row carries qty {position.qty!r} but the validated "
            f"facts settled {facts.filled_qty!r}")
    if not _close(float(position.entry_price), facts.fill_price, 1e-9):
        raise LedgerValidationError(
            f"position row carries entry {position.entry_price!r} but the "
            f"validated fill is {facts.fill_price!r}")
    if not _close(float(position.margin_used), facts.committed_margin_usd, 1e-9):
        raise LedgerValidationError(
            f"position row carries margin {position.margin_used!r} but the "
            f"validated commitment is {facts.committed_margin_usd!r}")

    header = PaperPositionSettlement(
        position_id=position.id,
        observation_id=facts.observation_id,
        signal_id=facts.signal_id,
        symbol=facts.symbol, asset_class=facts.asset_class,
        product=facts.product, venue=facts.venue,
        instrument_id=facts.instrument_id,
        position_side=facts.position_side,
        quantity_unit=facts.quantity_unit, multiplier=facts.multiplier,
        original_quantity=facts.filled_qty,
        original_notional_usd=facts.notional_usd,
        committed_margin_usd=facts.committed_margin_usd,
        decision_entry_price=facts.decision_price,
        actual_entry_fill=facts.fill_price,
        entry_execution_id=facts.execution_id,
        entry_fee_usd=facts.entry_fee_usd,
        entry_fee_basis=facts.entry_fee_basis,
        entry_fee_source=facts.entry_fee_source,
        entry_fee_quality=facts.entry_fee_quality,
        entry_fee_contract_count=facts.entry_fee_contract_count,
        entry_fee_contract_count_basis=facts.entry_fee_contract_count_basis,
        initial_stop=facts.initial_stop,
        initial_risk_usd=facts.initial_risk_usd,
        settlement_version=SETTLEMENT_VERSION,
        cost_model=facts.cost_model,
        execution_model=facts.execution_model,
        engine_epoch=facts.engine_epoch,
        status="OPEN", settlement_revision=0,
        opened_at=settlement_time, closed_at=None,
    )
    leg = PaperSettlementLeg(
        position_id=position.id,
        observation_id=facts.observation_id,
        signal_id=facts.signal_id,
        execution_id=facts.execution_id,
        kind=LEG_ENTRY,
        settlement_version=SETTLEMENT_VERSION,
        settlement_revision=0,
        symbol=facts.symbol, product=facts.product, venue=facts.venue,
        instrument_id=facts.instrument_id,
        position_side=facts.position_side,
        execution_side=facts.execution_side,
        requested_qty=facts.requested_qty, filled_qty=facts.filled_qty,
        quantity_unit=facts.quantity_unit, multiplier=facts.multiplier,
        decision_price=facts.decision_price, fill_price=facts.fill_price,
        notional_usd=facts.notional_usd,
        explicit_fee_usd=facts.entry_fee_usd,
        fee_basis=facts.entry_fee_basis,
        fee_source=facts.entry_fee_source,
        fee_quality=facts.entry_fee_quality,
        fee_contract_count=facts.entry_fee_contract_count,
        fee_contract_count_basis=facts.entry_fee_contract_count_basis,
        # An entry is not an outcome. Structurally zero, never computed.
        gross_pnl_usd=0.0, holding_cost_usd=0.0,
        released_margin_usd=0.0, hours_held=0.0,
        spread_attribution_usd=facts.spread_attribution_usd,
        slippage_attribution_usd=facts.slippage_attribution_usd,
        impact_attribution_usd=facts.impact_attribution_usd,
        execution_model=facts.execution_model,
        cost_model=facts.cost_model,
        fill_model=facts.fill_model,
        created_at=settlement_time,
        provenance_json=facts.provenance_json,
    )
    db.add(header)
    db.add(leg)
    return header, leg


def load_position_settlement(db, position_id: str):
    """Reconstruct the SEMANTIC accounting object from persisted rows.

    Read-only, for tests and B2. The pure dataclass never queries the DB
    itself; this is the one place rows become accounting. Returns None when
    the position has no canonical ledger — which is what legacy looks like,
    and must stay distinguishable from an empty one.
    """
    from app.database import PaperPositionSettlement, PaperSettlementLeg
    from lib.paper_settlement import PositionSettlement, SettlementLeg

    header = db.query(PaperPositionSettlement).filter(
        PaperPositionSettlement.position_id == position_id).first()
    if header is None:
        return None

    settlement = PositionSettlement(
        position_id=header.position_id,
        cost_model=header.cost_model,
        execution_model=header.execution_model,
        entry_fee_usd=float(header.entry_fee_usd or 0.0),
        committed_margin_usd=float(header.committed_margin_usd or 0.0),
    )
    # DETERMINISTIC LEDGER ORDER IS THE REVISION, not the clock. Two legs
    # written in one busy second have equal timestamps; the revision is the
    # concurrency authority and therefore the ordering authority, with
    # created_at and id only as stable tie-breakers.
    rows = db.query(PaperSettlementLeg).filter(
        PaperSettlementLeg.position_id == position_id).order_by(
        PaperSettlementLeg.settlement_revision,
        PaperSettlementLeg.created_at,
        PaperSettlementLeg.id).all()
    for row in rows:
        settlement.add(SettlementLeg(
            kind=row.kind,
            quantity=float(row.filled_qty or 0.0),
            fill_price=float(row.fill_price or 0.0),
            gross_pnl_usd=float(row.gross_pnl_usd or 0.0),
            explicit_fee_usd=float(row.explicit_fee_usd or 0.0),
            funding_usd=float(row.holding_cost_usd or 0.0),
            released_margin_usd=float(row.released_margin_usd or 0.0),
            hours_held=float(row.hours_held or 0.0),
            execution_id=row.execution_id,
            at=row.created_at,
        ))
    return settlement


def validate_position_projection(db, position, header) -> str | None:
    """P0 (B2C) — prove the mutable PaperPosition still agrees with the
    canonical ledger, or say exactly where it does not.

    The ledger already RECORDS what current exposure must be: at revision 0
    it is the header's original facts; after any exit leg it is that leg's
    persisted `remaining_qty_after` / `remaining_margin_after` — persisted
    running-state facts, deliberately not replayed arithmetic. A projection
    that drifted (a stray writer, a manual edit, a bug) must not become
    settlement authority for another exit.

    Returns a human-readable mismatch description, or None when coherent.
    Reads only; never mutates.
    """
    from app.database import PaperSettlementLeg
    from lib.paper_settlement import LEG_ENTRY

    rev = int(header.settlement_revision)
    if rev == 0:
        expected_qty = float(header.original_quantity)
        expected_margin = float(header.committed_margin_usd)
    else:
        legs = db.query(PaperSettlementLeg).filter(
            PaperSettlementLeg.position_id == header.position_id,
            PaperSettlementLeg.settlement_revision == rev).all()
        if len(legs) != 1:
            return (f"header names revision {rev} but {len(legs)} ledger "
                    f"legs carry that revision — the ledger and its header "
                    f"disagree about history")
        leg = legs[0]
        if leg.kind == LEG_ENTRY:
            return (f"revision {rev} is an ENTRY leg — an advanced header "
                    f"must name an exit leg")
        if leg.remaining_qty_after is None or leg.remaining_margin_after is None:
            return (f"revision {rev} leg carries no remaining-state facts")
        expected_qty = float(leg.remaining_qty_after)
        expected_margin = float(leg.remaining_margin_after)

    expected_notional = (expected_qty * float(header.actual_entry_fill)
                         * float(header.multiplier))

    # Lifecycle coherence: OPEN means exposure remains; CLOSED means none.
    if header.status == "OPEN" and expected_qty <= 0:
        return "header is OPEN but the ledger says no exposure remains"
    if header.status == "CLOSED" and expected_qty > 0:
        return (f"header is CLOSED but the ledger says {expected_qty:g} "
                f"remains")
    if header.status == "CLOSED" and position.status == "Open":
        return "header is CLOSED but the position row is still Open"
    if header.status == "OPEN" and position.status != "Open":
        return (f"header is OPEN but the position row is "
                f"{position.status!r}")

    tol = 1e-6
    def _differs(a, b):
        return abs(float(a) - float(b)) > tol * max(1.0, abs(float(a)),
                                                    abs(float(b)))
    if _differs(position.qty or 0.0, expected_qty):
        return (f"position qty {position.qty!r} disagrees with the ledger's "
                f"{expected_qty!r} at revision {rev}")
    if _differs(position.margin_used or 0.0, expected_margin):
        return (f"position margin {position.margin_used!r} disagrees with "
                f"the ledger's {expected_margin!r} at revision {rev}")
    if position.notional is not None and _differs(position.notional,
                                                  expected_notional):
        return (f"position notional {position.notional!r} disagrees with "
                f"the ledger's {expected_notional!r} at revision {rev}")
    return None
