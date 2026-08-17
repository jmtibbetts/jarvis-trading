"""Which venue would have executed this, and can it be simulated honestly?

WHY THIS IS SEPARATE FROM execution_snapshot.

`execution_snapshot` answers "what could venue V have filled?". It cannot
answer "which venue is V?" — that depends on the PRODUCT, and getting it
wrong is its own category of nonsense. `PAPER_VENUE=kraken` is a crypto
setting; routing AAPL, ES=F and EURUSD=X through Kraken because a single
environment variable said so would price an equity against a crypto book.

paper_engine already routes FEES by instrument (`is_crypto_symbol`,
`is_futures`) after a GOOGL trade was once charged the Kraken crypto taker
rate at a venue that does not list it. This is the same rule applied to
execution.

WHAT THE AUDIT FOUND, and why most of this file is refusals.

The paper book is not crypto-only — 487 crypto, 94 equity and 86 futures
positions are open right now. But `jobs/paper_trading._get_current_price()`
resolves through Alpaca's last price, a MarketAsset row, then a yfinance
futures cache. Every one of those is a MARK. None is a two-sided executable
quote, and the repository contains no Alpaca equity bid/ask path at all
(only an options-snapshot quote, which is a different product).

So today exactly one product can be filled honestly:

    PRODUCT   VENUE     MARK SOURCE            EXECUTABLE QUOTE      FILLABLE
    crypto    kraken    Alpaca/DB last         kraken_stream bid/ask  YES
    equity    alpaca    Alpaca/DB last         none                   NO
    futures   -         yfinance last          none                   NO
    forex     -         yfinance last          none                   NO

MARK AUTHORITY IS NOT EXECUTION AUTHORITY. A last price is still perfectly
good for valuation, charts, TA, signal generation and unrealized mark P&L.
It simply cannot answer "what would this order have filled at", and the
consequence of pretending otherwise is a simulator that makes money because
it is wrong.

Refusing an entry is the correct outcome of that gap, not a bug in this
module.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Why a product cannot be simulated. These are VENUE/DATA failures, and they
# must never be recorded against the thesis — a good signal that could not be
# executed is not a bad signal.
UNSUPPORTED_VIRTUAL_VENUE = "UNSUPPORTED_VIRTUAL_VENUE"
NO_EXECUTABLE_QUOTE       = "NO_EXECUTABLE_QUOTE"
EXECUTION_DATA_UNAVAILABLE = "EXECUTION_DATA_UNAVAILABLE"
STALE_EXECUTION_DATA      = "STALE_EXECUTION_DATA"
CROSSED_BOOK              = "CROSSED_BOOK"
ONE_SIDED_BOOK            = "ONE_SIDED_BOOK"

# Products whose execution venue has a real two-sided quote feed wired up.
# Adding a member here is a claim that lib/execution_snapshot can produce an
# AVAILABLE snapshot for it — nothing else belongs in this set.
_FILLABLE_PRODUCTS = frozenset({"crypto"})


def classify_product(symbol: str, asset_class: str | None = None) -> str:
    """crypto | equity | futures | forex | unknown.

    The stored asset_class vocabulary is a mess of thirty-odd spellings
    ("us_equity", "Equity", "equity", "Commodity/Defense"), so the SYMBOL is
    asked first through the instrument helpers that already exist, and the
    label is only a hint.
    """
    try:
        from lib.instruments import is_futures
        if is_futures(symbol):
            return "futures"
    except Exception:
        pass
    try:
        from lib.transaction_costs import is_crypto_symbol
        if is_crypto_symbol(symbol):
            return "crypto"
    except Exception:
        pass

    s = (symbol or "").upper()
    if s.endswith("=X") or (len(s) == 6 and s.isalpha()):
        return "forex"
    if s.endswith("=F"):
        return "futures"

    label = (asset_class or "").strip().lower()
    if "crypto" in label:
        return "crypto"
    if "future" in label:
        return "futures"
    if "forex" in label or label == "fx":
        return "forex"
    if "equity" in label or "etf" in label or "stock" in label:
        return "equity"
    return "unknown"


def resolve_execution_venue(symbol: str,
                            asset_class: str | None = None) -> tuple[str | None, str]:
    """(venue, product) that WOULD have executed this, or (None, product).

    PAPER_VENUE is deliberately consulted for crypto only. It is a crypto
    setting; letting it capture equities and futures is how one variable
    routes an S&P future through a Bitcoin exchange.
    """
    product = classify_product(symbol, asset_class)
    if product == "crypto":
        venue = (os.getenv("PAPER_VENUE") or os.getenv("DEFAULT_CRYPTO_VENUE")
                 or "kraken")
        return str(venue).lower(), product
    if product == "equity":
        # Where an equity WOULD execute. That it has no quote feed is a
        # separate question, answered by execution_readiness below.
        return "alpaca", product
    # Futures and forex have no simulated execution venue assigned at all.
    return None, product


class ExecutionReadiness:
    """Whether a fill may be simulated, and if not, exactly why."""

    __slots__ = ("ok", "venue", "product", "reason", "detail", "snapshot")

    def __init__(self, ok, venue, product, reason=None, detail=None, snapshot=None):
        self.ok = ok
        self.venue = venue
        self.product = product
        self.reason = reason
        self.detail = detail
        self.snapshot = snapshot

    def __repr__(self):
        return (f"ExecutionReadiness(ok={self.ok}, venue={self.venue!r}, "
                f"product={self.product!r}, reason={self.reason!r})")


def execution_readiness(symbol: str, asset_class: str | None = None, *,
                        max_age_s: float | None = None) -> ExecutionReadiness:
    """Can a fill be simulated for this instrument RIGHT NOW?

    A refusal here is a VENUE/DATA verdict. Callers must not record it
    against the strategy: a thesis that could not be executed because a
    quote was eight seconds stale is not a losing thesis.
    """
    from lib import execution_snapshot as ES

    venue, product = resolve_execution_venue(symbol, asset_class)

    if venue is None:
        return ExecutionReadiness(
            False, None, product, UNSUPPORTED_VIRTUAL_VENUE,
            f"no simulated execution venue is assigned for {product!r}; a mark "
            f"price cannot answer what an order would have filled at")

    if product not in _FILLABLE_PRODUCTS:
        return ExecutionReadiness(
            False, venue, product, NO_EXECUTABLE_QUOTE,
            f"{product!r} on {venue!r} has no two-sided executable quote feed "
            f"wired up; only a last/mark price is available and MARK AUTHORITY "
            f"IS NOT EXECUTION AUTHORITY")

    kwargs = {} if max_age_s is None else {"max_age_s": max_age_s}
    snap = ES.execution_market_snapshot(symbol, venue, product=product, **kwargs)
    if snap.status == ES.AVAILABLE:
        return ExecutionReadiness(True, venue, product, snapshot=snap)

    reason = {
        ES.STALE: STALE_EXECUTION_DATA,
        ES.CROSSED: CROSSED_BOOK,
        ES.ONE_SIDED: ONE_SIDED_BOOK,
    }.get(snap.status, EXECUTION_DATA_UNAVAILABLE)
    return ExecutionReadiness(False, venue, product, reason, snap.reason, snap)


def is_venue_data_failure(reason: str | None) -> bool:
    """True when a refusal is about the venue, not the thesis.

    The distinction has to survive into the learning set. A signal blocked by
    a stale quote must remain eligible; a signal rejected by risk must not.
    """
    return reason in {
        UNSUPPORTED_VIRTUAL_VENUE, NO_EXECUTABLE_QUOTE,
        EXECUTION_DATA_UNAVAILABLE, STALE_EXECUTION_DATA,
        CROSSED_BOOK, ONE_SIDED_BOOK,
    }
