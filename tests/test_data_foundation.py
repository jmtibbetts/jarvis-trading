"""Phase 3 raw data foundation — three clocks, BookHealth abstention,
counted backpressure, and the measured event store.

The properties under test are the ones that are impossible to retrofit:
an event written without its clocks can never get them back, a corrupt
book that produced an imbalance number has already poisoned its consumers,
and a queue that dropped without counting has already lied.
"""
import os
import tempfile
import time
import unittest

from lib.market_events import (
    INGEST_VERSION,
    BookSnapshotEvent,
    BoundedEventQueue,
    TradeEvent,
    event_to_dict,
    make_meta,
)
from lib.orderbook_stream import BOOK_STALE_SECONDS, OrderBook


class TestThreeClocks(unittest.TestCase):
    def test_meta_carries_all_three_clocks_and_provenance(self):
        m = make_meta("binance", "binance_l2_v1", exchange_ts=1000.0)
        self.assertEqual(m.source, "binance")
        self.assertEqual(m.source_schema_version, "binance_l2_v1")
        self.assertEqual(m.ingest_version, INGEST_VERSION)
        self.assertIsNotNone(m.exchange_ts)
        self.assertGreater(m.ingest_ts, 0)
        self.assertGreater(m.process_ts, 0)
        self.assertIsNotNone(m.clock_skew_ms)

    def test_missing_venue_clock_is_none_not_zero(self):
        """0.0 skew claims a perfectly synced clock; None says unknown.
        The difference matters to every staleness consumer downstream."""
        m = make_meta("binance", "binance_l2_v1", exchange_ts=None)
        self.assertIsNone(m.exchange_ts)
        self.assertIsNone(m.clock_skew_ms)

    def test_event_to_dict_promotes_meta_for_indexing(self):
        ev = TradeEvent(meta=make_meta("kraken", "kraken_v2", 5.0),
                        symbol="BTC/USD", price=50_000.0, size=0.1)
        d = event_to_dict(ev)
        for key in ("source", "exchange_ts", "ingest_ts", "process_ts",
                    "clock_skew_ms", "ingest_version", "kind", "symbol"):
            self.assertIn(key, d)
        self.assertEqual(d["kind"], "trade")


class TestBookHealth(unittest.TestCase):
    def _book(self, bids, asks, age=0.0):
        b = OrderBook()
        b.bids = dict(bids)
        b.asks = dict(asks)
        b.updated_at = time.time() - age
        return b

    def test_healthy_book_produces_stats(self):
        top = self._book({100.0: 1.0}, {101.0: 1.0}).top_levels()
        self.assertTrue(top["health"]["valid"])
        self.assertEqual(top["health"]["reason"], "ok")
        self.assertIsNotNone(top["imbalance"])
        self.assertEqual(top["best_bid"], 100.0)

    def test_crossed_book_abstains(self):
        """bid >= ask is venue fiction — every derived stat must be None,
        never a number computed from a book that cannot exist."""
        top = self._book({102.0: 1.0}, {101.0: 1.0}).top_levels()
        self.assertFalse(top["health"]["valid"])
        self.assertEqual(top["health"]["reason"], "crossed")
        self.assertIsNone(top["imbalance"])
        self.assertIsNone(top["spread"])
        self.assertIsNone(top["best_bid"])

    def test_one_sided_book_abstains(self):
        top = self._book({100.0: 1.0}, {}).top_levels()
        self.assertFalse(top["health"]["valid"])
        self.assertEqual(top["health"]["reason"], "empty_side")
        self.assertIsNone(top["imbalance"])

    def test_stale_book_abstains(self):
        top = self._book({100.0: 1.0}, {101.0: 1.0},
                         age=BOOK_STALE_SECONDS + 5).top_levels()
        self.assertFalse(top["health"]["valid"])
        self.assertEqual(top["health"]["reason"], "stale")
        self.assertIsNone(top["imbalance"])

    def test_never_updated_book_abstains(self):
        top = OrderBook().top_levels()
        self.assertFalse(top["health"]["valid"])
        self.assertEqual(top["health"]["reason"], "never_updated")


class TestBoundedQueue(unittest.TestCase):
    def _ev(self, source="binance"):
        return TradeEvent(meta=make_meta(source, "v1", None),
                          symbol="BTC/USD", price=1.0, size=1.0)

    def test_drop_oldest_is_counted_never_silent(self):
        q = BoundedEventQueue(maxsize=3, name="t")
        for _ in range(3):
            self.assertTrue(q.push(self._ev()))
        self.assertFalse(q.push(self._ev()))          # evicts the oldest
        s = q.stats()
        self.assertEqual(s["size"], 3)
        self.assertEqual(s["pushed"], 4)
        self.assertEqual(s["dropped_total"], 1)
        self.assertEqual(s["dropped"]["binance"], 1)

    def test_drops_attributed_per_source(self):
        q = BoundedEventQueue(maxsize=1, name="t")
        q.push(self._ev("binance"))
        q.push(self._ev("coinbase"))
        q.push(self._ev("coinbase"))
        self.assertEqual(q.stats()["dropped"], {"coinbase": 2})

    def test_drain_preserves_order_and_frees_space(self):
        q = BoundedEventQueue(maxsize=10, name="t")
        evs = [self._ev() for _ in range(5)]
        for e in evs:
            q.push(e)
        drained = q.drain(limit=3)
        self.assertEqual(drained, evs[:3])
        self.assertEqual(q.stats()["size"], 2)


class TestEventStore(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        self.dir = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(self.dir, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def _store(self):
        from lib.event_store import SQLiteEventStore
        return SQLiteEventStore()

    def test_append_read_roundtrip(self):
        store = self._store()
        ev = BookSnapshotEvent(
            meta=make_meta("binance", "binance_l2_v1", None),
            symbol="BTC", bids=((100.0, 1.0),), asks=((101.0, 2.0),),
            health_valid=True, health_reason="ok")
        n = store.append([event_to_dict(ev)])
        self.assertEqual(n, 1)
        rows = store.read("BTC", "book_snapshot", since_ts=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "binance")
        self.assertTrue(rows[0]["health_valid"])

    def test_bytes_by_day_measures_what_was_stored(self):
        """The §46 instrument: the migration decision reads this, so it
        must reflect actual serialized bytes, not estimates."""
        store = self._store()
        evs = [event_to_dict(TradeEvent(
            meta=make_meta("kraken", "v1", None),
            symbol="BTC/USD", price=50_000.0 + i, size=0.1))
            for i in range(10)]
        store.append(evs)
        days = store.bytes_by_day()
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["events"], 10)
        self.assertGreater(days[0]["bytes"], 100)
        self.assertEqual(days[0]["kind"], "trade")

    def test_summary_reports_file_and_payload_bytes(self):
        store = self._store()
        store.append([event_to_dict(self_ev) for self_ev in [
            TradeEvent(meta=make_meta("kraken", "v1", None),
                       symbol="ETH/USD", price=3000.0, size=1.0)]])
        s = store.summary()
        self.assertEqual(s["events"], 1)
        self.assertGreater(s["payload_bytes"], 0)

    def test_tiers_gate_persistence_cost(self):
        from lib.event_store import tier_of
        self.assertEqual(tier_of("BTC"), 1)
        self.assertEqual(tier_of("ETH/USD"), 1)
        self.assertEqual(tier_of("DOGE/USD"), 3)
        self.assertEqual(tier_of("NVDA"), 3)


class TestIsoParse(unittest.TestCase):
    def test_z_suffix_and_junk(self):
        from lib.market_events import parse_iso_ts
        ts = parse_iso_ts("2026-08-14T07:48:36.925533Z")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 1.7e9)
        self.assertIsNone(parse_iso_ts(None))
        self.assertIsNone(parse_iso_ts("not-a-time"))


class TestDrainAll(unittest.TestCase):
    def test_flusher_sees_every_registered_queue(self):
        from lib.market_events import drain_all, get_queue
        drain_all()  # start clean
        a = get_queue("t_drain_a", maxsize=10)
        b = get_queue("t_drain_b", maxsize=10)
        ev = TradeEvent(meta=make_meta("x", "v1", None),
                        symbol="BTC/USD", price=1.0, size=1.0)
        a.push(ev)
        b.push(ev)
        batch = drain_all()
        self.assertGreaterEqual(len(batch), 2)
        self.assertEqual(a.stats()["size"], 0)
        self.assertEqual(b.stats()["size"], 0)


class TestKrakenAdapter(unittest.TestCase):
    """Provider parsing stays in the adapter; what leaves is canonical."""

    def _drain(self, name):
        from lib.market_events import get_queue
        return get_queue(name).drain(limit=10_000)

    def test_tier3_symbol_stays_off_the_event_log(self):
        from lib.kraken_stream import _emit_trade_event
        self._drain("kraken_trades")
        _emit_trade_event("SOL/USD", {"price": 200.0, "qty": 1.0,
                                      "side": "buy",
                                      "timestamp": "2026-08-14T07:00:00Z"})
        self.assertEqual(self._drain("kraken_trades"), [])

    def test_trade_event_carries_venue_clock_and_skew(self):
        from lib.kraken_stream import _emit_trade_event
        self._drain("kraken_trades")
        _emit_trade_event("BTC/USD", {"price": 50_000.0, "qty": 0.25,
                                      "side": "sell",
                                      "timestamp": "2026-08-14T07:00:00Z"})
        evs = self._drain("kraken_trades")
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev.meta.source, "kraken")
        self.assertIsNotNone(ev.meta.exchange_ts)
        self.assertIsNotNone(ev.meta.clock_skew_ms)
        self.assertEqual(ev.side, "sell")
        self.assertEqual(ev.price, 50_000.0)

    def test_quotes_throttle_to_persist_cadence(self):
        from lib.kraken_stream import _emit_quote_event, _quote_marks
        self._drain("kraken_quotes")
        _quote_marks.pop("ETH/USD", None)
        q = {"bid": 3000.0, "ask": 3001.0, "bid_qty": 2.0, "ask_qty": 1.5}
        _emit_quote_event("ETH/USD", q)
        _emit_quote_event("ETH/USD", q)     # inside the interval — dropped
        evs = self._drain("kraken_quotes")
        self.assertEqual(len(evs), 1)
        # Ticker carries no venue time: None, never a fake zero.
        self.assertIsNone(evs[0].meta.exchange_ts)
        self.assertIsNone(evs[0].meta.clock_skew_ms)
        self.assertEqual(evs[0].bid_size, 2.0)


class TestDerivativesObservations(unittest.TestCase):
    """Funding/OI/long-short used to be fetched, served and discarded —
    emission rides the fetch path, throttled, tier-gated, venue-clocked."""

    def setUp(self):
        from lib.crypto_derivatives import _obs_marks
        _obs_marks.clear()
        self._drain()

    def _drain(self):
        from lib.market_events import get_queue
        return get_queue("derivatives_obs").drain(limit=10_000)

    def test_observation_carries_venue_clock_when_supplied(self):
        from lib.crypto_derivatives import _emit_observation
        _emit_observation("cryptocom", "BTC/USD", "funding_rate", 0.0001,
                          obs_iso="2026-08-14T07:00:00Z")
        evs = self._drain()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].metric, "funding_rate")
        self.assertEqual(evs[0].symbol, "BTC")
        self.assertIsNotNone(evs[0].meta.exchange_ts)

    def test_no_venue_clock_stores_none_not_fetch_time(self):
        from lib.crypto_derivatives import _emit_observation
        _emit_observation("okx", "ETH/USD", "open_interest_usd", 5e9)
        evs = self._drain()
        self.assertEqual(len(evs), 1)
        self.assertIsNone(evs[0].meta.exchange_ts)
        self.assertIsNone(evs[0].meta.clock_skew_ms)

    def test_hot_dashboard_cannot_multiply_slow_observations(self):
        from lib.crypto_derivatives import _emit_observation
        for _ in range(5):
            _emit_observation("okx", "BTC/USD", "funding_rate", 0.0002)
        self.assertEqual(len(self._drain()), 1)

    def test_tier3_and_missing_values_stay_off_the_log(self):
        from lib.crypto_derivatives import _emit_observation
        _emit_observation("okx", "DOGE/USD", "funding_rate", 0.001)
        _emit_observation("okx", "BTC/USD", "funding_rate", None)
        self.assertEqual(self._drain(), [])


class TestTdForexAdapter(unittest.TestCase):
    """TD price events -> canonical PriceTicks under desk =X identity,
    venue-clocked, throttled to the 30s persist cadence."""

    def setUp(self):
        from lib.td_forex_stream import _persist_marks, _ticks
        _persist_marks.clear()
        _ticks.clear()
        self._drain()

    def _drain(self):
        from lib.market_events import get_queue
        return get_queue("td_forex_ticks").drain(limit=10_000)

    def test_tick_lands_under_desk_identity_with_venue_clock(self):
        from lib.td_forex_stream import handle_price_event, latest_price
        handle_price_event({"event": "price", "symbol": "EUR/USD",
                            "price": 1.0842, "timestamp": 1755300000})
        evs = self._drain()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].symbol, "EURUSD=X")   # never TD's slash
        self.assertEqual(evs[0].price, 1.0842)
        self.assertEqual(evs[0].meta.exchange_ts, 1755300000.0)
        self.assertIsNotNone(evs[0].meta.clock_skew_ms)
        self.assertEqual(latest_price("EURUSD=X")["price"], 1.0842)

    def test_tick_firehose_throttles_but_memory_stays_fresh(self):
        from lib.td_forex_stream import handle_price_event, latest_price
        for i in range(20):
            handle_price_event({"event": "price", "symbol": "GBP/USD",
                                "price": 1.28 + i * 0.0001,
                                "timestamp": 1755300000 + i})
        self.assertEqual(len(self._drain()), 1)       # one persisted
        self.assertAlmostEqual(latest_price("GBPUSD=X")["price"], 1.2819)

    def test_unknown_symbol_and_junk_price_are_dropped(self):
        from lib.td_forex_stream import handle_price_event
        handle_price_event({"event": "price", "symbol": "USD/TRY",
                            "price": 33.1, "timestamp": 1})
        handle_price_event({"event": "price", "symbol": "EUR/USD",
                            "price": "n/a", "timestamp": 1})
        self.assertEqual(self._drain(), [])


class TestTierPersistenceHook(unittest.TestCase):
    def test_tier3_symbol_never_reaches_the_queue(self):
        from lib.market_events import get_queue
        from lib.orderbook_stream import _maybe_persist_snapshot
        q = get_queue("book_snapshots")
        before = q.stats()["pushed"]
        _maybe_persist_snapshot("binance", "DOGE", {"bids": [], "asks": [],
                                                    "health": {}}, None)
        self.assertEqual(q.stats()["pushed"], before)

    def test_tier1_snapshot_lands_health_stamped(self):
        from lib.market_events import get_queue
        from lib.orderbook_stream import _maybe_persist_snapshot, _persist_marks
        q = get_queue("book_snapshots")
        _persist_marks.pop("binance:BTC", None)
        before = q.stats()["pushed"]
        _maybe_persist_snapshot("binance", "BTC",
                                {"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]],
                                 "health": {"valid": True, "reason": "ok"}},
                                None)
        self.assertEqual(q.stats()["pushed"], before + 1)
        ev = q.drain(limit=10_000)[-1]
        self.assertTrue(ev.health_valid)
        self.assertEqual(ev.meta.source, "binance")


if __name__ == "__main__":
    unittest.main()
