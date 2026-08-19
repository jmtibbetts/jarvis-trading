"""COLLECTION and ANALYSIS jobs must not be able to move money.

The capability split is a static map. A map is a claim, not a proof — a job
called "market" could still open a position, and nothing about its name
would stop it. These tests take the economic fingerprint, run the real job
functions, and require the fingerprint to be identical afterwards. Research
tables may change; the economy may not.
"""
import unittest
from unittest.mock import patch


ECONOMIC_TABLES = (
    "paper_positions", "paper_trades", "trade_outcomes",
    "paper_position_settlements", "paper_settlement_legs",
    "paper_realized_outcomes", "auto_sim_positions", "auto_sim_trades",
    "dex_positions", "dex_trades",
)


def economic_fingerprint() -> dict:
    """Everything that is money, and nothing that is not."""
    from sqlalchemy import text

    from app.database import get_db
    out = {}
    with get_db() as db:
        for table in ECONOMIC_TABLES:
            try:
                out[table] = db.execute(
                    text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
            except Exception:
                out[table] = "ABSENT"
        for wallet, col in (("paper_portfolio", "cash"),
                            ("auto_sim_portfolios", "cash"),
                            ("dex_portfolio", "cash")):
            try:
                rows = db.execute(
                    text(f'SELECT "{col}" FROM "{wallet}"')).fetchall()
                out[f"{wallet}.{col}"] = [round(float(r[0] or 0), 8)
                                          for r in rows]
            except Exception:
                out[f"{wallet}.{col}"] = "ABSENT"
    return out


class EconomicSurfacesArePoisonedTests(unittest.TestCase):
    """Poison every path that can move money, then run the safe jobs. If a
    job touches one, the poison fires and names it."""

    POISONED = (
        ("lib.canonical_entry", "open_canonical_position"),
        ("lib.canonical_settlement", "settle_prepared_exit"),
        ("lib.canonical_settlement", "settle_position_entry"),
        ("lib.paper_engine", "open_paper_position"),
        ("lib.paper_engine", "close_paper_position"),
        ("lib.paper_engine", "partial_close_paper_position"),
        ("lib.exit_dispatch", "request_position_exit"),
        ("lib.exit_dispatch", "request_position_partial_exit"),
    )

    def _poison(self):
        import importlib
        from contextlib import ExitStack
        stack = ExitStack()
        fired = []

        def make(name):
            def boom(*a, **k):
                fired.append(name)
                raise AssertionError(f"a safe job called {name}")
            return boom

        for mod_name, attr in self.POISONED:
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            if hasattr(mod, attr):
                stack.enter_context(
                    patch.object(mod, attr, make(f"{mod_name}.{attr}")))
        return stack, fired

    def test_the_control_the_poison_is_live(self):
        """Without this, a broken patch set would pass every test below."""
        stack, fired = self._poison()
        with stack:
            from lib import exit_dispatch as ED
            with self.assertRaises(AssertionError):
                ED.request_position_exit("nope", caller_price=1.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
        self.assertTrue(fired)

    def test_collection_and_analysis_jobs_touch_no_economic_surface(self):
        """The real job entry points, called the way the scheduler calls
        them. Provider I/O is allowed to fail — that is not what is under
        test; reaching an economic boundary is."""
        from lib import job_capability as JC

        safe = [j for j, cap in JC.describe_flat().items()
                if cap in (JC.COLLECTION, JC.ANALYSIS)]
        self.assertGreater(len(safe), 25, "the safe set looks wrong")

        # Jobs whose entry point is cheap and offline-tolerant. Network
        # failures inside them are irrelevant to this test.
        import importlib
        checked = 0
        stack, fired = self._poison()
        before = economic_fingerprint()
        with stack:
            for mod_name in ("jobs.fetch_social_sentiment",
                             "jobs.collect_postmortems",
                             "jobs.evaluate_signals"):
                try:
                    mod = importlib.import_module(mod_name)
                except Exception:
                    continue
                if not hasattr(mod, "run"):
                    continue
                checked += 1
                try:
                    mod.run()
                except AssertionError:
                    raise                      # the poison fired — a real failure
                except Exception:
                    pass                       # provider/data failure: fine
        self.assertGreater(checked, 0, "no safe job was actually exercised")
        self.assertEqual(fired, [], f"a safe job reached: {fired}")
        self.assertEqual(economic_fingerprint(), before,
                         "a COLLECTION/ANALYSIS job changed the economy")


class TheEconomicFingerprintIsMeaningfulTests(unittest.TestCase):

    def test_it_notices_an_actual_economic_change(self):
        """A fingerprint that cannot detect a change proves nothing."""
        from app.database import PaperPortfolio, get_db
        before = economic_fingerprint()
        with get_db() as db:
            pf = db.query(PaperPortfolio).first()
            original = float(pf.cash)
            pf.cash = original + 1.23
            db.commit()
        try:
            self.assertNotEqual(economic_fingerprint(), before,
                                "the fingerprint cannot see a cash change")
        finally:
            with get_db() as db:
                pf = db.query(PaperPortfolio).first()
                pf.cash = original
                db.commit()
        self.assertEqual(economic_fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
