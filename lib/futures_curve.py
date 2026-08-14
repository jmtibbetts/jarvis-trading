"""Futures curve snapshots — term structure as a recorded observation.

The continuous symbol shows ONE price; the market quotes a strip of them.
The shape of that strip is information the desk has never captured:
backwardation (front premium) reads tight supply and pays longs a roll
yield; contango charges them one. The curve's SLOPE moving is often the
story behind a spot move that candles alone can't explain.

Each sync stores one CurveSnapshot per root: the enterable contracts from
the contract master (post-FND months excluded — a curve point nobody can
trade is not a tradable curve), each with its own price and days-to-risk.
front_code in every snapshot doubles as ROLL PROVENANCE: which specific
contract "CL=F" actually meant on any date is now a query, not a guess.

Per-contract quotes come from Yahoo's individual contract tickers
(CLV26.NYM …) — verified live 2026-08-14 against six contracts across
three venues. Same shadow-only rule as every 4A/4B feed: recorded,
influencing nothing until an ablation earns it a place.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Yahoo's venue suffix per root — the piece of the ticker that is Yahoo
# convention rather than exchange identity.
YAHOO_VENUE = {"CL": "NYM", "NG": "NYM", "PL": "NYM",
               "GC": "CMX", "SI": "CMX", "HG": "CMX",
               "ES": "CME", "NQ": "CME", "RTY": "CME", "YM": "CBT",
               "ZC": "CBT", "ZW": "CBT", "ZS": "CBT"}

# The roots the book actually trades — 4C's sector engines extend this
# when their instruments enter the universe, not before.
CURVE_ROOTS = ("CL", "NG", "GC", "SI", "HG", "ES", "NQ")

CURVE_DEPTH = 4                 # enterable contracts per snapshot
SNAPSHOT_BUCKET_HOURS = 4       # dedup granularity: one snapshot per bucket


def yahoo_ticker(contract) -> str | None:
    venue = YAHOO_VENUE.get(contract.root if contract.root in YAHOO_VENUE
                            else contract.root.lstrip("M"))
    return f"{contract.code}.{venue}" if venue else None


def curve_stats(points: list[dict]) -> dict:
    """Structure verdict + roll economics from an ordered strip.

    annualized_roll_pct is what holding the front and rolling to the
    second costs (contango, negative for a long) or pays (backwardation,
    positive) per year at today's spread — the carry a continuous-series
    backtest silently ignores.
    """
    if len(points) < 2:
        return {"structure": None, "spread_pct": None,
                "annualized_roll_pct": None, "slope_pct": None}
    front, second, last = points[0], points[1], points[-1]
    spread_pct = (second["price"] - front["price"]) / front["price"] * 100.0
    days_gap = second["dte"] - front["dte"]
    annualized = (-spread_pct * (365.0 / days_gap)) if days_gap > 0 else None
    slope_pct = (last["price"] - front["price"]) / front["price"] * 100.0
    return {
        "structure": "contango" if spread_pct > 0 else "backwardation",
        "spread_pct": round(spread_pct, 4),
        # Sign convention: positive = the roll PAYS a long (backwardation).
        "annualized_roll_pct": round(annualized, 2) if annualized is not None else None,
        "slope_pct": round(slope_pct, 4),
    }


def fetch_curve(root: str, asof: date | None = None,
                depth: int = CURVE_DEPTH) -> dict | None:
    """The enterable strip with live per-contract prices. None when fewer
    than two points fill — a one-point curve has no shape to record."""
    import yfinance as yf

    from lib.futures_contracts import listed_contracts

    asof = asof or date.today()
    points = []
    for c in listed_contracts(root, asof, n=depth):
        ticker = yahoo_ticker(c)
        if not ticker:
            continue
        try:
            h = yf.Ticker(ticker).history(period="2d", interval="1d")
            if h is None or len(h) == 0:
                continue
            points.append({"code": c.code,
                           "price": round(float(h["Close"].iloc[-1]), 6),
                           "risk_date": c.risk_date.isoformat(),
                           "dte": (c.risk_date - asof).days})
        except Exception as e:
            logger.debug(f"[Curve] {ticker} fetch failed: {e}")
    if len(points) < 2:
        return None
    return {"root": root, "as_of": asof.isoformat(), "points": points,
            **curve_stats(points)}


def sync_curves(roots: tuple = CURVE_ROOTS) -> dict:
    """The job body: one snapshot per root per time bucket, straight to
    the store (scheduler thread, slow cadence — same rationale as the
    official-data path)."""
    from lib.event_store import get_store
    from lib.market_events import CurveSnapshot, event_to_dict, make_meta

    now = datetime.now(timezone.utc)
    bucket = f"{now.date().isoformat()}T{now.hour // SNAPSHOT_BUCKET_HOURS:02d}"
    events, fetched = [], []
    for root in roots:
        try:
            curve = fetch_curve(root)
        except Exception as e:
            logger.warning(f"[Curve] {root} failed: {e}")
            continue
        if curve is None:
            continue
        fetched.append(root)
        events.append(event_to_dict(CurveSnapshot(
            meta=make_meta("yahoo_contracts", "yf_contract_strip_v1", None),
            symbol=root,
            points=tuple((p["code"], p["price"], p["dte"])
                         for p in curve["points"]),
            front_code=curve["points"][0]["code"],
            structure=curve["structure"],
            spread_pct=curve["spread_pct"],
            annualized_roll_pct=curve["annualized_roll_pct"],
            slope_pct=curve["slope_pct"],
            as_of=curve["as_of"],
            dedup_key=f"curve:{root}:{bucket}",
        )))
    stored = get_store().append(events)
    out = {"roots": list(roots), "fetched": fetched, "stored": stored}
    logger.info(f"[Curve] sync: {out}")
    return out
