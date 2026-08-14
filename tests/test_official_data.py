"""Phase 4A — official data adapters, judged on the properties that keep
replay honest: as_of never masquerades as release time, re-syncs are one
row, and parsers fail closed on malformed rows.

Parser fixtures are the REAL shapes captured live 2026-08-14 (CFTC
Socrata row, FINRA pipe file) — not invented approximations.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from lib.official_data import (
    COT_MARKETS,
    cot_release_ts,
    parse_cot_row,
    parse_finra_file,
)

# Trimmed from a live Socrata response, values intact.
COT_ROW = {
    "cftc_contract_market_code": "133741",
    "report_date_as_yyyy_mm_dd": "2026-08-04T00:00:00.000",
    "noncomm_positions_long_all": "20000",
    "noncomm_positions_short_all": "24000",
    "comm_positions_long_all": "9000",
    "comm_positions_short_all": "5500",
    "nonrept_positions_long_all": "3000",
    "nonrept_positions_short_all": "2500",
    "open_interest_all": "32000",
}

FINRA_FILE = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20260813|A|261283.883609|4800|445540.575669|B,Q,N
20260813|NVDA|1000000|0|2500000|B,Q,N
20260813|QQQ|400000|0|1000000|B,Q,N
20260813|BAD|not_a_number|0|100|Q
20260813|ZERO|5|0|0|Q
"""


class CotTests(unittest.TestCase):
    def test_nets_computed_per_cohort_and_mapped_to_desk_identity(self):
        stats = {s["series"]: s for s in parse_cot_row(COT_ROW)}
        self.assertEqual(stats["cot_noncomm_net"]["symbol"], "BTC/USD")
        self.assertEqual(stats["cot_noncomm_net"]["value"], -4000.0)
        self.assertEqual(stats["cot_comm_net"]["value"], 3500.0)
        self.assertEqual(stats["cot_nonrept_net"]["value"], 500.0)
        self.assertEqual(stats["cot_open_interest"]["value"], 32000.0)
        self.assertEqual(stats["cot_noncomm_net"]["as_of"], "2026-08-04")

    def test_untracked_market_yields_nothing(self):
        row = dict(COT_ROW, cftc_contract_market_code="999999")
        self.assertEqual(parse_cot_row(row), [])

    def test_malformed_position_drops_that_series_only(self):
        row = dict(COT_ROW, comm_positions_long_all="garbage")
        series = {s["series"] for s in parse_cot_row(row)}
        self.assertNotIn("cot_comm_net", series)
        self.assertIn("cot_noncomm_net", series)

    def test_release_is_the_friday_after_the_tuesday(self):
        # 2026-08-04 is a Tuesday; release Friday 2026-08-07 19:30 UTC.
        ts = cot_release_ts("2026-08-04")
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.assertEqual((d.year, d.month, d.day, d.hour, d.minute),
                         (2026, 8, 7, 19, 30))
        # The three-day gap IS the point: as_of joined directly would be a
        # crystal ball. Release must be strictly later.
        as_of_ts = datetime(2026, 8, 4, tzinfo=timezone.utc).timestamp()
        self.assertGreater(ts, as_of_ts)

    def test_all_tracked_markets_resolve_to_desk_symbols(self):
        from lib.instruments import asset_class_of
        for code, sym in COT_MARKETS.items():
            self.assertIn(asset_class_of(sym), ("Futures", "Crypto"))


class FinraTests(unittest.TestCase):
    def test_ratio_and_volume_for_universe_symbols_only(self):
        stats = parse_finra_file(FINRA_FILE, {"NVDA", "QQQ"})
        by_key = {(s["symbol"], s["series"]): s for s in stats}
        self.assertEqual(by_key[("NVDA", "finra_short_ratio")]["value"], 0.4)
        self.assertEqual(by_key[("QQQ", "finra_short_ratio")]["value"], 0.4)
        self.assertEqual(by_key[("NVDA", "finra_total_volume")]["value"], 2500000.0)
        self.assertEqual(by_key[("NVDA", "finra_short_ratio")]["as_of"], "2026-08-13")
        self.assertNotIn(("A", "finra_short_ratio"), by_key)

    def test_malformed_and_zero_volume_rows_fail_closed(self):
        stats = parse_finra_file(FINRA_FILE, {"BAD", "ZERO"})
        self.assertEqual(stats, [])


class DedupTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def test_resync_of_the_same_release_is_one_row(self):
        from lib.event_store import SQLiteEventStore
        from lib.market_events import OfficialStat, event_to_dict, make_meta

        store = SQLiteEventStore()
        ev = event_to_dict(OfficialStat(
            meta=make_meta("cftc", "cot_legacy_socrata_v1",
                           cot_release_ts("2026-08-04")),
            symbol="GC=F", series="cot_noncomm_net", value=1234.0,
            as_of="2026-08-04",
            dedup_key="cftc:cot_noncomm_net:GC=F:2026-08-04"))
        self.assertEqual(store.append([ev]), 1)
        # The overlap the 6-hour poll guarantees: same report, next run.
        self.assertEqual(store.append([ev]), 0)
        rows = store.read("GC=F", "official_stat", since_ts=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 1234.0)

    def test_stream_events_without_keys_never_collide(self):
        from lib.event_store import SQLiteEventStore
        from lib.market_events import TradeEvent, event_to_dict, make_meta

        store = SQLiteEventStore()
        evs = [event_to_dict(TradeEvent(
            meta=make_meta("kraken", "v1", None),
            symbol="BTC/USD", price=1.0, size=1.0)) for _ in range(3)]
        self.assertEqual(store.append(evs), 3)


if __name__ == "__main__":
    unittest.main()
