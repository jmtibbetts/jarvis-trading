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


# Row accessors live at module level so the OPEN-time check and the
# reporting view below cannot drift apart — a status panel that measured
# exposure differently from the guard would be worse than no panel.

# Quote currencies that all denominate the same dollar exposure. Most crypto,
# and nearly everything on a DEX, quotes in USDT or USDC rather than USD.
_STABLE_QUOTES = ("USDT", "USDC", "USD")


def canon_symbol(symbol) -> str:
    """Collapse a symbol to the bucket its EXPOSURE belongs in.

    `DOGE/USD` and `DOGE/USDT` are one coin and one bet. Before this, the
    guard compared raw uppercased strings, so a book holding 20% of DOGE/USD
    would happily accept another 20% of DOGE/USDT: two different strings, one
    instrument, 40% of equity on a 25% cap. That is the same double-counting
    §117 removes from coordination scoring — one thing counted as several —
    and the quote currency is exactly the kind of cosmetic difference that
    makes it invisible.

    Deliberately requires a SEPARATOR before the quote. Collapsing on a bare
    suffix would rewrite any equity ticker that happens to end in "USD", and
    a guard that quietly renames instruments is worse than one that misses a
    pairing.
    """
    s = str(symbol or "").upper().strip()
    for sep in ("/", "-", "_", ":"):
        if sep in s:
            base, _, quote = s.partition(sep)
            if quote in _STABLE_QUOTES and base:
                return f"{base}/USD"
            break
    return s


def _sym(p):
    return canon_symbol(getattr(p, "symbol", None) or
                        (p.get("symbol") if isinstance(p, dict) else "") or "")


def _get(p, attr):
    v = getattr(p, attr, None)
    if v is None and isinstance(p, dict):
        v = p.get(attr)
    return v


def _num(v):
    try:
        return abs(float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _not(p):
    v = _num(_get(p, "notional"))
    if v:
        return v
    # Books that do not persist notional fall back to qty x entry.
    # Exact for spot and margin; for a futures multiplier it is a
    # FLOOR, which errs toward refusing rather than permitting — the
    # correct direction for a guard to be wrong in.
    return _num(_get(p, "qty")) * _num(_get(p, "entry_price"))


def check(symbol: str, notional: float, loss_at_stop: float,
          equity: float, open_positions: list) -> dict:
    """May this position open? Returns the verdict with its arithmetic.

    open_positions: objects/dicts carrying `symbol` and `notional` for
    everything currently open. Passed in rather than queried so this
    stays pure and testable — the caller already holds the book.
    """
    if equity <= 0:
        return {"ok": False, "reason": "no equity to size against"}

    # Canonicalized, so the incoming symbol lands in the same bucket the
    # book rows were aggregated into — DOGE/USDT must meet DOGE/USD.
    sym = canon_symbol(symbol)
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


def headroom(symbol: str, equity: float, open_positions: list) -> dict:
    """The largest notional this symbol may still open. Pure.

    This exists because the guard alone was the wrong shape. `size_position`
    solves quantity from RISK — budget / stop_distance — and never looked at
    notional, so a tight stop produced an enormous position by construction:
    a $10 stop on a $620 entry with a $1,003 risk budget is 100 shares, which
    is $62k, which is 62% of a $100k book. Every such trade was proposed and
    then refused, and the operator saw a stream of "concentration limit"
    errors instead of trades. The two halves were never reconciled.

    The sizer now asks how much room is left and fits inside it, so the
    normal outcome is a SMALLER position rather than a rejected one.
    Rejection is reserved for the case where the remaining room cannot carry
    a meaningful bet at all, and then it says so in those terms.

    Note this returns room for a NEW position; it never asks anything to be
    closed. An existing holding above the cap keeps its size (limits govern
    opening) and simply leaves zero headroom.
    """
    if equity <= 0:
        return {"max_notional": 0.0, "symbol_headroom": 0.0,
                "gross_headroom": 0.0, "binding": "equity",
                "reason": "no equity to size against"}

    # Canonicalized, so the incoming symbol lands in the same bucket the
    # book rows were aggregated into — DOGE/USDT must meet DOGE/USD.
    sym = canon_symbol(symbol)
    existing_sym = sum(_not(p) for p in open_positions if _sym(p) == sym)
    gross = sum(_not(p) for p in open_positions)

    sym_room = max(0.0, equity * (MAX_SYMBOL_EXPOSURE_PCT / 100.0) - existing_sym)
    gross_room = max(0.0, equity * (MAX_GROSS_EXPOSURE_PCT / 100.0) - gross)
    room = min(sym_room, gross_room)

    return {
        "max_notional": room,
        "symbol_headroom": sym_room,
        "gross_headroom": gross_room,
        "existing_symbol_notional": existing_sym,
        "gross_notional": gross,
        "binding": ("symbol" if sym_room <= gross_room else "gross"),
        "reason": (
            f"{sym} may add ${room:,.0f} "
            f"({'symbol' if sym_room <= gross_room else 'gross'} cap binds)"
            if room > 0 else
            f"no room for {sym}: "
            + (f"already at {100.0 * existing_sym / equity:.0f}% of equity "
               f"(cap {MAX_SYMBOL_EXPOSURE_PCT:.0f}%)" if sym_room <= 0 else
               f"book gross is {100.0 * gross / equity:.0f}% "
               f"(cap {MAX_GROSS_EXPOSURE_PCT:.0f}%)")),
    }


def headroom_for_book(symbol: str, equity: float, db=None,
                      book: str = "paper") -> dict:
    """`headroom()` with the open book loaded. Never raises — and on failure
    returns ZERO room, because an unknown book is not permission to size."""
    try:
        from app.database import get_db
        if db is not None:
            return headroom(symbol, equity, open_rows(book, db))
        with get_db() as _db:
            return headroom(symbol, equity, open_rows(book, _db))
    except Exception as e:
        logger.warning(f"[Concentration] headroom failed for {symbol}: {e}")
        return {"max_notional": 0.0, "symbol_headroom": 0.0,
                "gross_headroom": 0.0, "binding": "error",
                "reason": f"headroom unavailable: {e}"}


# ── The books ────────────────────────────────────────────────────────────
# EVERY table a position writer opens into. This registry is the whole
# reason a third book cannot quietly appear unguarded:
# tests/test_concentration_parity.py discovers position models by shape and
# fails if one is missing from here.
#
# Each book is judged against ITSELF. Paper and Auto Sim are independent
# simulations with their own capital, so measuring one against the other's
# open rows would refuse trades on the strength of an unrelated experiment.
# "% of COMBINED equity" is a display concern (PositionsPaper.svelte); the
# limit that blocks an open is per-book.
POSITION_BOOKS: dict[str, str] = {
    "paper": "paper_positions",
    "auto_sim": "auto_sim_positions",
}


def _book_model(book: str):
    from app.database import AutoSimPosition, PaperPosition
    try:
        return {"paper": PaperPosition, "auto_sim": AutoSimPosition}[book]
    except KeyError:
        raise ValueError(
            f"unknown book {book!r}; register it in POSITION_BOOKS") from None


def open_rows(book: str, db):
    """Open rows of one book.

    The status filter is CASE-INSENSITIVE and that is load-bearing. This
    function previously compared against the literal "open" while every
    writer stores "Open", and SQLite's `=` is case-sensitive — so the guard
    matched nothing and judged every position against an empty book from
    the day it landed. It could still refuse a single position that alone
    breached the cap, which is why it looked alive, but accumulation (the
    actual failure: DOGE reaching 35% across several opens) sailed through.
    Comparing case-insensitively means neither casing can resurrect it.
    """
    from sqlalchemy import func
    model = _book_model(book)
    return db.query(model).filter(func.lower(model.status) == "open").all()


def book_status(book: str, equity: float, db=None) -> dict:
    """What the limits say about a book AS IT STANDS — the reporting view.

    Deliberately built from the same `_not`/`_sym` accessors and the same
    constants as check(), because the whole reason this exists is that the
    guard was silently inert for a week. A panel that measured exposure
    its own way could agree with the operator's eyes while disagreeing
    with the thing that actually blocks trades, which is the failure it is
    meant to make impossible.

    `over_limit` is not hypothetical: positions opened before the guard
    worked can sit above the cap right now. The cap governs OPENING, so an
    existing breach is reported, never auto-liquidated.
    """
    def _aggregate(rows) -> tuple[dict, int]:
        # MUST run inside the session: these are ORM instances, and reading
        # an attribute after the session closes raises DetachedInstanceError
        # rather than returning a number.
        acc: dict[str, dict] = {}
        for p in rows:
            s = _sym(p)
            if not s:
                continue
            e = acc.setdefault(s, {"symbol": s, "notional": 0.0, "rows": 0})
            e["notional"] += _not(p)
            e["rows"] += 1
        return acc, len(rows)

    try:
        from app.database import get_db
        if db is not None:
            by_symbol, n_rows = _aggregate(open_rows(book, db))
        else:
            with get_db() as _db:
                by_symbol, n_rows = _aggregate(open_rows(book, _db))
    except Exception as e:
        logger.warning(f"[Concentration] status failed for {book}: {e}")
        return {"book": book, "error": f"{type(e).__name__}: {str(e)[:120]}",
                "symbols": [], "positions": 0}

    equity = float(equity or 0)
    # A book with no positive equity is not a book within its limits — it is
    # a book that cannot answer the question. Auto Sim reached realized_pnl
    # -106,901 on 100,000 of starting cash, so equity went NEGATIVE, and
    # every percentage here silently became None while `over_limit` became
    # False. The panel then displayed $101k of XLF notional as "—%" with no
    # breach flagged, which reads as healthy. `check()` refuses every open in
    # this state; the report has to say the same thing.
    solvent = equity > 0
    for e in by_symbol.values():
        e["pct_of_equity"] = round(100.0 * e["notional"] / equity, 2) if solvent else None
        # None, not False: unknown is not "within limits".
        e["over_limit"] = (e["pct_of_equity"] > MAX_SYMBOL_EXPOSURE_PCT
                           if solvent else None)
        e["notional"] = round(e["notional"], 2)

    symbols = sorted(by_symbol.values(),
                     key=lambda e: e["notional"], reverse=True)
    gross = round(sum(e["notional"] for e in symbols), 2)
    gross_pct = round(100.0 * gross / equity, 2) if equity > 0 else None
    breaches = [e["symbol"] for e in symbols if e["over_limit"]]

    return {
        "book": book,
        "table": POSITION_BOOKS.get(book),
        "equity": round(equity, 2),
        "positions": n_rows,
        "gross_notional": gross,
        "gross_pct_of_equity": gross_pct,
        "gross_over_limit": (gross_pct > MAX_GROSS_EXPOSURE_PCT
                             if gross_pct is not None else None),
        # The state the percentages above are relative to. "insolvent" is
        # load-bearing: it is the difference between "this book is fine" and
        # "this book has lost more than it started with and is refusing every
        # trade", which looked identical before.
        "solvent": solvent,
        "state": "ok" if solvent else "insolvent",
        "state_detail": None if solvent else (
            f"equity is ${equity:,.2f} — the book has no capital to size "
            f"against, so every open is refused and exposure cannot be "
            f"expressed as a percentage"),
        "symbols": symbols,
        "symbols_over_limit": breaches,
        "top": symbols[0] if symbols else None,
        "limits": {
            "max_symbol_pct": MAX_SYMBOL_EXPOSURE_PCT,
            "max_gross_pct": MAX_GROSS_EXPOSURE_PCT,
            "min_risk_pct": MIN_RISK_PCT_OF_EQUITY,
        },
        # Breaches predating the working guard are legal-but-unwanted, not
        # a bug to be alarmed about twice.
        "note": ("limits govern OPENING; an existing position above the cap "
                 "is reported, never force-closed") if breaches else None,
    }


def check_against_book(symbol: str, notional: float, loss_at_stop: float,
                       equity: float, db=None, book: str = "paper") -> dict:
    """check() with the open book loaded for you. Never raises — a
    concentration check that errors must not become an unbounded open."""
    try:
        from app.database import get_db
        if db is not None:
            rows = open_rows(book, db)
            return check(symbol, notional, loss_at_stop, equity, rows)
        with get_db() as _db:
            rows = open_rows(book, _db)
            return check(symbol, notional, loss_at_stop, equity, rows)
    except Exception as e:
        logger.warning(f"[Concentration] check failed for {symbol}: {e}")
        # Fail CLOSED: an unknown book is not permission to concentrate.
        return {"ok": False, "limit": "error", "reason": f"check failed: {e}"}
