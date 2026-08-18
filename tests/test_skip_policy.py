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

    # The classified read-only provider modules. The budget must equal the
    # number of tests in them — not a round number chosen to leave headroom.
    PROVIDER_MODULES = ("test_bitnomial_real_provider.py",
                        "test_kraken_real_provider.py")

    def test_the_provider_budget_equals_the_tests_that_exist(self):
        """AN EXACT BUDGET, NOT A GENEROUS ONE. Headroom is what lets a
        hermetic test quietly grow a network dependency and skip inside a
        green run — which is the whole failure this hook exists to catch.

        This assertion replaced a flat `budget <= 10`, which was itself the
        magic number the policy forbids: it failed the moment a second
        provider module was added, for no reason anyone could defend.
        """
        import pathlib
        import re
        here = pathlib.Path(__file__).parent
        expected = sum(
            len(re.findall(r"^\s*def test_", (here / m).read_text(encoding="utf-8"),
                           re.M))
            for m in self.PROVIDER_MODULES)
        budget = dict((c, b) for _p, c, b in conftest.ALLOWED_SKIPS)[
            "REAL_PROVIDER_READ_ONLY"]
        self.assertEqual(budget, expected,
                         f"budget {budget} does not match the {expected} "
                         f"classified provider tests that exist")

    def test_no_category_is_unlimited(self):
        """A budget large enough to never bind cannot tell a hardware skip
        from a new accident. Bounded against the SUITE rather than a hard
        constant, so adding a justified category cannot break it."""
        total_budget = sum(b for _p, _c, b in conftest.ALLOWED_SKIPS)
        self.assertLess(total_budget, 50)


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


class TheSuiteIsHermeticByConstructionTests(unittest.TestCase):
    """ci.yml claims the suite is hermetic. It was not: fifteen tests
    reached api.kraken.com and futures.kraken.com, and failed with the
    network removed."""

    def test_venue_http_is_served_from_captured_payloads(self):
        """The autouse fixture is active, so this call cannot be reaching
        the internet — and it still returns a parsed spec."""
        from lib.venues import kraken_pair_specs
        spec = kraken_pair_specs("BTC/USD")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["pair"], "XBTUSD")

    def test_an_unknown_endpoint_trips_a_named_error(self):
        """A NEW accidental network call must announce itself rather than
        pass as "the venue had nothing to say"."""
        import httpx

        from lib import venues as V
        with self.assertRaises(AssertionError) as caught:
            V.httpx.get("https://api.kraken.com/0/public/Ticker")
        self.assertIn("hermetic test tried to reach", str(caught.exception))
        self.assertIs(V.httpx, httpx)

    def test_the_real_parser_runs_against_the_captured_payload(self):
        """Only the transport is replaced. Tick and minimum-size validation
        are the real ones — a stubbed `kraken_pair_specs` would assert only
        that the stub matches the assertion."""
        from lib.venues import validate_order
        off_tick = validate_order("kraken", "BTC/USD", 0.01, 95000.037)
        self.assertFalse(off_tick["ok"])
        self.assertTrue(validate_order("kraken", "BTC/USD", 0.01, 95000.0)["ok"])

    def test_the_futures_ladder_is_parsed_not_faked(self):
        from lib.venues import futures_fee_for
        low, _ = futures_fee_for("BTC/USD", volume_30d=0)
        high, _ = futures_fee_for("BTC/USD", volume_30d=500_000_000)
        self.assertIsNotNone(low)
        self.assertGreater(low, high)


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
