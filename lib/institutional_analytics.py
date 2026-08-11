"""Institutional 13F holdings analytics — pure functions over already-fetched
holding dicts (no I/O), so the aggregation logic is unit-testable in isolation
from EDGAR and the DB.

The central honest constraint, repeated from lib/sec_13f.py because it drives
every design decision here: 13F is a QUARTERLY snapshot filed up to 45 days
after quarter-end, covering LONG US equity positions only. So:

  - "Accumulation" here means a manager's reported share count rose between
    two quarter-end snapshots. It does NOT mean they are buying now, and it
    cannot see intra-quarter round trips (a manager could have bought and
    sold repeatedly between the two dates).
  - A position appearing/disappearing may reflect the security dropping off
    the 13(f) list, a manager falling below the $100M filing threshold, or a
    late/amended filing — not necessarily a real trade. Counts are reported
    as what the filings say, never asserted as trades.
  - Quarter-over-quarter comparison requires BOTH quarters to have been
    ingested. compare_quarters() returns an explicit
    insufficient_history marker rather than silently treating a missing
    prior quarter as "position opened from zero", which would badly
    overstate accumulation on first run.
"""
from __future__ import annotations


def aggregate_by_ticker(holdings: list[dict]) -> dict[str, dict]:
    """Collapse many managers' holdings of the same ticker into one row per
    ticker for a single period."""
    by_ticker: dict[str, dict] = {}
    for h in holdings:
        ticker = h.get("ticker")
        if not ticker:
            continue  # unresolved CUSIPs are excluded from ticker-level views, not guessed at
        entry = by_ticker.setdefault(ticker, {
            "ticker": ticker, "issuer_name": h.get("issuer_name"),
            "holder_count": 0, "total_value_usd": 0.0, "total_shares": 0.0, "holders": [],
        })
        entry["holder_count"] += 1
        entry["total_value_usd"] += h.get("value_usd") or 0.0
        entry["total_shares"] += h.get("shares") or 0.0
        entry["holders"].append({
            "filer_name": h.get("filer_name"),
            "value_usd": h.get("value_usd"),
            "shares": h.get("shares"),
        })
    for entry in by_ticker.values():
        entry["total_value_usd"] = round(entry["total_value_usd"], 2)
        entry["holders"].sort(key=lambda x: -(x["value_usd"] or 0))
    return by_ticker


def compare_quarters(current: dict[str, dict], prior: dict[str, dict]) -> list[dict]:
    """Quarter-over-quarter change per ticker.

    If `prior` is empty (only one quarter ingested so far), every ticker is
    marked insufficient_history=True with null deltas rather than being
    reported as a brand-new 100% accumulation — see module docstring."""
    insufficient = not prior
    results = []
    for ticker, cur in current.items():
        prev = prior.get(ticker)
        if insufficient:
            results.append({
                **_base_row(cur),
                "insufficient_history": True,
                "prior_holder_count": None, "holder_delta": None,
                "prior_shares": None, "share_delta": None, "share_change_pct": None,
                "status": "no_prior_quarter",
            })
            continue

        if prev is None:
            results.append({
                **_base_row(cur),
                "insufficient_history": False,
                "prior_holder_count": 0, "holder_delta": cur["holder_count"],
                "prior_shares": 0.0, "share_delta": cur["total_shares"], "share_change_pct": None,
                "status": "newly_reported",
            })
            continue

        share_delta = cur["total_shares"] - prev["total_shares"]
        pct = (share_delta / prev["total_shares"] * 100) if prev["total_shares"] else None
        results.append({
            **_base_row(cur),
            "insufficient_history": False,
            "prior_holder_count": prev["holder_count"],
            "holder_delta": cur["holder_count"] - prev["holder_count"],
            "prior_shares": prev["total_shares"],
            "share_delta": round(share_delta, 2),
            "share_change_pct": round(pct, 2) if pct is not None else None,
            "status": "increased" if share_delta > 0 else "decreased" if share_delta < 0 else "unchanged",
        })

    results.sort(key=lambda r: abs(r["share_change_pct"] or 0), reverse=True)
    return results


def _base_row(cur: dict) -> dict:
    return {
        "ticker": cur["ticker"], "issuer_name": cur["issuer_name"],
        "holder_count": cur["holder_count"], "total_value_usd": cur["total_value_usd"],
        "total_shares": cur["total_shares"],
    }


def compute_institutional_component(row: dict | None) -> dict | None:
    """Signed -100..100 institutional-accumulation component for
    lib/signal_fusion.py's smart-money alignment.

    Returns None (contributes nothing) when history is insufficient — a
    single ingested quarter genuinely carries no accumulation information,
    and scoring it as neutral-50 would dilute the other components rather
    than abstain."""
    if not row or row.get("insufficient_history"):
        return None
    pct = row.get("share_change_pct")
    if pct is None:
        return None
    # +/-50% quarter-over-quarter share change reaches full magnitude. 13F
    # position changes are large and lumpy compared to insider buys, so this
    # is deliberately less sensitive than the insider component's scaling.
    magnitude = min(100.0, abs(pct) / 50 * 100)
    score = magnitude if pct > 0 else -magnitude
    return {
        "score": round(score, 1),
        "share_change_pct": pct,
        "holder_count": row.get("holder_count"),
        "holder_delta": row.get("holder_delta"),
        "status": row.get("status"),
    }
