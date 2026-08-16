"""Cursor pagination, and the page cap the API actually enforces.

Two defects:

1. The collector fetched ONE page, saw `pagination.hasMore`, recorded the
   wallet as "truncated" and stopped. It never followed `nextCursor`. A
   wallet busier than one page between polls permanently lost the
   overflow — and the busiest wallets are precisely the ones worth
   watching, so the loss was concentrated exactly where it hurt most.

2. `HELIUS_PAGE_LIMIT` was clamped to 1000. The Transfers endpoint serves
   at most 100 per page, so every larger value was rejected or silently
   clamped while the caller believed it had asked for ten times more
   history than it received. `.env.example` compounded it by advertising
   HELIUS_BACKFILL_LIMIT=500 against a collector that fetched one page.

Depth now comes from PAGES, bounded by explicit budgets, and a partial
read announces itself instead of implying completeness.
"""
import unittest
from unittest.mock import patch

from lib.helius_client import MAX_TRANSFER_PAGE, transfers_paged


def page(n_rows: int, cursor: str | None):
    return {"data": [{"signature": f"s{i}", "amount": 1.0, "direction": "in"}
                     for i in range(n_rows)],
            "pagination": {"hasMore": cursor is not None,
                           "nextCursor": cursor}}


class CursorFollowingTests(unittest.TestCase):
    def test_two_pages_are_ingested_exactly_once(self):
        """The audit's stated case: 100 + 40 = 140, no duplicates."""
        pages = [page(100, "A"), page(40, None)]
        with patch("lib.helius_client.transfers",
                   side_effect=pages) as t:
            out = transfers_paged("addr")
        self.assertEqual(out["transfers_fetched"], 140)
        self.assertEqual(out["pages_fetched"], 2)
        self.assertTrue(out["fully_drained"])
        self.assertFalse(out["truncated_due_budget"])
        # The cursor from page 1 must have been sent with page 2.
        self.assertEqual(t.call_args_list[1].kwargs.get("cursor"), "A")

    def test_the_first_call_sends_no_cursor(self):
        with patch("lib.helius_client.transfers",
                   side_effect=[page(3, None)]) as t:
            transfers_paged("addr")
        self.assertIsNone(t.call_args_list[0].kwargs.get("cursor"))

    def test_a_single_page_needs_no_second_call(self):
        with patch("lib.helius_client.transfers",
                   side_effect=[page(10, None)]) as t:
            out = transfers_paged("addr")
        self.assertEqual(t.call_count, 1)
        self.assertTrue(out["fully_drained"])

    def test_hasMore_without_a_cursor_stops_rather_than_looping(self):
        body = {"data": [{"signature": "s"}],
                "pagination": {"hasMore": True, "nextCursor": None}}
        with patch("lib.helius_client.transfers", return_value=body):
            out = transfers_paged("addr")
        self.assertEqual(out["pages_fetched"], 1)


class BudgetTests(unittest.TestCase):
    """One pathological wallet must not spend the whole Helius budget."""

    def test_max_pages_stops_the_walk_and_says_so(self):
        with patch("lib.helius_client.transfers",
                   side_effect=lambda *a, **k: page(100, "more")):
            out = transfers_paged("addr", max_pages=3, max_records=10_000)
        self.assertEqual(out["pages_fetched"], 3)
        self.assertTrue(out["truncated_due_budget"])
        self.assertFalse(out["fully_drained"])
        self.assertIn("max_pages", out["truncation_reason"])

    def test_max_records_stops_the_walk_and_says_so(self):
        with patch("lib.helius_client.transfers",
                   side_effect=lambda *a, **k: page(100, "more")):
            out = transfers_paged("addr", max_records=250, max_pages=99)
        self.assertLessEqual(out["transfers_fetched"], 250)
        self.assertTrue(out["truncated_due_budget"])
        self.assertIn("max_records", out["truncation_reason"])

    def test_truncation_is_never_silent(self):
        """A partial read that reports completeness is the failure this
        whole change exists to prevent."""
        with patch("lib.helius_client.transfers",
                   side_effect=lambda *a, **k: page(100, "more")):
            out = transfers_paged("addr", max_pages=2)
        self.assertTrue(out["truncated_due_budget"])
        self.assertIsNotNone(out["truncation_reason"])


class PageSizeCapTests(unittest.TestCase):
    def test_a_request_never_exceeds_the_api_maximum(self):
        """HELIUS_PAGE_LIMIT=500 must not become a 500-record request."""
        with patch("lib.helius_client._request") as r:
            r.return_value.json.return_value = {"data": []}
            from lib.helius_client import transfers
            transfers("addr", limit=500)
        sent = r.call_args.kwargs["params"]["limit"]
        self.assertLessEqual(sent, MAX_TRANSFER_PAGE)

    def test_the_collector_clamps_its_configured_page_size(self):
        import os

        from lib.wallet_activity import _page_limit
        old = os.environ.get("HELIUS_PAGE_LIMIT")
        try:
            os.environ["HELIUS_PAGE_LIMIT"] = "500"
            self.assertEqual(_page_limit(), MAX_TRANSFER_PAGE)
            os.environ["HELIUS_PAGE_LIMIT"] = "50"
            self.assertEqual(_page_limit(), 50)
        finally:
            if old is None:
                os.environ.pop("HELIUS_PAGE_LIMIT", None)
            else:
                os.environ["HELIUS_PAGE_LIMIT"] = old

    def test_backfill_limit_is_a_total_not_a_page_size(self):
        """It advertised 500 while one page of 100 was fetched."""
        import os

        from lib.wallet_activity import _backfill_limit, _page_limit
        old = os.environ.get("HELIUS_BACKFILL_LIMIT")
        try:
            os.environ["HELIUS_BACKFILL_LIMIT"] = "500"
            self.assertEqual(_backfill_limit(), 500)
            self.assertGreater(_backfill_limit(), _page_limit())
        finally:
            if old is None:
                os.environ.pop("HELIUS_BACKFILL_LIMIT", None)
            else:
                os.environ["HELIUS_BACKFILL_LIMIT"] = old


class NoSecondPaginatorTests(unittest.TestCase):
    def test_pagination_is_not_reimplemented_outside_the_client(self):
        import inspect

        from lib import wallet_activity
        src = inspect.getsource(wallet_activity)
        self.assertNotIn("nextCursor", src,
                         "pagination belongs in helius_client only")


if __name__ == "__main__":
    unittest.main()
