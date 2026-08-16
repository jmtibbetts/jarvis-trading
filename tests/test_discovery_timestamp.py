"""A wallet that bought 12 minutes early must not be recorded at T0.

The defect, exactly: `traders_of_pool()` built rows carrying `block_time`,
while `_observe()` read `row.get("timestamp")` and fell back to
`tok["surge_started_at"]`. The read missed EVERY time — the field simply
had a different name — so every discovered pre-surge buyer was stamped as
entering at the precise moment the surge began.

seconds_before_surge was therefore 0 for every observation ever recorded.
The single question this pipeline exists to answer — "who was early?" —
could only ever answer "nobody", and it answered it confidently, with a
populated timestamp column and no error anywhere.

Two rules come out of it:
  1. ONE canonical timestamp name, normalized at the source.
  2. A missing time is NULL, never a substituted default. Substituting T0
     does not fill the gap, it asserts the most misleading value available.
"""
import unittest

from lib.wallet_discovery import _event_timestamp

T0 = 1_700_000_000.0          # surge threshold crossed
TWELVE_MIN = 720.0


class CanonicalTimestampTests(unittest.TestCase):
    def test_block_time_is_found(self):
        """THE bug: this row shape returned None, every time."""
        self.assertEqual(_event_timestamp({"block_time": T0 - TWELVE_MIN}),
                         T0 - TWELVE_MIN)

    def test_every_known_source_field_resolves(self):
        for field in ("event_timestamp", "block_time", "blockTime",
                      "timestamp", "ts"):
            self.assertEqual(_event_timestamp({field: T0}), T0, field)

    def test_iso_strings_resolve(self):
        got = _event_timestamp({"timestamp": "2026-08-16T12:00:00Z"})
        self.assertIsNotNone(got)
        self.assertIsInstance(got, float)

    def test_missing_time_is_none_not_a_default(self):
        self.assertIsNone(_event_timestamp({"signature": "abc"}))
        self.assertIsNone(_event_timestamp({}))

    def test_zero_and_empty_are_not_valid_times(self):
        """Unix epoch 0 is not a Solana block time; it is a missing value."""
        self.assertIsNone(_event_timestamp({"block_time": 0}))
        self.assertIsNone(_event_timestamp({"block_time": ""}))
        self.assertIsNone(_event_timestamp({"block_time": None}))

    def test_canonical_field_wins_over_legacy_aliases(self):
        self.assertEqual(
            _event_timestamp({"event_timestamp": T0, "block_time": T0 - 999}),
            T0)


class SecondsBeforeSurgeTests(unittest.TestCase):
    """The audit's stated regression, verbatim."""

    def test_a_buy_twelve_minutes_early_is_720_seconds_early(self):
        row = {"owner": "W", "signature": "s", "block_time": T0 - TWELVE_MIN}
        entry = _event_timestamp(row)
        self.assertEqual(entry, T0 - TWELVE_MIN)
        self.assertEqual(T0 - entry, 720.0)

    def test_the_old_lookup_would_have_produced_zero(self):
        """Proves the fixture actually reproduces the original defect —
        without this, the test above could pass against broken code."""
        row = {"owner": "W", "signature": "s", "block_time": T0 - TWELVE_MIN}
        old = row.get("timestamp") or T0        # the exact old expression
        self.assertEqual(old, T0)
        self.assertEqual(T0 - old, 0.0, "old path recorded a 12-min-early buy at T0")


class TradersOfPoolEmitsCanonicalFieldTests(unittest.TestCase):
    def test_rows_carry_event_timestamp(self):
        """Guard the producer, so the two ends cannot drift apart again."""
        import inspect

        from lib import wallet_discovery
        src = inspect.getsource(wallet_discovery.traders_of_pool)
        self.assertIn("event_timestamp", src,
                      "traders_of_pool must emit the canonical field name")

    def test_observe_does_not_fall_back_to_surge_start(self):
        import inspect

        from lib import wallet_discovery
        src = inspect.getsource(wallet_discovery._observe)
        self.assertIn("_event_timestamp", src)
        self.assertNotIn('row.get("timestamp") or tok.get("surge_started_at")',
                         src, "the substituting fallback is back")


if __name__ == "__main__":
    unittest.main()
