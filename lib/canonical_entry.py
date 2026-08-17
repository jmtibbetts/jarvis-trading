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

    execution_readiness()    which venue, and may it be filled at all
    execution_market_snapshot()  that venue's own bid/ask, graded and aged
    virtual_orders.execute_market()  BUY lifts the ask, SELL hits the bid,
                                     then adverse slippage
    open_paper_position(current_price=<ACTUAL FILL>)

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_canonical_position(signal: dict, *, decision_price: float | None = None,
                            max_age_s: float | None = None) -> dict:
    """Authorize, execute against the venue book, then settle the ACTUAL fill.

    Returns the `open_paper_position` result on success, or a refusal dict
    carrying `venue_failure=True` when the venue could not price a fill.
    """
    from lib import execution_policy as POL
    from lib import virtual_orders as VO
    from lib.paper_engine import open_paper_position
    from lib.trade_side import SHORT, parse_side_strict

    symbol = (signal.get("asset_symbol") or "").upper().strip()
    asset_class = signal.get("asset_class")
    raw_dir = signal.get("paper_direction") or signal.get("direction")

    side = parse_side_strict(raw_dir)
    if side is None:
        # Not a venue failure — an unreadable side is a bad order.
        return {"error": f"unparseable direction {raw_dir!r} — refusing to "
                         "assume a side for an order",
                "venue_failure": False}

    # ── 1. May this be filled at all, and by whom? ───────────────────────
    ready = POL.execution_readiness(symbol, asset_class,
                                    **({} if max_age_s is None else
                                       {"max_age_s": max_age_s}))
    if not ready.ok:
        logger.info("[CanonicalEntry] %s not executable: %s (%s)",
                    symbol, ready.reason, ready.detail)
        return {"error": ready.reason, "detail": ready.detail,
                "venue": ready.venue, "product": ready.product,
                "venue_failure": POL.is_venue_data_failure(ready.reason),
                "opened": False}

    snap = ready.snapshot

    # ── 2. Cross the book. BUY lifts the ask; SELL hits the bid. ─────────
    # Quantity is nominal here: the authorization below decides real size.
    # What this call establishes is the PRICE, which is the whole point.
    quote = VO.Quote(bid=snap.bid, ask=snap.ask, as_of=snap.venue_event_at,
                     source=snap.source)
    order = VO.VirtualOrder(symbol=symbol, side=("short" if side == SHORT else "long"),
                            quantity=float(signal.get("qty") or 1.0),
                            order_type=VO.MARKET,
                            signal_id=signal.get("id") or signal.get("signal_id"))
    execution = VO.execute_market(order, quote)

    if not execution.filled or not execution.fill_price:
        logger.info("[CanonicalEntry] %s produced no fill: state=%s reason=%s",
                    symbol, execution.state, execution.reject_reason)
        return {"error": execution.reject_reason or UNFILLED,
                "execution_state": execution.state,
                "venue": ready.venue, "product": ready.product,
                # An unfilled order is an execution fact, not a venue outage.
                "venue_failure": False, "opened": False}

    fill = float(execution.fill_price)

    # ── 3. Settle the ACTUAL fill through the existing authorization. ────
    # The mark is kept as decision_price so slippage stays measurable.
    result = open_paper_position(signal, current_price=fill)
    if not result.get("ok"):
        return result

    # THE REAL CONTRACT IS {"ok": True, "position": {"id": ...}}.
    # This read result["position_id"], which does not exist, so it silently
    # passed None and provenance was never stamped — every "canonical"
    # position persisted with execution_provenance NULL, is_canonical()
    # False, and the fail-closed exit guard blind to it. The tests passed
    # because they mocked a shape this codebase never returns.
    position_id = (result.get("position") or {}).get("id")
    if not position_id:
        raise RuntimeError(
            "open_paper_position reported success without a position id; "
            "refusing to leave an economically-open position with no "
            f"canonical provenance (result keys: {sorted(result)})")

    _stamp_provenance(position_id, signal=signal, ready=ready,
                      snap=snap, execution=execution,
                      decision_price=decision_price)
    result["execution"] = {
        "venue": ready.venue, "product": ready.product,
        "decision_price": decision_price, "fill_price": fill,
        "bid_at_submit": snap.bid, "ask_at_submit": snap.ask,
        "spread_attribution_usd": execution.spread_cost_usd,
        "slippage_attribution_usd": execution.slippage_usd,
        "execution_model_version": EXECUTION_MODEL_CANONICAL,
    }
    return result


def _stamp_provenance(position_id, *, signal, ready, snap, execution,
                      decision_price) -> None:
    """Record how this position came to exist, immutably.

    One JSON document rather than a dozen columns. NULL provenance means
    LEGACY — it is never inferred and never backfilled, because "this is a
    crypto symbol so it was probably Kraken" is a guess, and a guess in the
    execution ledger is indistinguishable from a measurement.
    """
    if not position_id:
        return
    # TWO DIFFERENT CLAIMS, RECORDED SEPARATELY AND HONESTLY.
    #
    # The EXECUTION model really is the venue book: this fill crossed a real
    # bid/ask. The COST model is NOT yet per_leg_v2 — open_paper_position
    # still writes sizing["round_trip_fees"] into PaperPosition.fees, which
    # is the legacy DEFERRED round-trip charge. Stamping per_leg_v2 here
    # would be FALSE PROVENANCE, and a position that lies about its own
    # accounting is worse than one that says nothing: the first corrupts
    # calibration silently, the second merely withholds it.
    #
    # Pass A.2 replaces the cost half; until then this records what actually
    # happened.
    payload = {
        "cost_model": COST_MODEL_LEGACY,
        "cost_model_note": (
            "entry settled through legacy round-trip fee accounting; "
            "per_leg_v2 is NOT yet in force for this position"),
        "execution_model": EXECUTION_MODEL_CANONICAL,
        "engine_epoch": CANONICAL_ENGINE_EPOCH,
        "source": "VIRTUAL_CEX_AGENT",
        "venue": ready.venue,
        "product": ready.product,
        "symbol": snap.symbol,
        "signal_id": signal.get("id") or signal.get("signal_id"),
        "decision_price": decision_price,
        "actual_entry_fill": execution.fill_price,
        "bid_at_submit": snap.bid,
        "ask_at_submit": snap.ask,
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
    try:
        from app.database import PaperPosition, get_db
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == position_id).first()
            if pos is not None:
                pos.execution_provenance = json.dumps(payload)
                db.commit()
    except Exception as e:
        # PROVENANCE IS PART OF SETTLEMENT TRUTH, not metadata about it.
        # An economically-open position with NULL provenance is invisible to
        # is_canonical(), so the fail-closed exit guard would let it through
        # the legacy close path — the exact hole this whole pass exists to
        # shut. Raising propagates to the caller, which unwinds.
        logger.error("[CanonicalEntry] could not stamp provenance on %s: %s",
                     position_id, e)
        raise


def is_canonical(position) -> bool:
    """True when this position was opened by the venue-book executor."""
    raw = getattr(position, "execution_provenance", None)
    if not raw:
        return False
    try:
        doc = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return False
    return doc.get("execution_model") == EXECUTION_MODEL_CANONICAL


def provenance_of(position) -> dict | None:
    raw = getattr(position, "execution_provenance", None)
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
