"""The skip policy is a rule, so it gets tested like one.

WHY THIS FILE EXISTS. `pytest_sessionfinish` sets `session.exitstatus = 1`
when a skip is undeclared. That is the right behaviour and it is observable
in exactly one way: run a whole suite and read the EXIT CODE. Six
Bitnomial provider skips went in undeclared, the summary line still said
"3,214 passed, 10 skipped", and the run was reported as green — twice —
because the exit code was never checked.

The summary line is not the verdict. The exit code is. And a rule that can
only be checked by reading an exit code will eventually be checked by
reading something else, so the rule is now a pure function with its own
assertions.
"""
import unittest

import conftest


class TheTaxonomyIsCategoryBasedNotATotalTests(unittest.TestCase):

    def test_every_declared_category_has_a_budget_and_a_pattern(self):
        for pattern, category, budget in conftest.ALLOWED_SKIPS:
            with self.subTest(category=category):
                self.assertTrue(pattern)
                self.assertTrue(category)
                self.assertIsInstance(budget, int)
                self.assertGreater(budget, 0)

    def test_read_only_provider_skips_are_their_own_category(self):
        """Filing them under EXTERNAL_INTEGRATION to satisfy an existing
        regex would erase the distinction that makes them safe to run."""
        cats = {c for _p, c, _b in conftest.ALLOWED_SKIPS}
        self.assertIn("REAL_PROVIDER_READ_ONLY", cats)
        self.assertIn("OPTIONAL_HARDWARE", cats)
        self.assertIn("EXTERNAL_INTEGRATION", cats)

    def test_no_category_is_unlimited(self):
        """A budget large enough to never bind is a budget nobody can
        defend, and it cannot tell a hardware skip from a new accident."""
        for _p, category, budget in conftest.ALLOWED_SKIPS:
            with self.subTest(category=category):
                self.assertLessEqual(budget, 10)


class AnUndeclaredSkipFailsTheSessionTests(unittest.TestCase):
    """The property the hook exists to enforce, asserted directly."""

    def test_a_declared_provider_skip_is_accepted(self):
        problems, counts = conftest.classify_skips([
            ("tests/test_bitnomial_real_provider.py:35",
             "REAL_PROVIDER_READ_ONLY: set JARVIS_REAL_PROVIDER_TESTS=1 to "
             "reach the network")])
        self.assertEqual(problems, [])
        self.assertEqual(counts["REAL_PROVIDER_READ_ONLY"], 1)

    def test_an_arbitrary_undeclared_skip_is_a_problem(self):
        problems, _ = conftest.classify_skips([
            ("tests/test_whatever.py:1", "just skipping this one for now")])
        self.assertEqual(len(problems), 1)
        self.assertIn("undeclared skip reason", problems[0])

    def test_exceeding_a_category_budget_is_a_problem(self):
        budget = dict((c, b) for _p, c, b in conftest.ALLOWED_SKIPS)[
            "REAL_PROVIDER_READ_ONLY"]
        reports = [(f"tests/t.py:{i}", "REAL_PROVIDER_READ_ONLY: opt in")
                   for i in range(budget + 1)]
        problems, _ = conftest.classify_skips(reports)
        self.assertTrue(any("exceeds its budget" in p for p in problems))

    def test_the_exact_budget_is_not_exceeded_by_the_exact_count(self):
        budget = dict((c, b) for _p, c, b in conftest.ALLOWED_SKIPS)[
            "REAL_PROVIDER_READ_ONLY"]
        reports = [(f"tests/t.py:{i}", "REAL_PROVIDER_READ_ONLY: opt in")
                   for i in range(budget)]
        problems, _ = conftest.classify_skips(reports)
        self.assertEqual(problems, [])

    def test_a_forbidden_reason_is_rejected_even_though_it_is_specific(self):
        """"the environment starts empty" hid twelve core-logic tests."""
        problems, _ = conftest.classify_skips([
            ("tests/t.py:1", "no outcome history")])
        self.assertEqual(len(problems), 1)
        self.assertIn("seed a deterministic fixture", problems[0])

    def test_a_forbidden_reason_outranks_a_declared_pattern(self):
        """Wrapping a forbidden reason in an allowed label must not launder
        it — FORBIDDEN is checked first, deliberately."""
        problems, _ = conftest.classify_skips([
            ("tests/t.py:1",
             "REAL_PROVIDER_READ_ONLY: no market data available")])
        self.assertEqual(len(problems), 1)
        self.assertIn("seed a deterministic fixture", problems[0])


class TheRealProviderFilesAreClassifiedTests(unittest.TestCase):

    def test_the_bitnomial_provider_module_uses_the_declared_reason(self):
        import pathlib
        src = (pathlib.Path(__file__).parent
               / "test_bitnomial_real_provider.py").read_text(encoding="utf-8")
        self.assertIn("REAL_PROVIDER_READ_ONLY", src)
        self.assertIn("JARVIS_REAL_PROVIDER_TESTS", src)

    def test_its_skips_classify_cleanly_under_the_policy(self):
        import pathlib
        import re
        src = (pathlib.Path(__file__).parent
               / "test_bitnomial_real_provider.py").read_text(encoding="utf-8")
        reason = re.search(r'skipUnless\([^,]+,\s*"([^"]+)"\s*\n?\s*"?([^"]*)"?',
                           src)
        self.assertIsNotNone(reason, "could not read the skip reason")
        problems, counts = conftest.classify_skips(
            [("tests/test_bitnomial_real_provider.py:1", reason.group(1))])
        self.assertEqual(problems, [])
        self.assertEqual(counts.get("REAL_PROVIDER_READ_ONLY"), 1)


if __name__ == "__main__":
    unittest.main()
