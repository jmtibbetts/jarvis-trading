"""Closing the loop: a manually executed trade becomes learning evidence.

THE ONE THING THIS MUST NEVER DO. `RealizedOutcome` stores every cost as a
FLOAT, so a fee nobody evidenced is indistinguishable, in the row, from a fee
that was genuinely zero. A trade whose exit fee was never entered therefore
reads as MORE PROFITABLE than it was — and the more the missing fee, the
better it looks. Learning from that teaches the desk that its most expensive
venues are its cheapest.

So eligibility is decided on EVIDENCE COMPLETENESS AND PROVENANCE, never on
the numeric value sitting in a float field. `unknown_cost_categories()` on the
trade — not `commission_usd == 0.0` on the outcome — is the authority.

AND IT IS RE-DERIVED HERE, AT THE CONSUMER. `manual_trades.learning_state`
already carries `BLOCKED_INCOMPLETE_COSTS`, stamped by the producer at close.
This module does not trust it. A stored status is a claim about a past
evaluation; a row edited by hand, a partially-applied migration, or a bug in
the producer would all leave a stale `PENDING` sitting on an incomplete trade,
and the gate would wave it through. The gate recomputes from the legs and cost
events every time.

WHAT IT PROJECTS INTO. The same `trade_outcomes` table the virtual book uses,
through the same writer (`canonical_learning.insert_learning_row`) — one row
shape, so the two populations stay comparable. What differs is the LABEL:
`outcome_source = manual_operator`, and `lib/learning_population` keeps that
label out of every statistic that is supposed to describe JARVIS's own
execution. Recorded, counted, separable — never pooled.

WHY NO AGGREGATES RUN HERE. Pattern memory and regime performance are
INCREMENTED, and signal accuracy is recomputed from a filtered query. Manual
rows are excluded from all three by admission policy, so running them would
either do nothing or corrupt them. The virtual projector runs them inside its
transaction; this one deliberately does not, and that asymmetry is the reason
re-projection is safe here and would not be there.

ONE TRADE, ONE ROW, FOREVER. The learning row's id IS the manual trade id, and
`uq_trade_outcomes_canonical` makes that a database fact rather than a
convention. A re-projection after a correction UPDATES that row in place —
it cannot become a second vote, because there is only ever one row to have.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from lib import learning_population as LP
from lib import manual_execution as mx

logger = logging.getLogger(__name__)

MANUAL_LEARNING_VERSION = "manual_learning_v1"

# ── Eligibility verdicts ─────────────────────────────────────────────────
ELIGIBLE_COMPLETE = "ELIGIBLE_COMPLETE"
BLOCKED_OPEN_TRADE = "BLOCKED_OPEN_TRADE"
BLOCKED_INVALID_STATE = "BLOCKED_INVALID_STATE"
# The name the producer already stamps, reused rather than renamed.
BLOCKED_INCOMPLETE_COSTS = "BLOCKED_INCOMPLETE_COSTS"
BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS = (
    "BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS")
BLOCKED_NO_ECONOMICS = "BLOCKED_NO_ECONOMICS"

VERDICTS = (ELIGIBLE_COMPLETE, BLOCKED_OPEN_TRADE, BLOCKED_INVALID_STATE,
            BLOCKED_INCOMPLETE_COSTS,
            BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS, BLOCKED_NO_ECONOMICS)

# ── Reconciliation materiality — POLICY, stated and versioned ────────────
# A venue-reported figure that disagrees with the component sum by a rounding
# hair is usable; one that disagrees by a tenth of the result means the cost
# model is missing something real about that venue, and calibrating on it
# would teach the gap as if it were edge.
#
# These are the operator's willingness to trust, not a property of any venue.
MATERIAL_UNEXPLAINED_FRACTION = 0.10      # of |component-derived net P&L|
MATERIAL_UNEXPLAINED_FLOOR_USD = 0.05     # so a tiny trade does not trip
RECONCILIATION_POLICY_VERSION = "manual_reconciliation_policy_v1"

# ── Return basis ─────────────────────────────────────────────────────────
# The canonical book states MARGIN. A manual trade whose margin was not
# wholly the operator's own money gets a DIFFERENT label, so nothing
# downstream can read it as a return on owned equity: $10k owned plus $10k
# of non-withdrawable credit is not $20k of equity, and a percentage against
# the larger denominator is a different claim from the one it resembles.
BASIS_MARGIN = "MARGIN"
BASIS_MARGIN_MIXED_CAPITAL = "MARGIN_MIXED_CAPITAL"


@dataclass(frozen=True)
class ManualLearningEligibility:
    """Whether this trade may teach anything, and exactly why not."""

    trade_id: str
    verdict: str
    detail: str
    unknown_cost_categories: tuple = ()
    reconciliation_status: str | None = None
    unexplained_delta_usd: float | None = None
    materiality_threshold_usd: float | None = None
    policy_version: str = RECONCILIATION_POLICY_VERSION

    @property
    def eligible(self) -> bool:
        return self.verdict == ELIGIBLE_COMPLETE

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return {**asdict(self), "eligible": self.eligible,
                "version": MANUAL_LEARNING_VERSION}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def eligibility(trade: mx.ManualTrade) -> ManualLearningEligibility:
    """RE-DERIVED from the trade's own legs and cost events. Never read from
    a stored status — see the module docstring."""

    def _v(verdict, detail, **kw):
        return ManualLearningEligibility(
            trade_id=trade.trade_id, verdict=verdict, detail=detail, **kw)

    if trade.state in (mx.OPEN, mx.PARTIALLY_CLOSED):
        return _v(BLOCKED_OPEN_TRADE,
                  f"the trade is {trade.state}: quantity is still on, so "
                  f"there is no final result to learn from. A position that "
                  f"has realized part of its P&L has not realized its P&L")
    if trade.state != mx.CLOSED:
        return _v(BLOCKED_INVALID_STATE,
                  f"the trade is {trade.state}, which produced no realized "
                  f"outcome — a trade that was never executed, or whose end "
                  f"was never evidenced, is not a market observation")

    if not trade.is_flat or trade.gross_pnl_usd is None:
        return _v(BLOCKED_NO_ECONOMICS,
                  "the trade is CLOSED but has no settled exit economics")

    # THE POISON GATE. Evidence completeness, not the float's value.
    unknown = trade.unknown_cost_categories()
    if unknown:
        return _v(BLOCKED_INCOMPLETE_COSTS,
                  f"no evidence for {', '.join(unknown)} — this product "
                  f"incurs {'them' if len(unknown) > 1 else 'it'}, and a net "
                  f"result computed without {'them' if len(unknown) > 1 else 'it'} "
                  f"is FLATTERING rather than merely incomplete. The float "
                  f"field would read 0.0 and be indistinguishable from a "
                  f"genuinely free trade",
                  unknown_cost_categories=tuple(unknown))

    rec = trade.reconciliation()
    status = rec.get("status")
    if status == "UNEXPLAINED_VENUE_COST":
        net = trade.net_pnl_usd or 0.0
        threshold = max(MATERIAL_UNEXPLAINED_FLOOR_USD,
                        MATERIAL_UNEXPLAINED_FRACTION * abs(net))
        delta = abs(float(rec.get("delta_usd") or 0.0))
        if delta > threshold:
            return _v(BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS,
                      f"the venue's own realized figure and the component "
                      f"sum disagree by ${delta:,.4f}, past the ${threshold:,.4f} "
                      f"this policy will trust. The gap is real and "
                      f"unexplained; calibrating on it would teach the "
                      f"missing cost as though it were edge",
                      reconciliation_status=status,
                      unexplained_delta_usd=float(rec.get("delta_usd") or 0.0),
                      materiality_threshold_usd=threshold)
        return _v(ELIGIBLE_COMPLETE,
                  f"reconciled to within ${threshold:,.4f}; the residual "
                  f"${delta:,.4f} is preserved on the row and is small "
                  f"enough to be rounding rather than a missing mechanism",
                  reconciliation_status=status,
                  unexplained_delta_usd=float(rec.get("delta_usd") or 0.0),
                  materiality_threshold_usd=threshold)

    return _v(ELIGIBLE_COMPLETE,
              "every cost this product incurs is evidenced"
              + (f"; {status}" if status else ""),
              reconciliation_status=status)


def _return_basis(trade: mx.ManualTrade) -> tuple:
    """(net_return_pct, gross_return_pct, basis). UNKNOWN stays None.

    A percentage whose denominator is not the operator's own money is
    labelled as such rather than dropped — the number is a true statement
    about COMMITTED MARGIN, and only the label stops it being read as a
    return on equity.
    """
    from lib.account_economics import is_owned_capital

    margin = trade.collateral_usd
    if not margin:
        return None, None, None
    net, gross = trade.net_pnl_usd, trade.gross_pnl_usd
    if net is None or gross is None:
        return None, None, None
    basis = (BASIS_MARGIN if is_owned_capital(trade.collateral_capital_kind)
             else BASIS_MARGIN_MIXED_CAPITAL)
    return (net / float(margin) * 100.0,
            gross / float(margin) * 100.0, basis)


def build_projection(trade: mx.ManualTrade, outcome):
    """The learning row for one manual trade. COPIES; derives nothing new.

    Entry-time metadata (confidence, score, reasoning) comes from the FROZEN
    RecommendationSnapshot, never from today's signal tables — and stays
    None for an unlinked trade rather than being invented.
    """
    from lib.canonical_learning import CanonicalLearningProjection

    rec = trade.recommendation
    net_pct, _gross_pct, basis = _return_basis(trade)

    return CanonicalLearningProjection(
        # The learning row IS the manual trade, by id. One row, forever.
        canonical_outcome_id=trade.trade_id,
        position_id=trade.trade_id,
        # NEVER fabricated: an unlinked operator trade has no signal.
        signal_id=(rec.signal_id if rec else None),
        symbol=trade.symbol,
        asset_class=_asset_class(trade),
        product=trade.product,
        instrument_id=(trade.instrument_id or trade.symbol),
        direction=trade.direction,
        quantity=float(outcome.quantity),
        quantity_unit=trade.quantity_unit,
        multiplier=float(trade.multiplier),
        entry_price=float(outcome.actual_entry_fill),
        exit_price=float(outcome.actual_exit_fill),
        gross_pnl_usd=float(outcome.gross_pnl_usd),
        net_pnl_usd=float(outcome.net_pnl_usd),
        explicit_fees_usd=float(outcome.explicit_fees_usd),
        net_return_pct=net_pct,
        return_pct_basis=basis,
        outcome=outcome.outcome,
        exit_reason=outcome.exit_reason,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        hold_minutes=outcome.hold_minutes,
        # Entry-time metadata: the frozen recommendation, or nothing.
        timeframe=None,
        confidence=(rec.confidence if rec else None),
        score=None,
        reasoning=None,
        ta_profile=None,
        ta_summary=None,
        market_regime=None,
        engine_epoch=trade.engine_epoch,
        outcome_version=outcome.outcome_version,
        execution_model=mx.MANUAL_EXECUTION_VERSION,
        cost_model=mx.MANUAL_EXECUTION_VERSION,
        settlement_version=MANUAL_LEARNING_VERSION,
    )


def _asset_class(trade: mx.ManualTrade) -> str | None:
    """Resolved from the PRODUCT, which the operator stated. Not guessed
    from the symbol, and None where the product does not imply one."""
    from lib.instruments import (COMMODITY_FUTURE, CRYPTO_PERP, CRYPTO_SPOT,
                                 DEX_SPOT, EQUITY_SHORT, EQUITY_SPOT,
                                 ETF_SPOT, FX_SPOT, INDEX_FUTURE)
    return {
        EQUITY_SPOT: "equity", EQUITY_SHORT: "equity", ETF_SPOT: "equity",
        CRYPTO_SPOT: "crypto", CRYPTO_PERP: "crypto", DEX_SPOT: "crypto",
        INDEX_FUTURE: "futures", COMMODITY_FUTURE: "futures",
        FX_SPOT: "forex",
    }.get(trade.product)


def validate_projection(p, trade) -> None:
    """PURE. Refuses a row that cannot be honest; never repairs one.

    Deliberately NOT `canonical_learning.validate_projection`: that one
    requires the settlement ledger's MARGIN basis and a finite percentage,
    which a manual trade without evidenced collateral genuinely does not
    have. Demanding it would force a fabricated denominator — the exact
    thing it exists to prevent.
    """
    import math

    from lib.canonical_learning import LearningValidationError

    for name in ("canonical_outcome_id", "position_id", "symbol", "product",
                 "instrument_id", "quantity_unit", "closed_at"):
        if not str(getattr(p, name) or "").strip():
            raise LearningValidationError(f"{name} is empty")
    if p.direction not in ("long", "short"):
        raise LearningValidationError(
            f"direction {p.direction!r} is not canonical")
    for name, v in (("quantity", p.quantity), ("multiplier", p.multiplier),
                    ("entry_price", p.entry_price),
                    ("exit_price", p.exit_price)):
        if not (math.isfinite(v) and v > 0):
            raise LearningValidationError(f"{name} {v!r} invalid")
    for name in ("gross_pnl_usd", "net_pnl_usd", "explicit_fees_usd"):
        if not math.isfinite(getattr(p, name)):
            raise LearningValidationError(f"{name} is not finite")
    if p.outcome not in ("WIN", "LOSS", "BREAKEVEN"):
        raise LearningValidationError(f"outcome {p.outcome!r} invalid")
    # A percentage and its denominator travel together or neither travels.
    if (p.net_return_pct is None) != (p.return_pct_basis is None):
        raise LearningValidationError(
            f"net_return_pct {p.net_return_pct!r} and return_pct_basis "
            f"{p.return_pct_basis!r} must be stated together — a percentage "
            f"whose denominator is unknown is not a percentage")
    if p.return_pct_basis is not None and p.return_pct_basis not in (
            BASIS_MARGIN, BASIS_MARGIN_MIXED_CAPITAL):
        raise LearningValidationError(
            f"return_pct_basis {p.return_pct_basis!r} is not a manual basis")
    if p.net_return_pct is not None and not math.isfinite(p.net_return_pct):
        raise LearningValidationError("net_return_pct is not finite")
    # Identity, not recomputation.
    if abs(p.net_pnl_usd - float(trade.net_pnl_usd)) > 1e-9:
        raise LearningValidationError("net_pnl_usd drifted during projection")


# ── Projection states on manual_trades.learning_state ────────────────────
PENDING = "PENDING"
APPLIED = "APPLIED"
#: A correction landed on a trade that had ALREADY been projected. The old
#: learning row still stands (it is the vote already cast) and the trade is
#: marked for re-projection. FAIL CLOSED: nothing is silently revised, and
#: nothing is silently duplicated either.
PENDING_REPROJECTION = "PENDING_REPROJECTION"

# Result vocabulary, mirroring canonical_learning's rather than inventing one.
MANUAL_LEARNING_APPLIED = "MANUAL_LEARNING_APPLIED"
MANUAL_LEARNING_REPROJECTED = "MANUAL_LEARNING_REPROJECTED"
MANUAL_LEARNING_ALREADY_APPLIED = "MANUAL_LEARNING_ALREADY_APPLIED"
MANUAL_LEARNING_BLOCKED = "MANUAL_LEARNING_BLOCKED"
MANUAL_LEARNING_INVALID = "MANUAL_LEARNING_INVALID"
MANUAL_LEARNING_NOT_FOUND = "MANUAL_LEARNING_NOT_FOUND"


def _set_state(conn, trade_id: str, state: str, error: str | None,
               applied_at: str | None = None):
    from sqlalchemy import text

    conn.execute(text(
        "UPDATE manual_trades SET learning_state=:s, learning_error=:e, "
        "learning_applied_at=COALESCE(:a, learning_applied_at), "
        "updated_at=:u WHERE id=:i"),
        {"s": state, "e": (error[:2000] if error else None),
         "a": applied_at, "u": _now_iso(), "i": trade_id})


def apply_manual_outcome(trade_id: str) -> dict:
    """Project ONE closed manual trade into canonical learning, exactly once.

    The gate is re-derived here rather than read from `learning_state` — a
    stored verdict describes a past evaluation, and this one has to be true
    NOW. See the module docstring.
    """
    from sqlalchemy import text

    from app.database import engine
    from lib import manual_trade_store as store
    from lib.canonical_learning import (LearningValidationError,
                                        insert_learning_row)

    try:
        trade = store.get(trade_id)
    except store.ManualTradeNotFound as e:
        return {"ok": False, "error": MANUAL_LEARNING_NOT_FOUND,
                "detail": str(e)}

    verdict = eligibility(trade)
    if not verdict.eligible:
        with engine.begin() as conn:
            _set_state(conn, trade_id, verdict.verdict, verdict.detail)
        return {"ok": False, "error": MANUAL_LEARNING_BLOCKED,
                "verdict": verdict.verdict, "detail": verdict.detail,
                "eligibility": verdict.as_dict()}

    try:
        outcome = mx.realized_outcome(trade)
        projection = build_projection(trade, outcome)
        validate_projection(projection, trade)
    except (mx.ManualExecutionError, LearningValidationError) as e:
        with engine.begin() as conn:
            _set_state(conn, trade_id, BLOCKED_INVALID_STATE, str(e))
        return {"ok": False, "error": MANUAL_LEARNING_INVALID,
                "detail": str(e)}

    applied_at = _now_iso()
    with engine.begin() as conn:
        existing = conn.execute(text(
            "SELECT id, pnl_usd, outcome_source FROM trade_outcomes "
            "WHERE canonical_outcome_id=:c"), {"c": trade_id}).fetchone()

        if existing is not None:
            if existing[2] != LP.MANUAL_OPERATOR:
                # Someone else owns this id. Refuse rather than overwrite a
                # row belonging to another population.
                return {"ok": False, "error": MANUAL_LEARNING_INVALID,
                        "detail": (f"a {existing[2]!r} learning row already "
                                   f"claims id {trade_id!r}")}
            unchanged = abs(float(existing[1] or 0.0)
                            - projection.net_pnl_usd) <= 1e-9
            state = conn.execute(text(
                "SELECT learning_state FROM manual_trades WHERE id=:i"),
                {"i": trade_id}).fetchone()
            state = state[0] if state else None

            if unchanged and state == APPLIED:
                # TRUE IDEMPOTENCE: the same call, the same row, no write.
                return {"ok": True, "idempotent": True,
                        "result": MANUAL_LEARNING_ALREADY_APPLIED,
                        "trade_outcome_id": existing[0]}

            # RE-PROJECTION, IN PLACE. There is exactly one row for this
            # trade and there always will be, so a corrected result revises
            # the vote instead of casting a second one.
            #
            # SAFE HERE, AND NOT SAFE FOR THE VIRTUAL BOOK: manual rows feed
            # no INCREMENTAL aggregate (pattern memory, regime performance),
            # because `learning_population` excludes them. A canonical
            # outcome could not be revised this way without double-counting
            # the increments its first projection already made.
            conn.execute(text("DELETE FROM trade_outcomes WHERE id=:i"),
                         {"i": existing[0]})
            insert_learning_row(conn, projection,
                                outcome_source=LP.MANUAL_OPERATOR,
                                projected_at=applied_at)
            _set_state(conn, trade_id, APPLIED, None, applied_at)
            return {"ok": True, "reprojected": True,
                    "result": MANUAL_LEARNING_REPROJECTED,
                    "trade_outcome_id": trade_id,
                    "outcome": projection.outcome,
                    "net_pnl_usd": projection.net_pnl_usd}

        insert_learning_row(conn, projection,
                            outcome_source=LP.MANUAL_OPERATOR,
                            projected_at=applied_at)
        _set_state(conn, trade_id, APPLIED, None, applied_at)

    logger.info("[ManualLearning] applied %s: %s %s %+0.2f USD",
                trade_id, projection.symbol, projection.outcome,
                projection.net_pnl_usd)
    return {"ok": True, "result": MANUAL_LEARNING_APPLIED,
            "trade_outcome_id": trade_id,
            "outcome": projection.outcome,
            "net_pnl_usd": projection.net_pnl_usd,
            "outcome_source": LP.MANUAL_OPERATOR,
            "eligibility": verdict.as_dict()}


def apply_pending_manual_outcomes(limit: int = 50) -> dict:
    """Bounded catch-up over CLOSED manual trades awaiting projection.

    NOT SCHEDULED — a callable tool, like its canonical counterpart. Each
    apply is independently transactional and idempotent.
    """
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id FROM manual_trades WHERE state='CLOSED' "
            "AND learning_state IN ('PENDING', 'PENDING_REPROJECTION', "
            "'BLOCKED_INCOMPLETE_COSTS', "
            "'BLOCKED_UNRECONCILED_CRITICAL_ECONOMICS') "
            "ORDER BY closed_at ASC LIMIT :lim"),
            {"lim": int(limit)}).fetchall()

    summary = {"scanned": len(rows), "applied": 0, "reprojected": 0,
               "already_applied": 0, "blocked": 0, "invalid": 0,
               "blocked_detail": {}}
    for (tid,) in rows:
        res = apply_manual_outcome(tid)
        if res.get("ok") and res.get("idempotent"):
            summary["already_applied"] += 1
        elif res.get("ok") and res.get("reprojected"):
            summary["reprojected"] += 1
        elif res.get("ok"):
            summary["applied"] += 1
        elif res.get("error") == MANUAL_LEARNING_BLOCKED:
            summary["blocked"] += 1
            v = res.get("verdict", "UNKNOWN")
            summary["blocked_detail"][v] = (
                summary["blocked_detail"].get(v, 0) + 1)
        else:
            summary["invalid"] += 1
    return summary


# ── The reader. Manual evidence that nothing consumes is inert ───────────
def operator_population(*, engine_epoch: str | None = None) -> dict:
    """What the OPERATOR'S OWN executed trades add up to — on their own.

    This exists so manual evidence is genuinely CONSUMED rather than merely
    stored: a learning row nothing reads is not learning, it is a log. It is
    also the population a future autonomy-readiness metric needs, kept
    separate from JARVIS's own record by construction rather than by a
    filter someone has to remember to write.

    Reports linked and unlinked trades apart. A trade answering a JARVIS
    thesis and one the operator found alone are different evidence about
    different things, and a single blended win rate answers neither.
    """
    from sqlalchemy import text

    from app.database import engine
    from lib.engine_epoch import ENGINE_EPOCH

    epoch = engine_epoch or ENGINE_EPOCH
    where, params = LP.sql_filter("outcome_source", LP.OPERATOR_EXECUTION)
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT o.outcome, o.pnl_usd, o.pnl_pct, o.return_pct_basis, "
            f"       o.signal_id, o.symbol, o.product, o.explicit_fees_usd, "
            f"       m.thesis_id "
            f"FROM trade_outcomes o "
            f"LEFT JOIN manual_trades m ON m.id = o.canonical_outcome_id "
            f"WHERE o.engine_epoch = :epoch AND {where}"),
            {"epoch": epoch, **params}).fetchall()

    def _tally(subset):
        n = len(subset)
        wins = sum(1 for r in subset if r[0] == "WIN")
        losses = sum(1 for r in subset if r[0] == "LOSS")
        pnl = sum(float(r[1] or 0.0) for r in subset)
        fees = sum(float(r[7] or 0.0) for r in subset)
        return {
            "trades": n,
            "wins": wins,
            "losses": losses,
            # None, not 0.0, when there is nothing to divide by. An empty
            # population has no win rate; it does not have a 0% one.
            "win_rate": (round(wins / n, 4) if n else None),
            "net_pnl_usd": round(pnl, 6) if n else None,
            "explicit_fees_usd": round(fees, 6) if n else None,
        }

    linked = [r for r in rows if r[8]]
    unlinked = [r for r in rows if not r[8]]
    return {
        "engine_epoch": epoch,
        "population": LP.MANUAL_OPERATOR,
        "all": _tally(rows),
        # Kept apart on purpose: only the linked subset says anything about
        # a JARVIS thesis, and only then about the THESIS — never about
        # JARVIS's execution, which did not happen.
        "thesis_linked": _tally(linked),
        "independent": _tally(unlinked),
        "version": MANUAL_LEARNING_VERSION,
        "note": ("operator-executed trades only. Deliberately absent from "
                 "calibration, expectancy, the edge/cost matrix, strategy "
                 "lifecycle, signal accuracy and the bootstrap gate — those "
                 "measure JARVIS's own execution, which these did not test"),
    }
