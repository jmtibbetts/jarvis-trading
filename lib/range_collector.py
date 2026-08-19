"""ONE market, sampled ONCE, serving every decision that is watching it.

WHY THIS IS SHARED AND NOT PER-DECISION.

MFE and MAE are claims about what happened BETWEEN two times. A quote at T0
and a quote at T0+15m cannot support either of them: the pair is consistent
with a straight line, with a spike to the target and back, and with a spike
through the stop and back. Those are three completely different verdicts on
the same decision, and picking one is fabrication. So forward evidence needs
samples ACROSS the interval — and the moment you accept that, the storage
question decides the architecture.

The naive shape is a sampler per pending observation. It is also the one
shape that cannot work: two hundred pending BTC-perp observations describe
two hundred decisions but exactly ONE market, so per-decision sampling stores
the same book two hundred times and scales with decision volume rather than
with the number of instruments that exist. Evidence is therefore keyed by
INSTRUMENT AND TIME, and every observation whose interval overlaps those rows
reads the same ones. Adding a decision costs nothing.

WHY CHRONOLOGY IS KEPT.

An earlier draft of this module stored per-minute high/low buckets. Buckets
are cheaper and can prove that a level was reached; they cannot prove WHICH
level was reached first, because ordering inside a bucket is erased by
construction — and that ordering is the entire difference between a win and
a loss. Nor can it be precomputed away: every observation carries its own
stop and target, so a shared market row cannot know which levels will matter
to a decision nobody has made yet. Timestamped samples keep the ordering.
Compaction into aggregates stays possible once every dependent horizon is
final, and is deliberately not built yet.

QUIET IS NOT THE SAME AS DOWN.

Sampling is change-triggered with a HEARTBEAT FLOOR: a row is written when
the top of book moves, and at least once per heartbeat while the feed is
healthy even when it does not. Without that floor a calm market and a dead
connection produce identical evidence — no rows — and only one of them ought
to reduce the quality of a measurement. With it, a gap in the samples means
the feed was actually down, which is exactly what a restart must not be able
to hide.

WHAT THIS MODULE REFUSES TO DO.

It does not read a venue directly and it opens no connection of its own.
Samples come from the maintained in-memory book via
`execution_snapshot.execution_market_snapshot()`, which is already the
product-correct authority: it knows the Kraken spot socket does not speak for
a perpetual, and it already refuses SHIB, whose price scale was never
verified. Sampling through that seam means the perp/spot substitution cannot
be reintroduced here by accident, and a product that fails closed at
execution also fails closed as evidence.

Only an AVAILABLE snapshot becomes a sample. A stale, crossed, one-sided or
desynced book is NOT recorded as a price — it produces no sample, which shows
up downstream as a gap and LOWERS the quality of the claim. That is the
intended behaviour: an outage must degrade the evidence, never be smoothed
over by the last good number.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RANGE_COLLECTOR_VERSION = "range_collector_v3_instrument_key"

# ── EXACT-CONTRACT PRODUCTS ──────────────────────────────────────────────
#
# A perpetual's economics belong to a LISTED CONTRACT, not to a display
# symbol. "BTC/USD perp" names a thesis; PBTCUCZ50 is the thing whose book
# actually determines the P&L. Two contracts can share a symbol, a venue and
# a product and still be different markets.
#
# THE RULE THAT MATTERS: for these products a NULL instrument_id means the
# contract is UNKNOWN. It never means "any contract". Anonymous evidence
# cannot become exact-contract evidence merely because no other contract
# happened to be present — that is how a resolver ends up accidentally
# right, which is indistinguishable from correct until the day it is not.
_EXACT_INSTRUMENT_PRODUCTS = frozenset({"CRYPTO_PERP"})


def requires_exact_instrument(product: str | None) -> bool:
    """Whether evidence for this product must match a specific contract.

    One authority, consulted by every seam, so the rule cannot be enforced
    in six places and quietly forgotten in the seventh.
    """
    return str(product or "") in _EXACT_INSTRUMENT_PRODUCTS

# While the feed is healthy, write at least one row this often even if the
# book has not moved. This is the floor that makes "quiet" legible.
HEARTBEAT_S = 30.0

# A hole longer than this means the heartbeat itself stopped arriving, which
# only happens when collection was actually down. Three heartbeats of slack
# absorbs ordinary jitter without absorbing an outage.
GAP_THRESHOLD_S = 3 * HEARTBEAT_S

# ── Evidence quality, in strict precedence order ─────────────────────────
#
# The operator vocabulary also lists STREAM_OBSERVED. It is deliberately NOT
# a separate tier: nothing measurable distinguishes it from
# HIGH_FREQUENCY_SAMPLED, and a label no measurement can tell apart is one
# that will eventually be wrong without anyone noticing.
INSUFFICIENT_RANGE_DATA = "INSUFFICIENT_RANGE_DATA"
GAP_PRESENT             = "GAP_PRESENT"
PARTIAL                 = "PARTIAL"
HIGH_FREQUENCY_SAMPLED  = "HIGH_FREQUENCY_SAMPLED"
COARSE_SAMPLED          = "COARSE_SAMPLED"

# Fraction of the interval that must lie between the first and last sample
# before the evidence counts as covering it.
MIN_COVERAGE = 0.9

# Mean samples per minute at or above which the feed was genuinely
# streaming rather than merely heartbeating.
HIGH_FREQUENCY_PER_MIN = 4.0

CHANGE    = "CHANGE"
HEARTBEAT = "HEARTBEAT"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        p = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return p if p.tzinfo else p.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Writing samples ──────────────────────────────────────────────────────

def _should_record(prev_bid, prev_ask, prev_at, bid, ask, at,
                   heartbeat_s: float) -> str | None:
    """THE SAMPLING RULE, in one place. CHANGE, HEARTBEAT, or None.

    Both the event-driven hot path and the polling path go through this, so
    there is one definition of what gets kept rather than two that drift.
    """
    if prev_bid is None or prev_ask is None or prev_at is None:
        return CHANGE
    if prev_bid != bid or prev_ask != ask:
        return CHANGE
    # Unchanged book: keep it only to hold the heartbeat up, so a quiet
    # market stays visibly observed without paying per poll.
    return HEARTBEAT if (at - prev_at).total_seconds() >= heartbeat_s else None


# ── The event-driven hot path ────────────────────────────────────────────
#
# MEASURED 2026-08-18 against the live venue: 16 active perpetuals produced
# ~180 provider messages/sec and ~31 TOP-OF-BOOK changes/sec (~115 per
# product per minute), while a 1Hz polling collector persisted only ~15 per
# product per minute — **about 13% of the real book movement.** The missing
# 87% is exactly the evidence MFE/MAE and touch chronology are made of, so
# the collector is driven by the ingest itself rather than by a clock.
#
# In-memory dedupe and a buffered writer keep that affordable: one dict
# lookup per update instead of a SELECT, and one batched INSERT per flush
# instead of a transaction per tick.

_LAST: dict = {}
_BUF: list = []
_BUF_LOCK = threading.Lock()
# ONE limit, not two. This used to be a separate, SILENT cap at
# 5,000 that fired before the counted one and discarded samples
# with no record. Retained as an alias so existing importers
# see the same single number.
MAX_BUFFER = 20_000


def note_quote(*, symbol: str, product: str, venue: str, bid, ask,
               at: datetime | None = None, source: str | None = None,
               instrument_id: str | None = None,
               heartbeat_s: float = HEARTBEAT_S) -> str | None:
    """Offer one top-of-book observation to the shared evidence buffer.

    Called from the market-data ingest on every book update. Cheap by
    design: no database work happens here, so a busy feed never blocks on
    a writer.
    """
    if bid is None or ask is None:
        return None
    at = at or _now()
    # SEAM 1. Contract is part of the stream identity: two contracts sharing
    # a visible symbol must keep independent change/heartbeat state, or one
    # suppresses the other's samples.
    key = (product, venue, symbol, instrument_id)
    prev = _LAST.get(key)
    reason = _should_record(*(prev or (None, None, None)), bid, ask, at,
                            heartbeat_s)
    if reason is None:
        return None
    _LAST[key] = (bid, ask, at)
    global _RECEIVED, _SHED_ON_APPEND
    with _BUF_LOCK:
        _RECEIVED += 1
        if len(_BUF) < MAX_BUFFERED_SAMPLES:
            _BUF.append({
                "product": product, "venue": venue, "symbol": symbol,
                "instrument_id": instrument_id, "market_data_source": source,
                "observed_at": at.isoformat(), "bid": bid, "ask": ask,
                "mid": (bid + ask) / 2.0, "sample_reason": reason,
            })
        else:
            # COUNTED, not silent. This used to drop the sample with no
            # record at all, so `received` and `persisted` could diverge
            # without anything in the system being able to say why.
            _SHED_ON_APPEND += 1
            if _SHED_ON_APPEND % 500 == 1:
                logger.error("[RangeCollector] buffer full at %d — shed %d "
                             "sample(s) on append so far",
                             MAX_BUFFERED_SAMPLES, _SHED_ON_APPEND)
    return reason


# A database that is briefly locked must not cost us evidence; a database
# that is down for hours must not cost us the process. Between those two,
# this is how many samples may wait in memory. At the observed ~25 rows/s
# this is roughly ten minutes of buffering.
MAX_BUFFERED_SAMPLES = 20_000

_DROPPED = 0          # shed from the FRONT after a failed write
_RECEIVED = 0         # observations that passed the change filter
_PERSISTED = 0        # rows actually committed
_RETRIED = 0          # rows put back after a failed write
_SHED_ON_APPEND = 0   # dropped because the buffer was already full


def ingestion_stats() -> dict:
    """Received vs persisted, and every way the two can differ.

    A collector must never be able to report health while `persisted` is far
    below `received` with nothing to explain the gap. Every divergence here
    has a name and a counter.

    ON CRASH EXPOSURE: the buffer is RAM. Flush cadence is 1s at a measured
    7-25 rows/s, so an abrupt kill loses about one second of samples. A
    durable spool was considered and NOT built: it would add a second write
    path competing for the same SQLite lock — the very contention that
    caused the original loss — plus checkpointing and dedup, to save ~1s of
    replaceable book snapshots.
    """
    with _BUF_LOCK:
        backlog = len(_BUF)
        oldest = _BUF[0].get("observed_at") if _BUF else None
    return {
        "received": _RECEIVED,
        "persisted": _PERSISTED,
        "retried": _RETRIED,
        "shed_on_append": _SHED_ON_APPEND,
        "shed_after_failure": _DROPPED,
        "backlog": backlog,
        "oldest_backlog_at": oldest,
        "buffer_limit": MAX_BUFFERED_SAMPLES,
        "flush_interval_s": 1.0,
        "unaccounted": _RECEIVED - _PERSISTED - _SHED_ON_APPEND
                       - _DROPPED - backlog,
    }


def dropped_sample_count() -> int:
    """Samples that were genuinely discarded, counted rather than forgotten.

    A silent drop and a reported drop are different failures. This is the
    reported one.
    """
    return _DROPPED


def flush_samples() -> int:
    """Write buffered observations. Returns rows written.

    ON FAILURE THE BATCH GOES BACK. This used to drain the buffer BEFORE
    the insert, so a transient "database is locked" — routine under WAL with
    a high-rate writer and 31 scheduler jobs — destroyed the batch
    permanently while the caller logged a warning and moved on. Measured in
    production: two failures in 35 minutes, ~100 Bitnomial quote samples
    gone. Paid evidence that cannot be re-fetched is exactly what must
    survive a transient error, so the rows are restored to the FRONT of the
    buffer and retried on the next tick.
    """
    global _DROPPED, _PERSISTED, _RETRIED
    from app.database import InstrumentQuoteSample, get_db

    with _BUF_LOCK:
        if not _BUF:
            return 0
        batch, _BUF[:] = list(_BUF), []
    try:
        with get_db() as db:
            db.bulk_insert_mappings(InstrumentQuoteSample.__mapper__, batch)
    except Exception:
        with _BUF_LOCK:
            _RETRIED += len(batch)
            # Front, because these are older than anything buffered while
            # the write was in flight, and chronology is the point.
            _BUF[:0] = batch
            excess = len(_BUF) - MAX_BUFFERED_SAMPLES
            if excess > 0:
                # Shed the OLDEST: if the database has been unreachable long
                # enough to overflow, recent market state is worth more than
                # stale market state.
                del _BUF[:excess]
                _DROPPED += excess
                logger.error("[RangeCollector] evidence buffer full — "
                             "dropped %d oldest sample(s), %d total dropped",
                             excess, _DROPPED)
        raise
    _PERSISTED += len(batch)
    return len(batch)


def buffered_count() -> int:
    with _BUF_LOCK:
        return len(_BUF)


def reset_stream_state() -> None:
    """Forget in-memory dedupe state. Used on disconnect and by tests.

    A reconnect must not compare a fresh book against a pre-disconnect one
    — the interval between them is unobserved, so the first post-reconnect
    quote is genuinely new evidence even if the numbers happen to match.
    """
    _LAST.clear()
    with _BUF_LOCK:
        _BUF.clear()


def record_sample(*, symbol: str, product: str, venue: str, snap,
                  at: datetime | None = None,
                  heartbeat_s: float = HEARTBEAT_S) -> bool:
    """Fold one venue snapshot into the shared evidence. True if written.

    Only an AVAILABLE two-sided snapshot is a sample. Everything else is a
    refusal this module deliberately does not paper over — see the module
    docstring.
    """
    from app.database import InstrumentQuoteSample, get_db

    if snap is None or not getattr(snap, "fillable", False):
        return False
    bid, ask = snap.bid, snap.ask
    if bid is None or ask is None:
        return False

    at = at or _now()
    instrument_id = getattr(snap, "instrument_id", None)
    if requires_exact_instrument(product) and not instrument_id:
        # SEAM 7 (producer half). An exact product whose snapshot carries no
        # contract cannot be filed under one.
        return False

    with get_db() as db:
        # SEAM 2. The previous sample must describe the SAME stream. Comparing
        # a PBTCUCZ50 quote against an anonymous or different-contract row
        # corrupts both the data and the CHANGE/HEARTBEAT classification.
        q = (db.query(InstrumentQuoteSample)
               .filter(InstrumentQuoteSample.product == product,
                       InstrumentQuoteSample.venue == venue,
                       InstrumentQuoteSample.symbol == symbol))
        if requires_exact_instrument(product):
            q = q.filter(InstrumentQuoteSample.instrument_id == instrument_id)
        prev = q.order_by(InstrumentQuoteSample.observed_at.desc()).first()
        reason = _should_record(
            prev.bid if prev else None, prev.ask if prev else None,
            _parse(prev.observed_at) if prev else None,
            bid, ask, at, heartbeat_s)
        if reason is None:
            return False

        db.add(InstrumentQuoteSample(
            product=product, venue=venue, symbol=symbol,
            instrument_id=instrument_id,
            market_data_source=getattr(snap, "source", None),
            observed_at=at.isoformat(),
            source_at=getattr(snap, "venue_event_at", None),
            bid=bid, ask=ask, mid=(bid + ask) / 2.0,
            sample_reason=reason))
        return True


# ── Reading interval evidence ────────────────────────────────────────────

@dataclass
class Sample:
    """A plain, session-free copy of one shared sample.

    ORM rows are deliberately NOT returned from this module. An earlier
    version handed live instances back to callers, and every attribute read
    after the session closed raised DetachedInstanceError into a caller's
    `except` — so resolution failed silently and rows sat PENDING forever
    while the logs said nothing louder than a warning. Detaching the data
    at the boundary makes that whole class of bug impossible.
    """

    at: datetime
    bid: float
    ask: float
    mid: float
    reason: str | None = None
    # Self-describing on purpose: an acceptance proof should not have to
    # trust that some SQL WHERE clause upstream was written correctly.
    instrument_id: str | None = None


@dataclass
class RangeEvidence:
    """What the market did across an interval, and how well we saw it."""

    symbol: str
    product: str
    venue: str
    instrument_id: str | None
    start: str
    end: str

    high_bid: float | None = None
    low_bid: float | None = None
    high_ask: float | None = None
    low_ask: float | None = None
    high_mid: float | None = None
    low_mid: float | None = None

    sample_count: int = 0
    first_sample_at: str | None = None
    last_sample_at: str | None = None
    max_sample_gap_s: float | None = None
    coverage_ratio: float | None = None

    quality: str = INSUFFICIENT_RANGE_DATA
    samples: list[Sample] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether these extrema may be quoted at all.

        INSUFFICIENT is the ONLY tier that blocks. PARTIAL and GAP_PRESENT
        describe real evidence about a real interval; it simply travels
        with a lower label so a study can filter on it. Discarding it would
        trade a stated weakness for a silent absence.
        """
        return self.quality != INSUFFICIENT_RANGE_DATA


def samples_between(*, symbol: str, product: str, venue: str,
                    start, end, instrument_id: str | None = None
                    ) -> list[Sample]:
    """Chronological shared samples in [start, end], detached from the DB.

    SEAM 3. For an exact-contract product the instrument is REQUIRED and is
    filtered in SQL. Omitting it returns nothing rather than everything:
    a missing contract is unknown identity, never a wildcard.
    """
    from app.database import InstrumentQuoteSample, get_db

    s, e = _parse(start), _parse(end)
    if s is None or e is None:
        return []
    if requires_exact_instrument(product) and not instrument_id:
        return []
    with get_db() as db:
        q = (db.query(InstrumentQuoteSample.observed_at,
                      InstrumentQuoteSample.bid,
                      InstrumentQuoteSample.ask,
                      InstrumentQuoteSample.mid,
                      InstrumentQuoteSample.sample_reason,
                      InstrumentQuoteSample.instrument_id)
               .filter(InstrumentQuoteSample.product == product,
                       InstrumentQuoteSample.venue == venue,
                       InstrumentQuoteSample.symbol == symbol,
                       InstrumentQuoteSample.observed_at >= s.isoformat(),
                       InstrumentQuoteSample.observed_at <= e.isoformat()))
        if requires_exact_instrument(product):
            q = q.filter(InstrumentQuoteSample.instrument_id == instrument_id)
        rows = q.order_by(InstrumentQuoteSample.observed_at.asc()).all()
    out = []
    for at, bid, ask, mid, reason, instr in rows:
        t = _parse(at)
        if t is None or bid is None or ask is None:
            continue
        out.append(Sample(at=t, bid=float(bid), ask=float(ask),
                          mid=float(mid) if mid is not None
                          else (float(bid) + float(ask)) / 2.0,
                          reason=reason, instrument_id=instr))
    return out


def range_over(*, symbol: str, product: str, venue: str,
               start, end, instrument_id: str | None = None
               ) -> RangeEvidence:
    """Aggregate the shared samples covering [start, end].

    THE GAP ARITHMETIC IS THE POINT. A window is continuously observed only
    if there is no hole anywhere in it, and holes hide in three places:
    between consecutive samples, and at the two EDGES where collection may
    have started late or stopped early. A restart that took the collector
    down for eight minutes of a fifteen-minute window leaves every
    surviving sample looking perfect; only the between-sample and edge
    measurements see it. All three are computed and the largest wins.

    Because sampling has a heartbeat floor, a large gap means the feed was
    down — not that the market was calm.
    """
    s, e = _parse(start), _parse(end)
    ev = RangeEvidence(symbol=symbol, product=product, venue=venue,
                       instrument_id=instrument_id,
                       start=s.isoformat() if s else str(start),
                       end=e.isoformat() if e else str(end))
    if s is None or e is None or e <= s:
        return ev

    rows = samples_between(symbol=symbol, product=product, venue=venue,
                           instrument_id=instrument_id,
                           start=s, end=e)
    if not rows:
        return ev

    ev.samples = rows
    ev.sample_count = len(rows)
    ev.high_bid = max(r.bid for r in rows)
    ev.low_bid = min(r.bid for r in rows)
    ev.high_ask = max(r.ask for r in rows)
    ev.low_ask = min(r.ask for r in rows)
    ev.high_mid = max(r.mid for r in rows)
    ev.low_mid = min(r.mid for r in rows)
    ev.first_sample_at = rows[0].at.isoformat()
    ev.last_sample_at = rows[-1].at.isoformat()

    gaps = [(b.at - a.at).total_seconds() for a, b in zip(rows, rows[1:])]
    gaps.append(max(0.0, (rows[0].at - s).total_seconds()))    # late start
    gaps.append(max(0.0, (e - rows[-1].at).total_seconds()))   # early stop
    ev.max_sample_gap_s = round(max(gaps), 3) if gaps else None

    span = (e - s).total_seconds()
    observed = (rows[-1].at - rows[0].at).total_seconds()
    ev.coverage_ratio = round(min(1.0, observed / span), 4) if span > 0 else None

    # ONE SAMPLE IS NOT A RANGE. Endpoint-only evidence is exactly the case
    # this module exists to refuse.
    if ev.sample_count < 2:
        ev.quality = INSUFFICIENT_RANGE_DATA
    elif (ev.max_sample_gap_s or 0.0) > GAP_THRESHOLD_S:
        ev.quality = GAP_PRESENT
    elif (ev.coverage_ratio or 0.0) < MIN_COVERAGE:
        ev.quality = PARTIAL
    elif ev.sample_count / max(span / 60.0, 1e-9) >= HIGH_FREQUENCY_PER_MIN:
        ev.quality = HIGH_FREQUENCY_SAMPLED
    else:
        ev.quality = COARSE_SAMPLED
    return ev


@dataclass
class Checkpoint:
    """The market as it stood AT a stated instant, from shared evidence."""

    bid: float | None = None
    ask: float | None = None
    at: str | None = None
    age_s: float | None = None
    source: str | None = None
    ok: bool = False
    reason: str | None = None
    instrument_id: str | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0


def checkpoint_at(*, symbol: str, product: str, venue: str, at,
                  tolerance_s: float = GAP_THRESHOLD_S,
                  instrument_id: str | None = None) -> Checkpoint:
    """The last observed book at or before `at`, if it is close enough.

    The observer must state the market at the HORIZON, not at its own
    wake-up, so the checkpoint is read BACKWARDS from `at` rather than
    taken live. Without this an observer running five minutes late would
    report a 20-minute horizon under a 15-minute label.

    A checkpoint older than `tolerance_s` is refused rather than stretched:
    a five-minute-old book does not describe the instant it is being asked
    about, and labelling it as though it did is the same error as treating
    a mark as a fill.
    """
    from app.database import InstrumentQuoteSample, get_db

    t = _parse(at)
    cp = Checkpoint(source=RANGE_COLLECTOR_VERSION,
                    instrument_id=instrument_id)
    if t is None:
        cp.reason = "unparseable checkpoint time"
        return cp
    if requires_exact_instrument(product) and not instrument_id:
        # SEAM 5. Without the contract there is nothing to be the checkpoint
        # OF, and the newest quote on ANOTHER contract must never win merely
        # because its observed_at is later.
        cp.reason = "exact contract identity unavailable for this product"
        return cp

    with get_db() as db:
        q = (db.query(InstrumentQuoteSample.observed_at,
                      InstrumentQuoteSample.bid,
                      InstrumentQuoteSample.ask,
                      InstrumentQuoteSample.market_data_source,
                      InstrumentQuoteSample.instrument_id)
               .filter(InstrumentQuoteSample.product == product,
                       InstrumentQuoteSample.venue == venue,
                       InstrumentQuoteSample.symbol == symbol,
                       InstrumentQuoteSample.observed_at <= t.isoformat()))
        if requires_exact_instrument(product):
            q = q.filter(InstrumentQuoteSample.instrument_id == instrument_id)
        row = q.order_by(InstrumentQuoteSample.observed_at.desc()).first()
    if row is None:
        cp.reason = "no shared evidence at or before this instant"
        return cp

    observed_at, bid, ask, src, instr = row
    cp.instrument_id = instr or instrument_id
    last = _parse(observed_at)
    if last is None or bid is None or ask is None:
        cp.reason = "sample carries no usable quote"
        return cp
    age = (t - last).total_seconds()
    cp.at, cp.age_s = last.isoformat(), round(age, 3)
    if age > tolerance_s:
        cp.reason = (f"nearest observation is {age:.0f}s before the horizon, "
                     f"older than the {tolerance_s:.0f}s allowed")
        return cp
    cp.bid, cp.ask = float(bid), float(ask)
    cp.source = src or RANGE_COLLECTOR_VERSION
    cp.ok = True
    return cp


# ── Collection ───────────────────────────────────────────────────────────

def instruments_pending() -> list[dict]:
    """The distinct instruments any pending outcome still needs covered.

    This is the shared-collection dividend made literal: the query is
    DISTINCT over instruments, so a thousand pending observations on one
    instrument produce exactly one stream to sample.
    """
    from app.database import DecisionOutcome, get_db

    with get_db() as db:
        rows = (db.query(DecisionOutcome.symbol, DecisionOutcome.product,
                         DecisionOutcome.venue, DecisionOutcome.instrument_id)
                  .filter(DecisionOutcome.status == "PENDING")
                  .distinct().all())
    out = []
    for sym, prod, ven, instr in rows:
        if not (sym and prod and ven):
            continue
        # SEAM 6. An exact product with no contract is NOT a wildcard target.
        # NEAR/USD intending a perpetual Bitnomial does not list stays
        # uncollectable rather than becoming "sample any NEAR perp".
        if requires_exact_instrument(prod) and not instr:
            continue
        out.append({"symbol": sym, "product": prod, "venue": ven,
                    "instrument_id": instr})
    return out


def collect_once(instruments: list[dict] | None = None,
                 heartbeat_s: float = HEARTBEAT_S) -> dict:
    """One sampling pass over every instrument with pending evidence.

    Reads the MAINTAINED in-memory books that the market-data runtime keeps
    up to date. It opens no connection and creates no second provider
    client, so one ingest serves execution, evidence and diagnostics alike.

    Runnable on its own — it does not need, and must not require, the
    autonomous trading scheduler to be running.
    """
    from lib.execution_snapshot import (DEFAULT_MAX_AGE_S,
                                        DEFAULT_PERP_MAX_AGE_S,
                                        execution_market_snapshot)

    targets = instruments_pending() if instruments is None else instruments
    out = {"instruments": len(targets), "recorded": 0, "refused": 0,
           "refusals": {}}
    for t in targets:
        product = t["product"]
        max_age = (DEFAULT_PERP_MAX_AGE_S if product == "CRYPTO_PERP"
                   else DEFAULT_MAX_AGE_S)
        try:
            snap = execution_market_snapshot(t["symbol"], t["venue"],
                                             product=product,
                                             max_age_s=max_age)
        except Exception as exc:            # a producer being down is normal
            out["refused"] += 1
            out["refusals"][f"{t['symbol']}:ERROR"] = str(exc)
            continue
        # SEAM 7. The snapshot must BE the contract we asked for. Writing
        # another contract's book under the requested instrument manufactures
        # exactly the contamination this key exists to prevent.
        want = t.get("instrument_id")
        if requires_exact_instrument(product) and want:
            got = getattr(snap, "instrument_id", None)
            if got != want:
                out["refused"] += 1
                out["refusals"][f"{t['symbol']}:INSTRUMENT_IDENTITY_MISMATCH"] = (
                    f"requested {want!r}, snapshot reported {got!r}")
                continue

        if record_sample(symbol=t["symbol"], product=product,
                         venue=t["venue"], snap=snap,
                         heartbeat_s=heartbeat_s):
            out["recorded"] += 1
        else:
            out["refused"] += 1
            if not snap.fillable:
                out["refusals"][f"{t['symbol']}:{snap.status}"] = snap.reason
    return out


def storage_projection(*, instruments: int, decisions_per_day: int,
                       changes_per_min: float = 6.0,
                       heartbeat_s: float = HEARTBEAT_S,
                       bytes_per_sample: int = 220) -> dict:
    """The audit the design decision rests on: shared vs per-decision.

    Reported rather than asserted, so it can be re-run when the instrument
    set or the measured message rate changes.
    """
    per_min = max(changes_per_min, 60.0 / heartbeat_s)
    shared_rows = instruments * per_min * 60 * 24
    shared_bytes = shared_rows * bytes_per_sample
    # The naive alternative: every decision samples its own instrument for
    # its own horizon. Cost scales with DECISIONS, which is the defect.
    naive_rows = decisions_per_day * per_min * 60 * 24
    naive_bytes = naive_rows * bytes_per_sample
    return {
        "instruments": instruments,
        "decisions_per_day": decisions_per_day,
        "samples_per_min_per_instrument": round(per_min, 2),
        "shared_rows_per_day": int(shared_rows),
        "shared_mb_per_day": round(shared_bytes / 1e6, 2),
        "shared_mb_per_30d": round(shared_bytes * 30 / 1e6, 2),
        "shared_gb_per_year": round(shared_bytes * 365 / 1e9, 2),
        "naive_rows_per_day": int(naive_rows),
        "naive_gb_per_year": round(naive_bytes * 365 / 1e9, 2),
        "ratio_naive_over_shared": (round(naive_rows / shared_rows, 1)
                                    if shared_rows else None),
    }
