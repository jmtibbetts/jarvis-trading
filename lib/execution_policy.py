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
resolved through Alpaca's last price, a MarketAsset row, then a yfinance
futures cache. Every one of those is a MARK, and none can answer what an
order would have filled at.

The equity gap has since been CLOSED from an already-configured provider:
alpaca-py's StockHistoricalDataClient exposes get_stock_latest_quote, which
returns bid_price/ask_price/sizes/timestamp. That API was confirmed by
introspecting the INSTALLED SDK rather than recalled, and it lives on a data
client with no order surface at all.

Futures and forex remain mark-only — no configured provider here offers them
a two-sided executable quote:

    PRODUCT   VENUE     MARK SOURCE            EXECUTABLE QUOTE      FILLABLE
    crypto    kraken    Alpaca/DB last         kraken_stream bid/ask  YES
    equity    alpaca    Alpaca/DB last         alpaca latest_quote    YES
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

UNKNOWN_PRODUCT           = "UNKNOWN_PRODUCT"

# ASSET CLASSES whose execution venue has a real two-sided quote feed wired
# up. Adding a member here is a claim that lib/execution_snapshot can produce
# an AVAILABLE snapshot for it — nothing else belongs in this set.
#
# NOTE THE NAME. This is an ASSET CLASS set, not a product set. It used to be
# called _FILLABLE_PRODUCTS and hold {"crypto", "equity"}, which is precisely
# the collapse this module now separates: "crypto" is not a product, and
# treating it as one is what let a perpetual and a spot pair share one
# identity all the way down to the fee schedule.
_FILLABLE_ASSET_CLASSES = frozenset({"crypto", "equity"})


def classify_asset_class(symbol: str, asset_class: str | None = None) -> str:
    """crypto | equity | futures | forex | unknown.

    THE ASSET CLASS, NOT THE PRODUCT. Answering "this is crypto" says
    nothing about whether the trade is spot or a perpetual, and those differ
    in fees, funding and liquidation. `resolve_product` answers that.

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


def resolve_product(symbol: str, asset_class: str | None = None, *,
                    signal: dict | None = None) -> str | None:
    """WHICH PRODUCT this thesis is being expressed as, or None.

    PRODUCT IS NOT LEVERAGE, and it is never derived from one. A perpetual
    held at 1x is still CRYPTO_PERP; spot bought at 1x is still CRYPTO_SPOT.
    Inferring it from `leverage > 1` is what once billed an entire perp book
    at spot rates whenever the conviction ladder bottomed out at 1x.

    The product comes from the CHOSEN EXPRESSION, in this order:

        1. what the signal explicitly states (a product_router Expression
           that already priced this way of holding the thesis)
        2. CRYPTO_PRODUCT — the desk's standing choice, and the SAME setting
           `paper_engine.venue_round_trip_fee` already prices against, so
           execution and the fee model cannot disagree about what was traded
        3. for everything else, the product implied by the instrument

    The vocabulary is imported from lib/product_router rather than respelled
    here; there is one taxonomy and this is not a second one.
    """
    from lib import product_router as PR

    ac = classify_asset_class(symbol, asset_class)

    if ac == "crypto":
        stated = (signal or {}).get("product") or (signal or {}).get("expression_product")
        if stated in (PR.CRYPTO_SPOT, PR.CRYPTO_PERP):
            return stated
        want = str(stated or os.getenv("CRYPTO_PRODUCT") or "perp").lower()
        return PR.CRYPTO_PERP if want.startswith("perp") else PR.CRYPTO_SPOT

    if ac == "equity":
        return PR.EQUITY_SPOT

    if ac == "forex":
        return PR.FX_SPOT

    if ac == "futures":
        # Index and commodity futures are different products with different
        # exchange fees. The universe knows which; when it does not, this
        # returns None rather than picking one — a guessed product would be
        # carried into the fee authority as though it had been established.
        try:
            from lib.futures_data import FUTURES_UNIVERSE
            cat = (FUTURES_UNIVERSE.get(symbol) or {}).get("category")
            if cat:
                return (PR.INDEX_FUTURE if "index" in str(cat).lower()
                        else PR.COMMODITY_FUTURE)
        except Exception:
            pass
        return None

    return None


def resolve_execution_venue(symbol: str,
                            asset_class: str | None = None) -> tuple[str | None, str]:
    """(venue, asset_class) that WOULD have executed this, or (None, class).

    PAPER_VENUE is deliberately consulted for crypto only. It is a crypto
    setting; letting it capture equities and futures is how one variable
    routes an S&P future through a Bitcoin exchange.

    THE SECOND ELEMENT IS AN ASSET CLASS. It was called `product` and it
    never was one — see `resolve_product` for the actual product.
    """
    ac = classify_asset_class(symbol, asset_class)
    if ac == "crypto":
        venue = (os.getenv("PAPER_VENUE") or os.getenv("DEFAULT_CRYPTO_VENUE")
                 or "kraken")
        return str(venue).lower(), ac
    if ac == "equity":
        # Where an equity WOULD execute. That it has no quote feed is a
        # separate question, answered by execution_readiness below.
        return "alpaca", ac
    # Futures and forex have no simulated execution venue assigned at all.
    return None, ac


class ExecutionReadiness:
    """Whether a fill may be simulated, and if not, exactly why.

    FOUR IDENTITIES, KEPT APART. They were collapsed into one field called
    `product` that held "crypto", which is none of them:

        asset_class  crypto | equity | futures | forex
        product      CRYPTO_SPOT | CRYPTO_PERP | EQUITY_SPOT | ...
        venue        which book the order reaches
        instrument   the contract or pair id at that venue

    Collapsing them is not a naming problem. A fee authority asked "which
    product?" and handed "crypto" cannot tell a perpetual from a spot pair,
    and answers with whichever schedule it happens to reach first.
    """

    __slots__ = ("ok", "venue", "product", "asset_class", "instrument",
                 "reason", "detail", "snapshot")

    def __init__(self, ok, venue, product, reason=None, detail=None,
                 snapshot=None, asset_class=None, instrument=None):
        self.ok = ok
        self.venue = venue
        self.product = product
        self.asset_class = asset_class
        self.instrument = instrument
        self.reason = reason
        self.detail = detail
        self.snapshot = snapshot

    def __repr__(self):
        return (f"ExecutionReadiness(ok={self.ok}, venue={self.venue!r}, "
                f"asset_class={self.asset_class!r}, product={self.product!r}, "
                f"reason={self.reason!r})")


def execution_readiness(symbol: str, asset_class: str | None = None, *,
                        max_age_s: float | None = None,
                        signal: dict | None = None) -> ExecutionReadiness:
    """Can a fill be simulated for this instrument RIGHT NOW?

    A refusal here is a VENUE/DATA verdict. Callers must not record it
    against the strategy: a thesis that could not be executed because a
    quote was eight seconds stale is not a losing thesis.
    """
    from lib import execution_snapshot as ES

    venue, ac = resolve_execution_venue(symbol, asset_class)
    product = resolve_product(symbol, asset_class, signal=signal)
    instrument = _instrument_id(symbol, venue, product)

    def _refuse(reason, detail):
        return ExecutionReadiness(False, venue, product, reason, detail,
                                  asset_class=ac, instrument=instrument)

    if venue is None:
        return _refuse(
            UNSUPPORTED_VIRTUAL_VENUE,
            f"no simulated execution venue is assigned for {ac!r}; a mark "
            f"price cannot answer what an order would have filled at")

    if ac not in _FILLABLE_ASSET_CLASSES:
        return _refuse(
            NO_EXECUTABLE_QUOTE,
            f"{ac!r} on {venue!r} has no two-sided executable quote feed "
            f"wired up; only a last/mark price is available and MARK AUTHORITY "
            f"IS NOT EXECUTION AUTHORITY")

    # An unresolvable product is refused rather than defaulted. Everything
    # downstream — the capability gate, the fee authority, settlement — takes
    # the product as established fact, so handing them a guess would launder
    # it into the ledger as a measurement.
    if product is None:
        return _refuse(
            UNKNOWN_PRODUCT,
            f"no product could be established for {symbol!r} ({ac}); the fee "
            f"schedule and venue capability both depend on knowing whether "
            f"this is spot or a derivative")

    kwargs = {} if max_age_s is None else {"max_age_s": max_age_s}
    snap = ES.execution_market_snapshot(symbol, venue, product=product, **kwargs)
    if snap.status == ES.AVAILABLE:
        return ExecutionReadiness(True, venue, product, snapshot=snap,
                                  asset_class=ac, instrument=instrument)

    reason = {
        ES.STALE: STALE_EXECUTION_DATA,
        ES.CROSSED: CROSSED_BOOK,
        ES.ONE_SIDED: ONE_SIDED_BOOK,
    }.get(snap.status, EXECUTION_DATA_UNAVAILABLE)
    return ExecutionReadiness(False, venue, product, reason, snap.reason, snap,
                              asset_class=ac, instrument=instrument)


def _instrument_id(symbol: str, venue: str | None, product: str | None):
    """The contract or pair id at this venue, when one is on file.

    Best effort and never fatal: an unknown instrument is a refusal the
    layers below already make on their own terms, and readiness is not the
    place to raise it.
    """
    if not (symbol and venue and product):
        return None
    try:
        from lib.instruments import resolve
        return resolve(symbol, venue=venue, product=product).instrument_id
    except Exception:
        return None


def is_venue_data_failure(reason: str | None) -> bool:
    """True when a refusal is about the venue, not the thesis.

    The distinction has to survive into the learning set. A signal blocked by
    a stale quote must remain eligible; a signal rejected by risk must not.
    """
    return reason in {
        UNSUPPORTED_VIRTUAL_VENUE, NO_EXECUTABLE_QUOTE,
        EXECUTION_DATA_UNAVAILABLE, STALE_EXECUTION_DATA,
        CROSSED_BOOK, ONE_SIDED_BOOK,
        # An unestablished product is a gap in THIS system's instrument
        # knowledge, not a verdict on the thesis — the signal stays eligible.
        UNKNOWN_PRODUCT,
    }
