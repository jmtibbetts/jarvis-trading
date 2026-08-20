"""What THIS ACCOUNT actually pays, and which of its money is actually its own.

TWO FACTS THE PUBLIC SCHEDULE CANNOT TELL YOU, both of which a manual desk
runs into immediately.

── 1. A PUBLIC FEE SCHEDULE IS NOT AN EFFECTIVE FEE ──────────────────────

`lib/venues.py` knows what a venue charges the world. It cannot know that
this account is in a maker-rebate tier, has a 30-day volume discount, or is
inside a promotional window where BTC and ETH trade at zero commission.

Those are ACCOUNT ENTITLEMENTS, and they must not be written into the venue
schedule. A schedule edited to match one account's promotion stops
describing the venue, so every OTHER consumer — sizing, the expectancy gate,
the catastrophic product test, any second account — silently inherits a
discount it does not have. Worse, promotions END. A temporary waiver baked
into a permanent table outlives itself and keeps quietly discounting trades
long after the operator started paying full price again.

So the schedule stays public and untouched, entitlements are separate,
time-bounded and evidenced, and the effective view is COMPUTED from both
while preserving each input. `public_usd` and `effective_usd` are both
reported, always, because the difference between them is the entitlement's
actual worth and nobody can measure it afterwards if only the answer was
kept.

── 2. ZERO FEE IS NOT ZERO COST ──────────────────────────────────────────

An entitlement covers the categories it names. NOTHING ELSE. A zero-commission
promotion does not make the spread free, the funding free, the gas free or a
liquidation penalty free — and `apply()` reports every category it did NOT
touch precisely so a reader cannot mistake a covered commission for a costless
trade. A trade whose only modelled cost was commission looks free under a
promotion, and it is not.

── 3. PROMOTIONAL CAPITAL IS NOT OWNED CAPITAL ───────────────────────────

A 100% deposit match doubles what can be traded. It does not double what is
owned, and it does not become withdrawable equity or realized profit because
it was recorded. Venues attach terms — volume thresholds, withdrawal locks,
expiry, forfeiture on withdrawal — and none of them are guessable from the
number.

So capital carries a KIND, ownership is a property of the kind rather than of
the amount, and the fail-closed direction is chosen deliberately: capital
whose kind is UNKNOWN is NOT owned. Treating unproven capital as owned
inflates equity, inflates the denominator of every return, and would let a
book report profit it cannot withdraw. Treating owned capital as unproven
understates a number a human can correct. Only one of those errors is
recoverable.

THIS MODULE DECIDES NOTHING ABOUT MONEY MOVEMENT. It classifies and it
computes views. No balance is credited here, and nothing here may fund any
book — virtual or otherwise.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ACCOUNT_ECONOMICS_VERSION = "account_economics_v1"

UNKNOWN = "UNKNOWN"


# ── Capital kinds ────────────────────────────────────────────────────────
OWN_CAPITAL = "OWN_CAPITAL"
PROMOTIONAL_CREDIT = "PROMOTIONAL_CREDIT"
TRADING_BONUS = "TRADING_BONUS"
VENUE_CREDIT = "VENUE_CREDIT"
BORROWED_CAPITAL = "BORROWED_CAPITAL"
CAPITAL_UNKNOWN = "CAPITAL_UNKNOWN"

CAPITAL_KINDS = (OWN_CAPITAL, PROMOTIONAL_CREDIT, TRADING_BONUS,
                 VENUE_CREDIT, BORROWED_CAPITAL, CAPITAL_UNKNOWN)


@dataclass(frozen=True)
class CapitalKindSpec:
    """Whether this kind of money is the account's, and whether it can leave.

    `owned` and `withdrawable` are `bool | "UNKNOWN"`. UNKNOWN is a real
    third state: a venue's promotional terms may genuinely be unread, and
    saying so is more useful than a confident False that hides the gap.
    But UNKNOWN never counts as owned — see `is_owned_capital`.
    """

    kind: str
    owned: bool | str
    withdrawable: bool | str
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


_CAPITAL: dict[str, CapitalKindSpec] = {
    OWN_CAPITAL: CapitalKindSpec(
        OWN_CAPITAL, owned=True, withdrawable=True,
        reason="the operator's own money, deposited and unencumbered"),
    PROMOTIONAL_CREDIT: CapitalKindSpec(
        PROMOTIONAL_CREDIT, owned=False, withdrawable=False,
        reason=("issued by the venue under promotional terms. It may size a "
                "position; it is not equity and does not become equity by "
                "being recorded. Only the venue's authoritative terms can "
                "convert it, and those terms are not modelled here")),
    TRADING_BONUS: CapitalKindSpec(
        TRADING_BONUS, owned=False, withdrawable=UNKNOWN,
        reason=("usable as margin, convertible only on conditions the venue "
                "sets — volume thresholds, holding periods, forfeiture on "
                "withdrawal. Unread terms stay UNKNOWN")),
    VENUE_CREDIT: CapitalKindSpec(
        VENUE_CREDIT, owned=False, withdrawable=UNKNOWN,
        reason="a venue-issued credit; terms govern, and are not assumed"),
    BORROWED_CAPITAL: CapitalKindSpec(
        BORROWED_CAPITAL, owned=False, withdrawable=False,
        reason=("extended by a counterparty and repayable. The position it "
                "carries is the account's; the principal is not")),
    CAPITAL_UNKNOWN: CapitalKindSpec(
        CAPITAL_UNKNOWN, owned=UNKNOWN, withdrawable=UNKNOWN,
        reason=("provenance not established. NOT treated as owned: unproven "
                "capital counted as equity inflates the book and every "
                "return computed against it")),
}


class AccountEconomicsError(ValueError):
    """An unknown capital kind, cost category, or malformed entitlement."""


def capital_spec(kind: str) -> CapitalKindSpec:
    """The kind's contract. An unrecognised kind RAISES rather than defaults.

    A permissive default here would be a default about OWNERSHIP.
    """
    found = _CAPITAL.get(str(kind or "").strip().upper())
    if found is None:
        raise AccountEconomicsError(
            f"{kind!r} is not a capital kind. Known kinds: "
            f"{', '.join(CAPITAL_KINDS)}")
    return found


def is_owned_capital(kind: str) -> bool:
    """FAIL-CLOSED. Only capital proven to be the account's returns True.

    UNKNOWN is not owned. This is the whole guard against a deposit match
    becoming equity by the act of writing it down.
    """
    return capital_spec(kind).owned is True


def owned_amount_usd(amount_usd: float | None, kind: str) -> float | None:
    """The part of an amount that is genuinely the account's.

    None in, None out — an unmeasured amount does not become zero owned
    capital, it stays unmeasured.
    """
    if amount_usd is None:
        return None
    return float(amount_usd) if is_owned_capital(kind) else 0.0


# ── Cost categories ──────────────────────────────────────────────────────
# THE CANONICAL SET, mirrored from `lib.realized_outcome.RealizedOutcome`'s
# EXPLICIT LEDGER CHARGES. Named rather than invented so an entitlement can
# only ever waive a cost the outcome record actually carries; a promotion
# that covers a category nothing charges is a promotion that covers nothing.
# `tests/test_manual_operator_execution.py` pins this against the dataclass
# so the two cannot drift apart silently.
COST_CATEGORIES = (
    "commission_usd",
    "regulatory_fees_usd",
    "pool_fees_usd",
    "network_fees_usd",
    "funding_usd",
    "borrow_cost_usd",
    "rollover_usd",
)

# Entitlement kinds. Each names a different reason the account pays
# something other than the public number.
FEE_TIER = "FEE_TIER"                    # standing, volume-based
VOLUME_DISCOUNT = "VOLUME_DISCOUNT"      # standing, thresholded
MAKER_REBATE = "MAKER_REBATE"            # the venue pays the account
PROMOTION = "PROMOTION"                  # TEMPORARY. Has an end.
FEE_WAIVER = "FEE_WAIVER"                # a category set to zero
TRADING_CREDIT = "TRADING_CREDIT"        # credit against fees

ENTITLEMENT_KINDS = (FEE_TIER, VOLUME_DISCOUNT, MAKER_REBATE, PROMOTION,
                     FEE_WAIVER, TRADING_CREDIT)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise AccountEconomicsError(f"unreadable timestamp {value!r}")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class AccountEntitlement:
    """One reason this account's cost differs from the venue's public one.

    Bound to an ACCOUNT and a VENUE, not to a venue alone — that binding is
    what stops it from leaking into the public schedule.

    `covers` names the cost categories affected and nothing else. A
    zero-commission promotion carries `("commission_usd",)`; it says
    nothing about funding, and `apply()` will not let it pretend otherwise.
    """

    entitlement_id: str
    account_label: str
    venue: str
    kind: str
    covers: tuple = ()

    # Exactly one of these expresses the effect.
    #   waive        -> the covered categories become 0.0
    #   rate_multiplier -> the covered categories scale (0.5 = half price;
    #                   a negative product is a rebate and is allowed)
    waive: bool = False
    rate_multiplier: float | None = None

    # TEMPORARY BY DEFAULT FOR PROMOTIONS. A promotion with no end date is
    # refused in __post_init__: an unbounded "temporary" waiver is exactly
    # the artefact that keeps discounting trades after it has expired.
    effective_from: str | None = None
    effective_until: str | None = None

    # Where this came from, ranked by `lib.venue_reconciliation`'s
    # authorities. An entitlement is a claim about money and needs one.
    evidence_type: str = "MODEL_DERIVED"
    evidence_source: str | None = None

    # Which products/symbols it applies to. Empty means "all at this venue".
    products: tuple = ()
    symbols: tuple = ()

    notes: str = ""
    version: str = ACCOUNT_ECONOMICS_VERSION

    def __post_init__(self):
        if self.kind not in ENTITLEMENT_KINDS:
            raise AccountEconomicsError(
                f"{self.kind!r} is not an entitlement kind. Known: "
                f"{', '.join(ENTITLEMENT_KINDS)}")
        unknown = [c for c in self.covers if c not in COST_CATEGORIES]
        if unknown:
            raise AccountEconomicsError(
                f"entitlement {self.entitlement_id!r} covers "
                f"{unknown!r}, which are not cost categories. An entitlement "
                f"may only modify a cost the outcome record actually carries")
        if not self.covers:
            raise AccountEconomicsError(
                f"entitlement {self.entitlement_id!r} covers no cost "
                f"category — an entitlement that names nothing modifies "
                f"nothing and must not be recorded as a discount")
        if self.waive and self.rate_multiplier is not None:
            raise AccountEconomicsError(
                f"entitlement {self.entitlement_id!r} both waives and "
                f"rescales; the two express different effects and combining "
                f"them leaves the result undefined")
        if not self.waive and self.rate_multiplier is None:
            raise AccountEconomicsError(
                f"entitlement {self.entitlement_id!r} states no effect")
        if self.rate_multiplier is not None:
            m = float(self.rate_multiplier)
            if not math.isfinite(m):
                raise AccountEconomicsError(
                    f"entitlement {self.entitlement_id!r} rate_multiplier "
                    f"{self.rate_multiplier!r} is not a finite number")
        # A PROMOTION IS TEMPORARY BY DEFINITION.
        if self.kind == PROMOTION and not self.effective_until:
            raise AccountEconomicsError(
                f"promotion {self.entitlement_id!r} has no effective_until. "
                f"A promotion without an end is a permanent schedule change "
                f"wearing a temporary name, and it will keep discounting "
                f"trades after the real promotion has ended")
        _parse_ts(self.effective_from)
        _parse_ts(self.effective_until)

    def active_at(self, at: str | None) -> bool:
        """Whether this entitlement was in force at a given moment.

        An UNREADABLE or ABSENT timestamp is NOT active. Applying a
        time-bounded waiver to a trade whose time is unknown is how an
        expired promotion reaches a trade that happened after it.
        """
        t = _parse_ts(at)
        if t is None:
            return False
        start, end = _parse_ts(self.effective_from), _parse_ts(self.effective_until)
        if start and t < start:
            return False
        if end and t > end:
            return False
        return True

    def applies_to(self, *, venue: str, product: str | None,
                   symbol: str | None) -> bool:
        if str(self.venue or "").upper() != str(venue or "").upper():
            return False
        if self.products and str(product or "").upper() not in {
                str(p).upper() for p in self.products}:
            return False
        if self.symbols and str(symbol or "").upper() not in {
                str(s).upper() for s in self.symbols}:
            return False
        return True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class EffectiveCostView:
    """The public cost, the account cost, and what stands between them.

    Both sides are kept. `public_usd` is what `lib/venues.py` says the venue
    charges; `effective_usd` is what this account paid. Reporting only the
    second makes an entitlement invisible, and an invisible entitlement
    cannot be audited, valued, or noticed when it expires.
    """

    account_label: str
    venue: str
    public_usd: dict = field(default_factory=dict)
    effective_usd: dict = field(default_factory=dict)
    applied: list = field(default_factory=list)
    untouched_categories: tuple = ()
    at: str | None = None
    version: str = ACCOUNT_ECONOMICS_VERSION

    @property
    def entitlement_value_usd(self) -> float | None:
        """What the entitlements were worth on this trade.

        None when either side has an UNKNOWN category — a saving computed
        against a missing cost is not a saving, it is a guess.
        """
        if any(v is None for v in self.public_usd.values()):
            return None
        if any(v is None for v in self.effective_usd.values()):
            return None
        return (sum(self.public_usd.values())
                - sum(self.effective_usd.values()))

    def as_dict(self) -> dict:
        return {
            "account_label": self.account_label,
            "venue": self.venue,
            "public_usd": dict(self.public_usd),
            "effective_usd": dict(self.effective_usd),
            "applied": list(self.applied),
            "untouched_categories": list(self.untouched_categories),
            "entitlement_value_usd": self.entitlement_value_usd,
            "at": self.at,
            "version": self.version,
            "note": ("effective_usd differs from public_usd only in the "
                     "categories an entitlement named. Every other cost "
                     "stands: a waived commission does not make spread, "
                     "funding, gas or a liquidation penalty free"),
        }


def apply(public_costs: dict, entitlements, *, account_label: str,
          venue: str, at: str | None, product: str | None = None,
          symbol: str | None = None) -> EffectiveCostView:
    """Compute the account-effective cost view. THE SCHEDULE IS NOT TOUCHED.

    `public_costs` maps cost categories to USD, where None means UNKNOWN.
    An UNKNOWN cost stays UNKNOWN through an entitlement: multiplying an
    unmeasured cost by 0.5 yields an unmeasured cost, and waiving one
    yields... an unmeasured cost, because a category nobody measured may
    not have been charged under this entitlement at all. Zero would be a
    claim; None is the fact.
    """
    unknown_cats = [c for c in public_costs if c not in COST_CATEGORIES]
    if unknown_cats:
        raise AccountEconomicsError(
            f"{unknown_cats!r} are not cost categories: "
            f"{', '.join(COST_CATEGORIES)}")

    effective = dict(public_costs)
    applied: list = []
    touched: set = set()

    for ent in entitlements or []:
        if not ent.applies_to(venue=venue, product=product, symbol=symbol):
            continue
        if str(ent.account_label) != str(account_label):
            continue
        if not ent.active_at(at):
            continue
        changed = []
        for cat in ent.covers:
            if cat not in effective:
                continue
            current = effective[cat]
            if current is None:
                # UNKNOWN survives the entitlement. See the docstring.
                continue
            new = (0.0 if ent.waive
                   else float(current) * float(ent.rate_multiplier))
            if new != current:
                changed.append({"category": cat, "from": current, "to": new})
            effective[cat] = new
            touched.add(cat)
        if changed or any(c in effective for c in ent.covers):
            applied.append({
                "entitlement_id": ent.entitlement_id,
                "kind": ent.kind,
                "covers": list(ent.covers),
                "evidence_type": ent.evidence_type,
                "evidence_source": ent.evidence_source,
                "effective_until": ent.effective_until,
                "changes": changed,
            })

    return EffectiveCostView(
        account_label=account_label, venue=venue,
        public_usd=dict(public_costs), effective_usd=effective,
        applied=applied,
        untouched_categories=tuple(
            c for c in public_costs if c not in touched),
        at=at)
