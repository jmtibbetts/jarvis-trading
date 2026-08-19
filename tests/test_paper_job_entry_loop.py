"""The automatic trading job must survive its own summary line.

`run()` is the ONLY thing the scheduler calls for paper trading. Everything
else in this module — mark-to-market, position management, candidate
evaluation, canonical entry — is reached through it. A test that calls those
pieces directly proves they work; it proves nothing about whether the job
that is supposed to call them can finish.
"""
import unittest
from unittest.mock import patch


class TheJobFinishesTests(unittest.TestCase):

    def test_run_completes_and_returns_a_result(self):
        """The scheduler's entry point, called the way the scheduler calls
        it: no arguments, real code path, result consumed."""
        from jobs import paper_trading as PT
        with patch.object(PT, "_get_all_prices", return_value={}), \
             patch.object(PT, "_fetch_ta", return_value={}):
            result = PT.run()
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("ok"), result)

    def test_the_summary_counters_are_reported(self):
        """The counters the summary claims to report must actually come from
        the candidate evaluation that produced them — a job that cannot name
        its own refusal counts cannot explain why it did nothing."""
        from jobs import paper_trading as PT
        with patch.object(PT, "_get_all_prices", return_value={}), \
             patch.object(PT, "_fetch_ta", return_value={}):
            result = PT.run()
        for key in ("ai_rejected", "skipped_no_price", "new_positions"):
            self.assertIn(key, result, f"{key} missing from the job result")


if __name__ == "__main__":
    unittest.main()
