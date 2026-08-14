"""Sector engines (4C) — stored facts become named shadow features.

One engine, a registry of sectors. Energy shipped first because its
fundamentals run deepest (44 years of EIA history); metals and index are
the SAME machinery pointed at their own COT rows and curve roots — a
sector without a fundamentals feed says so by omission, it does not get
a fabricated one.

Everything here is DERIVED, deterministically, from release-stamped rows
in the raw event store — which is what makes it point-in-time by
construction: `asof` filters on the RELEASE timestamp, so asking "what
did the desk know last Tuesday?" replays exactly the information set of
last Tuesday. Nothing is stored back; a derived number with a single
source of truth cannot drift from it.

The features and the conventions behind them:

  stocks_seasonal_z    this week's inventory LEVEL vs the same ISO week
                       over the prior 5 years (EIA's own 5-year-average
                       framing). Raw levels across 44 years mix a secular
                       trend into the seasonality; the 5-year window is
                       what keeps the z about the season.
  stocks_change_z      the week-over-week CHANGE vs all changes in the
                       trailing 3 years — "is this build big?" needs the
                       distribution of builds, not of levels.
  cot_spec_pctile_3y   speculator (noncommercial) net position as a
                       percentile of its trailing 3-year range — the
                       classic COT index. 100 = most long in 3 years.
  curve                latest snapshot verbatim: structure, roll economics
                       and the front contract's identity.

Source health is part of the answer, not a side channel: every block
carries the age of the release it stands on, and a source older than its
own cadence allows makes the block abstain (§43) rather than serve a
number wearing a fresher date than it has.

Shadow-only, like every 4A/4B feed. The OOS ablation decides what earns
influence; this module just makes the candidates NAMED and computable at
any historical instant.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# A weekly source more than ~2 cycles stale is a broken feed, not data.
MAX_WEEKLY_AGE_DAYS = 16
SEASONAL_YEARS = 5
CHANGE_WINDOW_YEARS = 3
COT_WINDOW_YEARS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _series(symbol: str, series: str, asof: datetime) -> list[tuple[date, float]]:
    """(as_of_date, value) rows RELEASED by `asof`, oldest first.

    exchange_ts on official stats is the release stamp; filtering on it —
    not on as_of — is the entire point-in-time discipline. A row released
    after `asof` did not exist for the desk at `asof`.
    """
    from lib.event_store import get_store

    out = []
    for row in get_store().read(symbol, "official_stat", since_ts=0,
                                limit=5000):
        if row.get("series") != series:
            continue
        rel = row.get("exchange_ts")
        if rel is None or rel > asof.timestamp():
            continue
        try:
            out.append((date.fromisoformat(str(row["as_of"])[:10]),
                        float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def _seasonal_z(history: list[tuple[date, float]],
                current: tuple[date, float],
                years: int = SEASONAL_YEARS) -> float | None:
    """Level vs the same ISO week (±1) over the prior `years` years."""
    cur_date, cur_value = current
    week = cur_date.isocalendar()[1]
    sample = [v for d, v in history
              if d < cur_date
              and (cur_date - d).days <= years * 366
              and abs(d.isocalendar()[1] - week) <= 1]
    if len(sample) < 8:
        return None
    mean = sum(sample) / len(sample)
    var = sum((v - mean) ** 2 for v in sample) / len(sample)
    if var <= 0:
        return None
    return round((cur_value - mean) / var ** 0.5, 3)


def _change_z(history: list[tuple[date, float]],
              years: int = CHANGE_WINDOW_YEARS) -> tuple[float, float] | None:
    """(latest w/w change, its z vs trailing changes)."""
    if len(history) < 10:
        return None
    cutoff = history[-1][0].toordinal() - years * 366
    deltas = [b[1] - a[1] for a, b in zip(history, history[1:])
              if b[0].toordinal() >= cutoff]
    if len(deltas) < 8:
        return None
    latest = deltas[-1]
    prior = deltas[:-1]
    mean = sum(prior) / len(prior)
    var = sum((v - mean) ** 2 for v in prior) / len(prior)
    if var <= 0:
        return None
    return round(latest, 2), round((latest - mean) / var ** 0.5, 3)


def _pctile(history: list[tuple[date, float]],
            years: int = COT_WINDOW_YEARS) -> float | None:
    """Latest value's percentile within the trailing window, inclusive."""
    if len(history) < 12:
        return None
    cutoff = history[-1][0].toordinal() - years * 366
    window = [v for d, v in history if d.toordinal() >= cutoff]
    if len(window) < 12:
        return None
    latest = window[-1]
    return round(100.0 * sum(1 for v in window if v <= latest) / len(window), 1)


def _latest_curve(root: str, asof: datetime) -> dict | None:
    from lib.event_store import get_store

    rows = [r for r in get_store().read(root, "curve_snapshot",
                                        since_ts=0, limit=2000)
            if (r.get("ingest_ts") or 0) <= asof.timestamp()]
    return rows[-1] if rows else None


def _fundamental_block(symbol: str, series: str, asof: datetime,
                       unit: str) -> dict:
    """One inventory series -> level, seasonal z, change z, health."""
    hist = _series(symbol, series, asof)
    if not hist:
        return {"abstain": "no released data", "series": series}
    cur_date, cur_value = hist[-1]
    age_days = (asof.date() - cur_date).days
    if age_days > MAX_WEEKLY_AGE_DAYS:
        return {"abstain": f"latest release describes {cur_date.isoformat()} "
                           f"({age_days}d old)", "series": series}
    chg = _change_z(hist)
    return {
        "series": series, "unit": unit,
        "level": cur_value, "as_of": cur_date.isoformat(),
        "age_days": age_days,
        "seasonal_z_5y": _seasonal_z(hist[:-1], hist[-1]),
        "wow_change": chg[0] if chg else None,
        "change_z_3y": chg[1] if chg else None,
        "history_n": len(hist),
    }


def _positioning_block(symbol: str, asof: datetime) -> dict:
    spec = _series(symbol, "cot_noncomm_net", asof)
    comm = _series(symbol, "cot_comm_net", asof)
    if not spec:
        return {"abstain": "no released COT"}
    cur_date, cur_value = spec[-1]
    age_days = (asof.date() - cur_date).days
    return {
        "spec_net": cur_value,
        "spec_pctile_3y": _pctile(spec),
        "comm_net": comm[-1][1] if comm else None,
        "as_of": cur_date.isoformat(),
        "age_days": age_days,
        # COT describes Tuesday and releases Friday; 12 days stale means a
        # missed release, not a slow one.
        **({"abstain": f"COT {age_days}d old"} if age_days > 12 else {}),
    }


def _curve_block(root: str, asof: datetime) -> dict:
    c = _latest_curve(root, asof)
    if c is None:
        return {"abstain": "no curve snapshot"}
    age_h = round((asof.timestamp() - (c.get("ingest_ts") or 0)) / 3600, 1)
    if age_h > 24:
        return {"abstain": f"curve snapshot {age_h}h old"}
    return {"structure": c.get("structure"),
            "annualized_roll_pct": c.get("annualized_roll_pct"),
            "slope_pct": c.get("slope_pct"),
            "front": c.get("front_code"), "age_hours": age_h}


# ── Sector registry ──────────────────────────────────────────────────────────
# Instruments per sector; `fundamentals` names a stored official series
# where one exists. Its ABSENCE for metals/index is deliberate honesty:
# COMEX warehouse stocks and index flow data have no keyless feed wired,
# and a sector without fundamentals must look like one, not carry a proxy.
SECTORS = {
    "energy": {
        "crude": {"stat_symbol": "CL=F", "curve_root": "CL",
                  "fundamentals": ("eia_crude_stocks_kbbl", "kbbl")},
        "natgas": {"stat_symbol": "NG=F", "curve_root": "NG",
                   "fundamentals": ("eia_natgas_storage_bcf", "bcf")},
    },
    "metals": {
        "gold": {"stat_symbol": "GC=F", "curve_root": "GC"},
        "silver": {"stat_symbol": "SI=F", "curve_root": "SI"},
        "copper": {"stat_symbol": "HG=F", "curve_root": "HG"},
    },
    "index": {
        "spx": {"stat_symbol": "ES=F", "curve_root": "ES"},
        "ndx": {"stat_symbol": "NQ=F", "curve_root": "NQ"},
    },
}


def sector_snapshot(sector: str, asof: datetime | None = None) -> dict:
    """The engine's one product: everything the desk knows about a sector
    AS OF a moment, from released data only. Call with a past `asof` and
    it replays that moment's information set exactly."""
    cfg = SECTORS.get(sector)
    if cfg is None:
        raise KeyError(f"unknown sector {sector!r} — "
                       f"registered: {sorted(SECTORS)}")
    asof = asof or _now()
    out = {"sector": sector, "asof": asof.isoformat(),
           "instruments": {},
           "note": ("point-in-time: filtered on release timestamps; derived "
                    "on demand from the raw event store; shadow-only")}
    for key, inst in cfg.items():
        block = {
            "positioning": _positioning_block(inst["stat_symbol"], asof),
            "curve": _curve_block(inst["curve_root"], asof),
        }
        if "fundamentals" in inst:
            series, unit = inst["fundamentals"]
            block["fundamentals"] = _fundamental_block(
                inst["stat_symbol"], series, asof, unit)
        out["instruments"][key] = block
    return out


def energy_snapshot(asof: datetime | None = None) -> dict:
    return sector_snapshot("energy", asof)
