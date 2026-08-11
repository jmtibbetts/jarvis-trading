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
