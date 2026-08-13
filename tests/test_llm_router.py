"""Nothing chose the thinking mode before this — a default chose.

`call_lm_studio(thinking=True)` was the signature. Fourteen call sites, three
passed it, and the other eleven bought chain-of-thought for news tagging and
ticker triage because nobody typed a parameter. These tests pin the two
properties that matter: the decision is always explicit and deterministic,
and the model is never the source of a number that arithmetic owns.
"""
import unittest

from lib import llm_router as R


class ModesAreExplicitTests(unittest.TestCase):
    def test_fast_and_deep_are_obeyed_verbatim(self):
        self.assertFalse(R.route("postmortem", R.FAST).thinking)
        self.assertTrue(R.route("extraction", R.DEEP).thinking)

    def test_an_unknown_mode_falls_back_to_auto_rather_than_to_deep(self):
        """The old failure mode was a silent default to the expensive path."""
        self.assertFalse(R.route("extraction", "TURBO").thinking)

    def test_an_unknown_task_still_routes(self):
        r = R.route("something_new", R.AUTO)
        self.assertIn(r.resolved_mode, (R.FAST, R.DEEP))

    def test_every_decision_carries_its_reason(self):
        for task in list(R.TASKS) + ["unknown"]:
            for mode in R.MODES:
                self.assertTrue(R.route(task, mode).reason.strip(), (task, mode))


class TheTaxonomyMatchesTheStatedPolicyTests(unittest.TestCase):
    """The two lists this was built from, asserted rather than described."""

    REASONING = ["contradiction_review", "trade_review", "regime_transition",
                 "catalyst_analysis", "macro_synthesis", "cross_asset_analysis",
                 "postmortem", "hypothesis_generation", "risk_guardian"]
    TRANSCRIPTION = ["extraction", "classification", "json_formatting",
                     "ticker_detection", "summarization", "notification",
                     "sentiment", "market_state", "news_tagging"]

    def test_judgement_work_reasons_by_default(self):
        for t in self.REASONING:
            self.assertTrue(R.route(t, R.AUTO).thinking, t)

    def test_transcription_work_does_not(self):
        for t in self.TRANSCRIPTION:
            self.assertFalse(R.route(t, R.AUTO).thinking, t)

    def test_every_task_declares_a_real_mode(self):
        for name, t in R.TASKS.items():
            self.assertIn(t.default_mode, R.MODES, name)
            self.assertTrue(t.why, name)


class AutoIsDeterministicTests(unittest.TestCase):
    """AUTO reads numbers the caller already has. It never asks a model
    whether to use a model, and the same input always gives the same answer."""

    def test_the_same_context_always_decides_the_same_way(self):
        ctx = {"leverage": 12, "pnl_pct": -3}
        first = R.route("extraction", R.AUTO, ctx)
        for _ in range(20):
            r = R.route("extraction", R.AUTO, ctx)
            self.assertEqual((r.thinking, r.reason), (first.thinking, first.reason))

    def test_a_bare_transcription_task_stays_cheap(self):
        self.assertFalse(R.route("classification", R.AUTO, {}).thinking)
        self.assertFalse(R.route("classification", R.AUTO, None).thinking)


class EscalationTriggersTests(unittest.TestCase):
    """Each item from the "thinking should be ON for" list, as a trigger."""

    def _fires(self, ctx):
        return R.route("classification", R.AUTO, ctx).thinking

    def test_high_leverage(self):
        self.assertTrue(self._fires({"leverage": R.HIGH_LEVERAGE}))
        self.assertFalse(self._fires({"leverage": R.HIGH_LEVERAGE - 0.1}))

    def test_contradictory_evidence(self):
        self.assertTrue(self._fires({"contradiction_count": R.CONTRADICTION_FLOOR}))
        self.assertFalse(self._fires({"contradiction_count": R.CONTRADICTION_FLOOR - 1}))
        self.assertTrue(self._fires({"contradictory_evidence": True}))

    def test_a_position_already_deep_underwater(self):
        self.assertTrue(self._fires({"pnl_pct": R.HIGH_RISK_LOSS_PCT}))
        self.assertFalse(self._fires({"pnl_pct": -1.0}))
        self.assertFalse(self._fires({"pnl_pct": 30.0}), "a winner is not a risk trigger")

    def test_regime_transition_catalyst_and_cross_asset(self):
        for flag in ("regime_transition", "major_catalyst", "cross_asset"):
            self.assertTrue(self._fires({flag: True}), flag)
            self.assertFalse(self._fires({flag: False}), flag)

    def test_an_unclassifiable_setup(self):
        self.assertTrue(self._fires({"strategy_match": 0.2}))
        self.assertTrue(self._fires({"strategy": "UNCLASSIFIED"}))
        self.assertFalse(self._fires({"strategy_match": 0.9, "strategy": "breakout"}))

    def test_a_large_share_of_the_book(self):
        self.assertTrue(self._fires({"portfolio_pct": R.LARGE_PORTFOLIO_PCT}))
        self.assertFalse(self._fires({"portfolio_pct": 2.0}))

    def test_horizon(self):
        """A weekly commitment is worth thinking about; a 1m scalp would be
        invalidated by the latency of thinking about it."""
        self.assertTrue(self._fires({"timeframe": "1W"}))
        self.assertTrue(self._fires({"timeframe": "1D"}))
        self.assertFalse(self._fires({"timeframe": "1m"}))
        self.assertFalse(self._fires({"timeframe": "15m"}))

    def test_the_majority_timeframe_is_not_a_trigger(self):
        """4H is 60% of every signal this system emits. Escalating on it made
        AUTO mean DEEP, which is the defect this module exists to remove."""
        self.assertFalse(self._fires({"timeframe": "4H"}))
        self.assertFalse(self._fires({"timeframe": "1H"}))

    def test_the_reason_names_the_trigger_that_fired(self):
        r = R.route("classification", R.AUTO, {"leverage": 20, "regime_transition": True})
        self.assertIn("leverage", r.reason)
        self.assertIn("regime transition", r.reason)

    def test_junk_context_does_not_escalate_or_explode(self):
        for ctx in ({"leverage": "lots"}, {"pnl_pct": None}, {"leverage": True},
                    {"portfolio_pct": ""}, {"timeframe": 5}):
            self.assertFalse(self._fires(ctx), ctx)

    def test_an_explicit_fast_still_wins_over_every_trigger(self):
        """The caller is allowed to say no. Otherwise FAST would be a lie."""
        loud = {"leverage": 50, "regime_transition": True, "contradiction_count": 9}
        self.assertFalse(R.route("classification", R.FAST, loud).thinking)


class ArithmeticIsNotTheModelsJobTests(unittest.TestCase):
    """Fees, P&L, leverage, liquidation, sizing, portfolio limits, EV and
    indicators are computed. A model asked for one of these does not decline
    — it returns a plausible number that is indistinguishable from a real
    one once it is in the dict."""

    def test_a_model_supplied_fee_is_dropped(self):
        cleaned, removed = R.strip_forbidden("trade_review",
                                             {"action": "EXIT", "fee_usd": 3.20})
        self.assertNotIn("fee_usd", cleaned)
        self.assertEqual(cleaned["action"], "EXIT")
        self.assertIn("fee_usd", removed)

    def test_every_forbidden_category_is_covered(self):
        for key in ("fee", "pnl_pct", "leverage", "liquidation_price",
                    "position_size", "portfolio_pct", "expected_value", "rsi"):
            cleaned, removed = R.strip_forbidden("trade_review", {key: 1, "keep": 2})
            self.assertNotIn(key, cleaned, key)
            self.assertEqual(cleaned["keep"], 2)

    def test_it_reaches_into_nested_objects_and_lists(self):
        cleaned, _ = R.strip_forbidden("trade_review", {
            "positions": [{"symbol": "BTC/USD", "qty": 5, "reasoning": "ok"}],
            "plan": {"detail": {"margin": 400, "note": "keep"}},
        })
        self.assertNotIn("qty", cleaned["positions"][0])
        self.assertEqual(cleaned["positions"][0]["symbol"], "BTC/USD")
        self.assertNotIn("margin", cleaned["plan"]["detail"])
        self.assertEqual(cleaned["plan"]["detail"]["note"], "keep")

    def test_matching_ignores_case_and_padding(self):
        cleaned, _ = R.strip_forbidden("trade_review", {" Leverage ": 20, "PnL_Pct": 3})
        self.assertEqual(cleaned, {})

    def test_judgement_fields_survive(self):
        """Direction, confidence and reasoning are opinions, not arithmetic —
        stripping them would gut the response."""
        payload = {"direction": "Short", "confidence": 72, "reasoning": "why",
                   "assessment": "DISAGREE", "action": "HOLD", "entry_price": 100.0}
        cleaned, removed = R.strip_forbidden("trade_review", payload)
        self.assertEqual(cleaned, payload)
        self.assertEqual(removed, [])

    def test_non_dict_input_passes_through(self):
        for v in ("text", 5, None, []):
            self.assertEqual(R.strip_forbidden("trade_review", v)[0], v)


class NoOneBypassesTheRouterTests(unittest.TestCase):
    """The whole point is that the choice is stated at the call site. A new
    direct call_lm_studio() re-creates the exact defect this replaced: the
    parameter still defaults to True, so an unrouted call silently buys
    chain-of-thought and records nothing."""

    def test_the_router_is_the_only_direct_caller(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for p in root.rglob("*.py"):
            parts = set(p.parts)
            if parts & {".venv", "tests", "node_modules", "__pycache__"}:
                continue
            if p.name in ("lmstudio.py", "llm_router.py"):
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "call_lm_studio(" in line and not line.strip().startswith("#"):
                    offenders.append(f"{p.relative_to(root)}:{i}")
        self.assertEqual(offenders, [], "route these through lib/llm_router.call()")


if __name__ == "__main__":
    unittest.main()


class TheRecordedReasonIsTheTriggerTests(unittest.TestCase):
    """Callers that need a different system prompt under thinking must not
    route twice to find out. Routing to pick a prompt and passing the RESULT
    back as the mode overwrote every reason with "caller asked for DEEP" —
    seven live positions escalated and the log could not say why."""

    def test_system_deep_swaps_the_prompt_without_a_second_routing(self):
        r = R.route("summarization", R.AUTO, {"timeframe": "1D"})
        self.assertTrue(r.thinking)
        self.assertNotIn("caller asked", r.reason)
        self.assertIn("1D", r.reason)

    def test_call_accepts_system_deep(self):
        import inspect
        self.assertIn("system_deep", inspect.signature(R.call).parameters)


class RoutinePositionsStayCheapTests(unittest.TestCase):
    """"Thinking ON for high-risk positions" only means something if a
    routine position is OFF. position_management defaulted to DEEP, so the
    P&L and leverage it was handed decided nothing — every open position
    reasoned, including one drifting 0.3% off entry."""

    def test_a_quiet_position_does_not_reason(self):
        self.assertFalse(R.route("position_management", R.AUTO, {
            "pnl_pct": -0.3, "leverage": 1, "timeframe": "4H"}).thinking)

    def test_a_position_deep_underwater_does(self):
        r = R.route("position_management", R.AUTO, {
            "pnl_pct": -12.0, "leverage": 1, "timeframe": "4H"})
        self.assertTrue(r.thinking)
        self.assertIn("down 12", r.reason)

    def test_a_heavily_leveraged_position_does(self):
        self.assertTrue(R.route("position_management", R.AUTO, {
            "pnl_pct": -0.5, "leverage": 20, "timeframe": "4H"}).thinking)

    def test_a_multi_week_position_does(self):
        self.assertTrue(R.route("position_management", R.AUTO, {
            "pnl_pct": -0.5, "leverage": 1, "timeframe": "1W"}).thinking)
