"""What a venue can actually be traded through — discovered, never assumed.

KRAKEN PRO IS THE PRIMARY REAL-WORLD TARGET. JARVIS trains virtually, but
it should train under the product economics it will actually meet, so a
Kraken-tradable product should be simulated the way Kraken prices it.

THE TRAP THIS MODULE EXISTS FOR. Kraken Pro's interface offers stocks and
ETFs to eligible US clients. Kraken's public API Center documents Spot
REST/WS, Futures REST/WS and FIX — and no stock trading contract. Those
are different claims, and the gap between them is exactly the sort of
thing a system assumes its way across:

    "the UI can do it"  ->  "the account can do it"  ->  "the API can do it"

Each arrow is an inference, and the last one is false today. A desk that
made it would build a stock execution path against an endpoint that does
not exist, discover it at the worst possible moment, and in the meantime
train on economics nothing can reproduce.

So capability is a MEASURED PROPERTY with an explicit status:

    DOCUMENTED    a published API contract exists
    DISCOVERED    probed live and confirmed working
    UI_ONLY       the product is tradable by a human, not by this program
    ASSUMED       believed, unverified — never sufficient for execution
    UNSUPPORTED   confirmed absent

Only DOCUMENTED and DISCOVERED may carry execution. Everything else is
research: JARVIS may observe the market, form a thesis and measure what
would have happened, and may NOT pretend it could have placed the order.

ENTITLEMENT IS ALSO NOT CAPABILITY. Kraken distinguishes Level I from
Level II futures data by subscription, so "we can trade futures" does not
imply "we can see full depth". A fill model that assumes book depth it is
not entitled to receive is guessing with extra steps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CAPABILITY_VERSION = "venue_caps_v1"

# ── Capability status ────────────────────────────────────────────────────
DOCUMENTED = "DOCUMENTED"
DISCOVERED = "DISCOVERED"
UI_ONLY = "UI_ONLY"
ASSUMED = "ASSUMED"
UNSUPPORTED = "UNSUPPORTED"

# ONLY these two may back a simulated order that claims venue realism.
EXECUTABLE_STATUSES = frozenset({DOCUMENTED, DISCOVERED})

# ── Market data entitlement ──────────────────────────────────────────────
LEVEL_1 = "LEVEL_1"          # top of book
LEVEL_2 = "LEVEL_2"          # full depth
NO_DEPTH = "NO_DEPTH"
ENTITLEMENT_UNKNOWN = "UNKNOWN"


@dataclass
class VenueCapability:
    venue: str
    product: str
    status: str = ASSUMED
    api_surface: str | None = None       # REST | WS | FIX | none
    data_entitlement: str = ENTITLEMENT_UNKNOWN
    fee_tier: str | None = None
    spec_version: str | None = None
    checked_at: str | None = None
    reason: str | None = None
    source: str | None = None

    @property
    def executable(self) -> bool:
        return self.status in EXECUTABLE_STATUSES

    @property
    def research_only(self) -> bool:
        return not self.executable and self.status != UNSUPPORTED

    def require_executable(self) -> "VenueCapability":
        if not self.executable:
            raise VenueCapabilityError(
                f"{self.venue}/{self.product}: {self.status}"
                f"{f' — {self.reason}' if self.reason else ''}. Research may "
                f"continue; simulated execution claiming this venue may not.")
        return self

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return {**asdict(self), "executable": self.executable,
                "research_only": self.research_only}


class VenueCapabilityError(RuntimeError):
    """Raised when execution is claimed against an unverified capability."""


# ── The registry ─────────────────────────────────────────────────────────
# Statuses reflect PUBLISHED API CONTRACTS as of this writing, not what the
# Pro interface offers a human. Anything not verifiable from a documented
# contract starts at ASSUMED or UI_ONLY and must be discovered before it
# can carry execution.
_REGISTRY: dict[tuple[str, str], VenueCapability] = {}


def _reg(venue: str, product: str, **kw) -> None:
    _REGISTRY[(venue.lower(), product)] = VenueCapability(
        venue=venue.lower(), product=product, **kw)


# Kraken — the primary target.
_reg("kraken", "CRYPTO_SPOT", status=DOCUMENTED, api_surface="REST+WS+FIX",
     source="Kraken API Center: Spot REST/WebSocket/FIX",
     data_entitlement=LEVEL_2)
_reg("kraken", "CRYPTO_PERP", status=DOCUMENTED, api_surface="REST+WS",
     source="Kraken API Center: Futures REST/WebSocket",
     data_entitlement=ENTITLEMENT_UNKNOWN,
     reason="Level I vs Level II depends on account subscription — query it")
_reg("kraken", "COMMODITY_FUTURE", status=DOCUMENTED, api_surface="REST+WS",
     source="Kraken Futures — CME/CBOT/NYMEX/COMEX listed contracts",
     data_entitlement=ENTITLEMENT_UNKNOWN)
_reg("kraken", "INDEX_FUTURE", status=DOCUMENTED, api_surface="REST+WS",
     source="Kraken Futures — index contracts",
     data_entitlement=ENTITLEMENT_UNKNOWN)
# THE ONE THAT MATTERS. Tradable in Kraken Pro by a human; no stock
# trading contract published in the API Center. UI access is not API
# access, and inferring otherwise builds a path to an endpoint that does
# not exist.
_reg("kraken", "EQUITY_SPOT", status=UI_ONLY, api_surface=None,
     source="Kraken Pro supports 11,000+ US stocks/ETFs for eligible clients",
     reason=("no stock trading contract published in the Kraken API Center — "
             "UI availability is not API availability"))
_reg("kraken", "ETF_SPOT", status=UI_ONLY, api_surface=None,
     reason="same as EQUITY_SPOT — verify before building an execution path")

# Alpaca — retained as a HIGH-VALUE DATA SOURCE, not the primary venue.
_reg("alpaca", "EQUITY_SPOT", status=DOCUMENTED, api_surface="REST",
     source="Alpaca Trading API", data_entitlement=LEVEL_1)
_reg("alpaca", "CRYPTO_SPOT", status=DOCUMENTED, api_surface="REST",
     source="Alpaca Trading API")

# On-chain. No brokerage, no entitlement — the chain is the venue.
_reg("dex", "DEX_SPOT", status=DOCUMENTED, api_surface="RPC",
     source="Solana RPC / DEX programs", data_entitlement=NO_DEPTH,
     reason="pool reserves are the book; depth certainty is per-pool")


def capability(venue: str, product: str) -> VenueCapability:
    """What we know about this venue/product pair.

    An unknown pair returns ASSUMED, never a permissive default. A venue
    nobody has characterised is not a venue anybody has verified.
    """
    key = (str(venue or "").lower(), str(product or ""))
    found = _REGISTRY.get(key)
    if found is not None:
        return found
    return VenueCapability(
        venue=key[0], product=key[1], status=ASSUMED,
        reason="no capability record — discover before claiming execution")


def executable_products(venue: str) -> list:
    """Products this venue can actually be traded through by a program."""
    return sorted(p for (v, p), c in _REGISTRY.items()
                  if v == str(venue or "").lower() and c.executable)


def research_only_products(venue: str) -> list:
    """Observable but not executable — the honest middle ground."""
    return sorted(p for (v, p), c in _REGISTRY.items()
                  if v == str(venue or "").lower() and c.research_only)


def record_discovery(venue: str, product: str, *, works: bool,
                     api_surface: str | None = None,
                     data_entitlement: str | None = None,
                     checked_at: str | None = None,
                     reason: str | None = None) -> VenueCapability:
    """Promote a capability from belief to evidence, or demote it.

    This is how UI_ONLY becomes DISCOVERED — by a live probe succeeding,
    not by anybody deciding it probably works.
    """
    key = (str(venue).lower(), product)
    cap = _REGISTRY.get(key) or VenueCapability(venue=key[0], product=product)
    cap.status = DISCOVERED if works else UNSUPPORTED
    cap.checked_at = checked_at
    if api_surface:
        cap.api_surface = api_surface
    if data_entitlement:
        cap.data_entitlement = data_entitlement
    cap.reason = reason or ("probed live and confirmed" if works
                            else "probed live and refused")
    cap.source = "live probe"
    _REGISTRY[key] = cap
    return cap


def assert_executable(venue: str, product: str) -> VenueCapability:
    return capability(venue, product).require_executable()


def depth_available(venue: str, product: str) -> dict:
    """Whether a fill model may assume book depth on this venue.

    ENTITLEMENT IS NOT CAPABILITY. Being able to trade Kraken futures does
    not mean being entitled to Level II. A fill model that assumes depth it
    cannot receive is guessing with extra steps — and it guesses in the
    flattering direction, because assumed depth is always sufficient.
    """
    cap = capability(venue, product)
    ent = cap.data_entitlement
    return {
        "venue": cap.venue, "product": cap.product,
        "entitlement": ent,
        "book_depth_usable": ent == LEVEL_2,
        "top_of_book_usable": ent in (LEVEL_1, LEVEL_2),
        "fill_model": ("ORDERBOOK_SIMULATED" if ent == LEVEL_2
                       else "TOP_OF_BOOK" if ent == LEVEL_1
                       else "CONSERVATIVE_BAR_TOUCH"),
        "reason": (cap.reason if ent == ENTITLEMENT_UNKNOWN else None),
    }


def snapshot() -> dict:
    """Everything known, for the UI and the integrity panel."""
    by_venue: dict = {}
    for (v, p), c in sorted(_REGISTRY.items()):
        by_venue.setdefault(v, []).append(c.as_dict())
    return {
        "version": CAPABILITY_VERSION,
        "venues": by_venue,
        "note": ("UI availability is not API availability. Only DOCUMENTED "
                 "and DISCOVERED capabilities may back simulated execution "
                 "that claims venue realism."),
    }
