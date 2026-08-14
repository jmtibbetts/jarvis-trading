"""Futures contract identity — root vs contract vs continuous (Phase 4B).

The standing correctness hole this closes: every futures trade in the
system is recorded against a continuous symbol (CL=F) with no contract
identity, no expiry awareness, no roll provenance. A continuous series is
an ANALYTICAL CONVENIENCE — nobody has ever held a position in it. The
thing actually held is a specific contract (CLU26) with a last-trade date
and, for physical products, a first-notice date after which a long can be
assigned DELIVERY of 1,000 barrels of actual crude.

Three layers, kept distinct on purpose:

  root        CL      the product
  contract    CLU26   the tradable thing, with dates
  continuous  CL=F    a stitched series for analysis, never for holding

What this module provides:
  - month-code algebra and per-product listing cycles
  - expiry / first-notice rule engine (typical exchange conventions)
  - front-contract selection that SKIPS past-first-notice contracts —
    the "front month" a retail account can actually enter
  - delivery_risk(): the hard-block/roll-warning verdict the entry path
    consults before any futures position opens

SOURCE AND CAVEAT (same rule as FUTURES_SPECS): the date rules encode
CME/ICE published conventions and are deliberately CONSERVATIVE — the
block engages a margin of days before the true deadline, so an
approximation error keeps a position OUT of the delivery window, never
in it. Before real money, verify each product's calendar against the
broker that will actually fill you. Futures are paper-only today; the
reason this exists NOW is that the learning ledger must not train on
entries a real account could never have made.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
CODE_BY_MONTH = {v: k for k, v in MONTH_CODES.items()}

# Listing cycles per root — the months that actually trade with liquidity.
# (CL/NG list every month; metals and grains follow their delivery cycles;
# index futures are quarterly.)
CYCLES = {
    "CL": tuple(MONTH_CODES),                  # monthly
    "NG": tuple(MONTH_CODES),                  # monthly
    "GC": ("G", "J", "M", "Q", "V", "Z"),
    "SI": ("H", "K", "N", "U", "Z"),
    "HG": ("H", "K", "N", "U", "Z"),
    "PL": ("F", "J", "N", "V"),
    "ES": ("H", "M", "U", "Z"),
    "NQ": ("H", "M", "U", "Z"),
    "YM": ("H", "M", "U", "Z"),
    "RTY": ("H", "M", "U", "Z"),
    "ZC": ("H", "K", "N", "U", "Z"),
    "ZW": ("H", "K", "N", "U", "Z"),
    "ZS": ("F", "H", "K", "N", "Q", "U", "X"),
}

# Cash-settled products cannot deliver anything; their risk is expiry
# itself, not a notice window.
CASH_SETTLED = {"ES", "NQ", "YM", "RTY"}

# Micro contracts share their parent's calendar exactly.
MICRO_PARENT = {"MES": "ES", "MNQ": "NQ", "MYM": "YM", "M2K": "RTY",
                "MCL": "CL", "MGC": "GC", "SIL": "SI"}

# The delivery-risk margins. Conservative on purpose: blocked means "a
# careful retail account would already have rolled".
BLOCK_DAYS_BEFORE_RISK = 2      # inside this → no new entries
ROLL_WARN_DAYS = 7              # inside this → enter with a roll warning


def _busday_back(d: date, n: int) -> date:
    """n business days before d (weekends only — exchange holidays are the
    per-broker verification this module's caveat demands)."""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _last_busday_of_month(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def last_trade_date(root: str, month: int, year: int) -> date:
    """Typical last-trading-day conventions per product family."""
    root = MICRO_PARENT.get(root, root)
    if root == "CL":
        # 3 business days before the 25th of the month PRECEDING delivery.
        py, pm = _prev_month(year, month)
        d25 = date(py, pm, 25)
        while d25.weekday() >= 5:
            d25 -= timedelta(days=1)
        return _busday_back(d25, 3)
    if root == "NG":
        # 3 business days before the first day of the delivery month.
        return _busday_back(date(year, month, 1), 3)
    if root in ("GC", "SI", "HG", "PL"):
        # Third-to-last business day of the delivery month.
        d = _last_busday_of_month(year, month)
        return _busday_back(d, 2)
    if root in CASH_SETTLED:
        # Third Friday of the contract month.
        return _nth_weekday(year, month, 4, 3)
    if root in ("ZC", "ZW", "ZS"):
        # Business day prior to the 15th of the delivery month.
        return _busday_back(date(year, month, 15), 1)
    # Unknown product: the conservative answer is "expired yesterday" —
    # an unknown calendar must fail closed, not trade forever.
    return date(1970, 1, 1)


def first_notice_date(root: str, month: int, year: int) -> date | None:
    """First notice day — after this, a long physical position can be
    assigned delivery. None for cash-settled products."""
    root = MICRO_PARENT.get(root, root)
    if root in CASH_SETTLED:
        return None
    if root in ("GC", "SI", "HG", "PL", "ZC", "ZW", "ZS"):
        # Last business day of the month before delivery.
        py, pm = _prev_month(year, month)
        return _last_busday_of_month(py, pm)
    if root in ("CL", "NG"):
        # Energy: notice follows last trade almost immediately; the binding
        # deadline for a retail long is last trade itself.
        return last_trade_date(root, month, year)
    return None


@dataclass(frozen=True)
class ContractIdentity:
    root: str
    month: int             # delivery/contract month
    year: int
    cash_settled: bool
    last_trade: date
    first_notice: date | None

    @property
    def code(self) -> str:
        """CLU26 — the identity a broker statement would show."""
        return f"{self.root}{CODE_BY_MONTH[self.month]}{self.year % 100:02d}"

    @property
    def risk_date(self) -> date:
        """The date a position must be gone by: min(first notice, last
        trade). For physical products with early notice this is FND —
        being long past it risks a delivery process, not just a roll."""
        if self.first_notice is not None and self.first_notice < self.last_trade:
            return self.first_notice
        return self.last_trade


def root_of(symbol: str) -> str | None:
    """CL=F / MCL=F / 'CL' -> 'CL'; None for anything non-futures."""
    s = str(symbol or "").upper().strip()
    if s.endswith("=F"):
        s = s[:-2]
    if s in CYCLES or s in MICRO_PARENT:
        return s
    return None


def contract(root: str, month_code: str, year: int) -> ContractIdentity:
    base = MICRO_PARENT.get(root, root)
    m = MONTH_CODES[month_code.upper()]
    return ContractIdentity(
        root=root, month=m, year=year,
        cash_settled=base in CASH_SETTLED,
        last_trade=last_trade_date(root, m, year),
        first_notice=first_notice_date(root, m, year),
    )


def listed_contracts(root: str, asof: date, n: int = 4) -> list[ContractIdentity]:
    """The next n contracts in the product's cycle whose risk date is
    still ahead — i.e. the ones an account could actually enter today."""
    base = MICRO_PARENT.get(root, root)
    cycle = CYCLES.get(base)
    if not cycle:
        return []
    out = []
    year = asof.year
    while len(out) < n and year < asof.year + 3:
        for code in cycle:
            c = contract(root, code, year)
            if c.risk_date > asof and len(out) < n:
                out.append(c)
        year += 1
    return out


def front_contract(root: str, asof: date) -> ContractIdentity | None:
    """The front month a retail account can actually hold: the nearest
    contract still BEFORE its risk date. This deliberately differs from
    'highest volume' — a contract past first notice may still print volume
    from commercials taking delivery, and following them into it is how a
    retail long ends up with a delivery notice."""
    nxt = listed_contracts(root, asof, n=1)
    return nxt[0] if nxt else None


def delivery_risk(symbol: str, asof: date | None = None) -> dict:
    """The entry-path verdict for a futures symbol (continuous or root).

    ok           front contract has comfortable runway
    roll_window  entries allowed; the position will need to roll soon and
                 the horizon should respect that
    blocked      no new entries — the tradable front is inside the
                 conservative margin of its risk date
    """
    asof = asof or date.today()
    root = root_of(symbol)
    if root is None:
        return {"level": "ok", "reason": "not a tracked futures product",
                "front": None}
    front = front_contract(root, asof)
    if front is None:
        return {"level": "blocked", "reason": f"no listed contract found for {root}",
                "front": None}
    days = (front.risk_date - asof).days
    kind = "last trade" if front.cash_settled else "first notice/last trade"
    out = {"front": front.code, "risk_date": front.risk_date.isoformat(),
           "days_to_risk": days, "cash_settled": front.cash_settled}
    if days <= BLOCK_DAYS_BEFORE_RISK:
        return {**out, "level": "blocked",
                "reason": f"{front.code} is {days}d from {kind} — roll, don't enter"}
    if days <= ROLL_WARN_DAYS:
        return {**out, "level": "roll_window",
                "reason": f"{front.code} is {days}d from {kind} — position must roll soon"}
    return {**out, "level": "ok", "reason": f"{front.code} has {days}d of runway"}
