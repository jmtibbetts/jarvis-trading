"""4C energy engine — the property that matters is point-in-time honesty:
a release the desk hadn't seen yet must be invisible at that asof, stale
sources must abstain, and the statistics must mean what their names say.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from lib.market_events import OfficialStat, event_to_dict, make_meta
from lib.sector_engine import (
    _change_z,
    _pctile,
    _seasonal_z,
    energy_snapshot,
)


def _stat(symbol, series, as_of, value, release_ts):
    return event_to_dict(OfficialStat(
        meta=make_meta("eia", "test_v1", release_ts),
        symbol=symbol, series=series, value=value, as_of=as_of,
        dedup_key=f"test:{series}:{symbol}:{as_of}"))


class StatsMathTests(unittest.TestCase):
    def test_seasonal_z_compares_same_week_across_years(self):
        # Five years of week-33s at 400, ±weeks at wild values that must
        # NOT contaminate the sample beyond the ±1-week window.
        hist = []
        for yr in range(2021, 2026):
            d = date.fromisocalendar(yr, 33, 5)
            hist.append((d, 400.0))
            hist.append((d - timedelta(weeks=1), 402.0))
            hist.append((d + timedelta(weeks=1), 398.0))
            hist.append((d - timedelta(weeks=8), 900.0))   # far-off season
        cur = (date.fromisocalendar(2026, 33, 5), 410.0)
        z = _seasonal_z(hist, cur)
        self.assertIsNotNone(z)
        self.assertGreater(z, 3.0)      # 410 vs a tight ~400 cluster

    def test_seasonal_z_refuses_thin_samples(self):
        hist = [(date(2025, 8, 15), 400.0)]
        self.assertIsNone(_seasonal_z(hist, (date(2026, 8, 14), 410.0)))

    def test_change_z_is_about_deltas_not_levels(self):
        # Alternating +4/+6 builds around a +5 latest: the level is at a
        # record but the CHANGE is dead average — z must be ~0.
        base = date(2024, 1, 5)
        hist, level = [], 100.0
        for i in range(60):
            hist.append((base + timedelta(weeks=i), level))
            level += 4.0 if i % 2 == 0 else 6.0
        hist.append((base + timedelta(weeks=60), hist[-1][1] + 5.0))
        latest, z = _change_z(hist)
        self.assertEqual(latest, 5.0)
        self.assertAlmostEqual(z, 0.0, places=1)

    def test_change_z_refuses_zero_variance(self):
        # Perfectly identical changes have no distribution to stand a
        # z-score on — None is the honest answer, not 0.0.
        base = date(2024, 1, 5)
        hist = [(base + timedelta(weeks=i), 100.0 + 5 * i) for i in range(60)]
        self.assertIsNone(_change_z(hist))

    def test_pctile_brackets_the_window(self):
        base = date(2024, 1, 5)
        hist = [(base + timedelta(weeks=i), float(i)) for i in range(52)]
        self.assertEqual(_pctile(hist), 100.0)      # latest is the max
        hist_low = hist[:-1] + [(hist[-1][0], -1.0)]
        self.assertLessEqual(_pctile(hist_low), 2.0)


class PointInTimeTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def _seed_weekly(self, weeks=8, series="eia_crude_stocks_kbbl",
                     symbol="CL=F", end=None):
        from lib.event_store import get_store
        end = end or datetime.now(timezone.utc)
        evs = []
        for i in range(weeks):
            d = (end - timedelta(weeks=weeks - 1 - i)).date()
            release = datetime.combine(d, datetime.min.time(),
                                       tzinfo=timezone.utc) + timedelta(days=5)
            # Alternating builds (+1050/+950) so delta variance exists —
            # a z-score against identical changes is honestly undefined.
            value = 400_000.0 + i * 1000 + (50 if i % 2 else 0)
            evs.append(_stat(symbol, series, d.isoformat(),
                             value, release.timestamp()))
        get_store().append(evs)

    def test_unreleased_data_is_invisible(self):
        """The stat released Friday must not exist on Wednesday — the
        crystal ball this entire discipline exists to confiscate."""
        self._seed_weekly(weeks=8)
        now = datetime.now(timezone.utc)
        latest_asof_visible = energy_snapshot(asof=now)[
            "instruments"]["crude"]["fundamentals"].get("as_of")
        # Rewind to before the newest release: its row must vanish.
        earlier = now - timedelta(days=6)
        earlier_view = energy_snapshot(asof=earlier)["instruments"]["crude"]["fundamentals"]
        if "as_of" in earlier_view:
            self.assertNotEqual(earlier_view["as_of"], latest_asof_visible)

    def test_stale_source_abstains(self):
        self._seed_weekly(weeks=8,
                          end=datetime.now(timezone.utc) - timedelta(days=40))
        block = energy_snapshot()["instruments"]["crude"]["fundamentals"]
        self.assertIn("abstain", block)

    def test_empty_store_abstains_everywhere(self):
        snap = energy_snapshot()["instruments"]
        self.assertIn("abstain", snap["crude"]["fundamentals"])
        self.assertIn("abstain", snap["crude"]["positioning"])
        self.assertIn("abstain", snap["crude"]["curve"])

    def test_fresh_data_produces_the_named_features(self):
        self._seed_weekly(weeks=12)
        block = energy_snapshot()["instruments"]["crude"]["fundamentals"]
        self.assertNotIn("abstain", block)
        # The NEWEST seeded week releases 5 days after its as_of — i.e.
        # in the future — so the visible latest is the PRIOR week (i=10:
        # 410,000). That off-by-one IS the release discipline working.
        self.assertEqual(block["level"], 410_000.0)
        self.assertEqual(block["wow_change"], 950.0)     # 410,000 - 409,050
        self.assertEqual(block["history_n"], 11)
        self.assertIsNotNone(block["change_z_3y"])


class SectorRegistryTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev

    def test_metals_and_index_serve_positioning_and_curve_only(self):
        from lib.sector_engine import sector_snapshot
        for sector, keys in (("metals", {"gold", "silver", "copper"}),
                             ("index", {"spx", "ndx"})):
            snap = sector_snapshot(sector)
            self.assertEqual(set(snap["instruments"]), keys)
            for block in snap["instruments"].values():
                # No fundamentals feed wired -> no fundamentals key. A
                # sector without one must look like it, never carry a proxy.
                self.assertNotIn("fundamentals", block)
                self.assertIn("positioning", block)
                self.assertIn("curve", block)

    def test_metals_positioning_flows_from_stored_cot(self):
        from lib.event_store import get_store
        from lib.sector_engine import sector_snapshot
        now = datetime.now(timezone.utc)
        evs = []
        for i in range(160):
            d = (now - timedelta(weeks=160 - i)).date()
            evs.append(_stat("GC=F", "cot_noncomm_net", d.isoformat(),
                             1000.0 * i, (now - timedelta(weeks=160 - i)
                                          ).timestamp()))
        get_store().append(evs)
        p = sector_snapshot("metals")["instruments"]["gold"]["positioning"]
        self.assertEqual(p["spec_net"], 159_000.0)
        self.assertEqual(p["spec_pctile_3y"], 100.0)   # monotone rise

    def test_unknown_sector_raises(self):
        from lib.sector_engine import sector_snapshot
        with self.assertRaises(KeyError):
            sector_snapshot("shipping")


if __name__ == "__main__":
    unittest.main()
