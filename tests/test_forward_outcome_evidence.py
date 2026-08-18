"""Evidence Phase B — forward outcome observer, shared range collector, and
the read-only market-data runtime that finally gives the perpetual book an
owner.

Every test here is HERMETIC. Samples are written directly or through a
stubbed snapshot; no websocket is opened and no provider is contacted.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from app.database import (DecisionObservation, DecisionOutcome,
                          InstrumentQuoteSample, get_db)
from lib import decision_outcome as DO
from lib import range_collector as RC
from lib.execution_snapshot import AVAILABLE, STALE, ExecutionMarketSnapshot

SPOT, PERP = "CRYPTO_SPOT", "CRYPTO_PERP"
KRAKEN, PERP_VENUE = "kraken", "kraken_derivatives_us"


def _snap(bid, ask, *, status=AVAILABLE, venue=KRAKEN, symbol="BTC/USD",
          product=SPOT):
    s = ExecutionMarketSnapshot(venue=venue, symbol=symbol, product=product,
                                bid=bid, ask=ask)
    s.status = status
    s.source = "test_twin"
    return s


def _clear():
    with get_db() as db:
        db.query(InstrumentQuoteSample).delete()
        db.query(DecisionOutcome).delete()
        db.query(DecisionObservation).delete()


def _obs(db, *, oid, side="long", product=SPOT, venue=KRAKEN,
         symbol="BTC/USD", decision="NO_TRADE", t0=None, timeframe="15m",
         bid=99.5, ask=100.5, stop=95.0, target=110.0, price=100.0,
         instrument_id=None):
    # A perpetual decision without its listed contract is now refused, so
    # perp fixtures must name one exactly as the live router does.
    if instrument_id is None and product == PERP:
        instrument_id = symbol
    row = DecisionObservation(
        observation_id=oid, symbol=symbol, asset_class="crypto",
        product=product, venue=venue, side=side, timeframe=timeframe,
        instrument_id=instrument_id,
        decision_at=(t0 or datetime.now(timezone.utc)).isoformat(),
        decision_price=price, bid=bid, ask=ask,
        intended_stop=stop, intended_target=target,
        final_decision=decision, binding_reason="EDGE_BELOW_THRESHOLD",
        binding_constraint="EDGE")
    db.add(row)
    db.flush()
    return row


def _outcome_row(oid, horizon="15m"):
    """Plain-dict copy of one outcome — ORM rows die with their session."""
    with get_db() as db:
        r = db.query(DecisionOutcome).filter(
            DecisionOutcome.observation_id == oid,
            DecisionOutcome.horizon == horizon).first()
        if r is None:
            return None
        return {c.name: getattr(r, c.name) for c in r.__table__.columns}


class _D(dict):
    """Attribute access over a plain dict, so assertions read naturally."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e


def _fill(symbol, product, venue, t0, prices, *, step_s=60,
          instrument_id=None):
    """Write a chronological series of (bid, ask) samples from t0."""
    if instrument_id is None and product == PERP:
        instrument_id = symbol
    for i, (b, a) in enumerate(prices):
        snap = _snap(b, a, symbol=symbol, product=product, venue=venue)
        snap.instrument_id = instrument_id
        RC.record_sample(symbol=symbol, product=product, venue=venue,
                         snap=snap, at=t0 + timedelta(seconds=i * step_s))


class HorizonSchedulingTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(hours=6)

    def test_no_trade_schedules_horizons(self):
        with get_db() as db:
            obs = _obs(db, oid="o-notrade")
            r = DO.schedule_for_observation(obs, db=db)
        self.assertEqual(r["scheduled"], 4)
        self.assertEqual(r["horizons"], ["15m", "30m", "1h", "2h"])

    def test_abstain_can_schedule(self):
        with get_db() as db:
            obs = _obs(db, oid="o-abstain", decision="ABSTAIN")
            r = DO.schedule_for_observation(obs, db=db)
        self.assertGreater(r["scheduled"], 0)

    def test_unparseable_side_gets_no_fake_direction(self):
        """The observation is kept; the DIRECTIONAL fields are not invented."""
        with get_db() as db:
            obs = _obs(db, oid="o-badside", side="¯\\_(ツ)_/¯")
            DO.schedule_for_observation(obs, db=db)
            rows = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-badside").all()
            self.assertTrue(rows)
            for r in rows:
                self.assertIsNone(r.side)

    def test_observation_horizon_pair_is_unique(self):
        with get_db() as db:
            obs = _obs(db, oid="o-dupe")
            DO.schedule_for_observation(obs, db=db)
        with get_db() as db:
            obs = db.query(DecisionObservation).filter(
                DecisionObservation.observation_id == "o-dupe").first()
            again = DO.schedule_for_observation(obs, db=db)
        self.assertEqual(again["scheduled"], 0)
        with get_db() as db:
            n = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-dupe").count()
        self.assertEqual(n, 4)

    def test_due_at_derives_from_decision_at_not_now(self):
        t0 = datetime.now(timezone.utc) - timedelta(days=3)
        with get_db() as db:
            obs = _obs(db, oid="o-due", t0=t0)
            DO.schedule_for_observation(obs, db=db)
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-due",
                DecisionOutcome.horizon == "15m").first()
            self.assertEqual(row.due_at, (t0 + timedelta(minutes=15)).isoformat())

    def test_horizon_policy_is_deterministic_per_timeframe(self):
        self.assertEqual(DO.horizons_for(timeframe="1d"), ("8h", "1d"))
        self.assertEqual(DO.horizons_for(timeframe="1m"),
                         ("1m", "5m", "15m", "30m"))
        # An unknown timeframe gets the neutral spread, never everything.
        self.assertEqual(DO.horizons_for(timeframe=None), DO.DEFAULT_HORIZONS)

    def test_expected_hold_adds_its_nearest_horizon(self):
        h = DO.horizons_for(timeframe="15m", expected_hold_hours=8.0)
        self.assertIn("8h", h)


class ReturnMathTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def _resolve(self, **kw):
        oid = kw.pop("oid")
        with get_db() as db:
            obs = _obs(db, oid=oid, t0=self.t0, **kw)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        return _D(_outcome_row(oid) or {})

    def test_midpoint_return_is_correct(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        r = self._resolve(oid="o-mid")
        self.assertAlmostEqual(r.midpoint_return_pct, 15.0, places=4)

    def test_long_direction_adjusted_return(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        r = self._resolve(oid="o-long", side="long")
        self.assertAlmostEqual(r.direction_adjusted_mid_return_pct, 15.0, places=4)

    def test_short_direction_adjusted_return_flips_sign(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        r = self._resolve(oid="o-short", side="short", stop=105.0, target=90.0)
        self.assertAlmostEqual(r.direction_adjusted_mid_return_pct, -15.0, places=4)

    def test_long_side_reference_uses_bid(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        r = self._resolve(oid="o-lbid", side="long")
        self.assertAlmostEqual(r.side_executable_reference, 114.5, places=6)

    def test_short_side_reference_uses_ask(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        r = self._resolve(oid="o-sask", side="short", stop=105.0, target=90.0)
        self.assertAlmostEqual(r.side_executable_reference, 115.5, places=6)

    def test_cheap_coin_percent_sanity(self):
        """0.043241 -> 0.044241 is +2.3126%, not 'a tiny $0.001 move'."""
        lo, hi = 0.043241, 0.044241
        _fill("PEPE/USD", SPOT, KRAKEN, self.t0,
              [(lo, lo), (hi, hi)] + [(hi, hi)] * 14)
        with get_db() as db:
            obs = _obs(db, oid="o-cheap", symbol="PEPE/USD", t0=self.t0,
                       bid=lo, ask=lo, price=lo, stop=0.04, target=0.05)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-cheap"))
        self.assertAlmostEqual(r.midpoint_return_pct, 2.3126, places=3)


class ExcursionTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def _resolve(self, oid, series, step_s=60, **kw):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, series, step_s=step_s)
        with get_db() as db:
            obs = _obs(db, oid=oid, t0=self.t0, **kw)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        return _D(_outcome_row(oid) or {})

    def test_long_mfe_and_mae_from_interval(self):
        # up to 110/111 then back down to 94/95
        series = [(99.5, 100.5), (110.0, 111.0), (94.0, 95.0)] + [(100.0, 101.0)] * 13
        r = self._resolve("o-mfe", series, side="long", stop=90.0, target=200.0)
        self.assertAlmostEqual(r.mfe_pct, 10.0, places=4)    # bid 110 vs ref 100
        self.assertAlmostEqual(r.mae_pct, -6.0, places=4)    # bid 94 vs ref 100

    def test_short_mfe_and_mae_are_measured_on_the_ask(self):
        series = [(99.5, 100.5), (89.0, 90.0), (114.0, 115.0)] + [(100.0, 101.0)] * 13
        r = self._resolve("o-smfe", series, side="short", stop=200.0, target=10.0)
        self.assertAlmostEqual(r.mfe_pct, 10.0, places=4)    # ask 90 vs ref 100
        self.assertAlmostEqual(r.mae_pct, -15.0, places=4)   # ask 115 vs ref 100

    def test_mfe_r_uses_the_original_stop_distance(self):
        series = [(99.5, 100.5), (110.0, 111.0)] + [(100.0, 101.0)] * 14
        r = self._resolve("o-mfer", series, side="long", stop=95.0, target=200.0)
        self.assertAlmostEqual(r.mfe_r, 2.0, places=4)       # 10% of 100 / 5

    def test_endpoint_only_evidence_cannot_produce_mfe(self):
        """THE HARD INVARIANT: one sample is not a range, and NULL != 0."""
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, [(99.5, 100.5)])
        with get_db() as db:
            obs = _obs(db, oid="o-endpoint", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-endpoint"))
        self.assertIsNone(r.mfe_pct)
        self.assertIsNone(r.mae_pct)
        self.assertEqual(r.range_quality, RC.INSUFFICIENT_RANGE_DATA)

    def test_missing_excursion_is_null_never_zero(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, [(99.5, 100.5)])
        with get_db() as db:
            obs = _obs(db, oid="o-null", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-null"))
        self.assertNotEqual(r.mfe_pct, 0.0)
        self.assertIsNone(r.mfe_pct)


class TouchOrderTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def _resolve(self, oid, series, step_s=60, **kw):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, series, step_s=step_s)
        with get_db() as db:
            obs = _obs(db, oid=oid, t0=self.t0, **kw)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        return _D(_outcome_row(oid) or {})

    def test_target_first(self):
        # step_s under the heartbeat: continuously observed, so the
        # ordering is genuinely provable.
        series = [(100.0, 101.0), (111.0, 112.0), (94.0, 95.0)] + [(100.0, 101.0)] * 13
        r = self._resolve("o-tf", series, side="long", stop=95.0, target=110.0,
                          step_s=5)
        self.assertEqual(r.touch_order, DO.TARGET_FIRST)
        self.assertTrue(r.target_touched)
        self.assertIsNotNone(r.target_first_seen_at)

    def test_stop_first(self):
        series = [(100.0, 101.0), (94.0, 95.0), (111.0, 112.0)] + [(100.0, 101.0)] * 13
        r = self._resolve("o-sf", series, side="long", stop=95.0, target=110.0,
                          step_s=5)
        self.assertEqual(r.touch_order, DO.STOP_FIRST)

    def test_touch_after_a_blind_interval_is_ambiguous_never_favourable(self):
        """A hole before the first crossing hides which level came first."""
        # One sample between the levels, then a long blind gap, then both
        # levels crossed. What happened inside the hole is unknown.
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, [(100.0, 101.0)])
        blind = self.t0 + timedelta(seconds=RC.HEARTBEAT_S * 4)
        _fill("BTC/USD", SPOT, KRAKEN, blind,
              [(94.0, 95.0), (111.0, 112.0)] + [(100.0, 101.0)] * 10,
              step_s=5)
        with get_db() as db:
            obs = _obs(db, oid="o-amb", t0=self.t0, side="long",
                       stop=95.0, target=110.0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-amb"))
        self.assertEqual(r.touch_order, DO.TOUCH_AMBIGUOUS)
        self.assertEqual(r.status, DO.AMBIGUOUS_INTRABAR)

    def test_continuous_observation_orders_the_touches(self):
        """With no blind interval the earlier crossing genuinely came first."""
        series = [(100.0, 101.0), (94.0, 95.0), (111.0, 112.0)] + [(100.0, 101.0)] * 13
        r = self._resolve("o-order", series, side="long", stop=95.0,
                          target=110.0, step_s=5)
        self.assertEqual(r.touch_order, DO.STOP_FIRST)

    def test_neither_touched(self):
        series = [(100.0, 101.0)] * 16
        r = self._resolve("o-neither", series, side="long", stop=95.0, target=110.0)
        self.assertEqual(r.touch_order, DO.NEITHER)
        self.assertFalse(r.target_touched)
        self.assertFalse(r.stop_touched)


class RangeQualityTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def test_quiet_but_connected_is_not_a_gap(self):
        """A calm market on a healthy feed keeps heartbeating."""
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(100.0, 101.0)] * 16, step_s=RC.HEARTBEAT_S)
        ev = RC.range_over(symbol="BTC/USD", product=SPOT, venue=KRAKEN,
                           start=self.t0, end=self.t0 + timedelta(minutes=7))
        self.assertNotEqual(ev.quality, RC.GAP_PRESENT)
        self.assertGreater(ev.sample_count, 2)

    def test_provider_downtime_degrades_quality(self):
        # samples, then an 8-minute hole, then samples again
        _fill("BTC/USD", SPOT, KRAKEN, self.t0, [(100.0 + i, 101.0 + i)
                                                 for i in range(3)], step_s=30)
        later = self.t0 + timedelta(minutes=9)
        _fill("BTC/USD", SPOT, KRAKEN, later, [(100.0 + i, 101.0 + i)
                                               for i in range(3)], step_s=30)
        ev = RC.range_over(symbol="BTC/USD", product=SPOT, venue=KRAKEN,
                           start=self.t0, end=self.t0 + timedelta(minutes=12))
        self.assertEqual(ev.quality, RC.GAP_PRESENT)
        self.assertGreater(ev.max_sample_gap_s, RC.GAP_THRESHOLD_S)

    def test_restart_cannot_claim_continuous_coverage(self):
        """Late start and early stop are both visible at the edges."""
        start = self.t0
        _fill("BTC/USD", SPOT, KRAKEN, start + timedelta(minutes=5),
              [(100.0, 101.0), (100.5, 101.5)], step_s=30)
        ev = RC.range_over(symbol="BTC/USD", product=SPOT, venue=KRAKEN,
                           start=start, end=start + timedelta(minutes=15))
        self.assertEqual(ev.quality, RC.GAP_PRESENT)

    def test_change_triggered_sampling_dedupes_an_unchanged_book(self):
        at = self.t0
        self.assertTrue(RC.record_sample(symbol="BTC/USD", product=SPOT,
                                         venue=KRAKEN, snap=_snap(100.0, 101.0),
                                         at=at))
        # identical quote one second later: nothing new to record
        self.assertFalse(RC.record_sample(symbol="BTC/USD", product=SPOT,
                                          venue=KRAKEN, snap=_snap(100.0, 101.0),
                                          at=at + timedelta(seconds=1)))
        # a moved book always records
        self.assertTrue(RC.record_sample(symbol="BTC/USD", product=SPOT,
                                         venue=KRAKEN, snap=_snap(100.5, 101.5),
                                         at=at + timedelta(seconds=2)))

    def test_unfillable_snapshot_is_never_recorded_as_a_price(self):
        self.assertFalse(RC.record_sample(
            symbol="BTC/USD", product=SPOT, venue=KRAKEN,
            snap=_snap(100.0, 101.0, status=STALE), at=self.t0))
        with get_db() as db:
            self.assertEqual(db.query(InstrumentQuoteSample).count(), 0)


class SharedCollectionTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def test_one_stream_serves_many_decisions(self):
        """200 pending BTC observations must collapse to ONE instrument."""
        with get_db() as db:
            for i in range(200):
                obs = _obs(db, oid=f"o-shared-{i}", t0=self.t0)
                DO.schedule_for_observation(obs, db=db)
        pending = RC.instruments_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["symbol"], "BTC/USD")

    def test_shared_samples_serve_every_overlapping_observation(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        with get_db() as db:
            for i in range(3):
                obs = _obs(db, oid=f"o-many-{i}", t0=self.t0)
                DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        with get_db() as db:
            rows = db.query(DecisionOutcome).filter(
                DecisionOutcome.horizon == "15m",
                DecisionOutcome.status == DO.COMPLETE).all()
            self.assertEqual(len(rows), 3)
            # one market, one answer
            self.assertEqual(len({r.mfe_pct for r in rows}), 1)
        with get_db() as db:
            self.assertEqual(db.query(InstrumentQuoteSample).count(), 16)

    def test_storage_projection_favours_shared_collection(self):
        p = RC.storage_projection(instruments=20, decisions_per_day=600)
        self.assertGreater(p["ratio_naive_over_shared"], 1.0)


class ProductAuthorityTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def test_perp_never_reads_spot_evidence(self):
        """Spot samples exist; a PERP outcome must NOT consume them."""
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        with get_db() as db:
            obs = _obs(db, oid="o-perp", product=PERP, venue=PERP_VENUE,
                       t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-perp"))
        self.assertEqual(r.status, DO.INSUFFICIENT_DATA)
        self.assertIsNone(r.midpoint_return_pct)
        self.assertIsNone(r.mfe_pct)

    def test_perp_uses_its_own_evidence_when_it_exists(self):
        _fill("PBTCUCZ50", PERP, PERP_VENUE, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        with get_db() as db:
            obs = _obs(db, oid="o-perp-ok", symbol="PBTCUCZ50", product=PERP,
                       venue=PERP_VENUE, t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-perp-ok"))
        self.assertEqual(r.status, DO.COMPLETE)
        self.assertAlmostEqual(r.midpoint_return_pct, 15.0, places=4)

    def test_unsupported_product_is_insufficient_data(self):
        with get_db() as db:
            obs = _obs(db, oid="o-fut", symbol="ES=F", product="FUTURES",
                       venue="none", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        r = _D(_outcome_row("o-fut"))
        self.assertEqual(r.status, DO.INSUFFICIENT_DATA)

    def test_collector_refuses_a_product_its_venue_cannot_price(self):
        """The spot socket must not be sampled as perpetual evidence."""
        r = RC.collect_once([{"symbol": "BTC/USD", "product": PERP,
                              "venue": KRAKEN}])
        self.assertEqual(r["recorded"], 0)
        self.assertEqual(r["refused"], 1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def test_observer_retry_is_idempotent(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        with get_db() as db:
            obs = _obs(db, oid="o-idem", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        first = DO.resolve_due()
        second = DO.resolve_due()
        # 15m and 30m are both due 40 minutes on; 1h and 2h are not.
        self.assertEqual(first["resolved"], 2)
        self.assertEqual(second["resolved"], 0)      # the retry does nothing

    def test_state_machine_is_monotonic(self):
        with get_db() as db:
            obs = _obs(db, oid="o-mono", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-mono").first()
            row.status = DO.COMPLETE
            db.flush()
            # a terminal row refuses a second finalisation outright
            self.assertFalse(DO.finalize(row, {"status": DO.EXPIRED}))
            self.assertEqual(row.status, DO.COMPLETE)

    def test_finalize_refuses_a_non_terminal_status(self):
        with get_db() as db:
            obs = _obs(db, oid="o-nonterm", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
            row = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-nonterm").first()
            with self.assertRaises(ValueError):
                DO.finalize(row, {"status": DO.PENDING})

    def test_due_query_uses_the_index(self):
        from sqlalchemy import text

        from app.database import engine
        with engine.connect() as c:
            plan = c.execute(text(
                "EXPLAIN QUERY PLAN SELECT id FROM decision_observation_outcomes "
                "WHERE status = 'PENDING' AND due_at <= '2026-01-01'")).fetchall()
        self.assertIn("ix_decision_outcome_due", " ".join(str(r) for r in plan))

    def test_not_yet_due_horizons_are_left_alone(self):
        t0 = datetime.now(timezone.utc)
        with get_db() as db:
            obs = _obs(db, oid="o-future", t0=t0)
            DO.schedule_for_observation(obs, db=db)
        DO.resolve_due()
        with get_db() as db:
            rows = db.query(DecisionOutcome).filter(
                DecisionOutcome.observation_id == "o-future").all()
            self.assertTrue(all(r.status == DO.PENDING for r in rows))


class NoPortfolioMutationTests(unittest.TestCase):
    """The observer collects evidence. It must not be able to trade."""

    def setUp(self):
        _clear()
        self.t0 = datetime.now(timezone.utc) - timedelta(minutes=40)

    def _counts(self):
        from app.database import (PaperPortfolio, PaperPosition, PaperTrade,
                                  TradeOutcome, get_db)
        with get_db() as db:
            pf = db.query(PaperPortfolio).first()
            return {
                "positions": db.query(PaperPosition).count(),
                "trades": db.query(PaperTrade).count(),
                "outcomes": db.query(TradeOutcome).count(),
                "cash": getattr(pf, "cash_balance", None),
                "total_trades": getattr(pf, "total_trades", None),
            }

    def test_observer_mutates_no_portfolio_state(self):
        _fill("BTC/USD", SPOT, KRAKEN, self.t0,
              [(99.5 + i, 100.5 + i) for i in range(16)])
        with get_db() as db:
            obs = _obs(db, oid="o-nomutate", t0=self.t0)
            DO.schedule_for_observation(obs, db=db)
        before = self._counts()
        DO.resolve_due()
        after = self._counts()
        self.assertEqual(before, after)

    def test_observer_module_imports_no_execution_surface(self):
        """AST, not prose: the observer cannot reach the trading engine."""
        import ast
        import pathlib
        src = pathlib.Path("lib/decision_outcome.py").read_text()
        banned = {"paper_engine", "virtual_orders", "execution_venue",
                  "canonical_entry", "paper_settlement", "learning_engine"}
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                found |= {b for b in banned if b in node.module}
            elif isinstance(node, ast.Import):
                for a in node.names:
                    found |= {b for b in banned if b in a.name}
        self.assertEqual(found, set())

    def test_rejected_evidence_is_not_execution_calibration_eligible(self):
        from lib.decision_observation import is_execution_calibration_eligible
        with get_db() as db:
            obs = _obs(db, oid="o-notcal")
            obs.source = "FORWARD_REJECTED_OBSERVATION"
            db.flush()
            self.assertFalse(is_execution_calibration_eligible(obs))


class MarketDataRuntimeTests(unittest.TestCase):
    """B1-B3: read-only feeds are owned, and owned SEPARATELY from trading."""

    def test_market_data_is_not_gated_on_the_trading_scheduler(self):
        import os
        from lib import market_data_runtime as MDR
        # The scheduler is disabled throughout the suite...
        self.assertEqual(os.environ.get("JARVIS_DISABLE_SCHEDULER"), "1")
        # ...and that alone must not decide whether feeds may run. Proven
        # by BEHAVIOUR, not by grepping the source: with the scheduler off
        # and only the market-data switch flipped, the answer changes.
        prior = os.environ.pop("JARVIS_DISABLE_MARKET_DATA", None)
        try:
            self.assertTrue(MDR.market_data_enabled())
        finally:
            if prior is not None:
                os.environ["JARVIS_DISABLE_MARKET_DATA"] = prior

    def test_market_data_has_its_own_named_switch(self):
        import os
        from lib import market_data_runtime as MDR
        self.assertEqual(MDR.DISABLE_ENV, "JARVIS_DISABLE_MARKET_DATA")
        # conftest forces it shut so hermetic CI opens no socket.
        self.assertEqual(os.environ.get("JARVIS_DISABLE_MARKET_DATA"), "1")
        self.assertFalse(MDR.market_data_enabled())

    def test_disabled_runtime_starts_no_feed(self):
        from lib import market_data_runtime as MDR
        r = MDR.start()
        self.assertFalse(r["enabled"])
        self.assertEqual(r["started"], {})

    def test_repeated_start_creates_no_duplicate_stream(self):
        from lib import bitnomial_market_data as MD
        MD.stop_stream()
        try:
            first = MD.start_stream(["PBTCUCZ50"], url="ws://127.0.0.1:9/none")
            second = MD.start_stream(["PBTCUCZ50"], url="ws://127.0.0.1:9/none")
            self.assertTrue(first)
            self.assertFalse(second)      # idempotent, not a second socket
        finally:
            MD.stop_stream()

    def test_stop_returns_the_service_to_a_restartable_state(self):
        from lib import bitnomial_market_data as MD
        MD.stop_stream()
        MD.start_stream(["PBTCUCZ50"], url="ws://127.0.0.1:9/none")
        self.assertTrue(MD.stop_stream())
        self.assertFalse(MD.stop_stream())          # idempotent
        self.assertTrue(MD.start_stream(["PBTCUCZ50"], url="ws://127.0.0.1:9/none"))
        MD.stop_stream()

    def test_health_reports_the_fields_ops_needs(self):
        from lib import bitnomial_market_data as MD
        h = MD.stream_health()
        for k in ("service_running", "connected", "subscribed",
                  "products_subscribed", "last_message_at", "reconnect_count",
                  "current_error", "desynced_products", "stale_products"):
            self.assertIn(k, h)

    def test_a_disconnect_invalidates_every_book(self):
        """A book that survived a gap is not a slightly older book."""
        from lib import bitnomial_market_data as MD
        MD.reset_books()
        b = MD.book_for("PBTCUCZ50", create=True)
        MD.reset_books()
        top = b.top()
        self.assertNotEqual(top and top.get("state"), MD.BOOK_OK)

    def test_runtime_exposes_no_order_or_account_surface(self):
        """Structurally read-only, proven by AST rather than promised."""
        import ast
        import pathlib
        for path in ("lib/market_data_runtime.py", "lib/bitnomial_market_data.py"):
            src = pathlib.Path(path).read_text()
            tree = ast.parse(src)
            names = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)}
            for banned in ("place_order", "submit_order", "cancel_order",
                           "amend_order", "withdraw", "transfer"):
                self.assertNotIn(banned, names, f"{path} exposes {banned}")
            self.assertNotIn("private", src.lower().split("wss://")[-1][:200])


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        _clear()

    def test_reference_basis_is_recorded(self):
        t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
        with get_db() as db:
            obs = _obs(db, oid="o-prov", t0=t0)
            DO.schedule_for_observation(obs, db=db)
        row = _D(_outcome_row("o-prov"))
        prov = json.loads(row.provenance)
        self.assertEqual(prov["reference_basis"], "T0_MIDPOINT")
        self.assertEqual(row.observer_version, DO.OUTCOME_OBSERVER_VERSION)

    def test_reference_falls_back_to_decision_price_when_one_sided(self):
        t0 = datetime.now(timezone.utc) - timedelta(minutes=40)
        with get_db() as db:
            obs = _obs(db, oid="o-prov2", t0=t0, bid=None, ask=None, price=100.0)
            DO.schedule_for_observation(obs, db=db)
        row = _D(_outcome_row("o-prov2"))
        self.assertEqual(json.loads(row.provenance)["reference_basis"],
                         "T0_DECISION_PRICE")
        self.assertEqual(row.reference_price, 100.0)


if __name__ == "__main__":
    unittest.main()
