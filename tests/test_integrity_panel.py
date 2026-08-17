"""Make the machine tell us when its own invariants are breaking.

Every defect this programme found had one signature: the system was
CONFIDENT and WRONG, and nothing distinguished that from confident and
right. A leveraged short marked as a long, futures P&L missing its
multiplier, a liquidity failure booked as a perfect exit, a wallet promoted
on statistics nothing ever wrote — none of them threw, and several looked
like improvements.

This panel runs the remedies continuously. The bar for inclusion is that a
check must be able to go RED on real rows: "we have 400 signals" is a
dashboard, "31 of these state no readable direction" is an invariant.
"""
import unittest

from lib.integrity_panel import (CRITICAL, OK, UNAVAILABLE, VIOLATION,
                                 ALL_CHECKS, run_all)


class PanelShapeTests(unittest.TestCase):
    def test_it_runs_against_the_real_database(self):
        r = run_all()
        self.assertEqual(r["total"], len(ALL_CHECKS))
        self.assertIn("verdict", r)

    def test_every_check_explains_why_it_matters(self):
        """A red light nobody understands gets ignored, then disabled."""
        for c in run_all()["checks"]:
            self.assertTrue(c["why_it_matters"], c["key"])
            self.assertTrue(c["title"], c["key"])

    def test_every_check_reports_what_it_scanned(self):
        for c in run_all()["checks"]:
            self.assertIn("scanned", c)
            self.assertIn("count", c)

    def test_statuses_are_from_the_known_set(self):
        for c in run_all()["checks"]:
            self.assertIn(c["status"], (OK, VIOLATION, UNAVAILABLE, "ERROR"))


class HealthyMeansActuallyLookedTests(unittest.TestCase):
    """A check that could not run is not a check that passed."""

    def test_unavailable_checks_prevent_a_clean_verdict(self):
        r = run_all()
        if r["unavailable"] or r["errors"]:
            self.assertFalse(r["healthy"])
            self.assertNotEqual(r["verdict"], "CLEAN")

    def test_violations_prevent_a_clean_verdict(self):
        r = run_all()
        if r["violations"]:
            self.assertFalse(r["healthy"])

    def test_critical_violations_are_named_as_such(self):
        r = run_all()
        if r["critical"]:
            self.assertIn("CRITICAL", r["verdict"])


class CountingPrecisionTests(unittest.TestCase):
    """The panel must not itself be confidently wrong."""

    def test_futures_counts_distinct_contracts_not_rows(self):
        """Counting rows made ONE unspecified contract with 140 signals
        read as '88 futures symbols'. A miscounting panel is the exact
        failure mode it exists to catch."""
        r = run_all()
        chk = next(c for c in r["checks"] if c["key"] == "futures_without_spec")
        if chk["status"] == VIOLATION:
            self.assertLessEqual(chk["count"], chk["scanned"])
            distinct = {e["symbol"] for e in chk["examples"]}
            self.assertLessEqual(len(distinct), chk["count"])
            self.assertIn("distinct", chk["detail"])

    def test_a_violation_count_never_exceeds_what_was_scanned(self):
        for c in run_all()["checks"]:
            if c["status"] == VIOLATION and c["scanned"]:
                self.assertLessEqual(c["count"], c["scanned"], c["key"])


class SpecificInvariantTests(unittest.TestCase):
    def test_unknown_directions_is_critical(self):
        chk = next(c for c in run_all()["checks"]
                   if c["key"] == "unknown_directions")
        self.assertEqual(chk["severity"], CRITICAL)

    def test_unspecified_futures_is_critical(self):
        """Wrong by 50x on ES and 100x on gold, invisibly."""
        chk = next(c for c in run_all()["checks"]
                   if c["key"] == "futures_without_spec")
        self.assertEqual(chk["severity"], CRITICAL)

    def test_an_unspecified_contract_is_refused_by_the_instrument_layer(self):
        """The panel reports it; the instrument layer enforces it. An
        unverified spec must not be invented to clear the check."""
        from lib.instruments import UnsupportedInstrument, resolve
        chk = next(c for c in run_all()["checks"]
                   if c["key"] == "futures_without_spec")
        for ex in chk["examples"]:
            self.assertFalse(resolve(ex["symbol"]).executable, ex["symbol"])
            with self.assertRaises(UnsupportedInstrument):
                resolve(ex["symbol"]).require_executable()

    def test_unattributed_outcomes_stay_unattributed(self):
        """They may not be fuzzy-matched to strategies to grow the sample."""
        chk = next(c for c in run_all()["checks"]
                   if c["key"] == "unattributed_outcomes")
        if chk["status"] == VIOLATION:
            self.assertIn("fuzzy-matched", chk["detail"])

    def test_dex_unpriceable_exits_are_critical(self):
        chk = next(c for c in run_all()["checks"]
                   if c["key"] == "dex_unpriceable_exit")
        self.assertEqual(chk["severity"], CRITICAL)


class RouteTests(unittest.TestCase):
    def test_the_integrity_route_returns_the_panel(self):
        from app.routers.platform import platform_integrity
        r = platform_integrity()
        self.assertEqual(r["total"], len(ALL_CHECKS))

    def test_the_venues_route_states_the_ui_rule(self):
        from app.routers.platform import platform_venues
        r = platform_venues()
        self.assertIn("UI availability is not API availability", r["note"])
        self.assertIn("kraken", r["venues"])
        self.assertIn("LIVE_KRAKEN", r["adapters"])


if __name__ == "__main__":
    unittest.main()
