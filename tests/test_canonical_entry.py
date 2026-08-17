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


def _fake_settlement(captured):
    """A double for `paper_engine.settle_entry` that matches the REAL
    signature and the REAL return shape.

    A1 was hidden for exactly as long as the doubles here invented their own
    contract, so this one takes the authorization positionally and the fill
    as a keyword, precisely as settlement does, and returns
    {"ok": True, "position": {...}} — the only success shape this codebase
    has ever produced. PREPARE is deliberately NOT mocked: sizing is the
    thing under test.
    """
    def fake_settle(auth, *, fill_price, execution_provenance=None,
                    canonical_entry_fee_usd=None):
        captured["fill"] = fill_price
        captured["qty"] = auth.qty
        captured["auth"] = auth
        captured["provenance"] = execution_provenance
        captured["entry_fee"] = canonical_entry_fee_usd
        return {"ok": True, "position": {"id": "pos-test"}}
    return fake_settle


class TheVenueBookIsTheFillAuthorityTests(unittest.TestCase):

    def _open(self, signal=None, bid=99.90, ask=100.10, decision=100.00):
        captured = {}
        with _kraken(bid, ask), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
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


class TheExecutedOrderIsTheAuthorizedOrderTests(unittest.TestCase):
    """A3/A4. The venue used to be handed a NOMINAL one unit purely to
    discover a price, and sizing happened afterwards.

    Two separate defects fell out of that. The order that was simulated was
    not the order that settled — so `spread_cost_usd` and `slippage_usd`
    described one unit of a trade nobody placed, off by the entire position
    size. And risk never priced the entry it was actually exposed at.
    """

    def _open(self, signal=None, bid=99.90, ask=100.10):
        captured = {}
        with _kraken(bid, ask), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(signal or SIGNAL,
                                             decision_price=100.00)
        return res, captured

    def test_the_venue_executes_the_sized_quantity_not_one_unit(self):
        """THE DEFECT. `quantity = signal.get("qty") or 1.0`."""
        res, cap = self._open()
        self.assertTrue(res.get("ok"), res)
        self.assertGreater(res["execution"]["filled_quantity"], 1.0,
                           "the venue was handed a nominal single unit again")
        self.assertAlmostEqual(res["execution"]["filled_quantity"],
                               cap["qty"], places=9)

    def test_a_nominal_qty_on_the_signal_cannot_size_the_order(self):
        """Sizing is risk's job. A `qty` hint on the signal used to go
        straight through to the venue."""
        _, hinted = self._open(dict(SIGNAL, qty=1.0))
        _, plain = self._open()
        self.assertAlmostEqual(hinted["qty"], plain["qty"], places=9)
        self.assertGreater(hinted["qty"], 1.0)

    def test_attribution_describes_the_whole_position_not_one_unit(self):
        """Spread cost scales with size. A one-unit figure on a 300-unit
        position understates the cost of trading by ~300x."""
        res, cap = self._open(bid=99.90, ask=100.10)
        qty = cap["qty"]
        spread_attr = res["execution"]["spread_attribution_usd"]
        # half the 0.20 spread, on every unit
        self.assertAlmostEqual(spread_attr, 0.10 * qty, places=6)
        self.assertGreater(spread_attr, 0.10,
                           "attribution is still priced on a single unit")

    def test_attribution_is_not_a_scaled_up_one_unit_result(self):
        """`ExecutionResult` must describe the true simulated order, so the
        filled quantity it reports has to BE the position's quantity."""
        res, cap = self._open()
        self.assertEqual(res["execution"]["filled_quantity"], cap["qty"])
        self.assertEqual(cap["provenance"]["filled_quantity"], cap["qty"])

    def test_the_settled_size_never_exceeds_what_was_authorized(self):
        res, cap = self._open()
        self.assertLessEqual(cap["qty"],
                             res["execution"]["authorized_quantity"] + 1e-9)

    def test_the_adverse_fill_shrinks_the_position_rather_than_the_stop(self):
        """Crossing the book moves ENTRY against us. The stop price does not
        move, so the distance to it grows, so the same quantity would risk
        more money than was approved. The SIZE gives way.

        Widening the stop instead would keep the quantity and quietly raise
        the loss the account is exposed to — the trade would look identical
        and be bigger.
        """
        res, cap = self._open(bid=99.90, ask=100.10)
        auth = cap["auth"]

        self.assertGreater(cap["fill"], 100.00, "the fill was not adverse")
        # The stop PRICE is exactly what the signal asked for — untouched.
        self.assertEqual(auth.stop, SIGNAL["stop_loss"])
        # The distance to it grew purely because the entry got worse.
        self.assertGreater(auth.stop_distance, 100.00 - SIGNAL["stop_loss"])
        # And the size absorbed it.
        self.assertLess(cap["qty"], res["execution"]["authorized_quantity"])

    def test_the_shrink_keeps_money_at_risk_inside_the_authorization(self):
        """`filled <= authorized` is not the guarantee that matters — dollars
        at the stop are. Quantity alone does not bound loss."""
        res, cap = self._open(bid=99.00, ask=101.00)   # a wide, costly book
        auth = cap["auth"]
        risk_at_stop = auth.qty * auth.stop_distance
        self.assertLessEqual(risk_at_stop, auth.loss_at_stop * (1 + 1e-6))
        self.assertLessEqual(cap["qty"],
                             res["execution"]["authorized_quantity"] + 1e-9)

    def test_a_short_also_executes_its_authorized_size(self):
        short = dict(SIGNAL, paper_direction="Short", stop_loss=105.0,
                     target_price=85.0)
        res, cap = self._open(short)
        self.assertTrue(res.get("ok"), res)
        self.assertGreater(res["execution"]["filled_quantity"], 1.0)
        self.assertLessEqual(cap["fill"], 99.90)


class NoExecutableDataMeansNoPositionTests(unittest.TestCase):
    """A VENUE failure is not a verdict on the thesis."""

    def _attempt(self, quote):
        captured = {}
        with patch("lib.kraken_stream.latest_quote", return_value=quote), \
             patch("lib.kraken_stream.trade_flow", return_value=None), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)
        return res, ("fill" in captured)

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
        captured = {}
        with patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(fut, decision_price=5432.25)
        self.assertNotIn("fill", captured)
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
    """build_provenance is a pure function now; PERSISTENCE is proven against
    a real database in test_canonical_entry_integration.py, because that is
    where the atomicity actually lives."""

    def _doc(self):
        from lib import execution_snapshot as ES
        from lib import virtual_orders as VO
        snap = ES.ExecutionMarketSnapshot(
            venue="kraken", symbol="BTC/USD", product="crypto",
            bid=99.90, ask=100.10, source="kraken_stream.latest_quote",
            venue_event_at="2026-08-17T00:00:00+00:00", age_ms=200.0,
            status=ES.AVAILABLE)
        ready = POL.ExecutionReadiness(True, "kraken", "crypto", snapshot=snap)
        order = VO.VirtualOrder(symbol="BTC/USD", side="long", quantity=10.0,
                                order_type=VO.MARKET)
        execution = VO.execute_market(order, VO.Quote(bid=99.90, ask=100.10))
        return CE.build_provenance(signal=SIGNAL, ready=ready, snap=snap,
                                   execution=execution, decision_price=100.0)

    def test_it_records_the_models_epoch_and_venue(self):
        doc = self._doc()
        self.assertEqual(doc["execution_model"], EXECUTION_MODEL_CANONICAL)
        self.assertEqual(doc["engine_epoch"], CE.CANONICAL_ENGINE_EPOCH)
        self.assertEqual(doc["venue"], "kraken")
        self.assertEqual(doc["bid_at_submit"], 99.90)
        self.assertEqual(doc["ask_at_submit"], 100.10)
        self.assertIsNotNone(doc["actual_entry_fill"])

    def test_it_never_uses_the_ambiguous_legacy_source(self):
        """9,370 historical rows say outcome_source="live" and mean
        forward-observed VIRTUAL. New data must not add to that."""
        doc = self._doc()
        self.assertEqual(doc["source"], "VIRTUAL_CEX_AGENT")
        self.assertNotEqual(doc["source"], "live")

    def test_it_is_json_serialisable(self):
        """Settlement persists it inside the transaction, so a value that
        cannot serialise would roll the whole entry back."""
        json.dumps(self._doc())

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


class ExecutionGoesThroughTheVenueBoundaryTests(unittest.TestCase):
    """A5. `canonical_entry` called `virtual_orders.execute_market()`
    directly, which walked straight past the three gates the boundary exists
    to apply — platform mode, venue capability, and the last risk check
    before submission — and coupled strategy to a fill model rather than to
    a venue. `virtual_orders` lives BELOW the adapter.
    """

    def _calls_in(self, relpath):
        """Every bare function name called in a module, by AST.

        Prose explaining that the boundary is respected must not be able to
        satisfy the test that checks it — that mistake has cost six cycles
        on this codebase already.
        """
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / relpath).read_text(
            encoding="utf-8")
        out = set()
        for c in ast.walk(ast.parse(src)):
            if isinstance(c, ast.Call):
                if isinstance(c.func, ast.Name):
                    out.add(c.func.id)
                elif isinstance(c.func, ast.Attribute):
                    out.add(c.func.attr)
        return out

    def test_canonical_entry_does_not_execute_orders_itself(self):
        calls = self._calls_in("lib/canonical_entry.py")
        self.assertNotIn("execute_market", calls,
                         "entry reached past the venue boundary again")
        self.assertNotIn("execute_limit", calls)
        self.assertIn("submit", calls, "entry no longer routes through a venue")

    def test_the_autonomous_job_does_not_execute_orders_itself(self):
        calls = self._calls_in("jobs/paper_trading.py")
        self.assertNotIn("execute_market", calls)
        self.assertNotIn("execute_limit", calls)

    def test_the_fill_arrives_as_a_typed_venue_submission(self):
        """A6 made `execution` a real dataclass field so the adapter's
        contract could be asserted rather than assumed."""
        import dataclasses

        from lib import execution_venue as EV
        from lib.execution_venue import VenueSubmission
        seen, captured = {}, {}
        original = EV.submit

        def spy(plan, **kw):
            sub = original(plan, **kw)
            seen["submission"] = sub
            return sub

        with _kraken(99.90, 100.10), \
             patch("lib.execution_venue.submit", spy), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)

        self.assertTrue(res.get("ok"), res)
        sub = seen["submission"]
        self.assertIsInstance(sub, VenueSubmission)
        self.assertTrue(sub.accepted)
        self.assertIn("execution", [f.name for f in dataclasses.fields(sub)])
        self.assertIsNotNone(sub.execution)

    def test_the_plan_that_crosses_the_boundary_carries_the_real_product(self):
        """Gate 2 asks the adapter whether it can execute this PRODUCT. Handed
        an asset class it would refuse — or worse, be given a permissive
        default."""
        from lib import product_router as PR
        seen, captured = {}, {}
        from lib import execution_venue as EV
        original = EV.submit

        def spy(plan, **kw):
            seen["plan"] = plan
            seen["kw"] = kw
            return original(plan, **kw)

        with _kraken(99.90, 100.10), \
             patch("lib.execution_venue.submit", spy), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            CE.open_canonical_position(SIGNAL, decision_price=100.0)

        self.assertIn(seen["plan"].product, PR.ALL_PRODUCTS)
        self.assertEqual(seen["kw"]["product"], seen["plan"].product)

    def test_the_adapter_can_actually_execute_that_product(self):
        from lib.execution_venue import VirtualCexAdapter
        from lib import product_router as PR
        adapter = VirtualCexAdapter()
        for product in (PR.CRYPTO_SPOT, PR.CRYPTO_PERP, PR.EQUITY_SPOT):
            with self.subTest(product=product):
                self.assertTrue(adapter.supports(product))
        self.assertFalse(adapter.supports("crypto"),
                         "an asset class must not pass as a product")

    def test_the_risk_gate_is_offered_the_decision_not_bypassed(self):
        """Gate 3 runs immediately before submission. Passing risk=None
        would make it decorative."""
        seen, captured = {}, {}
        from lib import execution_venue as EV
        original = EV.submit

        def spy(plan, **kw):
            seen["risk"] = kw.get("risk")
            return original(plan, **kw)

        with _kraken(99.90, 100.10), \
             patch("lib.execution_venue.submit", spy), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            CE.open_canonical_position(SIGNAL, decision_price=100.0)

        from lib.decision_types import RiskDecision
        self.assertIsInstance(seen["risk"], RiskDecision)
        self.assertFalse(seen["risk"].rejected)
        self.assertGreater(seen["risk"].qty, 0)

    def test_a_venue_refusal_opens_nothing_and_is_not_a_venue_outage(self):
        """A refusal is a RESULT. It is about the order, so it must not be
        laundered into `venue_failure` and excused from the record."""
        from lib.execution_venue import REFUSED_RISK, VenueSubmission
        captured = {}

        def refuse(plan, **kw):
            return VenueSubmission(False, "VIRTUAL_CEX", REFUSED_RISK,
                                   "quantity exceeds approved")

        with _kraken(99.90, 100.10), \
             patch("lib.execution_venue.submit", refuse), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)

        self.assertNotIn("fill", captured, "a refused order still settled")
        self.assertEqual(res["error"], REFUSED_RISK)
        self.assertFalse(res["venue_failure"])

    def test_an_accepted_submission_with_no_execution_settles_nothing(self):
        """A broken adapter contract is not a fill."""
        from lib.execution_venue import VenueSubmission
        captured = {}

        def hollow(plan, **kw):
            return VenueSubmission(True, "VIRTUAL_CEX")   # execution stays None

        with _kraken(99.90, 100.10), \
             patch("lib.execution_venue.submit", hollow), \
             patch("lib.paper_engine.settle_entry", _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)

        self.assertNotIn("fill", captured)
        self.assertFalse(res.get("ok"))


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
