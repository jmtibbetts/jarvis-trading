"""One market observation, however many policies act on it.

THE SAMPLE-INFLATION BUG. The Agent book and the Shadow control book see
the same signals. If both trade and both record an outcome, naive counting
calls that two independent observations — so a market that moved once votes
twice, confidence intervals shrink by root two for free, and a strategy
looks twice as validated as its evidence supports.

They are ONE market event with TWO POLICY RESULTS, and that is exactly what
makes the comparison valuable: same instrument, same moment, same evidence,
different decisions.

The error is dangerous because it is invisible in the direction everyone
wants — the sample looks bigger, the confidence looks better, and nothing
throws.
"""
import unittest

from lib.trade_thesis import (AGENT, ARMS, COUNTERFACTUAL, SHADOW, ArmResult,
                              TradeThesis, build, decompose, sample_count)


def thesis(symbol="NVDA", side="Long", **kw):
    return build(symbol=symbol, side=side, strategy="breakout",
                 timeframe="4H", entry=100.0, stop=98.0, target=106.0,
                 created_at="2026-08-16T12:00:00Z", **kw)


class ThesisIdentityTests(unittest.TestCase):
    def test_the_same_claim_produces_the_same_id(self):
        self.assertEqual(thesis().thesis_id, thesis().thesis_id)

    def test_a_different_claim_produces_a_different_id(self):
        self.assertNotEqual(thesis().thesis_id,
                            thesis(side="Short").thesis_id)
        self.assertNotEqual(thesis().thesis_id,
                            thesis(symbol="AAPL").thesis_id)

    def test_levels_are_part_of_the_claim(self):
        a = thesis()
        b = build(symbol="NVDA", side="Long", strategy="breakout",
                  timeframe="4H", entry=100.0, stop=95.0, target=106.0,
                  created_at="2026-08-16T12:00:00Z")
        self.assertNotEqual(a.thesis_id, b.thesis_id)

    def test_policy_choices_do_NOT_change_the_id(self):
        """THE rule. Size, leverage and venue are what the arms differ on.
        Including them would give Agent and Shadow different ids for one
        market event and reinstate the double-count."""
        a = thesis()
        b = thesis()
        b.evidence = {"anything": "different"}
        b.measured_edge = {"net_r": 0.4}
        self.assertEqual(a.thesis_id, b.thesis_id)

    def test_a_leveraged_short_normalises_to_its_side(self):
        self.assertEqual(thesis(side="Short_10x").side, "short")
        self.assertEqual(thesis(side="Short_10x").thesis_id,
                         thesis(side="Short_5x").thesis_id,
                         "leverage is a policy choice, not part of the claim")

    def test_an_unreadable_side_is_not_a_thesis(self):
        for d in ("Aggressive_Moon_Mode", "", None, "LONGSHORT"):
            with self.assertRaises(ValueError, msg=repr(d)):
                thesis(side=d)

    def test_fx_and_crypto_get_distinct_instrument_ids(self):
        self.assertNotEqual(thesis(symbol="EUR/USD").instrument_id,
                            thesis(symbol="BTC/USD").instrument_id)


class SampleCountTests(unittest.TestCase):
    """The guard that stops one market event voting twice."""

    def test_two_arms_on_one_thesis_is_ONE_sample(self):
        t = thesis().thesis_id
        rows = [ArmResult(t, AGENT, traded=True, net_r=1.2),
                ArmResult(t, SHADOW, traded=True, net_r=0.8)]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sample_count(rows), 1)

    def test_the_naive_count_would_have_doubled_it(self):
        t = thesis().thesis_id
        rows = [ArmResult(t, AGENT, traded=True), ArmResult(t, SHADOW, traded=True)]
        self.assertEqual(len(rows) / sample_count(rows), 2.0)

    def test_distinct_theses_count_separately(self):
        rows = [ArmResult(thesis().thesis_id, AGENT, traded=True),
                ArmResult(thesis(symbol="AAPL").thesis_id, AGENT, traded=True)]
        self.assertEqual(sample_count(rows), 2)

    def test_every_arm_on_one_thesis_is_still_one_sample(self):
        # Deliberately driven by ARMS itself: adding an arm (OPERATOR, when
        # the manual desk landed) must not be able to inflate the sample.
        t = thesis().thesis_id
        rows = [ArmResult(t, a, traded=True) for a in ARMS]
        self.assertEqual(sample_count(rows), 1)


class DecompositionTests(unittest.TestCase):
    def _paired(self, agent_r, shadow_r, symbol="NVDA"):
        t = thesis(symbol=symbol).thesis_id
        return [ArmResult(t, AGENT, traded=True, net_r=agent_r),
                ArmResult(t, SHADOW, traded=True, net_r=shadow_r)]

    def test_market_samples_counts_theses_not_arm_results(self):
        rows = self._paired(1.0, 0.5) + self._paired(2.0, 1.0, "AAPL")
        d = decompose(rows)
        self.assertEqual(d["arm_results"], 4)
        self.assertEqual(d["market_samples"], 2)

    def test_management_edge_is_the_per_thesis_difference(self):
        rows = self._paired(1.5, 0.5) + self._paired(2.0, 1.0, "AAPL")
        d = decompose(rows)
        self.assertAlmostEqual(d["management_delta_r_mean"], 1.0)

    def test_a_worse_agent_shows_a_negative_delta(self):
        """The finding must be reportable in both directions, or the
        experiment is decoration."""
        d = decompose(self._paired(0.2, 1.4))
        self.assertLess(d["management_delta_r_mean"], 0)

    def test_selection_edge_separates_what_only_one_arm_took(self):
        t1 = thesis().thesis_id
        t2 = thesis(symbol="AAPL").thesis_id
        rows = [
            ArmResult(t1, AGENT, traded=True, net_r=2.0),
            ArmResult(t1, SHADOW, traded=False, no_trade_reason="DECLINED"),
            ArmResult(t2, AGENT, traded=False, no_trade_reason="DECLINED"),
            ArmResult(t2, SHADOW, traded=True, net_r=-1.0),
        ]
        d = decompose(rows)
        self.assertEqual(d["agent_only"], 1)
        self.assertEqual(d["shadow_only"], 1)
        self.assertAlmostEqual(d["selection_agent_only_mean_r"], 2.0)
        self.assertAlmostEqual(d["selection_shadow_only_mean_r"], -1.0)

    def test_only_theses_BOTH_arms_saw_are_compared(self):
        """A thesis one arm never evaluated says nothing about relative
        skill — including it would measure coverage and call it edge."""
        t = thesis().thesis_id
        d = decompose([ArmResult(t, AGENT, traded=True, net_r=3.0)])
        self.assertEqual(d["paired"], 0)
        self.assertIn("both arms", d["detail"])

    def test_a_decline_is_recorded_as_a_result(self):
        """A policy that avoids losers has selection edge that never shows
        up in its own trade log."""
        t = thesis().thesis_id
        rows = [ArmResult(t, AGENT, traded=False, no_trade_reason="DECLINED"),
                ArmResult(t, SHADOW, traded=True, net_r=-2.0)]
        d = decompose(rows)
        self.assertEqual(d["shadow_only"], 1)
        self.assertEqual(d["market_samples"], 1)

    def test_neither_trading_still_counts_as_one_observation(self):
        t = thesis().thesis_id
        rows = [ArmResult(t, AGENT, traded=False, no_trade_reason="DECLINED"),
                ArmResult(t, SHADOW, traded=False, no_trade_reason="DECLINED")]
        d = decompose(rows)
        self.assertEqual(d["neither_traded"], 1)
        self.assertEqual(d["market_samples"], 1)


if __name__ == "__main__":
    unittest.main()
