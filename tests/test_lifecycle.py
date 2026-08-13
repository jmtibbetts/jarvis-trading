"""Two things that only matter because the earlier phases succeeded.

Phases 5 and 6 gave every strategy a measured expectancy. Trading whichever
bucket scores best is the classic way to lose money with statistics: those
numbers were computed on the same trades used to pick them, and with
fourteen strategies across several timeframes one WILL look excellent by
chance — and gets selected precisely because it got lucky. So the verdict
comes from data the ranking never saw.

And a setup is one thing that moves through states, not a new signal per
cycle. Measured earlier here: 12,845 of 39,235 signals were duplicate
regenerations, so every statistic computed from that table was weighting an
idea by how many scan cycles it happened to survive.
"""
import unittest

from lib.setup_state import (ACTIONABLE, ALLOWED, APPROACHING, CONFIRMED,
                             ENTRY_READY, EXPIRED, INVALIDATED, RETESTING,
                             STATES, TERMINAL, TRIGGERED, WATCHING,
                             can_transition, evaluate, is_actionable, setup_key)
from lib.strategy_lifecycle import (ACTIVE, DISABLED, EXPERIMENTAL,
                                    LIFECYCLE, MIN_OOS_TO_DISABLE,
                                    MIN_OOS_TRADES, REDUCED, SHADOW,
                                    SIZE_MULTIPLIER, classify, split_by_time,
                                    state_of, may_trade)


def ta(*, to_resistance=None, to_support=None, breaks=None):
    return {
        "atr_distances": {k: v for k, v in
                          (("to_resistance", to_resistance),
                           ("to_support", to_support)) if v is not None},
        "structure": {"levels": [], "divergences": [], "breaks": breaks or []},
    }


def brk(outcome, direction="up", price=120.0):
    return {"outcome": outcome, "direction": direction, "level_price": price,
            "bars_ago": 1, "detail": f"{outcome}"}


class SetupIdentityTests(unittest.TestCase):
    """The dedup this exists for fails silently if identity is unstable."""

    def test_the_same_setup_hashes_the_same(self):
        a = setup_key("BTC/USD", "Long", "4H", "breakout", 63860.60)
        b = setup_key("BTC/USD", "Long", "4H", "breakout", 63860.60)
        self.assertEqual(a, b)

    def test_a_recomputed_level_does_not_create_a_new_setup(self):
        """63,860.61 vs 63,860.60 is the same level. Treating them as
        different is exactly how duplicate regenerations happened."""
        a = setup_key("BTC/USD", "Long", "4H", "breakout", 63860.60)
        b = setup_key("BTC/USD", "Long", "4H", "breakout", 63860.61)
        self.assertEqual(a, b)

    def test_a_genuinely_different_level_is_a_different_setup(self):
        a = setup_key("BTC/USD", "Long", "4H", "breakout", 63860.0)
        b = setup_key("BTC/USD", "Long", "4H", "breakout", 71000.0)
        self.assertNotEqual(a, b)

    def test_direction_and_timeframe_are_part_of_identity(self):
        base = ("BTC/USD", "Long", "4H", "breakout", 63860.0)
        self.assertNotEqual(setup_key(*base),
                            setup_key("BTC/USD", "Short", "4H", "breakout", 63860.0))
        self.assertNotEqual(setup_key(*base),
                            setup_key("BTC/USD", "Long", "1D", "breakout", 63860.0))

    def test_case_does_not_split_a_setup(self):
        self.assertEqual(setup_key("btc/usd", "long", "4H", "breakout", 100.0),
                         setup_key("BTC/USD", "Long", "4H", "breakout", 100.0))


class TransitionsAreConstrainedTests(unittest.TestCase):
    """A bug must not walk a setup straight to ENTRY_READY and skip the
    trigger that was supposed to justify it."""

    def test_watching_cannot_jump_to_entry_ready(self):
        self.assertFalse(can_transition(WATCHING, ENTRY_READY))

    def test_terminal_states_are_terminal(self):
        for t in TERMINAL:
            self.assertEqual(ALLOWED[t], ())
            for s in STATES:
                if s != t:
                    self.assertFalse(can_transition(t, s), f"{t} -> {s}")

    def test_every_state_is_reachable_and_declared(self):
        for s in STATES:
            self.assertIn(s, ALLOWED)
        declared = set(ALLOWED) | {n for v in ALLOWED.values() for n in v}
        self.assertEqual(declared, set(STATES))

    def test_staying_put_is_always_allowed(self):
        for s in STATES:
            self.assertTrue(can_transition(s, s))

    def test_anything_may_invalidate_or_expire(self):
        for s in STATES:
            if s in TERMINAL:
                continue
            self.assertIn(INVALIDATED, ALLOWED[s], s)
            self.assertIn(EXPIRED, ALLOWED[s], s)


class StateFollowsThePriceTests(unittest.TestCase):

    def test_far_from_the_level_is_watching(self):
        r = evaluate(WATCHING, ta=ta(to_resistance=6.0), direction="Long", level=120.0)
        self.assertEqual(r["state"], WATCHING)

    def test_closing_in_is_approaching(self):
        r = evaluate(WATCHING, ta=ta(to_resistance=1.2), direction="Long", level=120.0)
        self.assertEqual(r["state"], APPROACHING)
        self.assertTrue(r["changed"])

    def test_backing_off_returns_to_watching(self):
        r = evaluate(APPROACHING, ta=ta(to_resistance=5.0), direction="Long", level=120.0)
        self.assertEqual(r["state"], WATCHING)

    def test_taking_the_level_triggers(self):
        r = evaluate(APPROACHING, ta=ta(breaks=[brk("held")]), direction="Long", level=120.0)
        self.assertEqual(r["state"], TRIGGERED)

    def test_a_holding_break_confirms(self):
        r = evaluate(TRIGGERED, ta=ta(breaks=[brk("held")]), direction="Long", level=120.0)
        self.assertEqual(r["state"], CONFIRMED)

    def test_a_sweep_invalidates_rather_than_confirming(self):
        """The premise was that the level would give way. It did not."""
        r = evaluate(TRIGGERED, ta=ta(breaks=[brk("sweep")]), direction="Long", level=120.0)
        self.assertEqual(r["state"], INVALIDATED)

    def test_a_failed_break_invalidates(self):
        r = evaluate(CONFIRMED, ta=ta(breaks=[brk("failed")]), direction="Long", level=120.0)
        self.assertEqual(r["state"], INVALIDATED)

    def test_a_retest_that_holds_becomes_entry_ready(self):
        r = evaluate(RETESTING, ta=ta(breaks=[brk("held")]), direction="Long", level=120.0)
        self.assertEqual(r["state"], ENTRY_READY)

    def test_running_out_of_time_expires(self):
        r = evaluate(APPROACHING, ta=ta(to_resistance=1.0), direction="Long",
                     level=120.0, bars_elapsed=100, max_bars=50)
        self.assertEqual(r["state"], EXPIRED)

    def test_a_terminal_setup_stops_moving(self):
        for t in TERMINAL:
            r = evaluate(t, ta=ta(breaks=[brk("held")]), direction="Long", level=120.0)
            self.assertEqual(r["state"], t)
            self.assertFalse(r["changed"])

    def test_only_resolved_states_are_actionable(self):
        """WATCHING and APPROACHING are awareness, not trades — surfacing
        them as signals is what produced a stream of near-identical cards
        for one idea getting closer."""
        for s in (WATCHING, APPROACHING, TRIGGERED, INVALIDATED, EXPIRED):
            self.assertFalse(is_actionable(s), s)
        for s in ACTIONABLE:
            self.assertTrue(is_actionable(s), s)

    def test_junk_input_does_not_raise(self):
        for bad in ({}, {"structure": None}, {"atr_distances": "x"}):
            self.assertIn(evaluate(WATCHING, ta=bad, direction="Long",
                                   level=None)["state"], STATES)


class OutOfSampleSplitTests(unittest.TestCase):

    def _rows(self, n):
        return [{"r": 1.0, "exited_at": f"2026-01-{i + 1:02d}"} for i in range(n)]

    def test_the_split_is_by_time_not_at_random(self):
        """A random split leaks: trades from the same day on the same symbol
        land on both sides, so validation has already seen the conditions it
        is meant to judge blind."""
        train, validate = split_by_time(self._rows(10))
        self.assertTrue(all(t["exited_at"] < v["exited_at"]
                            for t in train for v in validate))

    def test_both_sides_are_non_empty_for_any_usable_input(self):
        for n in (2, 3, 10, 100):
            train, validate = split_by_time(self._rows(n))
            self.assertTrue(train, n)
            self.assertTrue(validate, n)

    def test_a_single_trade_cannot_be_split(self):
        train, validate = split_by_time(self._rows(1))
        self.assertEqual(validate, [])


class LifecycleVerdictTests(unittest.TestCase):

    def _oos(self, trades, expected_r):
        return {"trades": trades, "expected_r": expected_r, "p_win": 0.5,
                "avg_win_r": 1.0, "avg_loss_r": 1.0}

    def test_a_profitable_out_of_sample_strategy_is_active(self):
        r = classify(self._oos(200, 0.3), self._oos(100, 0.25))
        self.assertEqual(r["state"], ACTIVE)
        self.assertEqual(r["size_multiplier"], 1.0)

    def test_a_marginal_one_is_reduced(self):
        r = classify(self._oos(200, 0.3), self._oos(100, 0.03))
        self.assertEqual(r["state"], REDUCED)
        self.assertLess(r["size_multiplier"], 1.0)
        self.assertGreater(r["size_multiplier"], 0.0)

    def test_too_few_trades_is_experimental_not_a_verdict(self):
        r = classify(self._oos(200, 0.3), self._oos(MIN_OOS_TRADES - 1, -0.9))
        self.assertEqual(r["state"], EXPERIMENTAL)

    def test_negative_but_thin_evidence_shadows_rather_than_disabling(self):
        """A disabled strategy stops producing data, so nothing ever
        revises the judgement that disabled it."""
        r = classify(self._oos(200, 0.3), self._oos(MIN_OOS_TRADES + 5, -0.2))
        self.assertEqual(r["state"], SHADOW)
        self.assertEqual(r["size_multiplier"], 0.0)

    def test_negative_with_enough_evidence_disables(self):
        r = classify(self._oos(200, 0.3), self._oos(MIN_OOS_TO_DISABLE + 10, -0.4))
        self.assertEqual(r["state"], DISABLED)
        self.assertEqual(r["size_multiplier"], 0.0)

    def test_curve_fitting_is_named(self):
        """Good in training, not out of it — the single most useful thing
        this module can detect."""
        r = classify(self._oos(200, 0.8), self._oos(100, -0.1))
        self.assertTrue(r["overfitted"])
        self.assertGreater(r["overfit_gap_r"], 0)

    def test_a_consistently_good_strategy_is_not_called_overfitted(self):
        r = classify(self._oos(200, 0.30), self._oos(100, 0.28))
        self.assertFalse(r["overfitted"])

    def test_the_verdict_comes_from_out_of_sample_not_in_sample(self):
        """Identical training numbers, opposite validation — opposite
        verdicts. If in-sample leaked in, these would agree."""
        good = classify(self._oos(200, 0.9), self._oos(100, 0.4))
        bad = classify(self._oos(200, 0.9), self._oos(MIN_OOS_TO_DISABLE + 10, -0.4))
        self.assertEqual(good["state"], ACTIVE)
        self.assertEqual(bad["state"], DISABLED)

    def test_every_state_has_a_size_and_a_reason(self):
        for s in LIFECYCLE:
            self.assertIn(s, SIZE_MULTIPLIER)
        r = classify(self._oos(200, 0.3), self._oos(100, 0.25))
        self.assertTrue(r["reason"])

    def test_shadow_and_disabled_never_trade(self):
        self.assertEqual(SIZE_MULTIPLIER[SHADOW], 0.0)
        self.assertEqual(SIZE_MULTIPLIER[DISABLED], 0.0)


class UnknownIsNotBadTests(unittest.TestCase):
    """Refusing everything unmeasured is how the system stops generating the
    evidence it needs — the deadlock this codebase has already hit once."""

    def test_a_never_seen_strategy_is_allowed_at_small_size(self):
        st = state_of("a_strategy_that_has_never_traded", cache={"strategies": {}})
        self.assertEqual(st["state"], EXPERIMENTAL)
        self.assertGreater(st["size_multiplier"], 0.0)

    def test_may_trade_agrees_with_the_size_multiplier(self):
        empty = {"strategies": {}}
        self.assertTrue(may_trade("anything", cache=empty))
        disabled = {"strategies": {"x": {"state": DISABLED, "size_multiplier": 0.0}}}
        self.assertFalse(may_trade("x", cache=disabled))


if __name__ == "__main__":
    unittest.main()
