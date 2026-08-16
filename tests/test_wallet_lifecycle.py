"""W6 — repeat sightings are evidence, and promotion is a decision.

Two defects, one theme.

EVIDENCE. Discovery saw a wallet already in the registry and `continue`d,
throwing the sighting away. If wallet A appears before ten independent
token surges, that recurrence is the most valuable signal the system
produces — and being already-known is exactly what made it worth noticing.

LIFECYCLE. The only lifecycle write in the whole codebase was a single
`w.status = "WATCH"` inside the scoring function, so SMART_MONEY and
HIGH_CONVICTION were unreachable states that `counts()` nonetheless
reported. Promotion was a side effect of measurement.

CHECKPOINT 4 from the audit: repeated appearances must increase evidence
without duplicating the wallet identity.
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.database import WalletObservation, WalletRegistry, get_db
from lib import wallet_alpha, wallet_lifecycle
from lib import wallet_registry as reg

W = "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"
NOW = datetime.now(timezone.utc)


def _clear():
    with get_db() as db:
        db.query(WalletObservation).delete()
        db.query(WalletRegistry).delete()


class Checkpoint4Tests(unittest.TestCase):
    """One identity, many observations."""

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def test_ten_sightings_are_one_wallet_and_ten_observations(self):
        with get_db() as db:
            reg.upsert_wallet(db, W, status=reg.CANDIDATE)
            for i in range(10):
                wallet_alpha.record_observation(
                    db, wallet_address=W, mint=f"mint{i}",
                    signature=f"sig{i}", entry_timestamp=NOW.isoformat(),
                    entry_price_usd=1.0, discovery_source="pool_traders")
            db.flush()
            self.assertEqual(
                db.query(WalletRegistry).filter(
                    WalletRegistry.address == W).count(), 1,
                "the registry holds ONE identity")
            self.assertEqual(
                db.query(WalletObservation).filter(
                    WalletObservation.wallet_address == W).count(), 10,
                "the observation table holds MANY sightings")

    def test_discovery_records_a_sighting_for_an_already_known_wallet(self):
        """The exact line that used to `continue`."""
        from lib.wallet_discovery import _observe
        with get_db() as db:
            reg.upsert_wallet(db, W, status=reg.WATCH)
            made = _observe(db, W, {"mint": "m1", "pool": "p1", "name": "TOK",
                                    "surge_started_at": NOW.isoformat()},
                            "pool_traders",
                            {"signature": "sigA", "timestamp": NOW.isoformat()})
            db.flush()
            self.assertEqual(made, 1)
            self.assertEqual(
                db.query(WalletObservation).filter(
                    WalletObservation.wallet_address == W).count(), 1)


class PromotionTests(unittest.TestCase):
    """Scoring measures; lifecycle decides."""

    def _wallet(self, **kw):
        class Row:
            status = "WATCH"
            smart_money_score = 70.0
            confidence_score = 60.0
            qualified_trades = 20
            whale_score = 99.0
            last_seen_at = NOW.isoformat()
            last_score_update = NOW.isoformat()
            updated_at = NOW.isoformat()
            address = W
        r = Row()
        for k, v in kw.items():
            setattr(r, k, v)
        return r

    def _alpha(self, score, n):
        return {"alpha_score": score, "measurable": score is not None,
                "horizons": {"1h": {"n": n, "median_return_pct": 10.0}}}

    def test_a_whale_is_not_promoted_for_being_large(self):
        """whale_score appears in NO promotion rule. The first live
        discovery pass would otherwise have promoted a Binance hot wallet."""
        w = self._wallet(status="CANDIDATE", smart_money_score=10.0,
                         confidence_score=5.0, whale_score=100.0)
        v = wallet_lifecycle.evaluate(w, self._alpha(90.0, 50))
        self.assertEqual(v["status"], "CANDIDATE")
        self.assertFalse(v["changed"])

    def test_watch_requires_a_measured_score(self):
        w = self._wallet(status="CANDIDATE", smart_money_score=None)
        v = wallet_lifecycle.evaluate(w)
        self.assertFalse(v["changed"])
        self.assertIn("unmeasured", v["reasons"][0])

    def test_candidate_promotes_to_watch_on_score_and_confidence(self):
        w = self._wallet(status="CANDIDATE", smart_money_score=60.0,
                         confidence_score=30.0)
        v = wallet_lifecycle.evaluate(w)
        self.assertEqual(v["status"], "WATCH")

    def test_smart_money_requires_measured_post_entry_alpha(self):
        """'this wallet trades well' and 'following it creates an
        opportunity' are different claims. SMART_MONEY asserts the second."""
        w = self._wallet(status="WATCH", smart_money_score=80.0,
                         confidence_score=80.0, qualified_trades=40)
        # Every trading gate cleared, but no alpha observations.
        v = wallet_lifecycle.evaluate(w, self._alpha(None, 0))
        self.assertEqual(v["status"], "WATCH")
        self.assertIn("alpha observations", v["reasons"][0])

        v2 = wallet_lifecycle.evaluate(w, self._alpha(70.0, 10))
        self.assertEqual(v2["status"], "SMART_MONEY")

    def test_promotion_is_one_tier_at_a_time(self):
        """A spectacular CANDIDATE reaches WATCH, not SMART_MONEY."""
        w = self._wallet(status="CANDIDATE", smart_money_score=99.0,
                         confidence_score=99.0, qualified_trades=200)
        v = wallet_lifecycle.evaluate(w, self._alpha(99.0, 99))
        self.assertEqual(v["status"], "WATCH")

    def test_high_conviction_requires_copyability(self):
        w = self._wallet(status="SMART_MONEY", smart_money_score=90.0,
                         confidence_score=90.0)
        a = self._alpha(80.0, 30)
        held = wallet_lifecycle.evaluate(w, a, {"copy_score": 10.0})
        self.assertEqual(held["status"], "SMART_MONEY")
        self.assertIn("copyability", held["reasons"][0])
        moved = wallet_lifecycle.evaluate(w, a, {"copy_score": 60.0})
        self.assertEqual(moved["status"], "HIGH_CONVICTION")


class DemotionTests(unittest.TestCase):

    def _wallet(self, **kw):
        class Row:
            status = "SMART_MONEY"
            smart_money_score = 70.0
            confidence_score = 60.0
            qualified_trades = 20
            whale_score = None
            last_seen_at = NOW.isoformat()
            last_score_update = NOW.isoformat()
            updated_at = NOW.isoformat()
            address = W
        r = Row()
        for k, v in kw.items():
            setattr(r, k, v)
        return r

    def test_a_quiet_wallet_degrades(self):
        old = (NOW - timedelta(days=60)).isoformat()
        w = self._wallet(last_seen_at=old, last_score_update=old)
        v = wallet_lifecycle.evaluate(w)
        self.assertEqual(v["status"], "DEGRADED")
        self.assertIn("no new evidence", v["reasons"][0])

    def test_a_falling_score_degrades(self):
        w = self._wallet(smart_money_score=40.0)
        v = wallet_lifecycle.evaluate(w)
        self.assertEqual(v["status"], "DEGRADED")

    def test_falling_alpha_degrades_only_on_enough_evidence(self):
        w = self._wallet()
        thin = wallet_lifecycle.evaluate(
            w, {"alpha_score": 10.0, "measurable": True,
                "horizons": {"1h": {"n": 2}}})
        self.assertNotEqual(thin["status"], "DEGRADED",
                            "two observations cannot demote a wallet")
        thick = wallet_lifecycle.evaluate(
            w, {"alpha_score": 10.0, "measurable": True,
                "horizons": {"1h": {"n": 20}}})
        self.assertEqual(thick["status"], "DEGRADED")

    def test_excluded_entities_are_never_touched(self):
        w = self._wallet(status="EXCLUDED_ENTITY", smart_money_score=99.0)
        v = wallet_lifecycle.evaluate(w)
        self.assertEqual(v["status"], "EXCLUDED_ENTITY")
        self.assertFalse(v["changed"])


class DecouplingTests(unittest.TestCase):

    def test_scoring_no_longer_writes_status(self):
        """Promotion must not be a side effect of measurement."""
        import ast
        import inspect

        from lib import wallet_scoring
        src = inspect.getsource(wallet_scoring.score_registry_wallets)
        tree = ast.parse(src.lstrip())
        writes = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  for t in n.targets
                  if getattr(t, "attr", None) == "status"]
        self.assertEqual(writes, [],
                         "lib/wallet_lifecycle owns status transitions")

    def test_every_declared_state_is_reachable(self):
        """counts() reported buckets nothing could fill."""
        import inspect
        src = inspect.getsource(wallet_lifecycle)
        for state in ("WATCH", "SMART_MONEY", "HIGH_CONVICTION",
                      "DEGRADED", "ARCHIVED"):
            self.assertIn(f'"{state}"', src, f"{state} unreachable")


if __name__ == "__main__":
    unittest.main()
