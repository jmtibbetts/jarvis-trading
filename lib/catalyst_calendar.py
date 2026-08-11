"""
Universal catalyst calendar — assembled ONLY from sources this system already
has, each entry labeled with its real granularity and provenance:

  deterministic market dates (computed, exact):
    - monthly options expiration (3rd Friday). NOTE: exchange holiday
      adjustments are NOT applied — when the 3rd Friday is a holiday (e.g.
      Good Friday) the true expiration shifts; entries carry
      approximation="holiday_shifts_not_applied" rather than pretending
      exchange-calendar precision this module doesn't have.
    - quarterly futures expiration (3rd Friday of Mar/Jun/Sep/Dec), same
      approximation label.
    - 13F filing deadlines (45 days after each calendar quarter end) — these
      are statutory and exact.

  data-derived (from ingested feeds, granularity as stated):
    - earnings: tickers reporting THIS WEEK (the free source is
      week-granular; no per-day precision exists here and none is invented)
    - IPO pricings: 424B4-priced deals from the IPO pipeline (dated by
      filing date; the listing typically follows within days)

Deliberately absent: FOMC meeting dates, CPI release dates, and other
economic-release schedules — no free machine-readable forward calendar is
wired in, and hardcoding dates that silently go stale is worse than absence.
"""
from __future__ import annotations

from datetime import date, timedelta

QUARTERLY_MONTHS = (3, 6, 9, 12)


def third_friday(year: int, month: int) -> date:
    """The 3rd Friday of a month — standard monthly options expiration."""
    d = date(year, month, 1)
    # weekday(): Monday=0 ... Friday=4
    first_friday_day = 1 + (4 - d.weekday()) % 7
    return date(year, month, first_friday_day + 14)


def next_options_expirations(today: date, count: int = 3) -> list[dict]:
    out = []
    year, month = today.year, today.month
    while len(out) < count:
        expiry = third_friday(year, month)
        if expiry >= today:
            out.append({
                "date": expiry.isoformat(),
                "type": "OPTIONS_EXPIRATION",
                "title": "Monthly options expiration (3rd Friday)",
                "approximation": "holiday_shifts_not_applied",
                "days_away": (expiry - today).days,
            })
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def next_futures_expirations(today: date, count: int = 2) -> list[dict]:
    out = []
    year, month = today.year, today.month
    while len(out) < count:
        if month in QUARTERLY_MONTHS:
            expiry = third_friday(year, month)
            if expiry >= today:
                out.append({
                    "date": expiry.isoformat(),
                    "type": "FUTURES_EXPIRATION",
                    "title": "Quarterly index futures expiration (3rd Friday)",
                    "approximation": "holiday_shifts_not_applied",
                    "days_away": (expiry - today).days,
                })
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def next_13f_deadlines(today: date, count: int = 2) -> list[dict]:
    """Statutory: 13F-HR due 45 days after each calendar quarter end. Exact —
    a burst of institutional-holdings data lands around each deadline."""
    out = []
    year = today.year - 1
    quarter_ends = []
    for y in (year, year + 1, year + 2):
        quarter_ends += [date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)]
    for qe in quarter_ends:
        deadline = qe + timedelta(days=45)
        if deadline >= today and len(out) < count:
            out.append({
                "date": deadline.isoformat(),
                "type": "SEC_13F_DEADLINE",
                "title": f"13F filing deadline for quarter ended {qe.isoformat()}",
                "approximation": None,
                "days_away": (deadline - today).days,
            })
    return out


def assemble_calendar(today: date,
                      earnings_tickers: set[str] | None = None,
                      tracked_equities: set[str] | None = None,
                      priced_ipos: list[dict] | None = None) -> dict:
    """Merge deterministic dates with data-derived events, sorted by date.
    Earnings are week-granular and carried as a single entry listing affected
    tracked tickers — inventing per-day precision the source doesn't have
    would be fabrication."""
    events = (
        next_options_expirations(today)
        + next_futures_expirations(today)
        + next_13f_deadlines(today)
    )

    relevant_earnings = sorted(
        (earnings_tickers or set()) & (tracked_equities or set())
    ) if tracked_equities else sorted(earnings_tickers or set())
    if relevant_earnings:
        events.append({
            "date": today.isoformat(),
            "type": "EARNINGS_THIS_WEEK",
            "title": f"{len(relevant_earnings)} tracked ticker(s) report earnings this week",
            "tickers": relevant_earnings,
            "granularity": "week",
            "approximation": "source_is_week_granular",
            "days_away": 0,
        })

    for ipo in priced_ipos or []:
        events.append({
            "date": (ipo.get("latest_filed_at") or today.isoformat())[:10],
            "type": "IPO_PRICED",
            "title": f"IPO priced: {ipo.get('company_name')}"
                     + (f" ({ipo.get('ticker')})" if ipo.get("ticker") else ""),
            "ticker": ipo.get("ticker"),
            "approximation": "listing_follows_pricing_by_days",
            "days_away": None,
        })

    events.sort(key=lambda e: e["date"])
    return {
        "events": events,
        "note": (
            "Deterministic market dates plus events from ingested feeds only. "
            "Each entry states its granularity/approximation. Economic-release "
            "dates (FOMC, CPI) are absent by design — no free forward calendar "
            "is wired in, and stale hardcoded dates would be worse."
        ),
    }
