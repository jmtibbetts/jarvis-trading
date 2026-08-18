"""B2A — the canonical exit's FINANCIAL SETTLEMENT CORE.

This module answers exactly one question:

    given an exit execution that has ALREADY been established — filled,
    priced, fee'd, and carry-quoted — what may the account book mutate?

It deliberately does NOT acquire market data, construct exit orders, submit
to venues, price fees, or measure carry. All of that produces PREPARED FACTS
before this layer; a settlement that consults live state is rewriting
history, and a settlement that re-prices is a second execution decision
wearing an accountant's coat. The market-facing orchestrator (B2B) will
produce `ExitSettlementFacts`; every accounting invariant here is testable
without a provider, a quote stream, or a venue.

THE CONTRACTS, in the order the transaction enforces them:

    identity is FROZEN        the exit may only settle the exact
                              symbol/product/venue/instrument the B1 header
                              recorded — no router, no resolver, no config
    the basis cannot change   same quantity_unit, same multiplier
    the side must REDUCE      a long exits by SELLING, a short by BUYING;
                              anything else is adding exposure
    revision is the           expected_revision must equal the header's;
    concurrency authority     a stale reader gets STALE_SETTLEMENT_REVISION
                              and mutates nothing — never a silent retry
    the execution is          a retried execution_id returns an explicit
    idempotent                IDEMPOTENT_ALREADY_SETTLED and moves nothing
    quantity only shrinks     filled_qty <= remaining, representation
                              tolerance only — never another discrete unit
    margin release is         capital coming home, not P&L; FINAL releases
    return of capital         ALL remaining margin so dust cannot trap it
    one thesis, one vote      partials are accounting legs; only FINAL
                              creates the outcome, the counters, the trade

PORTFOLIO realized_pnl SEMANTICS — AUDITED AND PINNED (B2A §27). The
measured legacy behaviour is CUMULATIVE-AS-REALIZED: every legacy close
(partial or final) adds that leg's net P&L, in lockstep with a PaperTrade
row carrying the same number. Canonical settlement keeps the cumulative
semantics with one deliberate placement: each exit leg accrues its own net
economics (gross - exit fee - holding cost) when it settles, and the ENTRY
FEE accrues at FINAL. Placed there, not at entry, because (a) B1 shipped
with entry leaving realized_pnl untouched, and (b) the strongest measurable
product invariant is that a COMPLETED position's total contribution to
portfolio.realized_pnl equals its single aggregate PaperTrade.realized_pnl —
which this placement preserves exactly:

    delta(realized_pnl) over the position
        = sum(leg gross - exit fees - holding) - entry fee
        = canonical net_pnl_usd
        = the one aggregate PaperTrade.realized_pnl

Not a hybrid: the entry fee is charged once, at a defined moment, and the
lifecycle total is the canonical net.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from lib.holding_cost_authority import UNAVAILABLE as HC_UNAVAILABLE
from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                  EXECUTION_MODEL_CANONICAL,
                                  EXECUTION_SIDE_BUY, EXECUTION_SIDE_SELL,
                                  LEG_FINAL_EXIT, LEG_PARTIAL_EXIT,
                                  SETTLEMENT_VERSION)
from lib.realized_outcome import (FORCED_LIQUIDATION, MARGIN_CALL, STOP_EXIT,
                                  TARGET_EXIT, VOLUNTARY_EXIT)

logger = logging.getLogger(__name__)

# The canonical partial/scale-out reason. A partial is an accounting event;
# its reason says why quantity left, not what the strategy concluded.
SCALE_OUT = "SCALE_OUT"

EXIT_REASONS = frozenset({VOLUNTARY_EXIT, STOP_EXIT, TARGET_EXIT,
                          MARGIN_CALL, FORCED_LIQUIDATION, SCALE_OUT})

# Refusal vocabulary — named, so a caller can tell them apart.
NOT_CANONICAL_SETTLEMENT_POSITION = "NOT_CANONICAL_SETTLEMENT_POSITION"
STALE_SETTLEMENT_REVISION = "STALE_SETTLEMENT_REVISION"
IDEMPOTENT_ALREADY_SETTLED = "IDEMPOTENT_ALREADY_SETTLED"
EXIT_VALIDATION_FAILED = "EXIT_SETTLEMENT_VALIDATION_FAILED"
# Same execution id, different position — an id collision, not a retry.
EXECUTION_ID_COLLISION = "EXECUTION_ID_COLLISION"
# Same execution id, same position, DIFFERENT economic facts. A retry
# describes the same event or it is not a retry.
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

# What carry mechanism each product is ALLOWED to be charged under. A
# CRYPTO_PERP quote claiming NOT_APPLICABLE is not an established zero — it
# is a structurally impossible zero, and it refuses.
_EXPECTED_HOLDING_KIND = {
    "CRYPTO_PERP": "FUNDING",
    "EQUITY_SHORT": "BORROW",
    "CRYPTO_SPOT": "NOT_APPLICABLE",
    "EQUITY_SPOT": "NOT_APPLICABLE",
    "ETF_SPOT": "NOT_APPLICABLE",
}

_REP_TOL = 1e-9


class ExitValidationError(ValueError):
    """Prepared exit facts that contradict themselves or the ledger.
    FAIL CLOSED: nothing mutates."""


def _close_enough(a: float, b: float, tol: float = _REP_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)),
                                                 abs(float(b)))


@dataclass(frozen=True)
class ExitSettlementFacts:
    """One prepared exit execution, ready to be settled — nothing else.

    Three prices, three facts, never collapsed:
        trigger_price         the stop/target/liquidation level that fired
        decision_exit_price   the mark/reference at the exit decision
        fill_price            what the venue actually paid
    """
    position_id: str
    expected_revision: int
    execution_id: str

    symbol: str
    product: str
    venue: str
    instrument_id: str

    position_side: str            # long | short
    execution_side: str           # buy | sell

    requested_qty: float
    filled_qty: float
    quantity_unit: str
    multiplier: float

    fill_price: float
    trigger_price: float | None = None
    decision_exit_price: float | None = None

    # Exit fee — the FeeQuote's facts, flattened. Already priced; never
    # re-priced here.
    fee_usd: float = 0.0
    fee_basis: str | None = None
    fee_source: str | None = None
    fee_quality: str | None = None
    fee_contract_count: float | None = None
    fee_contract_count_basis: str | None = None
    fee_notional_usd: float | None = None

    # Holding cost — the HoldingCostQuote's facts, flattened. Signed: a
    # negative amount is carry RECEIVED. The quote's OWN identity travels
    # too (P0.1): a quote that priced a different symbol, product, exposure
    # or interval is a charge for a different position, and without these
    # fields settlement could not tell.
    holding_cost_usd: float = 0.0
    holding_cost_kind: str | None = None
    holding_cost_source: str | None = None
    holding_cost_quality: str | None = None
    holding_cost_version: str | None = None
    holding_cost_symbol: str | None = None
    holding_cost_product: str | None = None
    holding_cost_notional_usd: float | None = None
    holding_cost_rate: float | None = None
    hours_held: float | None = None

    spread_attribution_usd: float = 0.0
    slippage_attribution_usd: float = 0.0
    impact_attribution_usd: float = 0.0

    fill_model: str | None = None
    exit_reason: str = VOLUNTARY_EXIT
    settled_at: str = ""
    provenance: dict = field(default_factory=dict)


def exit_facts(*, position_id: str, expected_revision: int, execution_id: str,
               symbol: str, product: str, venue: str, instrument_id: str,
               position_side: str, execution_side: str,
               requested_qty: float, filled_qty: float,
               quantity_unit: str, multiplier: float,
               fill_price: float, fee_quote, holding_quote,
               settled_at: str, exit_reason: str = VOLUNTARY_EXIT,
               trigger_price: float | None = None,
               decision_exit_price: float | None = None,
               spread_attribution_usd: float = 0.0,
               slippage_attribution_usd: float = 0.0,
               impact_attribution_usd: float = 0.0,
               fill_model: str | None = None,
               provenance: dict | None = None) -> ExitSettlementFacts:
    """Flatten the prepared quote objects into one immutable fact set.

    Refuses quotes that are not settlement-grade: a fee quote that is not
    ok, or a holding quote that is UNAVAILABLE. An unavailable carry is not
    zero — settling it as zero records free carry, which is the exact lie
    the holding-cost authority exists to prevent.
    """
    if fee_quote is None or not getattr(fee_quote, "ok", False) \
            or fee_quote.fee_usd is None:
        raise ExitValidationError(
            f"exit fee quote is not settlement-grade: "
            f"{getattr(fee_quote, 'reason', 'missing')!r} — refusing to "
            f"settle an exit whose cost is unknown")
    if holding_quote is None or not getattr(holding_quote, "ok", False) \
            or holding_quote.amount_usd is None \
            or holding_quote.quality == HC_UNAVAILABLE:
        raise ExitValidationError(
            f"holding-cost quote is not settlement-grade: "
            f"{getattr(holding_quote, 'reason', 'missing')!r} — an "
            f"unavailable carry is not zero")
    return ExitSettlementFacts(
        position_id=position_id, expected_revision=int(expected_revision),
        execution_id=execution_id, symbol=symbol, product=product,
        venue=venue, instrument_id=instrument_id,
        position_side=position_side, execution_side=execution_side,
        requested_qty=float(requested_qty), filled_qty=float(filled_qty),
        quantity_unit=quantity_unit, multiplier=float(multiplier),
        fill_price=float(fill_price), trigger_price=trigger_price,
        decision_exit_price=decision_exit_price,
        fee_usd=float(fee_quote.fee_usd), fee_basis=fee_quote.fee_basis,
        fee_source=fee_quote.source, fee_quality=fee_quote.quality,
        fee_contract_count=fee_quote.contract_count,
        fee_contract_count_basis=fee_quote.contract_count_basis,
        fee_notional_usd=fee_quote.notional_usd,
        holding_cost_usd=float(holding_quote.amount_usd),
        holding_cost_kind=holding_quote.kind,
        holding_cost_source=holding_quote.source,
        holding_cost_quality=holding_quote.quality,
        holding_cost_version=holding_quote.version,
        # FROM the quote, never rederived — these are what binding checks.
        holding_cost_symbol=holding_quote.symbol,
        holding_cost_product=holding_quote.product,
        holding_cost_notional_usd=holding_quote.notional_usd,
        holding_cost_rate=holding_quote.rate,
        hours_held=holding_quote.hours_held,
        spread_attribution_usd=float(spread_attribution_usd),
        slippage_attribution_usd=float(slippage_attribution_usd),
        impact_attribution_usd=float(impact_attribution_usd),
        fill_model=fill_model, exit_reason=exit_reason,
        settled_at=settled_at, provenance=dict(provenance or {}))


def validate_exit_settlement_facts(facts: ExitSettlementFacts) -> None:
    """PURE. No session, no provider, no fee authority, no carry authority.
    Proves the prepared facts agree with each other; the transaction proves
    they agree with the ledger."""
    f = facts
    for name in ("position_id", "execution_id", "symbol", "product",
                 "venue", "instrument_id", "settled_at"):
        if not str(getattr(f, name) or "").strip():
            raise ExitValidationError(f"{name} is empty — an exit with an "
                                      f"anonymous {name} cannot be settled")
    if f.exit_reason not in EXIT_REASONS:
        raise ExitValidationError(
            f"exit_reason {f.exit_reason!r} is not canonical "
            f"({sorted(EXIT_REASONS)}) — a stop is not a liquidation, and "
            f"the vocabulary keeps them apart")
    if f.expected_revision < 0:
        raise ExitValidationError(
            f"expected_revision {f.expected_revision!r} is negative")

    # ── Quantities (§11.1) ───────────────────────────────────────────────
    req, filled = float(f.requested_qty), float(f.filled_qty)
    if not (math.isfinite(req) and math.isfinite(filled)):
        raise ExitValidationError(
            f"quantities are not finite (requested {req!r}, filled "
            f"{filled!r})")
    if not (req > 0 and filled > 0):
        raise ExitValidationError(
            f"quantities must be positive (requested {req!r}, filled "
            f"{filled!r})")
    if filled > req + _REP_TOL * max(1.0, req):
        raise ExitValidationError(
            f"filled {filled!r} exceeds requested {req!r} — a fill larger "
            f"than its own order is not a fill")

    # ── Basis (§11.2) ────────────────────────────────────────────────────
    if not str(f.quantity_unit or "").strip():
        raise ExitValidationError("quantity_unit is empty")
    if not (math.isfinite(f.multiplier) and f.multiplier > 0):
        raise ExitValidationError(
            f"multiplier {f.multiplier!r} is not a finite positive number")
    if f.quantity_unit == "CONTRACTS":
        for label, q in (("filled", filled), ("requested", req)):
            if abs(q - round(q)) > _REP_TOL * max(1.0, q):
                raise ExitValidationError(
                    f"a historical execution of {q!r} {label} CONTRACTS is "
                    f"not executable arithmetic — refusing to round a fill "
                    f"that already happened")

    # ── Sides ────────────────────────────────────────────────────────────
    if f.position_side not in ("long", "short"):
        raise ExitValidationError(
            f"position_side {f.position_side!r} is not canonical")
    if f.execution_side not in (EXECUTION_SIDE_BUY, EXECUTION_SIDE_SELL):
        raise ExitValidationError(
            f"execution_side {f.execution_side!r} is not canonical")
    reducing = ((f.position_side == "long"
                 and f.execution_side == EXECUTION_SIDE_SELL)
                or (f.position_side == "short"
                    and f.execution_side == EXECUTION_SIDE_BUY))
    if not reducing:
        raise ExitValidationError(
            f"a {f.position_side} position does not exit by "
            f"{f.execution_side.upper()} — that execution ADDS exposure, "
            f"and settlement never infers intent from the P&L sign")

    # ── Prices ───────────────────────────────────────────────────────────
    fill = float(f.fill_price)
    if not (math.isfinite(fill) and fill > 0):
        raise ExitValidationError(f"fill price {f.fill_price!r} is not a "
                                  f"finite positive price")
    for label, p in (("trigger_price", f.trigger_price),
                     ("decision_exit_price", f.decision_exit_price)):
        if p is not None and not (math.isfinite(float(p)) and float(p) > 0):
            raise ExitValidationError(f"{label} {p!r} is not a finite "
                                      f"positive price")

    # ── Fee (§11.3) — already priced; only its coherence is checked ─────
    if not (math.isfinite(f.fee_usd) and f.fee_usd >= 0):
        raise ExitValidationError(f"exit fee {f.fee_usd!r} is not a finite "
                                  f"non-negative number")
    from lib.fee_authority import EXECUTED_EXACT, PER_CONTRACT
    if f.quantity_unit == "CONTRACTS" and f.fee_basis == PER_CONTRACT:
        if f.fee_contract_count_basis != EXECUTED_EXACT:
            raise ExitValidationError(
                f"an executed CONTRACTS exit must be fee'd at the exact "
                f"filled count; got basis {f.fee_contract_count_basis!r}")
        if f.fee_contract_count is None or not _close_enough(
                float(f.fee_contract_count), filled):
            raise ExitValidationError(
                f"fee counted {f.fee_contract_count!r} contracts against a "
                f"fill of {filled!r} — the fee describes a different trade")
    if f.fee_notional_usd is not None:
        expected = filled * fill * f.multiplier
        if not _close_enough(float(f.fee_notional_usd), expected, 1e-6):
            raise ExitValidationError(
                f"fee quote notional {f.fee_notional_usd!r} is not "
                f"{filled:g} x {fill:g} x {f.multiplier:g} = {expected!r}")

    # ── Holding cost (§11.4) — negative is legal; unknown is not ────────
    if not math.isfinite(f.holding_cost_usd):
        raise ExitValidationError(
            f"holding cost {f.holding_cost_usd!r} is not finite")
    if f.holding_cost_quality == HC_UNAVAILABLE:
        raise ExitValidationError(
            "holding cost quality is UNAVAILABLE — an unavailable carry is "
            "not zero, and settling it as zero records free carry")

    # ── P0.1: the quote must have priced THIS exit's exposure ───────────
    if f.holding_cost_symbol is not None \
            and str(f.holding_cost_symbol).upper() != str(f.symbol).upper():
        raise ExitValidationError(
            f"the holding quote priced {f.holding_cost_symbol!r} but this "
            f"exit settles {f.symbol!r} — a carry charge for a different "
            f"symbol is a charge for a different position")
    if f.holding_cost_product is not None \
            and str(f.holding_cost_product).upper() != str(f.product).upper():
        raise ExitValidationError(
            f"the holding quote priced product {f.holding_cost_product!r} "
            f"but this exit settles {f.product!r}")
    expected_kind = _EXPECTED_HOLDING_KIND.get(str(f.product).upper())
    if expected_kind is not None and f.holding_cost_kind != expected_kind:
        raise ExitValidationError(
            f"a {f.product} position's carry mechanism is {expected_kind}, "
            f"but the quote says {f.holding_cost_kind!r} — a structurally "
            f"impossible zero is not an established zero")


def settle_prepared_exit(facts: ExitSettlementFacts) -> dict:
    """THE financial mutation boundary for canonical exits.

    Owns one SHORT transaction — every provider-shaped question was answered
    before this call. Reloads everything inside the transaction; a stale
    pre-execution snapshot is not trusted for anything economic.
    """
    from lib.runtime_mode import forbid_economic_mutation
    forbid_economic_mutation("canonical_exit_settlement")

    try:
        validate_exit_settlement_facts(facts)
    except ExitValidationError as e:
        logger.error("[CanonicalSettlement] %s exit facts refused: %s",
                     facts.position_id, e)
        return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                "detail": str(e)}

    from app.database import (PaperPortfolio, PaperPosition,
                              PaperPositionSettlement, PaperRealizedOutcome,
                              PaperSettlementLeg, PaperTrade, get_db, new_id)
    from lib.paper_settlement import LEG_ENTRY

    f = facts
    with get_db() as db:
        # ── Idempotency FIRST — and idempotent means SAME ECONOMIC EVENT
        # (P0.2), not merely same id. Same id on another position is a
        # collision; same id with different economics is a conflict; only a
        # true retry — the already-settled leg describing this exact
        # execution — succeeds without mutation. expected_revision is
        # deliberately NOT compared: a legitimate retry naturally arrives
        # after the revision advanced.
        prior = db.query(PaperSettlementLeg).filter(
            PaperSettlementLeg.execution_id == f.execution_id).first()
        if prior is not None:
            if prior.position_id != f.position_id:
                return {"ok": False, "error": EXECUTION_ID_COLLISION,
                        "detail": (f"execution {f.execution_id} already "
                                   f"settled into position "
                                   f"{prior.position_id}, not "
                                   f"{f.position_id} — one execution id "
                                   f"names one economic event")}
            diffs = []
            for name, prior_v, new_v, numeric in (
                    ("symbol", prior.symbol, f.symbol, False),
                    ("product", prior.product, f.product, False),
                    ("venue", prior.venue, f.venue, False),
                    ("instrument_id", prior.instrument_id,
                     f.instrument_id, False),
                    ("execution_side", prior.execution_side,
                     f.execution_side, False),
                    ("quantity_unit", prior.quantity_unit,
                     f.quantity_unit, False),
                    ("exit_reason", prior.exit_reason, f.exit_reason, False),
                    ("holding_cost_type", prior.holding_cost_type,
                     f.holding_cost_kind, False),
                    ("requested_qty", prior.requested_qty,
                     f.requested_qty, True),
                    ("filled_qty", prior.filled_qty, f.filled_qty, True),
                    ("multiplier", prior.multiplier, f.multiplier, True),
                    ("fill_price", prior.fill_price, f.fill_price, True),
                    ("explicit_fee_usd", prior.explicit_fee_usd,
                     f.fee_usd, True),
                    ("holding_cost_usd", prior.holding_cost_usd,
                     f.holding_cost_usd, True)):
                if numeric:
                    same = (prior_v is not None and new_v is not None
                            and _close_enough(float(prior_v), float(new_v)))
                else:
                    same = prior_v == new_v
                if not same:
                    diffs.append(f"{name}: settled {prior_v!r}, "
                                 f"retry claims {new_v!r}")
            if diffs:
                return {"ok": False, "error": IDEMPOTENCY_CONFLICT,
                        "detail": ("the same execution id arrived with "
                                   "different economic facts — a retry "
                                   "describes the same event or it is not "
                                   "a retry (" + "; ".join(diffs) + ")")}
            return {"ok": True, "idempotent": True,
                    "result": IDEMPOTENT_ALREADY_SETTLED,
                    "position_id": prior.position_id,
                    "leg_id": prior.id,
                    "revision": prior.settlement_revision}

        pos = db.query(PaperPosition).filter(
            PaperPosition.id == f.position_id).first()
        header = db.query(PaperPositionSettlement).filter(
            PaperPositionSettlement.position_id == f.position_id).first()

        # ── Canonical-only. Legacy and hybrid fail CLOSED (§56) ──────────
        if header is None:
            return {"ok": False,
                    "error": NOT_CANONICAL_SETTLEMENT_POSITION,
                    "detail": (f"position {f.position_id} has no canonical "
                               f"settlement ledger; the legacy close paths "
                               f"own legacy positions and this layer never "
                               f"falls through to them")}
        if pos is None:
            return {"ok": False, "error": NOT_CANONICAL_SETTLEMENT_POSITION,
                    "detail": f"position {f.position_id} does not exist"}
        if pos.status != "Open" or header.status != "OPEN":
            return {"ok": False, "error": NOT_CANONICAL_SETTLEMENT_POSITION,
                    "detail": (f"position is {pos.status!r} / header "
                               f"{header.status!r} — only an OPEN canonical "
                               f"position can settle an exit")}
        if (header.settlement_version != SETTLEMENT_VERSION
                or header.cost_model != COST_MODEL_CANONICAL
                or header.execution_model != EXECUTION_MODEL_CANONICAL):
            return {"ok": False, "error": NOT_CANONICAL_SETTLEMENT_POSITION,
                    "detail": (f"header models "
                               f"({header.settlement_version!r}, "
                               f"{header.cost_model!r}, "
                               f"{header.execution_model!r}) are not the "
                               f"current canonical set — a hybrid gets no "
                               f"v1 exit settlement")}
        from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
        if header.engine_epoch != CANONICAL_ENGINE_EPOCH:
            return {"ok": False, "error": NOT_CANONICAL_SETTLEMENT_POSITION,
                    "detail": (f"header epoch {header.engine_epoch!r} is "
                               f"not the current {CANONICAL_ENGINE_EPOCH!r}")}

        # ── Frozen identity (§16) and basis (§17) ───────────────────────
        for name in ("symbol", "product", "venue", "instrument_id"):
            if getattr(header, name) != getattr(f, name):
                return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                        "detail": (f"frozen identity mismatch on {name}: "
                                   f"entry settled "
                                   f"{getattr(header, name)!r}, exit claims "
                                   f"{getattr(f, name)!r} — an exit may only "
                                   f"settle the identity its entry froze")}
        if header.quantity_unit != f.quantity_unit or not _close_enough(
                float(header.multiplier), f.multiplier, 1e-12):
            return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                    "detail": (f"unit basis mismatch: entry counted "
                               f"{header.quantity_unit!r} x "
                               f"{header.multiplier!r}, exit claims "
                               f"{f.quantity_unit!r} x {f.multiplier!r}")}
        if header.position_side != f.position_side:
            return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                    "detail": (f"the header holds a {header.position_side}, "
                               f"the exit claims a {f.position_side}")}

        # ── Revision is the concurrency authority (§19) ──────────────────
        if int(header.settlement_revision) != int(f.expected_revision):
            return {"ok": False, "error": STALE_SETTLEMENT_REVISION,
                    "detail": (f"expected revision {f.expected_revision}, "
                               f"ledger is at {header.settlement_revision} — "
                               f"another settlement won; re-preparing is a "
                               f"NEW economic authorization, not a retry")}

        # ── P0.1: the carry quote is bound to THIS leg's exposure and
        # interval — checkable only here, where the entry fill and the
        # opened_at timestamp live. A quote whose notional or interval
        # describes a different exposure refuses; its dollar amount is
        # never booked anyway.
        if f.holding_cost_kind in ("FUNDING", "BORROW"):
            expected_notional = (float(f.filled_qty)
                                 * float(header.actual_entry_fill)
                                 * float(header.multiplier))
            if f.holding_cost_notional_usd is None or not _close_enough(
                    float(f.holding_cost_notional_usd), expected_notional,
                    1e-6):
                return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                        "detail": (f"the holding quote priced "
                                   f"${f.holding_cost_notional_usd!r} of "
                                   f"exposure but this leg closes "
                                   f"{f.filled_qty:g} x "
                                   f"{header.actual_entry_fill:g} x "
                                   f"{header.multiplier:g} = "
                                   f"${expected_notional:,.6f} — a carry "
                                   f"charge for a different exposure")}
            try:
                from datetime import datetime as _dt
                t0 = _dt.fromisoformat(str(header.opened_at))
                t1 = _dt.fromisoformat(str(f.settled_at))
                expected_hours = (t1 - t0).total_seconds() / 3600.0
            except (TypeError, ValueError) as e:
                return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                        "detail": f"cannot establish the holding interval: "
                                  f"{e}"}
            if f.hours_held is None or abs(float(f.hours_held)
                                           - expected_hours) > 1.0 / 3600.0:
                return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                        "detail": (f"the holding quote priced "
                                   f"{f.hours_held!r} hours but this leg "
                                   f"settles {expected_hours:.6f} hours "
                                   f"after entry — one interval, one "
                                   f"charge")}

        # ── Quantity can only shrink (§21) ────────────────────────────────
        q_before = float(pos.qty)
        m_before = float(pos.margin_used or 0.0)
        filled = float(f.filled_qty)
        if filled > q_before + _REP_TOL * max(1.0, q_before):
            return {"ok": False, "error": EXIT_VALIDATION_FAILED,
                    "detail": (f"exit fills {filled:g} against a remaining "
                               f"{q_before:g} — no tolerance authorizes "
                               f"another unit, and nothing flips through "
                               f"zero")}

        # ── PARTIAL vs FINAL is DERIVED, never trusted (§22) ─────────────
        is_final = _close_enough(filled, q_before)
        kind = LEG_FINAL_EXIT if is_final else LEG_PARTIAL_EXIT

        # ── Economics (§23-26) ───────────────────────────────────────────
        entry_fill = float(header.actual_entry_fill)
        mult = float(header.multiplier)
        sign = 1.0 if header.position_side == "long" else -1.0
        gross = (float(f.fill_price) - entry_fill) * filled * mult * sign

        if is_final:
            release = m_before                     # ALL of it; dust dies here
            q_after, m_after, notional_after = 0.0, 0.0, 0.0
        else:
            release = m_before * (filled / q_before)
            q_after = q_before - filled
            m_after = m_before - release
            notional_after = q_after * entry_fill * mult

        cash_delta = release + gross - f.fee_usd - f.holding_cost_usd
        leg_net = gross - f.fee_usd - f.holding_cost_usd

        new_revision = int(header.settlement_revision) + 1
        leg = PaperSettlementLeg(
            id=new_id(),
            position_id=f.position_id,
            observation_id=header.observation_id,
            signal_id=header.signal_id,
            execution_id=f.execution_id,
            kind=kind,
            settlement_version=SETTLEMENT_VERSION,
            settlement_revision=new_revision,
            symbol=f.symbol, product=f.product, venue=f.venue,
            instrument_id=f.instrument_id,
            position_side=f.position_side, execution_side=f.execution_side,
            requested_qty=float(f.requested_qty), filled_qty=filled,
            quantity_unit=f.quantity_unit, multiplier=f.multiplier,
            decision_price=f.decision_exit_price,
            fill_price=float(f.fill_price),
            notional_usd=filled * float(f.fill_price) * mult,
            explicit_fee_usd=f.fee_usd,
            fee_basis=f.fee_basis, fee_source=f.fee_source,
            fee_quality=f.fee_quality,
            fee_contract_count=f.fee_contract_count,
            fee_contract_count_basis=f.fee_contract_count_basis,
            gross_pnl_usd=gross,
            holding_cost_usd=f.holding_cost_usd,
            released_margin_usd=release,
            hours_held=float(f.hours_held or 0.0),
            spread_attribution_usd=f.spread_attribution_usd,
            slippage_attribution_usd=f.slippage_attribution_usd,
            impact_attribution_usd=f.impact_attribution_usd,
            execution_model=EXECUTION_MODEL_CANONICAL,
            cost_model=COST_MODEL_CANONICAL,
            fill_model=f.fill_model,
            created_at=f.settled_at,
            provenance_json=json.dumps(f.provenance) if f.provenance else None,
            exit_reason=f.exit_reason,
            trigger_price=f.trigger_price,
            holding_cost_type=f.holding_cost_kind,
            holding_cost_source=f.holding_cost_source,
            holding_cost_quality=f.holding_cost_quality,
            holding_cost_version=f.holding_cost_version,
            remaining_qty_after=q_after,
            remaining_margin_after=m_after,
        )
        db.add(leg)

        # ── Position mutation (§29/§31) ──────────────────────────────────
        pos.qty = q_after
        pos.notional = notional_after
        pos.margin_used = m_after
        pos.current_price = float(f.fill_price)
        pos.updated_at = f.settled_at
        if is_final:
            pos.status = "Closed"
            pos.unrealized_pnl = 0.0
            pos.unrealized_pct = 0.0

        portfolio = db.query(PaperPortfolio).first()
        if portfolio is None:
            raise ExitValidationError("no paper portfolio row exists")
        portfolio.cash = float(portfolio.cash) + cash_delta
        portfolio.updated_at = f.settled_at
        # Cumulative-as-realized (pinned above): this leg's net accrues now.
        portfolio.realized_pnl = float(portfolio.realized_pnl or 0.0) + leg_net

        header.settlement_revision = new_revision

        result = {"ok": True, "kind": kind, "position_id": f.position_id,
                  "leg_id": leg.id, "revision": new_revision,
                  "gross_pnl_usd": gross, "released_margin_usd": release,
                  "cash_delta_usd": cash_delta,
                  "remaining_qty": q_after, "remaining_margin": m_after}

        if not is_final:
            return result

        # ── FINAL: close, prove, and write the one truth (§31-39) ────────
        header.status = "CLOSED"
        header.closed_at = f.settled_at
        header.final_execution_id = f.execution_id

        db.flush()
        exit_rows = (db.query(PaperSettlementLeg)
                     .filter(PaperSettlementLeg.position_id == f.position_id,
                             PaperSettlementLeg.kind != LEG_ENTRY)
                     .order_by(PaperSettlementLeg.settlement_revision,
                               PaperSettlementLeg.created_at,
                               PaperSettlementLeg.id).all())

        closed_qty = sum(float(l.filled_qty or 0.0) for l in exit_rows)
        if not _close_enough(closed_qty, float(header.original_quantity),
                             1e-6):
            raise ExitValidationError(
                f"final close invariant violated: exits total {closed_qty!r} "
                f"against an original {header.original_quantity!r} — a "
                f"header must not close over a ledger that did not close "
                f"its position")
        released_total = sum(float(l.released_margin_usd or 0.0)
                             for l in exit_rows)
        if not _close_enough(released_total,
                             float(header.committed_margin_usd), 1e-6):
            raise ExitValidationError(
                f"margin release invariant violated: legs released "
                f"{released_total!r} of a committed "
                f"{header.committed_margin_usd!r} — capital cannot "
                f"disappear or be manufactured")

        # The entry fee accrues to realized_pnl HERE, once (pinned above).
        entry_fee = float(header.entry_fee_usd or 0.0)
        portfolio.realized_pnl = float(portfolio.realized_pnl) - entry_fee

        # ── One canonical outcome, from settlement truth (§35) ───────────
        from lib.realized_outcome import build_from_settlement
        outcome = build_from_settlement(header, exit_rows)
        row = PaperRealizedOutcome(
            id=new_id(),
            position_id=f.position_id,
            signal_id=header.signal_id,
            source=outcome.source, venue_type=outcome.venue_type,
            venue=header.venue, product=header.product,
            instrument_id=header.instrument_id, symbol=header.symbol,
            side=header.position_side,
            quantity=float(header.original_quantity),
            quantity_unit=header.quantity_unit,
            multiplier=float(header.multiplier),
            decision_entry_price=header.decision_entry_price,
            actual_entry_fill=float(header.actual_entry_fill),
            decision_exit_price=outcome.decision_exit_price,
            actual_exit_fill=outcome.actual_exit_fill,
            gross_pnl_usd=outcome.gross_pnl_usd,
            spread_attribution_usd=outcome.spread_attribution_usd,
            slippage_attribution_usd=outcome.slippage_attribution_usd,
            price_impact_attribution_usd=outcome.price_impact_attribution_usd,
            commission_usd=outcome.commission_usd,
            regulatory_fees_usd=outcome.regulatory_fees_usd,
            funding_usd=outcome.funding_usd,
            borrow_cost_usd=outcome.borrow_cost_usd,
            net_pnl_usd=outcome.net_pnl_usd,
            initial_risk_usd=outcome.initial_risk_usd,
            gross_r=outcome.gross_r, net_r=outcome.net_r,
            gross_return_pct=outcome.gross_return_pct,
            net_return_pct=outcome.net_return_pct,
            return_pct_basis=outcome.return_pct_basis,
            outcome=outcome.outcome, exit_reason=outcome.exit_reason,
            opened_at=header.opened_at, closed_at=f.settled_at,
            hold_minutes=outcome.hold_minutes,
            engine_epoch=header.engine_epoch,
            outcome_version=outcome.outcome_version,
            execution_model=header.execution_model,
            cost_model_version=header.cost_model,
            settlement_version=SETTLEMENT_VERSION,
            provenance_json=json.dumps({"exit_provenance": f.provenance})
                if f.provenance else None,
            learning_state="PENDING",
        )
        db.add(row)
        header.realized_outcome_id = row.id

        # ── One thesis, one vote (§38) — FINAL only, judged on NET ───────
        portfolio.total_trades = float(portfolio.total_trades or 0) + 1
        if outcome.net_pnl_usd > 0:
            portfolio.winning_trades = float(portfolio.winning_trades or 0) + 1

        # ── One aggregate PaperTrade compatibility projection (§37).
        # Readers measured: the loss-streak guard, the morning brief and
        # learning history all consume paper_trades. Populated FROM the
        # outcome, never recomputed; one row per POSITION, not per leg.
        trade = PaperTrade(
            id=new_id(),
            position_id=f.position_id,
            symbol=header.symbol,
            asset_class=pos.asset_class,
            direction=pos.direction,
            side=header.position_side,
            leverage=float(pos.leverage or 1.0),
            qty=float(header.original_quantity),
            entry_price=float(header.actual_entry_fill),
            exit_price=outcome.actual_exit_fill,
            notional=float(header.original_notional_usd),
            gross_pnl=outcome.gross_pnl_usd,
            fees=outcome.explicit_fees_usd,
            fee_basis="per_leg_v2_aggregate",
            realized_pnl=outcome.net_pnl_usd,
            pnl_pct=outcome.net_return_pct,
            close_reason=f.exit_reason,
            signal_id=header.signal_id,
            opened_at=header.opened_at,
            closed_at=f.settled_at,
        )
        db.add(trade)
        row.paper_trade_id = trade.id

        result.update({"realized_outcome_id": row.id,
                       "net_pnl_usd": outcome.net_pnl_usd,
                       "outcome": outcome.outcome,
                       "paper_trade_id": trade.id})
        return result
