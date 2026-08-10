"""Tests for lib/backtester.py and the get_cached_range() cache helper.

No test in this file makes a real network call. Cache tests patch
lib.ohlcv_cache.get_cache_db to point at an in-memory SQLite engine (same
pattern as tests/test_slippage.py). The walk-forward / signal-construction
tests build synthetic OHLCV DataFrames by hand in Python and call the
network-free helpers (evaluate_checkpoint / walk_symbol) directly — they never
touch lib.ohlcv_cache.backfill_symbol, yfinance, or run_backtest's
orchestration layer.
"""
import random
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from lib.ohlcv_cache import CacheBase, OHLCVBar, get_cached_range
from lib.backtester import evaluate_checkpoint, walk_symbol, compute_equity_curve


# ── synthetic OHLCV builders (no cache, no network) ─────────────────────────

def _make_frame(n: int, freq: str, start: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    rows = []
    for c in closes:
        rows.append({
            "open": c * 0.999,
            "high": c * 1.004,
            "low": c * 0.996,
            "close": c,
            "volume": 10_000.0,
        })
    df = pd.DataFrame(rows, index=idx)
    return df


def _uptrend_closes(n: int, base: float = 100.0, pct_per_bar: float = 0.006) -> list[float]:
    return [base * ((1 + pct_per_bar) ** i) for i in range(n)]


def _choppy_closes(n: int, base: float = 100.0, seed: int = 7, amplitude_pct: float = 0.01) -> list[float]:
    """IID random noise bounded tightly around `base` — no persistent drift
    AND no bar-to-bar momentum/autocorrelation (unlike a mean-reverting walk,
    which still produces short smooth runs that a momentum-based TA engine
    can — correctly! — pick up on; and unlike a perfectly flat/constant
    series, which is a separate degenerate case: a zero MACD histogram
    deterministically reads as 'bearish' in lib.ta_engine's bias formula, and
    RSI on zero variance is NaN). Deterministic seed keeps the test
    reproducible.

    Because build_ta_fallback_signals only requires its OWN two timeframes
    to agree, any single checkpoint has a roughly coin-flip chance of firing
    even on pure noise (each timeframe's 5-factor bias formula is rarely
    exactly neutral) — that is expected and not itself a bug. What actually
    keeps a genuinely non-trending series from generating a runaway number of
    *resolved* trades is walk_symbol's one-open-trade-at-a-time rule: once a
    hypothetical trade opens, it must hit its target or stop before another
    can open, and tight noise like this rarely travels far enough to hit
    either — so the position mostly just sits OPEN, blocking further entries
    for the rest of the walk. That full-walk behavior (not isolated
    per-checkpoint sampling) is what tests/test_backtester.py's
    WalkSymbolTests.test_flat_market_produces_zero_or_very_few_trades checks.
    """
    rng = random.Random(seed)
    return [base * (1 + rng.uniform(-amplitude_pct, amplitude_pct)) for _ in range(n)]


def _uptrend_frames(n_1d: int = 120, n_4h: int = 800) -> dict[str, pd.DataFrame]:
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    start_1d = end - timedelta(days=n_1d)
    start_4h = end - timedelta(hours=4 * n_4h)
    return {
        "1D": _make_frame(n_1d, "1D", start_1d.isoformat(), _uptrend_closes(n_1d, pct_per_bar=0.01)),
        "4H": _make_frame(n_4h, "4h", start_4h.isoformat(), _uptrend_closes(n_4h, pct_per_bar=0.0015)),
    }


def _flat_frames(n_1d: int = 120, n_4h: int = 800) -> dict[str, pd.DataFrame]:
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    start_1d = end - timedelta(days=n_1d)
    start_4h = end - timedelta(hours=4 * n_4h)
    return {
        "1D": _make_frame(n_1d, "1D", start_1d.isoformat(), _choppy_closes(n_1d, seed=7)),
        "4H": _make_frame(n_4h, "4h", start_4h.isoformat(), _choppy_closes(n_4h, seed=13)),
    }


# ── evaluate_checkpoint: single-checkpoint decision logic ──────────────────

class EvaluateCheckpointTests(unittest.TestCase):
    def test_strong_uptrend_produces_a_long_candidate(self):
        frames = _uptrend_frames()
        checkpoint = frames["4H"].index[-1].to_pydatetime()
        result = evaluate_checkpoint("TESTUP", frames, checkpoint, min_composite_score=0.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "Long")
        self.assertIn("composite_score", result)

    def test_strong_uptrend_clears_the_default_composite_gate(self):
        frames = _uptrend_frames()
        checkpoint = frames["4H"].index[-1].to_pydatetime()
        result = evaluate_checkpoint("TESTUP", frames, checkpoint, min_composite_score=55.0)
        self.assertIsNotNone(result, "a clean, fresh uptrend should clear the default 55 composite gate")
        self.assertGreaterEqual(result["composite_score"], 55.0)

    def test_flat_market_candidates_show_no_systematic_directional_bias(self):
        # A clean uptrend deterministically fires Long every time (see
        # test_strong_uptrend_produces_a_long_candidate). If the deterministic
        # TA-fallback pipeline were "over-triggering" — manufacturing a fake
        # directional edge out of pure noise — we'd expect isolated checkpoints
        # sampled across choppy/sideways data to skew the same way too. They
        # don't: build_ta_fallback_signals only reacts to each timeframe's own
        # short-term momentum, so on IID noise the direction it picks is
        # effectively a coin flip. (The full-walk trade *count* sanity check
        # against over-triggering lives in
        # WalkSymbolTests.test_flat_market_produces_zero_or_very_few_trades —
        # see _choppy_closes()'s docstring for why that is the more meaningful
        # level to check signal *volume* at.)
        frames = _flat_frames()
        d1 = frames["1D"]
        sample_points = d1.index[-15::3]  # every 3rd day over the last ~45 days
        directions = []
        for cp in sample_points:
            cp_dt = cp.to_pydatetime()
            sliced = {tf: df[df.index <= cp] for tf, df in frames.items()}
            result = evaluate_checkpoint("TESTFLAT", sliced, cp_dt, min_composite_score=0.0)
            if result is not None:
                directions.append(result["direction"])
        self.assertGreater(len(directions), 0, "expected at least one candidate across the sampled checkpoints")
        self.assertGreater(
            len(set(directions)), 1,
            f"choppy/sideways noise produced only {set(directions)} — expected a mix, not a systematic bias",
        )

    def test_insufficient_bars_returns_none(self):
        frames = _uptrend_frames(n_1d=90, n_4h=400)
        thin = {"1D": frames["1D"].tail(5), "4H": frames["4H"].tail(5)}
        checkpoint = thin["4H"].index[-1].to_pydatetime()
        result = evaluate_checkpoint("TESTTHIN", thin, checkpoint, min_composite_score=0.0)
        self.assertIsNone(result)

    def test_single_valid_timeframe_returns_none(self):
        frames = _uptrend_frames()
        only_one = {"4H": frames["4H"]}
        checkpoint = frames["4H"].index[-1].to_pydatetime()
        result = evaluate_checkpoint("TESTONE", only_one, checkpoint, min_composite_score=0.0)
        self.assertIsNone(result)


# ── walk_symbol: multi-checkpoint walk-forward, no I/O ──────────────────────

class WalkSymbolTests(unittest.TestCase):
    def test_uptrend_generates_at_least_one_resolved_trade(self):
        frames = _uptrend_frames()
        checkpoints = list(frames["1D"].index)
        trades = walk_symbol("TESTUP", frames, checkpoints, "1D", "4H", min_composite_score=55.0)
        self.assertGreater(len(trades), 0)
        for t in trades:
            self.assertIn(t["outcome"], {"TARGET_HIT", "STOP_HIT", "AMBIGUOUS", "OPEN", "EXPIRED", "INVALID_DATA"})
        # A strong, low-noise uptrend should resolve mostly to TARGET_HIT.
        decided = [t for t in trades if t["outcome"] in ("TARGET_HIT", "STOP_HIT")]
        if decided:
            wins = sum(1 for t in decided if t["outcome"] == "TARGET_HIT")
            self.assertGreaterEqual(wins / len(decided), 0.5)

    def test_only_one_open_trade_at_a_time(self):
        frames = _uptrend_frames()
        checkpoints = list(frames["1D"].index)
        trades = walk_symbol("TESTUP", frames, checkpoints, "1D", "4H", min_composite_score=55.0)
        # Each trade's generated_at must be strictly after the previous trade's
        # resolution — i.e. the walk never opens a second hypothetical trade
        # while one is still open.
        for prev, nxt in zip(trades, trades[1:]):
            prev_resolved = prev.get("resolution_date") or ""
            next_opened = nxt.get("generated_at") or ""
            self.assertLessEqual(prev_resolved, next_opened)

    def test_flat_market_produces_zero_or_very_few_trades(self):
        # Sanity check against over-triggering. Tight IID noise has nowhere
        # near enough range to travel the ATR-derived target/stop distance a
        # candidate signal is opened with, so after the first (if any)
        # hypothetical trade opens it just sits OPEN for the rest of the
        # series — walk_symbol's one-trade-at-a-time rule then blocks any
        # further entries. See _choppy_closes()'s docstring for the full
        # reasoning on why this — not isolated per-checkpoint sampling — is
        # the meaningful level to check signal *volume* at.
        frames = _flat_frames()
        checkpoints = list(frames["1D"].index)
        trades = walk_symbol("TESTFLAT", frames, checkpoints, "1D", "4H", min_composite_score=55.0)
        self.assertLessEqual(len(trades), 2)

    def test_no_lookahead_bars_after_checkpoint_are_never_visible(self):
        # Regression guard: feed walk_symbol frames that go far beyond the
        # checkpoint list, and confirm a checkpoint mid-series only ever sees
        # bars up to and including itself. We do this by checking that
        # evaluate_checkpoint is invoked (indirectly) with a bars_by_tf slice
        # whose max index never exceeds the checkpoint — verified here by
        # patching evaluate_checkpoint to record what it saw. Checkpoints are
        # taken after the MIN_BARS_FOR_TA warm-up (the first ~20 daily bars
        # never reach evaluate_checkpoint at all — insufficient history — so
        # sampling those wouldn't exercise the thing being tested).
        frames = _uptrend_frames()
        checkpoints = list(frames["1D"].index)[25:35]
        seen_max_ts = []

        import lib.backtester as backtester_mod
        original = backtester_mod.evaluate_checkpoint

        def spy(symbol, bars_by_tf, checkpoint, **kwargs):
            for tf, df in bars_by_tf.items():
                seen_max_ts.append((checkpoint, df.index.max()))
            return original(symbol, bars_by_tf, checkpoint, **kwargs)

        with patch.object(backtester_mod, "evaluate_checkpoint", side_effect=spy):
            backtester_mod.walk_symbol("TESTUP", frames, checkpoints, "1D", "4H", min_composite_score=0.0)

        self.assertTrue(seen_max_ts, "expected at least one checkpoint to have enough bars")
        for checkpoint, max_seen in seen_max_ts:
            self.assertLessEqual(max_seen, pd.Timestamp(checkpoint))


# ── compute_equity_curve: pure R-multiple compounding math ─────────────────

class ComputeEquityCurveTests(unittest.TestCase):
    def test_wins_and_losses_compound_equity_by_r_multiple(self):
        trades = [
            {  # entry 100, stop 95 -> stop_distance_pct=5; mfe 10 -> R=2
                "entry_price": 100.0, "stop_loss": 95.0, "outcome": "TARGET_HIT",
                "mfe_pct": 10.0, "mae_pct": -1.0, "resolution_date": "2026-01-02T00:00:00+00:00",
            },
            {  # entry 100, stop 95 -> stop_distance_pct=5; mae -5 -> R=-1
                "entry_price": 100.0, "stop_loss": 95.0, "outcome": "STOP_HIT",
                "mfe_pct": 1.0, "mae_pct": -5.0, "resolution_date": "2026-01-05T00:00:00+00:00",
            },
        ]
        curve, final_equity = compute_equity_curve(trades, starting_equity=10000.0, risk_pct_per_trade=1.0)
        # trade 1: R=2 -> equity *= 1 + 0.01*2 = 1.02 -> 10200
        # trade 2: R=-1 -> equity *= 1 + 0.01*-1 = 0.99 -> 10098
        self.assertAlmostEqual(final_equity, 10000.0 * 1.02 * 0.99, places=4)
        self.assertEqual([d for d, _ in curve], ["2026-01-02", "2026-01-05"])

    def test_open_and_ambiguous_trades_do_not_affect_equity(self):
        trades = [
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "OPEN",
             "mfe_pct": 50.0, "mae_pct": -50.0, "resolution_date": "2026-01-01T00:00:00+00:00"},
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "AMBIGUOUS",
             "mfe_pct": 50.0, "mae_pct": -50.0, "resolution_date": "2026-01-02T00:00:00+00:00"},
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "INVALID_DATA",
             "mfe_pct": 50.0, "mae_pct": -50.0, "resolution_date": "2026-01-03T00:00:00+00:00"},
        ]
        curve, final_equity = compute_equity_curve(trades, starting_equity=10000.0)
        self.assertEqual(curve, [])
        self.assertEqual(final_equity, 10000.0)

    def test_zero_stop_distance_is_skipped_not_a_crash(self):
        trades = [
            {"entry_price": 100.0, "stop_loss": 100.0, "outcome": "TARGET_HIT",
             "mfe_pct": 10.0, "mae_pct": 0.0, "resolution_date": "2026-01-01T00:00:00+00:00"},
        ]
        curve, final_equity = compute_equity_curve(trades, starting_equity=10000.0)
        self.assertEqual(final_equity, 10000.0)
        self.assertEqual(curve, [])

    def test_same_day_resolutions_keep_the_last_one(self):
        trades = [
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "TARGET_HIT",
             "mfe_pct": 5.0, "mae_pct": -1.0, "resolution_date": "2026-01-01T09:00:00+00:00"},
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "STOP_HIT",
             "mfe_pct": 1.0, "mae_pct": -5.0, "resolution_date": "2026-01-01T15:00:00+00:00"},
        ]
        curve, final_equity = compute_equity_curve(trades, starting_equity=10000.0, risk_pct_per_trade=1.0)
        self.assertEqual(len(curve), 1)
        self.assertEqual(curve[0][0], "2026-01-01")
        # Both trades applied in chronological order, last curve point == final equity
        self.assertAlmostEqual(curve[0][1], final_equity, places=6)

    def test_chronological_ordering_is_enforced_regardless_of_input_order(self):
        trades = [
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "STOP_HIT",
             "mfe_pct": 1.0, "mae_pct": -5.0, "resolution_date": "2026-01-05T00:00:00+00:00"},
            {"entry_price": 100.0, "stop_loss": 95.0, "outcome": "TARGET_HIT",
             "mfe_pct": 10.0, "mae_pct": -1.0, "resolution_date": "2026-01-02T00:00:00+00:00"},
        ]
        curve, final_equity = compute_equity_curve(trades, starting_equity=10000.0, risk_pct_per_trade=1.0)
        self.assertEqual([d for d, _ in curve], ["2026-01-02", "2026-01-05"])
        self.assertAlmostEqual(final_equity, 10000.0 * 1.02 * 0.99, places=4)


# ── get_cached_range: pure cache read against an in-memory SQLite engine ────

class GetCachedRangeTests(unittest.TestCase):
    def _session_factory(self):
        engine = create_engine("sqlite:///:memory:")
        CacheBase.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        @contextmanager
        def closed_session():
            try:
                yield session
                session.commit()
            finally:
                pass

        return engine, session, closed_session

    def test_returns_bars_within_range_sorted_ascending(self):
        engine, session, closed_session = self._session_factory()
        try:
            rows = [
                ("2026-01-01T00:00:00+00:00", 10.0),
                ("2026-01-03T00:00:00+00:00", 12.0),
                ("2026-01-02T00:00:00+00:00", 11.0),  # inserted out of order
                ("2025-12-31T00:00:00+00:00", 9.0),   # before range
                ("2026-01-10T00:00:00+00:00", 20.0),  # after range
            ]
            for ts, close in rows:
                session.add(OHLCVBar(
                    symbol="AAPL", timeframe="1D", ts=ts,
                    open=close, high=close + 0.5, low=close - 0.5, close=close,
                    volume=1000.0, source="yfinance",
                ))
            session.commit()

            import lib.ohlcv_cache as cache_mod
            with patch.object(cache_mod, "get_cache_db", closed_session):
                df = get_cached_range(
                    "AAPL", "1D",
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 1, 3, tzinfo=timezone.utc),
                )
            self.assertIsNotNone(df)
            self.assertEqual(len(df), 3)
            self.assertListEqual(list(df["close"]), [10.0, 11.0, 12.0])
            self.assertTrue(df.index.is_monotonic_increasing)
        finally:
            engine.dispose()

    def test_no_rows_in_range_returns_none(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(OHLCVBar(
                symbol="AAPL", timeframe="1D", ts="2025-01-01T00:00:00+00:00",
                open=1, high=1, low=1, close=1, volume=1, source="yfinance",
            ))
            session.commit()

            import lib.ohlcv_cache as cache_mod
            with patch.object(cache_mod, "get_cache_db", closed_session):
                df = get_cached_range(
                    "AAPL", "1D",
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    datetime(2026, 1, 3, tzinfo=timezone.utc),
                )
            self.assertIsNone(df)
        finally:
            engine.dispose()

    def test_crypto_symbol_normalization_matches_slash_usd_storage(self):
        engine, session, closed_session = self._session_factory()
        try:
            session.add(OHLCVBar(
                symbol="BTC/USD", timeframe="4H", ts="2026-01-01T00:00:00+00:00",
                open=50000, high=50100, low=49900, close=50050, volume=5, source="yfinance",
            ))
            session.commit()

            import lib.ohlcv_cache as cache_mod
            with patch.object(cache_mod, "get_cache_db", closed_session):
                df = get_cached_range(
                    "BTC/USD", "4H",
                    datetime(2025, 12, 31, tzinfo=timezone.utc),
                    datetime(2026, 1, 2, tzinfo=timezone.utc),
                )
            self.assertIsNotNone(df)
            self.assertEqual(len(df), 1)
            self.assertEqual(float(df["close"].iloc[0]), 50050.0)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
