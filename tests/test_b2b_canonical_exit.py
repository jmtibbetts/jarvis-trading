"""B2B — the canonical exit orchestrator against its own frozen book.

Every position is opened by the REAL canonical entry chain; every exit goes
through `close_canonical_position` — frozen B1 identity, exact contract
book, reduce-only plan, ExecutionVenue, exact fee, exact carry, B2A
settlement. Only the market feeds are stubbed, and the permanent invariants
get their own hostile tests:

    MARK/TRIGGER AUTHORITY IS NOT FILL AUTHORITY
    a long exits into the BID, a short lifts the ASK
    no fallback on exit — a broken book leaves the position OPEN
    no contract substitution, no config drift, no silent re-preparation
"""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import canonical_exit as CX
from lib import canonical_settlement as CS
from lib import fee_authority as FA
from lib import holding_cost_authority as HC
from lib import instruments as INST
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
ENTRY_BID, ENTRY_ASK = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-b2b-{next(_seq)}",
            "product": PR.CRYPTO_PERP}
    base.update(over)
    return base


def _at(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _spot_feed():
    return patch.multiple(
        "lib.kraken_stream",
        latest_quote=lambda symbol: {"bid": SPOT_BID, "ask": SPOT_ASK,
                                     "at": _at(0.2)},
        trade_flow=lambda symbol, window=200: None)


def _seed_book(bid=ENTRY_BID, ask=ENTRY_ASK, age_s=0.1,
               market_state=MD.STATE_OPEN):
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(age_s).isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid / TICK), 50]],
                "asks": [[int(ask / TICK), 50]]})
    if market_state != MD.STATE_OPEN:
        book.apply({"type": "status", "ack_id": "1001", "symbol": PERP_SYM,
                    "state": market_state})
    return book


class _B2BHarness(unittest.TestCase):

    def setUp(self):
        _seed_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

        # And AFTER each test too: refusal tests deliberately leave open
        # positions, and an open BTC/USD position consumes the shared test
        # book's concentration headroom for every later entry test.
        def _close_leftovers():
            with get_db() as db:
                db.query(PaperPosition).filter(
                    PaperPosition.symbol == "BTC/USD",
                    PaperPosition.status == "Open").update(
                    {"status": "Closed"})
                db.commit()
        self.addCleanup(_close_leftovers)

    def _portfolio(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {"cash": float(p.cash),
                    "total": float(p.total_trades or 0),
                    "wins": float(p.winning_trades or 0)}

    def _state(self, position_id):
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperRealizedOutcome, PaperSettlementLeg,
                                  PaperTrade, get_db)
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == position_id).first()
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).first()
            legs = (db.query(PaperSettlementLeg)
                    .filter(PaperSettlementLeg.position_id == position_id)
                    .order_by(PaperSettlementLeg.settlement_revision).all())
            outcomes = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.position_id == position_id).all()
            trades = db.query(PaperTrade).filter(
                PaperTrade.position_id == position_id).all()
            db.expunge_all()
        return pos, header, legs, outcomes, trades

    def _enter(self, signal=None):
        from lib import canonical_entry as CE
        with _spot_feed():
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        pos, header, *_ = self._state(res["position"]["id"])
        return pos, header

    def _assert_untouched(self, position_id, before):
        self.assertEqual(self._portfolio(), before,
                         "a refused exit moved money")
        pos, header, legs, outcomes, trades = self._state(position_id)
        self.assertEqual(pos.status, "Open")
        self.assertEqual(header.settlement_revision, 0)
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])
        self.assertEqual(trades, [])


class LongAcceptanceTests(_B2BHarness):
    """§38 — the direct long proof, end to end."""

    def _close_full(self, bid=65_000.0, ask=65_100.0, **kw):
        pos, header = self._enter()
        _seed_book(bid=bid, ask=ask)
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id, **kw)
        return pos, header, before, res

    def test_a_long_full_close_settles_through_the_whole_chain(self):
        pos, header, before, res = self._close_full(
            exit_reason=CS.TARGET_EXIT)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["kind"], "FINAL_EXIT")

        pos2, header2, legs, outcomes, trades = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(header2.settlement_revision, 1)
        exit_leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]

        # SELL into the BID, then adverse slippage LOWER.
        self.assertLessEqual(exit_leg.fill_price, 65_000.0)
        self.assertEqual(exit_leg.execution_side, "sell")
        self.assertEqual(exit_leg.instrument_id, PERP_SYM)
        self.assertEqual(exit_leg.quantity_unit, "CONTRACTS")
        self.assertAlmostEqual(exit_leg.multiplier, 0.01)
        self.assertEqual(exit_leg.exit_reason, CS.TARGET_EXIT)

        # Exact fee at the exact filled count.
        self.assertEqual(exit_leg.fee_contract_count, exit_leg.filled_qty)
        self.assertEqual(exit_leg.fee_contract_count_basis,
                         FA.EXECUTED_EXACT)
        # Carry with provenance — the baseline default, labelled as such.
        self.assertEqual(exit_leg.holding_cost_type, HC.KIND_FUNDING)
        self.assertEqual(exit_leg.holding_cost_quality, HC.DEFAULT_BASELINE)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(trades), 1)
        after = self._portfolio()
        self.assertEqual(after["total"] - before["total"], 1)
        # Cash identity from the persisted ledger.
        expected = (header.committed_margin_usd + exit_leg.gross_pnl_usd
                    - exit_leg.explicit_fee_usd - exit_leg.holding_cost_usd)
        self.assertAlmostEqual(after["cash"] - before["cash"], expected,
                               places=6)

    def test_learning_stays_pending_and_uncalled(self):
        from lib import learning_engine as LE

        def explode(*a, **k):
            raise AssertionError("record_trade_outcome was called by exit")
        with patch.object(LE, "record_trade_outcome", explode):
            pos, header, before, res = self._close_full()
        self.assertTrue(res.get("ok"), res)
        _, _, _, (o,), _ = self._state(pos.id)
        self.assertEqual(o.learning_state, "PENDING")
        self.assertIsNone(o.trade_outcome_id)


class ShortAcceptanceTests(_B2BHarness):
    """§39 — short closes BUY the ask; below entry the short wins, and
    positive funding is a CREDIT to the short."""

    def test_a_short_full_close_buys_the_ask_and_wins_below_entry(self):
        pos, header = self._enter(_signal(paper_direction="Short",
                                          stop_loss=68_000.0,
                                          target_price=58_000.0))
        _seed_book(bid=63_000.0, ask=63_100.0)
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id)
        self.assertTrue(res.get("ok"), res)

        pos2, header2, legs, outcomes, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        # BUY lifts the ASK, adverse slippage HIGHER.
        self.assertGreaterEqual(leg.fill_price, 63_100.0)
        self.assertEqual(leg.execution_side, "buy")
        self.assertGreater(leg.gross_pnl_usd, 0,
                           "a short that covered below entry lost money")
        self.assertAlmostEqual(leg.multiplier, 0.01)
        self.assertLessEqual(leg.holding_cost_usd, 0,
                             "positive funding must CREDIT the short")
        self.assertEqual(outcomes[0].side, "short")


class BookSideTests(_B2BHarness):
    """§40 — a deliberately wide book makes side inversion impossible to
    miss."""

    WIDE_BID, WIDE_ASK = 60_000.0, 61_000.0

    def test_a_long_exit_prices_from_the_bid_side(self):
        pos, _ = self._enter()
        _seed_book(bid=self.WIDE_BID, ask=self.WIDE_ASK)
        res = CX.close_canonical_position(pos.id)
        self.assertTrue(res.get("ok"), res)
        self.assertLessEqual(res["fill_price"], self.WIDE_BID)

    def test_a_short_exit_prices_from_the_ask_side(self):
        pos, _ = self._enter(_signal(paper_direction="Short",
                                     stop_loss=68_000.0,
                                     target_price=58_000.0))
        _seed_book(bid=self.WIDE_BID, ask=self.WIDE_ASK)
        res = CX.close_canonical_position(pos.id)
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(res["fill_price"], self.WIDE_ASK)


class StopIsATriggerNotAFillTests(_B2BHarness):
    """§41/§42 — the golden rule's sharpest edge."""

    def test_a_gap_through_the_stop_fills_at_the_book_not_the_stop(self):
        pos, header = self._enter()
        _seed_book(bid=62_500.0, ask=62_600.0)     # gapped through 64,000
        res = CX.close_canonical_position(pos.id,
                                          exit_reason=CS.STOP_EXIT,
                                          trigger_price=64_000.0)
        self.assertTrue(res.get("ok"), res)
        self.assertLessEqual(res["fill_price"], 62_500.0,
                             "the stop trigger became the fill")

        _, _, legs, (o,), _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.trigger_price, 64_000.0)
        self.assertNotEqual(leg.fill_price, 64_000.0)
        self.assertEqual(leg.exit_reason, CS.STOP_EXIT)
        self.assertEqual(o.outcome, "LOSS",
                         "the tail loss below the stop was not recorded")

    def test_trigger_decision_and_fill_are_three_distinct_facts(self):
        pos, _ = self._enter()
        _seed_book(bid=62_500.0, ask=62_600.0)
        res = CX.close_canonical_position(pos.id,
                                          exit_reason=CS.STOP_EXIT,
                                          trigger_price=64_000.0,
                                          decision_price=63_900.0)
        self.assertTrue(res.get("ok"), res)
        _, _, legs, _, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.trigger_price, 64_000.0)
        self.assertEqual(leg.decision_price, 63_900.0)
        self.assertNotEqual(leg.fill_price, leg.trigger_price)
        self.assertNotEqual(leg.fill_price, leg.decision_price)
        self.assertNotEqual(leg.trigger_price, leg.decision_price)


class PartialExitTests(_B2BHarness):

    def test_partial_then_final_through_the_orchestrator(self):
        pos, header = self._enter()
        before = self._portfolio()
        _seed_book(bid=64_700.0, ask=64_800.0)
        p = CX.close_canonical_position(pos.id, requested_qty=4.0,
                                        exit_reason=CS.SCALE_OUT)
        self.assertTrue(p.get("ok"), p)
        self.assertEqual(p["kind"], "PARTIAL_EXIT")
        self.assertEqual(p["filled_qty"], 4.0)

        mid_pos, mid_header, _, outcomes, trades = self._state(pos.id)
        self.assertEqual(mid_pos.qty, pos.qty - 4.0)
        self.assertEqual(mid_header.settlement_revision, 1)
        self.assertEqual(outcomes, [])
        self.assertEqual(trades, [])
        self.assertEqual(self._portfolio()["total"], before["total"])

        _seed_book(bid=64_700.0, ask=64_800.0)
        f = CX.close_canonical_position(pos.id)
        self.assertTrue(f.get("ok"), f)
        self.assertEqual(f["kind"], "FINAL_EXIT")
        end_pos, end_header, legs, outcomes2, _ = self._state(pos.id)
        self.assertEqual(end_pos.status, "Closed")
        self.assertEqual(end_header.settlement_revision, 2)
        self.assertEqual(len(outcomes2), 1)
        self.assertEqual(self._portfolio()["total"] - before["total"], 1)

    def test_a_fraction_normalizes_down_and_says_so(self):
        """§44 — 7 contracts x 0.5 = 3.5 theoretical, 3 executable."""
        import json
        from lib import paper_engine as PE
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).delete()
            db.query(PaperPortfolio).first().cash = 100_000.0
            db.commit()
        # 0.27% of 100k = $270 of risk budget: 7 contracts at the mid's
        # $35.50/contract AND still 7 at the post-fill repricing's ~$37.35,
        # so no resubmission shrinks the position under the test.
        with patch.object(PE, "TRADE_MARGIN_PCT", 0.27):
            pos, header = self._enter()
        self.assertEqual(pos.qty, 7.0)

        _seed_book(bid=64_700.0, ask=64_800.0)
        res = CX.close_canonical_position(pos.id, close_fraction=0.5,
                                          exit_reason=CS.SCALE_OUT)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["filled_qty"], 3.0, "3.5 rounded UP to 4")
        _, _, legs, _, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "PARTIAL_EXIT"][0]
        prov = json.loads(leg.provenance_json)
        self.assertAlmostEqual(prov["theoretical_qty"], 3.5)
        self.assertAlmostEqual(prov["authorized_qty"], 3.0)

    def test_a_corrupt_fractional_remainder_refuses_a_full_close(self):
        """§45 — 2.4 CONTRACTS remaining is corrupted state, not a partial
        request."""
        from app.database import PaperPosition, get_db
        pos, _ = self._enter()
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == pos.id).update({"qty": 2.4})
            db.commit()
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_INVALID_QUANTITY)
        self.assertEqual(self._portfolio(), before)


class NoFallbackOnExitTests(_B2BHarness):
    """§46/§47 — a broken book leaves the position OPEN, by name."""

    def _refused_open(self, pos, before, res, error=None):
        self.assertFalse(res.get("ok"), res)
        if error:
            self.assertEqual(res["error"], error)
        self._assert_untouched(pos.id, before)

    def test_a_stale_book_refuses(self):
        pos, _ = self._enter()
        _seed_book()
        # The book ages from RECEIPT, so staleness is injected where it is
        # measured: the top-of-book report.
        real_top = MD.latest_top

        def aged(sym):
            top = real_top(sym)
            if top is not None:
                top = dict(top, age_s=3_600.0)
            return top
        before = self._portfolio()
        with patch.object(MD, "latest_top", aged):
            res = CX.close_canonical_position(pos.id)
        self._refused_open(pos, before, res, CX.EXIT_MARKET_DATA_UNAVAILABLE)
        self.assertIn("STALE", str(res.get("reason", "")).upper())

    def test_a_desynced_book_refuses(self):
        pos, _ = self._enter()
        book = _seed_book()
        book.invalidate("injected gap")
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id)
        self._refused_open(pos, before, res, CX.EXIT_MARKET_DATA_UNAVAILABLE)

    def test_a_halted_market_refuses(self):
        pos, _ = self._enter()
        _seed_book(market_state=MD.STATE_HALT)
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id)
        self._refused_open(pos, before, res, CX.EXIT_MARKET_DATA_UNAVAILABLE)

    def test_a_closed_session_refuses(self):
        pos, _ = self._enter()
        _seed_book(market_state=MD.STATE_CLOSE)
        before = self._portfolio()
        res = CX.close_canonical_position(pos.id)
        self._refused_open(pos, before, res, CX.EXIT_MARKET_DATA_UNAVAILABLE)

    def test_a_legacy_position_is_refused_by_name(self):
        from lib.paper_engine import open_paper_position
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).delete()
            db.query(PaperPortfolio).first().cash = 100_000.0
            db.commit()
        res = open_paper_position(
            _signal(product="CRYPTO_SPOT", entry_price=100.0,
                    stop_loss=95.0, target_price=115.0),
            current_price=100.0)
        self.assertTrue(res.get("ok"), res)
        out = CX.close_canonical_position(res["position"]["id"])
        self.assertFalse(out.get("ok"))
        self.assertEqual(out["error"], CX.NOT_CANONICAL_POSITION)


class DriftTests(_B2BHarness):
    """§48/§49 — the frozen contract survives resolver and config drift."""

    def test_contract_drift_refuses_before_the_fill_model(self):
        pos, _ = self._enter()
        _seed_book()
        real = BP.resolve

        class Drifted:
            pass

        def drifted(symbol):
            spec = real(symbol)
            d = Drifted()
            for name in dir(spec):
                if not name.startswith("_"):
                    try:
                        setattr(d, name, getattr(spec, name))
                    except AttributeError:
                        pass
            d.symbol = "PBTCUCZ99"
            return d

        from lib import virtual_orders as VO

        def explode(*a, **k):
            raise AssertionError("the fill model ran against a drifted "
                                 "contract")
        before = self._portfolio()
        with patch.object(BP, "resolve", drifted), \
             patch.object(VO, "execute_market", explode):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_MARKET_DATA_UNAVAILABLE)
        self.assertEqual(res.get("reason"), "EXECUTION_INSTRUMENT_MISMATCH")
        self._assert_untouched(pos.id, before)

    def test_config_drift_cannot_change_the_exit_identity(self):
        import os
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        with patch.dict(os.environ, {"CRYPTO_PRODUCT": "CRYPTO_SPOT",
                                     "PAPER_VENUE": "binance"}):
            res = CX.close_canonical_position(pos.id)
        self.assertTrue(res.get("ok"), res)
        _, _, legs, _, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.product, PR.CRYPTO_PERP)
        self.assertEqual(leg.venue, BP.KRAKEN_US_VENUE)
        self.assertEqual(leg.instrument_id, PERP_SYM)

    def test_router_poison_the_persisted_identity_is_enough(self):
        """§50 — today's resolvers are never consulted for a frozen exit."""
        from lib import execution_policy as POL
        from lib import routing_identity as RI

        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        def explode(*a, **k):
            raise AssertionError("a frozen exit consulted a live resolver")
        with patch.object(POL, "resolve_product", explode), \
             patch.object(POL, "resolve_execution_venue", explode), \
             patch.object(RI, "resolve_execution_identity", explode):
            res = CX.close_canonical_position(pos.id)
        self.assertTrue(res.get("ok"), res)

    def test_the_router_poison_control(self):
        """The same poison kills a legacy readiness call — proof it's live."""
        from lib import execution_policy as POL

        def explode(*a, **k):
            raise AssertionError("resolve_product poison reached")
        with patch.object(POL, "resolve_product", explode):
            with self.assertRaises(AssertionError):
                POL.execution_readiness("BTC/USD", "crypto",
                                        signal={"product": PR.CRYPTO_PERP})


class MarkPoisonTests(_B2BHarness):
    """§51 — no reference price is fill authority."""

    def test_favorable_marks_change_nothing_economic(self):
        from app.database import PaperPosition, get_db
        pos, header = self._enter()
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == pos.id).update(
                {"current_price": 999_999.0})
            db.commit()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = CX.close_canonical_position(pos.id,
                                          trigger_price=999_998.0,
                                          decision_price=999_997.0)
        self.assertTrue(res.get("ok"), res)
        # The fill came from the book, not from any of the three marks.
        self.assertLessEqual(res["fill_price"], 64_700.0)
        _, _, legs, (o,), _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.trigger_price, 999_998.0)   # reference only
        self.assertEqual(leg.decision_price, 999_997.0)  # reference only
        self.assertLessEqual(o.actual_exit_fill, 64_700.0)
        self.assertLessEqual(leg.gross_pnl_usd, 0,
                             "a poisoned mark manufactured profit")


class PreparedFactRefusalTests(_B2BHarness):
    """§54-§57 — failures between execution and settlement leave the
    position open and the book unmoved."""

    def test_a_corrupt_holding_notional_is_refused_by_b2a(self):
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        real = HC.holding_cost

        def corrupted(*a, **kw):
            q = real(*a, **kw)
            from dataclasses import replace
            return replace(q, notional_usd=float(q.notional_usd) * 10.0)
        before = self._portfolio()
        with patch.object(HC, "holding_cost", corrupted):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CS.EXIT_VALIDATION_FAILED)
        self._assert_untouched(pos.id, before)

    def test_an_unavailable_holding_cost_refuses_before_settlement(self):
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        broken = HC.HoldingCostQuote(ok=False, amount_usd=None,
                                     quality=HC.UNAVAILABLE,
                                     reason=HC.HOLDING_COST_UNAVAILABLE)
        before = self._portfolio()
        with patch.object(HC, "holding_cost", lambda *a, **k: broken):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_HOLDING_COST_UNAVAILABLE)
        self._assert_untouched(pos.id, before)

    def test_an_unavailable_fee_refuses_before_settlement(self):
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        broken = FA.FeeQuote(ok=False, reason=FA.FEE_AUTHORITY_UNAVAILABLE,
                             detail="injected")
        before = self._portfolio()
        with patch.object(FA, "leg_fee", lambda *a, **k: broken):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_FEE_UNAVAILABLE)
        self._assert_untouched(pos.id, before)

    def test_a_corrupt_execution_result_refuses_before_the_fee(self):
        """§56 — the contradiction fires before fee and carry are even
        priced."""
        from lib import virtual_orders as VO
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        real = VO.execute_market

        def corrupting(*a, **kw):
            res = real(*a, **kw)
            res.quantity_unit = "COINS"
            return res
        fee_called = {"n": 0}
        real_fee = FA.leg_fee

        def counting_fee(*a, **kw):
            fee_called["n"] += 1
            return real_fee(*a, **kw)
        before = self._portfolio()
        with patch.object(VO, "execute_market", corrupting), \
             patch.object(FA, "leg_fee", counting_fee):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_EXECUTION_CONTRADICTION)
        self.assertEqual(fee_called["n"], 0,
                         "a contradicted execution was priced anyway")
        self._assert_untouched(pos.id, before)

    def test_an_instrument_swap_is_refused_at_the_adapter(self):
        """§57 — plan PBTCUCZ50, supplied identity another contract."""
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        real = INST.resolve_for_execution

        def swapped(*a, **kw):
            from dataclasses import replace
            return replace(real(*a, **kw), instrument_id="PETHUCZ50")
        before = self._portfolio()
        with patch.object(INST, "resolve_for_execution", swapped):
            res = CX.close_canonical_position(pos.id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_EXECUTION_REFUSED)
        self.assertEqual(res.get("reason"), "REFUSED_UNIT_BASIS_MISMATCH")
        self._assert_untouched(pos.id, before)


class StaleRevisionTests(_B2BHarness):
    """§58 — a fill prepared for revision N is never replayed against N+1."""

    def test_a_concurrent_settlement_stales_the_prepared_exit(self):
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        # A competing partial, prepared directly against revision 0.
        fill_price = 64_690.0
        fee = FA.leg_fee("BTC/USD", notional=2 * fill_price * 0.01,
                         price=fill_price, product=PR.CRYPTO_PERP,
                         venue=BP.KRAKEN_US_VENUE, maker=False,
                         exact_contract_count=2.0,
                         execution_instrument=INST.resolve_for_execution(
                             "BTC/USD", product=PR.CRYPTO_PERP,
                             venue=BP.KRAKEN_US_VENUE,
                             instrument_id=PERP_SYM),
                         actual_fill_price=fill_price)

        real_settle = CS.settle_prepared_exit
        state = {"hijacked": False}

        def hijack(facts):
            if not state["hijacked"]:
                state["hijacked"] = True
                hold = HC.holding_cost(
                    "BTC/USD", product=PR.CRYPTO_PERP,
                    notional_usd=2 * header.actual_entry_fill * 0.01,
                    hours_held=(datetime.fromisoformat(facts.settled_at)
                                - datetime.fromisoformat(
                                    str(header.opened_at))
                                ).total_seconds() / 3600.0,
                    is_short=False)
                rival = CS.exit_facts(
                    position_id=pos.id, expected_revision=0,
                    execution_id=f"rival-{next(_seq)}",
                    symbol="BTC/USD", product=PR.CRYPTO_PERP,
                    venue=BP.KRAKEN_US_VENUE, instrument_id=PERP_SYM,
                    position_side="long", execution_side="sell",
                    requested_qty=2.0, filled_qty=2.0,
                    quantity_unit="CONTRACTS", multiplier=0.01,
                    fill_price=fill_price, fee_quote=fee,
                    holding_quote=hold, settled_at=facts.settled_at,
                    exit_reason=CS.SCALE_OUT)
                won = real_settle(rival)
                assert won.get("ok"), won
            return real_settle(facts)

        with patch("lib.canonical_settlement.settle_prepared_exit", hijack):
            res = CX.close_canonical_position(pos.id)

        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CS.STALE_SETTLEMENT_REVISION)
        self.assertTrue(res.get("reprepare_required"))
        # Only the rival's partial exists; the stale fill settled nothing.
        _, header2, legs, outcomes, _ = self._state(pos.id)
        self.assertEqual(header2.settlement_revision, 1)
        self.assertEqual(len([l for l in legs if l.kind != "ENTRY"]), 1)
        self.assertEqual(outcomes, [])


class ModeAndAmbiguityTests(_B2BHarness):

    def test_evidence_only_settles_nothing(self):
        import os
        pos, _ = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        before = self._portfolio()
        os.environ["JARVIS_RUNTIME_MODE"] = "EVIDENCE_ONLY"
        try:
            with self.assertRaises(Exception):
                CX.close_canonical_position(pos.id)
        finally:
            os.environ.pop("JARVIS_RUNTIME_MODE", None)
        self._assert_untouched(pos.id, before)

    def test_qty_and_fraction_together_are_ambiguous(self):
        pos, _ = self._enter()
        res = CX.close_canonical_position(pos.id, requested_qty=2.0,
                                          close_fraction=0.5)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CX.EXIT_INVALID_QUANTITY)


class EquityFeeSideTests(unittest.TestCase):
    """§53 — the fee authority receives the EXECUTION side. Unit-level: the
    canonical equity chain is not deterministic in this environment, and
    PBTC acceptance is not weakened to accommodate it."""

    def test_a_long_equity_exit_sells_and_pays_regulatory_fees(self):
        q = FA.leg_fee("AAPL", notional=10_000.0, price=100.0,
                       product="EQUITY_SPOT", venue="alpaca",
                       side="short")             # execution side: SELL
        self.assertTrue(q.ok)
        self.assertGreater(q.fee_usd, 0.0)
        self.assertEqual(q.fee_basis, FA.REGULATORY_PER_SHARE)

    def test_a_short_cover_buys_and_pays_no_sell_side_fee(self):
        q = FA.leg_fee("AAPL", notional=10_000.0, price=100.0,
                       product="EQUITY_SPOT", venue="alpaca",
                       side="long")              # execution side: BUY
        self.assertTrue(q.ok)
        self.assertEqual(q.fee_usd, 0.0)


class StructuralGuardTests(unittest.TestCase):
    """§63 — secondary to the behavioral proofs above."""

    def _calls(self):
        import ast
        import pathlib
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "canonical_exit.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
        return names

    def test_the_orchestrator_owns_no_economics_and_uses_the_boundary(self):
        calls = self._calls()
        for forbidden in ("execute_market", "close_paper_position",
                          "partial_close_paper_position", "add", "commit",
                          "record_trade_outcome", "resolve_product",
                          "resolve_execution_venue",
                          "resolve_execution_identity"):
            self.assertNotIn(forbidden, calls,
                             f"canonical_exit calls {forbidden}")
        for required in ("submit", "resolve_for_execution", "leg_fee",
                         "holding_cost", "settle_prepared_exit",
                         "execution_readiness", "normalize_quantity_down",
                         "execution_disagreement"):
            self.assertIn(required, calls,
                          f"canonical_exit never calls {required}")


if __name__ == "__main__":
    unittest.main()
