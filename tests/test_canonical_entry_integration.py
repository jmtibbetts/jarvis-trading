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


# Declares a SPOT expression: `kraken` is the spot WebSocket, and that is
# the only crypto product this desk has an executable feed for.
SIGNAL = {
    "asset_symbol": "BTC/USD", "asset_class": "Crypto",
    "paper_direction": "Long", "entry_price": 100.0,
    "stop_loss": 95.0, "target_price": 115.0,
    "timeframe": "4H", "confidence": 75, "product": "CRYPTO_SPOT",
}


class TheRealReturnContractTests(unittest.TestCase):
    """Pin the shape, so a future mock cannot drift from it unnoticed."""

    def _paper_engine_tree(self):
        import ast
        import pathlib
        return ast.parse((pathlib.Path(__file__).parent.parent
                          / "lib" / "paper_engine.py").read_text(encoding="utf-8"))

    def test_settlement_returns_position_not_position_id(self):
        """The success contract is built by SETTLE — the half that actually
        creates the row. open_paper_position is now the composition of
        prepare_entry and settle_position_entry, so the shape is asserted where it is
        constructed rather than where it is returned from."""
        import ast
        fn = next(n for n in ast.walk(self._paper_engine_tree())
                  if isinstance(n, ast.FunctionDef) and n.name == "settle_position_entry")
        keys = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys
                            if isinstance(k, ast.Constant))
        self.assertIn("position", keys)

    def test_no_position_id_key_exists_anywhere_in_paper_engine(self):
        """Strictly wider than the original check, which only read one
        function: the key this codebase has never returned must not appear
        in ANY of them, so a mock cannot find one to imitate."""
        import ast
        keys = set()
        for node in ast.walk(self._paper_engine_tree()):
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys
                            if isinstance(k, ast.Constant))
        self.assertNotIn("position_id", keys,
                         "no top-level position_id exists; mocks must not invent one")

    def test_canonical_entry_never_reads_the_invented_flat_key(self):
        """The original defect, kept as a regression.

        Provenance now travels INTO settlement, so the id is not read back
        at all — but the key this codebase never returned must not reappear.
        """
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "canonical_entry.py").read_text(encoding="utf-8")
        code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
        self.assertNotIn('result.get("position_id")', code)
        self.assertNotIn('result["position_id"]', code)


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

    def test_cash_fell_by_the_committed_margin_plus_the_entry_fee(self):
        """A2. Free cash pays for BOTH: the margin it commits and the fee it
        actually spends. Only the second is a cost."""
        from app.database import PaperPosition, get_db
        before, _ = self._portfolio_cash()
        res = self._open()
        after, _ = self._portfolio_cash()

        margin = float(res["position"]["margin_required"])
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            fee = json.loads(pos.execution_provenance)["entry_fee_usd"]

        self.assertGreater(fee, 0.0, "the entry leg was settled free")
        self.assertAlmostEqual(before - after, margin + fee, places=4)

    def test_committed_margin_is_not_a_loss(self):
        """THE ACCOUNTING FACT. Margin MOVED, it did not vanish:

            equity = free_cash + committed_margin + unrealized_pnl

        so opening a position reduces equity by the FEE alone. Treating the
        margin as spent would report a $1,000 commitment as a $1,000 loss the
        instant the trade opened.
        """
        from app.database import PaperPosition, get_db
        before_cash, _ = self._portfolio_cash()
        res = self._open()
        after_cash, _ = self._portfolio_cash()

        margin = float(res["position"]["margin_required"])
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            fee = json.loads(pos.execution_provenance)["entry_fee_usd"]
            committed = float(pos.margin_used or 0.0)

        self.assertAlmostEqual(committed, margin, places=4)
        equity_before = before_cash
        equity_after = after_cash + committed          # cash + committed margin
        self.assertAlmostEqual(equity_before - equity_after, fee, places=4,
                               msg="equity fell by more than the fee — margin "
                                   "is being treated as vanished capital")
        self.assertLess(fee, margin,
                        "a fee the size of the margin is a units error")

    def test_the_provenance_states_the_cost_model_that_actually_settled(self):
        """FALSE PROVENANCE IS WORSE THAN NONE — which is why this asserted
        COST_MODEL_LEGACY until A2. The label moved only when the behaviour
        did: the entry leg is now priced at the executed size and DEBITED at
        settlement, so `fees` stays 0 and the exit cannot charge it again."""
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
            fee_basis = pos.fee_basis

        self.assertEqual(doc["execution_model"], EXECUTION_MODEL_CANONICAL)
        self.assertEqual(doc["cost_model"], COST_MODEL_CANONICAL)
        self.assertNotEqual(doc["cost_model"], COST_MODEL_LEGACY)
        # THE EVIDENCE FOR THE CLAIM. A deferred round-trip estimate left on
        # the row would be billed again at close — the double charge the
        # per-leg model exists to remove.
        self.assertEqual(stored_fee, 0.0,
                         "a legacy deferred round-trip fee is still on the row")
        self.assertEqual(fee_basis, "per_leg_v2_entry")
        self.assertGreater(doc["entry_fee_usd"], 0.0)

    def test_the_persisted_position_satisfies_the_tightened_classifier(self):
        """is_canonical now requires all four claims, and a position this
        path produces must satisfy every one of them."""
        from app.database import PaperPosition, get_db
        from lib.canonical_entry import is_canonical
        res = self._open()
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            doc = json.loads(pos.execution_provenance)
            self.assertTrue(is_canonical(pos))
        self.assertTrue(str(doc["entry_execution_id"]).strip())

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


class SettlementIsAtomicTests(unittest.TestCase):
    """A7. The claim "re-raising unwinds it" was FALSE.

    `get_db()` commits on context exit, so open_paper_position committed
    position and cash, and provenance was persisted by a SECOND session.
    When that failed the first was already committed. Measured on a
    disposable book before the fix: $1,250.17 debited, one orphan position,
    provenance NULL — and a NULL-provenance position is invisible to
    is_canonical(), so the fail-closed exit guard would have let it through
    the legacy close path. The exception unwound nothing.

    Provenance is now built first and handed to settlement, so position,
    cash and provenance commit together or not at all.
    """

    def _cash_and_count(self):
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            cash = float(db.query(PaperPortfolio).first().cash)
            n = db.query(PaperPosition).filter(
                PaperPosition.status == "Open").count()
        return cash, n

    def setUp(self):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    # THE GUARANTEE IS THE PROPERTY, NOT THE MECHANISM.
    #
    # These asserted that the injected RuntimeError ESCAPED
    # `open_canonical_position`. A.1.1 made the entry path catch a rolled-back
    # settlement and return a named fail-closed refusal instead, which is
    # what a caller can actually act on. The property A7 exists to protect —
    # NO ECONOMIC MUTATION SURVIVES A FAILED ENTRY — is unchanged and is now
    # asserted directly, along with the refusal itself, rather than through
    # the exception that used to carry it.

    def test_a_failure_during_settlement_rolls_back_cash_and_position(self):
        import lib.paper_engine as PE
        from lib.canonical_entry import open_canonical_position

        before_cash, before_n = self._cash_and_count()
        with _kraken_quote(), patch.object(PE, "json") as J:
            J.dumps.side_effect = RuntimeError("injected provenance failure")
            res = open_canonical_position(SIGNAL, decision_price=100.0)
        after_cash, after_n = self._cash_and_count()

        self.assertFalse(res.get("ok"), "a failed entry reported success")
        self.assertEqual(res["error"], "SETTLEMENT_ROLLED_BACK")
        self.assertEqual(after_cash, before_cash, "cash was debited by a failed entry")
        self.assertEqual(after_n, before_n, "a failed entry left a position behind")

    def test_no_orphan_position_with_null_provenance_can_exist(self):
        """The specific state the guard cannot see."""
        import lib.paper_engine as PE
        from app.database import PaperPosition, get_db
        from lib.canonical_entry import open_canonical_position

        with _kraken_quote(), patch.object(PE, "json") as J:
            J.dumps.side_effect = RuntimeError("injected provenance failure")
            res = open_canonical_position(SIGNAL, decision_price=100.0)
        self.assertFalse(res.get("ok"))

        with get_db() as db:
            orphans = db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD",
                PaperPosition.status == "Open",
                PaperPosition.execution_provenance.is_(None)).count()
        self.assertEqual(orphans, 0)

    def test_the_happy_path_still_persists_provenance_in_one_transaction(self):
        from app.database import PaperPosition, get_db
        from lib.canonical_entry import is_canonical, open_canonical_position
        with _kraken_quote():
            res = open_canonical_position(SIGNAL, decision_price=100.0)
        self.assertTrue(res.get("ok"))
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == res["position"]["id"]).first()
            self.assertIsNotNone(pos.execution_provenance)
            self.assertTrue(is_canonical(pos))

    def test_provenance_is_not_stamped_after_the_commit(self):
        """By AST: no separate get_db session may persist provenance."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "canonical_entry.py").read_text(encoding="utf-8")
        called = {c.func.id for c in ast.walk(ast.parse(src))
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertNotIn("get_db", called,
                         "canonical entry must not open its own session; "
                         "settlement owns the transaction")


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
