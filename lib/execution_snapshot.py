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
# Derivatives sessions are scheduled and can halt, and a book can be
# published while the session is shut. "No fill because the market is
# closed" and "no fill because we cannot see the market" are different
# facts and only one of them is a data problem.
MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
MARKET_HALTED   = "MARKET_HALTED"
BOOK_DESYNCED   = "BOOK_DESYNCED"   # sequence integrity lost; not priceable

# Only this one may price a normal fill. The others are all refusals with
# different explanations, and the difference is what tells an operator
# whether to look at the network, the clock, or the venue.
FILLABLE = frozenset({AVAILABLE})

# How old a quote may be before it stops describing an executable market.
# Deliberately short: a 30-second-old crypto quote is a historical fact, not
# an offer, and treating it as one is how a simulator invents free money.
DEFAULT_MAX_AGE_S = 10.0

# ── Perpetual books age differently from a spot ticker ───────────────────
# A TICKER that stops arriving means the data stopped. A BOOK that stops
# changing means nobody moved a price — the levels are still live — so
# copying spot's 10s would mark a perfectly good quiet book stale.
#
# MEASURED against the live Bitnomial feed on 2026-08-17, 90s across
# PBTCUCZ50 / PETHUIZ50 / PXRPUHZ50 / PDOGUKZ50, n=4,363 updates:
#
#     median 0.00s   p95 0.39s   p99 1.43s   max 11.12s
#
# The 11.12s maximum was a genuine quiet period on XRP, not an outage. 15s
# sits above the observed quiet-period maximum with margin, so this flags a
# DEAD FEED rather than a calm market. It is a measurement with a date, not
# a round number — re-measure if the product set changes.
DEFAULT_PERP_MAX_AGE_S = 15.0
PERP_AGE_MEASURED_ON = "2026-08-17"


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

    A reader that has already reached a TERMINAL verdict keeps it. "The
    session is closed" and "the book desynced" are statements about the
    market and the feed, not about the prices — regrading them as
    UNAVAILABLE would collapse three different remedies into one message.
    """
    if snap.status in (MARKET_NOT_OPEN, MARKET_HALTED, BOOK_DESYNCED):
        return snap
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
def _from_bitnomial_perp(symbol: str,
                         snap: ExecutionMarketSnapshot) -> ExecutionMarketSnapshot:
    """US PERPETUALS, priced from the exchange they actually list on.

    The execution VENUE is Kraken Derivatives US; the market-data SOURCE is
    Bitnomial's public book. Those are different facts and the snapshot says
    both, so nothing downstream can read "kraken" and conclude the spot
    WebSocket priced this.

    Raw book integers are converted here and ONLY here, through the
    product's audited price scale. A product whose scale was not verified
    never reaches this function — `bitnomial_products.resolve` refuses it —
    so there is no path on which the wrong multiplier is silently applied.
    """
    from lib import bitnomial_market_data as MD
    from lib import bitnomial_products as BP

    prod = BP.resolve(symbol)
    if not prod.ok:
        snap.status = UNAVAILABLE
        snap.reason = f"{prod.reason}: {prod.detail}"
        snap.provenance["refusal"] = prod.reason
        return snap

    # P0.3: THE FROZEN CONTRACT IS THE ONLY CONTRACT — checked BEFORE the
    # book may price anything. If the caller froze PBTC_A and the current
    # underlying resolution selects PBTC_B, that is not a roll to perform,
    # it is a settlement identity nobody authorized. Refuse.
    requested = snap.provenance.get("requested_instrument_id")
    if requested and prod.symbol != requested:
        snap.status = UNAVAILABLE
        snap.reason = (f"frozen contract {requested!r} but the venue "
                       f"currently resolves {symbol} to {prod.symbol!r}; "
                       f"refusing rather than pricing another contract's "
                       f"book")
        snap.provenance["refusal"] = "EXECUTION_INSTRUMENT_MISMATCH"
        return snap

    # THE CONTRACT IS THE INSTRUMENT IDENTITY, and it belongs on the typed
    # field rather than only in provenance.  was declared on
    # the snapshot and assigned by NO reader, so every consumer read None:
    # 153,946 stored quote samples carry a NULL instrument while the
    # decisions beside them name PBTCUCZ50. Evidence cannot be matched to a
    # contract it never recorded.
    snap.instrument_id = prod.symbol
    snap.provenance.update({
        "bitnomial_symbol": prod.symbol, "bitnomial_product_id": prod.product_id,
        "product_code": prod.product_code, "contract_size": prod.contract_size,
        "contract_size_unit": prod.contract_size_unit,
        "price_increment": prod.price_increment,
        "market_data_source": prod.market_data_source,
        "execution_venue": prod.venue,
    })

    top = MD.latest_top(prod.symbol)
    if top is None:
        snap.status = UNAVAILABLE
        snap.reason = (f"the Bitnomial feed has not seen {prod.symbol}; the "
                       f"read-only stream may not be running")
        return snap

    snap.source = top["source"]
    snap.provenance.update({"ack_id": top["ack_id"],
                            "book_state": top["state"],
                            "market_state": top["market_state"],
                            "bid_levels": top["bid_levels"],
                            "ask_levels": top["ask_levels"],
                            "depth_bids": top["depth_bids"],
                            "depth_asks": top["depth_asks"],
                            "snapshot_count": top["snapshot_count"]})

    # A BOOK WHOSE SEQUENCE IS UNPROVEN IS NOT A SLIGHTLY WORSE BOOK.
    if top["state"] == MD.BOOK_DESYNCED:
        snap.status = BOOK_DESYNCED
        snap.reason = (f"book sequence integrity lost "
                       f"({top['desync_reason']}); awaiting a fresh snapshot")
        return snap
    if top["state"] == MD.BOOK_CLOSED:
        snap.status = MARKET_NOT_OPEN
        snap.reason = f"{prod.symbol} session is {top['market_state']!r}"
        return snap
    if top["state"] == MD.BOOK_HALTED:
        snap.status = MARKET_HALTED
        snap.reason = f"{prod.symbol} is halted"
        return snap
    if top["state"] == MD.BOOK_EMPTY:
        snap.status = UNAVAILABLE
        snap.reason = f"subscribed to {prod.symbol} but no snapshot has arrived"
        return snap

    if top["bid_raw"] is not None:
        snap.bid = prod.price_usd(top["bid_raw"])
        snap.bid_size = top["bid_size"]
    if top["ask_raw"] is not None:
        snap.ask = prod.price_usd(top["ask_raw"])
        snap.ask_size = top["ask_size"]
    snap.venue_event_at = top["venue_event_at"]
    if top["age_s"] is not None:
        snap.age_ms = max(0.0, float(top["age_s"]) * 1000.0)

    # Depth is RECORDED but not yet consumed by the fill model. Saying so
    # here keeps the follow-up honest: claiming depth-aware impact while
    # filling at top of book would misdescribe the simulator.
    snap.depth = None
    snap.provenance["depth_recorded_not_consumed"] = True
    return snap


_READERS = {
    "kraken": lambda sym, snap: _from_kraken(sym, snap),
    "alpaca": lambda sym, snap: _from_alpaca_equity(sym, snap),
    "binance": lambda sym, snap: _from_orderbook_stream("binance", sym, snap),
    "binanceus": lambda sym, snap: _from_orderbook_stream("binance", sym, snap),
    "coinbase": lambda sym, snap: _from_orderbook_stream("coinbase", sym, snap),
    # US PERPETUALS. A separate identity on purpose — overloading the
    # "kraken" reader is exactly how a spot book came to price a perpetual.
    "kraken_derivatives_us": lambda sym, snap: _from_bitnomial_perp(sym, snap),
}

# WHICH PRODUCTS EACH READER ACTUALLY SPEAKS FOR. A10.
#
# A venue is not a book. `kraken` here means `wss://ws.kraken.com/v2` — the
# SPOT WebSocket — and its ticker channel carries the spot book and nothing
# else. Kraken's perpetuals trade behind a different endpoint at different
# prices.
#
# Before this map existed the venue NAME alone authorised the fill, so a
# CRYPTO_PERP order was priced against the spot book. That is not an
# approximation: spot and perp diverge by basis and funding, they have
# separate liquidity, and the perp is the instrument whose price actually
# determines the P&L. Labelling a spot quote as perp execution truth is the
# same class of error as labelling a mark as a fill — the error this whole
# subsystem exists to remove.
#
# US perpetuals are a further step removed: they list on BITNOMIAL, a
# separate CFTC-regulated exchange, under their own product codes (PBTCUC,
# PETHUI). Kraken's own support documentation for US perpetual futures
# describes no public read-only market-data endpoint for them, so this desk
# cannot price one honestly and says so rather than substituting.
#
# THE DOCUMENTED PATH FOR INTERNATIONAL PERPS, recorded so nobody has to
# rediscover it and nobody is tempted to invent one:
#
#     GET https://futures.kraken.com/derivatives/api/v3/tickers
#     public, no authentication, returns bid/ask/bidSize/askSize/markPrice
#     per contract  (Kraken API Center — Futures API)
#
# It is deliberately NOT wired up here. Doing so is a REAL_PROVIDER_READ_ONLY
# expansion with its own symbol mapping and staleness grading, and it would
# still not answer for the US/Bitnomial contracts. Until that work is done a
# perpetual has no executable quote, and the entry is refused.
_READER_PRODUCTS = {
    "kraken": frozenset({"CRYPTO_SPOT"}),
    "alpaca": frozenset({"EQUITY_SPOT", "ETF_SPOT", "CRYPTO_SPOT"}),
    "binance": frozenset({"CRYPTO_SPOT"}),
    "binanceus": frozenset({"CRYPTO_SPOT"}),
    "coinbase": frozenset({"CRYPTO_SPOT"}),
    # A10.1. The US perpetual venue speaks for PERPETUALS ONLY. It must not
    # acquire CRYPTO_SPOT: a perpetual book is no more a spot price than a
    # spot book was a perpetual price, and the whole point of this map is
    # that the substitution cannot happen in either direction.
    "kraken_derivatives_us": frozenset({"CRYPTO_PERP"}),
}


def prices_product(venue: str, product: str | None) -> bool:
    """Whether this venue's WIRED FEED speaks for this product.

    An unstated product cannot be checked and is allowed through, for the
    callers that predate product identity; `execution_policy` establishes
    one before it reaches here.
    """
    if not product:
        return True
    allowed = _READER_PRODUCTS.get(str(venue or "").strip().lower())
    return bool(allowed and str(product) in allowed)


def products_for(venue: str) -> frozenset:
    """What this venue's feed can honestly price."""
    return _READER_PRODUCTS.get(str(venue or "").strip().lower(), frozenset())


def execution_market_snapshot(symbol: str, venue: str, *,
                              product: str | None = None,
                              instrument_id: str | None = None,
                              max_age_s: float = DEFAULT_MAX_AGE_S
                              ) -> ExecutionMarketSnapshot:
    """The executable state of `venue` for `symbol`, or a stated refusal.

    Never falls back to another venue. A caller that cannot price a fill
    from this must refuse the fill — that is a VENUE failure, not a bad
    thesis, and the two must stay distinguishable in the learning set.

    `instrument_id` is the FROZEN contract the caller has already committed
    to (P0.3). It is recorded as `requested_instrument_id` — separately from
    the reader-confirmed `snap.instrument_id`, so nobody can mistake "the
    caller stated it" for "the reader confirmed it" — and a reader that
    resolves a DIFFERENT contract refuses rather than pricing the wrong
    book. The architecture must not depend on the coincidence that today's
    resolution happens to equal yesterday's frozen one.
    """
    v = (venue or "").strip().lower()
    snap = ExecutionMarketSnapshot(venue=v, symbol=symbol, product=product,
                                   received_at=_now_iso())
    if instrument_id:
        snap.provenance["requested_instrument_id"] = str(instrument_id)
    reader = _READERS.get(v)
    if reader is None:
        snap.status = UNAVAILABLE
        snap.reason = (f"no execution-data authority for venue {venue!r}; "
                       f"another venue's book is evidence, not this venue's "
                       f"executable market")
        snap.provenance["known_venues"] = sorted(_READERS)
        return snap

    # THE VENUE IS NOT THE BOOK. A reader that speaks for the spot book must
    # not be allowed to price a perpetual just because both are "kraken".
    if not prices_product(v, product):
        snap.status = UNAVAILABLE
        snap.reason = (
            f"{v!r} has an execution feed for {sorted(products_for(v))} but "
            f"not for {product!r}; a spot quote is not a perpetual's price — "
            f"they diverge by basis and funding and have separate liquidity, "
            f"and the perpetual is the instrument that determines the P&L")
        snap.provenance["feed_products"] = sorted(products_for(v))
        snap.provenance["requested_product"] = product
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

    # THE READER MUST CONFIRM THE FROZEN CONTRACT, whichever reader ran. The
    # Bitnomial path refuses before touching the book; this central check is
    # the backstop for every other reader that stamps an instrument.
    requested = snap.provenance.get("requested_instrument_id")
    if requested and snap.instrument_id and snap.instrument_id != requested:
        snap.status = UNAVAILABLE
        snap.reason = (f"the frozen instrument is {requested!r} but this "
                       f"venue resolves {snap.instrument_id!r}; pricing the "
                       f"other contract would settle an identity nobody "
                       f"authorized")
        snap.provenance["refusal"] = "EXECUTION_INSTRUMENT_MISMATCH"
        return snap

    snap.provenance.setdefault("venue_authority", v)
    return _grade(snap, max_age_s)
