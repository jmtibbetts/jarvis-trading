"""The on-chain panel's endpoint — what it says when the data is bad.

lib.onchain.latest_context returns an empty dict both when a series is
stale past its 4-day tolerance and when it was never synced. That is the
right call for a join key — a week-old reading must not masquerade as
today's network state — and the wrong one for an ops panel, where "we
have no data" and "we have data we refuse to use" need different fixes.

These pin the distinction, and the discipline that survives it: the level
is never reported without the percentile against the asset's own history,
because MVRV 2.4 is euphoric for one asset and ordinary for another.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from lib.market_events import OfficialStat, event_to_dict, make_meta


def _stat(symbol, series, as_of, value, release_ts):
    return event_to_dict(OfficialStat(
        meta=make_meta("coinmetrics", "test_v1", release_ts),
        symbol=symbol, series=series, value=value, as_of=as_of,
        dedup_key=f"t:{series}:{symbol}:{as_of}"))


class OnChainEndpointTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("JARVIS_EVENTS_DB_PATH")
        d = tempfile.mkdtemp(prefix="jarvis-test-events-")
        os.environ["JARVIS_EVENTS_DB_PATH"] = os.path.join(d, "ev.db")

    def tearDown(self):
        if self._prev is not None:
            os.environ["JARVIS_EVENTS_DB_PATH"] = self._prev
        else:
            os.environ.pop("JARVIS_EVENTS_DB_PATH", None)

    def _seed(self, symbol="BTC/USD", days=400, ends_days_ago=1):
        """A daily MVRV series whose most recent point is `ends_days_ago`
        old — the knob these tests turn."""
        from lib.event_store import get_store
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(days):
            d = now - timedelta(days=ends_days_ago + (days - 1 - i))
            rows.append(_stat(symbol, "cm_CapMVRVCur", d.date().isoformat(),
                              1.0 + i * 0.001, d.timestamp()))
        get_store().append(rows)

    def _asset(self, out, symbol="BTC/USD"):
        return next(a for a in out["assets"] if a["symbol"] == symbol)

    def test_no_data_says_never_synced_not_silence(self):
        from app.routers.intel import onchain_context
        a = self._asset(onchain_context())
        self.assertEqual(a["state"], "never_synced")
        self.assertIn("detail", a)
        self.assertNotIn("mvrv", a)

    def test_fresh_data_reports_level_and_percentile(self):
        from app.routers.intel import onchain_context
        self._seed(ends_days_ago=1)
        a = self._asset(onchain_context())
        self.assertEqual(a["state"], "fresh")
        self.assertIn("mvrv", a)
        self.assertIsNotNone(a["mvrv_pctile_2y"],
                             "the level alone is not the gauge")
        self.assertEqual(a["mvrv_age_days"], 1)
        self.assertTrue(a["joined"], "fresh data must reach the join")

    def test_stale_data_is_labelled_stale_and_not_joined(self):
        """Past the 4-day tolerance the panel still SHOWS the number — an
        operator needs to see what went stale — but says plainly that
        nothing is being joined against it."""
        from app.routers.intel import onchain_context
        self._seed(ends_days_ago=9)
        a = self._asset(onchain_context())
        self.assertEqual(a["state"], "stale")
        self.assertEqual(a["mvrv_age_days"], 9)
        self.assertIn("mvrv", a, "a stale reading is still worth seeing")
        self.assertFalse(a["joined"],
                         "stale data must not reach the candidate join")

    def test_the_stale_boundary_matches_the_join(self):
        """The panel's label and the join's tolerance must be the same
        number — a panel that says 'fresh' about data the join discards is
        worse than no panel."""
        from app.routers.intel import onchain_context
        for age, expected in ((4, "fresh"), (5, "stale")):
            with self.subTest(age_days=age):
                self.tearDown()
                self.setUp()
                self._seed(ends_days_ago=age)
                a = self._asset(onchain_context())
                self.assertEqual(a["state"], expected)
                self.assertEqual(a["joined"], expected == "fresh")

    def test_every_configured_asset_appears(self):
        from app.routers.intel import onchain_context
        from lib.onchain import ASSETS
        out = onchain_context()
        self.assertEqual({a["symbol"] for a in out["assets"]}, set(ASSETS))

    def test_thin_history_reports_level_without_a_fabricated_percentile(self):
        """Under 12 observations there is no percentile to compute. The
        level still shows; the gauge reports None rather than a number
        invented from four points."""
        from app.routers.intel import onchain_context
        self._seed(days=5, ends_days_ago=1)
        a = self._asset(onchain_context())
        self.assertEqual(a["state"], "fresh")
        self.assertIn("mvrv", a)
        self.assertIsNone(a["mvrv_pctile_2y"])


if __name__ == "__main__":
    unittest.main()
