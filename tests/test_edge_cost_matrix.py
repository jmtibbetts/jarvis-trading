"""P27 — the edge–cost matrix.

The behaviour that matters is the DIAGNOSIS, not the arithmetic. A cell
that loses money can lose it two ways, and the two call for opposite
responses:

    gross 0.42R - cost 0.50R = -0.08R   the setup works, the venue eats it
    gross 0.02R - cost 0.09R = -0.07R   the setup does not work

A blended P&L reports both as "this setup lost money" and retires the
first one — a working thesis thrown away over a routing decision. These
tests pin the separation, the sample bar underneath it, and the two
honesty flags that stop the matrix overstating its own evidence.
"""
import unittest

from lib.edge_cost_matrix import (COST_ESTIMATED, COST_REALIZED, LIMIT_COST,
                                  LIMIT_EDGE, LIMIT_EVIDENCE, LIMIT_NONE,
                                  MIN_CELL_SAMPLE, _cell, matrix)


def row(gross, cost, *, source="live", basis=COST_ESTIMATED):
    return {"gross_r": gross, "cost_r": cost, "outcome_source": source,
            "cost_basis": basis}


class WhyACellFails(unittest.TestCase):
    def test_real_edge_eaten_by_cost_is_a_routing_result(self):
        c = _cell([row(0.42, 0.50)] * MIN_CELL_SAMPLE)
        self.assertEqual(c["verdict"], "UNTRADEABLE")
        self.assertEqual(c["limiting"], LIMIT_COST)

    def test_absent_edge_is_not_blamed_on_cost(self):
        """Cheaper routing cannot save a setup with no gross edge, and
        saying COST here would send someone hunting for a better venue."""
        c = _cell([row(0.02, 0.09)] * MIN_CELL_SAMPLE)
        self.assertEqual(c["limiting"], LIMIT_EDGE)

    def test_a_negative_gross_edge_is_an_edge_problem(self):
        c = _cell([row(-0.30, 0.10)] * MIN_CELL_SAMPLE)
        self.assertEqual(c["limiting"], LIMIT_EDGE)

    def test_a_cell_that_clears_says_nothing_is_limiting_it(self):
        c = _cell([row(0.60, 0.10)] * MIN_CELL_SAMPLE)
        self.assertEqual(c["verdict"], "TRADEABLE")
        self.assertEqual(c["limiting"], LIMIT_NONE)

    def test_too_few_trades_is_evidence_not_a_verdict(self):
        """A thin bucket has not earned an opinion. Calling it UNTRADEABLE
        would retire a strategy on noise."""
        c = _cell([row(-0.9, 0.4)] * (MIN_CELL_SAMPLE - 1))
        self.assertEqual(c["verdict"], "UNKNOWN")
        self.assertEqual(c["limiting"], LIMIT_EVIDENCE)

    def test_unpriceable_costs_produce_unknown_rather_than_free(self):
        """Pricing an unknown cost as zero flatters exactly the cells
        whose cost could not be established."""
        c = _cell([row(0.42, None)] * MIN_CELL_SAMPLE)
        self.assertIsNone(c["cost_r_median"])
        self.assertIsNone(c["net_r"])
        self.assertEqual(c["verdict"], "UNKNOWN")
        self.assertEqual(c["unpriced"], MIN_CELL_SAMPLE)


class EvidenceIsNotOverstated(unittest.TestCase):
    def test_a_replay_only_cell_says_so(self):
        """7,740 samples reads as overwhelming. 7,740 REPLAYED samples and
        zero live fills is a different claim — and the replay weighting
        cannot express it, because in a homogeneous cell the weight
        divides out of both sides."""
        c = _cell([row(0.4, 0.1, source="replay")] * 20)
        self.assertEqual(c["evidence"], "REPLAY_ONLY")
        self.assertEqual(c["n_live"], 0)

    def test_a_mixed_cell_is_labelled_mixed(self):
        c = _cell([row(0.4, 0.1, source="replay")] * 10
                  + [row(0.4, 0.1)] * 10)
        self.assertEqual(c["evidence"], "MIXED")

    def test_replayed_optimism_is_weighted_below_live_evidence(self):
        """A replayed fill assumed perfect execution and that both a bar's
        high and low were reachable. Pooling it 1:1 with a real fill lets
        the optimistic half set the answer."""
        live_loss = [row(-1.0, 0.1)] * 10
        replay_win = [row(1.0, 0.1, source="replay")] * 10
        blended = _cell(live_loss + replay_win)
        self.assertLess(blended["gross_r"], 0.0)

    def test_cost_bases_are_never_averaged_into_one_unlabelled_column(self):
        """An ESTIMATE and a MEASUREMENT are different claims about the
        same word. A cell holding both says MIXED rather than picking one."""
        c = _cell([row(0.4, 0.1)] * 5
                  + [row(0.4, 0.1, basis=COST_REALIZED)] * 5)
        self.assertEqual(c["cost_basis"], "MIXED")


class EndToEndOverTheBook(unittest.TestCase):
    """The whole pass, over closed outcomes written for this test.

    The suite runs on a throwaway database, so these rows ARE the book —
    which is the point: the matrix must produce its answer from stored
    outcomes and the instrument authority, not from anything ambient.
    """

    @classmethod
    def setUpClass(cls):
        from app.database import TradeOutcome, TradingSignal, get_db
        from lib.calibration import CURRENT_EPOCH
        with get_db() as db:
            for i in range(12):
                # A wide stop (10% of entry) so the round trip is cheap in
                # R, and a winning exit — a cell that should clear.
                db.add(TradingSignal(id=f"ECM-S{i}", asset_symbol="AAPL",
                                     direction="Long", strategy="test_wide",
                                     timeframe="1D", stop_loss=90.0))
                db.add(TradeOutcome(signal_id=f"ECM-S{i}", symbol="AAPL",
                                    asset_class="equity", direction="Long",
                                    timeframe="1D", entry_price=100.0,
                                    exit_price=110.0, hold_duration_m=1440,
                                    outcome="WIN", engine_epoch=CURRENT_EPOCH,
                                    outcome_source="live",
                                    exited_at="2099-01-01T00:00:00+00:00"))
            for i in range(12):
                # Same +1R gross, but a stop 0.2% wide. Cost in R scales as
                # 1/stop distance, so this is the COST-limited twin.
                db.add(TradingSignal(id=f"ECM-T{i}", asset_symbol="AAPL",
                                     direction="Long", strategy="test_tight",
                                     timeframe="5m", stop_loss=99.8))
                db.add(TradeOutcome(signal_id=f"ECM-T{i}", symbol="AAPL",
                                    asset_class="equity", direction="Long",
                                    timeframe="5m", entry_price=100.0,
                                    exit_price=100.2, hold_duration_m=15,
                                    outcome="WIN", engine_epoch=CURRENT_EPOCH,
                                    outcome_source="live",
                                    exited_at="2099-01-01T00:00:00+00:00"))
            db.commit()
        cls.m = matrix(days=365 * 100)
        cls.cells = {(c["strategy"], c["timeframe"]): c for c in cls.m["cells"]}

    @classmethod
    def tearDownClass(cls):
        # The database is per-session, not per-file: these rows would
        # otherwise reach whatever expectancy or calibration test runs next.
        from app.database import TradeOutcome, TradingSignal, get_db
        with get_db() as db:
            db.query(TradeOutcome).filter(
                TradeOutcome.signal_id.like("ECM-%")).delete(
                synchronize_session=False)
            db.query(TradingSignal).filter(
                TradingSignal.id.like("ECM-%")).delete(
                synchronize_session=False)
            db.commit()

    def test_it_builds_without_dropping_an_arm(self):
        self.assertEqual(self.m["errors"], [])
        self.assertGreaterEqual(self.m["cells_total"], 2)

    def test_the_wide_stop_cell_clears(self):
        c = self.cells[("test_wide", "1D")]
        self.assertEqual(c["n"], 12)
        self.assertAlmostEqual(c["gross_r"], 1.0, places=3)
        self.assertEqual(c["verdict"], "TRADEABLE")
        self.assertEqual(c["limiting"], LIMIT_NONE)

    def test_the_same_gross_edge_on_a_tight_stop_is_cost_limited(self):
        """Identical +1R gross. The only difference is stop width, and
        that alone decides whether the trade survives its own costs."""
        c = self.cells[("test_tight", "5m")]
        self.assertAlmostEqual(c["gross_r"], 1.0, places=3)
        self.assertGreater(c["cost_r_median"],
                           self.cells[("test_wide", "1D")]["cost_r_median"])
        self.assertEqual(c["limiting"], LIMIT_COST)

    def test_products_are_resolved_rather_than_renamed_asset_classes(self):
        """`asset_class` says "equity" and cannot tell spot from a
        perpetual. The product axis comes from the instrument authority."""
        self.assertEqual(self.cells[("test_wide", "1D")]["product"],
                         "EQUITY_SPOT")
        self.assertFalse(set(self.m["axes"]["product"])
                         & {"equity", "crypto", "unknown"})

    def test_every_cell_carries_its_limiting_factor(self):
        for c in self.m["cells"]:
            self.assertIn(c["limiting"],
                          (LIMIT_COST, LIMIT_EDGE, LIMIT_EVIDENCE, LIMIT_NONE))

    def test_a_single_venue_is_not_reported_as_a_routing_finding(self):
        """compare_venues still returns a lesson with one arm on file, and
        its NO_EDGE_ANYWHERE text claims the cost model is not the problem
        — which contradicts the COST cells. One arm cannot answer 'bad
        signal or bad venue', so it is marked non-comparable."""
        v = self.m["venues"]
        if len(v["venues_with_outcomes"]) < 2:
            self.assertFalse(v["comparable"])
            self.assertIn("both arms", v["not_comparable_reason"])
        else:
            self.assertTrue(v["comparable"])


if __name__ == "__main__":
    unittest.main()
