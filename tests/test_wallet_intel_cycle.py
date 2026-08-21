"""The completed wallet-intelligence cycle, and what it must never conclude.

THREE COMPONENTS EXISTED AND NOTHING CALLED THEM. `wallet_swaps` decoded
full transactions, `wallet_scoring` measured wallets and `wallet_alpha`
measured post-entry moves — each complete, each tested, each reachable only
from the disabled legacy scheduler. This file pins the behaviour of the one
bounded pathway that now calls them, and pins hardest on the conclusions
that would be WRONG rather than merely missing.

THE FOUR THAT MATTER MOST, because full-transaction evidence is the only
thing that can see any of them and the transfers feed cannot:

  a FAILED transaction is not a trade      — the feed reports the attempted
                                             movements and no error
  a token OUT with no consideration        — the wallet sent something away
    is not a sale                            and was paid nothing
  a token IN with no payment                — an airdrop looks exactly like
    is not a purchase                        a buy from transfer rows alone
  MULTIPLE LEGS ARE NOT MULTIPLE VOTES     — and neither is one signature
                                             classified twice, which is what
                                             a reclassification would do
                                             without supersession
"""
import ast
import pathlib
import unittest

from lib import wallet_event_classifier as C
from lib import wallet_intel_cycle as CY
from lib import wallet_price_snapshots as P
from lib import wallet_scoring as S
from lib import wallet_shadow_intel as SI
from lib import wallet_swap_enrichment as E
from lib import wallet_swaps as W

ROOT = pathlib.Path(__file__).parent.parent

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
NATIVE = C.NATIVE_SOL_PSEUDO_MINT
TOKEN = "Hgm7RLGKPCexampleTokenMint1111111111111111x"
OTHER = "DpphPNJUs8exampleTokenMint2222222222222222y"
WALLET = "7xKvAaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsT"

T0 = 1_787_000_000.0


def leg(mint, direction, amount, *, sig="sig-1", cp=None, symbol=None):
    return C.TransferLeg(signature=sig, mint=mint, direction=direction,
                         amount=amount, counterparty=cp,
                         watched_wallet=WALLET, symbol=symbol,
                         block_time=T0, observed_ts=T0,
                         parser_version="helius_v1_transfers_v1")


def enrichment(**kw):
    base = {"signature": "sig-1", "wallet_address": WALLET,
            "state": E.ENRICHED, "kind": W.BUY, "reason": "test",
            "base_mint": TOKEN, "base_amount": 1000.0,
            "quote_mint": USDC, "quote_amount": 500.0,
            "notional_usd": 500.0, "entry_price_usd": 0.5,
            "tx_success": True, "refusal_reason": None,
            "evidence_quality": E.BALANCE_DELTA_EVIDENCE, "block_time": T0}
    base.update(kw)
    return lambda sig: base


def src(path):
    return (ROOT / path).read_text(encoding="utf-8")


# ── C, D, E: what full-transaction evidence must refuse ──────────────────
class TestNotATrade(unittest.TestCase):
    """The three conclusions the transfers feed cannot avoid on its own."""

    def test_c_failed_transaction_cannot_become_a_trade(self):
        """A reverted swap moved nothing. The legs describe an ATTEMPT."""
        # The decoder refuses it at the source...
        tx = {"meta": {"err": {"InstructionError": [0, "Custom"]}, "fee": 5000},
              "transaction": {"signatures": ["sig-1"], "message": {}}}
        row = W.normalize_swap(tx, WALLET)
        self.assertEqual(row["kind"], W.NOT_A_TRADE)
        self.assertIn("failed", (row.get("reason") or "").lower())

        # ...and the classifier reports it as a non-trading event, not as a
        # buy and not as a silent absence.
        legs = [leg(USDC, "out", 500.0), leg(TOKEN, "in", 1000.0)]
        ev = C.classify_group(legs, enrichment_lookup=enrichment(
            state=E.REFUSED_NON_TRADING, kind=W.NOT_A_TRADE, tx_success=False,
            reason="transaction failed on chain"))
        self.assertEqual(ev.event_type, C.FAILED_TRANSACTION)
        self.assertEqual(ev.classification, C.CLASSIFIED_NON_TRADING_EVENT)
        self.assertFalse(ev.is_trading_event)
        self.assertIn(C.FAILED_TRANSACTION, C.NON_TRADING_EVENT_TYPES)

    def test_d_token_out_without_consideration_is_not_a_sell(self):
        """Value left and nothing came back. That is a transfer."""
        v = W.classify({TOKEN: -1000.0})
        self.assertEqual(v["kind"], W.NOT_A_TRADE)
        self.assertIn("nothing came back", v["reason"])

        ev = C.classify_group(
            [leg(TOKEN, "out", 1000.0)],
            enrichment_lookup=enrichment(state=E.REFUSED_NON_TRADING,
                                         kind=W.NOT_A_TRADE,
                                         reason=v["reason"]))
        self.assertNotEqual(ev.event_type, C.TOKEN_SELL)
        self.assertFalse(ev.is_trading_event)

    def test_e_token_in_without_payment_is_not_a_buy(self):
        """An airdrop is indistinguishable from a purchase in transfer rows."""
        v = W.classify({TOKEN: 1000.0})
        self.assertEqual(v["kind"], W.NOT_A_TRADE)
        self.assertIn("nothing was paid", v["reason"])

        ev = C.classify_group(
            [leg(TOKEN, "in", 1000.0)],
            enrichment_lookup=enrichment(state=E.REFUSED_NON_TRADING,
                                         kind=W.NOT_A_TRADE,
                                         reason=v["reason"]))
        self.assertNotEqual(ev.event_type, C.TOKEN_BUY)
        self.assertFalse(ev.is_trading_event)

    def test_lp_operation_is_not_a_swap(self):
        """Two assets in for one out is a liquidity add wearing a buy."""
        v = W.classify({USDC: -500.0, "SOL": -2.0, TOKEN: 1000.0})
        self.assertEqual(v["kind"], W.NOT_A_TRADE)
        ev = C.classify_group(
            [leg(TOKEN, "in", 1000.0)],
            enrichment_lookup=enrichment(state=E.REFUSED_NON_TRADING,
                                         kind=W.NOT_A_TRADE,
                                         reason=v["reason"]))
        self.assertEqual(ev.event_type, C.LIQUIDITY_ADD)
        self.assertFalse(ev.is_trading_event)


# ── B, F, G, H, R: identity and subject selection ────────────────────────
class TestEvidenceUpgrade(unittest.TestCase):

    def test_b_enrichment_upgrades_an_unknown_transfer_to_a_trade(self):
        """The population this whole phase exists to move."""
        legs = [leg(TOKEN, "in", 1000.0)]
        before = C.classify_group(legs)
        self.assertEqual(before.event_type, C.UNKNOWN_TRANSFER)

        after = C.classify_group(legs, enrichment_lookup=enrichment())
        self.assertEqual(after.event_type, C.TOKEN_BUY)
        self.assertEqual(after.classification, C.CLASSIFIED_TRADING_EVENT)
        self.assertEqual(after.evidence_quality, C.BALANCE_DELTA_EVIDENCE)
        self.assertEqual(after.subject_mint, TOKEN)
        self.assertEqual(after.direction, "BUY")

    def test_balance_delta_outranks_every_transfer_reading(self):
        self.assertEqual(C.EVIDENCE_RANK[0], C.BALANCE_DELTA_EVIDENCE)
        self.assertLess(C.EVIDENCE_RANK.index(C.BALANCE_DELTA_EVIDENCE),
                        C.EVIDENCE_RANK.index(C.PAIRED_SWAP_LEGS))

    def test_f_native_sol_and_wsol_stay_distinct(self):
        """Measured: they differ in exactly ONE character, the last.

        Same length, same 42-character prefix. Any identity test that
        compares a prefix, or that trusts `symbol == "SOL"`, silently reads
        one as the other — and native SOL is 70% of every leg in the store,
        so getting it wrong inverts the most common event in the data.
        """
        self.assertNotEqual(NATIVE, WSOL)
        self.assertEqual(len(NATIVE), len(WSOL))
        self.assertEqual(NATIVE[:-1], WSOL[:-1])
        self.assertEqual((NATIVE[-1], WSOL[-1]), ("1", "2"))
        # Both price a position; neither IS one.
        self.assertTrue(C.is_quote_asset(NATIVE))
        self.assertTrue(C.is_quote_asset(WSOL))
        # Identity is by exact membership, never by prefix.
        quotes = C._quote_mints()
        self.assertIn(NATIVE, quotes)
        self.assertIn(WSOL, quotes)
        self.assertFalse(C.is_quote_asset(NATIVE[:-1] + "3"))

    def test_r_the_swap_ledger_sentinel_never_becomes_a_mint(self):
        """`wallet_swaps` folds WSOL onto the literal string "SOL".

        That fold is right for netting a wallet's economics and wrong as an
        identity: putting "SOL" in a mint column is a ticker pretending to
        be a mint, which is the confusion the mint-only rule exists to stop.
        """
        self.assertEqual(W.NATIVE_SOL, "SOL")
        self.assertEqual(C.canonical_subject_mint(W.NATIVE_SOL), NATIVE)
        self.assertEqual(C.canonical_subject_mint(TOKEN), TOKEN)
        self.assertIsNone(C.canonical_subject_mint(None))

        ev = C.classify_group([leg(TOKEN, "in", 1.0)],
                              enrichment_lookup=enrichment(
                                  quote_mint=W.NATIVE_SOL))
        self.assertEqual(ev.quote_mint, NATIVE)
        self.assertNotEqual(ev.quote_mint, "SOL")
        self.assertNotEqual(ev.subject_mint, "SOL")
        # And the merge is DISCLOSED rather than hidden.
        self.assertIn("WSOL", ev.reason)

    def test_a_quote_asset_subject_is_refused_not_traded(self):
        """SOL is what a position is priced in, not a position."""
        ev = C.classify_group([leg(NATIVE, "in", 5.0)],
                              enrichment_lookup=enrichment(
                                  base_mint=W.NATIVE_SOL, quote_mint=USDC))
        self.assertEqual(ev.subject_mint, NATIVE)
        verdict = SI.evaluate([ev], wallet_quality={"measurable": True})
        self.assertEqual(verdict["state"], SI.STATE_REFUSED)
        self.assertEqual(verdict["refusal_reason"], SI.UNSUPPORTED_ASSET)

    def test_g_alphabetical_leg_order_cannot_select_the_subject(self):
        """1,879 buys against 10 sells was alphabetical order, not a market."""
        # OTHER sorts before TOKEN; TOKEN is the larger economic leg.
        legs = [leg(OTHER, "in", 1.0), leg(TOKEN, "in", 5000.0),
                leg(USDC, "out", 900.0)]
        ev = C.classify_group(legs)
        self.assertEqual(ev.subject_mint, TOKEN)
        # Reversing the input order cannot change the answer.
        self.assertEqual(C.classify_group(list(reversed(legs))).subject_mint,
                         TOKEN)

    def test_h_multiple_legs_produce_one_economic_event(self):
        """A routed swap is one action however many transfers carry it."""
        legs = [leg(TOKEN, "in", 1000.0, sig="s"),
                leg(USDC, "out", 500.0, sig="s"),
                leg(NATIVE, "out", 0.001, sig="s"),
                leg(NATIVE, "out", 0.002, sig="s")]
        events = C.classify_all(legs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].leg_count, 4)

    def test_h_enriched_multi_leg_is_still_one_event(self):
        legs = [leg(TOKEN, "in", 1000.0, sig="s"),
                leg(USDC, "out", 500.0, sig="s"),
                leg(NATIVE, "out", 0.003, sig="s")]
        events = C.classify_all(legs, enrichment_lookup=enrichment())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].leg_count, 3)
        self.assertEqual(events[0].evidence_quality, C.BALANCE_DELTA_EVIDENCE)

    def test_enrichment_with_nothing_useful_falls_back(self):
        """An upgrade that cannot answer must not DESTROY the transfer read."""
        legs = [leg(TOKEN, "in", 1000.0), leg(USDC, "out", 500.0)]
        ev = C.classify_group(legs, enrichment_lookup=lambda s: None)
        self.assertEqual(ev.event_type, C.TOKEN_BUY)
        self.assertEqual(ev.evidence_quality, C.PAIRED_SWAP_LEGS)

    def test_partial_enrichment_stays_partial(self):
        """A real swap with no priceable leg is direction without dollars."""
        ev = C.classify_group([leg(TOKEN, "in", 1.0)],
                              enrichment_lookup=enrichment(
                                  state=E.PARTIAL, kind=W.TOKEN_TOKEN,
                                  notional_usd=None,
                                  reason="token-to-token swap"))
        self.assertEqual(ev.classification, C.PARTIAL_EVIDENCE)
        self.assertFalse(ev.is_trading_event)


# ── I, J: bounded, idempotent enrichment ─────────────────────────────────
class TestEnrichmentBudgets(unittest.TestCase):

    def test_i_enrichment_identity_is_signature_and_wallet(self):
        from app.database import WalletSwapEnrichment as M

        uniques = [c for c in M.__table__.constraints
                   if c.__class__.__name__ == "UniqueConstraint"]
        cols = {tuple(sorted(c.name for c in u.columns)) for u in uniques}
        self.assertIn(("signature", "wallet_address"), cols)

    def test_i_a_settled_signature_is_never_bought_twice(self):
        """ENRICHED and REFUSED are both ANSWERS. Re-reading learns nothing."""
        for state in (E.ENRICHED, E.REFUSED_NON_TRADING, E.PARTIAL,
                      E.PERMANENTLY_UNRESOLVED):
            self.assertIn(state, E.TERMINAL_STATES)
        # PENDING and a retryable failure are NOT settled.
        self.assertNotIn(E.RETRYABLE_FAILURE, E.TERMINAL_STATES)
        self.assertNotIn(E.PENDING, E.TERMINAL_STATES)

    def test_j_failures_are_bounded_and_back_off(self):
        self.assertGreaterEqual(E.MAX_ATTEMPTS, 1)
        self.assertLessEqual(E.MAX_ATTEMPTS, 5)
        self.assertTrue(all(b > 0 for b in E.RETRY_BACKOFF_S))
        self.assertEqual(sorted(E.RETRY_BACKOFF_S), list(E.RETRY_BACKOFF_S),
                         "backoff must not shrink with each failure")

    def test_j_budgets_are_explicit_and_finite(self):
        self.assertGreater(E.max_signatures_per_cycle(), 0)
        self.assertGreater(E.max_provider_calls_per_cycle(), 0)
        self.assertLessEqual(E.max_signatures_per_cycle(), 500)
        self.assertGreater(E.MIN_SPACING_S, 0)
        self.assertGreater(E.MAX_AGE_SECONDS, 0)

    def test_j_a_zero_budget_spends_nothing(self):
        def boom(*a, **k):                      # pragma: no cover
            raise AssertionError("provider must not be called")

        out = E.enrich_pending(limit=0, max_calls=0, rpc_fn=boom)
        self.assertEqual(out["provider_calls"], 0)
        self.assertEqual(out["attempted"], 0)

    def test_j_a_provider_failure_never_raises(self):
        """One unreadable transaction must not stop the cycle behind it."""
        def boom(*a, **k):
            raise RuntimeError("provider down")

        out = E.enrich_pending(limit=1, max_calls=1, rpc_fn=boom)
        self.assertIsInstance(out, dict)
        self.assertIn("failures", out)


# ── K, L, M, N: scoring that runs, and refuses ───────────────────────────
class TestScoring(unittest.TestCase):

    def test_one_scoring_implementation_not_two(self):
        """A second wallet score would mean two things called a score."""
        body = src("lib/wallet_scoring.py")
        self.assertIn("def _score_one(", body)
        # Both entry points must delegate to it.
        tree = ast.parse(body)
        for name in ("score_registry_wallets", "score_wallets"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {c.func.id for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            self.assertIn("_score_one", calls,
                          f"{name} must reuse the one scoring implementation")

    def test_k_the_targeted_pass_selects_by_address(self):
        out = S.score_wallets([], limit=5)
        self.assertEqual(out["requested"], 0)
        self.assertEqual(out["attempted"], 0)

    def test_k_only_changed_evidence_is_rescored(self):
        """Not all 1,086 registry rows every fifteen minutes."""
        body = src("lib/wallet_intel_cycle.py")
        self.assertIn("wallet_swap_enrichment", body)
        self.assertIn("updated_at >= :s", body)
        self.assertIn("MAX_WALLETS_RESCORED", body)
        self.assertLessEqual(CY.MAX_WALLETS_RESCORED, 50)

    def test_l_insufficient_evidence_stays_insufficient(self):
        """Three round trips is not eight, and must not round up."""
        rec = {"trades": [{"pnl_usd": 1.0, "return_pct": 5.0,
                           "notional_usd": 100.0, "cost_basis_usd": 100.0,
                           "price_quality": "MEASURED", "hold_seconds": 60}
                          for _ in range(3)],
               "closed": 3, "still_open": 0, "unpriced_legs": 0}
        out = S.score_wallet(rec)
        self.assertFalse(out["measurable"])
        self.assertLess(out["trades_scored"], S.MIN_TRADES_FOR_SCORE)

    def test_l_no_evidence_is_never_a_neutral_score(self):
        out = S.score_wallet({"trades": [], "closed": 0, "still_open": 0,
                              "unpriced_legs": 0})
        self.assertFalse(out["measurable"])
        self.assertIsNone(out.get("smart_money_score"))

    def test_l_an_unscored_wallet_is_unknown_not_neutral(self):
        q = SI.wallet_quality_snapshot(["nobody-has-ever-seen-this-address"])
        self.assertFalse(q["measurable"])
        self.assertEqual(q["known"], 0)
        self.assertEqual(q["wallets"][0]["quality"], "UNKNOWN")

    def test_m_bootstrap_does_not_require_a_jarvis_thesis(self):
        """The circular failure, closed by construction.

        A wallet is scored from ITS OWN economic events. If a JARVIS thesis
        were required for that evidence, no wallet could ever become scored
        and no thesis could ever be justified.
        """
        self.assertEqual(S.SCORE_BOOTSTRAP_POPULATION,
                         "OBSERVED_WALLET_ECONOMIC_EVENTS")
        # The two populations must not READ each other. Naming the other
        # one in a comment is the whole point of the comment, so this tests
        # for access — a query, an import or a model reference.
        for path in ("lib/wallet_alpha.py", "lib/wallet_scoring.py"):
            body = src(path)
            code = "\n".join(ln for ln in body.splitlines()
                             if not ln.lstrip().startswith("#"))
            for token in ("WalletShadowEvent", "WalletShadowOutcome",
                          "FROM wallet_shadow", "wallet_shadow_intel"):
                self.assertNotIn(token, code,
                                 f"{path} must not read JARVIS thesis "
                                 f"outcomes to score a wallet")

    def test_m_alpha_needs_a_proven_entry_not_a_holding(self):
        """"Owns 500,000 of this token" says nothing about when or at what."""
        from lib import wallet_alpha as A

        self.assertEqual(A.ALPHA_ELIGIBLE_CLASSES, frozenset({A.VERIFIED_BUY_ENTRY}))
        self.assertFalse(A.is_alpha_eligible(A.HOLDER_SNAPSHOT))
        self.assertFalse(A.is_alpha_eligible(A.POOL_TX_SIGNER))
        self.assertFalse(A.is_alpha_eligible(A.PARTICIPANT_SIGHTING))
        self.assertTrue(A.is_alpha_eligible(A.VERIFIED_BUY_ENTRY))

    def test_n_wallet_quality_is_read_point_in_time(self):
        """Today's score is not what was known when the event happened."""
        q = SI.wallet_quality_snapshot([])
        self.assertIn("point-in-time", q["note"])
        self.assertIn("never neutral", q["note"])
        # The values are read INSIDE the session, so a rescore earlier in
        # the same cycle cannot detach them and fail the whole pass.
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("READ EVERY VALUE INSIDE THE SESSION", body)


# ── O, P, Q, S: prices ───────────────────────────────────────────────────
class TestPrices(unittest.TestCase):

    def test_o_priority_order_is_the_policy(self):
        """A due checkpoint has a deadline; background coverage does not."""
        self.assertEqual(P.PRIORITY_ORDER[0], P.DUE_CHECKPOINT)
        self.assertEqual(P.PRIORITY_ORDER[1], P.NEW_EVENT_REFERENCE)
        self.assertEqual(P.PRIORITY_ORDER[-1], P.BACKGROUND_COVERAGE)

    def test_o_collection_is_bounded(self):
        self.assertGreater(P.max_calls_per_cycle(), 0)
        self.assertLessEqual(P.MINTS_PER_CALL, 30)
        out = P.collect(mints=[], max_calls=0)
        self.assertEqual(out["provider_calls"], 0)

    def test_o_a_quote_asset_is_never_requested_as_a_position(self):
        body = src("lib/wallet_price_snapshots.py")
        self.assertIn("if C.is_quote_asset(mint):", body)

    def test_p_missing_price_stays_missing(self):
        ctx = SI.market_context("a-mint-nobody-has-priced-ever", T0)
        self.assertEqual(ctx["state"], SI.NO_PRICE)
        self.assertIsNone(ctx["price_usd"])
        self.assertNotEqual(ctx["price_usd"], 0)

    def test_p_no_mint_is_not_a_price_problem(self):
        ctx = SI.market_context(None, T0)
        self.assertEqual(ctx["state"], SI.UNKNOWN_TOKEN_IDENTITY)

    def test_q_stale_price_is_refused_by_name(self):
        ev = C.classify_group([leg(TOKEN, "in", 1000.0),
                               leg(USDC, "out", 500.0)])
        stale = {"state": SI.STALE_PRICE, "price_usd": 1.0,
                 "price_age_seconds": 99_999.0, "liquidity_usd": 10 ** 9}
        v = SI.evaluate([ev], wallet_quality={"measurable": True}, ctx=stale)
        self.assertEqual(v["refusal_reason"], SI.STALE_PRICE)

    def test_s_event_time_price_is_not_todays_price(self):
        """A snapshot outside the window is reported, never reused."""
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("Today's price is NOT a substitute for the price then",
                      body)
        self.assertGreater(SI.PRICE_MAX_AGE_SECONDS, 0)

    def test_s_a_recorded_reference_price_outlives_its_snapshot(self):
        """Pruning the snapshot store must not demote a standing thesis."""
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("THE RECORD OF A PRICE OUTLIVES THE SNAPSHOT", body)
        self.assertIn("reference_price_preserved", body)

    def test_quote_series_staleness_is_not_relaxed(self):
        """The gap that made every wallet unscoreable — and its guard."""
        from lib import quote_valuation as Q

        self.assertEqual(Q.MAX_BAR_DISTANCE_HOURS, 6)
        state = P.quote_series_state()
        self.assertIn("series", state)
        for s in state["series"]:
            if s.get("age_hours") is not None:
                self.assertEqual(
                    s["state"],
                    "FRESH" if s["age_hours"] <= Q.MAX_BAR_DISTANCE_HOURS
                    else "STALE")

    def test_a_stale_quote_still_refuses_to_value_a_trade(self):
        from lib import quote_valuation as Q

        v = Q.value_in_usd(3.0, "SOL", 1_000_000.0)   # far outside any bar
        self.assertIsNone(v["usd_value"])
        self.assertEqual(v["price_quality"], Q.UNAVAILABLE)


# ── T, U, V, W, X, Y, Z: theses and outcomes ─────────────────────────────
class TestTheses(unittest.TestCase):

    def test_v_eligible_evidence_creates_exactly_one_thesis(self):
        ev = C.classify_group([leg(TOKEN, "in", 1000.0),
                               leg(USDC, "out", 500.0)])
        ctx = {"state": "FRESH", "price_usd": 1.0, "price_age_seconds": 10.0,
               "liquidity_usd": 10 ** 9}
        v = SI.evaluate([ev], wallet_quality={"measurable": True}, ctx=ctx)
        self.assertEqual(v["state"], SI.STATE_ELIGIBLE)
        self.assertIsNone(v["refusal_reason"])
        self.assertEqual(len(SI.cluster([ev])), 1)

    def test_w_every_refusal_is_named(self):
        ev = C.classify_group([leg(TOKEN, "in", 1000.0),
                               leg(USDC, "out", 500.0)])
        v = SI.evaluate([ev], wallet_quality={"measurable": False,
                                              "max_sample_count": 0,
                                              "unknown": 1})
        self.assertEqual(v["state"], SI.STATE_REFUSED)
        self.assertIn(v["refusal_reason"], SI.REFUSAL_REASONS)
        self.assertTrue(v["reason"])

    def test_w_unknown_quality_and_insufficient_history_are_different(self):
        ev = C.classify_group([leg(TOKEN, "in", 1000.0),
                               leg(USDC, "out", 500.0)])
        never = SI.evaluate([ev], wallet_quality={"measurable": False,
                                                  "max_sample_count": 0})
        some = SI.evaluate([ev], wallet_quality={"measurable": False,
                                                 "max_sample_count": 3})
        self.assertEqual(never["refusal_reason"], SI.UNKNOWN_WALLET_QUALITY)
        self.assertEqual(some["refusal_reason"], SI.INSUFFICIENT_WALLET_HISTORY)

    def test_x_reprocessing_is_idempotent_on_the_cluster(self):
        ev = C.classify_group([leg(TOKEN, "in", 1000.0),
                               leg(USDC, "out", 500.0)])
        self.assertEqual(SI.cluster_key(ev), SI.cluster_key(ev))
        # The key is an ABSOLUTE time bucket, so processing a narrower leg
        # window cannot re-split a cluster.
        self.assertIn("//", src("lib/wallet_shadow_intel.py"))

        # ONE ROW PER CLUSTER, enforced by the database rather than by
        # remembering to check first.
        from app.database import WalletShadowEvent as M

        unique_cols = {tuple(sorted(c.name for c in u.columns))
                       for u in M.__table__.constraints
                       if u.__class__.__name__ == "UniqueConstraint"}
        self.assertIn(("cluster_id",), unique_cols)

    def test_y_reclassification_supersedes_rather_than_double_votes(self):
        """The cluster key hashes the event TYPE.

        So correcting a classification necessarily mints a NEW cluster id.
        Without supersession the corrected observation would be ADDED to the
        uncorrected one and the desk would count both — a double vote
        produced by the one pass whose purpose is to correct.
        """
        legs = [leg(TOKEN, "in", 1000.0)]
        before = C.classify_group(legs)
        after = C.classify_group(legs, enrichment_lookup=enrichment())
        self.assertNotEqual(SI.cluster_key(before), SI.cluster_key(after))

        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("def _supersede_moved(", body)
        self.assertIn("MULTIPLE LEGS ARE NOT MULTIPLE VOTES", body)
        self.assertEqual(SI.SUPERSEDED, "SUPERSEDED")

    def test_y_superseded_rows_are_excluded_from_every_read(self):
        """A retired reading must not keep voting."""
        self.assertIn("revision_state", SI.CURRENT_ONLY)
        body = src("lib/wallet_shadow_intel.py")
        # Every aggregate in performance() filters on it.
        perf = body[body.index("def performance("):]
        for frag in ("SELECT state, COUNT(*)", "SELECT refusal_reason",
                     "SELECT event_type", "SELECT classification"):
            i = perf.index(frag)
            self.assertIn("CURRENT_ONLY", perf[i:i + 400],
                          f"{frag} must exclude superseded rows")
        router = src("app/routers/onchain.py")
        self.assertIn("def _current(model):", router)

    def test_y_a_superseded_observation_retires_its_checkpoints(self):
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("UPDATE wallet_shadow_outcomes SET status=:ex", body)

    def test_t_each_horizon_resolves_independently(self):
        self.assertEqual(sorted(SI.HORIZONS),
                         sorted(SI.HORIZON_TOLERANCE_SECONDS))
        # A tighter horizon gets a tighter tolerance.
        self.assertLess(SI.HORIZON_TOLERANCE_SECONDS["15m"],
                        SI.HORIZON_TOLERANCE_SECONDS["24h"])

    def test_u_unresolved_is_never_a_loss(self):
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("UNRESOLVED", body)
        self.assertNotIn("net_return_pct=0", body)
        # A checkpoint with no qualifying price keeps its status.
        resolve = body[body.index("def resolve_outcomes("):]
        self.assertIn("reason = NO_PRICE", resolve.replace('"', ""))
        self.assertIn("still_unresolved", resolve)

    def test_z_no_net_edge_is_claimed_before_costs(self):
        costs = SI.expected_costs({})
        self.assertEqual(costs["quality"], "ASSUMPTION")
        self.assertGreater(costs["round_trip_pct"], 0)
        self.assertIn("not a measurement", costs["note"])
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn("gross_return_pct", body)
        self.assertIn("net_return_pct", body)

    def test_z_an_assumed_cost_is_never_presented_as_measured(self):
        self.assertNotEqual(SI.expected_costs({})["quality"], "MEASURED")

    def test_no_expectancy_below_the_sample_floor(self):
        self.assertGreaterEqual(SI.MIN_SAMPLE_FOR_EXPECTANCY, 20)


# ── A, AA: the cycle runs itself ─────────────────────────────────────────
class TestCycle(unittest.TestCase):

    def test_a_the_poll_runs_the_cycle(self):
        """New observations enter the cycle automatically."""
        body = src("lib/wallet_poller.py")
        self.assertIn("wallet_intel_cycle", body)
        self.assertIn("run_once()", body)
        import inspect

        from lib import wallet_poller

        self.assertIn("run_cycle",
                      inspect.signature(wallet_poller.poll_once).parameters)

    def test_aa_no_manual_step_is_required(self):
        """The page must update without POST /process or a Claude script."""
        body = src("lib/wallet_intel_cycle.py")
        for stage in ("ENRICH_SWAP_EVIDENCE", "PROCESS_SHADOW_EVENTS",
                      "RESOLVE_OUTCOMES", "COLLECT_PRICE_SNAPSHOTS"):
            self.assertIn(stage, body)
        self.assertEqual(len(CY.STAGES), len(set(CY.STAGES)))

    def test_a_cycle_owns_no_timer_and_no_thread(self):
        """DO NOT BUILD ANOTHER SCHEDULER."""
        body = src("lib/wallet_intel_cycle.py")
        self.assertNotIn("Thread(", body)
        self.assertNotIn("add_job", body)
        self.assertNotIn("BackgroundScheduler", body)
        self.assertNotIn("time.sleep", body)
        self.assertEqual(CY.status()["driven_by"], "wallet_poller")

    def test_a_cycle_refuses_to_overlap_itself(self):
        self.assertIn("_RUN_LOCK", src("lib/wallet_intel_cycle.py"))
        self.assertIn("CYCLE_ALREADY_RUNNING", src("lib/wallet_intel_cycle.py"))

    def test_a_one_stage_failing_does_not_stop_the_rest(self):
        body = src("lib/wallet_intel_cycle.py")
        self.assertIn("Its failure is ITS failure", body)
        self.assertIn("CYCLE_PARTIAL", body)

    def test_a_missing_counts_are_none_not_zero(self):
        s = CY.status()
        for key in ("signatures_enriched", "price_snapshots",
                    "theses_created", "outcomes_resolved"):
            self.assertIn(key, s)
        self.assertIn("missing is not zero", s["note"])

    def test_the_cycle_processes_deltas_not_the_whole_history(self):
        self.assertGreater(CY.PROCESS_WINDOW_SECONDS, 0)
        self.assertLessEqual(CY.PROCESS_WINDOW_SECONDS, 14 * 24 * 3600)
        body = src("lib/wallet_intel_cycle.py")
        self.assertIn("since_ts=since", body)


# ── AC, AD, AE-AH: what must stay out of reach ───────────────────────────
class TestIsolation(unittest.TestCase):

    def test_ac_no_full_wallet_address_leaves_the_desk(self):
        self.assertEqual(SI.safe_label("A" * 44), "AAAA…AAAA")
        self.assertNotIn("A" * 44, SI.safe_label("A" * 44))
        body = src("lib/wallet_shadow_intel.py")
        self.assertIn('"wallets_json": _json([safe_label(w)', body)

    def test_ac_the_enrichment_row_is_not_rendered_by_address(self):
        """The address is needed to COMPUTE deltas, never to display them."""
        router = src("app/routers/onchain.py")
        cycle = router[router.index("def onchain_intel_cycle("):
                       router.index("def onchain_intel_cycle_run(")]
        self.assertNotIn("wallet_address", cycle)

    def test_ad_no_execution_path_is_reachable_from_the_cycle(self):
        for path in ("lib/wallet_intel_cycle.py",
                     "lib/wallet_swap_enrichment.py",
                     "lib/wallet_price_snapshots.py"):
            body = src(path)
            for forbidden in ("sendTransaction", "signTransaction",
                              "submit_order", "place_order", "Keypair",
                              "sign(", "broker", "withdraw"):
                self.assertNotIn(forbidden, body,
                                 f"{forbidden} must not appear in {path}")

    def test_ae_ah_the_cycle_writes_no_money_bearing_table(self):
        """Virtual cash, both virtual books and trade_outcomes are untouched.

        Tested as ACCESS, not as vocabulary: `wallet_shadow_intel` states in
        prose that it writes nothing to `trade_outcomes`, and that sentence
        is worth keeping.
        """
        models = ("TradeOutcome", "PaperPosition", "PaperTrade",
                  "DexPosition", "DexBalance", "Portfolio")
        writes = ("INSERT INTO trade_outcomes", "UPDATE trade_outcomes",
                  "INSERT INTO paper_", "UPDATE paper_",
                  "INSERT INTO dex_", "UPDATE dex_")
        for path in ("lib/wallet_intel_cycle.py",
                     "lib/wallet_swap_enrichment.py",
                     "lib/wallet_price_snapshots.py",
                     "lib/wallet_shadow_intel.py"):
            body = src(path)
            code = "\n".join(ln for ln in body.splitlines()
                             if not ln.lstrip().startswith("#"))
            for model in models:
                self.assertNotIn(model, code,
                                 f"{path} must not touch {model}")
            for stmt in writes:
                self.assertNotIn(stmt, body,
                                 f"{path} must not write via {stmt!r}")

    def test_ah_shadow_evidence_carries_its_own_source(self):
        self.assertEqual(SI.SOURCE, "HELIUS_WALLET_INTELLIGENCE")
        self.assertEqual(SI.EXECUTION_MODE, "SHADOW")

    def test_ai_the_cycle_does_not_consult_the_legacy_scheduler(self):
        body = src("lib/wallet_intel_cycle.py")
        self.assertNotIn("JARVIS_DISABLE_SCHEDULER", body)
        self.assertNotIn("app.scheduler", body)

    def test_aj_polling_and_the_cycle_are_separately_controlled(self):
        from lib import wallet_poller

        self.assertEqual(wallet_poller.POLLING_ENABLED_ENV,
                         "JARVIS_HELIUS_WALLET_POLLING_ENABLED")
        self.assertEqual(CY.ENABLED_ENV, "JARVIS_WALLET_INTEL_CYCLE_ENABLED")
        self.assertNotEqual(CY.ENABLED_ENV, wallet_poller.POLLING_ENABLED_ENV)


# ── AB: the disclaimer ───────────────────────────────────────────────────
class TestDesk(unittest.TestCase):

    def test_ab_every_shadow_pick_carries_the_no_order_disclaimer(self):
        page = src("frontend/src/lib/sections/OnChain.svelte")
        self.assertIn("SHADOW INTELLIGENCE — NO ORDER SUBMITTED", page)

    def test_the_four_required_panels_exist(self):
        page = src("frontend/src/lib/sections/OnChain.svelte")
        for title in ("Intelligence Cycle", "Swap Evidence",
                      "Wallet-Score Coverage", "Price Coverage"):
            self.assertIn(f'title="{title}"', page)

    def test_the_desk_reads_one_route_for_all_four(self):
        page = src("frontend/src/lib/sections/OnChain.svelte")
        self.assertIn("/onchain/intel/cycle", page)

    def test_missing_renders_as_a_dash_never_as_zero(self):
        page = src("frontend/src/lib/sections/OnChain.svelte")
        for field in ("c.signatures_enriched", "c.price_snapshots",
                      "c.theses_created", "c.outcomes_resolved",
                      "pc.pending_mints", "s3.pending_candidates"):
            self.assertIn(f'{{{field} ?? "—"}}', page)

    def test_the_manual_trigger_is_labelled_diagnostic(self):
        page = src("frontend/src/lib/sections/OnChain.svelte")
        self.assertIn("diagnostic", page.lower())
        router = src("app/routers/onchain.py")
        self.assertIn("DIAGNOSTIC ONLY", router)


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
