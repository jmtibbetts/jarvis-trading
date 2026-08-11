import unittest

from lib.scenario_engine import build_scenario, build_scenarios


def _ta(structure="uptrend", labels=("HH", "HL"), event=None,
        st_dir="up", st_level=95.0, swing_high=110.0, swing_low=95.0,
        breakout_up=False, breakout_down=False, price=105.0):
    return {
        "price": {"last": price},
        "atr": {"value": 2.0, "pct": 1.9},
        "market_structure": {
            "structure": structure, "labels": list(labels), "event": event,
            "last_swing_high": swing_high, "last_swing_low": swing_low,
        },
        "supertrend": {"direction": st_dir, "level": st_level, "flipped_this_bar": False},
        "donchian": {"upper": 111.0, "lower": 94.0, "mid": 102.5,
                     "breakout_up": breakout_up, "breakout_down": breakout_down},
    }


class ScenarioStateTests(unittest.TestCase):
    def test_uptrend_long_is_watch_with_explicit_trigger(self):
        s = build_scenario("long", _ta())
        self.assertEqual(s["state"], "WATCH")
        self.assertEqual(s["trigger"], "close above 110.0")
        self.assertEqual(s["invalidation"], "close below 95.0")

    def test_uptrend_short_is_no_trade(self):
        """Direction against both structure and trend, nothing activated."""
        s = build_scenario("short", _ta())
        self.assertEqual(s["state"], "NO_TRADE")
        self.assertIn("market structure is uptrend", s["against"][0])

    def test_bos_up_makes_long_ready(self):
        s = build_scenario("long", _ta(event="BOS_UP", price=112.0))
        self.assertEqual(s["state"], "READY")
        self.assertIn("structure event BOS_UP already occurred", s["evidence"])

    def test_choch_down_makes_short_ready_even_in_uptrend(self):
        """CHoCH is the earliest reversal evidence: the uptrend label argues
        against the short, but the activation event has already occurred, so
        the scenario is READY with the conflict visible in `against`."""
        s = build_scenario("short", _ta(event="CHOCH_DOWN", price=94.0))
        self.assertEqual(s["state"], "READY")
        self.assertTrue(any("uptrend" in a for a in s["against"]))

    def test_downtrend_mirrors(self):
        ta = _ta(structure="downtrend", labels=("LH", "LL"), st_dir="down",
                 st_level=108.0, price=97.0)
        long_s = build_scenario("long", ta)
        short_s = build_scenario("short", ta)
        self.assertEqual(long_s["state"], "NO_TRADE")
        self.assertEqual(short_s["state"], "WATCH")
        self.assertEqual(short_s["trigger"], "close below 95.0")
        self.assertEqual(short_s["invalidation"], "close above 110.0")

    def test_range_both_directions_watch(self):
        """A range forces no trade in either direction but names the break
        level each side would need."""
        ta = _ta(structure="range", labels=("LH", "HL"))
        long_s = build_scenario("long", ta)
        short_s = build_scenario("short", ta)
        self.assertEqual(long_s["state"], "WATCH")
        self.assertEqual(short_s["state"], "WATCH")

    def test_triggers_fall_back_to_donchian_when_no_swings(self):
        ta = _ta()
        ta["market_structure"] = {"structure": None, "labels": [], "event": None,
                                  "last_swing_high": None, "last_swing_low": None}
        s = build_scenario("long", ta)
        self.assertEqual(s["state"], "WATCH")
        self.assertEqual(s["trigger_level"], 111.0)  # Donchian upper, a real computed level

    def test_no_levels_at_all_is_no_trade_not_invented_levels(self):
        ta = {"price": {"last": 100.0}, "atr": None, "market_structure": None,
              "supertrend": None, "donchian": None}
        s = build_scenario("long", ta)
        self.assertEqual(s["state"], "NO_TRADE")
        self.assertIsNone(s["trigger"])


class BuildScenariosTests(unittest.TestCase):
    def test_returns_both_directions(self):
        r = build_scenarios(_ta())
        self.assertEqual(r["long"]["direction"], "long")
        self.assertEqual(r["short"]["direction"], "short")

    def test_errored_ta_returns_none(self):
        self.assertIsNone(build_scenarios({"error": "insufficient data"}))
        self.assertIsNone(build_scenarios(None))


if __name__ == "__main__":
    unittest.main()
