"""Regression tests for the MACD crossover truthiness bug.

compute_timeframe emits "bullish" / "bearish" / "none". "none" is a
NON-EMPTY string and therefore TRUTHY, so `if macd["crossover"]` reported a
crossover on every bar that had none. Live impact, both confirmed:

  - lib/ta_engine.py compact prompt marked "X" (crossover) for EVERY symbol
    in EVERY LLM batch, so the model reasoned on a signal that did not exist
  - lib/learning_engine.py stamped a crossover into EVERY pattern signature,
    so pattern memory was keyed on the same phantom
"""
import unittest

from lib.ta_engine import has_crossover, crossover_direction, CROSSOVER_NONE


class CrossoverTruthinessTests(unittest.TestCase):
    def test_none_is_not_a_crossover(self):
        self.assertFalse(has_crossover({"crossover": "none"}))
        self.assertIsNone(crossover_direction({"crossover": "none"}))

    def test_the_bare_string_is_truthy_which_is_why_this_bug_existed(self):
        self.assertTrue(bool(CROSSOVER_NONE))          # the trap
        self.assertFalse(has_crossover({"crossover": CROSSOVER_NONE}))  # the fix

    def test_real_crossovers_are_detected(self):
        self.assertTrue(has_crossover({"crossover": "bullish"}))
        self.assertTrue(has_crossover({"crossover": "bearish"}))
        self.assertEqual(crossover_direction({"crossover": "bullish"}), "bullish")
        self.assertEqual(crossover_direction({"crossover": "bearish"}), "bearish")

    def test_missing_and_empty_are_safe(self):
        for macd in ({}, None, {"crossover": None}, {"crossover": ""}):
            self.assertFalse(has_crossover(macd))
            self.assertIsNone(crossover_direction(macd))

    def test_prompt_does_not_mark_a_crossover_when_there_is_none(self):
        """The compact LLM block must not show 'X' for crossover='none'."""
        import inspect
        from lib import ta_engine
        src = inspect.getsource(ta_engine)
        self.assertIn('mc   = "X" if has_crossover(macd) else ""', src)
        self.assertNotIn('"X" if macd.get("crossover") else ""', src)

    def test_pattern_signature_does_not_claim_a_phantom_crossover(self):
        import inspect
        from lib import learning_engine
        src = inspect.getsource(learning_engine)
        self.assertNotIn('macd.get("crossover", False)', src)


if __name__ == "__main__":
    unittest.main()
