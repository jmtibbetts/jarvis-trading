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


class TestPaperMarginSizing:
    """Margin-first sizing: the trade amount IS the committed capital.
    A $10 trade at 10x controls $100 but is still a $10 trade."""

    def test_leverage_ladder_2_to_20(self):
        from lib.paper_engine import score_leverage
        assert score_leverage(55) == 2
        assert score_leverage(100) == 20
        assert score_leverage(0) == 2           # floor
        prev = 0
        for sc in range(55, 101):
            lev = score_leverage(sc)
            assert lev >= prev and 2 <= lev <= 20
            prev = lev

    def test_ten_dollars_at_ten_x_is_a_ten_dollar_trade(self):
        from lib.paper_engine import size_position
        r = size_position(equity=1000, entry=100.0, stop=98.0, leverage=10,
                          free_cash=1000, margin_override=10)
        assert r["ok"]
        assert r["margin"] == 10                 # $10 committed, not $100
        assert r["notional"] == 100              # $100 of exposure
        assert r["qty"] == 1.0
        assert r["loss_at_stop"] == 2.0          # a 2% move costs 20% of the $10

    def test_leverage_scales_exposure_not_committed_capital(self):
        from lib.paper_engine import size_position
        low = size_position(100_000, 100.0, 98.0, 2, 100_000)
        high = size_position(100_000, 100.0, 98.0, 20, 100_000)
        assert low["margin"] == high["margin"] == 1000     # same capital committed
        assert high["notional"] == low["notional"] * 10    # 10x the exposure
        assert high["loss_at_stop"] == low["loss_at_stop"] * 10

    def test_notional_never_exceeds_committed_times_leverage(self):
        from lib.paper_engine import size_position
        r = size_position(100_000, 250.0, 245.0, 15, 100_000)
        assert r["notional"] <= r["margin"] * r["leverage"] + 1e-6

    def test_margin_capped_by_free_cash(self):
        from lib.paper_engine import size_position
        r = size_position(1_000_000, 100.0, 99.0, 5, 10_000)   # big equity, little cash
        assert r["ok"] and r["capped_by_cash"]
        assert r["margin"] <= 10_000 * 0.15 + 0.01

    def test_rejects_impossible_sizing(self):
        from lib.paper_engine import size_position
        assert size_position(0, 100.0, 99.0, 5, 100_000)["ok"] is False      # no equity
        assert size_position(100_000, 0, 99.0, 5, 100_000)["ok"] is False    # no entry


class TestPortfolioCapacityLimits:
    """Per-trade sizing does not bound a portfolio — 1% each x 86 trades
    committed 99.7% of the paper account. These are the portfolio caps."""

    def test_paper_caps_are_sane(self):
        from lib.paper_engine import MAX_DEPLOYED_PCT, MAX_OPEN_POSITIONS, TRADE_MARGIN_PCT
        assert 0 < MAX_DEPLOYED_PCT <= 100
        assert MAX_OPEN_POSITIONS > 0
        # Neither cap may be decorative: at the per-trade size, the position
        # count must reach the deployment ceiling rather than trip first.
        assert MAX_OPEN_POSITIONS * TRADE_MARGIN_PCT >= MAX_DEPLOYED_PCT
        # ...and the account must never be fully committed.
        assert MAX_DEPLOYED_PCT < 100

    def test_autosim_caps_are_sane(self):
        from lib.auto_simulator import MAX_DEPLOYED_PCT, MAX_OPEN_POSITIONS, MARGIN_PER_SIGNAL
        assert 0 < MAX_DEPLOYED_PCT <= 100
        # 60 positions x $1,000 = $60,000 = exactly the 60% ceiling on $100k,
        # so neither limit is decorative.
        assert MAX_OPEN_POSITIONS * MARGIN_PER_SIGNAL >= 100_000 * (MAX_DEPLOYED_PCT / 100)

    def test_paper_refuses_beyond_deployment_cap(self, monkeypatch):
        """A book already at the ceiling must reject new entries with a
        reason, not silently drain to zero cash."""
        import lib.paper_engine as pe

        class _Row:
            status = "Open"
            margin_used = 60_000.0

        class _Q:
            def filter(self, *a, **k): return self
            def all(self): return [_Row()]
            def first(self): return None

        class _DB:
            def query(self, *a, **k): return _Q()
            def add(self, *a, **k): pass
            def flush(self): pass

        import contextlib

        @contextlib.contextmanager
        def _fake_db():
            yield _DB()

        monkeypatch.setattr(pe, "get_db", _fake_db)
        monkeypatch.setattr(pe, "_get_portfolio_cash", lambda db: type("P", (), {"cash": 40_000.0})())
        res = pe.open_paper_position(
            {"asset_symbol": "TEST", "direction": "Long", "entry_price": 100.0,
             "stop_loss": 98.0, "target_price": 106.0, "composite_score": 70},
            current_price=100.0,
        )
        assert "error" in res
        assert "cap" in res["error"].lower() or "full" in res["error"].lower()
