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


# THE SIGNAL DECLARES ITS EXPRESSION. Product comes from the chosen way of
# holding the thesis (A9), and CRYPTO_SPOT is the one this desk has a wired
# executable feed for — `kraken` here is the SPOT WebSocket. A perpetual has
# no quote source, and that refusal is asserted explicitly in
# PerpFillsNeedPerpQuotesTests rather than left to an environment default.
SIGNAL = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
          "paper_direction": "Long", "entry_price": 100.0,
          "stop_loss": 95.0, "target_price": 115.0, "timeframe": "4H",
          "id": "sig-1", "product": "CRYPTO_SPOT"}


def _fake_settlement(captured):
    """A double for `paper_engine.settle_position_entry` that matches the REAL
    signature and the REAL return shape.

    A1 was hidden for exactly as long as the doubles here invented their own
    contract, so this one takes the authorization positionally and the fill
    as a keyword, precisely as settlement does, and returns
    {"ok": True, "position": {...}} — the only success shape this codebase
    has ever produced. PREPARE is deliberately NOT mocked: sizing is the
    thing under test.
    """
    def fake_settle(auth, *, fill_price, execution_provenance=None,
                    canonical_entry_fee_usd=None, observation_id=None,
                    execution_id=None):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
        with patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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

    def _doc(self, fee_quote=None):
        from lib import execution_snapshot as ES
        from lib import fee_authority as FA
        from lib import product_router as PR
        from lib import virtual_orders as VO
        snap = ES.ExecutionMarketSnapshot(
            venue="kraken", symbol="BTC/USD", product=PR.CRYPTO_PERP,
            bid=99.90, ask=100.10, source="kraken_stream.latest_quote",
            venue_event_at="2026-08-17T00:00:00+00:00", age_ms=200.0,
            status=ES.AVAILABLE)
        ready = POL.ExecutionReadiness(True, "kraken", PR.CRYPTO_PERP,
                                       asset_class="crypto", snapshot=snap)
        order = VO.VirtualOrder(symbol="BTC/USD", side="long", quantity=10.0,
                                order_type=VO.MARKET)
        execution = VO.execute_market(order, VO.Quote(bid=99.90, ask=100.10))
        fee = fee_quote or FA.leg_fee(
            "BTC/USD", notional=1_000.0, price=100.0,
            product=PR.CRYPTO_PERP, venue="kraken")
        return CE.build_provenance(signal=SIGNAL, ready=ready, snap=snap,
                                   execution=execution, decision_price=100.0,
                                   authorized_qty=10.0, fee_quote=fee)

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

    def test_the_cost_model_is_per_leg_and_the_fee_is_recorded(self):
        """A2. This said legacy_round_trip_v1 until settlement genuinely
        changed, because false provenance is worse than none."""
        doc = self._doc()
        self.assertEqual(doc["cost_model"], COST_MODEL_CANONICAL)
        self.assertGreater(doc["entry_fee_usd"], 0.0)
        self.assertIsNotNone(doc["entry_fee_basis"])
        self.assertIsNotNone(doc["entry_fee_source"])

    def test_the_fee_quality_travels_with_the_number(self):
        """A labelled estimate must never be pooled as a measurement."""
        doc = self._doc()
        self.assertIn("cost_model_fee_quality", doc)
        self.assertIn("cost_model_fee_is_measured", doc)

    def test_provenance_cannot_be_built_without_a_priced_entry_leg(self):
        """Stamping per_leg_v2 while charging nothing would be a free trade
        wearing the label of an accurately-costed one. The fee is checked
        before anything else is read, so this refuses outright."""
        from lib import fee_authority as FA
        for bad in (None, FA.FeeQuote(ok=False, reason="nope")):
            with self.subTest(fee_quote=bad):
                with self.assertRaises(ValueError):
                    CE.build_provenance(signal=SIGNAL, ready=None, snap=None,
                                        execution=None, decision_price=100.0,
                                        fee_quote=bad)


class TheEntryLegIsPricedBeforeItSettlesTests(unittest.TestCase):
    """A2. Settlement wrote sizing["round_trip_fees"] — a DEFERRED round-trip
    estimate computed before the order existed and charged at close."""

    def _open(self, **env):
        import os
        captured = {}
        with patch.dict(os.environ, env), _kraken(99.90, 100.10), \
             patch("lib.paper_engine.settle_position_entry",
                   _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)
        return res, captured

    def test_the_entry_fee_is_computed_and_handed_to_settlement(self):
        res, cap = self._open()
        self.assertTrue(res.get("ok"), res)
        self.assertIsNotNone(cap["entry_fee"])
        self.assertGreater(cap["entry_fee"], 0.0)

    def test_the_fee_is_priced_on_the_executed_size_not_the_authorized_one(self):
        """The order shrank after the fill; the fee must follow the position
        that actually exists."""
        res, cap = self._open()
        auth = cap["auth"]
        self.assertLess(auth.qty, res["execution"]["authorized_quantity"])
        expected = cap["provenance"]["entry_fee_usd"]
        self.assertAlmostEqual(cap["entry_fee"], expected, places=9)

    def test_a_taker_rate_is_used_because_a_market_order_crosses(self):
        res, cap = self._open()
        self.assertFalse(cap["provenance"].get("maker", False))
        self.assertEqual(cap["provenance"]["cost_model"], COST_MODEL_CANONICAL)

    def test_no_fee_authority_means_no_position(self):
        """Settling anyway would debit nothing while stamping the position
        per_leg_v2 — a free trade wearing an accurate label."""
        from lib import fee_authority as FA
        captured = {}
        unavailable = FA.FeeQuote(ok=False, reason=FA.FEE_AUTHORITY_UNAVAILABLE,
                                  detail="no schedule")
        with _kraken(99.90, 100.10), \
             patch("lib.fee_authority.leg_fee", return_value=unavailable), \
             patch("lib.paper_engine.settle_position_entry",
                   _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)
        self.assertNotIn("fill", captured, "an unpriced entry still settled")
        self.assertEqual(res["error"], FA.FEE_AUTHORITY_UNAVAILABLE)
        self.assertFalse(res["venue_failure"])

    def test_a_catastrophic_product_is_refused_before_it_opens(self):
        """A per-contract cost does not dilute with size, so an instrument
        whose round trip eats an unacceptable share of its own notional can
        never pay for itself — SHIB's US contract is $0.45 and costs $0.30 to
        trade, 67%, at any size.

        The fee is injected rather than reached through a live schedule: the
        products that can currently produce a catastrophic figure are the US
        per-contract perpetuals, and those now refuse earlier still, at the
        quote authority (A10). This asserts the backstop itself.
        """
        from lib import fee_authority as FA
        captured = {}
        ruinous = FA.FeeQuote(ok=True, fee_usd=5_000.0,
                              fee_basis=FA.PER_CONTRACT, contract_count=1.0,
                              quality=FA.EXCHANGE_SCHEDULE, source="test")
        with _kraken(99.90, 100.10), \
             patch("lib.fee_authority.leg_fee", return_value=ruinous), \
             patch("lib.paper_engine.settle_position_entry",
                   _fake_settlement(captured)):
            res = CE.open_canonical_position(SIGNAL, decision_price=100.0)

        self.assertNotIn("fill", captured, "a catastrophic product still opened")
        self.assertEqual(res["error"], CE.FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL)
        self.assertFalse(res["venue_failure"],
                         "an uneconomic instrument is not a venue outage")

    def test_an_ordinary_fee_passes_the_catastrophic_gate(self):
        """The gate must catch only the instrument that could never pay for
        itself. Rejecting a profitable trade is simulator error too."""
        res, cap = self._open()
        self.assertTrue(res.get("ok"), res)
        self.assertIn("fill", cap)

    def test_the_gate_uses_notional_on_both_sides_of_the_comparison(self):
        """It must not compare a fee against MARGIN, or against an
        unleveraged move in the underlying — mixing denominators kills
        trades that are comfortably viable, and rejecting a profitable trade
        is simulator error too."""
        import inspect
        src = inspect.getsource(CE.open_canonical_position)
        gate = src[src.index("CATASTROPHIC-PRODUCT GATE"):
                   src.index("FEE_EXCEEDS_VIABLE_SHARE_OF_NOTIONAL,")]
        # B0 moved the denominator from the authorization's reference-priced
        # notional to the EXECUTED notional — still a notional, same
        # ExecutionResult as the fee's numerator. The invariant this test
        # exists for is unchanged: never margin, never loss-at-stop.
        self.assertIn("executed_notional", gate)
        self.assertNotIn("final.margin", gate)
        self.assertNotIn("loss_at_stop", gate)


class PerpFillsNeedPerpQuotesTests(unittest.TestCase):
    """A10. The venue name alone used to authorise the fill.

    `kraken` in the reader registry means `wss://ws.kraken.com/v2` — the SPOT
    WebSocket. Its ticker channel carries the spot book and nothing else, yet
    a CRYPTO_PERP order was priced against it. Spot and perp diverge by basis
    and funding, have separate liquidity, and it is the PERP whose price
    determines the P&L. Labelling a spot quote as perpetual execution truth
    is the same class of error as labelling a mark as a fill.
    """

    PERP = dict(SIGNAL, product="CRYPTO_PERP")

    def setUp(self):
        """The perpetual book registry is PROCESS-GLOBAL, so a test file that
        seeds one leaks into any test that expects none. These cases are
        about what happens with NO perpetual book available, and that has to
        be true regardless of what ran first."""
        from lib import bitnomial_market_data as MD
        MD.reset_books()
        self.addCleanup(MD.reset_books)

    def _attempt(self, signal):
        captured = {}
        with _kraken(99.90, 100.10), \
             patch("lib.paper_engine.settle_position_entry",
                   _fake_settlement(captured)):
            res = CE.open_canonical_position(signal, decision_price=100.0)
        return res, captured

    def test_the_kraken_feed_is_spot_and_says_so(self):
        from lib import execution_snapshot as ES
        self.assertEqual(ES.products_for("kraken"), frozenset({"CRYPTO_SPOT"}))
        self.assertTrue(ES.prices_product("kraken", "CRYPTO_SPOT"))
        self.assertFalse(ES.prices_product("kraken", "CRYPTO_PERP"))

    def test_a_perp_opens_nothing_against_a_spot_book(self):
        """A10.1 gave perpetuals a real book (Bitnomial), so the refusal is
        no longer unconditional — but the property this pins never changes:
        with NO perpetual book available, a perp opens NOTHING rather than
        quietly falling back to the spot quote sitting right there.
        """
        from lib import bitnomial_market_data as MD
        MD.reset_books()
        res, cap = self._attempt(self.PERP)
        self.assertNotIn("fill", cap, "a perp was filled off the spot book")
        self.assertTrue(POL.is_venue_data_failure(res["error"]), res)
        self.assertNotEqual(res["error"], "OK")

    def test_the_refusal_is_a_venue_gap_not_a_losing_thesis(self):
        """Recording it against the strategy would teach the learner that
        perpetual theses lose, when what is missing is a data feed."""
        res, _ = self._attempt(self.PERP)
        self.assertTrue(res["venue_failure"])
        self.assertTrue(POL.is_venue_data_failure(res["error"]))

    def test_spot_still_fills_because_the_spot_feed_speaks_for_it(self):
        """The refusal must be about the PRODUCT, not a blanket outage."""
        res, cap = self._attempt(SIGNAL)
        self.assertTrue(res.get("ok"), res)
        self.assertIn("fill", cap)

    def test_the_snapshot_itself_refuses_rather_than_relying_on_the_caller(self):
        """Defence in depth: a caller that forgets to check must still not
        receive a spot quote labelled as a perpetual's."""
        from lib import execution_snapshot as ES
        with _kraken(99.90, 100.10):
            snap = ES.execution_market_snapshot("BTC/USD", "kraken",
                                                product="CRYPTO_PERP")
        self.assertEqual(snap.status, ES.UNAVAILABLE)
        self.assertNotEqual(snap.status, ES.AVAILABLE)
        self.assertIsNone(snap.bid)
        self.assertIsNone(snap.ask)

    def test_no_spot_reader_claims_a_perp_it_cannot_price(self):
        """Adding a product to _READER_PRODUCTS is a claim that the feed
        behind it actually carries that book.

        Exactly ONE venue may claim CRYPTO_PERP — the US derivatives venue,
        whose feed is the Bitnomial book. Every SPOT reader must still
        refuse it: that a perpetual book now exists somewhere is not licence
        for `wss://ws.kraken.com/v2` to price one.
        """
        from lib import bitnomial_products as BP
        from lib import execution_snapshot as ES
        claimants = {v for v, products in ES._READER_PRODUCTS.items()
                     if "CRYPTO_PERP" in products}
        self.assertEqual(claimants, {BP.KRAKEN_US_VENUE})
        for venue in ("kraken", "alpaca", "binance", "binanceus", "coinbase"):
            with self.subTest(venue=venue):
                self.assertNotIn("CRYPTO_PERP", ES.products_for(venue),
                                 f"the {venue} SPOT feed claims to price "
                                 f"perpetuals")


class IsCanonicalRequiresAllFourClaimsTests(unittest.TestCase):
    """A hybrid — venue-book fill, legacy costs — is a real state this
    codebase produced between A5 and A2. Testing only `execution_model`
    accepted it as canonical."""

    def _pos(self, **overrides):
        doc = {"execution_model": EXECUTION_MODEL_CANONICAL,
               "cost_model": COST_MODEL_CANONICAL,
               "engine_epoch": CE.CANONICAL_ENGINE_EPOCH,
               "entry_execution_id": "exec-1"}
        doc.update(overrides)
        return type("P", (), {"execution_provenance": json.dumps(doc)})()

    def test_all_four_present_is_canonical(self):
        self.assertTrue(CE.is_canonical(self._pos()))

    def test_a_legacy_cost_model_is_not_canonical(self):
        self.assertFalse(CE.is_canonical(self._pos(cost_model=COST_MODEL_LEGACY)))

    def test_a_foreign_epoch_is_not_canonical(self):
        self.assertFalse(CE.is_canonical(self._pos(engine_epoch="2020-01-01-old")))

    def test_a_missing_execution_id_is_not_canonical(self):
        self.assertFalse(CE.is_canonical(self._pos(entry_execution_id="")))
        self.assertFalse(CE.is_canonical(self._pos(entry_execution_id=None)))

    def test_the_hybrid_is_still_refused_by_the_fail_closed_exit_guard(self):
        """THE POINT. Tightening the classifier must not narrow the guard —
        a guard fails closed by refusing MORE, never less."""
        from lib.paper_engine import _refuse_legacy_close
        hybrid = self._pos(cost_model=COST_MODEL_LEGACY)
        self.assertFalse(CE.is_canonical(hybrid))
        self.assertTrue(CE.has_canonical_fill(hybrid))
        refusal = _refuse_legacy_close(hybrid)
        self.assertIsNotNone(refusal, "the legacy close path accepted a "
                                      "venue-book fill again")
        self.assertEqual(refusal["error"],
                         "CANONICAL_POSITION_REQUIRES_EXECUTION_SETTLEMENT")

    def test_a_genuinely_legacy_position_still_passes_the_guard(self):
        from lib.paper_engine import _refuse_legacy_close
        legacy = type("P", (), {"execution_provenance": None})()
        self.assertFalse(CE.has_canonical_fill(legacy))
        self.assertIsNone(_refuse_legacy_close(legacy))


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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
             patch("lib.paper_engine.settle_position_entry", _fake_settlement(captured)):
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
