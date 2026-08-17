"""The mark stopped being the fill.

    price  = _get_current_price(sym, prices) or sig["entry_price"] or 0.0
    result = open_paper_position(sig, current_price=price)

`_get_current_price` walks Alpaca's last price, a MarketAsset row, then a
yfinance cache — three MARKS. Handing one to `open_paper_position` made it
the entry fill, so every paper position ever opened here filled at a price no
order could have executed at: mid at best, and better than mid whenever the
spread was wide. That is a simulator that makes money because it is wrong.

Entry now crosses the venue's own book. The mark survives as decision_price,
which is what makes slippage measurable at all.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import canonical_entry as CE
from lib import execution_policy as POL
from lib.paper_settlement import (COST_MODEL_CANONICAL, COST_MODEL_LEGACY,
                                  EXECUTION_MODEL_CANONICAL)


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _kraken(bid, ask, age_s=0.2):
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": bid, "ask": ask, "at": _at(age_s)},
        trade_flow=lambda symbol, window=200: None)


SIGNAL = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
          "paper_direction": "Long", "entry_price": 100.0,
          "stop_loss": 95.0, "target_price": 115.0, "timeframe": "4H",
          "id": "sig-1"}


class TheVenueBookIsTheFillAuthorityTests(unittest.TestCase):

    def _open(self, signal=None, bid=99.90, ask=100.10, decision=100.00):
        captured = {}

        def fake_open(sig, current_price=None):
            captured["fill"] = current_price
            return {"ok": True, "position": {"id": "pos-test"}}

        with _kraken(bid, ask), \
             patch("lib.paper_engine.open_paper_position", fake_open):
            res = CE.open_canonical_position(signal or SIGNAL,
                                             decision_price=decision)
        return res, captured

    def test_a_market_buy_settles_at_or_above_the_ask(self):
        """BUY lifts the ask. Filling at the mid would hand the model half
        the spread on every trade — free money the market never offers."""
        res, cap = self._open(bid=99.90, ask=100.10, decision=100.00)
        self.assertTrue(res.get("ok"))
        self.assertGreaterEqual(cap["fill"], 100.10)

    def test_the_fill_is_never_the_mark(self):
        res, cap = self._open(bid=99.90, ask=100.10, decision=100.00)
        self.assertNotEqual(cap["fill"], 100.00, "the mark became the fill again")

    def test_a_market_sell_settles_at_or_below_the_bid(self):
        short = dict(SIGNAL, paper_direction="Short", stop_loss=105.0,
                     target_price=85.0)
        res, cap = self._open(signal=short, bid=99.90, ask=100.10)
        self.assertTrue(res.get("ok"))
        self.assertLessEqual(cap["fill"], 99.90)

    def test_the_decision_price_is_preserved_alongside_the_fill(self):
        """Slippage is only measurable against what we intended to pay."""
        res, _ = self._open(decision=100.00)
        ex = res["execution"]
        self.assertEqual(ex["decision_price"], 100.00)
        self.assertNotEqual(ex["fill_price"], ex["decision_price"])
        self.assertEqual(ex["bid_at_submit"], 99.90)
        self.assertEqual(ex["ask_at_submit"], 100.10)

    def test_a_wider_spread_costs_more(self):
        _, tight = self._open(bid=99.99, ask=100.01)
        _, wide = self._open(bid=99.00, ask=101.00)
        self.assertGreater(wide["fill"], tight["fill"])


class NoExecutableDataMeansNoPositionTests(unittest.TestCase):
    """A VENUE failure is not a verdict on the thesis."""

    def _attempt(self, quote):
        opened = {"called": False}

        def fake_open(sig, current_price=None):
            opened["called"] = True
            return {"ok": True, "position": {"id": "pos-test"}}

        with patch("lib.kraken_stream.latest_quote", return_value=quote), \
             patch("lib.kraken_stream.trade_flow", return_value=None), \
             patch("lib.paper_engine.open_paper_position", fake_open):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)
        return res, opened["called"]

    def test_a_stale_quote_opens_nothing(self):
        res, opened = self._attempt({"bid": 99.9, "ask": 100.1, "at": _at(600.0)})
        self.assertFalse(opened, "a stale quote must not reach settlement")
        self.assertEqual(res["error"], POL.STALE_EXECUTION_DATA)
        self.assertTrue(res["venue_failure"])

    def test_a_crossed_book_opens_nothing(self):
        res, opened = self._attempt({"bid": 101.0, "ask": 100.0, "at": _at(0.1)})
        self.assertFalse(opened)
        self.assertEqual(res["error"], POL.CROSSED_BOOK)
        self.assertTrue(res["venue_failure"])

    def test_a_one_sided_book_opens_nothing(self):
        res, opened = self._attempt({"bid": 99.9, "ask": None, "at": _at(0.1)})
        self.assertFalse(opened)
        self.assertEqual(res["error"], POL.ONE_SIDED_BOOK)

    def test_a_missing_quote_opens_nothing(self):
        res, opened = self._attempt(None)
        self.assertFalse(opened)
        self.assertTrue(res["venue_failure"])

    def test_every_venue_refusal_is_flagged_as_such(self):
        """It is what stops the scheduler counting it as a rejected thesis."""
        for quote in ({"bid": 99.9, "ask": 100.1, "at": _at(600.0)},
                      {"bid": 101.0, "ask": 100.0, "at": _at(0.1)}, None):
            with self.subTest(quote=quote):
                res, _ = self._attempt(quote)
                self.assertTrue(POL.is_venue_data_failure(res["error"]))

    def test_futures_are_refused_before_any_quote_is_sought(self):
        fut = dict(SIGNAL, asset_symbol="ES=F", asset_class="Futures")
        opened = {"called": False}

        def fake_open(sig, current_price=None):
            opened["called"] = True
            return {"ok": True}

        with patch("lib.paper_engine.open_paper_position", fake_open):
            res = CE.open_canonical_position(fut, decision_price=5432.25)
        self.assertFalse(opened["called"])
        self.assertEqual(res["error"], POL.UNSUPPORTED_VIRTUAL_VENUE)

    def test_an_unreadable_side_is_not_a_venue_failure(self):
        """A bad order is the caller's fault, and must not be excused as a
        venue outage."""
        bad = dict(SIGNAL, paper_direction="sideways")
        with _kraken(99.9, 100.1):
            res = CE.open_canonical_position(bad, decision_price=100.0)
        self.assertFalse(res["venue_failure"])
        self.assertIn("unparseable", res["error"])


class ProvenanceSaysWhichSimulatorProducedItTests(unittest.TestCase):

    def test_a_canonical_position_records_its_models_and_epoch(self):
        stamped = {}

        class _Pos:
            execution_provenance = None

        pos = _Pos()

        def fake_open(sig, current_price=None):
            return {"ok": True, "position": {"id": "pos-1"}}

        from contextlib import contextmanager

        @contextmanager
        def fake_db():
            class _DB:
                def query(self, *a):
                    return self
                def filter(self, *a):
                    return self
                def first(self):
                    return pos
                def commit(self):
                    stamped["done"] = True
            yield _DB()

        with _kraken(99.90, 100.10), \
             patch("lib.paper_engine.open_paper_position", fake_open), \
             patch("app.database.get_db", fake_db):
            CE.open_canonical_position(SIGNAL, decision_price=100.0)

        doc = json.loads(pos.execution_provenance)
        # FALSE PROVENANCE IS WORSE THAN NONE. The fill genuinely crossed the
        # venue book, so execution_model is canonical. The ledger still ran
        # legacy round-trip fee accounting, so cost_model says LEGACY until
        # per-leg settlement exists. This test previously asserted per_leg_v2
        # — a claim the accounting did not support.
        self.assertEqual(doc["cost_model"], COST_MODEL_LEGACY)
        self.assertNotEqual(doc["cost_model"], COST_MODEL_CANONICAL)
        self.assertEqual(doc["execution_model"], EXECUTION_MODEL_CANONICAL)
        self.assertEqual(doc["engine_epoch"], CE.CANONICAL_ENGINE_EPOCH)
        self.assertEqual(doc["source"], "VIRTUAL_CEX_AGENT")
        self.assertEqual(doc["venue"], "kraken")
        self.assertEqual(doc["bid_at_submit"], 99.90)
        self.assertEqual(doc["ask_at_submit"], 100.10)
        self.assertIsNotNone(doc["actual_entry_fill"])

    def test_new_outcomes_never_use_the_ambiguous_legacy_source(self):
        """9,370 historical rows say outcome_source="live" and mean
        forward-observed VIRTUAL. New data must not add to that confusion."""
        pos = type("P", (), {"execution_provenance": None})()
        from contextlib import contextmanager

        @contextmanager
        def fake_db():
            class _DB:
                def query(self, *a): return self
                def filter(self, *a): return self
                def first(self): return pos
                def commit(self): pass
            yield _DB()

        with _kraken(99.9, 100.1), \
             patch("lib.paper_engine.open_paper_position",
                   lambda s, current_price=None: {"ok": True, "position": {"id": "p"}}), \
             patch("app.database.get_db", fake_db):
            CE.open_canonical_position(SIGNAL, decision_price=100.0)
        doc = json.loads(pos.execution_provenance)
        self.assertNotEqual(doc["source"], "live")
        self.assertNotIn("LIVE_CEX", doc["source"])

    def test_a_legacy_position_is_not_guessed_into_canonical(self):
        """NULL provenance means legacy. "It is crypto so it was probably
        Kraken" is a guess, and a guess in the execution ledger is
        indistinguishable from a measurement."""
        legacy = type("P", (), {"execution_provenance": None})()
        self.assertFalse(CE.is_canonical(legacy))
        self.assertIsNone(CE.provenance_of(legacy))

    def test_unparseable_provenance_is_not_canonical(self):
        broken = type("P", (), {"execution_provenance": "{not json"})()
        self.assertFalse(CE.is_canonical(broken))


class CanonicalPositionsCannotUseLegacyCloseArithmeticTests(unittest.TestCase):
    """Pass A wired entry; exit is Pass B. Until then a canonical position
    must not settle at whatever price it is handed."""

    def _canonical_pos(self):
        return type("P", (), {"execution_provenance": json.dumps(
            {"execution_model": EXECUTION_MODEL_CANONICAL})})()

    def test_the_guard_refuses_a_canonical_position(self):
        from lib.paper_engine import _refuse_legacy_close
        refusal = _refuse_legacy_close(self._canonical_pos())
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["error"],
                         "CANONICAL_POSITION_REQUIRES_EXECUTION_SETTLEMENT")
        self.assertFalse(refusal["ok"])

    def test_the_guard_leaves_legacy_positions_alone(self):
        from lib.paper_engine import _refuse_legacy_close
        legacy = type("P", (), {"execution_provenance": None})()
        self.assertIsNone(_refuse_legacy_close(legacy))

    def test_both_close_paths_consult_the_guard(self):
        """By AST — the prose explaining the guard must not satisfy it."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "lib" / "paper_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fname in ("close_paper_position", "partial_close_paper_position"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == fname)
            calls = {c.func.id for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            with self.subTest(function=fname):
                self.assertIn("_refuse_legacy_close", calls)


class TheAutonomousEntrySiteNoLongerFillsAtTheMarkTests(unittest.TestCase):

    def test_paper_trading_calls_the_canonical_entry(self):
        """AST, over the runtime module. A direct open_paper_position call
        with a mark would reintroduce the defect."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent
               / "jobs" / "paper_trading.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = {c.func.id for c in ast.walk(tree)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertIn("open_canonical_position", called)
        self.assertNotIn("open_paper_position", called,
                         "the autonomous path must not open positions directly")


if __name__ == "__main__":
    unittest.main()
