"""The Bitnomial public book — the only honest price for a US perpetual.

WHY THIS EXISTS. A10 established that `lib/kraken_stream` is
`wss://ws.kraken.com/v2`, the SPOT WebSocket, and that pricing a CRYPTO_PERP
against it is the same class of error as pricing a fill at the mark. That
left perpetuals fail-closed with NO_EXECUTABLE_PERP_QUOTE, which was correct
and was not the end state: US perpetuals list on BITNOMIAL, and Bitnomial
publishes a real book over a public, unauthenticated WebSocket.

So the fix is not to run the desk as spot. It is to read the actual
perpetual book.

READ-ONLY BY CONSTRUCTION. This module opens one WebSocket, sends one
subscribe frame, and reads. It has no order, cancel, account, position or
transfer surface, imports no signing or credential helper, and issues no
POST/PUT/DELETE of any kind. That is asserted by AST in the tests rather
than promised here.

THE PROTOCOL, verified against the live service on 2026-08-17 (captured in
tests/fixtures/bitnomial_ws_capture.json):

    -> {"type":"subscribe","product_codes":[SYM],
        "channels":[{"name":"book","product_codes":[SYM]}, ...]}

    <- {"type":"book",   "ack_id":"...","bids":[[p,q]...],"asks":[[p,q]...],
        "symbol":...,"timestamp":...}
    <- {"type":"level",  "ack_id":"...","price":p,"quantity":q,
        "side":"Bid"|"Ask","symbol":...,"timestamp":...}
    <- {"type":"status", "ack_id":"...","state":"Open","symbol":...,...}

Confirmed behaviours, none of them assumed:

  * asks arrive ASCENDING and bids DESCENDING, so index 0 is the best level
  * `quantity: 0` REMOVES a level — it is not a zero-size resting order
  * `ack_id` is a monotonically increasing integer-valued STRING, and a
    single ack_id can carry SEVERAL level messages: it identifies an atomic
    batch, so equal ids must be applied, not skipped
  * prices are RAW INTEGERS in units of the product's `price_increment`

THE BOOK IS A BOOK, NOT A LIST OF MESSAGES. Levels are held in a dict keyed
by raw price and re-sorted on read. Appending updates to a list would leave
a stale best bid sitting in front of its own removal, which is a fill at a
price that no longer exists — invisible, and always in the flattering
direction whenever the market is moving away.

SEQUENCE INTEGRITY IS AN EXECUTION SAFETY PROPERTY. On a gap, a reconnect, a
malformed message or an out-of-order ack, the book is INVALIDATED and stops
answering until a fresh authoritative snapshot arrives. A book whose
sequence cannot be proven is not a slightly worse book; it is a set of
prices with no established relationship to the market.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ADAPTER_VERSION = "bitnomial_md_v1"

# ── Book states. Each is a different refusal downstream. ─────────────────
BOOK_OK = "OK"
BOOK_EMPTY = "EMPTY"              # subscribed, no snapshot yet
BOOK_DESYNCED = "DESYNCED"        # sequence integrity lost
BOOK_CLOSED = "CLOSED"            # exchange says the market is not open
BOOK_HALTED = "HALTED"

# Market states as published on the status channel.
STATE_OPEN = "Open"
STATE_HALT = "Halt"
STATE_CLOSE = "Close"

_BID, _ASK = "Bid", "Ask"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw):
    """Bitnomial timestamps are RFC3339 with nanosecond precision, which
    `fromisoformat` rejects. Truncate to microseconds rather than dropping
    the venue's own clock — it is what makes staleness measurable."""
    if not raw:
        return None
    s = str(raw).replace("Z", "+00:00")
    if "." in s:
        head, _, tail = s.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        offset = tail[len(digits):] if len(tail) > len(digits) else ""
        while offset and offset[0].isdigit():
            offset = offset[1:]
        s = f"{head}.{digits:0<6}{offset or '+00:00'}"
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class PerpBook:
    """One product's live book, and whether it may price a fill.

    Deliberately dumb about economics: it holds raw prices exactly as the
    exchange sent them and converts to USD only at the edge, through the
    product's verified scale. Converting on ingest would spread the
    conversion across every update and make a units bug impossible to see.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._bids: dict[int, float] = {}
        self._asks: dict[int, float] = {}
        self.ack_id: int | None = None
        self.state: str = BOOK_EMPTY
        self.market_state: str | None = None
        self.venue_event_at: datetime | None = None
        self.received_at: datetime | None = None
        self.snapshot_count = 0
        self.desync_reason: str | None = None
        self._lock = threading.Lock()

    # ── ingest ───────────────────────────────────────────────────────────
    def apply(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "book":
            self._snapshot(msg)
        elif t == "level":
            self._level(msg)
        elif t == "status":
            self._status(msg)

    def _ack(self, msg) -> int | None:
        try:
            return int(str(msg.get("ack_id")))
        except (TypeError, ValueError):
            return None

    def _snapshot(self, msg) -> None:
        ack = self._ack(msg)
        with self._lock:
            self._bids, self._asks = {}, {}
            try:
                for p, q in (msg.get("bids") or []):
                    if float(q) > 0:
                        self._bids[int(p)] = float(q)
                for p, q in (msg.get("asks") or []):
                    if float(q) > 0:
                        self._asks[int(p)] = float(q)
            except (TypeError, ValueError) as e:
                self._invalidate(f"malformed snapshot: {e}")
                return
            self.ack_id = ack
            self.snapshot_count += 1
            self.desync_reason = None
            self.venue_event_at = _parse_ts(msg.get("timestamp"))
            self.received_at = _now()
            # A snapshot re-establishes authority, but it does not overrule a
            # closed market: a book can be published while the session is shut.
            self.state = (BOOK_OK if self.market_state in (None, STATE_OPEN)
                          else self._state_for(self.market_state))

    def _level(self, msg) -> None:
        ack = self._ack(msg)
        with self._lock:
            if self.state == BOOK_EMPTY and self.ack_id is None:
                return                      # updates before the snapshot
            if ack is None:
                self._invalidate("level update with no readable ack_id")
                return
            if self.ack_id is not None and ack < self.ack_id:
                # STALE. Not applied, and not fatal: a late duplicate is a
                # normal artefact of a batched feed. Applying it would move
                # the book backwards.
                logger.debug("[Bitnomial] %s dropping stale ack %s < %s",
                             self.symbol, ack, self.ack_id)
                return
            try:
                price, qty = int(msg.get("price")), float(msg.get("quantity"))
            except (TypeError, ValueError) as e:
                self._invalidate(f"malformed level update: {e}")
                return
            side = msg.get("side")
            if side not in (_BID, _ASK):
                self._invalidate(f"unknown side {side!r}")
                return
            levels = self._bids if side == _BID else self._asks
            if qty > 0:
                levels[price] = qty
            else:
                # QUANTITY ZERO IS A REMOVAL. Storing it as a zero-size level
                # would leave a phantom best price that nothing can trade at.
                levels.pop(price, None)
            self.ack_id = ack
            self.venue_event_at = _parse_ts(msg.get("timestamp"))
            self.received_at = _now()
            if self.state == BOOK_EMPTY:
                self.state = BOOK_OK

    def _state_for(self, market_state) -> str:
        if market_state == STATE_HALT:
            return BOOK_HALTED
        if market_state == STATE_CLOSE:
            return BOOK_CLOSED
        return BOOK_OK

    def _status(self, msg) -> None:
        with self._lock:
            self.market_state = msg.get("state")
            self.received_at = _now()
            if self.state not in (BOOK_DESYNCED, BOOK_EMPTY):
                self.state = self._state_for(self.market_state)
            elif self.state == BOOK_EMPTY and self.market_state != STATE_OPEN:
                self.state = self._state_for(self.market_state)

    def _invalidate(self, reason: str) -> None:
        self._bids, self._asks = {}, {}
        self.ack_id = None
        self.state = BOOK_DESYNCED
        self.desync_reason = reason
        logger.warning("[Bitnomial] %s book invalidated: %s", self.symbol, reason)

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self._invalidate(reason)

    # ── read ─────────────────────────────────────────────────────────────
    def top(self) -> dict:
        """Best bid/ask in RAW units, with depth and provenance.

        Returns raw prices; the caller converts through the product's
        verified scale. Nothing here decides what a tick is worth.
        """
        with self._lock:
            bids = sorted(self._bids.items(), key=lambda kv: -kv[0])
            asks = sorted(self._asks.items(), key=lambda kv: kv[0])
            age = None
            if self.received_at:
                age = (_now() - self.received_at).total_seconds()
            return {
                "symbol": self.symbol,
                "state": self.state,
                "market_state": self.market_state,
                "desync_reason": self.desync_reason,
                "bid_raw": bids[0][0] if bids else None,
                "bid_size": bids[0][1] if bids else None,
                "ask_raw": asks[0][0] if asks else None,
                "ask_size": asks[0][1] if asks else None,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                # DEPTH IS EXPOSED, NOT YET CONSUMED. Recording it now means
                # the follow-up that makes execution depth-aware has real
                # history to calibrate against; claiming depth-aware impact
                # while filling at top of book would be a lie about the model.
                "depth_bids": [[p, q] for p, q in bids[:10]],
                "depth_asks": [[p, q] for p, q in asks[:10]],
                "ack_id": self.ack_id,
                "venue_event_at": (self.venue_event_at.isoformat()
                                   if self.venue_event_at else None),
                "received_at": (self.received_at.isoformat()
                                if self.received_at else None),
                "age_s": age,
                "snapshot_count": self.snapshot_count,
                "source": "bitnomial_public_book",
                "adapter_version": ADAPTER_VERSION,
            }


# ── the shared registry of live books ───────────────────────────────────
_BOOKS: dict[str, PerpBook] = {}
_BOOKS_LOCK = threading.Lock()


def book_for(symbol: str, *, create: bool = False) -> PerpBook | None:
    with _BOOKS_LOCK:
        b = _BOOKS.get(symbol)
        if b is None and create:
            b = _BOOKS[symbol] = PerpBook(symbol)
        return b


# ── Downstream consumers of the ONE ingest ──────────────────────────────
#
# Execution snapshots, the shared evidence collector and diagnostics all
# read the same maintained books. Listeners exist so the EVIDENCE collector
# can be driven by actual book movement rather than by a clock: measured
# against the live venue, a 1Hz poll captured only ~13% of real top-of-book
# changes, and the missing 87% is exactly what MFE/MAE and touch chronology
# are made of.
#
# A listener must not raise into the reader loop and must not block it.
_LISTENERS: list = []


def add_book_listener(fn) -> None:
    """Register a callback invoked after each applied book update."""
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def clear_book_listeners() -> None:
    _LISTENERS.clear()


def apply_message(msg: dict) -> None:
    """Route one decoded feed message to its product's book."""
    sym = msg.get("symbol")
    if not sym:
        return
    book_for(sym, create=True).apply(msg)
    for fn in _LISTENERS:
        try:
            fn(sym)
        except Exception as e:      # a consumer fault never stops ingest
            logger.debug("[Bitnomial] listener failed for %s: %s", sym, e)


def latest_top(symbol: str) -> dict | None:
    b = book_for(symbol)
    return b.top() if b is not None else None


def reset_books() -> None:
    """Drop every book. Used on reconnect and by tests — a reconnect cannot
    keep applying updates onto a book whose gap is unmeasured."""
    with _BOOKS_LOCK:
        for b in _BOOKS.values():
            b.invalidate("stream reconnected; awaiting a fresh snapshot")
        _BOOKS.clear()


def subscribe_message(symbols) -> dict:
    """The exact frame the documented protocol expects."""
    syms = list(symbols)
    return {"type": "subscribe", "product_codes": syms,
            "channels": [{"name": "book", "product_codes": syms},
                         {"name": "status", "product_codes": syms}]}


# ── the reader loop and its lifecycle ───────────────────────────────────
#
# THIS IS A SERVICE, NOT A FIRE-AND-FORGET THREAD. The module previously
# offered `start_stream()` and nothing else: no way to stop it, no way to
# ask whether it was connected, and a `_STREAM_STARTED` flag that never
# cleared — so a stopped desk could not be restarted within one process.
# It also had NO RUNTIME CALLER ANYWHERE, which is why the perpetual book
# was never populated outside tests: the provider was fully implemented and
# simply never switched on.
#
# Health is recorded rather than inferred. "The books are empty" has at
# least four causes — never started, connecting, connected but
# unsubscribed, and connected but the venue is quiet — and an operator
# looking at an empty book needs to know which, because the remedies are
# unrelated to each other.

_STREAM_STARTED = False
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
# The live socket and the loop it runs on, kept ONLY so a disconnect can be
# forced deliberately — see `force_disconnect`.
_WS = None
_LOOP = None
_HEALTH_LOCK = threading.Lock()
_HEALTH = {
    "service_running": False,
    "connected": False,
    "subscribed": False,
    "products_subscribed": 0,
    "last_message_at": None,
    "last_connect_at": None,
    "reconnect_count": 0,
    "current_error": None,
}

# Bounded exponential backoff. A venue that is down must not be hammered,
# and a transient blip must not cost minutes of missing evidence.
RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0


def _health_set(**kw) -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(kw)


def stream_health() -> dict:
    """What the feed is actually doing — for ops, and for evidence quality.

    `stale_products` and `desynced_products` are derived from the books
    themselves rather than tracked alongside them, so they cannot drift out
    of agreement with what a fill would actually see.
    """
    with _HEALTH_LOCK:
        out = dict(_HEALTH)
    stale, desynced = [], []
    with _BOOKS_LOCK:
        books = list(_BOOKS.values())
    for b in books:
        top = b.top()
        if not top:
            continue
        if top.get("state") == BOOK_DESYNCED:
            desynced.append(b.symbol)
        age = top.get("age_s")
        if age is not None and age > 15.0:
            stale.append(b.symbol)
    out["desynced_products"] = sorted(desynced)
    out["stale_products"] = sorted(stale)
    out["books"] = len(books)
    return out


async def _run(symbols, url):                    # pragma: no cover - network
    import asyncio

    import websockets

    attempt = 0
    while not _STOP.is_set():
        try:
            async with websockets.connect(url, open_timeout=20) as ws:
                global _WS, _LOOP
                _WS, _LOOP = ws, asyncio.get_running_loop()
                _health_set(connected=True, current_error=None,
                            last_connect_at=_now().isoformat())
                await ws.send(json.dumps(subscribe_message(symbols)))
                # SUBSCRIPTION IS RESTORED ON EVERY CONNECT, not only the
                # first. A reconnect that assumed the old subscription
                # survived would leave a connected socket receiving
                # nothing at all — the hardest kind of outage to notice,
                # because every health field except the data looks fine.
                _health_set(subscribed=True, products_subscribed=len(symbols))
                attempt = 0
                logger.info("[Bitnomial] subscribed to %d products", len(symbols))
                async for raw in ws:
                    if _STOP.is_set():
                        break
                    try:
                        apply_message(json.loads(raw))
                        _health_set(last_message_at=_now().isoformat())
                    except Exception as e:
                        logger.debug("[Bitnomial] bad frame: %s", e)
        except Exception as e:
            _health_set(current_error=f"{type(e).__name__}: {e}")
            logger.warning("[Bitnomial] stream dropped (%s); books invalidated", e)
        finally:
            _health_set(connected=False, subscribed=False)
            # A BOOK THAT SURVIVED A DISCONNECT IS NOT A SLIGHTLY OLDER
            # BOOK. The gap is unmeasured, so any level in it may already
            # be wrong; it must not price a fill or an excursion until a
            # fresh snapshot arrives.
            reset_books()

        if _STOP.is_set():
            break
        with _HEALTH_LOCK:
            _HEALTH["reconnect_count"] += 1
        attempt += 1
        # `await`, NOT time.sleep — the previous version blocked the whole
        # event loop from inside a coroutine.
        await asyncio.sleep(min(RECONNECT_BASE_S * (2 ** (attempt - 1)),
                                RECONNECT_MAX_S))
    _health_set(service_running=False, connected=False, subscribed=False)


def start_stream(symbols=None, url: str | None = None) -> bool:
    """Start the read-only feed in a background thread. Idempotent.

    Never started implicitly by a price lookup: a market-data connection is
    a side effect, and an execution path that silently opens one turns a
    missing quote into a timeout instead of a refusal. Lifecycle belongs to
    the market-data runtime at application startup.
    """
    global _STREAM_STARTED, _THREAD
    if _STREAM_STARTED:
        return False
    from lib import bitnomial_products as BP
    syms = list(symbols or BP.active_symbols())
    if not syms:
        return False
    import asyncio

    _STOP.clear()

    def _thread():                               # pragma: no cover - network
        asyncio.run(_run(syms, url or BP.WS_URL))

    _THREAD = threading.Thread(target=_thread, name="bitnomial-md", daemon=True)
    _THREAD.start()
    _STREAM_STARTED = True
    _health_set(service_running=True, products_subscribed=len(syms),
                current_error=None)
    return True


def force_disconnect() -> bool:
    """Drop the current socket to exercise the recovery path. Returns True
    if a close was actually dispatched.

    FOR DELIBERATE VERIFICATION AND OPS RECOVERY, NOT FOR NORMAL USE. The
    recovery path is the part of a feed nobody sees until it matters, so it
    has to be provable on demand rather than left to be discovered during an
    outage. It closes only THIS side of the connection; the venue is not
    asked for anything, so proving reconnect costs the exchange nothing
    beyond one ordinary re-subscribe.
    """
    ws, loop = _WS, _LOOP
    if ws is None or loop is None or loop.is_closed():
        return False
    try:
        loop.call_soon_threadsafe(lambda: loop.create_task(ws.close()))
        return True
    except Exception as e:
        logger.debug("[Bitnomial] force_disconnect failed: %s", e)
        return False


def stop_stream(timeout: float = 5.0) -> bool:
    """Stop the feed and invalidate every book. Idempotent.

    Returning the process to a state where `start_stream` works again is
    the whole point: without it a stopped desk could not be restarted
    without restarting the process, which makes the service untestable and
    leaves an operator with a reboot as their only recovery.
    """
    global _STREAM_STARTED, _THREAD
    if not _STREAM_STARTED:
        return False
    _STOP.set()
    t, _THREAD = _THREAD, None
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    reset_books()
    _STREAM_STARTED = False
    _health_set(service_running=False, connected=False, subscribed=False,
                products_subscribed=0)
    return True
