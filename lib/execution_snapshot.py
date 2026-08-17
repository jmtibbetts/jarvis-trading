"""What was executable ON THIS VENUE, right now — and nothing else.

WHY THIS MODULE EXISTS.

`lib/execution_recorder.capture_microstructure()` took a `venue` argument,
defaulted it to `"alpaca"`, and then read market state from whatever global
producer happened to be importable:

    from lib.orderbook_stream import get_book_snapshot   # does not exist
    from lib.kraken_stream    import get_tape_stats      # does not exist

Both calls raised ImportError into a swallowed `except`, so microstructure
capture had been silently recording nothing at all. That is the small half of
the problem.

The large half is that fixing the NAMES would have made it worse. The real
accessors are `orderbook_stream.get_latest_snapshot(exchange, symbol)` —
keyed by EXCHANGE, serving Binance and Coinbase — and
`kraken_stream.trade_flow(symbol)`, which is Kraken-only. Wire those into a
row labelled `venue="alpaca"` and you get an execution observation that is
correctly fetched and wrongly attributed: Kraken's tape and Coinbase's book,
filed under Alpaca, feeding the learning set as though one venue had shown
all of it.

CROSS-VENUE EVIDENCE DOES NOT BECOME VENUE EXECUTION TRUTH.

So this module answers exactly one question — "what could THIS venue have
filled?" — and refuses to answer it with another venue's data. A Coinbase
book remains useful evidence for a signal; it is not Kraken depth, and it can
never become a Kraken fill.

MISSING IS NOT ZERO. A venue with no depth feed reports `depth=None` and
`depth_status=UNAVAILABLE`, never `depth=0`, which would read as a market
with no liquidity — a very different and much more tradeable-looking claim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Status: how usable is this snapshot for pricing a fill? ───────────────
AVAILABLE   = "AVAILABLE"      # two-sided, fresh, sane
STALE       = "STALE"          # real, but older than the caller allows
CROSSED     = "CROSSED"        # bid >= ask; the book is not usable
ONE_SIDED   = "ONE_SIDED"      # only one side quoted
UNAVAILABLE = "UNAVAILABLE"    # nothing to read
FALLBACK    = "FALLBACK"       # from a labelled substitute, never silent

# Only this one may price a normal fill. The others are all refusals with
# different explanations, and the difference is what tells an operator
# whether to look at the network, the clock, or the venue.
FILLABLE = frozenset({AVAILABLE})

# How old a quote may be before it stops describing an executable market.
# Deliberately short: a 30-second-old crypto quote is a historical fact, not
# an offer, and treating it as one is how a simulator invents free money.
DEFAULT_MAX_AGE_S = 10.0


@dataclass
class ExecutionMarketSnapshot:
    """The executable state of ONE venue for ONE instrument."""

    venue: str
    symbol: str
    product: str | None = None
    instrument_id: str | None = None

    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    # Depth is a separate claim from the top of book, and separately
    # attributed: a venue can have a real quote and no depth feed at all.
    depth: dict | None = None
    depth_status: str = UNAVAILABLE
    depth_source: str | None = None
    imbalance: float | None = None

    recent_trade_flow: dict | None = None
    trade_flow_source: str | None = None

    venue_event_at: str | None = None     # the venue's own timestamp, if given
    received_at: str | None = None        # when we saw it
    age_ms: float | None = None

    source: str | None = None             # the producer that answered
    status: str = UNAVAILABLE
    reason: str | None = None
    provenance: dict = field(default_factory=dict)

    # ── Derived, and only where honest ───────────────────────────────────
    @property
    def mid(self) -> float | None:
        """A REFERENCE. Never a fill — see lib/virtual_orders."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        m, s = self.mid, self.spread
        if not m or s is None:
            return None
        return s / m

    @property
    def fillable(self) -> bool:
        """Whether a normal fill may be priced from this."""
        return self.status in FILLABLE

    def as_dict(self) -> dict:
        return {
            "venue": self.venue, "symbol": self.symbol, "product": self.product,
            "instrument_id": self.instrument_id,
            "bid": self.bid, "ask": self.ask,
            "bid_size": self.bid_size, "ask_size": self.ask_size,
            "mid": self.mid, "spread": self.spread, "spread_pct": self.spread_pct,
            "depth": self.depth, "depth_status": self.depth_status,
            "depth_source": self.depth_source, "imbalance": self.imbalance,
            "recent_trade_flow": self.recent_trade_flow,
            "trade_flow_source": self.trade_flow_source,
            "venue_event_at": self.venue_event_at, "received_at": self.received_at,
            "age_ms": self.age_ms, "source": self.source,
            "status": self.status, "reason": self.reason,
            "provenance": dict(self.provenance),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grade(snap: ExecutionMarketSnapshot, max_age_s: float) -> ExecutionMarketSnapshot:
    """Decide the status from what was actually read.

    Order matters: a crossed book is unusable regardless of age, and a
    one-sided book cannot price either direction, so both outrank staleness.
    """
    if snap.bid is None and snap.ask is None:
        snap.status = UNAVAILABLE
        snap.reason = snap.reason or "no quote from this venue"
        return snap
    if snap.bid is None or snap.ask is None:
        snap.status = ONE_SIDED
        snap.reason = "only one side of the book is quoted"
        return snap
    if snap.bid <= 0 or snap.ask <= 0:
        snap.status = UNAVAILABLE
        snap.reason = "non-positive price"
        return snap
    if snap.bid >= snap.ask:
        snap.status = CROSSED
        snap.reason = f"crossed book: bid {snap.bid} >= ask {snap.ask}"
        return snap
    if snap.age_ms is not None and snap.age_ms > max_age_s * 1000.0:
        snap.status = STALE
        snap.reason = (f"quote is {snap.age_ms / 1000.0:.1f}s old, older than the "
                       f"{max_age_s:.0f}s this caller allows")
        return snap
    snap.status = AVAILABLE
    return snap


# ── Per-venue readers. Each reads ONLY its own venue. ─────────────────────

def _from_kraken(symbol: str, snap: ExecutionMarketSnapshot) -> ExecutionMarketSnapshot:
    """Kraken: real bid/ask and a real tape. NO depth feed is implemented.

    The absence is reported rather than filled in from Coinbase or Binance,
    which is the entire point of this module.
    """
    from lib.kraken_stream import latest_quote, trade_flow

    q = latest_quote(symbol)
    if q:
        snap.bid = _f(q.get("bid"))
        snap.ask = _f(q.get("ask"))
        snap.source = "kraken_stream.latest_quote"
        at = q.get("at")
        if isinstance(at, datetime):
            snap.venue_event_at = at.isoformat()
            snap.age_ms = max(0.0, (datetime.now(timezone.utc) - at).total_seconds() * 1000.0)
    else:
        snap.reason = "kraken stream has not seen a quote for this symbol"

    flow = trade_flow(symbol)
    if flow:
        snap.recent_trade_flow = flow
        snap.trade_flow_source = "kraken_stream.trade_flow"

    # DEPTH: Kraken has no L2 adapter here yet. None, not zero, and the
    # status says which — "no depth feed" and "no liquidity" are opposite
    # claims and only one of them is true.
    snap.depth = None
    snap.depth_status = UNAVAILABLE
    snap.depth_source = None
    snap.provenance["depth_note"] = (
        "no Kraken L2 adapter implemented; cross-venue depth is deliberately "
        "NOT substituted")
    return snap


def _from_orderbook_stream(exchange: str, symbol: str,
                           snap: ExecutionMarketSnapshot) -> ExecutionMarketSnapshot:
    """Binance / Coinbase, each read only for itself."""
    from lib.orderbook_stream import get_latest_snapshot

    book = get_latest_snapshot(exchange, symbol)
    if not book:
        snap.reason = f"{exchange} stream has no book for this symbol"
        return snap

    snap.bid = _f(book.get("best_bid"))
    snap.ask = _f(book.get("best_ask"))
    snap.source = f"orderbook_stream[{exchange}]"
    health = book.get("health") or {}
    age_s = health.get("age_seconds")
    if age_s is not None:
        snap.age_ms = float(age_s) * 1000.0
    if not health.get("valid", True):
        snap.reason = f"book reports itself invalid: {health.get('reason')}"

    bid_depth, ask_depth = _f(book.get("bid_depth")), _f(book.get("ask_depth"))
    if bid_depth is not None or ask_depth is not None:
        snap.depth = {"bid_depth": bid_depth, "ask_depth": ask_depth,
                      "bids": book.get("bids"), "asks": book.get("asks")}
        snap.depth_status = AVAILABLE
        snap.depth_source = f"orderbook_stream[{exchange}]"
    snap.imbalance = _f(book.get("imbalance"))
    return snap


def _from_alpaca_equity(symbol: str,
                        snap: ExecutionMarketSnapshot) -> ExecutionMarketSnapshot:
    """US equities, from Alpaca's MARKET DATA client.

    READ-ONLY BY CONSTRUCTION, not by promise. `StockHistoricalDataClient`
    is a data client: it has no order surface at all. The order-capable
    `TradingClient` is a different class in a different module and is
    deliberately not imported here — "we promise not to call submit_order"
    is not a safety property.

    The API was confirmed against the INSTALLED SDK rather than from
    memory: get_stock_latest_quote(StockLatestQuoteRequest) returns a Quote
    carrying bid_price, ask_price, bid_size, ask_size and timestamp. That
    is a genuine two-sided executable quote, which is what closes the
    equity gap — the previous mark chain (Alpaca last, MarketAsset row,
    yfinance) could only ever answer "what is it worth".

    Depth: Alpaca's latest-quote endpoint carries top-of-book SIZES but no
    book. Sizes are recorded; depth stays None.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    from lib.alpaca_client import get_alpaca_creds

    key, secret, _paper = get_alpaca_creds()
    if not key or not secret:
        snap.reason = "no Alpaca credentials configured for market data"
        return snap

    client = StockHistoricalDataClient(key, secret)
    quotes = client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbol))
    q = (quotes or {}).get(symbol) or (quotes or {}).get(symbol.upper())
    if q is None:
        snap.reason = f"Alpaca returned no quote for {symbol}"
        return snap

    snap.bid = _f(getattr(q, "bid_price", None))
    snap.ask = _f(getattr(q, "ask_price", None))
    snap.bid_size = _f(getattr(q, "bid_size", None))
    snap.ask_size = _f(getattr(q, "ask_size", None))
    snap.source = "alpaca.get_stock_latest_quote"
    ts = getattr(q, "timestamp", None)
    if ts is not None:
        try:
            snap.venue_event_at = ts.isoformat()
            snap.age_ms = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() * 1000.0)
        except Exception:
            pass

    # Top-of-book sizes are not a book. Depth stays unavailable.
    snap.depth = None
    snap.depth_status = UNAVAILABLE
    snap.provenance["depth_note"] = (
        "Alpaca latest-quote carries top-of-book sizes only; no L2 book")
    if snap.bid_size is not None and snap.ask_size is not None:
        tot = snap.bid_size + snap.ask_size
        if tot > 0:
            snap.imbalance = round((snap.bid_size - snap.ask_size) / tot, 6)
    return snap


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# venue -> the reader that speaks for it. A venue absent from this map has
# no execution-data authority, which is a fact worth reporting rather than
# an invitation to borrow another venue's.
_READERS = {
    "kraken": lambda sym, snap: _from_kraken(sym, snap),
    "alpaca": lambda sym, snap: _from_alpaca_equity(sym, snap),
    "binance": lambda sym, snap: _from_orderbook_stream("binance", sym, snap),
    "binanceus": lambda sym, snap: _from_orderbook_stream("binance", sym, snap),
    "coinbase": lambda sym, snap: _from_orderbook_stream("coinbase", sym, snap),
}


def execution_market_snapshot(symbol: str, venue: str, *,
                              product: str | None = None,
                              max_age_s: float = DEFAULT_MAX_AGE_S
                              ) -> ExecutionMarketSnapshot:
    """The executable state of `venue` for `symbol`, or a stated refusal.

    Never falls back to another venue. A caller that cannot price a fill
    from this must refuse the fill — that is a VENUE failure, not a bad
    thesis, and the two must stay distinguishable in the learning set.
    """
    v = (venue or "").strip().lower()
    snap = ExecutionMarketSnapshot(venue=v, symbol=symbol, product=product,
                                   received_at=_now_iso())
    reader = _READERS.get(v)
    if reader is None:
        snap.status = UNAVAILABLE
        snap.reason = (f"no execution-data authority for venue {venue!r}; "
                       f"another venue's book is evidence, not this venue's "
                       f"executable market")
        snap.provenance["known_venues"] = sorted(_READERS)
        return snap

    try:
        reader(symbol, snap)
    except Exception as e:
        # A producer that is not running is a normal condition, not an
        # error to swallow into an empty dict.
        snap.status = UNAVAILABLE
        snap.reason = f"{type(e).__name__}: {e}"
        logger.debug("[ExecSnap] %s/%s unavailable: %s", v, symbol, e)
        return snap

    snap.provenance.setdefault("venue_authority", v)
    return _grade(snap, max_age_s)
