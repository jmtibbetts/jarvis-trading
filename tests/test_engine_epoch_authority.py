"""Does a canonical trade ever reach the machinery that learns from it?

THE QUESTION THIS FILE EXISTS TO ANSWER. Two epoch strings live in this
codebase and they do not match:

    lib.canonical_entry.CANONICAL_ENGINE_EPOCH   stamped on the economics
    lib.calibration.CURRENT_EPOCH                filtered on by the learners

`canonical_learning` deliberately stamps the PERSISTED epoch — the one that
produced the economics — onto its `trade_outcomes` row. Every calibration,
expectancy and edge-cost query then filters `engine_epoch == CURRENT_EPOCH`.

If those two strings differ, a canonical trade settles perfectly, projects
its outcome perfectly, records `learning_state = APPLIED` — and is then
invisible to everything that consumes outcomes. The book would learn
nothing while reporting that it had.

This matters for the cutover specifically: a NEW economic epoch that only
lands on one side of that boundary reproduces the same silence.
"""
import unittest


class TheTwoEpochStringsTests(unittest.TestCase):

    def test_the_economics_epoch_and_the_learning_epoch_agree(self):
        from lib.calibration import CURRENT_EPOCH
        from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
        self.assertEqual(
            CANONICAL_ENGINE_EPOCH, CURRENT_EPOCH,
            "a canonical outcome is stamped with the economics epoch but "
            "every learner filters on the calibration epoch; while these "
            "differ, canonical trades are invisible to learning")


class ACanonicalOutcomeIsVisibleToTheLearnersTests(unittest.TestCase):
    """The behavioural form. A structural string comparison could be
    satisfied by making the two constants equal while some third consumer
    still filters on something else, so this asks the consumers."""

    def _seed_canonical_outcome(self):
        """One canonical trade_outcomes row, stamped exactly as the real
        projection stamps it."""
        from app.database import TradeOutcome, get_db, new_id
        from lib.canonical_entry import CANONICAL_ENGINE_EPOCH
        oid = new_id()
        with get_db() as db:
            db.query(TradeOutcome).delete()
            db.add(TradeOutcome(
                id=oid, symbol="BTC/USD", direction="Long",
                entry_price=64_000.0, exit_price=65_000.0,
                pnl_pct=1.2, outcome="win",
                engine_epoch=CANONICAL_ENGINE_EPOCH,
                entered_at="2026-08-18T00:00:00+00:00",
                exited_at="2026-08-18T04:00:00+00:00"))
            db.commit()
        return oid

    def test_the_current_epoch_query_counts_it(self):
        from app.database import TradeOutcome, get_db
        from lib.calibration import CURRENT_EPOCH
        self._seed_canonical_outcome()
        with get_db() as db:
            n = db.query(TradeOutcome).filter(
                TradeOutcome.engine_epoch == CURRENT_EPOCH).count()
        self.assertEqual(
            n, 1,
            "the canonical outcome exists but the CURRENT_EPOCH filter — "
            "the one calibration, expectancy, the edge-cost matrix and the "
            "paper job's historical edge all use — does not see it")


if __name__ == "__main__":
    unittest.main()


class TheEpochHasExactlyOneDefinitionTests(unittest.TestCase):
    """Structural. The behavioural tests above would still pass if someone
    reintroduced a second literal that happened to match today — which is
    precisely how the drift this file documents came about."""

    def test_no_module_defines_its_own_epoch_literal(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        # A bare date-like epoch literal assigned to an epoch-ish name.
        pat = re.compile(
            r"^\s*(CANONICAL_ENGINE_EPOCH|CURRENT_EPOCH|ENGINE_EPOCH)\s*=\s*"
            r"['\"]\d{4}-\d{2}-\d{2}")
        offenders = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith((".venv/", "tests/")) or "site-packages" in rel:
                continue
            for n, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore")
                        .splitlines(), 1):
                if pat.match(line):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        # lib/engine_epoch.py is the one allowed definition.
        self.assertEqual(
            [o for o in offenders
             if not o.startswith("lib/engine_epoch.py")], [],
            f"a second epoch literal reappeared: {offenders}")
        self.assertTrue(
            any(o.startswith("lib/engine_epoch.py") for o in offenders),
            "the search matched nothing at all — it is broken")

    def test_the_epoch_is_not_generated_at_runtime(self):
        """P1.1 — a model version, not a session ID. Two processes running
        the same code must agree, and re-importing must not change it."""
        import importlib

        from lib import engine_epoch
        first = engine_epoch.ENGINE_EPOCH
        importlib.reload(engine_epoch)
        self.assertEqual(first, engine_epoch.ENGINE_EPOCH)
        import inspect
        src = inspect.getsource(engine_epoch)
        line = [l for l in src.splitlines()
                if l.startswith("ENGINE_EPOCH")][0]
        self.assertRegex(line, r'^ENGINE_EPOCH = "[^"]+"$',
                         "the epoch is computed, not stated")
        for forbidden in ("datetime", "time.", "uuid", "os.getenv"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_the_new_epoch_is_not_a_prior_one(self):
        from lib.engine_epoch import ENGINE_EPOCH, PRIOR_EPOCHS
        self.assertNotIn(ENGINE_EPOCH, PRIOR_EPOCHS)
        self.assertIn("2026-08-13", PRIOR_EPOCHS)
        self.assertIn("2026-08-17-venue-book", PRIOR_EPOCHS)

    def test_prior_epoch_outcomes_are_not_counted_as_current(self):
        """P1.3 — historical labels are preserved, not relabelled. They
        simply stop being evidence about THIS machine."""
        from lib.engine_epoch import PRIOR_EPOCHS, is_current
        for old in PRIOR_EPOCHS:
            self.assertFalse(is_current(old), old)
        self.assertFalse(is_current(None))

    def test_the_evidence_campaign_is_a_different_concept(self):
        """P1.4 — the collector's campaign identity and the paper engine's
        economic epoch are not the same thing and must not be merged."""
        import inspect

        from lib import engine_epoch
        src = inspect.getsource(engine_epoch)
        self.assertNotIn("FORWARD_EVIDENCE", src)
        self.assertNotIn("campaign", src.lower())
