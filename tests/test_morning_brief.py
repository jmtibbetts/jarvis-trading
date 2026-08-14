"""The brief assembles; it never computes. Pinned: complete shape on an
empty desk (a fresh DB must yield zeros, not crashes), window arithmetic,
and the releases calendar by weekday.
"""
import unittest
from unittest.mock import patch

from app.database import init_db
from lib.morning_brief import build_brief


class BriefShapeTests(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_empty_desk_yields_full_shape_with_zeros(self):
        b = build_brief(24)
        self.assertEqual(b["window_hours"], 24)
        for key in ("gate_experiment", "corpus", "book", "platform",
                    "positioning_extremes", "releases_today"):
            self.assertIn(key, b)
        self.assertIsInstance(b["gate_experiment"]["arms"], dict)
        self.assertEqual(b["book"]["open_positions"], 0)
        self.assertIsInstance(b["corpus"]["ablation_coverage"]
                              ["resolved_total"], int)

    def test_releases_follow_the_weekday(self):
        import lib.morning_brief as mb
        from datetime import datetime, timezone

        cases = {2: "petroleum", 3: "natgas", 4: "COT", 5: "weekend"}
        for wd, needle in cases.items():
            fake = datetime(2026, 8, 10 + wd, 12, tzinfo=timezone.utc)
            self.assertEqual(fake.weekday(), wd)
            with patch.object(mb, "_now", return_value=fake):
                joined = " ".join(mb._releases_today())
            self.assertIn(needle, joined, f"weekday {wd}")

    def test_monday_has_no_phantom_releases(self):
        import lib.morning_brief as mb
        from datetime import datetime, timezone

        fake = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)  # Monday
        with patch.object(mb, "_now", return_value=fake):
            self.assertEqual(mb._releases_today(), [])


if __name__ == "__main__":
    unittest.main()
