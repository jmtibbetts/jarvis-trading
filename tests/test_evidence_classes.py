"""A signer is not a buyer, and a holder is not an entry.

Being a signer on a pool transaction proves the wallet participated in
something involving that address. It does not prove it bought the surged
token, in what size, or at what price. A holder snapshot is weaker still:
"owns 500,000 TOKEN X" says nothing about WHEN it was acquired or what was
paid — and a real entry is the one thing post-entry alpha requires.

The subtle part, and the reason this needed fixing even though nothing was
visibly broken: alpha was protected only INCIDENTALLY. Signer and holder
rows happen to carry no entry price, so `resolve` skipped them. That is
protection by accident — the day any code path filled in a plausible
price, holder snapshots would silently have begun producing alpha
observations, with no line changing in wallet_alpha.

So the class gate is now explicit and sits AHEAD of the price check.
"""
import unittest

from lib.wallet_alpha import (ALPHA_ELIGIBLE_CLASSES, EVIDENCE_CLASSES,
                              HOLDER_SNAPSHOT, PARTICIPANT_SIGHTING,
                              POOL_TX_SIGNER, VERIFIED_BUY_ENTRY,
                              VERIFIED_SELL_EXIT, evidence_class_for,
                              is_alpha_eligible)


class ClassificationTests(unittest.TestCase):
    def test_a_pool_signer_is_not_a_buyer(self):
        self.assertEqual(evidence_class_for("pool_traders"), POOL_TX_SIGNER)
        self.assertFalse(is_alpha_eligible(POOL_TX_SIGNER))

    def test_a_holder_snapshot_is_not_an_entry(self):
        self.assertEqual(evidence_class_for("holders"), HOLDER_SNAPSHOT)
        self.assertFalse(is_alpha_eligible(HOLDER_SNAPSHOT))

    def test_an_unknown_source_gets_the_weakest_class(self):
        """Fail closed: a new source earns a stronger class by proving a
        balance change, never by being unrecognised here."""
        self.assertEqual(evidence_class_for("brand_new_source"),
                         PARTICIPANT_SIGHTING)
        self.assertEqual(evidence_class_for(None), PARTICIPANT_SIGHTING)
        self.assertFalse(is_alpha_eligible(PARTICIPANT_SIGHTING))

    def test_only_a_verified_buy_can_anchor_alpha(self):
        self.assertEqual(ALPHA_ELIGIBLE_CLASSES, {VERIFIED_BUY_ENTRY})
        for c in EVIDENCE_CLASSES - {VERIFIED_BUY_ENTRY}:
            self.assertFalse(is_alpha_eligible(c), c)
        self.assertTrue(is_alpha_eligible(VERIFIED_BUY_ENTRY))

    def test_a_sell_is_not_an_entry_either(self):
        self.assertFalse(is_alpha_eligible(VERIFIED_SELL_EXIT))

    def test_legacy_rows_with_no_class_are_ineligible(self):
        """Observations recorded before the distinction existed must not be
        promoted by default — NULL is the safe direction."""
        self.assertFalse(is_alpha_eligible(None))
        self.assertFalse(is_alpha_eligible(""))


class VerifiedRequiresAPriceTests(unittest.TestCase):
    """"Verified" that carries no price is the exact fabrication these
    classes exist to prevent."""

    def _record(self, session, **kw):
        from lib.wallet_alpha import record_observation
        return record_observation(
            session, wallet_address=kw.pop("w", "W1"), mint=kw.pop("m", "M1"),
            signature=kw.pop("sig", "S1"), entry_timestamp=1_700_000_000,
            **kw)

    def test_a_claimed_verified_buy_without_a_price_is_downgraded(self):
        from app.database import get_db
        with get_db() as db:
            row, _ = self._record(db, evidence_class=VERIFIED_BUY_ENTRY,
                                  entry_price_usd=None)
            self.assertEqual(row.evidence_class, PARTICIPANT_SIGHTING)
            self.assertFalse(row.alpha_eligible)
            db.rollback()

    def test_a_verified_buy_with_a_price_is_kept(self):
        from app.database import get_db
        with get_db() as db:
            row, _ = self._record(db, sig="S2", evidence_class=VERIFIED_BUY_ENTRY,
                                  entry_price_usd=0.42)
            self.assertEqual(row.evidence_class, VERIFIED_BUY_ENTRY)
            self.assertTrue(row.alpha_eligible)
            db.rollback()

    def test_the_source_decides_when_no_class_is_claimed(self):
        from app.database import get_db
        with get_db() as db:
            row, _ = self._record(db, sig="S3", discovery_source="holders")
            self.assertEqual(row.evidence_class, HOLDER_SNAPSHOT)
            db.rollback()


class ResolveRefusesIneligibleTests(unittest.TestCase):
    """The gate must be the CLASS, not the accidental absence of a price."""

    class _Row:
        def __init__(self, ec, px):
            self.evidence_class = ec
            self.entry_price_usd = px
            self.entry_timestamp = "2026-08-16T12:00:00+00:00"
            self.mint = "M"
            self.horizons_resolved = ""
            self.fully_resolved = 0

    def test_a_holder_snapshot_WITH_a_price_is_still_refused(self):
        """THE case. Under the old code this would have resolved happily,
        because the only gate was the missing price."""
        from lib.wallet_alpha import resolve_observation
        out = resolve_observation(None, self._Row(HOLDER_SNAPSHOT, 1.23),
                               lambda *_a, **_k: 2.0)
        self.assertEqual(out["resolved"], [])
        self.assertIn("not an entry", out["skipped"])

    def test_a_pool_signer_with_a_price_is_refused(self):
        from lib.wallet_alpha import resolve_observation
        out = resolve_observation(None, self._Row(POOL_TX_SIGNER, 1.23),
                               lambda *_a, **_k: 2.0)
        self.assertEqual(out["resolved"], [])

    def test_a_verified_buy_is_allowed_through_the_class_gate(self):
        from lib.wallet_alpha import resolve_observation
        out = resolve_observation(None, self._Row(VERIFIED_BUY_ENTRY, 1.23),
                               lambda *_a, **_k: 2.0)
        self.assertNotIn("not an entry", out.get("skipped") or "")

    def test_the_class_gate_precedes_the_price_gate(self):
        """Ordering matters: an ineligible class must be refused for being
        ineligible, not for happening to lack a price."""
        from lib.wallet_alpha import resolve_observation
        out = resolve_observation(None, self._Row(HOLDER_SNAPSHOT, None),
                               lambda *_a, **_k: 2.0)
        self.assertIn("not an entry", out["skipped"])


class DiscoveryTagsItsSightingsTests(unittest.TestCase):
    def test_observe_passes_an_evidence_class(self):
        import inspect

        from lib import wallet_discovery
        src = inspect.getsource(wallet_discovery._observe)
        self.assertIn("evidence_class", src,
                      "discovery must classify the strength of its sightings")


if __name__ == "__main__":
    unittest.main()
