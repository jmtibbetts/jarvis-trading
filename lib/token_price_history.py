"""Historical USD price for a Solana mint, at a moment in time.

Post-entry alpha asks "what was this token worth an hour after the wallet
entered", and that is a HISTORICAL question. The desk's existing pricing
helpers answer a different one — `token_pricing.resolve_prices` returns
CURRENT prices, and `/wallet/intel` was reading them out of wallet #1's
present balances, which is not a pricing service at all.

The honest source available today is the surge pipeline's own
`TokenActivitySnapshot` rows: one observation per pool per scan, already
being persisted for baselines, each carrying `price_usd` and `captured_at`.
That gives real historical coverage for exactly the tokens the desk has
been watching, at roughly the scan cadence.

Its limits are stated rather than hidden:

  - resolution is the scan interval, not the trade. A price 20 minutes from
    the requested moment is returned as such, with its distance, and refused
    beyond a tolerance that scales with the horizon.
  - coverage begins when the token first entered a scan. There is no
    backfill, so an entry older than the desk's first sighting is UNKNOWN.

A horizon that cannot be priced stays NULL and is retried later. It is
never recorded as a zero return.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# How far a stored snapshot may sit from the requested moment. Scaled to
# the horizon: being 10 minutes off is fatal for a 5m return and trivial
# for a 24h one.
TOLERANCE_FRACTION = 0.35
MIN_TOLERANCE_S = 240.0
MAX_TOLERANCE_S = 7200.0


def _as_dt(when) -> datetime | None:
    if when is None:
        return None
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    try:
        if isinstance(when, (int, float)):
            return datetime.fromtimestamp(float(when), tz=timezone.utc)
        t = datetime.fromisoformat(str(when))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def price_at(mint: str, when, *, horizon_s: float | None = None,
             session=None) -> float | None:
    """USD price of `mint` nearest `when`, or None.

    None means unknown, and callers must treat it as unknown rather than
    substituting a current price — valuing an hour-old moment at today's
    price is the error this module exists to prevent.
    """
    r = price_at_detailed(mint, when, horizon_s=horizon_s, session=session)
    return r.get("price_usd")


def price_at_detailed(mint: str, when, *, horizon_s: float | None = None,
                      session=None) -> dict:
    from app.database import TokenActivitySnapshot, get_db

    target = _as_dt(when)
    out = {"mint": mint, "price_usd": None, "source": None,
           "quality": "UNAVAILABLE", "distance_s": None, "reason": None,
           "as_of": None}
    if target is None or not mint:
        out["reason"] = "no mint or timestamp"
        return out

    tol = MIN_TOLERANCE_S
    if horizon_s:
        tol = min(MAX_TOLERANCE_S,
                  max(MIN_TOLERANCE_S, float(horizon_s) * TOLERANCE_FRACTION))

    def _run(s):
        lo = (target - timedelta(seconds=tol * 2)).isoformat()
        hi = (target + timedelta(seconds=tol * 2)).isoformat()
        rows = (s.query(TokenActivitySnapshot.captured_at,
                        TokenActivitySnapshot.price_usd)
                .filter(TokenActivitySnapshot.mint == mint,
                        TokenActivitySnapshot.price_usd.isnot(None),
                        TokenActivitySnapshot.captured_at >= lo,
                        TokenActivitySnapshot.captured_at <= hi).all())
        best, best_d = None, None
        for captured_at, price in rows:
            t = _as_dt(captured_at)
            if t is None or not price:
                continue
            d = abs((t - target).total_seconds())
            if best_d is None or d < best_d:
                best, best_d = (t, float(price)), d
        if best is None:
            out["reason"] = ("no stored snapshot near that moment — the token "
                             "was not being scanned then")
            return out
        if best_d > tol:
            out["reason"] = (f"nearest snapshot is {best_d / 60:.1f}m away, "
                             f"beyond the {tol / 60:.1f}m tolerance for this horizon")
            out["distance_s"] = best_d
            return out
        out.update(price_usd=best[1], source="token_activity_snapshots",
                   quality="MEASURED", distance_s=best_d,
                   as_of=best[0].isoformat())
        return out

    if session is not None:
        return _run(session)
    with get_db() as db:
        return _run(db)
