"""Pass B final — every exit caller routes through one classification layer.

THE GOLDEN RULE AT THE CALLER BOUNDARY. A caller may say "reduce this
position", and may offer a mark, a trigger and a reason. A caller may NEVER
decide the product, the venue, the contract, the executable side, the fill,
the fee, the carry, or the settlement arithmetic — a canonical position
already owns all of those.

So these tests attack the two ways a caller could still smuggle its own
economics in:

    a generic cross-market MARK deciding that a canonical stop/target/
    margin call has fired, when the contract's OWN executable book has not
    reached that level

    a caller-supplied price becoming the FILL

and the two administrative hazards routing creates:

    a portfolio reset that reseeds fresh capital while exposure is open

    a TP1 scale-out that fires twice because its compatibility latch was
    never set
"""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import canonical_settlement as CS
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
ENTRY_BID, ENTRY_ASK = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-route-{next(_seq)}",
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


def _seed_book(bid=ENTRY_BID, ask=ENTRY_ASK):
    MD.reset_books()
    book = MD.book_for(PERP_SYM, create=True)
    book.apply({"type": "book", "ack_id": "1000", "symbol": PERP_SYM,
                "timestamp": _at(0.1).isoformat().replace("+00:00", "Z"),
                "bids": [[int(bid / TICK), 50]],
                "asks": [[int(ask / TICK), 50]]})
    return book


class _RoutingHarness(unittest.TestCase):

    def setUp(self):
        _seed_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPortfolio, PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).delete()
            pf = db.query(PaperPortfolio).first()
            if pf is not None:
                pf.cash = 100_000.0
            db.commit()

        def _close_leftovers():
            with get_db() as db:
                db.query(PaperPosition).filter(
                    PaperPosition.status == "Open").update(
                    {"status": "Closed"})
                db.commit()
        self.addCleanup(_close_leftovers)

    # ── state ────────────────────────────────────────────────────────────
    def _portfolio(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {"cash": float(p.cash), "id": p.id,
                    "total": float(p.total_trades or 0)}

    def _state(self, position_id):
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  PaperRealizedOutcome, PaperSettlementLeg,
                                  get_db)
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
            db.expunge_all()
        return pos, header, legs, outcomes

    def _set_position(self, position_id, **fields):
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == position_id).update(fields)
            db.commit()

    def _assert_still_open(self, position_id, cash_before):
        pos, header, legs, outcomes = self._state(position_id)
        self.assertEqual(pos.status, "Open",
                         "a refused/unconfirmed exit closed the position")
        self.assertEqual(header.settlement_revision, 0)
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])
        self.assertEqual(self._portfolio()["cash"], cash_before,
                         "a refused/unconfirmed exit moved cash")

    # ── chain ────────────────────────────────────────────────────────────
    def _enter(self, signal=None):
        from lib import canonical_entry as CE
        with _spot_feed():
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        pos, header, *_ = self._state(res["position"]["id"])
        return pos, header

    def _legacy_position(self, price=100.0):
        """A real legacy position — no canonical provenance, no ledger."""
        from lib.paper_engine import open_paper_position
        res = open_paper_position(
            _signal(product="CRYPTO_SPOT", asset_symbol="ETH/USD",
                    entry_price=price, stop_loss=price * 0.95,
                    target_price=price * 1.15),
            current_price=price)
        self.assertTrue(res.get("ok"), res)
        return res["position"]["id"]


class GenericMarkCannotFireACanonicalTriggerTests(_RoutingHarness):
    """P0.1-P0.3 — the contract's OWN executable book confirms an automated
    trigger. A cross-market mark is evidence that something MAY have
    happened; it is not authority to liquidate a contract whose book has
    not reached the level."""

    def _mark_to_market(self, mark):
        from lib.paper_engine import mark_to_market
        return mark_to_market({"BTC/USD": mark})

    def test_a_generic_mark_below_the_stop_does_not_close(self):
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=64_000.0, target_price=70_000.0)
        # The exact PBTC book has NOT reached the stop: bid 64,200 > 64,000.
        _seed_book(bid=64_200.0, ask=64_250.0)
        cash = self._portfolio()["cash"]

        res = self._mark_to_market(63_900.0)      # generic mark crosses

        self.assertEqual(
            [c for c in res["closed"] if c["symbol"] == "BTC/USD"], [],
            "a generic mark closed a canonical position whose own book "
            "never reached the stop")
        self._assert_still_open(pos.id, cash)

    def test_a_generic_mark_above_the_target_does_not_close(self):
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=55_000.0, target_price=66_000.0)
        # Exact bid 65,900 has NOT reached the 66,000 target.
        _seed_book(bid=65_900.0, ask=65_950.0)
        cash = self._portfolio()["cash"]

        res = self._mark_to_market(66_100.0)

        self.assertEqual(
            [c for c in res["closed"] if c["symbol"] == "BTC/USD"], [])
        self._assert_still_open(pos.id, cash)

    def test_a_short_target_needs_the_ask_not_a_generic_mark(self):
        pos, header = self._enter(_signal(paper_direction="Short",
                                          stop_loss=68_000.0,
                                          target_price=58_000.0))
        self._set_position(pos.id, stop_loss=70_000.0, target_price=63_000.0)
        # A SHORT exits by BUYING: the ASK must reach the target. 63,100 has
        # not, even though a generic 62,900 print suggests it did.
        _seed_book(bid=63_050.0, ask=63_100.0)
        cash = self._portfolio()["cash"]

        res = self._mark_to_market(62_900.0)

        self.assertEqual(
            [c for c in res["closed"] if c["symbol"] == "BTC/USD"], [])
        self._assert_still_open(pos.id, cash)

    def test_a_generic_mark_cannot_margin_call_a_healthy_book(self):
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=1.0, target_price=999_999.0)
        # The exact book is barely below entry — nowhere near a margin call.
        _seed_book(bid=64_450.0, ask=64_500.0)
        cash = self._portfolio()["cash"]

        # A generic mark far below entry makes the LEGACY arithmetic say
        # margin call (equity in position < 15% of margin).
        res = self._mark_to_market(30_000.0)

        self.assertEqual(
            [c for c in res["closed"] if c["symbol"] == "BTC/USD"], [],
            "a cross-venue price liquidated a contract whose own book had "
            "not reached the condition")
        self._assert_still_open(pos.id, cash)

    def test_the_control_only_the_book_decides(self):
        """§30 — WITHOUT THIS THE REFUSALS ABOVE PROVE NOTHING. The same
        caller mark, twice; only the exact book changes. First it does not
        confirm and nothing settles; then it does and the exit executes.

        A target is used rather than a stop because the legacy mark
        arithmetic is unit-blind (it prices 26 PBTC CONTRACTS as 26 coins),
        so any adverse mark margin-calls in that arithmetic and the trigger
        under test would be the margin call rather than the stop. That
        wrongness is exactly why a caller mark may not settle anything —
        and the canonical book refuses it, as the margin-call test above
        proves.
        """
        pos, header = self._enter()
        self._set_position(pos.id, stop_loss=1.0, target_price=64_900.0)
        cash = self._portfolio()["cash"]

        # The executable bid has NOT reached the 64,900 target.
        _seed_book(bid=64_800.0, ask=64_850.0)
        first = self._mark_to_market(65_000.0)
        self.assertEqual(
            [c for c in first["closed"] if c["symbol"] == "BTC/USD"], [])
        self.assertEqual(len(first["trigger_refused"]), 1)
        self._assert_still_open(pos.id, cash)

        # SAME caller mark. Only the book moved.
        _seed_book(bid=65_100.0, ask=65_150.0)
        second = self._mark_to_market(65_000.0)

        closed = [c for c in second["closed"] if c["symbol"] == "BTC/USD"]
        self.assertEqual(len(closed), 1, second)
        self.assertEqual(closed[0]["route"], "CANONICAL")
        pos2, header2, legs, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(len(outcomes), 1)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        # Three facts: the THRESHOLD triggered, the mark was the reference,
        # the book paid.
        self.assertEqual(leg.trigger_price, 64_900.0)
        self.assertEqual(leg.decision_price, 65_000.0)
        self.assertLessEqual(leg.fill_price, 65_100.0)
        self.assertNotEqual(leg.fill_price, 65_000.0,
                            "the caller mark became the fill")


class SoftResetCannotManufactureCapitalTests(_RoutingHarness):
    """P0.4 — NEVER FRESH CASH + OLD OPEN EXPOSURE."""

    def test_a_refused_close_blocks_the_reseed(self):
        from lib.paper_engine import soft_reset_paper_portfolio
        pos, header = self._enter()
        before = self._portfolio()

        # The exact book is stale, so the canonical exit cannot execute.
        real_top = MD.latest_top

        def aged(sym):
            top = real_top(sym)
            return dict(top, age_s=3_600.0) if top else top
        with patch.object(MD, "latest_top", aged):
            res = soft_reset_paper_portfolio()

        self.assertFalse(res.get("ok"),
                         "a reset that could not close everything reported "
                         "success")
        self.assertEqual(res.get("error"), "RESET_INCOMPLETE")
        after = self._portfolio()
        self.assertEqual(after["cash"], before["cash"],
                         "fresh capital was seeded over open exposure")
        self.assertEqual(after["id"], before["id"],
                         "the portfolio row was replaced anyway")
        pos2, _, _, _ = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")

    def test_a_clean_reset_still_reseeds(self):
        """The control: with a live book every position closes and the
        reseed proceeds."""
        from lib.paper_engine import soft_reset_paper_portfolio
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)

        res = soft_reset_paper_portfolio()

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self._portfolio()["cash"], 100_000.0)
        pos2, _, _, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(len(outcomes), 1)


class Tp1CannotScaleOutTwiceTests(_RoutingHarness):
    """P0.5 — one TP1 per position. Canonical settlement does not use
    `scaled_out` as accounting authority, but the caller's latch must still
    be set or the trigger fires every cycle."""

    def test_tp1_fires_once_across_two_cycles(self):
        from jobs.paper_trading import _maybe_scale_out_paper
        pos, header = self._enter()
        # A target that puts TP1 within reach of the current book.
        self._set_position(pos.id, target_price=66_000.0)
        _seed_book(bid=65_500.0, ask=65_600.0)

        def _pos_dict():
            p, _, _, _ = self._state(pos.id)
            return {"id": p.id, "symbol": p.symbol, "entry_price":
                    p.entry_price, "target_price": p.target_price,
                    "qty": p.qty, "direction": p.direction,
                    "scaled_out": bool(p.scaled_out)}

        first = _maybe_scale_out_paper(_pos_dict(), 65_500.0)
        self.assertIsNotNone(first, "TP1 never fired")

        _seed_book(bid=65_500.0, ask=65_600.0)
        second = _maybe_scale_out_paper(_pos_dict(), 65_500.0)
        self.assertIsNone(second, "TP1 scaled out a second time")

        _, header2, legs, outcomes = self._state(pos.id)
        partials = [l for l in legs if l.kind == "PARTIAL_EXIT"]
        self.assertEqual(len(partials), 1)
        self.assertEqual(header2.settlement_revision, 1)
        self.assertEqual(outcomes, [], "a partial produced an outcome")

    def test_the_latch_is_compatibility_only_not_accounting_authority(self):
        """A deliberate non-TP1 partial must still be possible afterwards."""
        from lib import exit_dispatch as ED
        from jobs.paper_trading import _maybe_scale_out_paper
        pos, header = self._enter()
        self._set_position(pos.id, target_price=66_000.0)
        _seed_book(bid=65_500.0, ask=65_600.0)

        def _pos_dict():
            p, _, _, _ = self._state(pos.id)
            return {"id": p.id, "symbol": p.symbol,
                    "entry_price": p.entry_price,
                    "target_price": p.target_price, "qty": p.qty,
                    "direction": p.direction,
                    "scaled_out": bool(p.scaled_out)}

        self.assertIsNotNone(_maybe_scale_out_paper(_pos_dict(), 65_500.0))
        p, _, _, _ = self._state(pos.id)
        self.assertTrue(p.scaled_out, "the compatibility latch was not set")

        _seed_book(bid=65_500.0, ask=65_600.0)
        res = ED.request_position_partial_exit(
            pos.id, fraction=0.5, caller_price=65_500.0,
            caller_reason="scale_out", caller_source="TEST_DIRECT")
        self.assertTrue(res.get("ok"), res)
        _, header2, legs, _ = self._state(pos.id)
        self.assertEqual(
            len([l for l in legs if l.kind == "PARTIAL_EXIT"]), 2,
            "scaled_out blocked a deliberate canonical partial")
        self.assertEqual(header2.settlement_revision, 2)


class ClassificationTests(_RoutingHarness):
    """§3 — one classifier, independently testable. A mixed state is never
    adjudicated toward whichever economy happens to work."""

    def _classify(self, position_id):
        from lib import exit_dispatch as ED
        from app.database import (PaperPosition, PaperPositionSettlement,
                                  get_db)
        with get_db() as db:
            pos = db.query(PaperPosition).filter(
                PaperPosition.id == position_id).first()
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == position_id).first()
            route = ED.classify_position(pos, header)
            db.expunge_all()
        return route

    def test_a_legacy_position_is_legacy(self):
        from lib import exit_dispatch as ED
        pid = self._legacy_position()
        self.assertEqual(self._classify(pid), ED.LEGACY)

    def test_a_canonical_position_is_canonical(self):
        from lib import exit_dispatch as ED
        pos, _ = self._enter()
        self.assertEqual(self._classify(pos.id), ED.CANONICAL)

    def test_a_canonical_fill_with_no_ledger_is_hybrid(self):
        from lib import exit_dispatch as ED
        from app.database import PaperPositionSettlement, get_db
        pos, _ = self._enter()
        with get_db() as db:
            db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == pos.id).delete()
            db.commit()
        self.assertEqual(self._classify(pos.id), ED.HYBRID)

    def test_a_ledger_with_no_canonical_fill_is_hybrid(self):
        from lib import exit_dispatch as ED
        pos, _ = self._enter()
        self._set_position(pos.id, execution_provenance=None)
        self.assertEqual(self._classify(pos.id), ED.HYBRID)

    def test_a_legacy_cost_model_on_a_venue_fill_is_hybrid(self):
        import json
        from lib import exit_dispatch as ED
        pos, _ = self._enter()
        doc = json.loads(pos.execution_provenance)
        doc["cost_model"] = "legacy_round_trip_v1"
        self._set_position(pos.id, execution_provenance=json.dumps(doc))
        self.assertEqual(self._classify(pos.id), ED.HYBRID)

    def test_a_superseded_header_model_is_hybrid(self):
        from lib import exit_dispatch as ED
        from app.database import PaperPositionSettlement, get_db
        pos, _ = self._enter()
        with get_db() as db:
            db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == pos.id).update(
                {"settlement_version": "paper_settlement_v0"})
            db.commit()
        self.assertEqual(self._classify(pos.id), ED.HYBRID)


class HybridNeverFallsBackTests(_RoutingHarness):
    """§23/§37 — at every surface, and the legacy leaf stays unmutated."""

    def _hybridize(self):
        import json
        pos, _ = self._enter()
        doc = json.loads(pos.execution_provenance)
        doc["cost_model"] = "legacy_round_trip_v1"
        self._set_position(pos.id, execution_provenance=json.dumps(doc))
        return pos

    def test_every_surface_refuses_a_hybrid(self):
        from lib import exit_dispatch as ED
        pos = self._hybridize()
        cash = self._portfolio()["cash"]
        _seed_book(bid=64_700.0, ask=64_800.0)

        for kwargs in ({"caller_reason": "manual",
                        "caller_source": "API_MANUAL"},
                       {"caller_reason": "telegram_manual",
                        "caller_source": "TELEGRAM_MANUAL"},
                       {"caller_reason": "stop_loss",
                        "caller_source": "MARK_TO_MARKET",
                        "trigger_price": 64_000.0},
                       {"caller_reason": "reset",
                        "caller_source": "SOFT_RESET"}):
            res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                           **kwargs)
            self.assertFalse(res.get("ok"), res)
            self.assertEqual(res["route"], ED.HYBRID)
            self.assertEqual(res["error"], ED.HYBRID_POSITION_EXIT_REFUSED)

        p, header, legs, outcomes = self._state(pos.id)
        self.assertEqual(p.status, "Open")
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])
        self.assertEqual(self._portfolio()["cash"], cash)

    def test_a_hybrid_partial_refuses_too(self):
        from lib import exit_dispatch as ED
        pos = self._hybridize()
        res = ED.request_position_partial_exit(
            pos.id, fraction=0.5, caller_price=64_700.0,
            caller_reason="scale_out_tp1", caller_source="PAPER_TP1")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["route"], ED.HYBRID)


class TheLegacyGuardIsStillPermanentTests(_RoutingHarness):
    """§24/§5 — the lock stays on even though the normal callers now use
    another door. Defense in depth, not a migration artefact."""

    def test_the_legacy_leaf_still_refuses_a_canonical_position(self):
        from lib.paper_engine import (
            CANONICAL_REQUIRES_EXECUTION_SETTLEMENT, close_paper_position)
        pos, _ = self._enter()
        cash = self._portfolio()["cash"]
        res = close_paper_position(pos.id, 64_700.0, reason="manual")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"),
                         CANONICAL_REQUIRES_EXECUTION_SETTLEMENT)
        self._assert_still_open(pos.id, cash)

    def test_the_legacy_partial_leaf_still_refuses(self):
        from lib.paper_engine import (
            CANONICAL_REQUIRES_EXECUTION_SETTLEMENT,
            partial_close_paper_position)
        pos, _ = self._enter()
        cash = self._portfolio()["cash"]
        res = partial_close_paper_position(pos.id, 0.5, 64_700.0,
                                           reason="scale_out")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"),
                         CANONICAL_REQUIRES_EXECUTION_SETTLEMENT)
        self._assert_still_open(pos.id, cash)


class CallerMarkIsNeverACanonicalFillTests(_RoutingHarness):
    """§28/§29 — the final mechanical proof that ten mark-as-fill paths are
    gone, with the legacy control that keeps it from being vacuous."""

    def _close_at(self, mark, source):
        from lib import exit_dispatch as ED
        from sqlalchemy import text
        from app.database import (PaperPortfolio, PaperPosition, PaperTrade,
                                  engine, get_db)
        # A CONTROLLED COMPARISON: every input except the caller's mark is
        # held constant. Entry sizing legitimately responds to equity, the
        # recent loss streak, and accumulated historical edge — and the
        # first close moves all three (it books a trade, and its learning
        # projection writes an outcome). Leaving those in would compare two
        # differently-sized positions and prove nothing about the mark.
        with get_db() as db:
            db.query(PaperPosition).delete()
            db.query(PaperTrade).delete()
            db.query(PaperPortfolio).first().cash = 100_000.0
            db.commit()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM trade_outcomes"))
        _seed_book()                      # the SAME entry book both times
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=mark,
                                       caller_reason="manual",
                                       caller_source=source)
        self.assertTrue(res.get("ok"), res)
        _, _, legs, outcomes = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        return res, leg, outcomes[0]

    def test_an_absurd_caller_mark_changes_no_economics(self):
        """§28 — THE FINAL MECHANICAL PROOF that the ten mark-as-fill paths
        are gone. Compared PER UNIT rather than per position: position SIZE
        is a legitimate function of the book's accumulated evidence, and two
        sequential entries are not required to be the same size. What must
        be identical is every price and every per-contract cost — those are
        the numbers a caller's mark could have moved, and did not."""
        sane, leg_sane, o_sane = self._close_at(64_700.0, "API_MANUAL")
        poisoned, leg_p, o_p = self._close_at(999_999.0, "API_MANUAL")

        # The fill came from the book, so it is identical to the cent.
        self.assertEqual(leg_p.fill_price, leg_sane.fill_price)
        self.assertEqual(poisoned["close_price"], sane["close_price"])
        self.assertLessEqual(leg_p.fill_price, 64_700.0)

        def per_unit(leg):
            q = float(leg.filled_qty)
            return (round(leg.explicit_fee_usd / q, 12),
                    round(leg.gross_pnl_usd / q, 9))
        self.assertEqual(per_unit(leg_p), per_unit(leg_sane),
                         "an absurd caller mark moved the per-contract "
                         "economics")
        # Carry is deliberately NOT in that tuple: it is a function of the
        # elapsed interval, which differs by milliseconds between two
        # sequential runs. What matters here is that the mark did not touch
        # it — the quote priced the same instrument on the same terms.
        self.assertEqual(leg_p.holding_cost_type, leg_sane.holding_cost_type)
        self.assertEqual(leg_p.holding_cost_quality,
                         leg_sane.holding_cost_quality)
        # And the leg's arithmetic is a pure function of the BOOK's fill.
        for leg in (leg_p, leg_sane):
            self.assertAlmostEqual(
                leg.gross_pnl_usd,
                (leg.fill_price - o_sane.actual_entry_fill)
                * leg.filled_qty * 0.01, places=6)

        # Only the REFERENCE moved.
        self.assertEqual(leg_p.decision_price, 999_999.0)
        self.assertEqual(leg_sane.decision_price, 64_700.0)

    def test_the_legacy_control_a_mark_still_is_the_legacy_fill(self):
        """Legacy semantics are deliberately unchanged: there, the caller's
        price IS the fill. Without this the poison test proves nothing."""
        from lib import exit_dispatch as ED
        a = self._legacy_position(price=100.0)
        res_a = ED.request_position_exit(a, caller_price=110.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
        self.assertTrue(res_a.get("ok"), res_a)
        self.assertEqual(res_a["route"], ED.LEGACY)

        b = self._legacy_position(price=100.0)
        res_b = ED.request_position_exit(b, caller_price=90.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
        self.assertTrue(res_b.get("ok"), res_b)
        self.assertNotEqual(res_a["pnl"], res_b["pnl"],
                            "the legacy mark stopped being the legacy fill "
                            "— the poison test above is vacuous")

    def test_cash_delta_is_never_reported_as_pnl(self):
        """§13 — cash delta carries the released margin, which was always
        ours."""
        res, leg, o = self._close_at(64_700.0, "API_MANUAL")
        self.assertAlmostEqual(res["pnl"], o.net_pnl_usd, places=9)
        self.assertGreater(leg.released_margin_usd, 0)
        self.assertNotAlmostEqual(res["pnl"],
                                  res["pnl"] + leg.released_margin_usd,
                                  places=2)


class ReasonMappingTests(unittest.TestCase):
    """§6 — one mapping; an unknown string refuses rather than guessing."""

    def test_the_audited_caller_strings_all_map(self):
        from lib import exit_dispatch as ED
        from lib.realized_outcome import (ADMINISTRATIVE_RESET, MARGIN_CALL,
                                          STOP_EXIT, TARGET_EXIT,
                                          VOLUNTARY_EXIT)
        cases = {
            "stop_loss": STOP_EXIT,
            "take_profit": TARGET_EXIT,
            "margin_call": MARGIN_CALL,
            "scale_out_tp1": "SCALE_OUT",
            "reset": ADMINISTRATIVE_RESET,
            "manual": VOLUNTARY_EXIT,
            "manual flatten": VOLUNTARY_EXIT,
            "telegram_manual": VOLUNTARY_EXIT,
            "api_manual": VOLUNTARY_EXIT,
            "risk_guard": VOLUNTARY_EXIT,
            "AI EXIT: the thesis broke down": VOLUNTARY_EXIT,
        }
        for caller, expected in cases.items():
            self.assertEqual(ED.canonical_reason_for(caller), expected,
                             f"{caller!r} mapped wrongly")

    def test_every_mapped_reason_is_a_real_canonical_reason(self):
        from lib import canonical_settlement as CS
        from lib import exit_dispatch as ED
        for value in set(ED._REASON_MAP.values()):
            self.assertIn(value, CS.EXIT_REASONS,
                          f"{value!r} is not in the settlement vocabulary")

    def test_an_unknown_reason_refuses_rather_than_guessing(self):
        from lib import exit_dispatch as ED
        self.assertIsNone(ED.canonical_reason_for("wat_is_this"))
        self.assertIsNone(ED.canonical_reason_for("emergency_unwind"))


class AdministrativeResetIsNotStrategyEvidenceTests(_RoutingHarness):
    """§7 — a reset gets financial history and no learning vote."""

    def _learning_state(self, outcome_id):
        from app.database import PaperRealizedOutcome, get_db
        with get_db() as db:
            row = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.id == outcome_id).one()
            state = row.learning_state
            db.expunge_all()
        return state

    def test_a_reset_close_is_administrative_and_skips_learning(self):
        from sqlalchemy import text
        from app.database import engine
        from lib import canonical_learning as CL
        from lib import exit_dispatch as ED
        from lib.realized_outcome import ADMINISTRATIVE_RESET

        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="reset",
                                       caller_source="SOFT_RESET")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["reason"], ADMINISTRATIVE_RESET)

        _, _, legs, outcomes = self._state(pos.id)
        o = outcomes[0]
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.exit_reason, ADMINISTRATIVE_RESET)
        # Financial truth exists and stays queryable.
        self.assertIsNotNone(o.net_pnl_usd)
        # Learning declined it by policy, at the dispatcher's handoff.
        self.assertEqual(res["learning"]["result"],
                         CL.LEARNING_SKIPPED_POLICY)
        self.assertEqual(self._learning_state(o.id), CL.SKIPPED_POLICY)
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM trade_outcomes "
                "WHERE canonical_outcome_id=:c"), {"c": o.id}).fetchone()[0]
        self.assertEqual(n, 0, "an administrative reset taught the strategy")

        again = CL.apply_realized_outcome(o.id)
        self.assertTrue(again.get("ok"))
        self.assertTrue(again.get("skipped"))

    def test_a_sweep_never_teaches_a_reset(self):
        from lib import canonical_learning as CL
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_exit(pos.id, caller_price=64_700.0,
                                       caller_reason="reset",
                                       caller_source="SOFT_RESET")
        self.assertTrue(res.get("ok"), res)
        _, _, _, outcomes = self._state(pos.id)
        CL.apply_pending_realized_outcomes(limit=500)
        self.assertEqual(self._learning_state(outcomes[0].id),
                         CL.SKIPPED_POLICY)


class LearningHandoffTests(_RoutingHarness):
    """§31/§32 — the trade is closed; learning is secondary."""

    def test_a_routed_final_applies_learning_once(self):
        from sqlalchemy import text
        from app.database import engine
        from lib import exit_dispatch as ED

        pos, header = self._enter()
        _seed_book(bid=64_900.0, ask=65_000.0)
        res = ED.request_position_exit(pos.id, caller_price=64_900.0,
                                       caller_reason="manual",
                                       caller_source="API_MANUAL")
        self.assertTrue(res.get("ok"), res)
        outcome_id = res["realized_outcome_id"]
        self.assertEqual(res["learning"]["result"], "LEARNING_APPLIED")
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id FROM trade_outcomes WHERE canonical_outcome_id=:c"),
                {"c": outcome_id}).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], outcome_id)

    def test_a_routed_partial_never_votes(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_700.0, ask=64_800.0)
        res = ED.request_position_partial_exit(
            pos.id, fraction=0.5, caller_price=64_700.0,
            caller_reason="scale_out_tp1", caller_source="PAPER_TP1")
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("learning", res)
        _, _, _, outcomes = self._state(pos.id)
        self.assertEqual(outcomes, [])

    def test_a_learning_failure_does_not_reverse_the_close(self):
        from lib import canonical_learning as CL
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_900.0, ask=65_000.0)
        before = self._portfolio()["cash"]

        with patch.object(CL, "apply_realized_outcome",
                          side_effect=RuntimeError("injected learning fault")):
            res = ED.request_position_exit(pos.id, caller_price=64_900.0,
                                           caller_reason="manual",
                                           caller_source="API_MANUAL")

        self.assertTrue(res.get("ok"),
                        "a learning fault reversed a correct financial close")
        self.assertFalse(res["learning"]["ok"])
        pos2, header2, _, outcomes = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(len(outcomes), 1)
        self.assertNotEqual(self._portfolio()["cash"], before)

    def test_a_second_full_close_creates_no_second_execution(self):
        """§33 — closed state is the natural idempotency for full exits."""
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_900.0, ask=65_000.0)
        first = ED.request_position_exit(pos.id, caller_price=64_900.0,
                                         caller_reason="manual",
                                         caller_source="API_MANUAL")
        self.assertTrue(first.get("ok"), first)
        cash = self._portfolio()["cash"]

        second = ED.request_position_exit(pos.id, caller_price=64_900.0,
                                          caller_reason="manual",
                                          caller_source="API_MANUAL")
        self.assertFalse(second.get("ok"))
        self.assertEqual(second["error"], ED.POSITION_NOT_OPEN)
        self.assertEqual(self._portfolio()["cash"], cash)
        _, _, legs, outcomes = self._state(pos.id)
        self.assertEqual(len([l for l in legs if l.kind != "ENTRY"]), 1)
        self.assertEqual(len(outcomes), 1)


class EvidenceOnlyBlocksEveryRouteTests(_RoutingHarness):
    """§27 — dispatch is not a way around the mode boundary."""

    def _under_evidence_only(self, fn):
        import os
        os.environ["JARVIS_RUNTIME_MODE"] = "EVIDENCE_ONLY"
        try:
            return fn()
        finally:
            os.environ.pop("JARVIS_RUNTIME_MODE", None)

    def test_a_canonical_route_mutates_nothing(self):
        from lib import exit_dispatch as ED
        pos, header = self._enter()
        _seed_book(bid=64_900.0, ask=65_000.0)
        cash = self._portfolio()["cash"]
        with self.assertRaises(Exception):
            self._under_evidence_only(
                lambda: ED.request_position_exit(
                    pos.id, caller_price=64_900.0, caller_reason="manual",
                    caller_source="API_MANUAL"))
        self._assert_still_open(pos.id, cash)

    def test_a_legacy_route_mutates_nothing(self):
        from lib import exit_dispatch as ED
        pid = self._legacy_position()
        cash = self._portfolio()["cash"]
        with self.assertRaises(Exception):
            self._under_evidence_only(
                lambda: ED.request_position_exit(
                    pid, caller_price=110.0, caller_reason="manual",
                    caller_source="API_MANUAL"))
        self.assertEqual(self._portfolio()["cash"], cash)


class NoProductionCallerTouchesTheLegacyLeafTests(unittest.TestCase):
    """§25 — proved from the code, not from documentation."""

    ALLOWED = {
        "lib/paper_engine.py",       # the definitions themselves
        "lib/exit_dispatch.py",      # the one legitimate legacy branch
        "lib/runtime_mode.py",       # prose about the mutation guard
    }

    def test_no_production_module_calls_the_legacy_leaves_directly(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).parent.parent
        offenders = []
        for folder in ("lib", "app", "jobs"):
            for path in (root / folder).rglob("*.py"):
                rel = path.relative_to(root).as_posix()
                if rel in self.ALLOWED:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = None
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    if name in ("close_paper_position",
                                "partial_close_paper_position"):
                        offenders.append(f"{rel}:{node.lineno} {name}")
        self.assertEqual(offenders, [],
                         "production code still reaches the legacy exit "
                         "leaves directly instead of the dispatcher: "
                         + "; ".join(offenders))

    def test_the_audited_callers_reference_the_dispatcher(self):
        import pathlib
        root = pathlib.Path(__file__).parent.parent
        for rel in ("lib/paper_engine.py", "jobs/paper_trading.py",
                    "jobs/telegram_bot.py", "app/routers/trading.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertIn("exit_dispatch", src,
                          f"{rel} never reaches the dispatcher")


if __name__ == "__main__":
    unittest.main()
