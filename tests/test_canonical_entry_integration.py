"""The test that would have caught A1, written against the real contract.

WHAT WENT WRONG. `canonical_entry` read `result["position_id"]`. The real
contract is `{"ok": True, "position": {"id": ...}}`, so it always got None,
`_stamp_provenance(None, ...)` returned immediately, and every "canonical"
position persisted with `execution_provenance` NULL. That means
`is_canonical()` was False for all of them, and the fail-closed exit guard —
the entire safety property of the Pass A checkpoint — was blind.

The unit tests passed throughout, because they mocked `open_paper_position`
as returning a top-level `position_id` that this codebase has never returned.
A test double that invents its own contract proves only that the double
agrees with itself.

So this exercises the REAL paper_engine against a REAL disposable database.
Only the market-data boundary is stubbed, because that is genuinely external.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


BID, ASK = 99.90, 100.10


def _kraken_quote():
    """Only the external venue feed is stubbed."""
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": BID, "ask": ASK, "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


SIGNAL = {
    "asset_symbol": "BTC/USD", "asset_class": "Crypto",
    "paper_direction": "Long", "entry_price": 100.0,
    "stop_loss": 95.0, "target_price": 115.0,
    "timeframe": "4H", "confidence": 75,
}


class TheRealReturnContractTests(unittest.TestCase):
    """Pin the shape, so a future mock cannot drift from it unnoticed."""

    def test_open_paper_position_returns_position_not_position_id(self):
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "paper_engine.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "open_paper_position")
        keys = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys
                            if isinstance(k, ast.Constant))
        self.assertIn("position", keys)
        self.assertNotIn("position_id", keys,
                         "no top-level position_id exists; mocks must not invent one")

    def test_canonical_entry_reads_the_real_path(self):
        """It must consume result["position"]["id"], not a flat key."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "canonical_entry.py").read_text(encoding="utf-8")
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        self.assertIn('result.get("position")', code)
        self.assertNotIn('result.get("position_id")', code)


class CanonicalEntryAgainstARealDatabaseTests(unittest.TestCase):
    """No mock of paper_engine. Real sizing, real cash, real row."""

    def _portfolio_cash(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return float(p.cash), p

    def _open(self):
        from lib.canonical_entry import open_canonical_position
        with _kraken_quote():
            return open_canonical_position(SIGNAL, decision_price=100.0)

    def setUp(self):
        # Each test starts from a book with no BTC/USD position.
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    def test_a_canonical_entry_persists_non_null_provenance(self):
        """THE BUG. Every position used to land with provenance NULL."""
        from app.database import PaperPosition, get_db
        res = self._open()
        self.assertTrue(res.get("ok"), res)
        pos_id = res["position"]["id"]

        with get_db() as db:
            pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
            self.assertIsNotNone(pos)
            self.assertIsNotNone(pos.execution_provenance,
                                 "canonical entry persisted NULL provenance")
            doc = json.loads(pos.execution_provenance)

        self.assertEqual(doc["venue"], "kraken")
        self.assertEqual(doc["source"], "VIRTUAL_CEX_AGENT")
        self.assertEqual(doc["bid_at_submit"], BID)
        self.assertEqual(doc["ask_at_submit"], ASK)

    def test_the_persisted_position_is_recognised_as_canonical(self):
        """Without this the fail-closed exit guard cannot see it."""
        from app.database import PaperPosition, get_db
        from lib.canonical_entry import is_canonical
        res = self._open()
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            self.assertTrue(is_canonical(pos))

    def test_the_legacy_close_path_refuses_the_persisted_position(self):
        """End to end: a real row, through the real guard."""
        from lib.paper_engine import close_paper_position
        res = self._open()
        out = close_paper_position(res["position"]["id"], 105.0, reason="test")
        self.assertEqual(out.get("error"),
                         "CANONICAL_POSITION_REQUIRES_EXECUTION_SETTLEMENT")

    def test_the_legacy_partial_close_path_refuses_it_too(self):
        from lib.paper_engine import partial_close_paper_position
        res = self._open()
        out = partial_close_paper_position(res["position"]["id"], 0.5, 105.0)
        self.assertEqual(out.get("error"),
                         "CANONICAL_POSITION_REQUIRES_EXECUTION_SETTLEMENT")

    def test_the_entry_fill_crossed_the_ask_rather_than_the_mark(self):
        """decision_price was 100.00, strictly inside the spread."""
        res = self._open()
        entry = float(res["position"]["entry_price"])
        self.assertGreaterEqual(entry, ASK)
        self.assertNotEqual(entry, 100.00)

    def test_cash_fell_by_the_margin_that_was_actually_committed(self):
        before, _ = self._portfolio_cash()
        res = self._open()
        after, _ = self._portfolio_cash()
        margin = float(res["position"]["margin_required"])
        self.assertAlmostEqual(before - after, margin, places=4)

    def test_the_provenance_states_the_cost_model_that_actually_settled(self):
        """FALSE PROVENANCE IS WORSE THAN NONE. The fill genuinely came from
        the venue book, so execution_model is canonical — but the ledger
        still ran legacy round-trip fee accounting, so cost_model must say
        so until per-leg settlement exists."""
        from app.database import PaperPosition, get_db
        from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                          COST_MODEL_LEGACY,
                                          EXECUTION_MODEL_CANONICAL)
        res = self._open()
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            doc = json.loads(pos.execution_provenance)
            stored_fee = float(pos.fees or 0.0)

        self.assertEqual(doc["execution_model"], EXECUTION_MODEL_CANONICAL)
        self.assertEqual(doc["cost_model"], COST_MODEL_LEGACY)
        self.assertNotEqual(doc["cost_model"], COST_MODEL_CANONICAL)
        # And the evidence for that claim: a legacy round-trip fee is present.
        self.assertGreater(stored_fee, 0.0,
                           "legacy round-trip fee is still what settled")

    def test_a_venue_refusal_opens_nothing_and_moves_no_cash(self):
        from lib.canonical_entry import open_canonical_position
        before, _ = self._portfolio_cash()
        with patch("lib.kraken_stream.latest_quote",
                   return_value={"bid": BID, "ask": ASK, "at": _at(900.0)}), \
             patch("lib.kraken_stream.trade_flow", return_value=None):
            res = open_canonical_position(SIGNAL, decision_price=100.0)
        after, _ = self._portfolio_cash()
        self.assertTrue(res.get("venue_failure"))
        self.assertAlmostEqual(before, after, places=6)

    def test_a_second_identical_entry_does_not_double_open(self):
        """paper_engine already refuses a duplicate open per symbol; this
        proves the canonical path inherits that rather than bypassing it."""
        from app.database import PaperPosition, get_db
        first = self._open()
        self.assertTrue(first.get("ok"))
        second = self._open()
        self.assertFalse(second.get("ok"), second)
        with get_db() as db:
            n = db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD",
                PaperPosition.status == "Open").count()
        self.assertEqual(n, 1, "a duplicate entry opened a second position")


class VenueSubmissionIsProperlyTypedTests(unittest.TestCase):
    """F13. `execution = None` was shared class state dataclasses never saw."""

    def test_execution_is_a_real_dataclass_field(self):
        import dataclasses
        from lib.execution_venue import VenueSubmission
        names = [f.name for f in dataclasses.fields(VenueSubmission)]
        self.assertIn("execution", names)

    def test_execution_is_instance_local(self):
        from lib.execution_venue import VenueSubmission
        a = VenueSubmission(accepted=True, venue_family="virtual_cex")
        b = VenueSubmission(accepted=True, venue_family="virtual_cex")
        a.execution = "set-on-a-only"
        self.assertIsNone(b.execution)


if __name__ == "__main__":
    unittest.main()
