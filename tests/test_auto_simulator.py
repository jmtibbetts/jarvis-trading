import unittest
from types import SimpleNamespace

from lib.auto_simulator import (
    _AUTO_SIM_LOCK, _leverage, _pnl, _side, _unseen_candidates,
    run_auto_simulator,
)


class AutoSimulatorMathTests(unittest.TestCase):
    def test_long_and_short_pnl_are_directional(self):
        long_position = SimpleNamespace(entry_price=100, qty=10, side="long")
        short_position = SimpleNamespace(entry_price=100, qty=10, side="short")
        self.assertEqual(_pnl(long_position, 105), 50)
        self.assertEqual(_pnl(short_position, 95), 50)
        self.assertEqual(_pnl(short_position, 105), -50)

    def test_leverage_and_side_are_derived_without_broker_routing(self):
        self.assertEqual(_leverage("Long_5x"), 5)
        self.assertEqual(_leverage("Short_Leveraged"), 2)
        self.assertEqual(_side("Short_10x"), "short")
        self.assertEqual(_side("Long"), "long")

    def test_a_signal_is_not_reopened_after_it_has_been_seen(self):
        signals = [
            SimpleNamespace(id="new", status="Active"),
            SimpleNamespace(id="seen", status="Active"),
            SimpleNamespace(id="expired", status="Expired"),
        ]
        candidates = _unseen_candidates(signals, {"seen"})
        self.assertEqual([signal.id for signal in candidates], ["new"])

    def test_overlapping_run_returns_busy_instead_of_raising(self):
        _AUTO_SIM_LOCK.acquire()
        try:
            self.assertEqual(
                run_auto_simulator(),
                {"busy": True, "reason": "run_in_progress"},
            )
        finally:
            _AUTO_SIM_LOCK.release()


if __name__ == "__main__":
    unittest.main()


class TestLeverageLadder:
    """Score-scaled leverage + physics-respecting stops (user spec:
    5x-100x by signal strength; scalp stops <=3%, longer <=10%; a
    position can never out-risk its liquidation point)."""

    def test_score_maps_to_ladder(self):
        from lib.auto_simulator import score_leverage
        assert score_leverage(55) == 5
        assert score_leverage(100) == 100
        assert score_leverage(0) == 5          # floor
        assert score_leverage(77.5) in (30, 40)  # middle of the ladder
        # monotonic: stronger signal never gets less leverage
        prev = 0
        for sc in range(55, 101):
            lev = score_leverage(sc)
            assert lev >= prev
            prev = lev

    def test_liquidation_cap_beats_wide_stops(self):
        from lib.auto_simulator import leverage_capped_stop
        # 100x long: 0.9% max move regardless of a 5% signal stop
        assert leverage_capped_stop(100.0, 95.0, "long", 100, "4H") == 99.1

    def test_horizon_caps(self):
        from lib.auto_simulator import leverage_capped_stop
        # scalp: 3% ceiling clamps a 5% signal stop
        assert leverage_capped_stop(100.0, 95.0, "long", 5, "15m") == 97.0
        # longer: 10% ceiling leaves a 5% signal stop alone
        assert leverage_capped_stop(100.0, 95.0, "long", 5, "4H") == 95.0
        # short side mirrors
        assert leverage_capped_stop(100.0, 105.0, "short", 5, "15m") == 103.0
