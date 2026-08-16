"""The eight training-evidence invariants.

Each exists because a plausible shortcut would corrupt the training set in
a way that LOOKS like improvement:

1. Fuzzy-matching old outcomes to strategies inflates every per-strategy
   statistic while looking exactly as confident as measured evidence.
2. Counting execution rows instead of theses makes one market event vote
   once per arm.
3. Unpaired comparison throws away the variance reduction that pairing
   gives for free.
4. Collapsing the ID levels loses a question the system needs to answer.
5. Depth certainty as a display label sizes a modelled pool like a
   measured one — honest provenance, dishonest behaviour.
6. A wallet that spends its last lamport cannot pay for the swap it just
   made.
7. Free failed transactions teach that bad route selection is costless,
   and reverts cluster exactly where the opportunity looks best.
8. Charging a cost twice, or never, both misprice the strategy.
"""
import unittest

from lib.dex_swap_math import (DEPTH_IMPACT_MULTIPLIER, DEPTH_SIZE_FACTOR,
                               depth_adjusted_size, failed_transaction_cost,
                               spendable_native)
from lib.trade_thesis import (AGENT, LEGACY_UNATTRIBUTED, SHADOW, ArmResult,
                              attribution_state, build, is_attributable,
                              make_arm_id, paired_deltas, sample_count)


def th(**kw):
    base = dict(symbol="BTC/USD", side="Long", strategy="breakout",
                timeframe="15m", entry=100.0, stop=98.0, target=106.0,
                created_at="2026-08-16T12:00:00Z")
    base.update(kw)
    return build(**base)


class Invariant1_NoFuzzyAttributionTests(unittest.TestCase):
    def test_an_outcome_without_a_signal_is_legacy_unattributed(self):
        self.assertEqual(attribution_state(None, "t1"), LEGACY_UNATTRIBUTED)
        self.assertEqual(attribution_state("", "t1"), LEGACY_UNATTRIBUTED)

    def test_an_outcome_without_a_thesis_is_legacy_unattributed(self):
        self.assertEqual(attribution_state("s1", None), LEGACY_UNATTRIBUTED)

    def test_both_present_is_attributable(self):
        self.assertTrue(is_attributable("s1", "t1"))
        self.assertEqual(attribution_state("s1", "t1"), "ATTRIBUTED")

    def test_arm_results_report_their_own_attribution(self):
        unattributed = ArmResult("t1", AGENT, traded=True)
        attributed = ArmResult("t1", AGENT, traded=True, signal_id="s1")
        self.assertEqual(unattributed.attribution, LEGACY_UNATTRIBUTED)
        self.assertEqual(attributed.attribution, "ATTRIBUTED")


class Invariant2_ThesisIsTheSampleUnitTests(unittest.TestCase):
    def test_regenerated_setups_do_not_become_separate_theses(self):
        """THE defect this closes. The scheduler re-emits the same setup
        with a few bp of drift; on raw floats that manufactured a second
        independent market event."""
        a = th(entry=100.01, created_at="2026-08-16T12:00:00Z")
        b = th(entry=100.03, created_at="2026-08-16T12:08:00Z")
        self.assertEqual(a.thesis_id, b.thesis_id)

    def test_a_genuinely_different_entry_is_a_different_thesis(self):
        self.assertNotEqual(th(entry=100.0).thesis_id, th(entry=112.0).thesis_id)

    def test_bucketing_is_proportional_not_absolute(self):
        """25bp must mean the same on a memecoin as on BTC. A fixed epsilon
        would merge every memecoin level and split identical BTC prints."""
        micro_same = (th(symbol="BONK/USD", entry=0.000004000).thesis_id ==
                      th(symbol="BONK/USD", entry=0.000004001).thesis_id)
        micro_diff = (th(symbol="BONK/USD", entry=0.000004000).thesis_id ==
                      th(symbol="BONK/USD", entry=0.000005000).thesis_id)
        self.assertTrue(micro_same)
        self.assertFalse(micro_diff)

    def test_a_much_later_setup_is_a_new_thesis(self):
        self.assertNotEqual(th(created_at="2026-08-16T12:00:00Z").thesis_id,
                            th(created_at="2026-08-17T12:00:00Z").thesis_id)

    def test_sample_count_uses_theses_not_arm_rows(self):
        t = th().thesis_id
        rows = [ArmResult(t, AGENT, traded=True), ArmResult(t, SHADOW, traded=True)]
        self.assertEqual(sample_count(rows), 1)


class Invariant3_PairedOutcomesTests(unittest.TestCase):
    def test_delta_is_computed_per_shared_thesis(self):
        t = th().thesis_id
        rows = [ArmResult(t, AGENT, traded=True, net_r=1.4),
                ArmResult(t, SHADOW, traded=True, net_r=0.8)]
        d = paired_deltas(rows)
        self.assertEqual(len(d), 1)
        self.assertAlmostEqual(d[0]["delta_net_r"], 0.6)

    def test_unpaired_theses_produce_no_delta(self):
        self.assertEqual(paired_deltas(
            [ArmResult(th().thesis_id, AGENT, traded=True, net_r=1.0)]), [])

    def test_five_hundred_theses_give_five_hundred_differences(self):
        """Entries are spaced PROPORTIONALLY (1% apart), not by a fixed
        dollar step. Consecutive integers at entry=500 sit inside the same
        25bp bucket by design — a naive `100.0 + i` fixture collapses ~7%
        of them and would read as a bug in the counter rather than as the
        bucketing doing its job."""
        rows = []
        for i in range(500):
            t = th(entry=100.0 * (1.01 ** i)).thesis_id
            rows += [ArmResult(t, AGENT, traded=True, net_r=1.0),
                     ArmResult(t, SHADOW, traded=True, net_r=0.5)]
        self.assertEqual(len(rows), 1000)
        self.assertEqual(sample_count(rows), 500)
        self.assertEqual(len(paired_deltas(rows)), 500)
        self.assertAlmostEqual(paired_deltas(rows)[0]["delta_net_r"], 0.5)

    def test_regeneration_drift_mostly_collapses_and_never_under_counts(self):
        """A KNOWN LIMIT OF GRID BUCKETING, stated rather than hidden.

        Rounding onto a grid collapses the vast majority of regeneration
        drift, but two values a few bp apart can still straddle a bucket
        boundary and stay distinct. That error is BOUNDED and one-sided: it
        can only ever over-count slightly, never merge two genuinely
        different claims — and it is far smaller than the raw-float case
        it replaced, where every pass produced a new thesis.

        Asserting that any specific near-pair collapses would be asserting
        something grid bucketing cannot promise, so this measures the rate
        instead.
        """
        pairs = 200
        collapsed = sum(
            1 for i in range(pairs)
            if th(entry=500.0 + i).thesis_id == th(entry=500.0 + i + 0.05).thesis_id)
        self.assertGreater(collapsed / pairs, 0.85,
                           "1bp of drift should almost always be one thesis")

    def test_a_genuinely_different_level_never_collapses(self):
        """The one-sided guarantee: distinct claims stay distinct."""
        for base in (0.000004, 1.0, 100.0, 500.0, 95_000.0):
            self.assertNotEqual(th(entry=base).thesis_id,
                                th(entry=base * 1.05).thesis_id, base)


class Invariant4_IdHierarchyTests(unittest.TestCase):
    def test_arms_on_one_thesis_get_distinct_arm_ids(self):
        t = th().thesis_id
        self.assertNotEqual(make_arm_id(t, AGENT), make_arm_id(t, SHADOW))

    def test_the_same_arm_on_one_thesis_is_stable(self):
        t = th().thesis_id
        self.assertEqual(make_arm_id(t, AGENT), make_arm_id(t, AGENT))

    def test_policy_variants_of_one_arm_stay_distinct(self):
        """An A/B on sizing must not overwrite itself."""
        t = th().thesis_id
        self.assertNotEqual(make_arm_id(t, AGENT, "risk_1pct"),
                            make_arm_id(t, AGENT, "risk_2pct"))

    def test_every_level_is_carried_separately(self):
        r = ArmResult("t1", AGENT, signal_id="s1", execution_id="e1",
                      outcome_id="o1")
        for f in ("thesis_id", "arm_id", "execution_id", "outcome_id",
                  "signal_id"):
            self.assertTrue(getattr(r, f), f)


class Invariant5_DepthDrivesSizingTests(unittest.TestCase):
    def test_less_certain_depth_sizes_smaller(self):
        v = depth_adjusted_size(10_000, "VERIFIED")["size_usd"]
        a = depth_adjusted_size(10_000, "ASSUMED_BALANCED_POOL")["size_usd"]
        m = depth_adjusted_size(10_000, "MODELLED_ESTIMATE")["size_usd"]
        self.assertGreater(v, a)
        self.assertGreater(a, m)

    def test_unknown_confidence_falls_to_the_most_conservative(self):
        """A pool nobody classified is not a pool anybody measured."""
        self.assertEqual(depth_adjusted_size(10_000, None)["size_factor"],
                         DEPTH_SIZE_FACTOR["MODELLED_ESTIMATE"])
        self.assertEqual(depth_adjusted_size(10_000, "nonsense")["size_factor"],
                         DEPTH_SIZE_FACTOR["MODELLED_ESTIMATE"])

    def test_uncertain_depth_also_weights_predicted_impact_up(self):
        self.assertGreater(DEPTH_IMPACT_MULTIPLIER["MODELLED_ESTIMATE"],
                           DEPTH_IMPACT_MULTIPLIER["VERIFIED"])

    def test_it_reaches_the_real_sizing_path_not_just_the_helper(self):
        from lib.dex_paper import size_for_pool
        v = size_for_pool(500_000, 100_000, dex="raydium",
                          depth_confidence="VERIFIED")
        m = size_for_pool(500_000, 100_000, dex="raydium",
                          depth_confidence="MODELLED_ESTIMATE")
        self.assertGreater(v["size_usd"], m["size_usd"])
        self.assertEqual(m["depth_confidence"], "MODELLED_ESTIMATE")


class Invariant6_NativeGasReserveTests(unittest.TestCase):
    def test_the_last_lamport_cannot_be_spent(self):
        s = spendable_native(0.405)
        self.assertLess(s["max_spendable_sol"], 0.405)
        self.assertGreater(s["execution_reserve_sol"], 0)

    def test_a_wallet_below_the_reserve_cannot_transact(self):
        self.assertFalse(spendable_native(0.0000001)["can_transact"])

    def test_a_funded_wallet_can(self):
        self.assertTrue(spendable_native(1.0)["can_transact"])

    def test_a_priority_fee_increases_the_reserve(self):
        low = spendable_native(1.0, priority_lamports=0)
        high = spendable_native(1.0, priority_lamports=5_000_000)
        self.assertGreater(high["execution_reserve_sol"],
                           low["execution_reserve_sol"])


class Invariant7_FailedTransactionCostTests(unittest.TestCase):
    def test_a_failed_onchain_tx_costs_gas_and_yields_nothing(self):
        f = failed_transaction_cost(sol_price_usd=200.0)
        self.assertEqual(f["tokens_out"], 0.0)
        self.assertGreater(f["network_fee_usd"], 0)

    def test_a_pre_submission_rejection_costs_nothing(self):
        f = failed_transaction_cost(sol_price_usd=200.0, reached_chain=False)
        self.assertEqual(f["network_fee_sol"], 0.0)

    def test_the_two_failures_are_distinguishable(self):
        self.assertNotEqual(
            failed_transaction_cost(sol_price_usd=200.0)["reached_chain"],
            failed_transaction_cost(sol_price_usd=200.0,
                                    reached_chain=False)["reached_chain"])


class Invariant8_CostsChargedOnceTests(unittest.TestCase):
    def test_gas_does_not_reduce_pool_output(self):
        from lib.dex_swap_math import quote_swap
        a = quote_swap(1_000, 500_000, sol_price_usd=200.0, priority_lamports=0)
        b = quote_swap(1_000, 500_000, sol_price_usd=200.0,
                       priority_lamports=5_000_000)
        self.assertAlmostEqual(a["received_usd"], b["received_usd"])

    def test_attribution_is_not_subtracted_from_cash(self):
        from lib.realized_outcome import build as build_outcome
        o = build_outcome(symbol="NVDA", direction="Long", entry_fill=100.0,
                          exit_fill=110.0, quantity=10.0,
                          spread_attribution_usd=5.0,
                          slippage_attribution_usd=5.0)
        self.assertAlmostEqual(o.net_pnl_usd, 100.0)


if __name__ == "__main__":
    unittest.main()
