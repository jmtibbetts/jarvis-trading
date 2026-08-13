"""The same asset, different ticker at every venue.

Tokenized equities are the worst case: one underlying, several issuers, and
each venue naming it differently. SpaceX alone appears as

    BTCC        SPCX/USD        what the operator sees on the screen
    OKX         XSPCX/USD       the xStock issue — live, and the price BTCC quotes
    (various)   SPCXB/USD       the bStocks issue — a DIFFERENT instrument

Typing the ticker you are looking at should not fail. But an alias is a
claim that two names are the same tradeable thing, and that claim can be
wrong — SPCXB is a separate issue with its own liquidity and its own price,
not an alias for XSPCX. So aliases are explicit and narrow, never inferred
from a fuzzy name match.

Resolution is also always REPORTED, never silent. Substituting one ticker
for another without saying so would mean the operator asks for one
instrument and quietly holds a different one.
"""
from __future__ import annotations

# venue_ticker -> canonical tracked symbol.
# Only pairs confirmed to quote the same instrument at the same price.
ALIASES: dict[str, str] = {
    # SpaceX, xStock issue. BTCC lists it as SPCX/USD; the tracked symbol is
    # the OKX xStock ticker, and the operator confirms the prices match.
    #
    # Only the /USD form is aliased. Bare "SPCX" is NOT — it is already
    # tracked here as a separate equity at $146.15, and aliasing it would
    # shadow a real symbol the operator deliberately added: they would ask
    # for the equity and silently get the token. An alias may resolve an
    # ambiguity; it must never overwrite something that already exists.
    #
    # NOT aliased to SPCXB/USD either, which is the bStocks issue — a
    # different instrument that had no live quote when this was written.
    "SPCX/USD": "XSPCX/USD",
}


def resolve(symbol: str | None) -> tuple[str, str | None]:
    """(canonical symbol, note explaining any substitution).

    The note exists so the substitution is visible. A silent rename means
    asking for one instrument and holding another.
    """
    raw = (symbol or "").strip().upper()
    target = ALIASES.get(raw)
    if not target:
        return raw, None
    return target, (f"{raw} is the venue ticker for {target} — "
                    f"added as {target}, which is what the price feed quotes")


def aliases_for(canonical: str | None) -> list[str]:
    """Every venue ticker that maps to this symbol, for display."""
    c = (canonical or "").strip().upper()
    return sorted(k for k, v in ALIASES.items() if v == c)
