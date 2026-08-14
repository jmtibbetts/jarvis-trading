"""4C shadow features — context at the moment of judgment.

Pinned properties: context is release-filtered (a report not yet public
is invisible), absent feeds are absent keys (never zeros), an empty
context stores NULL rather than an empty shell, and the candidate row
carries the context it was born under.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from lib.market_events import (
    DerivativesObservation,
    OfficialStat,
    event_to_dict,
    make_meta,
)


def _stat(symbol, series, as_of, value, release_ts, source="cftc"):
    return event_to_dict(OfficialStat(
        meta=make_meta(source, "test_v1", release_ts),
        symbol=symbol, series=series, value=value, as_of=as_of,
        dedup_key=f"t:{series}:{symbol}:{as_of}"))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")
        from lib.candidate_context import _cache
        _cache.clear()

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def _seed_cot(self, symbol="BTC/USD", weeks=160):
        from lib.event_store import get_store
        now = datetime.now(timezone.utc)
        get_store().append([
            _stat(symbol, "cot_noncomm_net",
                  (now - timedelta(weeks=weeks - i)).date().isoformat(),
                  100.0 * i, (now - timedelta(weeks=weeks - i)).timestamp())
            for i in range(weeks)])

    def test_empty_store_yields_none_not_empty_shell(self):
        from lib.candidate_context import build_context
        self.assertIsNone(build_context("BTC/USD"))
        self.assertIsNone(build_context("NVDA"))

    def test_crypto_context_carries_cot_and_schema(self):
        from lib.candidate_context import CONTEXT_SCHEMA_VERSION, build_context
        self._seed_cot()
        ctx = build_context("BTC/USD")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["schema"], CONTEXT_SCHEMA_VERSION)
        self.assertEqual(ctx["cot_spec_pctile_3y"], 100.0)
        # No derivatives feed seeded -> no funding key. Absent is absent.
        self.assertNotIn("funding_rate", ctx)

    def test_unreleased_report_is_invisible(self):
        from lib.candidate_context import build_context
        from lib.event_store import get_store
        now = datetime.now(timezone.utc)
        # Released history plus one row whose release is in the future.
        self._seed_cot(weeks=20)
        get_store().append([_stat(
            "BTC/USD", "cot_noncomm_net", now.date().isoformat(),
            999_999.0, (now + timedelta(days=2)).timestamp())])
        ctx = build_context("BTC/USD")
        self.assertNotEqual(ctx.get("cot_spec_net"), 999_999.0)

    def test_stale_derivatives_are_dropped_not_served(self):
        from lib.candidate_context import build_context
        from lib.event_store import get_store
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        ev = event_to_dict(DerivativesObservation(
            meta=make_meta("okx", "test_v1", None),
            symbol="BTC", metric="funding_rate", value=0.0001))
        ev["ingest_ts"] = old.timestamp()
        get_store().append([ev])
        self.assertIsNone(build_context("BTC/USD"))

    def test_fresh_derivatives_flow_into_crypto_context(self):
        from lib.candidate_context import build_context
        from lib.event_store import get_store
        evs = []
        for metric, value in (("funding_rate", 0.0002),
                              ("open_interest_usd", 2.1e9),
                              ("long_short_ratio", 2.4)):
            evs.append(event_to_dict(DerivativesObservation(
                meta=make_meta("okx", "test_v1", None),
                symbol="BTC", metric=metric, value=value)))
        get_store().append(evs)
        ctx = build_context("BTC/USD")
        self.assertEqual(ctx["funding_rate"], 0.0002)
        self.assertEqual(ctx["oi_usd"], 2.1e9)
        self.assertEqual(ctx["long_short_ratio"], 2.4)

    def test_equity_context_is_finra_shaped(self):
        from lib.candidate_context import build_context
        from lib.event_store import get_store
        now = datetime.now(timezone.utc)
        get_store().append([_stat(
            "NVDA", "finra_short_ratio", now.date().isoformat(), 0.41,
            now.timestamp() - 3600, source="finra")])
        ctx = build_context("NVDA")
        self.assertEqual(ctx["finra_short_ratio"], 0.41)
        self.assertNotIn("cot_spec_pctile_3y", ctx)

    def test_candidate_row_carries_context_at_birth(self):
        from app.database import CandidateSignal, get_db, init_db
        from lib.candidates import record_candidate
        init_db()
        self._seed_cot()
        scored = {"asset_symbol": "BTC/USD", "timeframe": "4H",
                  "direction": "Long", "entry_price": 50_000.0,
                  "stop_loss": 49_000.0, "target_price": 53_000.0,
                  "composite_score": 61.0,
                  "score_breakdown": {"ta_confluence": 70, "volatility": 40,
                                      "conflict_ratio": 0.3}}
        with get_db() as db:
            row = record_candidate(db, scored, "persisted",
                                   signal_id="TEST-CTX-1")
            db.commit()
            rid = row.id
        try:
            with get_db() as db:
                stored = db.query(CandidateSignal).get(rid)
                ctx = json.loads(stored.market_context)
                self.assertEqual(ctx["cot_spec_pctile_3y"], 100.0)
        finally:
            with get_db() as db:
                db.query(CandidateSignal).filter(
                    CandidateSignal.id == rid).delete()
                db.commit()


if __name__ == "__main__":
    unittest.main()
