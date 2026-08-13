"""How long a setup is supposed to take, in one place.

The chart timeframe is what says whether a position is a 25-minute scalp or
a three-week hold, and the same adverse move means opposite things in those
two cases: 3% kills a 5-minute trade and is ordinary noise on a weekly.

The hold-estimate table already existed in THREE copies — the signals API,
the Telegram formatter, and the signal card — each a bare string map, none
usable by the position-management loop, which therefore managed every trade
as though it shared one horizon. This module is the single source: the same
numbers produce the human label those three already showed AND the minute
windows the exit logic needs.
"""
from __future__ import annotations

from datetime import datetime, timezone

# (typical_min_minutes, typical_max_minutes, human label)
#
# Roughly 5-20 bars of the signal's own timeframe — the span over which the
# setup either works or is refuted. Labels are kept verbatim from what the
# UI and Telegram already displayed, so this consolidation changes no text
# the operator is used to reading.
HORIZONS: dict[str, tuple[int, int, str]] = {
    "1m":  (5, 20, "<30 min"),
    "3m":  (15, 60, "<30 min"),
    "5m":  (25, 90, "<1 hr"),
    "15m": (60, 300, "1-4 hr"),
    "30m": (120, 600, "2-8 hr"),
    "1H":  (240, 1_200, "4-24 hr"),
    "2H":  (480, 2_400, "1-3 days"),
    "4H":  (960, 4_320, "1-5 days"),
    "1D":  (4_320, 20_160, "1-4 weeks"),
    "1W":  (20_160, 80_640, "1-3 months"),
}

# An unknown timeframe is given hours, not minutes or weeks: the middle of
# the range, so a missing value cannot silently turn a scalp into a hold or
# a hold into a scalp.
DEFAULT_HORIZON = (240, 1_440, "varies")

SCALP_TIMEFRAMES = {"1m", "3m", "5m", "15m"}
LONGER_TIMEFRAMES = {"30m", "1H", "2H", "4H", "1D", "1W"}

# The trader-facing category, duplicated alongside the hold map in the same
# three places. Kept verbatim; "1W" is new — the other copies simply had no
# row for it and fell through to "position", which is where it belongs anyway.
CATEGORY: dict[str, str] = {
    "1m": "scalp", "3m": "scalp", "5m": "scalp",
    "15m": "intraday", "30m": "intraday", "1H": "intraday",
    "2H": "swing", "4H": "swing",
    "1D": "position", "1W": "position",
}
DEFAULT_CATEGORY = "position"


def category(timeframe: str | None) -> str:
    return CATEGORY.get(_normalize(timeframe), DEFAULT_CATEGORY)


def category_map() -> dict[str, str]:
    return dict(CATEGORY)


def _normalize(timeframe: str | None) -> str:
    return str(timeframe or "").strip()


def horizon(timeframe: str | None) -> tuple[int, int, str]:
    return HORIZONS.get(_normalize(timeframe), DEFAULT_HORIZON)


def hold_estimate(timeframe: str | None) -> str:
    """The label the signal card and Telegram already show."""
    return horizon(timeframe)[2]


def expected_hold_minutes(timeframe: str | None) -> tuple[int, int]:
    return horizon(timeframe)[0], horizon(timeframe)[1]


def is_scalp(timeframe: str | None) -> bool:
    return _normalize(timeframe) in SCALP_TIMEFRAMES


def hold_map() -> dict[str, str]:
    """timeframe -> label, for the API and Telegram formatters that want the
    whole table rather than one lookup."""
    return {tf: label for tf, (_lo, _hi, label) in HORIZONS.items()}


def format_duration(minutes: float) -> str:
    m = max(0.0, float(minutes))
    if m < 90:
        return f"{m:.0f} min"
    if m < 2_880:
        return f"{m / 60:.1f} h"
    if m < 20_160:
        return f"{m / 1_440:.1f} days"
    return f"{m / 10_080:.1f} weeks"


def age_minutes(opened_at: str | None) -> float | None:
    if not opened_at:
        return None
    try:
        t = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return None


def room_multiplier(timeframe: str | None) -> float:
    """How much wider a trailing stop should sit for this timeframe.

    A trail calibrated for a 5-minute chart applied to a daily position stops
    it out on its first ordinary pullback — the thesis never gets the room it
    needs. Derived from the expected hold rather than a second table, so the
    two cannot drift apart.

    Square-root damped: a 1D hold is 18x a 1H hold but does not want an 18x
    wider trail.
    """
    lo, _hi = expected_hold_minutes(timeframe)
    base_lo, _ = expected_hold_minutes("1H")
    return max(0.6, min(4.0, (lo / base_lo) ** 0.5))


def hold_status(timeframe: str | None, opened_at: str | None,
                stale_multiple: float = 3.0) -> dict:
    """Where this position sits against the clock its own setup implies.

    A trade that has run several times past its expected hold without
    resolving is no longer the trade that was entered — the setup has been
    refuted by time rather than by price, which no stop-loss can express.
    """
    lo, hi, label = horizon(timeframe)
    age = age_minutes(opened_at)
    if age is None:
        return {"timeframe": timeframe, "label": label, "age_min": None,
                "state": "unknown", "summary": f"expected hold {label}"}
    if age < lo:
        state = "early"
    elif age <= hi:
        state = "within expected hold"
    elif age <= hi * stale_multiple:
        state = "overdue"
    else:
        state = "stale"
    return {
        "timeframe": timeframe,
        "label": label,
        "age_min": round(age, 1),
        "expected_min": lo,
        "expected_max": hi,
        "state": state,
        "summary": (f"open {format_duration(age)} on a {timeframe or '?'} setup "
                    f"(expected {label}) — {state}"),
    }
