"""How much of an authorized order the observed book can defensibly execute.

THE DISTINCTION THIS FILE EXISTS TO HOLD. Bitnomial publishes ten levels a
side and stops sending updates for levels that fall out of scope. So the
visible book is a FLOOR on liquidity, never a total, and there are two false
statements available:

    "the rest definitely fills"        — the fixed-slippage model's claim
    "the rest definitely cannot fill"  — the naive depth model's claim

Both are wrong. The truthful statement is narrower: *this* quantity is
supported by liquidity we can currently see, and beyond it we do not know.
Every state name here is chosen so that nothing downstream can round that
uncertainty into either direction.

SHRINK ONLY, NEVER ENLARGE. Risk decides the maximum quantity; execution may
only reduce it. Visible liquidity is not permission to take a larger
position — if the book happens to show ten times what risk authorized, risk
still wins. This is a one-way valve and is asserted as one.

NO PARTICIPATION FACTOR. Taking "25% of visible depth" would be an invented
constant of exactly the kind the 0.21% slippage turned out to be — a number
with no measurement behind it, applied to a venue it was never derived from.
When there is evidence for a participation model it can be added as a
versioned empirical model. Until then the cap is the visible book itself.

STALE DEPTH IS NOT DEPTH. A book that has not been refreshed recently
describes a market that may no longer exist, and sizing from it is the same
error as pricing from it. Freshness is checked here rather than trusted from
upstream.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# ── outcomes, named so they cannot be misread ────────────────────────────
FULLY_SUPPORTED_BY_VISIBLE_DEPTH = "FULLY_SUPPORTED_BY_VISIBLE_DEPTH"
VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN = (
    "VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN")
NO_EXECUTABLE_VISIBLE_LIQUIDITY = "NO_EXECUTABLE_VISIBLE_LIQUIDITY"
STALE_BOOK = "STALE_BOOK"
INVALID_BOOK = "INVALID_BOOK"
NO_MARKET_DATA = "NO_MARKET_DATA"

# NOT EVERY PRODUCT HAS A DEPTH FEED. Equities and spot pairs are priced
# from quotes with no published ladder, so there is no visible depth to size
# against and this authority has nothing to say. That is different from a
# perpetual whose book we DO receive and which is stale or empty — refusing
# a product merely because Bitnomial does not quote it would be the same
# category error as pricing a perpetual from spot.
DEPTH_NOT_PUBLISHED = "DEPTH_NOT_PUBLISHED"

# States in which a quantity may be submitted at all.
EXECUTABLE_STATES = frozenset({FULLY_SUPPORTED_BY_VISIBLE_DEPTH,
                               VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN,
                               DEPTH_NOT_PUBLISHED})

BUY = "BUY"
SELL = "SELL"

# A book older than this describes a market that may no longer exist.
# Deliberately tighter than the pricing staleness bound: sizing commits
# capital across several levels, and the deeper levels are the ones the feed
# updates least often.
MAX_BOOK_AGE_S = 5.0


@dataclass(frozen=True)
class Capability:
    """What may be submitted, and exactly why it is not more."""
    state: str
    risk_authorized_qty: float
    visible_executable_qty: float
    final_submittable_qty: float
    shrink_qty: float = 0.0
    shrink_reason: str | None = None
    levels_required: int = 0
    book_age_s: float | None = None
    book_state: str | None = None
    instrument_id: str | None = None
    quantity_unit: str | None = None
    multiplier: float | None = None
    detail: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return (self.state in EXECUTABLE_STATES
                and self.final_submittable_qty > 0)

    def as_provenance(self) -> dict:
        """What the settlement record should carry about this decision."""
        return {
            "capability_state": self.state,
            "risk_authorized_qty": self.risk_authorized_qty,
            "visible_executable_qty": self.visible_executable_qty,
            "final_submittable_qty": self.final_submittable_qty,
            "shrink_qty": self.shrink_qty,
            "shrink_reason": self.shrink_reason,
            "levels_required": self.levels_required,
            "book_age_s": self.book_age_s,
            "instrument_id": self.instrument_id,
            "quantity_unit": self.quantity_unit,
            "multiplier": self.multiplier,
        }


def _levels_for(levels, wanted: float) -> tuple[float, int]:
    """Walk best-first. Returns (quantity covered, levels touched)."""
    remaining = float(wanted)
    covered = 0.0
    used = 0
    for _price, size in levels or []:
        if remaining <= 0:
            break
        avail = float(size or 0)
        if avail <= 0:
            continue
        take = min(remaining, avail)
        covered += take
        remaining -= take
        used += 1
    return covered, used


def assess(*, side: str, risk_authorized_qty: float, book,
           instrument_id: str | None = None,
           quantity_unit: str | None = None,
           multiplier: float | None = None,
           expects_depth: bool = True,
           max_age_s: float = MAX_BOOK_AGE_S) -> Capability:
    """How much of `risk_authorized_qty` the visible book supports.

    `book` is the venue's own top-of-book dict — the same structure the
    market-data layer already publishes — carrying `depth_bids`,
    `depth_asks`, `state` and `age_s`. Quantities are in the venue's units,
    which for Bitnomial are CONTRACTS; the multiplier travels alongside for
    provenance and is deliberately NOT applied here.
    """
    base = dict(risk_authorized_qty=float(risk_authorized_qty or 0),
                visible_executable_qty=0.0, final_submittable_qty=0.0,
                instrument_id=instrument_id, quantity_unit=quantity_unit,
                multiplier=multiplier)

    if not book:
        if not expects_depth:
            # No ladder is published for this product at all. The authority
            # abstains rather than refusing: it has no evidence either way,
            # and silence is not a liquidity finding.
            base["visible_executable_qty"] = base["risk_authorized_qty"]
            base["final_submittable_qty"] = base["risk_authorized_qty"]
            return Capability(
                state=DEPTH_NOT_PUBLISHED,
                detail="this product publishes no depth ladder; visible-"
                       "liquidity sizing does not apply",
                **base)
        return Capability(state=NO_MARKET_DATA, shrink_reason=NO_MARKET_DATA,
                          shrink_qty=base["risk_authorized_qty"],
                          detail="a depth book was expected for this "
                                 "instrument and none was published",
                          **base)

    book_state = book.get("state")
    age = book.get("age_s")
    base_evidence = {"book_state": book_state, "age_s": age,
                     "market_state": book.get("market_state")}

    if book_state and book_state != "OK":
        return Capability(state=INVALID_BOOK, book_state=book_state,
                          book_age_s=age, shrink_reason=INVALID_BOOK,
                          shrink_qty=base["risk_authorized_qty"],
                          detail=f"book state {book_state!r}",
                          evidence=base_evidence, **base)

    if age is None or not math.isfinite(float(age)) or float(age) > max_age_s:
        return Capability(
            state=STALE_BOOK, book_state=book_state, book_age_s=age,
            shrink_reason=STALE_BOOK,
            shrink_qty=base["risk_authorized_qty"],
            detail=(f"book age {age}s exceeds {max_age_s}s — sizing from a "
                    f"market that may no longer exist is the same error as "
                    f"pricing from it"),
            evidence=base_evidence, **base)

    levels = book.get("depth_asks") if side == BUY else book.get("depth_bids")
    visible = float(sum(float(q or 0) for _p, q in (levels or [])))
    wanted = base["risk_authorized_qty"]

    if wanted <= 0:
        return Capability(state=NO_EXECUTABLE_VISIBLE_LIQUIDITY,
                          book_state=book_state, book_age_s=age,
                          shrink_reason="NOTHING_AUTHORIZED",
                          detail="risk authorized no quantity",
                          evidence=base_evidence, **base)

    if visible <= 0:
        return Capability(
            state=NO_EXECUTABLE_VISIBLE_LIQUIDITY, book_state=book_state,
            book_age_s=age, shrink_reason=NO_EXECUTABLE_VISIBLE_LIQUIDITY,
            shrink_qty=wanted,
            detail=f"no visible size on the {side} side",
            evidence=base_evidence, **base)

    covered, used = _levels_for(levels, wanted)
    # THE ONE-WAY VALVE. Never above what risk authorized, whatever the
    # book shows.
    final = min(wanted, covered, visible)
    base["visible_executable_qty"] = visible
    base["final_submittable_qty"] = final

    if final >= wanted - 1e-12:
        return Capability(
            state=FULLY_SUPPORTED_BY_VISIBLE_DEPTH, book_state=book_state,
            book_age_s=age, levels_required=used, shrink_qty=0.0,
            evidence=base_evidence, **base)

    return Capability(
        state=VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN,
        book_state=book_state, book_age_s=age, levels_required=used,
        shrink_qty=round(wanted - final, 12),
        shrink_reason=VISIBLE_DEPTH_EXHAUSTED_REMAINDER_UNKNOWN,
        detail=(f"{final:g} of {wanted:g} is supported by currently visible "
                f"depth; the remaining {wanted - final:g} is NOT known to be "
                f"unfillable — it is simply not observable from a ten-level "
                f"feed, so it is not claimed either way"),
        evidence=base_evidence, **base)
