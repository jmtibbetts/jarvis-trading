"""WHAT PRODUCT IS THIS DECISION ABOUT — asked at T0, before anything else.

THE DEFECT THIS EXISTS TO FIX.

`decision_observation.build()` read product, venue, asset_class and instrument
from the `ExecutionReadiness` artifact. That is the correct source for any
decision that reaches readiness — and most prospective decisions never do. The
evidence-only funnel terminates earlier:

    candidate -> T0 edge -> AI review -> AI_REJECTED_ENTRY -> observation

No readiness exists at that point, so `ready = None` and all four identity
fields were written NULL. Measured on the live campaign: **95 of 95
observations had NULL product and venue**, and every outcome horizon that came
due resolved INSUFFICIENT_DATA — 125 of them, against zero COMPLETE — while
9,622 BTC/USD Bitnomial quote samples sat in the same database covering the
very intervals those horizons spanned. The evidence was never missing. The
decisions simply could not say which product they were about, so nothing could
be joined to them.

TWO QUESTIONS, DELIBERATELY KEPT APART.

    RoutingIdentity     what product/venue/instrument is this decision about?
    ExecutionReadiness  can that product be executed RIGHT NOW?

The obvious shortcut — call `execution_readiness()` before the AI review — is
wrong, because it collapses those into one. Readiness opens books, grades
staleness and can refuse; identity is a static classification that must hold
even for a decision refused three gates earlier. A refusal has a product too,
and that is exactly the row research needs most.

SO THIS MODULE READS NO MARKET DATA. No quote, no book, no staleness, no
provider, no fill. It composes the classification authorities that already
exist in `lib.execution_policy` and returns what they say.

FROZEN ONCE, CARRIED FORWARD. Resolved at T0 and passed down the pipeline, the
same discipline `MeasuredEdge` already follows: compute once, carry forward,
persist once. Re-resolving later would read whatever configuration happens to
hold then, which is how a decision quietly changes product halfway through its
own lifetime.

PRODUCT IS NEVER INFERRED FROM CAPABILITY. If the intended expression is a
perpetual and the venue does not list it, the identity stays CRYPTO_PERP and
the CAPABILITY gate refuses it later. Downgrading it to spot because the perp
is unavailable would be choosing the product after seeing the answer, and it
would file a refusal under an instrument nobody intended to trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ROUTING_RESOLVER_VERSION = "routing_identity_v1"

# How the product was established. Recorded because "the signal said so" and
# "the desk default applied" are different claims about the same column, and
# only one of them survives a configuration change.
SIGNAL_EXPLICIT = "SIGNAL_EXPLICIT"
DESK_CONFIG = "DESK_CONFIG"
INSTRUMENT_IMPLIED = "INSTRUMENT_IMPLIED"
UNRESOLVED = "UNRESOLVED"

RESOLVED = "RESOLVED"
PARTIAL = "PARTIAL"          # asset class known, product or venue is not


class RoutingIdentityConflict(RuntimeError):
    """A later stage disagreed with the frozen T0 identity.

    Not an update — a defect. A decision that was made about a perpetual
    cannot later be settled as spot, and silently preferring one of the two
    would leave a row whose product no longer describes the decision that
    produced it.
    """


@dataclass(frozen=True)
class RoutingIdentity:
    """The instrument identity of ONE decision, frozen at T0."""

    symbol: str
    asset_class: str | None = None
    product: str | None = None
    venue: str | None = None
    instrument_id: str | None = None

    identity_status: str = UNRESOLVED
    product_identity_source: str = UNRESOLVED
    resolver_version: str = ROUTING_RESOLVER_VERSION
    provenance: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.identity_status == RESOLVED

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "asset_class": self.asset_class,
                "product": self.product, "venue": self.venue,
                "instrument_id": self.instrument_id,
                "identity_status": self.identity_status,
                "product_identity_source": self.product_identity_source,
                "resolver_version": self.resolver_version}

    def assert_agrees_with(self, *, product=None, venue=None,
                           asset_class=None, where: str = "readiness") -> None:
        """Later stages may CONFIRM this identity. They may not change it."""
        for name, mine, theirs in (("product", self.product, product),
                                   ("venue", self.venue, venue),
                                   ("asset_class", self.asset_class, asset_class)):
            if mine and theirs and str(mine) != str(theirs):
                raise RoutingIdentityConflict(
                    f"{where} says {name}={theirs!r} but the decision was "
                    f"frozen at {name}={mine!r} for {self.symbol}. A frozen "
                    f"T0 identity is not re-derived downstream; one of the "
                    f"two authorities is wrong and silently preferring "
                    f"either would misfile the evidence.")


def resolve_execution_identity(symbol: str, asset_class: str | None = None, *,
                               signal: dict | None = None) -> RoutingIdentity:
    """The T0 identity of a decision about `symbol`. Reads no market data.

    Never raises for an unknown instrument: an unresolvable identity is a
    fact worth recording, and the capability gate is the place that refuses.
    """
    from lib import execution_policy as EP

    sym = (symbol or "").upper().strip()
    if not sym:
        return RoutingIdentity(symbol=symbol or "", identity_status=UNRESOLVED,
                               provenance={"reason": "no symbol"})

    try:
        klass = EP.classify_asset_class(sym, asset_class)
        product = EP.resolve_product(sym, klass, signal=signal)
        venue, klass = EP.resolve_execution_venue(sym, klass, product)
        instrument = EP._instrument_id(sym, venue, product)
    except Exception as e:
        logger.debug("[RoutingIdentity] %s unresolved: %s", sym, e)
        return RoutingIdentity(symbol=sym, asset_class=asset_class,
                               identity_status=UNRESOLVED,
                               provenance={"error": f"{type(e).__name__}: {e}"})

    # WHERE THE PRODUCT CAME FROM. `resolve_product` consults the signal's
    # own expression first, then the desk's standing choice, then what the
    # instrument implies — so the source is read back the same way rather
    # than guessed from the answer.
    source = UNRESOLVED
    if product:
        explicit = bool(signal and (signal.get("product")
                                    or (signal.get("expression") or {}).get("product")
                                    if isinstance(signal.get("expression"), dict)
                                    else signal.get("product")))
        if explicit:
            source = SIGNAL_EXPLICIT
        elif klass == "crypto":
            source = DESK_CONFIG
        else:
            source = INSTRUMENT_IMPLIED

    status = RESOLVED if (product and venue) else (
        PARTIAL if klass and klass != "unknown" else UNRESOLVED)

    return RoutingIdentity(
        symbol=sym, asset_class=klass, product=product, venue=venue,
        instrument_id=instrument, identity_status=status,
        product_identity_source=source,
        provenance={"asset_class_hint": asset_class})
