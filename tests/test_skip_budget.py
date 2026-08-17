"""The skip allowlist itself, checked.

ENFORCEMENT LIVES IN conftest.pytest_sessionfinish, not here — a test cannot
observe the session it belongs to, and the first version of this file forked
a second full suite to try, which tripled every run for no extra safety.

What is left here is the part a unit test can do well: prove the RULES are
coherent and that they still classify the cases they were written for. If
someone loosens a pattern until it excuses everything, these fail.

The history: eighteen tests skipped on every run, tolerated because the
previous platform skipped a similar number. Twelve were core logic — the
expectancy cost gate, which refuses a setup carrying 13R of round-trip cost
— skipping only because a hermetic database starts empty. Those assertions
had never executed in CI, and the check was green throughout.
"""
import re
import unittest

from conftest import ALLOWED_SKIPS, FORBIDDEN_SKIPS


class TheRulesAreCoherentTests(unittest.TestCase):

    def test_every_allowed_pattern_compiles_and_is_specific(self):
        for pattern, category, budget in ALLOWED_SKIPS:
            with self.subTest(category=category):
                re.compile(pattern)
                self.assertGreater(len(pattern), 8,
                                   "a pattern this loose would excuse anything")
                self.assertGreaterEqual(budget, 1)

    def test_no_allowed_pattern_matches_a_forbidden_reason(self):
        """The failure that would quietly disarm this: an allowlist entry
        broad enough to swallow one of the reasons we banned."""
        banned_examples = [
            "no outcome history in this environment",
            "bucket not tradeable in this environment",
            "no market data available",
            "mutates the live paper book — set RUN_DB_MUTATING_TESTS=1",
            "mutates the live auto-sim book — set RUN_DB_MUTATING_TESTS=1",
        ]
        for reason in banned_examples:
            for pattern, category, _ in ALLOWED_SKIPS:
                with self.subTest(reason=reason, category=category):
                    self.assertIsNone(
                        re.search(pattern, reason),
                        f"{category} would excuse {reason!r}")

    def test_every_banned_reason_is_actually_caught(self):
        """The other direction: the forbidden patterns must still match the
        exact reasons this audit removed, so a regression is caught by
        NAME rather than only by being undeclared."""
        for reason in ("no outcome history in this environment",
                       "bucket not tradeable in this environment",
                       "no market data available",
                       "mutates the live paper book — set RUN_DB_MUTATING_TESTS=1"):
            with self.subTest(reason=reason):
                self.assertTrue(
                    any(re.search(p, reason, re.I) for p, _ in FORBIDDEN_SKIPS),
                    f"{reason!r} would slip through as merely undeclared")

    def test_the_surviving_skips_are_still_classified(self):
        """The two categories deliberately kept, matched against the exact
        reason strings the tests emit."""
        live = {
            "EXTERNAL_INTEGRATION: needs operator Kraken credentials - set "
            "RUN_KRAKEN_INTEGRATION=1": "EXTERNAL_INTEGRATION",
            "EXTERNAL_INTEGRATION: needs a live market-data provider - set "
            "RUN_PROVIDER_INTEGRATION=1": "EXTERNAL_INTEGRATION",
            "hardware-only: no NPU on this host (CPU is the required "
            "baseline; this test compares two devices and needs both)":
                "OPTIONAL_HARDWARE",
        }
        for reason, expected in live.items():
            with self.subTest(reason=reason[:40]):
                matched = [c for p, c, _ in ALLOWED_SKIPS if re.search(p, reason)]
                self.assertEqual(matched, [expected])

    def test_every_forbidden_rule_names_the_fix(self):
        """"Not allowed" without "do this instead" gets worked around."""
        for pattern, why in FORBIDDEN_SKIPS:
            with self.subTest(pattern=pattern[:30]):
                re.compile(pattern)
                self.assertGreater(len(why), 40, "name the remedy, not just the rule")


if __name__ == "__main__":
    unittest.main()
