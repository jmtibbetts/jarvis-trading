"""Wallet observations -> classified events -> shadow theses -> outcomes.

THE TWO THINGS THIS FILE EXISTS TO STOP.

A TRANSFER IS NOT A TRADE. The Helius transfers feed carries no program and
no instruction, so a lone token movement cannot be shown to be a purchase.
Calling it one would put a fabricated trade into a performance table, and a
fabricated trade is indistinguishable from a real one once it is there.

MULTIPLE LEGS ARE NOT MULTIPLE VOTES. A routed swap arrives as seven or
eight transfers and several watched wallets may act on the same token in the
same minute. Counting any of that separately turns one market event into a
crowd of agreeing witnesses.

The native-SOL case is pinned hardest, because it was a real defect: Helius
reports native SOL under a 43-character pseudo-mint one character from
WSOL's, it is 70% of every leg in the store, and treating it as a tradeable
token made the classifier read ordinary SOL-for-token swaps backwards.
"""
import ast
import pathlib
import unittest

from lib import wallet_event_classifier as C
from lib import wallet_shadow_intel as SI

ROOT = pathlib.Path(__file__).parent.parent

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
NATIVE = C.NATIVE_SOL_PSEUDO_MINT
TOKEN = "Hgm7RLGKPCexampleTokenMint1111111111111111x"
OTHER = "DpphPNJUs8exampleTokenMint2222222222222222y"
WALLET = "7xKvAaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsT"
EXCHANGE = "9zExchangeAddress111111111111111111111111111"

T0 = 1_787_000_000.0


def leg(mint, direction, amount, *, sig="sig-1", cp=None, wallet=WALLET,
        symbol=None, t=T0):
    return C.TransferLeg(signature=sig, mint=mint, direction=direction,
                         amount=amount, counterparty=cp, watched_wallet=wallet,
                         symbol=symbol, block_time=t, observed_ts=t,
                         parser_version="helius_v1_transfers_v1")


def no_entities(_addr):
    return None


# ── A/B/D. What a swap is, and what a transfer is not ────────────────────
class ClassificationTests(unittest.TestCase):

    def test_an_authoritative_swap_becomes_one_trading_event(self):
        """A — SOL out, token in, one signature: that IS a buy."""
        legs = [leg(NATIVE, "out", 2.0, symbol="SOL"),
                leg(TOKEN, "in", 1_000_000.0)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.TOKEN_BUY)
        self.assertEqual(e.classification, C.CLASSIFIED_TRADING_EVENT)
        self.assertEqual(e.evidence_quality, C.PAIRED_SWAP_LEGS)
        self.assertEqual(e.direction, "BUY")
        self.assertEqual(e.subject_mint, TOKEN)
        self.assertEqual(e.quote_symbol, "SOL")

    def test_the_reverse_is_a_sell(self):
        legs = [leg(TOKEN, "out", 1_000_000.0), leg(USDC, "in", 900.0)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.TOKEN_SELL)
        self.assertEqual(e.direction, "SELL")
        self.assertEqual(e.subject_mint, TOKEN)
        self.assertEqual(e.quote_symbol, "USDC")

    def test_an_ordinary_transfer_does_not_become_a_trade(self):
        """B — one leg, unknown counterparty. Nothing establishes a trade."""
        e = C.classify_group([leg(TOKEN, "in", 500.0)],
                             entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.UNKNOWN_TRANSFER)
        self.assertEqual(e.classification, C.UNKNOWN)
        self.assertFalse(e.is_trading_event)
        self.assertIn("no program", e.reason)

    def test_native_sol_is_a_quote_asset_not_a_traded_token(self):
        """THE DEFECT. Helius reports native SOL as a 43-char pseudo-mint
        one character from WSOL. Omitting it made SOL the SUBJECT of every
        SOL-for-token swap and inverted the direction of 70% of the feed."""
        self.assertNotEqual(NATIVE, WSOL)
        self.assertEqual(len(NATIVE), len(WSOL))
        for m in (NATIVE, WSOL, "SOL", USDC):
            self.assertTrue(C.is_quote_asset(m), m)

        legs = [leg(NATIVE, "out", 5.0, symbol="SOL"),
                leg(TOKEN, "in", 9_000.0)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.subject_mint, TOKEN,
                         "native SOL was treated as the traded token")
        self.assertEqual(e.quote_mint, NATIVE)

    def test_a_token_for_token_swap_claims_no_direction(self):
        legs = [leg(TOKEN, "out", 10.0), leg(OTHER, "in", 20.0)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.classification, C.PARTIAL_EVIDENCE)
        self.assertFalse(e.is_trading_event)

    def test_the_largest_leg_is_the_subject_not_the_first(self):
        """A routed swap has fee and wrapping legs. Taking `legs[0]` picked
        whichever mint sorted first — it skewed the real data 1,879 buys to
        10 sells, which is alphabetical order, not a market."""
        legs = [leg(NATIVE, "out", 0.000005, symbol="SOL"),   # gas dust
                leg(NATIVE, "out", 4.0, symbol="SOL"),        # the payment
                leg("AAAAtinyMint111111111111111111111111111", "in", 0.01),
                leg(TOKEN, "in", 5_000_000.0)]                # the position
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.subject_mint, TOKEN)
        self.assertAlmostEqual(e.quote_amount, 4.0)

    def test_mint_identity_survives_a_null_symbol(self):
        """D — the feed reports null symbols for exactly the SPL tokens that
        matter, so a ticker can never be the identity."""
        legs = [leg(NATIVE, "out", 1.0, symbol="SOL"),
                leg(TOKEN, "in", 100.0, symbol=None)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.subject_mint, TOKEN)
        self.assertIsNone(e.subject_symbol)


# ── C. Non-trading activity is excluded, by name ─────────────────────────
class NonTradingExclusionTests(unittest.TestCase):

    def test_a_self_transfer_is_not_a_trade(self):
        e = C.classify_group([leg(TOKEN, "out", 10.0, cp=WALLET)],
                             entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.SELF_TRANSFER)
        self.assertEqual(e.classification, C.CLASSIFIED_NON_TRADING_EVENT)

    def test_exchange_custody_movements_are_classified_apart(self):
        def entities(addr):
            return ({"entity_type": "EXCHANGE", "entity_name": "SomeCEX",
                     "is_protocol": True, "is_trader": False}
                    if addr == EXCHANGE else None)

        out = C.classify_group([leg(TOKEN, "out", 10.0, cp=EXCHANGE)],
                               entity_lookup=entities)
        self.assertEqual(out.event_type, C.EXCHANGE_DEPOSIT)
        inn = C.classify_group([leg(TOKEN, "in", 10.0, cp=EXCHANGE)],
                               entity_lookup=entities)
        self.assertEqual(inn.event_type, C.EXCHANGE_WITHDRAWAL)
        for e in (out, inn):
            self.assertEqual(e.classification, C.CLASSIFIED_NON_TRADING_EVENT)
            self.assertFalse(e.is_trading_event)

    def test_bridge_staking_and_liquidity_get_their_own_types(self):
        for etype, direction, expected in (
                ("BRIDGE", "out", C.BRIDGE_TRANSFER),
                ("STAKING", "out", C.STAKING_DEPOSIT),
                ("STAKING", "in", C.STAKING_WITHDRAWAL),
                ("AMM", "out", C.LIQUIDITY_ADD),
                ("AMM", "in", C.LIQUIDITY_REMOVE)):
            e = C.classify_group(
                [leg(TOKEN, direction, 10.0, cp=EXCHANGE)],
                entity_lookup=lambda a, t=etype: {"entity_type": t,
                                                  "entity_name": None,
                                                  "is_protocol": True,
                                                  "is_trader": False})
            self.assertEqual(e.event_type, expected, f"{etype}/{direction}")
            self.assertFalse(e.is_trading_event)

    def test_dust_is_not_a_position(self):
        e = C.classify_group([leg(TOKEN, "in", 1e-15)],
                             entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.DUST_OR_SPAM)

    def test_a_lone_stablecoin_leg_is_treasury_movement(self):
        e = C.classify_group([leg(USDC, "in", 5_000.0)],
                             entity_lookup=no_entities)
        self.assertEqual(e.event_type, C.STABLECOIN_INFLOW)
        self.assertFalse(e.is_trading_event)

    def test_many_legs_one_direction_claims_nothing(self):
        """904 real signatures move a quote asset OUT and a token OUT with
        nothing coming back. No consideration was observed, so no sale."""
        legs = [leg(NATIVE, "out", 0.01, symbol="SOL"),
                leg(TOKEN, "out", 1_000.0)]
        e = C.classify_group(legs, entity_lookup=no_entities)
        self.assertEqual(e.classification, C.PARTIAL_EVIDENCE)
        self.assertFalse(e.is_trading_event)
        self.assertIn("NO consideration", e.reason)


# ── H/I. One observation, however many legs and wallets ──────────────────
class SuppressionTests(unittest.TestCase):

    def test_one_transaction_with_many_legs_is_one_event(self):
        """H — eight legs, one signature, one economic event."""
        legs = [leg(NATIVE, "out", 1.0, sig="S1", symbol="SOL"),
                leg(NATIVE, "out", 0.00001, sig="S1", symbol="SOL"),
                leg(TOKEN, "in", 100.0, sig="S1"),
                leg(TOKEN, "in", 5.0, sig="S1")]
        events = C.classify_all(legs, entity_lookup=no_entities)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].leg_count, 4)

    def test_copying_wallets_are_one_market_observation(self):
        """I — five wallets buying the same token in the same window are one
        piece of evidence about that token, not five."""
        events = []
        for i in range(5):
            events += C.classify_all(
                [leg(NATIVE, "out", 1.0, sig=f"S{i}", wallet=f"W{i}",
                     symbol="SOL"),
                 leg(TOKEN, "in", 100.0, sig=f"S{i}", wallet=f"W{i}")],
                entity_lookup=no_entities)
        self.assertEqual(len(events), 5)
        clusters = SI.cluster(events)
        self.assertEqual(len(clusters), 1,
                         "five copying wallets became five votes")
        self.assertEqual(len(clusters[0][1]), 5)

    def test_the_same_action_a_day_apart_stays_two_observations(self):
        a = C.classify_group(
            [leg(NATIVE, "out", 1.0, sig="A", symbol="SOL", t=T0),
             leg(TOKEN, "in", 100.0, sig="A", t=T0)],
            entity_lookup=no_entities)
        b = C.classify_group(
            [leg(NATIVE, "out", 1.0, sig="B", symbol="SOL", t=T0 + 86_400),
             leg(TOKEN, "in", 100.0, sig="B", t=T0 + 86_400)],
            entity_lookup=no_entities)
        self.assertEqual(len(SI.cluster([a, b])), 2)

    def test_opposite_directions_never_merge(self):
        buy = C.classify_group([leg(NATIVE, "out", 1.0, sig="A", symbol="SOL"),
                                leg(TOKEN, "in", 100.0, sig="A")],
                               entity_lookup=no_entities)
        sell = C.classify_group([leg(TOKEN, "out", 100.0, sig="B"),
                                 leg(USDC, "in", 90.0, sig="B")],
                                entity_lookup=no_entities)
        self.assertEqual(len(SI.cluster([buy, sell])), 2)


# ── E/F/G/J/Q. The gate ──────────────────────────────────────────────────
class EligibilityTests(unittest.TestCase):

    def _buy(self):
        return C.classify_group(
            [leg(NATIVE, "out", 5.0, symbol="SOL"),
             leg(TOKEN, "in", 1_000_000.0)], entity_lookup=no_entities)

    GOOD_WQ = {"measurable": True, "known": 1, "unknown": 0,
               "best_score": 82.0, "max_sample_count": 40}
    GOOD_CTX = {"state": "FRESH", "price_usd": 0.01,
                "price_age_seconds": 30.0, "liquidity_usd": 250_000.0,
                "price_source": "token_activity_snapshot",
                "price_at": "2026-08-19T09:00:00+00:00"}

    def test_an_eligible_event_becomes_a_shadow_thesis(self):
        """J — and ONLY a shadow thesis."""
        v = SI.evaluate([self._buy()], wallet_quality=self.GOOD_WQ,
                        ctx=self.GOOD_CTX)
        self.assertEqual(v["state"], SI.STATE_ELIGIBLE)
        self.assertIsNone(v["refusal_reason"])
        self.assertGreater(v["notional_usd"], SI.MIN_NOTIONAL_USD)
        self.assertEqual(SI.EXECUTION_MODE, "SHADOW")
        self.assertEqual(SI.SOURCE, "HELIUS_WALLET_INTELLIGENCE")

    def test_unknown_wallet_quality_refuses_rather_than_defaulting(self):
        """F — an unproven wallet is not a neutral one."""
        v = SI.evaluate([self._buy()],
                        wallet_quality={"measurable": False, "unknown": 1,
                                        "known": 0, "max_sample_count": 0},
                        ctx=self.GOOD_CTX)
        self.assertEqual(v["state"], SI.STATE_REFUSED)
        self.assertEqual(v["refusal_reason"], SI.UNKNOWN_WALLET_QUALITY)

    def test_a_thin_sample_refuses_with_its_own_reason(self):
        v = SI.evaluate([self._buy()],
                        wallet_quality={"measurable": False, "unknown": 1,
                                        "known": 0, "max_sample_count": 3},
                        ctx=self.GOOD_CTX)
        self.assertEqual(v["refusal_reason"], SI.INSUFFICIENT_WALLET_HISTORY)

    def test_a_stale_price_is_refused_not_used(self):
        """G — a price from an hour away describes a different moment."""
        ctx = {**self.GOOD_CTX, "state": SI.STALE_PRICE,
               "price_age_seconds": 99_999.0}
        v = SI.evaluate([self._buy()], wallet_quality=self.GOOD_WQ, ctx=ctx)
        self.assertEqual(v["refusal_reason"], SI.STALE_PRICE)

    def test_a_missing_price_never_becomes_zero(self):
        ctx = {**self.GOOD_CTX, "state": SI.NO_PRICE, "price_usd": None}
        v = SI.evaluate([self._buy()], wallet_quality=self.GOOD_WQ, ctx=ctx)
        self.assertEqual(v["refusal_reason"], SI.NO_PRICE)

    def test_thin_liquidity_refuses(self):
        ctx = {**self.GOOD_CTX, "liquidity_usd": 100.0}
        v = SI.evaluate([self._buy()], wallet_quality=self.GOOD_WQ, ctx=ctx)
        self.assertEqual(v["refusal_reason"], SI.LOW_LIQUIDITY)

    def test_an_economically_trivial_position_refuses(self):
        """Q — size is checked before anything is called evidence."""
        ctx = {**self.GOOD_CTX, "price_usd": 1e-12}
        v = SI.evaluate([self._buy()], wallet_quality=self.GOOD_WQ, ctx=ctx)
        self.assertEqual(v["refusal_reason"], SI.BELOW_ECONOMIC_SIZE)

    def test_a_non_trading_event_refuses_before_anything_else(self):
        e = C.classify_group([leg(USDC, "in", 5_000.0)],
                             entity_lookup=no_entities)
        v = SI.evaluate([e], wallet_quality=self.GOOD_WQ, ctx=self.GOOD_CTX)
        self.assertEqual(v["refusal_reason"], SI.NON_TRADING_TRANSFER)

    def test_a_quote_asset_can_never_be_the_subject(self):
        e = self._buy()
        e.subject_mint = USDC
        v = SI.evaluate([e], wallet_quality=self.GOOD_WQ, ctx=self.GOOD_CTX)
        self.assertEqual(v["refusal_reason"], SI.UNSUPPORTED_ASSET)

    def test_every_refusal_reason_is_in_the_declared_vocabulary(self):
        for r in (SI.UNKNOWN_WALLET_QUALITY, SI.STALE_PRICE, SI.NO_PRICE,
                  SI.LOW_LIQUIDITY, SI.BELOW_ECONOMIC_SIZE,
                  SI.NON_TRADING_TRANSFER, SI.UNSUPPORTED_ASSET,
                  SI.INSUFFICIENT_WALLET_HISTORY,
                  SI.PARTIAL_TRANSACTION_EVIDENCE, SI.UNKNOWN_EVENT_TYPE):
            self.assertIn(r, SI.REFUSAL_REASONS)


# ── E. Point-in-time wallet quality ──────────────────────────────────────
class PointInTimeTests(unittest.TestCase):

    def test_an_unscored_wallet_is_unknown_never_neutral(self):
        snap = SI.wallet_quality_snapshot(["NoSuchWalletAddress1111111111"])
        self.assertFalse(snap["measurable"])
        self.assertEqual(snap["known"], 0)
        self.assertEqual(snap["wallets"][0]["quality"], "UNKNOWN")
        self.assertIsNone(snap["best_score"])

    def test_the_snapshot_carries_its_own_version_and_time(self):
        snap = SI.wallet_quality_snapshot([])
        for k in ("score_version", "score_at", "as_of"):
            self.assertIn(k, snap)

    def test_a_wallet_label_never_exposes_the_address(self):
        addr = "7xKvAaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsT"
        label = SI.safe_label(addr)
        self.assertNotIn(addr, label)
        self.assertLess(len(label), 12)
        self.assertEqual(SI.safe_label(None), "UNKNOWN")


# ── K/W. Execution isolation ─────────────────────────────────────────────
class ExecutionIsolationTests(unittest.TestCase):

    FORBIDDEN = ("lib.paper_engine", "lib.dex_paper", "lib.dex_wallet",
                 "lib.canonical_entry", "lib.canonical_exit",
                 "lib.execution_venue", "lib.virtual_orders",
                 "lib.canonical_settlement", "lib.settlement_ledger",
                 "lib.alpaca_client", "lib.kraken_account",
                 "jobs.execute_signals", "jobs.paper_trading")
    FORBIDDEN_NAMES = ("submit_order", "place_order", "open_paper_position",
                       "close_paper_position", "sendTransaction",
                       "signTransaction", "Keypair", "execute_market",
                       "fund_wallet")

    def _imports(self, rel):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name)
        return found

    def test_neither_module_reaches_an_execution_surface(self):
        for rel in ("lib/wallet_event_classifier.py",
                    "lib/wallet_shadow_intel.py"):
            imported = self._imports(rel)
            src = (ROOT / rel).read_text(encoding="utf-8")
            for mod in self.FORBIDDEN:
                self.assertNotIn(mod, imported, f"{rel} imports {mod}")
            for name in self.FORBIDDEN_NAMES:
                self.assertNotIn(name, src, f"{rel} mentions {name}")

    def test_the_execution_mode_is_shadow_and_only_shadow(self):
        self.assertEqual(SI.EXECUTION_MODE, "SHADOW")
        from lib import execution_mode as EM
        self.assertNotIn(SI.SOURCE, EM.MODES)


# ── N/O/P/R/S/T. Idempotence, attribution, isolation ─────────────────────
class PersistenceTests(unittest.TestCase):

    def _economy(self):
        from app.database import (DexBalance, DexFundingEvent, DexPortfolio,
                                  DexPosition, DexTrade, PaperPortfolio,
                                  PaperPosition, PaperTrade, get_db)
        with get_db() as db:
            pf = db.query(PaperPortfolio).first()
            return {"cash": pf.cash if pf else None,
                    "realized_pnl": pf.realized_pnl if pf else None,
                    "paper_positions": db.query(PaperPosition).count(),
                    "paper_trades": db.query(PaperTrade).count(),
                    "dex_balances": db.query(DexBalance).count(),
                    "dex_funding": db.query(DexFundingEvent).count(),
                    "dex_portfolio": db.query(DexPortfolio).count(),
                    "dex_positions": db.query(DexPosition).count(),
                    "dex_trades": db.query(DexTrade).count()}

    def _seed_observations(self, n_signatures: int = 6) -> int:
        """Write synthetic Helius transfers into the TEST event store.

        Without this the idempotence check runs against an empty store and
        proves nothing — a pass that processes zero rows is trivially
        repeatable. These are the only synthetic rows this suite creates and
        they never touch the operator's store: `conftest` redirects the
        event path, and `app.database` refuses the operator DB under pytest.
        """
        from lib.event_store import get_store
        from lib.market_events import OnChainEvent, event_to_dict, make_meta

        rows = []
        for i in range(n_signatures):
            sig = f"seed-sig-{i}"
            # One routed swap: SOL out, token in, plus a gas-dust leg.
            for mint, direction, amount, sym in (
                    (NATIVE, "out", 3.0 + i, "SOL"),
                    (NATIVE, "out", 0.000005, "SOL"),
                    (TOKEN, "in", 1_000_000.0 + i, None)):
                r = event_to_dict(OnChainEvent(
                    meta=make_meta("helius", "helius_v1_transfers_v1",
                                   T0 + i * 3600),
                    symbol=sym or mint,
                    metric=f"wallet_transfer_{direction}",
                    value=amount, chain="solana",
                    dedup_key=f"helius:{sig}:{mint}:{EXCHANGE}:{direction}"))
                r["watched_wallet"] = WALLET
                r["mint"] = mint
                r["counterparty"] = EXCHANGE
                rows.append(r)
        return get_store().append(rows)

    def test_processing_is_idempotent_and_moves_no_virtual_money(self):
        """N + R/S/T — repeating the pass casts no second observation."""
        from sqlalchemy import text

        from app.database import engine

        seeded = self._seed_observations()
        self.assertGreater(seeded, 0, "the seed wrote nothing to process")

        before = self._economy()
        first = SI.process(limit=500)
        second = SI.process(limit=500)

        self.assertGreater(first["clusters"], 0,
                           "nothing was processed — this proves nothing")
        self.assertGreater(first["inserted"], 0)
        self.assertEqual(second["inserted"], 0,
                         "a repeated pass inserted new observations")
        self.assertEqual(second["updated"], first["clusters"])
        self.assertEqual(first["clusters"], second["clusters"])
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM wallet_shadow_events")).fetchone()[0]
            dupes = conn.execute(text(
                "SELECT COUNT(*) FROM (SELECT cluster_id FROM "
                "wallet_shadow_events GROUP BY cluster_id HAVING COUNT(*) > 1)"
            )).fetchone()[0]
        self.assertEqual(dupes, 0, "a cluster voted twice")
        self.assertEqual(n, first["clusters"] or n)
        self.assertEqual(self._economy(), before,
                         "shadow processing moved the virtual economy")

    def test_every_persisted_row_is_attributed_to_this_source(self):
        """O/P — and therefore cannot be pooled with anything else."""
        from sqlalchemy import text

        from app.database import engine

        SI.process(limit=200)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT source, execution_mode "
                "FROM wallet_shadow_events")).fetchall()
        for source, mode in rows:
            self.assertEqual(source, "HELIUS_WALLET_INTELLIGENCE")
            self.assertEqual(mode, "SHADOW")

    def test_shadow_rows_never_reach_the_canonical_learning_table(self):
        """P — separate table, separate population, no filter to forget."""
        from sqlalchemy import text

        from app.database import engine

        SI.process(limit=200)
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM trade_outcomes WHERE outcome_source "
                "= 'HELIUS_WALLET_INTELLIGENCE'")).fetchone()[0]
        self.assertEqual(n, 0)

    def test_performance_states_no_expectancy_below_the_sample_floor(self):
        """Q — a number from four observations looks like one from four
        hundred, and only one of them means anything."""
        SI.process(limit=200)
        perf = SI.performance()
        self.assertEqual(perf["source"], "HELIUS_WALLET_INTELLIGENCE")
        self.assertEqual(perf["execution_mode"], "SHADOW")
        self.assertGreaterEqual(perf["min_sample_for_expectancy"], 20)
        for _h, cell in perf["horizons"].items():
            if not cell["sample_sufficient"]:
                self.assertIn("expectancy_note", cell)

    def test_costs_are_carried_beside_every_return(self):
        perf = SI.performance()
        self.assertGreater(perf["estimated_round_trip_cost_pct"], 0)
        for _h, cell in perf["horizons"].items():
            self.assertIn("gross_return_pct", cell)
            self.assertIn("net_return_pct", cell)


# ── L/M. Forward outcomes ────────────────────────────────────────────────
class OutcomeTests(unittest.TestCase):

    def test_the_declared_horizons_and_tolerances_exist(self):
        for h in ("15m", "1h", "4h", "24h", "7d"):
            self.assertIn(h, SI.HORIZONS)
            self.assertIn(h, SI.HORIZON_TOLERANCE_SECONDS)

    def test_a_checkpoint_with_no_qualifying_price_stays_unresolved(self):
        """M — and UNRESOLVED is never a loss."""
        out = SI.resolve_outcomes(limit=50)
        for k in ("examined", "resolved", "still_unresolved", "not_yet_due"):
            self.assertIn(k, out)
        self.assertEqual(SI.UNRESOLVED, "UNRESOLVED")
        self.assertNotEqual(SI.UNRESOLVED, "LOSS")

    def test_a_price_outside_tolerance_does_not_resolve_a_checkpoint(self):
        self.assertLess(SI.HORIZON_TOLERANCE_SECONDS["15m"],
                        SI.HORIZON_TOLERANCE_SECONDS["7d"])
        # A 15m checkpoint may not be answered by a six-hour-old price.
        self.assertLess(SI.HORIZON_TOLERANCE_SECONDS["15m"], 3600)


if __name__ == "__main__":
    unittest.main()
