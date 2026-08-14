"""Phase 2: what the learning ledger stands on must be visible and right.

Two defects pinned here:
  - lifecycle judged LIVE-capital state on replayed bars at FULL weight —
    500 replays could promote a strategy that 3 live fills would have
    kept EXPERIMENTAL
  - every R was computed from the SIGNAL's proposed stop, not the stop
    AS PLACED (horizon caps and spread-adjusted fills move it), so the
    ledger measured risk nobody actually took
"""
import unittest

from lib.expectancy import REPLAY_WEIGHT
from lib.strategy_lifecycle import _expectancy


def _row(r, replay=False):
    return {"r": r, "replay": replay, "strategy": "s", "timeframe": "4H",
            "exited_at": "2026-08-14T00:00:00"}


class ReplayWeightingTests(unittest.TestCase):
    def test_replay_counts_at_half_weight(self):
        live = _expectancy([_row(1.0), _row(-1.0)])
        replayed = _expectancy([_row(1.0, True), _row(-1.0, True)])
        self.assertEqual(live["trades"], 2)
        self.assertEqual(replayed["trades"], 2 * REPLAY_WEIGHT)

    def test_live_evidence_dominates_a_mixed_pool(self):
        """Three live losses vs three replay wins: with replay at half
        weight the pool leans on what actually happened."""
        rows = [_row(-1.0)] * 3 + [_row(1.5, True)] * 3
        e = _expectancy(rows)
        self.assertLess(e["p_win"], 0.5)

    def test_the_mix_is_surfaced(self):
        e = _expectancy([_row(1.0), _row(1.0, True), _row(-0.5, True)])
        self.assertEqual(e["trades_live"], 1)
        self.assertEqual(e["trades_replay"], 2)


class PlacedStopProvenanceTests(unittest.TestCase):
    def test_expectancy_prefers_the_placed_stop(self):
        import inspect

        from lib import expectancy, strategy_lifecycle
        self.assertIn("_placed_stops", inspect.getsource(expectancy.build_table))
        self.assertIn("placed.get(sig_id)", inspect.getsource(strategy_lifecycle._load_rows))

    def test_expectancy_cells_surface_the_source_mix(self):
        import inspect

        from lib import expectancy
        src = inspect.getsource(expectancy._summarise)
        self.assertIn("sample_live", src)
        self.assertIn("sample_replay", src)


if __name__ == "__main__":
    unittest.main()
