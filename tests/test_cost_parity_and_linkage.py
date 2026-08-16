"""P11 + P14 — cost parity, and outcomes that remember their signal.

P14 first, because it is the smaller and worse defect. `pos_signal_id` was
read at the top of the paper close path and then never passed to learning.
Every paper outcome therefore arrived detached from the signal that
produced it, so it could not be attributed to a strategy, timeframe, setup,
source or model version. It became an anonymous win/loss that can only ever
teach an aggregate — and an aggregate is the one thing this system already
had too much of.

P11 is the parity rule: the cost model used by the GATE must be the cost
model realized by the EXCHANGE. A gate refusing trades on
spread + slippage + commission + funding while the book records commission
only is selecting against costs it never pays.

Keeping both numbers answers a question one number cannot: was the SIGNAL
bad, or was the COST ESTIMATE bad? Those need opposite responses, and a
desk that records only realized cost will retire profitable strategies for
having been priced badly.
"""
import unittest

from lib.cost_parity import (COMPONENTS, MATERIAL_ERROR_PCT, compare,
                             realized_from_outcome, summarize)


class LinkageTests(unittest.TestCase):
    """P14 — the outcome must remember where it came from."""

    def test_the_full_close_passes_the_signal_id(self):
        import inspect

        from lib import paper_engine
        src = inspect.getsource(paper_engine)
        self.assertIn("signal_id=pos_signal_id", src,
                      "paper outcomes are orphaned from their signal again")

    def test_both_close_paths_pass_it(self):
        """A partial exit is still evidence about the same thesis."""
        import inspect

        from lib import paper_engine
        src = inspect.getsource(paper_engine)
        self.assertGreaterEqual(src.count("signal_id=pos_signal_id"), 2)

    def test_the_recorder_accepts_it(self):
        import inspect

        from lib import learning_engine
        self.assertIn("signal_id", inspect.signature(
            learning_engine.record_trade_outcome).parameters)


class ParityTests(unittest.TestCase):
    def test_a_matched_estimate_shows_no_error(self):
        c = compare(estimated={"spread": 5.0, "commission": 2.0},
                    realized={"spread": 5.0, "commission": 2.0})
        self.assertEqual(c.error_usd, 0.0)
        self.assertFalse(c.materially_wrong)

    def test_underestimation_is_flagged_as_the_dangerous_direction(self):
        """The gate let trades through that could not pay for themselves."""
        c = compare(estimated={"spread": 5.0}, realized={"spread": 20.0})
        self.assertTrue(c.underestimated)
        self.assertTrue(c.materially_wrong)
        self.assertGreater(c.error_usd, 0)

    def test_overestimation_is_also_reported(self):
        """A gate that is too pessimistic rejects good trades — a real
        cost, just a quieter one."""
        c = compare(estimated={"spread": 20.0}, realized={"spread": 5.0})
        self.assertFalse(c.underestimated)
        self.assertTrue(c.materially_wrong)

    def test_a_cost_the_book_forgot_shows_as_full_underestimation(self):
        """THE parity failure: the gate charged funding, the book did not."""
        c = compare(estimated={"commission": 2.0, "funding": 8.0},
                    realized={"commission": 2.0})
        self.assertAlmostEqual(c.error_usd, -8.0)
        comp = c.by_component()
        self.assertAlmostEqual(comp["funding"]["realized_usd"], 0.0)

    def test_a_missing_realized_component_is_zero_not_assumed_equal(self):
        c = compare(estimated={"pool": 3.0}, realized={})
        self.assertAlmostEqual(c.realized_total_usd, 0.0)

    def test_error_is_reported_per_component(self):
        """An aggregate 30% off is not actionable; knowing it is entirely
        funding on a long hold is."""
        c = compare(estimated={"spread": 5.0, "funding": 1.0},
                    realized={"spread": 5.0, "funding": 9.0})
        comp = c.by_component()
        self.assertAlmostEqual(comp["spread"]["error_usd"], 0.0)
        self.assertAlmostEqual(comp["funding"]["error_usd"], 8.0)

    def test_costs_convert_to_r_when_risk_is_known(self):
        c = compare(estimated={"spread": 10.0}, realized={"spread": 20.0},
                    initial_risk_usd=100.0)
        self.assertAlmostEqual(c.estimated_total_r, 0.10)
        self.assertAlmostEqual(c.realized_total_r, 0.20)

    def test_no_risk_leaves_r_unknown_rather_than_zero(self):
        c = compare(estimated={"spread": 10.0}, realized={"spread": 10.0})
        self.assertIsNone(c.estimated_total_r)

    def test_small_noise_is_not_called_materially_wrong(self):
        c = compare(estimated={"spread": 100.0}, realized={"spread": 105.0})
        self.assertLess(abs(c.error_pct), MATERIAL_ERROR_PCT)
        self.assertFalse(c.materially_wrong)


class RealizedExtractionTests(unittest.TestCase):
    def test_attribution_and_charges_are_both_counted(self):
        """The gate estimated the FULL economic cost. Comparing it against
        ledger charges alone makes every estimate look pessimistic and
        pushes the desk to loosen a gate that was right."""
        from lib.realized_outcome import build

        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=110.0, quantity=10.0,
                  commission_usd=2.0, spread_attribution_usd=4.0,
                  slippage_attribution_usd=3.0)
        r = realized_from_outcome(o)
        self.assertAlmostEqual(r["commission"], 2.0)
        self.assertAlmostEqual(r["spread"], 4.0)
        self.assertAlmostEqual(r["slippage"], 3.0)

    def test_funding_keeps_its_sign_where_others_take_absolutes(self):
        """A short RECEIVING funding genuinely reduces the cost of the
        trade, so flattening it to a positive would misprice every short."""
        from lib.realized_outcome import build

        o = build(symbol="BTC/USD", direction="Short_10x", entry_fill=100.0,
                  exit_fill=90.0, quantity=1.0, funding_usd=-5.0)
        self.assertLess(realized_from_outcome(o)["funding"], 0)

    def test_every_component_name_is_known_to_the_comparison(self):
        from lib.realized_outcome import build

        o = build(symbol="NVDA", direction="Long", entry_fill=100.0,
                  exit_fill=101.0, quantity=1.0)
        for k in realized_from_outcome(o):
            self.assertIn(k, COMPONENTS, k)


class SummaryTests(unittest.TestCase):
    def test_an_empty_set_says_so_rather_than_reporting_zero(self):
        self.assertEqual(summarize([])["n"], 0)

    def test_the_systematically_wrong_component_is_identified(self):
        rows = [compare(estimated={"spread": 5.0, "funding": 1.0},
                        realized={"spread": 5.0, "funding": 9.0})
                for _ in range(5)]
        self.assertEqual(summarize(rows)["worst_component"], "funding")

    def test_median_is_reported_beside_the_mean(self):
        """One gap fill should not make a correct model look broken."""
        rows = [compare(estimated={"spread": 5.0}, realized={"spread": 5.0})
                for _ in range(9)]
        rows.append(compare(estimated={"spread": 5.0}, realized={"spread": 500.0}))
        s = summarize(rows)
        self.assertAlmostEqual(s["median_error_usd"], 0.0)
        self.assertGreater(s["mean_error_usd"], 0.0)

    def test_the_underestimation_rate_is_reported(self):
        rows = [compare(estimated={"spread": 1.0}, realized={"spread": 9.0}),
                compare(estimated={"spread": 9.0}, realized={"spread": 1.0})]
        self.assertAlmostEqual(summarize(rows)["underestimated_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
