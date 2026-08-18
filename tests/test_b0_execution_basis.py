"""B0 finalization — one economic quantity, stated once, agreed everywhere.

The sizing core (B0.2) made risk speak PBTCUCZ50 contracts. This file guards
the rest of the chain: the ORDER objects. Its four claims, in the order the
chain makes them:

    the OrderPlan carries the exact basis it was built from
    the VirtualOrder receives that basis rather than re-deriving one
    a stated basis that disagrees anywhere REFUSES before the fill model
    an ExecutionResult that contradicts its plan REFUSES before settlement

Everything here drives the REAL canonical chain against a REAL perpetual
book (the same harness as test_perp_execution_routing); recorders call
through rather than canning results, so the production arithmetic is what
gets asserted. The one deliberate exception is the mismatch-injection tests,
which corrupt exactly one fact to prove the refusal fires — and every
refusal is proven by the absence of a settlement call, not by a log line.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_products as BP
from lib import instruments as INST
from lib import product_router as PR
from lib import bitnomial_market_data as MD

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
PERP_BID_USD, PERP_ASK_USD = 64_500.0, 64_600.0

PERP_SIGNAL = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
               "paper_direction": "Long", "entry_price": 64_400.0,
               "stop_loss": 61_000.0, "target_price": 70_000.0,
               "timeframe": "4H", "id": "sig-b0-final",
               "product": PR.CRYPTO_PERP}


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _spot_feed():
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": SPOT_BID, "ask": SPOT_ASK,
                                     "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


def _seed_perp_book():
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(PERP_BID_USD / TICK), 50]],
                "asks": [[int(PERP_ASK_USD / TICK), 50]]})
    return book


def _pbtc():
    return INST.resolve_for_execution(
        "BTC/USD", product=PR.CRYPTO_PERP, venue=BP.KRAKEN_US_VENUE,
        instrument_id=PERP_SYM)


class _CallThrough:
    """Record every call and CALL THROUGH — a double that cans its answer
    tests the double."""

    def __init__(self, real):
        self.real, self.calls, self.results = real, [], []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        out = self.real(*a, **kw)
        self.results.append(out)
        return out


class _ChainHarness(unittest.TestCase):
    """The real canonical PBTC chain, with only the settlement leaf and the
    market feeds replaced."""

    def setUp(self):
        _seed_perp_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    def _run(self, mutate_execution=None, signal=None):
        """Open canonically; return (result, captures).

        `mutate_execution(res)` — when given — corrupts the ExecutionResult
        the venue hands back, AFTER the fill model built it honestly. That is
        the injection point for disagreement tests: the corruption happens
        downstream of everything that could legitimately refuse it earlier.
        """
        from lib import canonical_entry as CE
        from lib import execution_venue as EV
        from lib import virtual_orders as VO

        captures = {"settled": []}

        def fake_settle(auth, *, fill_price, execution_provenance=None,
                        canonical_entry_fee_usd=None, observation_id=None,
                        execution_id=None):
            captures["settled"].append(auth)
            captures["fill"] = fill_price
            captures["provenance"] = execution_provenance
            captures["fee"] = canonical_entry_fee_usd
            return {"ok": True, "position": {"id": "pos-b0"}}

        submit_rec = _CallThrough(EV.submit)
        exec_rec = _CallThrough(VO.execute_market)

        real_adapter_market = exec_rec
        if mutate_execution is not None:
            def corrupted(*a, **kw):
                res = exec_rec(*a, **kw)
                mutate_execution(res)
                return res
            real_adapter_market = corrupted

        with _spot_feed(), \
             patch("lib.execution_venue.submit", submit_rec), \
             patch("lib.virtual_orders.execute_market", real_adapter_market), \
             patch("lib.paper_engine.settle_position_entry", fake_settle):
            res = CE.open_canonical_position(signal or PERP_SIGNAL,
                                             decision_price=64_400.0)

        captures["submits"] = submit_rec
        captures["fills"] = exec_rec
        return res, captures


class TheOrderPlanCarriesTheExactBasisTests(_ChainHarness):

    def _final_plan(self, cap):
        # The plan of the LAST submission — the one whose execution settles.
        (plan,), kw = cap["submits"].calls[-1]
        return plan, kw

    def test_the_plan_names_the_contract(self):
        res, cap = self._run()
        self.assertTrue(res.get("ok"), res)
        plan, _ = self._final_plan(cap)
        self.assertEqual(plan.instrument_id, PERP_SYM)

    def test_the_plan_counts_contracts(self):
        _, cap = self._run()
        plan, _ = self._final_plan(cap)
        self.assertEqual(plan.quantity_unit, "CONTRACTS")
        self.assertEqual(plan.qty, float(int(plan.qty)),
                         f"a plan for part of a contract: {plan.qty}")

    def test_the_plan_multiplier_is_the_contract_size(self):
        _, cap = self._run()
        plan, _ = self._final_plan(cap)
        self.assertAlmostEqual(plan.multiplier, 0.01)

    def test_the_plan_basis_is_the_resolved_identity_not_a_re_resolution(self):
        """The instrument handed to the venue and the plan's own basis must
        be the same object's facts."""
        _, cap = self._run()
        plan, kw = self._final_plan(cap)
        inst = kw["instrument"]
        self.assertEqual(plan.instrument_id, inst.instrument_id)
        self.assertEqual(plan.quantity_unit, inst.quantity_unit)
        self.assertAlmostEqual(plan.multiplier, float(inst.multiplier))


class TheVirtualOrderReceivesTheBasisTests(_ChainHarness):

    def test_the_order_carries_contracts(self):
        _, cap = self._run()
        (order, _quote), _kw = cap["fills"].calls[-1]
        self.assertEqual(order.quantity_unit, "CONTRACTS")

    def test_a_plan_instrument_unit_disagreement_refuses_before_the_fill(self):
        """E — virtual_orders must never pick which stated basis wins, so
        the adapter refuses before execute_market runs at all."""
        from lib import execution_venue as EV
        from lib.decision_types import OrderPlan

        plan = OrderPlan(symbol="BTC/USD", venue=BP.KRAKEN_US_VENUE,
                         side="long", order_type="market", qty=2.0,
                         entry=64_600.0, initial_stop=61_000.0,
                         notional=1292.0, product=PR.CRYPTO_PERP,
                         instrument_id=PERP_SYM,
                         quantity_unit="CONTRACTS", multiplier=0.01)
        coins = INST.resolve_for_execution("BTC/USD", product="CRYPTO_SPOT")

        def explode(*a, **k):
            raise AssertionError("execute_market ran on a plan whose unit "
                                 "basis disagrees with the instrument's")
        from lib.virtual_orders import Quote
        quote = Quote(bid=PERP_BID_USD, ask=PERP_ASK_USD, as_of=_at(0.1),
                      source="test")
        with patch("lib.virtual_orders.execute_market", explode):
            sub = EV.submit(plan, venue_family=EV.VIRTUAL_CEX,
                            product=PR.CRYPTO_PERP,
                            venue=BP.KRAKEN_US_VENUE, quote=quote,
                            instrument=coins)
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, EV.REFUSED_UNIT_BASIS)

    def test_a_plan_for_a_different_contract_refuses_too(self):
        from lib import execution_venue as EV
        from lib.decision_types import OrderPlan
        from lib.virtual_orders import Quote

        plan = OrderPlan(symbol="BTC/USD", venue=BP.KRAKEN_US_VENUE,
                         side="long", order_type="market", qty=2.0,
                         entry=64_600.0, initial_stop=61_000.0,
                         notional=1292.0, product=PR.CRYPTO_PERP,
                         instrument_id="PETHUCZ50",
                         quantity_unit="CONTRACTS", multiplier=0.01)
        quote = Quote(bid=PERP_BID_USD, ask=PERP_ASK_USD, as_of=_at(0.1),
                      source="test")
        sub = EV.submit(plan, venue_family=EV.VIRTUAL_CEX,
                        product=PR.CRYPTO_PERP, venue=BP.KRAKEN_US_VENUE,
                        quote=quote, instrument=_pbtc())
        self.assertFalse(sub.accepted)
        self.assertEqual(sub.reason, EV.REFUSED_UNIT_BASIS)


class AnExecutionThatContradictsItsPlanNeverSettlesTests(_ChainHarness):
    """F — the ExecutionResult is never patched to fit; the entry refuses,
    and 'refuses' means NO settlement call, not an error string beside one."""

    def test_a_result_in_coins_does_not_settle(self):
        def corrupt(res):
            res.quantity_unit = "COINS"
        res, cap = self._run(mutate_execution=corrupt)
        self.assertFalse(res.get("ok"))
        self.assertEqual(cap["settled"], [],
                         "a COINS result against a CONTRACTS plan settled")
        self.assertIn("COINS", res.get("detail", ""))

    def test_a_result_at_the_generic_multiplier_does_not_settle(self):
        def corrupt(res):
            res.multiplier = 1.0
        res, cap = self._run(mutate_execution=corrupt)
        self.assertFalse(res.get("ok"))
        self.assertEqual(cap["settled"], [])

    def test_a_fill_larger_than_its_own_request_does_not_settle(self):
        def corrupt(res):
            res.filled_quantity = res.requested_quantity + 1.0
        res, cap = self._run(mutate_execution=corrupt)
        self.assertFalse(res.get("ok"))
        self.assertEqual(cap["settled"], [])

    def test_an_honest_execution_still_settles(self):
        """The control for this class: with nothing corrupted the same
        chain settles — so the refusals above are the checks firing, not
        the harness failing."""
        res, cap = self._run()
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(cap["settled"]), 1)


class FourStagesOneQuantityTests(_ChainHarness):
    """G — RiskDecision, EntryAuthorization, OrderPlan, VirtualOrder and
    ExecutionResult, captured from ONE real run, all describe the same
    economic quantity in the same units. Not four fixtures built to match —
    the actual objects the chain produced."""

    def test_the_five_objects_agree(self):
        res, cap = self._run()
        self.assertTrue(res.get("ok"), res)

        (plan,), submit_kw = cap["submits"].calls[-1]
        risk = submit_kw["risk"]                      # RiskDecision
        (order, _), _ = cap["fills"].calls[-1]        # VirtualOrder
        execution = cap["fills"].results[-1]          # ExecutionResult
        auth = cap["settled"][0]                      # EntryAuthorization

        # One unit basis.
        self.assertEqual(risk.quantity_unit, "CONTRACTS")
        self.assertEqual(plan.quantity_unit, "CONTRACTS")
        self.assertEqual(order.quantity_unit, "CONTRACTS")
        self.assertEqual(execution.quantity_unit, "CONTRACTS")
        self.assertEqual(auth.sizing.get("quantity_unit"), "CONTRACTS")

        # One multiplier.
        for got in (risk.multiplier, plan.multiplier, execution.multiplier,
                    auth.sizing.get("multiplier")):
            self.assertAlmostEqual(got, 0.01)

        # One contract identity, where the object carries identity.
        self.assertEqual(plan.instrument_id, PERP_SYM)
        inst = submit_kw["instrument"]
        self.assertEqual(inst.instrument_id, PERP_SYM)

        # One quantity, and it is whole contracts.
        qty = float(auth.qty)
        self.assertEqual(qty, float(int(qty)))
        self.assertGreater(qty, 0)
        self.assertEqual(float(risk.qty), qty)
        self.assertEqual(float(plan.qty), qty)
        self.assertEqual(float(order.quantity), qty)
        self.assertEqual(float(execution.requested_quantity), qty)
        self.assertEqual(float(execution.filled_quantity), qty)


class TheGenericPathIsNeverConsultedTests(_ChainHarness):
    """H — the poison, plus the control that makes its silence evidence."""

    def test_poisoned_generic_lookups_do_not_move_the_canonical_answer(self):
        # Distinct signal ids: the decision-observation lifecycle rightly
        # refuses two different executions claiming one decision identity.
        clean, cap_clean = self._run(
            signal=dict(PERP_SIGNAL, id="sig-b0-poison-clean"))
        self.assertTrue(clean.get("ok"), clean)
        clean_auth = cap_clean["settled"][0]

        real_spec = INST.get_spec

        def poisoned_spec(symbol):
            spec = real_spec(symbol)
            return type(spec)(**{**spec.__dict__, "multiplier": 999.0}) \
                if hasattr(spec, "__dict__") else spec

        with patch.object(INST, "get_spec", poisoned_spec):
            dirty, cap_dirty = self._run(
                signal=dict(PERP_SIGNAL, id="sig-b0-poison-dirty"))
        self.assertTrue(dirty.get("ok"), dirty)
        dirty_auth = cap_dirty["settled"][0]

        self.assertEqual(dirty_auth.qty, clean_auth.qty)
        self.assertAlmostEqual(dirty_auth.loss_at_stop,
                               clean_auth.loss_at_stop, places=6)
        self.assertAlmostEqual(cap_dirty["fee"], cap_clean["fee"], places=9)

    def test_the_control_the_poison_bites_a_legacy_resolution(self):
        """Without this the silence above is vacuous. The legacy fill path
        resolves the bare symbol; poison THAT resolution and the legacy
        order must fail — proving the poison is live in exactly the code
        the canonical path is claimed to avoid."""
        from lib import virtual_orders as VO

        def explode(*a, **k):
            raise AssertionError("poison reached")
        order = VO.VirtualOrder(symbol="BTC/USD", side="long", quantity=3.0,
                                order_type="market")
        quote = VO.Quote(bid=64_000.0, ask=64_010.0, as_of=_at(0.1),
                         source="test")
        with patch.object(INST, "resolve", explode):
            with self.assertRaises(AssertionError):
                VO.execute_market(order, quote, instrument=None)


if __name__ == "__main__":
    unittest.main()


class TheFeeCountsWhatActuallyFilledTests(unittest.TestCase):
    """I — planning forecasts round UP; executed history counts EXACTLY.

    Two different questions wearing one schedule. A pre-trade estimate that
    understates a cost approves trades the fees will kill, so planning
    ceils. An executed fee that rounds ANYTHING is charging for a trade
    that did not happen — 2 filled contracts cost 2 x per-side, whatever
    notional arithmetic would reconstruct.
    """

    FILL = 64_600.0
    UNIT = FILL * 0.01                     # $646 of notional per contract

    def _exact(self, count=2.0, notional=None, **kw):
        from lib import fee_authority as FA
        base = dict(notional=(count * self.UNIT if notional is None
                              else notional),
                    price=self.FILL, product=PR.CRYPTO_PERP,
                    venue=BP.KRAKEN_US_VENUE, maker=False,
                    exact_contract_count=count,
                    execution_instrument=_pbtc(),
                    actual_fill_price=self.FILL)
        base.update(kw)
        return FA.leg_fee("BTC/USD", **base)

    def test_planning_still_rounds_up(self):
        """26 — the conservative forecast is untouched: 2.5 contracts of
        notional plans as 3."""
        from lib import fee_authority as FA
        q = FA.leg_fee("BTC/USD", notional=2.5 * self.UNIT, price=self.FILL,
                       product=PR.CRYPTO_PERP, venue=BP.KRAKEN_US_VENUE,
                       maker=False)
        self.assertTrue(q.ok, q.detail)
        self.assertEqual(q.contract_count, 3.0)
        self.assertEqual(q.contract_count_basis, FA.PLANNING_ROUND_UP)
        self.assertAlmostEqual(q.fee_usd, 3 * 0.15)

    def test_an_executed_fee_uses_the_exact_count(self):
        """27 — 2 filled contracts pay for exactly 2."""
        from lib import fee_authority as FA
        q = self._exact(count=2.0)
        self.assertTrue(q.ok, q.detail)
        self.assertEqual(q.contract_count, 2.0)
        self.assertEqual(q.contract_count_basis, FA.EXECUTED_EXACT)
        self.assertAlmostEqual(q.fee_usd, 2 * 0.15)
        self.assertEqual(q.fee_basis, FA.PER_CONTRACT)
        self.assertEqual(q.quality, FA.EXCHANGE_SCHEDULE,
                         "the schedule stays authoritative either way")

    def test_a_contradictory_executed_notional_refuses(self):
        """29 — the count, price and multiplier must multiply back to the
        notional, or no fee exists to quote."""
        from lib import fee_authority as FA
        q = self._exact(count=2.0, notional=2.0 * self.FILL)  # coins math
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, FA.EXECUTION_UNIT_MISMATCH)

    def test_a_fractional_executed_count_refuses(self):
        from lib import fee_authority as FA
        q = self._exact(count=2.73, notional=2.73 * self.UNIT)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, FA.EXECUTION_UNIT_MISMATCH)


class TheFeeFollowsTheSurvivingExecutionTests(_ChainHarness):
    """I4 — when the post-fill repricing shrinks the order and a smaller one
    is resubmitted, the fee describes the resubmission, not the first fill
    and not the plan."""

    def test_a_resubmitted_entry_pays_fees_on_the_final_count(self):
        """Budget $107 at the mid's $35.50/contract affords 3 contracts;
        at the fill's $36.00 it affords 2 — so the 3-lot reprices down and
        resubmits as 2. Everything downstream must say 2."""
        from lib import fee_authority as FA
        from lib import paper_engine as PE
        from app.database import PaperPortfolio, PaperPosition, get_db

        with get_db() as db:
            db.query(PaperPosition).delete()
            pf = db.query(PaperPortfolio).first()
            pf.cash = 100_000.0
            db.commit()

        with patch.object(PE, "TRADE_MARGIN_PCT", 0.107):
            res, cap = self._run(
                signal=dict(PERP_SIGNAL, id="sig-b0-resubmit"))

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(cap["fills"].calls), 2,
                         "the boundary was chosen to force one resubmission")
        first = cap["fills"].results[0]
        final = cap["fills"].results[-1]
        self.assertEqual(float(first.filled_quantity), 3.0)
        self.assertEqual(float(final.filled_quantity), 2.0)

        # The fee is the FINAL execution's: 2 contracts, counted exactly.
        doc = cap["provenance"]
        self.assertEqual(doc["entry_fee_contract_count"], 2.0)
        self.assertEqual(doc["entry_fee_contract_count_basis"],
                         FA.EXECUTED_EXACT)
        self.assertAlmostEqual(cap["fee"], 2 * 0.15)
        # And the persisted execution facts describe the same trade.
        self.assertEqual(doc["filled_quantity"], 2.0)
        self.assertAlmostEqual(doc["executed_notional_usd"],
                               2.0 * final.fill_price * 0.01, places=6)
        self.assertEqual(cap["settled"][0].qty, 2.0)


class ProvenanceSpeaksOneBasisTests(_ChainHarness):
    """J — no persisted document may pair PBTCUCZ50 with COINS, a 1.0
    multiplier, or a fee count reconstructed from coin notional."""

    def test_the_provenance_is_internally_coherent(self):
        res, cap = self._run(signal=dict(PERP_SIGNAL, id="sig-b0-prov"))
        self.assertTrue(res.get("ok"), res)
        doc = cap["provenance"]

        self.assertEqual(doc["product"], PR.CRYPTO_PERP)
        self.assertEqual(doc["venue"], BP.KRAKEN_US_VENUE)
        self.assertEqual(doc["instrument"], PERP_SYM)
        self.assertEqual(doc["quantity_unit"], "CONTRACTS")
        self.assertAlmostEqual(doc["multiplier"], 0.01)

        # Quantities relate downward and describe one order.
        self.assertLessEqual(doc["filled_quantity"],
                             doc["requested_quantity"])
        self.assertLessEqual(doc["requested_quantity"],
                             doc["authorized_quantity"])

        # The executed notional is the contract arithmetic, not coin math.
        self.assertAlmostEqual(
            doc["executed_notional_usd"],
            doc["filled_quantity"] * doc["actual_entry_fill"]
            * doc["multiplier"], places=6)

        # The fee counted the filled contracts, exactly.
        from lib import fee_authority as FA
        self.assertEqual(doc["entry_fee_contract_count"],
                         doc["filled_quantity"])
        self.assertEqual(doc["entry_fee_contract_count_basis"],
                         FA.EXECUTED_EXACT)
        self.assertIsNone(doc["entry_fee_rate"])
