"""B2A — the exit settlement core: frozen facts in, exact accounting out.

Every position here is opened by the REAL canonical entry chain (real perp
book, real risk, real fee authority, real B1 ledger), and every exit is a
PREPARED fact set settled through `settle_prepared_exit` — no market
orchestration, which is the whole point of B2A: every accounting invariant
is provable without a provider.

The golden rule, exit edition:

    an exit fill is a historical fact
    margin release is capital, not P&L
    partial exits are accounting legs, not trade votes
    one final position creates one final outcome
    retries cannot settle twice
"""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP
from lib import canonical_settlement as CS
from lib import fee_authority as FA
from lib import holding_cost_authority as HC
from lib import instruments as INST
from lib import product_router as PR

PERP_SYM, TICK = "PBTCUCZ50", 5.0
SPOT_BID, SPOT_ASK = 64_400.0, 64_410.0
PERP_BID_USD, PERP_ASK_USD = 64_500.0, 64_600.0

_seq = itertools.count(1)


def _signal(**over):
    base = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "paper_direction": "Long", "entry_price": 64_400.0,
            "stop_loss": 61_000.0, "target_price": 70_000.0,
            "timeframe": "4H", "id": f"sig-b2a-{next(_seq)}",
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


class _B2AHarness(unittest.TestCase):
    """Real canonical PBTC entry; prepared, deterministic exits."""

    FUNDING_8H = 0.0001                    # pinned; no DB lookup in tests

    def setUp(self):
        _seed_perp_book()
        self.addCleanup(MD.reset_books)
        from app.database import PaperPosition, get_db
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.symbol == "BTC/USD").delete()
            db.commit()

    # ── state readers ─────────────────────────────────────────────────────
    def _portfolio(self):
        from app.database import PaperPortfolio, get_db
        with get_db() as db:
            p = db.query(PaperPortfolio).first()
            return {"cash": float(p.cash),
                    "total": float(p.total_trades or 0),
                    "wins": float(p.winning_trades or 0),
                    "realized": float(p.realized_pnl or 0)}

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
                    .order_by(PaperSettlementLeg.settlement_revision,
                              PaperSettlementLeg.created_at,
                              PaperSettlementLeg.id).all())
            outcomes = db.query(PaperRealizedOutcome).filter(
                PaperRealizedOutcome.position_id == position_id).all()
            trades = db.query(PaperTrade).filter(
                PaperTrade.position_id == position_id).all()
            db.expunge_all()
        return pos, header, legs, outcomes, trades

    # ── chain drivers ─────────────────────────────────────────────────────
    def _enter(self, signal=None):
        from lib import canonical_entry as CE
        with _spot_feed():
            res = CE.open_canonical_position(signal or _signal(),
                                             decision_price=64_400.0)
        self.assertTrue(res.get("ok"), res)
        pos, header, legs, _, _ = self._state(res["position"]["id"])
        return pos, header

    def _exit_facts(self, header, pos, *, filled, fill_price,
                    expected_revision=None, execution_id=None,
                    exit_reason=CS.VOLUNTARY_EXIT, hours=8.0,
                    is_short=None, **over):
        """Prepared facts, priced by the REAL fee authority and the REAL
        holding-cost authority — preparation is allowed to price; only the
        settlement layer is not."""
        short = (header.position_side == "short") if is_short is None \
            else is_short
        fee = FA.leg_fee("BTC/USD",
                         notional=filled * fill_price * 0.01,
                         price=fill_price, product=PR.CRYPTO_PERP,
                         venue=BP.KRAKEN_US_VENUE, maker=False,
                         exact_contract_count=filled,
                         execution_instrument=_pbtc(),
                         actual_fill_price=fill_price)
        hold = HC.holding_cost(
            "BTC/USD", product=PR.CRYPTO_PERP,
            notional_usd=filled * header.actual_entry_fill * 0.01,
            hours_held=hours, is_short=short,
            funding_rate_8h=self.FUNDING_8H)
        settled_at = (datetime.fromisoformat(str(header.opened_at))
                      + timedelta(hours=hours)).isoformat()
        base = dict(
            position_id=header.position_id,
            expected_revision=(header.settlement_revision
                               if expected_revision is None
                               else expected_revision),
            execution_id=execution_id or f"exit-{next(_seq)}",
            symbol=header.symbol, product=header.product,
            venue=header.venue, instrument_id=header.instrument_id,
            position_side=header.position_side,
            execution_side=("buy" if header.position_side == "short"
                            else "sell"),
            requested_qty=filled, filled_qty=filled,
            quantity_unit=header.quantity_unit,
            multiplier=header.multiplier,
            fill_price=fill_price, fee_quote=fee, holding_quote=hold,
            settled_at=settled_at, exit_reason=exit_reason,
            decision_exit_price=fill_price - 5.0,
            trigger_price=None)
        base.update(over)
        return CS.exit_facts(**base), fee, hold


class LongFullCloseTests(_B2AHarness):
    """§47 — the long direct settlement proof, exact to the cent and below."""

    EXIT = 65_000.0

    def _settle_full(self):
        pos, header = self._enter()
        before = self._portfolio()
        facts, fee, hold = self._exit_facts(header, pos, filled=pos.qty,
                                            fill_price=self.EXIT,
                                            exit_reason=CS.TARGET_EXIT)
        res = CS.settle_prepared_exit(facts)
        self.assertTrue(res.get("ok"), res)
        return pos, header, before, facts, fee, hold, res

    def test_the_full_close_settles_exactly(self):
        pos, header, before, facts, fee, hold, res = self._settle_full()
        after = self._portfolio()
        pos2, header2, legs, outcomes, trades = self._state(pos.id)

        qty, entry = pos.qty, header.actual_entry_fill
        gross = (self.EXIT - entry) * qty * 0.01
        self.assertGreater(gross, 0)
        self.assertAlmostEqual(res["gross_pnl_usd"], gross, places=9)
        self.assertAlmostEqual(res["released_margin_usd"],
                               header.committed_margin_usd, places=9)

        # Cash identity: release + gross - fee - holding, exactly.
        expected_delta = (header.committed_margin_usd + gross
                          - fee.fee_usd - hold.amount_usd)
        self.assertAlmostEqual(after["cash"] - before["cash"],
                               expected_delta, places=9)

        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(pos2.qty, 0.0)
        self.assertEqual(pos2.margin_used, 0.0)
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(header2.settlement_revision, 1)
        self.assertEqual(header2.final_execution_id, facts.execution_id)
        self.assertEqual(len([l for l in legs if l.kind != "ENTRY"]), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(header2.realized_outcome_id, outcomes[0].id)
        self.assertEqual(len(trades), 1)

        # One trade, one vote, judged on NET.
        self.assertEqual(after["total"] - before["total"], 1)
        net = gross - fee.fee_usd - hold.amount_usd - header.entry_fee_usd
        self.assertEqual(after["wins"] - before["wins"],
                         1 if net > 0 else 0)
        # realized_pnl accrues the position's canonical net, exactly (§27).
        self.assertAlmostEqual(after["realized"] - before["realized"], net,
                               places=9)
        self.assertAlmostEqual(outcomes[0].net_pnl_usd, net, places=9)
        self.assertAlmostEqual(trades[0].realized_pnl, net, places=9)

    def test_the_final_leg_records_every_exit_fact(self):
        pos, header, _, facts, fee, hold, _ = self._settle_full()
        _, _, legs, _, _ = self._state(pos.id)
        leg = [l for l in legs if l.kind == "FINAL_EXIT"][0]
        self.assertEqual(leg.exit_reason, CS.TARGET_EXIT)
        self.assertEqual(leg.fill_price, self.EXIT)
        self.assertEqual(leg.decision_price, self.EXIT - 5.0)
        self.assertIsNone(leg.trigger_price)
        self.assertEqual(leg.holding_cost_type, HC.KIND_FUNDING)
        self.assertEqual(leg.holding_cost_quality,
                         HC.LATEST_RATE_EXTRAPOLATED)
        self.assertEqual(leg.holding_cost_version, HC.HOLDING_COST_VERSION)
        self.assertAlmostEqual(leg.holding_cost_usd, hold.amount_usd)
        self.assertEqual(leg.remaining_qty_after, 0.0)
        self.assertEqual(leg.remaining_margin_after, 0.0)
        self.assertEqual(leg.fee_contract_count_basis, FA.EXECUTED_EXACT)
        self.assertEqual(leg.fee_contract_count, pos.qty)
        self.assertEqual(leg.execution_side, "sell")

    def test_the_outcome_is_settlement_truth(self):
        from lib.realized_outcome import SETTLEMENT_OUTCOME_VERSION
        pos, header, _, facts, fee, hold, _ = self._settle_full()
        _, _, legs, (o,), _ = self._state(pos.id)[0:3] + self._state(pos.id)[3:]
        self.assertEqual(o.outcome_version, SETTLEMENT_OUTCOME_VERSION)
        self.assertEqual(o.learning_state, "PENDING")
        self.assertIsNone(o.learning_applied_at)
        self.assertEqual(o.return_pct_basis, "MARGIN")
        self.assertEqual(o.quantity_unit, "CONTRACTS")
        self.assertAlmostEqual(o.multiplier, 0.01)
        self.assertEqual(o.instrument_id, PERP_SYM)
        # Entry fee once: commission = entry fee + exit fee, no more.
        self.assertAlmostEqual(o.commission_usd,
                               header.entry_fee_usd + fee.fee_usd, places=9)
        self.assertAlmostEqual(o.funding_usd, hold.amount_usd, places=9)
        self.assertAlmostEqual(
            o.net_r, o.net_pnl_usd / header.initial_risk_usd, places=9)
        self.assertAlmostEqual(
            o.net_return_pct,
            o.net_pnl_usd / header.committed_margin_usd * 100.0, places=9)


class ShortFullCloseTests(_B2AHarness):
    """§48 — the leveraged-short sign class, protected permanently."""

    def test_a_short_exit_below_entry_is_a_positive_gross(self):
        pos, header = self._enter(_signal(
            paper_direction="Short", stop_loss=68_000.0,
            target_price=58_000.0))
        self.assertEqual(header.position_side, "short")
        before = self._portfolio()
        exit_price = 63_000.0                       # below entry: short wins
        facts, fee, hold = self._exit_facts(header, pos, filled=pos.qty,
                                            fill_price=exit_price)
        self.assertEqual(facts.execution_side, "buy")
        res = CS.settle_prepared_exit(facts)
        self.assertTrue(res.get("ok"), res)

        gross = (header.actual_entry_fill - exit_price) * pos.qty * 0.01
        self.assertGreater(gross, 0)
        self.assertAlmostEqual(res["gross_pnl_usd"], gross, places=9)
        # Positive funding: the SHORT RECEIVED carry, so the holding cost is
        # negative and settlement CREDITS it.
        self.assertLess(hold.amount_usd, 0)
        after = self._portfolio()
        expected = (header.committed_margin_usd + gross - fee.fee_usd
                    - hold.amount_usd)
        self.assertAlmostEqual(after["cash"] - before["cash"], expected,
                               places=9)
        _, _, _, (o,), _ = self._state(pos.id)
        self.assertEqual(o.side, "short")
        self.assertAlmostEqual(o.gross_pnl_usd, gross, places=9)


class PartialThenFinalTests(_B2AHarness):
    """§49/§50 — partials are accounting legs; the vote happens once."""

    def test_partial_then_final(self):
        pos, header = self._enter()
        q0, m0 = pos.qty, pos.margin_used
        self.assertGreater(q0, 4)
        before = self._portfolio()

        # ── partial: 4 contracts at hour 8 ──────────────────────────────
        pf, pfee, phold = self._exit_facts(header, pos, filled=4.0,
                                           fill_price=65_000.0, hours=8.0,
                                           exit_reason=CS.SCALE_OUT)
        pres = CS.settle_prepared_exit(pf)
        self.assertTrue(pres.get("ok"), pres)
        self.assertEqual(pres["kind"], "PARTIAL_EXIT")

        mid = self._portfolio()
        pos1, header1, legs1, outcomes1, trades1 = self._state(pos.id)
        self.assertEqual(pos1.status, "Open")
        self.assertEqual(header1.status, "OPEN")
        self.assertEqual(header1.settlement_revision, 1)
        self.assertEqual(pos1.qty, q0 - 4.0)
        self.assertAlmostEqual(pos1.margin_used, m0 * (q0 - 4.0) / q0,
                               places=6)
        self.assertEqual(outcomes1, [], "a partial produced an outcome")
        self.assertEqual(trades1, [], "a partial produced a PaperTrade")
        self.assertEqual(mid["total"], before["total"],
                         "a partial voted as a trade")
        # The 4 closed contracts paid carry on THEIR notional over 8 hours.
        self.assertAlmostEqual(
            phold.amount_usd,
            4 * header.actual_entry_fill * 0.01 * self.FUNDING_8H, places=9)

        # ── final: the remaining quantity at hour 24 ────────────────────
        ff, ffee, fhold = self._exit_facts(header1, pos1,
                                           filled=pos1.qty,
                                           fill_price=64_800.0, hours=24.0,
                                           expected_revision=1,
                                           exit_reason=CS.VOLUNTARY_EXIT)
        fres = CS.settle_prepared_exit(ff)
        self.assertTrue(fres.get("ok"), fres)
        self.assertEqual(fres["kind"], "FINAL_EXIT")

        after = self._portfolio()
        pos2, header2, legs2, outcomes2, trades2 = self._state(pos.id)
        self.assertEqual(pos2.status, "Closed")
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(header2.settlement_revision, 2)
        exit_legs = [l for l in legs2 if l.kind != "ENTRY"]
        self.assertEqual([l.kind for l in exit_legs],
                         ["PARTIAL_EXIT", "FINAL_EXIT"])
        self.assertEqual([l.settlement_revision for l in exit_legs], [1, 2])
        # §32/§33: the ledger closed the whole position and returned the
        # whole margin.
        self.assertAlmostEqual(sum(l.filled_qty for l in exit_legs), q0,
                               places=9)
        self.assertAlmostEqual(
            sum(l.released_margin_usd for l in exit_legs),
            header.committed_margin_usd, places=6)
        # The 6 remaining paid carry over 24 hours — their own interval.
        self.assertAlmostEqual(
            fhold.amount_usd,
            (q0 - 4) * header.actual_entry_fill * 0.01
            * self.FUNDING_8H * 3, places=9)

        self.assertEqual(len(outcomes2), 1)
        self.assertEqual(len(trades2), 1)
        self.assertEqual(after["total"] - before["total"], 1,
                         "one thesis voted more than once")

        # §34 cash identity, from the persisted ledger. `before` was read
        # AFTER entry, so this window sees the committed margin COME BACK:
        # delta = released(==committed) + gross - exit fees - carry.
        gross_total = sum(l.gross_pnl_usd for l in exit_legs)
        fees_total = sum(l.explicit_fee_usd for l in exit_legs)
        carry_total = sum(l.holding_cost_usd for l in exit_legs)
        net = gross_total - fees_total - carry_total - header.entry_fee_usd
        self.assertAlmostEqual(after["cash"] - before["cash"],
                               header.committed_margin_usd
                               + gross_total - fees_total - carry_total,
                               places=6)
        self.assertAlmostEqual(after["realized"] - before["realized"], net,
                               places=6)
        self.assertAlmostEqual(outcomes2[0].net_pnl_usd, net, places=6)

    def test_three_partials_close_the_book_exactly(self):
        pos, header = self._enter()
        q0 = pos.qty
        self.assertGreater(q0, 5 + 2 + 3 - 1)
        plan = [2.0, 3.0, q0 - 5.0]
        revs = []
        for i, chunk in enumerate(plan):
            p, h, *_ = self._state(pos.id)[:2]
            facts, _, _ = self._exit_facts(h, p, filled=chunk,
                                           fill_price=64_700.0 + i * 10,
                                           hours=8.0 * (i + 1),
                                           expected_revision=i,
                                           exit_reason=(CS.SCALE_OUT
                                                        if i < 2 else
                                                        CS.VOLUNTARY_EXIT))
            res = CS.settle_prepared_exit(facts)
            self.assertTrue(res.get("ok"), res)
            revs.append(res["revision"])
        self.assertEqual(revs, [1, 2, 3])

        pos2, header2, legs, outcomes, trades = self._state(pos.id)
        exit_legs = [l for l in legs if l.kind != "ENTRY"]
        self.assertEqual([l.kind for l in exit_legs],
                         ["PARTIAL_EXIT", "PARTIAL_EXIT", "FINAL_EXIT"])
        self.assertAlmostEqual(sum(l.filled_qty for l in exit_legs), q0)
        self.assertAlmostEqual(sum(l.released_margin_usd for l in exit_legs),
                               header.committed_margin_usd, places=6)
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(trades), 1)
        # Weighted exit VWAP, from the ledger.
        vwap = (sum(l.fill_price * l.filled_qty for l in exit_legs)
                / sum(l.filled_qty for l in exit_legs))
        self.assertAlmostEqual(outcomes[0].actual_exit_fill, vwap, places=9)
        # Ledger gross is the authority, not VWAP arithmetic.
        self.assertAlmostEqual(outcomes[0].gross_pnl_usd,
                               sum(l.gross_pnl_usd for l in exit_legs),
                               places=9)

    def test_a_second_partial_is_not_blocked_by_scaled_out(self):
        """§30 — the ledger, not the legacy boolean, is the authority."""
        from app.database import PaperPosition, get_db
        pos, header = self._enter()
        f1, _, _ = self._exit_facts(header, pos, filled=2.0,
                                    fill_price=64_700.0,
                                    exit_reason=CS.SCALE_OUT)
        self.assertTrue(CS.settle_prepared_exit(f1).get("ok"))
        with get_db() as db:
            db.query(PaperPosition).filter(
                PaperPosition.id == pos.id).update({"scaled_out": True})
            db.commit()
        p1, h1, *_ = self._state(pos.id)[:2]
        f2, _, _ = self._exit_facts(h1, p1, filled=2.0,
                                    fill_price=64_710.0,
                                    expected_revision=1,
                                    exit_reason=CS.SCALE_OUT)
        res = CS.settle_prepared_exit(f2)
        self.assertTrue(res.get("ok"),
                        f"scaled_out blocked a canonical partial: {res}")


class ConcurrencyTests(_B2AHarness):
    """§51-53 — retries and races cannot settle twice."""

    def test_the_same_execution_id_is_idempotent(self):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       execution_id="exit-idem-1",
                                       exit_reason=CS.SCALE_OUT)
        first = CS.settle_prepared_exit(facts)
        self.assertTrue(first.get("ok"), first)
        before = self._portfolio()
        second = CS.settle_prepared_exit(facts)
        self.assertTrue(second.get("ok"))
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(second["result"], CS.IDEMPOTENT_ALREADY_SETTLED)
        after = self._portfolio()
        self.assertEqual(before, after, "an idempotent retry moved money")
        _, header2, legs, _, _ = self._state(pos.id)
        self.assertEqual(header2.settlement_revision, 1)
        self.assertEqual(len([l for l in legs if l.kind != "ENTRY"]), 1)

    def test_a_stale_revision_settles_nothing(self):
        pos, header = self._enter()
        a, _, _ = self._exit_facts(header, pos, filled=2.0,
                                   fill_price=64_700.0,
                                   expected_revision=0,
                                   exit_reason=CS.SCALE_OUT)
        b, _, _ = self._exit_facts(header, pos, filled=3.0,
                                   fill_price=64_710.0,
                                   expected_revision=0,
                                   exit_reason=CS.SCALE_OUT)
        self.assertTrue(CS.settle_prepared_exit(a).get("ok"))
        before = self._portfolio()
        res = CS.settle_prepared_exit(b)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], CS.STALE_SETTLEMENT_REVISION)
        self.assertEqual(self._portfolio(), before)
        _, header2, legs, _, _ = self._state(pos.id)
        self.assertEqual(header2.settlement_revision, 1)
        self.assertEqual(len([l for l in legs if l.kind != "ENTRY"]), 1)

    def test_competing_finalizations_produce_one_outcome(self):
        pos, header = self._enter()
        a, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                   fill_price=64_800.0, expected_revision=0)
        b, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                   fill_price=64_805.0, expected_revision=0)
        first = CS.settle_prepared_exit(a)
        self.assertTrue(first.get("ok"), first)
        before = self._portfolio()
        second = CS.settle_prepared_exit(b)
        self.assertFalse(second.get("ok"))
        self.assertIn(second["error"], (CS.STALE_SETTLEMENT_REVISION,
                                        CS.NOT_CANONICAL_SETTLEMENT_POSITION))
        self.assertEqual(self._portfolio(), before)
        _, _, _, outcomes, trades = self._state(pos.id)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(trades), 1)

    def test_the_outcome_table_enforces_one_per_position(self):
        from sqlalchemy.exc import IntegrityError
        from app.database import PaperRealizedOutcome, get_db
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                       fill_price=64_800.0)
        self.assertTrue(CS.settle_prepared_exit(facts).get("ok"))
        with self.assertRaises(IntegrityError):
            with get_db() as db:
                db.add(PaperRealizedOutcome(
                    position_id=pos.id, venue=BP.KRAKEN_US_VENUE,
                    product=PR.CRYPTO_PERP, instrument_id=PERP_SYM,
                    symbol="BTC/USD", side="long", quantity=1.0,
                    quantity_unit="CONTRACTS", multiplier=0.01,
                    actual_entry_fill=1.0, actual_exit_fill=1.0,
                    gross_pnl_usd=0.0, net_pnl_usd=0.0, outcome="BREAKEVEN",
                    closed_at="t", outcome_version="x"))


class RefusalTests(_B2AHarness):
    """Prepared facts that contradict the ledger — or themselves — move
    nothing."""

    def _refused(self, facts, error=None):
        before = self._portfolio()
        res = CS.settle_prepared_exit(facts)
        self.assertFalse(res.get("ok"), res)
        if error:
            self.assertEqual(res["error"], error)
        self.assertEqual(self._portfolio(), before,
                         "a refused exit moved money")
        return res

    def test_a_long_exit_by_buying_is_refused(self):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       execution_side="buy",
                                       exit_reason=CS.SCALE_OUT)
        self._refused(facts, CS.EXIT_VALIDATION_FAILED)

    def test_a_fractional_contract_fill_is_refused_not_rounded(self):
        pos, header = self._enter()
        with self.assertRaises(CS.ExitValidationError):
            # The fee authority itself refuses a fractional executed count,
            # so the facts cannot even be built — which is the point.
            self._exit_facts(header, pos, filled=2.4, fill_price=64_700.0)

    def test_an_overfill_of_remaining_quantity_is_refused(self):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=pos.qty + 1,
                                       fill_price=64_700.0)
        self._refused(facts, CS.EXIT_VALIDATION_FAILED)

    def test_a_wrong_instrument_is_refused(self):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       instrument_id="PETHUCZ50",
                                       exit_reason=CS.SCALE_OUT)
        self._refused(facts, CS.EXIT_VALIDATION_FAILED)

    def test_a_coins_exit_against_a_contracts_header_is_refused(self):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       quantity_unit="COINS", multiplier=1.0,
                                       exit_reason=CS.SCALE_OUT)
        self._refused(facts, CS.EXIT_VALIDATION_FAILED)

    def test_an_unavailable_holding_cost_cannot_become_facts(self):
        pos, header = self._enter()
        broken = HC.HoldingCostQuote(ok=False, amount_usd=None,
                                     quality=HC.UNAVAILABLE)
        fee = FA.leg_fee("BTC/USD", notional=2 * 64_700.0 * 0.01,
                         price=64_700.0, product=PR.CRYPTO_PERP,
                         venue=BP.KRAKEN_US_VENUE, maker=False,
                         exact_contract_count=2.0,
                         execution_instrument=_pbtc(),
                         actual_fill_price=64_700.0)
        with self.assertRaises(CS.ExitValidationError):
            CS.exit_facts(
                position_id=header.position_id, expected_revision=0,
                execution_id="exit-hc", symbol=header.symbol,
                product=header.product, venue=header.venue,
                instrument_id=header.instrument_id,
                position_side="long", execution_side="sell",
                requested_qty=2.0, filled_qty=2.0,
                quantity_unit="CONTRACTS", multiplier=0.01,
                fill_price=64_700.0, fee_quote=fee, holding_quote=broken,
                settled_at="2026-08-18T20:00:00+00:00")

    def test_a_legacy_position_is_refused_by_name(self):
        from lib.paper_engine import open_paper_position
        res = open_paper_position(_signal(product="CRYPTO_SPOT"),
                                  current_price=100.0)
        self.assertTrue(res.get("ok"), res)
        fee = FA.leg_fee("BTC/USD", notional=101.0, price=101.0,
                         product="CRYPTO_SPOT", venue="kraken", maker=False)
        hold = HC.holding_cost("BTC/USD", product="CRYPTO_SPOT",
                               notional_usd=200.0, hours_held=1.0,
                               is_short=False)
        facts = CS.exit_facts(
            position_id=res["position"]["id"], expected_revision=0,
            execution_id="exit-legacy", symbol="BTC/USD",
            product="CRYPTO_SPOT", venue="kraken",
            instrument_id="BTC/USD", position_side="long",
            execution_side="sell", requested_qty=1.0, filled_qty=1.0,
            quantity_unit="COINS", multiplier=1.0, fill_price=101.0,
            fee_quote=fee, holding_quote=hold,
            settled_at="2026-08-18T20:00:00+00:00")
        self._refused(facts, CS.NOT_CANONICAL_SETTLEMENT_POSITION)

    def test_a_hybrid_header_is_refused_not_downgraded(self):
        from app.database import PaperPositionSettlement, get_db
        pos, header = self._enter()
        with get_db() as db:
            db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == pos.id).update(
                {"cost_model": "legacy_round_trip_v1"})
            db.commit()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       exit_reason=CS.SCALE_OUT)
        self._refused(facts, CS.NOT_CANONICAL_SETTLEMENT_POSITION)

    def test_evidence_only_settles_nothing(self):
        import os
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=2.0,
                                       fill_price=64_700.0,
                                       exit_reason=CS.SCALE_OUT)
        before = self._portfolio()
        os.environ["JARVIS_RUNTIME_MODE"] = "EVIDENCE_ONLY"
        try:
            with self.assertRaises(Exception):
                CS.settle_prepared_exit(facts)
        finally:
            os.environ.pop("JARVIS_RUNTIME_MODE", None)
        self.assertEqual(self._portfolio(), before)
        _, header2, legs, outcomes, _ = self._state(pos.id)
        self.assertEqual(header2.settlement_revision, 0)
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])


class RollbackMatrixTests(_B2AHarness):
    """§54 — the PROPERTY: no economic mutation survives a failure at any
    stage of the final settlement."""

    def _assert_final_unwinds(self, inject):
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                       fill_price=64_800.0)
        before = self._portfolio()
        with self.assertRaises(Exception):
            inject(lambda: CS.settle_prepared_exit(facts))
        self.assertEqual(self._portfolio(), before,
                         "a failed final settlement moved money")
        pos2, header2, legs, outcomes, trades = self._state(pos.id)
        self.assertEqual(pos2.status, "Open")
        self.assertEqual(pos2.qty, pos.qty)
        self.assertEqual(pos2.margin_used, pos.margin_used)
        self.assertEqual(header2.status, "OPEN")
        self.assertEqual(header2.settlement_revision, 0)
        self.assertEqual([l.kind for l in legs], ["ENTRY"])
        self.assertEqual(outcomes, [])
        self.assertEqual(trades, [])

    def test_failure_building_the_outcome_unwinds_everything(self):
        import lib.realized_outcome as RO

        def run(settle):
            with patch.object(RO, "build_from_settlement",
                              side_effect=RuntimeError("injected")):
                return settle()
        self._assert_final_unwinds(run)

    def test_failure_constructing_the_outcome_row_unwinds_everything(self):
        import app.database as DBM

        def run(settle):
            with patch.object(DBM, "PaperRealizedOutcome",
                              side_effect=RuntimeError("injected")):
                return settle()
        self._assert_final_unwinds(run)

    def test_failure_constructing_the_trade_row_unwinds_everything(self):
        import app.database as DBM

        def run(settle):
            with patch.object(DBM, "PaperTrade",
                              side_effect=RuntimeError("injected")):
                return settle()
        self._assert_final_unwinds(run)

    def test_failure_at_commit_unwinds_everything(self):
        from sqlalchemy import event
        from app.database import SessionLocal

        def boom(session):
            raise RuntimeError("injected: before_commit")

        def run(settle):
            event.listen(SessionLocal, "before_commit", boom)
            try:
                return settle()
            finally:
                event.remove(SessionLocal, "before_commit", boom)
        self._assert_final_unwinds(run)


class FrozenFactsTests(_B2AHarness):
    """§57 — settlement consults NOTHING that produces facts; poison it all
    and the prepared facts still settle. Then the control."""

    def test_settlement_survives_a_poisoned_world(self):
        from lib import execution_policy as POL
        from lib import execution_venue as EV
        from lib import risk_engine as RE
        from lib import transaction_costs as TC
        from lib import virtual_orders as VO

        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                       fill_price=64_800.0)

        def explode(*a, **k):
            raise AssertionError("settlement consulted a fact producer")
        with patch.object(FA, "leg_fee", explode), \
             patch.object(RE, "solve_position", explode), \
             patch.object(INST, "resolve_for_execution", explode), \
             patch.object(PR, "route", explode), \
             patch.object(POL, "execution_readiness", explode), \
             patch.object(EV, "submit", explode), \
             patch.object(VO, "execute_market", explode), \
             patch.object(TC, "_latest_funding_rate", explode):
            res = CS.settle_prepared_exit(facts)
        self.assertTrue(res.get("ok"), res)
        _, header2, _, outcomes, _ = self._state(pos.id)
        self.assertEqual(header2.status, "CLOSED")
        self.assertEqual(len(outcomes), 1)

    def test_the_control_the_poison_kills_preparation(self):
        """The same poison must bite the layer that legitimately prices —
        without this, the silence above proves nothing."""
        def explode(*a, **k):
            raise AssertionError("leg_fee poison reached")
        with patch.object(FA, "leg_fee", explode):
            with self.assertRaises(AssertionError):
                FA.leg_fee("BTC/USD", notional=1.0, price=1.0,
                           product=PR.CRYPTO_PERP)


class QueryPlanTests(_B2AHarness):
    """§60 — B2B's lookups are index walks."""

    def test_the_lookups_use_indexes(self):
        from sqlalchemy import text
        from app.database import engine
        pos, header = self._enter()
        facts, _, _ = self._exit_facts(header, pos, filled=pos.qty,
                                       fill_price=64_800.0)
        self.assertTrue(CS.settle_prepared_exit(facts).get("ok"))

        queries = {
            "outcome by position": ("SELECT * FROM paper_realized_outcomes "
                                    "WHERE position_id = :v"),
            "header by position": ("SELECT * FROM paper_position_settlements "
                                   "WHERE position_id = :v"),
            "leg by execution": ("SELECT * FROM paper_settlement_legs "
                                 "WHERE execution_id = :v"),
            "ordered legs": ("SELECT * FROM paper_settlement_legs "
                             "WHERE position_id = :v "
                             "ORDER BY settlement_revision"),
        }
        with engine.connect() as conn:
            for name, q in queries.items():
                plan = " | ".join(str(r) for r in conn.execute(
                    text("EXPLAIN QUERY PLAN " + q), {"v": "x"}))
                self.assertIn("USING INDEX", plan.upper(),
                              f"{name}: {plan}")
                self.assertNotIn("SCAN paper_", plan, f"{name}: {plan}")
        # The composite index also spares the sort for revision ordering.
        with engine.connect() as conn:
            plan = " | ".join(str(r) for r in conn.execute(
                text("EXPLAIN QUERY PLAN " + queries["ordered legs"]),
                {"v": "x"}))
        self.assertNotIn("TEMP B-TREE", plan.upper(),
                         f"revision ordering sorts instead of walking the "
                         f"composite index: {plan}")


class StructuralDefenseTests(unittest.TestCase):
    """§57 structural half — secondary to the poison proof above."""

    def _calls(self):
        import ast
        import pathlib
        tree = ast.parse((pathlib.Path(__file__).parent.parent / "lib"
                          / "canonical_settlement.py").read_text(
            encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
        return names

    def test_the_settlement_core_produces_no_facts(self):
        calls = self._calls()
        for forbidden in ("leg_fee", "solve_position", "holding_cost",
                          "execution_readiness", "submit", "execute_market",
                          "resolve_for_execution", "route",
                          "record_trade_outcome", "funding_cost_pct",
                          "_latest_funding_rate"):
            self.assertNotIn(forbidden, calls,
                             f"settlement core calls {forbidden}")


if __name__ == "__main__":
    unittest.main()
