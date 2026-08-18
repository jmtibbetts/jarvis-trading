"""The autonomous entry path, with the venue book as fill authority.

WHAT THIS REPLACES.

    price  = _get_current_price(sym, prices) or sig["entry_price"] or 0.0
    result = open_paper_position(sig, current_price=price)

`_get_current_price` walks Alpaca's last price, a MarketAsset row, then a
yfinance futures cache. All three are MARKS. Handing one to
`open_paper_position` made it the entry fill, so every paper position this
book has ever opened was filled at a price no order could have been executed
at — mid-market at best, and better than mid whenever the spread was wide.
That is the shape of a simulator that makes money because it is wrong.

WHAT REPLACES IT.

    execution_readiness()      which venue and product, and may it be filled
    execution_market_snapshot() that venue's own bid/ask, graded and aged
    prepare_entry()            the AUTHORIZED quantity, before any order
    execution_venue.submit()   OrderPlan -> VirtualCexAdapter -> fill
    settle_position_entry(fill_price=<ACTUAL FILL>)

EXECUTION GOES THROUGH THE BOUNDARY, NOT AROUND IT. This called
`virtual_orders.execute_market()` directly, which skipped all three gates
the boundary exists to apply — platform mode, venue capability, and the
last risk check before submission — and coupled the strategy layer to a
fill model instead of to a venue. `virtual_orders` lives BELOW the adapter.
The only thing that crosses the boundary is an OrderPlan, which is what
makes a live venue a change to one adapter rather than a rewrite.

The last line is deliberate. All of the authorization that already exists —
side parsing, stop and target validation, horizon clamps, sizing, leverage,
concentration, free cash, deployment limits — stays exactly where it is.
Duplicating it here would create a second risk engine, and two risk engines
disagree eventually. What changes is the PRICE that logic settles at: the
executed fill instead of the mark.

MARK AUTHORITY IS NOT EXECUTION AUTHORITY. The mark is still recorded, as
`decision_price`, which is what makes slippage measurable at all.

REFUSALS ARE NOT LOSSES. A stale Kraken quote is not a losing BTC thesis, so
a venue/data refusal returns a named venue reason, opens nothing, moves no
cash, and records no outcome. `is_venue_data_failure()` is what keeps it off
the strategy's record.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Every position opened through here carries these, and legacy positions
# carry NULL provenance. The pair is what stops two simulators' economics
# being pooled as though one machine produced them.
from lib.paper_settlement import (COST_MODEL_CANONICAL,  # noqa: E402,F401
                                  COST_MODEL_LEGACY,
                                  EXECUTION_MODEL_CANONICAL)

# The learning epoch that begins with the venue-book executor. Outcomes from
# the direct-mark era stay in their own epoch; they remain useful research
# about theses and features, and they are not execution evidence.
CANONICAL_ENGINE_EPOCH = "2026-08-17-venue-book"

# Canonical refusal reasons that are ABOUT THE ORDER rather than the venue.
UNFILLED = "UNFILLED"
REJECTED_BY_EXECUTION = "REJECTED_BY_EXECUTION"
# The instrument costs more to trade than it can plausibly return, at any
# size. A property of the PRODUCT, not a verdict on the thesis.
FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL = "FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL"
# The trade was authorized but its audit record could not be written. An
# accepted canonical trade fails CLOSED on this: a position with no evidence
# of why it was allowed is the state this subsystem exists to prevent.
DECISION_OBSERVATION_PERSIST_FAILED = "DECISION_OBSERVATION_PERSIST_FAILED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_canonical_position(signal: dict, *, decision_price: float | None = None,
                            max_age_s: float | None = None,
                            edge=None, edge_gate_role=None) -> dict:
    """PREPARE, EXECUTE the authorized quantity, then SETTLE the actual fill.

    THE ORDER OF THESE STEPS IS THE WHOLE POINT. This used to push a NOMINAL
    one unit through the venue purely to discover a price, and size
    afterwards. Two things were wrong with that. The order that was simulated
    was not the order that settled, so `spread_cost_usd` and `slippage_usd`
    described a single unit of a trade nobody placed — attribution that is
    off by the position size, which on a 330-unit entry is off by 330x. And
    sizing after execution means risk never saw the price it was actually
    exposed at.

    Returns the settlement result on success, or a refusal dict carrying
    `venue_failure=True` when the venue could not price a fill.
    """
    from lib import execution_policy as POL
    from lib import execution_venue as EV
    from lib import fee_authority as FA
    from lib import venues as V
    from lib import virtual_orders as VO
    from lib.decision_types import OrderPlan
    from lib.paper_engine import prepare_entry, settle_position_entry
    from lib.trade_side import SHORT, parse_side_strict

    from lib import decision_observation as DO

    symbol = (signal.get("asset_symbol") or "").upper().strip()
    asset_class = signal.get("asset_class")
    raw_dir = signal.get("paper_direction") or signal.get("direction")
    decision_at = _now()
    # Gate verdicts accumulate as the decision proceeds, so a refusal
    # records WHICH gates were reached and passed rather than collapsing to
    # a single `eligible = false`.
    gates: dict = {}

    def _observe(decision, *, reason=None, ready=None, authorization=None,
                 fee_quote=None, venue_failure=False, position_id=None,
                 execution_id=None, execution_state=None):
        """EVERY MATERIAL DECISION IS AN OBSERVATION — traded or refused.

        A rejected opportunity used to leave nothing behind, which is why
        11,775 historical rejections are unanswerable today. This is the
        audit trail, built from the artifacts the decision actually used;
        it opens no position, moves no cash and counts no trade.
        """
        try:
            row = DO.build(signal=signal, ready=ready,
                           authorization=authorization, fee_quote=fee_quote,
                           decision=decision, binding_reason=reason,
                           decision_price=decision_price, gates=dict(gates),
                           venue_data_failure=venue_failure,
                           position_id=position_id, decision_at=decision_at,
                           execution_id=execution_id,
                           edge=edge, edge_gate_role=edge_gate_role,
                           execution_state=execution_state)
            return DO.record(row)
        except Exception as e:                       # never take a trade down
            logger.error("[CanonicalEntry] decision observation failed for "
                         "%s: %s", symbol, e, exc_info=True)
            return None

    side = parse_side_strict(raw_dir)
    if side is None:
        # Not a venue failure — an unreadable side is a bad order.
        gates["side_parse"] = "FAIL"
        _observe(DO.NO_TRADE, reason="UNPARSEABLE_SIDE")
        return {"error": f"unparseable direction {raw_dir!r} — refusing to "
                         "assume a side for an order",
                "venue_failure": False}
    gates["side_parse"] = "PASS"

    # ── 1. May this be filled at all, and by whom? ───────────────────────
    ready = POL.execution_readiness(symbol, asset_class, signal=signal,
                                    **({} if max_age_s is None else
                                       {"max_age_s": max_age_s}))
    if not ready.ok:
        logger.info("[CanonicalEntry] %s not executable: %s (%s)",
                    symbol, ready.reason, ready.detail)
        gates["executable_quote"] = "FAIL"
        _observe(DO.NO_TRADE, reason=ready.reason, ready=ready,
                 venue_failure=POL.is_venue_data_failure(ready.reason))
        return {"error": ready.reason, "detail": ready.detail,
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": POL.is_venue_data_failure(ready.reason),
                "opened": False}
    gates["executable_quote"] = "PASS"
    gates["capability"] = "PASS"

    snap = ready.snapshot
    quote = VO.Quote(bid=snap.bid, ask=snap.ask, as_of=snap.venue_event_at,
                     source=snap.source)
    venue_side = "short" if side == SHORT else "long"

    def _submit(auth):
        """Offer an authorization to the venue THROUGH THE BOUNDARY.

        This used to call `virtual_orders.execute_market()` directly, which
        skipped every gate the boundary exists to apply — platform mode,
        venue capability, and the last risk check before submission — and
        made the strategy layer depend on a fill model instead of on a
        venue. `virtual_orders` belongs BELOW the adapter; the only thing
        above it is a plan.

        The plan carries the product from A9, so the capability gate is
        asking about a real product rather than an asset class.
        """
        plan = OrderPlan(
            symbol=symbol, venue=ready.venue, side=venue_side,
            order_type="market", qty=float(auth.qty),
            entry=float(auth.reference_price), initial_stop=float(auth.stop),
            target=float(auth.target), notional=float(auth.notional),
            leverage=float(auth.leverage), product=ready.product)
        return EV.submit(plan, venue_family=EV.VIRTUAL_CEX,
                         risk=auth.risk_decision(), product=ready.product,
                         venue=ready.venue, quote=quote)

    def _refuse_submission(sub, authorization=None):
        """A venue refusal is a RESULT, and it is about the order, not the
        market data — so it must not be excused as a venue outage."""
        logger.info("[CanonicalEntry] %s refused by %s: %s (%s)",
                    symbol, sub.venue_family, sub.reason, sub.detail)
        gates["venue_submission"] = "FAIL"
        _observe(DO.NO_TRADE, reason=sub.reason or REJECTED_BY_EXECUTION,
                 ready=ready, authorization=authorization)
        return {"error": sub.reason or REJECTED_BY_EXECUTION,
                "detail": sub.detail,
                "execution_state": getattr(sub.execution, "state", None),
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    # ── 2. PREPARE. Authorize a real size, against the venue's own mid ───
    # The mid is a REFERENCE, never a fill — it is the screen price a desk
    # sizes on before it learns what it actually paid. The fill below will
    # be worse than this by construction, and step 4 is what makes the
    # position honest about that.
    prep = prepare_entry(signal, reference_price=quote.mid)
    if "authorization" not in prep:
        gates["risk_sizing"] = "FAIL"
        _observe(DO.NO_TRADE, reason=_sizing_reason(prep), ready=ready)
        return {**prep, "venue_failure": False, "opened": False}
    authorized = prep["authorization"]
    gates["risk_sizing"] = "PASS"

    # ── 3. EXECUTE THE AUTHORIZED QUANTITY. BUY lifts the ask, SELL hits
    #      the bid, and the result describes THIS order. ──────────────────
    submission = _submit(authorized)
    if not submission.accepted:
        return _refuse_submission(submission, authorized)

    execution = submission.execution
    if execution is None or not execution.filled or not execution.fill_price:
        # An accepted submission with no usable execution is a broken
        # adapter contract, not a fill. A6 made `execution` a real typed
        # field precisely so this could be asserted rather than assumed.
        return _refuse_submission(submission, authorized)

    fill = float(execution.fill_price)

    # ── 4. POST-FILL REVALIDATION ────────────────────────────────────────
    # Crossing the book moved the entry AGAINST us, so the same quantity now
    # sits further from the same stop and risks more money than was approved.
    # The fix is to re-authorize at the price actually paid — through the
    # SAME risk engine, not a correction factor applied here — and keep
    # whichever size is smaller. Execution may shrink an order; nothing may
    # enlarge one.
    repriced = prepare_entry(signal, reference_price=fill)
    if "authorization" not in repriced:
        gates["post_fill_risk"] = "FAIL"
        _observe(DO.NO_TRADE, reason=_sizing_reason(repriced), ready=ready,
                 authorization=authorized)
        return {**repriced, "venue_failure": False, "opened": False}
    final = repriced["authorization"]
    if final.qty > authorized.qty:
        final = final.shrunk_to(authorized.qty)

    if final.qty < authorized.qty - 1e-9:
        # A SMALLER ORDER IS RE-SUBMITTED. The alternative — editing the
        # filled ExecutionResult down to the new size — would fabricate an
        # execution that never happened, which is the same class of lie as
        # filling at the mark. The fill price is independent of size in this
        # model (no depth), so this converges in exactly one resubmission.
        logger.info("[CanonicalEntry] %s re-submitting %.8g -> %.8g units "
                    "after the fill at %.8g repriced the risk",
                    symbol, authorized.qty, final.qty, fill)
        submission = _submit(final)
        if not submission.accepted:
            return _refuse_submission(submission, final)
        execution = submission.execution
        if execution is None or not execution.filled or not execution.fill_price:
            return _refuse_submission(submission, final)
        fill = float(execution.fill_price)

    # THE AUTHORIZATION THAT SURVIVES IS THE FIRST ONE. Re-pricing at the
    # fill re-ran the risk engine, and a second engine run must not become a
    # second, larger approval — so the money at risk is checked back against
    # what was approved BEFORE the order went out.
    # TOLERANCE IN THE UNITS OF THE ROUNDING THAT PRODUCED THE NUMBER. Both
    # figures come from a quantity rounded to 6dp, so they are meaningful
    # only to within one such step; a 1e-6 RELATIVE comparison is tighter
    # than the arithmetic underneath and produced the unreadable refusal
    # "$863.46 exceeds $863.46". Size itself is still bounded exactly, by
    # `final.qty <= authorized.qty` above.
    if final.loss_at_stop > authorized.loss_at_stop + authorized.risk_quantum:
        logger.error("[CanonicalEntry] %s refusing to settle: repriced risk "
                     "$%.2f exceeds the authorized $%.2f",
                     symbol, final.loss_at_stop, authorized.loss_at_stop)
        gates["post_fill_risk"] = "FAIL"
        _observe(DO.NO_TRADE, reason=REJECTED_BY_EXECUTION, ready=ready,
                 authorization=final)
        return {"error": REJECTED_BY_EXECUTION,
                "detail": (f"repricing at the fill raised money-at-stop from "
                           f"${authorized.loss_at_stop:,.2f} to "
                           f"${final.loss_at_stop:,.2f}; execution may shrink "
                           f"an order, never enlarge one"),
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    # FAIL CLOSED. The position about to be written must be the order that
    # was actually simulated — if those two ever disagree, the attribution
    # is fiction and no position is worth writing.
    if abs(float(execution.filled_quantity) - float(final.qty)) > 1e-9:
        logger.error("[CanonicalEntry] %s refusing to settle: executed %s "
                     "units but authorization holds %s",
                     symbol, execution.filled_quantity, final.qty)
        gates["execution_matches_authorization"] = "FAIL"
        _observe(DO.NO_TRADE, reason=REJECTED_BY_EXECUTION, ready=ready,
                 authorization=final)
        return {"error": REJECTED_BY_EXECUTION,
                "detail": (f"executed {execution.filled_quantity} units against "
                           f"an authorization of {final.qty} — settling would "
                           f"record a position no simulated order produced"),
                "venue": ready.venue, "product": ready.product,
                "venue_failure": False, "opened": False}

    # ── 5. PRICE THE ENTRY LEG. One leg, at the executed size and price. ──
    # A2. Settlement previously wrote sizing["round_trip_fees"] — a DEFERRED
    # round-trip ESTIMATE, computed before the order existed and charged at
    # close. Under the per-leg model the entry leg is priced from what
    # actually filled and debited now, so `fees` stays 0 and the close path
    # cannot bill the same economics twice.
    fee_quote = FA.leg_fee(symbol, notional=float(final.notional),
                           price=fill, product=ready.product,
                           venue=ready.venue, side=venue_side,
                           # A market order crosses the book; it is a TAKER.
                           maker=False)
    if not fee_quote.ok or fee_quote.fee_usd is None:
        # FAIL CLOSED. Settling anyway would debit nothing and stamp a
        # position as per_leg_v2 while charging it nothing at all — a free
        # trade wearing the label of an accurately-costed one.
        logger.warning("[CanonicalEntry] %s has no entry fee authority: %s",
                       symbol, fee_quote.detail)
        gates["fee_authority"] = "FAIL"
        _observe(DO.NO_TRADE,
                 reason=fee_quote.reason or FA.FEE_AUTHORITY_UNAVAILABLE,
                 ready=ready, authorization=final)
        return {"error": fee_quote.reason or FA.FEE_AUTHORITY_UNAVAILABLE,
                "detail": fee_quote.detail,
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    # THE CATASTROPHIC-PRODUCT GATE. Some instruments cost an unacceptable
    # share of their own notional to trade at ANY size — a flat per-contract
    # fee does not dilute, so buying more contracts buys more fees rather
    # than cheaper ones. This is the same constant and the same denominator
    # `us_perp_viability` uses: fee as a percentage of NOTIONAL, on both
    # sides of the comparison.
    #
    # It is deliberately NOT a profitability judgement and must not become
    # one. Comparing a fee against margin, or against an unleveraged move in
    # the underlying, mixes denominators and kills trades that are
    # comfortably viable — rejecting a profitable trade is simulator error
    # too. This only catches the instrument that could never pay for itself.
    round_trip_pct = (2.0 * float(fee_quote.fee_usd)
                      / abs(float(final.notional))) * 100.0
    if round_trip_pct > V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL:
        logger.warning("[CanonicalEntry] %s refused: round trip is %.2f%% of "
                       "notional (limit %.2f%%)", symbol, round_trip_pct,
                       V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL)
        gates["fee_authority"] = "PASS"
        gates["catastrophic_product"] = "FAIL"
        _observe(DO.NO_TRADE, reason=FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL,
                 ready=ready, authorization=final, fee_quote=fee_quote)
        return {"error": FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL,
                "detail": (f"${fee_quote.fee_usd:,.2f} per side on "
                           f"${abs(final.notional):,.2f} of notional is a "
                           f"{round_trip_pct:.2f}% round trip, above the "
                           f"{V.MAX_VIABLE_FEE_PCT_OF_NOTIONAL:g}% ceiling; a "
                           f"per-contract cost does not dilute with size, so "
                           f"this instrument cannot pay for itself at any"),
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    # ── 6. SETTLE the actual fill. ───────────────────────────────────────
    # The mark is kept as decision_price so slippage stays measurable.
    #
    # Provenance is built FIRST and handed to settlement, so position, cash
    # and provenance commit in ONE transaction. Stamping it afterwards ran a
    # second transaction: when that failed, the first had already committed
    # and left an economically-open position with NULL provenance — invisible
    # to is_canonical(), and therefore invisible to the exit guard.
    # ONE execution identity, minted here so it can be BOTH stamped into
    # provenance and linked from the decision observation. It used to be
    # generated inside build_provenance, where nothing else could see it —
    # so the observation's `execution_id` was silently always NULL and the
    # causal chain decision -> execution -> position had a hole in the
    # middle.
    entry_execution_id = _execution_id()
    provenance = build_provenance(signal=signal, ready=ready, snap=snap,
                                  execution=execution,
                                  decision_price=decision_price,
                                  authorized_qty=authorized.qty,
                                  fee_quote=fee_quote,
                                  entry_execution_id=entry_execution_id)
    gates["fee_authority"] = "PASS"
    gates["catastrophic_product"] = "PASS"
    # WRITTEN BEFORE SETTLEMENT, deliberately. A canonical position must not
    # be able to exist without the decision record that authorised it, and
    # this write is a short transaction holding no provider call.
    observation_id = _observe(DO.TRADE, ready=ready, authorization=final,
                              fee_quote=fee_quote,
                              execution_id=entry_execution_id,
                              execution_state=DO.EXEC_SIMULATED_FILLED)

    # FAIL CLOSED. A CANONICAL POSITION MUST NOT EXIST WITHOUT THE DECISION
    # RECORD THAT AUTHORIZED IT.
    #
    # For a REFUSAL, losing the observation costs evidence and nothing else —
    # no cash moved. For an ACCEPTED trade it would create exactly the state
    # this whole subsystem exists to prevent: a position in the book with no
    # record of why it was allowed, indistinguishable later from the 11,775
    # historical decisions nobody can explain. The trade is abandoned before
    # any economic mutation: no settlement, no margin, no fee, no position.
    if not observation_id:
        logger.error("[CanonicalEntry] %s refusing to settle: the decision "
                     "observation could not be persisted", symbol)
        return {"error": DECISION_OBSERVATION_PERSIST_FAILED,
                "detail": ("the trade was authorized but its decision record "
                           "could not be written; settling anyway would put a "
                           "position in the book with no evidence of why it "
                           "was allowed"),
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    # THE LINKAGE IS PART OF THE SETTLEMENT TRANSACTION, not a third one
    # after it. Advancing the observation separately would leave a window in
    # which the ledger is correct — position created, margin and fee debited
    # — while the evidence chain still said SIMULATED_FILLED with no
    # position_id. Settlement now closes the chain or rolls back everything.
    try:
        result = settle_position_entry(
            final, fill_price=fill, execution_provenance=provenance,
            canonical_entry_fee_usd=float(fee_quote.fee_usd),
            observation_id=observation_id,
            execution_id=entry_execution_id)
    except Exception as e:
        # The whole transaction unwound: no position, no margin, no fee. The
        # decision still happened, so it is finalised as failed rather than
        # left pending forever.
        logger.error("[CanonicalEntry] %s settlement rolled back: %s",
                     symbol, e, exc_info=True)
        DO.finalise_failed_settlement(observation_id,
                                    "SETTLEMENT_TRANSACTION_ROLLED_BACK")
        return {"error": "SETTLEMENT_ROLLED_BACK", "detail": str(e),
                "venue": ready.venue, "product": ready.product,
                "asset_class": ready.asset_class,
                "venue_failure": False, "opened": False}

    if not result.get("ok"):
        # A refusal BEFORE any economic mutation — duplicate open, book
        # full, deployment cap, insufficient cash. THE DECISION STAYS TRADE:
        # JARVIS did decide to trade and the venue did produce a fill, so
        # rewriting the judgment to NO_TRADE would be a lie about what
        # happened. The lifecycle carries the truth instead, and a
        # SETTLEMENT_FAILED row is never execution-calibration eligible.
        # Nothing moved, so this diagnostic update needs no shared
        # transaction with the settlement that refused.
        DO.finalise_failed_settlement(observation_id,
                                    _settlement_failure_reason(result))
        return result

    result["observation_id"] = observation_id

    # THE REAL CONTRACT IS {"ok": True, "position": {"id": ...}}.
    # This read result["position_id"], which does not exist, so it silently
    # passed None and provenance was never stamped — every "canonical"
    # position persisted with execution_provenance NULL, is_canonical()
    # False, and the fail-closed exit guard blind to it. The tests passed
    # because they mocked a shape this codebase never returns.
    result["execution"] = {
        "venue": ready.venue, "product": ready.product,
        "asset_class": ready.asset_class, "instrument": ready.instrument,
        "decision_price": decision_price, "fill_price": fill,
        "bid_at_submit": snap.bid, "ask_at_submit": snap.ask,
        "authorized_quantity": authorized.qty,
        "filled_quantity": execution.filled_quantity,
        # These now describe the REAL order, not one nominal unit of it.
        "spread_attribution_usd": execution.spread_cost_usd,
        "slippage_attribution_usd": execution.slippage_usd,
        "execution_model_version": EXECUTION_MODEL_CANONICAL,
    }
    return result


def _settlement_failure_reason(result: dict) -> str:
    """A NAMED reason, not the prose the caller happened to log.

    `settlement_failure_reason` is meant to be queryable years later, and
    "Paper deployment cap reached: $12,345 of $60,000 ..." is a sentence
    about one moment, not a category.
    """
    err = str(result.get("error") or "").lower()
    if "already open" in err:
        return "DUPLICATE_OPEN"
    if "book full" in err:
        return "MAX_POSITIONS"
    if "deployment cap" in err:
        return "DEPLOYMENT_CAP"
    if "insufficient" in err:
        return "INSUFFICIENT_CASH"
    if "concentration" in err:
        return "CONCENTRATION"
    return "SETTLEMENT_REFUSED"


def _sizing_reason(prep: dict) -> str:
    """The NAMED reason sizing refused, for the observation record.

    "paper sizing rejected: ..." is a sentence; a binding reason has to be
    queryable years later, which is the whole point of the record.
    """
    err = str(prep.get("error") or "").lower()
    if "concentration" in err:
        return "CONCENTRATION"
    if "deployment cap" in err:
        return "DEPLOYMENT_CAP"
    if "already open" in err:
        return "DUPLICATE_OPEN"
    if "book full" in err:
        return "MAX_POSITIONS"
    if "insufficient" in err:
        return "INSUFFICIENT_CASH"
    return "RISK_SIZING_REJECTED"


def build_provenance(*, signal, ready, snap, execution,
                     decision_price, authorized_qty=None,
                     fee_quote=None, entry_execution_id=None) -> dict:
    """How this position came to exist. Persisted by settlement, atomically.

    One JSON document rather than a dozen columns. NULL provenance means
    LEGACY — it is never inferred and never backfilled, because "this is a
    crypto symbol so it was probably Kraken" is a guess, and a guess in the
    execution ledger is indistinguishable from a measurement.
    """
    # TWO DIFFERENT CLAIMS, AND BOTH ARE NOW TRUE.
    #
    # The EXECUTION model is the venue book: this fill crossed a real
    # bid/ask. The COST model is per_leg_v2 as of A2 — the entry leg is
    # priced by the fee authority from the quantity that actually filled and
    # DEBITED at settlement, so `fees` stays 0 and the close path cannot
    # bill the same economics a second time.
    #
    # This said COST_MODEL_LEGACY until settlement genuinely changed,
    # because false provenance is worse than none: a position that lies
    # about its own accounting corrupts calibration silently, where one that
    # says nothing merely withholds it. The label moved only when the
    # behaviour did, and `cost_model_fee_quality` carries how much the
    # number can be trusted — a labelled ESTIMATED_PERP rate is not a
    # measurement and must never be pooled as one.
    fee = fee_quote
    if fee is None or not getattr(fee, "ok", False):
        raise ValueError(
            "build_provenance requires a priced entry leg — a canonical "
            "position must not be stamped per_leg_v2 while being charged "
            "nothing")
    payload = {
        "cost_model": COST_MODEL_CANONICAL,
        "cost_model_note": (
            "entry leg priced at the executed size and debited at "
            "settlement; PaperPosition.fees stays 0 so the exit cannot "
            "charge the same economics again"),
        "entry_fee_usd": fee.fee_usd,
        "entry_fee_basis": fee.fee_basis,
        "entry_fee_rate": fee.rate,
        "entry_fee_contract_count": fee.contract_count,
        "entry_fee_source": fee.source,
        "cost_model_fee_quality": fee.quality,
        "cost_model_fee_is_measured": fee.is_measured,
        # THE ENTRY EXECUTION IDENTITY. is_canonical() requires it, so a
        # position cannot claim the canonical model without naming the
        # execution that produced it.
        "entry_execution_id": entry_execution_id or _execution_id(),
        "execution_model": EXECUTION_MODEL_CANONICAL,
        "engine_epoch": CANONICAL_ENGINE_EPOCH,
        "source": "VIRTUAL_CEX_AGENT",
        # FOUR IDENTITIES, RECORDED SEPARATELY. `product` used to hold
        # "crypto" — an asset class — so nothing persisted could distinguish a
        # perpetual from a spot pair after the fact, and a calibration set
        # that pools the two is measuring a product that does not exist.
        "venue": ready.venue,
        "asset_class": ready.asset_class,
        "product": ready.product,
        "instrument": ready.instrument,
        "symbol": snap.symbol,
        "signal_id": signal.get("id") or signal.get("signal_id"),
        "decision_price": decision_price,
        "actual_entry_fill": execution.fill_price,
        "bid_at_submit": snap.bid,
        "ask_at_submit": snap.ask,
        # SIZE, so the attribution below can be checked rather than trusted.
        # These were computed on a nominal single unit while the position
        # settled at whatever sizing produced, and nothing recorded the
        # discrepancy because nothing recorded the quantity.
        "authorized_quantity": authorized_qty,
        "filled_quantity": execution.filled_quantity,
        "quantity_unit": execution.quantity_unit,
        "multiplier": execution.multiplier,
        "spread_attribution_usd": execution.spread_cost_usd,
        "slippage_attribution_usd": execution.slippage_usd,
        "impact_attribution_usd": 0.0,
        "fill_model": execution.fill_model,
        "market_source": snap.source,
        "venue_observed_at": snap.venue_event_at,
        "snapshot_age_ms": snap.age_ms,
        "snapshot_status": snap.status,
        "settled_at": _now(),
    }
    return payload


def _execution_id() -> str:
    """A fresh identity for one entry execution.

    `new_id` is a uuid4 generator, not a session — importing it here does
    not give this module a database handle, and the AST guard that forbids
    `get_db` in this file still holds.
    """
    from app.database import new_id
    return str(new_id())


def is_canonical(position) -> bool:
    """True when this position was opened by the venue-book executor AND
    settled under per-leg costs AND belongs to this engine epoch AND names
    the execution that produced it.

    ALL FOUR, because each alone admits a hybrid. Testing only
    `execution_model` accepted a position whose fill came from the venue
    book but whose costs were still the legacy deferred round-trip estimate
    — a real state this codebase produced between A5 and A2, and the exact
    kind of half-honest economics that is worse than uniformly optimistic
    ones: uniformly wrong is diagnosable, selectively wrong is not.

    After this pass no new hybrid may be produced. Anything that needs to
    recognise "canonical fill, legacy cost" must ask for it by a DIFFERENT
    name — see `has_canonical_fill`.
    """
    doc = provenance_of(position)
    if not doc:
        return False
    return bool(
        doc.get("execution_model") == EXECUTION_MODEL_CANONICAL
        and doc.get("cost_model") == COST_MODEL_CANONICAL
        and doc.get("engine_epoch") == CANONICAL_ENGINE_EPOCH
        and str(doc.get("entry_execution_id") or "").strip())


def has_canonical_fill(position) -> bool:
    """The WEAKER claim, under its own name: the fill crossed a venue book,
    whatever the cost model says.

    This exists so nothing is tempted to loosen `is_canonical` to cover the
    hybrid case. It is also what the FAIL-CLOSED exit guard keys on: a guard
    must refuse everything the legacy close path could mis-settle, which is
    wider than what the classifier calls fully canonical.
    """
    doc = provenance_of(position)
    return bool(doc and doc.get("execution_model") == EXECUTION_MODEL_CANONICAL)


def provenance_of(position) -> dict | None:
    raw = getattr(position, "execution_provenance", None)
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
