"""Portfolio concentration limits — checked at OPEN time, against the book.

Why not in the sizing formula: an earlier attempt put a notional ceiling
inside solve_position and it collided head-on with risk parity, the
invariant that every trade risks the same dollars regardless of stop
width. Correct risk-first sizing at 1x with an ordinary 5% stop already
produces 20% notional, so any ceiling tight enough to bind a leveraged
monster also strangles ordinary trades. Four tests refused it, correctly.

The resolution: sizing stays mathematically pure (budget / risk-per-unit,
constraints only shrink), and CONCENTRATION — which is a property of the
portfolio, not of any single trade — is enforced where it actually lives.
A position is judged against what is already open.

What this bounds, measured 2026-08-16 on a $100k paper book:
  XAUT/USD   146% of equity in one position
  LTC/USD     74%
  BNB/USD     53%
Each had correct 1-3% margin and correct sub-budget risk. Exposure was
the unbounded quantity, and exposure is what gaps through stops.

It also refuses the opposite failure: a stop so tight that safe sizing
leaves a position risking almost nothing. Such a trade pays real fees,
occupies a slot, and teaches the ledger nothing — "too small to matter"
is a rejection reason, not a position.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# One instrument may control at most this share of equity. 25% still
# admits the ordinary 1x/5%-stop trade (20% notional) while forcing at
# least four instruments before the book is full — variety by
# construction, which is also what the training corpus needs.
MAX_SYMBOL_EXPOSURE_PCT = 25.0

# Total controlled notional across all open positions. Generous because
# leverage is legitimate here (the operator's broker offers 1-25x), but
# not unbounded: past this the book is one correlated shock from ruin.
MAX_GROSS_EXPOSURE_PCT = 400.0

# A position risking less than this fraction of equity at its stop is
# noise wearing a ticker. Fees alone would dominate its P&L.
MIN_RISK_PCT_OF_EQUITY = 0.02


def check(symbol: str, notional: float, loss_at_stop: float,
          equity: float, open_positions: list) -> dict:
    """May this position open? Returns the verdict with its arithmetic.

    open_positions: objects/dicts carrying `symbol` and `notional` for
    everything currently open. Passed in rather than queried so this
    stays pure and testable — the caller already holds the book.
    """
    if equity <= 0:
        return {"ok": False, "reason": "no equity to size against"}

    def _sym(p):
        return str(getattr(p, "symbol", None) or
                   (p.get("symbol") if isinstance(p, dict) else "") or "").upper()

    def _not(p):
        v = getattr(p, "notional", None)
        if v is None and isinstance(p, dict):
            v = p.get("notional")
        try:
            return abs(float(v or 0))
        except (TypeError, ValueError):
            return 0.0

    sym = str(symbol or "").upper()
    notional = abs(float(notional or 0))

    # Same instrument already open counts toward the same bucket — two
    # 20% positions in one symbol is a 40% bet on one thing.
    existing_sym = sum(_not(p) for p in open_positions if _sym(p) == sym)
    gross = sum(_not(p) for p in open_positions)

    sym_pct = 100.0 * (existing_sym + notional) / equity
    gross_pct = 100.0 * (gross + notional) / equity
    risk_pct = 100.0 * abs(float(loss_at_stop or 0)) / equity

    detail = {
        "symbol_exposure_pct": round(sym_pct, 1),
        "gross_exposure_pct": round(gross_pct, 1),
        "risk_pct_of_equity": round(risk_pct, 4),
        "limits": {
            "max_symbol_pct": MAX_SYMBOL_EXPOSURE_PCT,
            "max_gross_pct": MAX_GROSS_EXPOSURE_PCT,
            "min_risk_pct": MIN_RISK_PCT_OF_EQUITY,
        },
    }

    if sym_pct > MAX_SYMBOL_EXPOSURE_PCT:
        return {**detail, "ok": False, "limit": "symbol",
                "reason": (f"{sym} would be {sym_pct:.0f}% of equity "
                           f"(cap {MAX_SYMBOL_EXPOSURE_PCT:.0f}%)")}
    if gross_pct > MAX_GROSS_EXPOSURE_PCT:
        return {**detail, "ok": False, "limit": "gross",
                "reason": (f"gross exposure would be {gross_pct:.0f}% "
                           f"(cap {MAX_GROSS_EXPOSURE_PCT:.0f}%)")}
    if risk_pct < MIN_RISK_PCT_OF_EQUITY:
        return {**detail, "ok": False, "limit": "trivial",
                "reason": (f"risks only {risk_pct:.3f}% of equity — stop is "
                           f"too tight to size meaningfully; fees would "
                           f"dominate the outcome")}
    return {**detail, "ok": True, "limit": None, "reason": "within limits"}


def check_against_book(symbol: str, notional: float, loss_at_stop: float,
                       equity: float, db=None) -> dict:
    """check() with the open book loaded for you. Never raises — a
    concentration check that errors must not become an unbounded open."""
    try:
        from app.database import PaperPosition, get_db
        if db is not None:
            rows = db.query(PaperPosition).filter(
                PaperPosition.status == "open").all()
            return check(symbol, notional, loss_at_stop, equity, rows)
        with get_db() as _db:
            rows = _db.query(PaperPosition).filter(
                PaperPosition.status == "open").all()
            return check(symbol, notional, loss_at_stop, equity, rows)
    except Exception as e:
        logger.warning(f"[Concentration] check failed for {symbol}: {e}")
        # Fail CLOSED: an unknown book is not permission to concentrate.
        return {"ok": False, "limit": "error", "reason": f"check failed: {e}"}
