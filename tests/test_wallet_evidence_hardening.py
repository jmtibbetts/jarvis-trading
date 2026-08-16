"""W-7/8/9 — fail-closed eligibility, cached identity, horizon tolerance.

Three defects that share a shape: a capability existed, and the production
path either did not use it or used it in a way that inverted its meaning.

W-7  `signature_count` is None both when the activity call was never made
     AND when it FAILED. Both fell past the check into `eligible: True`,
     so a Helius timeout promoted an unmeasured address to "active enough
     to analyse" and the expensive history work that follows was spent on
     evidence nobody had.

W-8  `batch_identity` existed in the client with ZERO callers. Entity
     classification relied on a ten-entry hardcoded infrastructure map
     while Helius's own labels went unasked-for — and unasked-for every
     pass, forever, is also why caching matters.

W-9  `price_at` accepts `horizon_s` and scales its match tolerance from
     it. The resolver never passed it, so every lookup used the MINIMUM
     tolerance and long-horizon observations stayed unresolved for want of
     a precision the question never needed.
"""
import unittest
import uuid

from app.database import WalletRegistry, get_db


class FailClosedEligibilityTests(unittest.TestCase):
    """UNKNOWN means retry. It must never mean yes."""

    # A structurally valid, unlabelled address — classify() will call it a
    # trader candidate, so eligibility turns purely on the activity check.
    ADDR = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWN"

    def _elig(self, sig_count, error=None):
        from lib.wallet_classify import trader_eligibility
        return trader_eligibility(self.ADDR, evidence={
            "checked": True, "exists": True, "executable": False,
            "owner_program": "11111111111111111111111111111111",
            "lamports": 5_000_000, "signature_count": sig_count,
            "error": error})

    def test_a_failed_activity_lookup_is_unknown_not_eligible(self):
        out = self._elig(None, error="HeliusError: timeout")
        self.assertFalse(out["eligible"], "a timeout must not read as active")
        self.assertEqual(out["eligibility"], "UNKNOWN")
        self.assertTrue(out["retry"])
        self.assertIn("could not be measured", out["eligibility_reason"])

    def test_a_measured_low_count_is_ineligible_and_not_retried(self):
        out = self._elig(1)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["eligibility"], "INELIGIBLE")
        self.assertFalse(out["retry"])

    def test_a_measured_healthy_count_is_eligible(self):
        out = self._elig(25)
        self.assertTrue(out["eligible"], out.get("eligibility_reason"))
        self.assertEqual(out["eligibility"], "ELIGIBLE")

    def test_the_three_states_are_distinguishable(self):
        seen = {self._elig(25)["eligibility"],
                self._elig(1)["eligibility"],
                self._elig(None, "boom")["eligibility"]}
        self.assertEqual(seen, {"ELIGIBLE", "INELIGIBLE", "UNKNOWN"})


class IdentityCacheTests(unittest.TestCase):
    def test_an_unlabelled_result_is_not_proof_of_a_human(self):
        from lib.wallet_classify import entity_from_identity
        self.assertIsNone(entity_from_identity({"type": "unknown"}))
        self.assertIsNone(entity_from_identity({}))
        self.assertIsNone(entity_from_identity(None))

    def test_known_infrastructure_maps_into_the_entity_taxonomy(self):
        from lib.wallet_classify import NON_TRADER_ENTITIES, entity_from_identity
        for t, expect in (("exchange", "CEX"), ("bridge", "BRIDGE"),
                          ("pool", "LIQUIDITY_POOL"), ("program", "PROGRAM")):
            got = entity_from_identity({"type": t})
            self.assertEqual(got, expect, t)
            self.assertIn(got, NON_TRADER_ENTITIES)

    def test_a_fresh_label_is_not_re_queried(self):
        from app.database import now_iso
        from lib.wallet_classify import identity_is_fresh
        self.assertTrue(identity_is_fresh(now_iso()))
        self.assertFalse(identity_is_fresh("2020-01-01T00:00:00+00:00"))
        self.assertFalse(identity_is_fresh(None))

    def test_cached_addresses_are_not_looked_up_again(self):
        from unittest.mock import patch

        from app.database import now_iso
        from lib.wallet_classify import resolve_identities
        with get_db() as db:
            a = "W" + uuid.uuid4().hex[:30]
            db.add(WalletRegistry(address=a, status="CANDIDATE",
                                  source="test",
                                  identity_checked_at=now_iso(),
                                  identity_type="CEX"))
            db.flush()
            with patch("lib.helius_client.batch_identity") as bi:
                stats = resolve_identities(db, [a])
            bi.assert_not_called()
            self.assertEqual(stats["cached"], 1)
            db.rollback()

    def test_an_unlabelled_result_is_cached_too(self):
        """Otherwise the common case is re-queried forever."""
        from unittest.mock import patch

        from lib.wallet_classify import resolve_identities
        with get_db() as db:
            a = "W" + uuid.uuid4().hex[:30]
            db.add(WalletRegistry(address=a, status="CANDIDATE", source="test"))
            db.flush()
            with patch("lib.helius_client.batch_identity",
                       return_value={a: {"type": "unknown"}}):
                stats = resolve_identities(db, [a])
            self.assertEqual(stats["unlabelled"], 1)
            row = db.query(WalletRegistry).filter_by(address=a).one()
            self.assertIsNotNone(row.identity_checked_at)
            db.rollback()

    def test_a_labelled_exchange_is_excluded_from_trader_scoring(self):
        from unittest.mock import patch

        from lib.wallet_classify import resolve_identities
        with get_db() as db:
            a = "W" + uuid.uuid4().hex[:30]
            db.add(WalletRegistry(address=a, status="CANDIDATE", source="test"))
            db.flush()
            with patch("lib.helius_client.batch_identity",
                       return_value={a: {"type": "exchange", "name": "Binance 2"}}):
                resolve_identities(db, [a])
            row = db.query(WalletRegistry).filter_by(address=a).one()
            self.assertEqual(row.entity_type, "CEX")
            self.assertEqual(row.status, "EXCLUDED_ENTITY")
            db.rollback()

    def test_a_failed_lookup_leaves_cached_labels_intact(self):
        from unittest.mock import patch

        from app.database import now_iso
        from lib.wallet_classify import resolve_identities
        with get_db() as db:
            a = "W" + uuid.uuid4().hex[:30]
            db.add(WalletRegistry(address=a, status="CANDIDATE", source="test",
                                  identity_type="CEX", identity_name="Binance",
                                  identity_checked_at="2020-01-01T00:00:00+00:00"))
            db.flush()
            with patch("lib.helius_client.batch_identity",
                       side_effect=RuntimeError("network")):
                stats = resolve_identities(db, [a])
            row = db.query(WalletRegistry).filter_by(address=a).one()
            self.assertEqual(row.identity_type, "CEX",
                             "a network problem does not un-know an identity")
            self.assertEqual(stats["errors"], 1)
            db.rollback()


class HorizonToleranceTests(unittest.TestCase):
    def test_the_resolver_passes_the_horizon(self):
        """The whole defect: it did not."""
        from datetime import datetime, timezone

        from lib.wallet_alpha import VERIFIED_BUY_ENTRY, resolve_observation

        seen = []

        def lookup(mint, when, *, horizon_s=None):
            seen.append(horizon_s)
            return 2.0

        class Row:
            evidence_class = VERIFIED_BUY_ENTRY
            entry_price_usd = 1.0
            entry_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
            mint = "M"
            horizons_resolved = ""
            fully_resolved = 0

        resolve_observation(None, Row(), lookup)
        self.assertTrue(seen, "no horizon reached the price lookup")
        self.assertTrue(all(h is not None for h in seen))
        # Every horizon should be distinct and increasing in scale.
        self.assertGreater(max(seen), min(seen))

    def test_tolerance_scales_with_the_horizon(self):
        from lib.token_price_history import (MAX_TOLERANCE_S, MIN_TOLERANCE_S,
                                             TOLERANCE_FRACTION)
        five_min = max(MIN_TOLERANCE_S, 300 * TOLERANCE_FRACTION)
        one_day = min(MAX_TOLERANCE_S, 86_400 * TOLERANCE_FRACTION)
        self.assertGreater(one_day, five_min,
                           "a 24h alpha may accept a match a 5m alpha must not")

    def test_a_simple_two_argument_lookup_still_works(self):
        """Custom price sources and older tests must not break."""
        from datetime import datetime, timezone

        from lib.wallet_alpha import VERIFIED_BUY_ENTRY, resolve_observation

        class Row:
            evidence_class = VERIFIED_BUY_ENTRY
            entry_price_usd = 1.0
            entry_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
            mint = "M"
            horizons_resolved = ""
            fully_resolved = 0

        out = resolve_observation(None, Row(), lambda m, w: 2.0)
        self.assertTrue(out["resolved"])


if __name__ == "__main__":
    unittest.main()
