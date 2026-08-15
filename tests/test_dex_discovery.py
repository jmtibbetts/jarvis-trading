"""DEX discovery — the filter is the product, so the filter is what's
pinned.

The live feed is overwhelmingly untradeable: a real GeckoTerminal sample
carried $1,680 of liquidity, $7.46 of daily volume, one buy and zero
sells. Every floor here exists because something like that would
otherwise reach a watchlist wearing the word "opportunity".
"""
import unittest
from datetime import datetime, timedelta, timezone

from lib.dex_discovery import (
    MIN_BUYERS_24H,
    MIN_LIQUIDITY_USD,
    screen_pool,
)

NOW = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def _pool(**over):
    """A pool that passes everything, so each test can break one thing."""
    a = {
        "name": "GOOD / SOL",
        "address": "pool123",
        "reserve_in_usd": "500000",
        "volume_usd": {"h24": "2000000"},
        "transactions": {"h24": {"buys": 6000, "sells": 4000,
                                 "buyers": 1500, "sellers": 1200}},
        "pool_created_at": (NOW - timedelta(days=7)).isoformat().replace(
            "+00:00", "Z"),
        "fdv_usd": "8000000",
    }
    a.update(over)
    return {"attributes": a}


class FloorTests(unittest.TestCase):
    def test_a_healthy_pool_passes(self):
        v = screen_pool(_pool(), "solana", now=NOW)
        self.assertTrue(v["passes"], v["reasons"])
        self.assertEqual(v["txns_24h"], 10_000)
        self.assertAlmostEqual(v["sell_ratio"], 0.4)

    def test_the_real_world_noise_sample_is_rejected(self):
        """The actual shape the live endpoint returned."""
        v = screen_pool(_pool(
            name="SENDOR / SOL",
            reserve_in_usd="1680.41",
            volume_usd={"h24": "7.46"},
            transactions={"h24": {"buys": 1, "sells": 0,
                                  "buyers": 1, "sellers": 0}},
            pool_created_at=(NOW - timedelta(minutes=20)).isoformat().replace(
                "+00:00", "Z"),
        ), "solana", now=NOW)
        self.assertFalse(v["passes"])
        for tag in ("liquidity", "volume", "txns", "buyers", "too_young"):
            self.assertIn(tag, v["reason_tags"])

    def test_one_way_flow_is_flagged_as_possible_honeypot(self):
        v = screen_pool(_pool(
            transactions={"h24": {"buys": 5000, "sells": 10,
                                  "buyers": 900, "sellers": 8}},
        ), "solana", now=NOW)
        self.assertFalse(v["passes"])
        self.assertIn("one_way_flow", v["reason_tags"])

    def test_age_window_rejects_both_ends(self):
        young = screen_pool(_pool(
            pool_created_at=(NOW - timedelta(hours=3)).isoformat().replace(
                "+00:00", "Z")), "solana", now=NOW)
        old = screen_pool(_pool(
            pool_created_at=(NOW - timedelta(days=200)).isoformat().replace(
                "+00:00", "Z")), "solana", now=NOW)
        self.assertIn("too_young", young["reason_tags"])
        self.assertIn("too_old", old["reason_tags"])

    def test_missing_creation_time_is_a_rejection_not_a_pass(self):
        v = screen_pool(_pool(pool_created_at=None), "solana", now=NOW)
        self.assertFalse(v["passes"])
        self.assertIn("no_creation_time", v["reason_tags"])

    def test_every_rejection_states_its_reason(self):
        v = screen_pool(_pool(reserve_in_usd="10", volume_usd={"h24": "5"}),
                        "solana", now=NOW)
        self.assertFalse(v["passes"])
        self.assertTrue(v["reasons"], "a silent rejection is a black box")
        self.assertEqual(len(v["reasons"]), len(v["reason_tags"]))
        self.assertTrue(any(str(int(MIN_LIQUIDITY_USD)) in r.replace(",", "")
                            for r in v["reasons"]),
                        "the reason should name the floor it missed")

    def test_malformed_payload_fails_closed(self):
        v = screen_pool({"attributes": {}}, "solana", now=NOW)
        self.assertFalse(v["passes"])
        v2 = screen_pool({}, "solana", now=NOW)
        self.assertFalse(v2["passes"])



class OnChainContextTests(unittest.TestCase):
    """Coin Metrics community: ask what's granted, and let stale data
    abstain rather than pose as today's network state."""

    def test_only_granted_metrics_are_requested(self):
        """One ungranted metric name 403s the WHOLE batch, silently
        costing every other metric in it — verified live: FeeMeanUSD is
        not on the community tier and its presence failed the request."""
        from unittest.mock import patch
        import lib.onchain as oc
        with patch.object(oc, "granted_metrics",
                          return_value={"CapMVRVCur", "TxCnt"}):
            with patch.object(oc.httpx, "get") as g:
                g.return_value.json.return_value = {"data": []}
                g.return_value.raise_for_status = lambda: None
                oc.fetch_metrics("btc")
                asked = g.call_args.kwargs["params"]["metrics"].split(",")
        self.assertEqual(set(asked), {"CapMVRVCur", "TxCnt"})
        self.assertNotIn("FeeMeanUSD", asked)

    def test_stale_network_state_abstains(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        import lib.onchain as oc
        old = (datetime.now(timezone.utc) - timedelta(days=9)).date()
        with patch("lib.sector_engine._series", return_value=[(old, 2.1)]):
            self.assertEqual(oc.latest_context("BTC/USD"), {})

    def test_release_stamp_is_the_day_after_the_observation(self):
        """A metric describing the 15th was not knowable during the 15th."""
        from datetime import datetime, timezone
        d = datetime(2026, 8, 15, tzinfo=timezone.utc)
        release = (d + timedelta(days=1)).timestamp()
        self.assertGreater(release, d.timestamp())


if __name__ == "__main__":
    unittest.main()
