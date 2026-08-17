"""Everything the desk knows about ONE instrument, assembled in one answer.

Until this existed, an instrument's facts were scattered across surfaces
that each answered a different question: the scanner said a setup existed,
the sizer said what one contract was worth, the venue panel said where it
could be filled, and nothing said whether those three agreed. An operator
who wanted "what is 6J=F and why can't I trade it" had to know which four
pages to visit and which of them to believe.

THE REFUSAL IS THE HEADLINE, NOT AN ERROR STATE. `6J=F` resolves to
UNSUPPORTED with MISSING_CONTRACT_SPEC and has signals sitting behind it.
That is a fact about the instrument worth as much screen space as a price,
and it is reported here with the count of what is blocked — an operator who
sees "140 signals cannot be sized until the CME multiplier is verified" can
act; one who sees an empty panel assumes a bug and goes looking for it.

NOTHING HERE DERIVES. Identity comes from lib.instruments, capability from
lib.venue_capabilities, cost from lib.transaction_costs, and each section
carries the reason when its answer is unavailable rather than a zero.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The reference stop used to price an instrument when the operator has not
# proposed one. Deliberately the cost FLOOR from lib.transaction_costs
# rather than a round number: pricing at the tightest stop that can still
# pay for itself answers "what does this instrument cost to trade at its
# best case", which is the question a workspace should open on.
REFERENCE_MAX_COST_R = 0.50


def _venue_view(product: str) -> list[dict]:
    """Every venue's stance on THIS product, executable ones first.

    A product no venue has been characterised for returns an empty list —
    which the UI must render as "no venue characterised", not as "no
    venues exist". They are different claims.
    """
    from lib.venue_capabilities import snapshot
    rows: list[dict] = []
    for venue, caps in (snapshot().get("venues") or {}).items():
        for cap in caps:
            if cap.get("product") == product:
                rows.append({**cap, "venue": venue})
    rows.sort(key=lambda c: (not c.get("executable"), c.get("venue") or ""))
    return rows


def _cost_view(identity, entry: float | None) -> dict:
    """What a round trip costs on this instrument, priced at the cost floor.

    Without a price there is no percentage to convert, so this returns the
    floor stop alone rather than inventing an entry.
    """
    from lib.transaction_costs import estimate_costs, min_viable_stop_pct

    symbol = identity.canonical_symbol
    try:
        floor = min_viable_stop_pct(symbol, max_cost_r=REFERENCE_MAX_COST_R)
    except Exception as e:                                  # pragma: no cover
        logger.debug(f"[Workspace] cost floor failed for {symbol}: {e}")
        return {"available": False,
                "reason": f"cost floor not computable: {e}"}

    out = {
        "available": True,
        "min_viable_stop_pct": round(floor * 100.0, 4),
        "max_cost_r": REFERENCE_MAX_COST_R,
        "note": ("The tightest stop that can still pay for itself. Cost in R "
                 "scales as 1/stop distance, so a stop inside this floor is "
                 "not a thin trade — it is a non-trade."),
    }
    if not entry or entry <= 0:
        out["reference"] = None
        out["reference_reason"] = (
            "no price available, so the round trip cannot be priced in R — "
            "the floor above is a percentage of entry and stands on its own")
        return out

    stop = float(entry) * (1.0 - floor)
    try:
        out["reference"] = estimate_costs(symbol, float(entry), stop)
        out["reference_entry"] = float(entry)
        out["reference_stop"] = round(stop, 8)
    except Exception as e:                                  # pragma: no cover
        out["reference"] = None
        out["reference_reason"] = f"cost estimate failed: {e}"
    return out


def _activity(db, variants: set[str]) -> dict:
    """Signals, considered candidates and closed outcomes for this symbol.

    Matched on every spelling a venue might use, because the book records
    `BTCUSD` and the signal records `BTC/USD` and a workspace that matched
    one of them would report an instrument as untouched while it was held.
    """
    from app.database import CandidateSignal, TradeOutcome, TradingSignal

    syms = sorted(variants)
    sig_rows = (db.query(TradingSignal)
                .filter(TradingSignal.asset_symbol.in_(syms))
                .order_by(TradingSignal.generated_at.desc()).limit(400).all())
    active = [s for s in sig_rows if (s.status or "") in ("Active", "Approved")]

    cand_n = (db.query(CandidateSignal)
              .filter(CandidateSignal.symbol.in_(syms)).count())
    outcomes = (db.query(TradeOutcome)
                .filter(TradeOutcome.symbol.in_(syms)).all())

    wins = sum(1 for o in outcomes if o.outcome == "WIN")
    decided = [o for o in outcomes if o.outcome in ("WIN", "LOSS")]
    pnls = [float(o.pnl_pct) for o in outcomes if o.pnl_pct is not None]

    return {
        "signals_total": len(sig_rows),
        "signals_active": len(active),
        "candidates_considered": cand_n,
        "outcomes_closed": len(outcomes),
        # A win rate over fewer than a handful of trades is noise wearing a
        # percentage sign, so it is withheld rather than rounded.
        "win_rate_pct": (round(wins / len(decided) * 100.0, 1)
                         if len(decided) >= 5 else None),
        "win_rate_reason": (None if len(decided) >= 5 else
                            f"only {len(decided)} decided outcomes — too few "
                            f"to quote a win rate"),
        "avg_pnl_pct": (round(sum(pnls) / len(pnls), 3) if pnls else None),
        "recent_signals": [{
            "id": s.id, "direction": s.direction, "timeframe": s.timeframe,
            "status": s.status, "strategy": s.strategy,
            "entry_price": s.entry_price, "stop_loss": s.stop_loss,
            "target_price": s.target_price,
            "composite_score": s.composite_score,
            "generated_at": s.generated_at,
        } for s in sig_rows[:12]],
    }


def _exposure(db, variants: set[str]) -> list[dict]:
    """Open positions in this instrument, across every book that holds one."""
    from app.database import PaperPosition
    syms = sorted(variants)
    out: list[dict] = []
    try:
        rows = (db.query(PaperPosition)
                .filter(PaperPosition.symbol.in_(syms),
                        PaperPosition.status == "Open").all())
        for p in rows:
            out.append({
                "book": "paper", "symbol": p.symbol,
                "direction": p.direction, "qty": p.qty,
                "entry_price": p.entry_price,
                "stop_loss": p.stop_loss,
                "initial_stop_loss": p.initial_stop_loss,
                "opened_at": p.opened_at,
            })
    except Exception as e:                                  # pragma: no cover
        logger.debug(f"[Workspace] paper exposure failed: {e}")
    return out


def workspace(symbol: str, *, product: str | None = None,
              entry: float | None = None) -> dict:
    """One instrument, everything known about it, with every gap named.

    Never raises on an unknown instrument: an instrument this system
    refuses to trade is a legitimate thing to open a workspace on, and it
    is the case where the operator most needs to see WHY.
    """
    from lib.instruments import resolve, variants

    ident = resolve(symbol, product=product)
    ident_d = ident.as_dict()
    spellings = variants(ident.canonical_symbol or symbol)

    activity: dict = {}
    exposure: list[dict] = []
    try:
        from app.database import get_db
        with get_db() as db:
            activity = _activity(db, spellings)
            exposure = _exposure(db, spellings)
    except Exception as e:
        logger.warning(f"[Workspace] activity unavailable for {symbol}: {e}")
        activity = {"unavailable": True, "reason": str(e)}

    refusal = None
    if not ident.executable:
        # The count is the point. "UNSUPPORTED" alone reads as a shrug;
        # "UNSUPPORTED, and 143 signals were produced against it" is a work
        # item. Recorded and ACTIVE are reported separately — a historical
        # total presented as a live backlog overstates the urgency, and
        # this panel exists to stop exactly that kind of rounding.
        acted = activity if isinstance(activity, dict) else {}
        refusal = {
            "status": ident.status,
            "reason": ident.reason or "no execution spec",
            "signals_recorded": acted.get("signals_total"),
            "signals_active": acted.get("signals_active"),
            "detail": (
                f"{ident.display_symbol} resolves but cannot be sized or "
                f"simulated. Research continues; execution does not. The "
                f"spec must be verified against the exchange — inferring a "
                f"multiplier would make every outcome on it wrong by "
                f"whatever the real one is."),
        }

    return {
        "symbol": symbol,
        "canonical_symbol": ident.canonical_symbol,
        "identity": ident_d,
        "executable": ident.executable,
        "refusal": refusal,
        "spellings": sorted(spellings),
        "venues": _venue_view(ident.product),
        "cost": _cost_view(ident, entry),
        "activity": activity,
        "exposure": exposure,
    }
