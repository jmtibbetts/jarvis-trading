"""The attention queue — the Command Center's input.

The property worth pinning is not that items appear. It is that an EMPTY
queue is honest. "Nothing needs you" is a strong claim, and it is exactly
what a silently-failing producer fabricates: if the position-risk scan
throws and is swallowed, the queue renders empty and reads as all-clear
while a position sits one tick off its stop.

So a failing producer must not empty the queue, must not be silent, and
must flip `complete` to false.
"""
import unittest

import lib.attention as attention
from lib.attention import (CATEGORIES, CRITICAL, HIGH, MEDIUM,
                           AttentionItem, collect)


def _item(priority=HIGH, category="SYSTEM", title="t"):
    return AttentionItem(id=f"x:{title}", priority=priority, category=category,
                         title=title, reason="r", source="s",
                         detected_at="2026-08-17T00:00:00+00:00")


class AFailingProducerIsNeverSilent(unittest.TestCase):
    def setUp(self):
        self._real = attention.PRODUCERS

    def tearDown(self):
        attention.PRODUCERS = self._real

    def test_one_dead_producer_does_not_empty_the_queue(self):
        def boom(_now):
            raise RuntimeError("db gone")
        attention.PRODUCERS = (("boom", boom),
                               ("ok", lambda now: [_item(title="survivor")]))
        q = collect()
        self.assertEqual(q["total"], 1)
        self.assertEqual(q["items"][0]["title"], "survivor")

    def test_the_failure_is_reported_and_completeness_is_lost(self):
        def boom(_now):
            raise RuntimeError("db gone")
        attention.PRODUCERS = (("boom", boom),)
        q = collect()
        self.assertFalse(q["complete"])
        self.assertEqual(len(q["degraded"]), 1)
        self.assertEqual(q["degraded"][0]["producer"], "boom")
        self.assertIn("db gone", q["degraded"][0]["error"])

    def test_a_degraded_queue_says_what_the_gap_means(self):
        """"RuntimeError" tells an operator nothing. The consequence —
        that this category cannot appear at all — is the part that
        changes what they should conclude from an empty list."""
        def boom(_now):
            raise RuntimeError("x")
        attention.PRODUCERS = (("positions_near_stop", boom),)
        q = collect()
        self.assertIn("does not rule this category out",
                      q["degraded"][0]["means"])

    def test_an_empty_queue_from_healthy_producers_is_complete(self):
        attention.PRODUCERS = (("quiet", lambda now: []),)
        q = collect()
        self.assertEqual(q["total"], 0)
        self.assertTrue(q["complete"])
        self.assertEqual(q["degraded"], [])


class RankingIsByUrgency(unittest.TestCase):
    def setUp(self):
        self._real = attention.PRODUCERS

    def tearDown(self):
        attention.PRODUCERS = self._real

    def test_critical_outranks_high_outranks_medium(self):
        attention.PRODUCERS = (("p", lambda now: [
            _item(MEDIUM, title="m"), _item(CRITICAL, title="c"),
            _item(HIGH, title="h")]),)
        titles = [i["title"] for i in collect()["items"]]
        self.assertEqual(titles, ["c", "h", "m"])

    def test_counts_are_reported_per_priority_and_category(self):
        attention.PRODUCERS = (("p", lambda now: [
            _item(CRITICAL, "DATA", "a"), _item(HIGH, "DATA", "b"),
            _item(HIGH, "SYSTEM", "c")]),)
        q = collect()
        self.assertEqual(q["by_priority"], {CRITICAL: 1, HIGH: 2})
        self.assertEqual(q["by_category"], {"DATA": 2, "SYSTEM": 1})

    def test_truncation_is_reported_rather_than_hidden(self):
        """A queue that quietly drops items understates how much needs
        doing, which is the same failure mode as an empty one."""
        attention.PRODUCERS = (("p", lambda now: [
            _item(title=str(n)) for n in range(10)]),)
        q = collect(limit=3)
        self.assertEqual(len(q["items"]), 3)
        self.assertEqual(q["total"], 10)
        self.assertEqual(q["truncated"], 7)


class ItemShape(unittest.TestCase):
    # Save and restore the REAL tuple. Filtering the stub back out would
    # leave PRODUCERS empty, and the database is per-session — every later
    # test file would then collect from a queue with no producers in it and
    # see a perfectly "complete" empty result.
    def setUp(self):
        self._real = attention.PRODUCERS

    def tearDown(self):
        attention.PRODUCERS = self._real

    def test_every_producer_is_callable(self):
        for _name, fn in attention.PRODUCERS:
            self.assertTrue(callable(fn))
        self.assertIn("POSITION_RISK", CATEGORIES)
        self.assertIn("APPROVAL", CATEGORIES)

    def test_items_carry_a_rank_for_the_ui_to_sort_on(self):
        attention.PRODUCERS = (("p", lambda now: [_item(CRITICAL)]),)
        self.assertEqual(collect()["items"][0]["rank"], 0)


class AgainstTheRealProducers(unittest.TestCase):
    """The real set must run end to end. The suite's database is empty, so
    this asserts the producers EXECUTE rather than that they find things —
    a producer that raises on an empty book would be a live outage."""

    def test_all_producers_run_on_an_empty_book(self):
        q = collect()
        self.assertTrue(q["complete"], f"degraded: {q['degraded']}")
        self.assertEqual(q["producers_run"], q["producers_total"])


if __name__ == "__main__":
    unittest.main()
