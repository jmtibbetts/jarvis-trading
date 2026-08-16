"""A transfer is not a trade — proven by balance change, not by pairing legs.

Scoring reconstructed round trips by pairing inbound and outbound transfer
legs inside one signature. That is a plausible-looking heuristic and it is
wrong often enough to poison a win rate: `/v1/transfers` describes value
MOVING, and it cannot tell a swap from a withdrawal, a buy from an airdrop,
or a sell from collateral posted to a lending market. Every one of those
produced a "trade" with a cost basis and a P&L that fed profit factor and
the smart-money score.

The rule here is opposing NET balance movement for the wallet itself. That
also makes multi-hop routes correct without parsing them — intermediate
legs net to zero by construction, so USDC -> WSOL -> BONK reads as "spent
USDC, received BONK", which is what the wallet actually did.
"""
import unittest

from lib.wallet_swaps import (BUY, NATIVE_SOL, NOT_A_TRADE, SELL,
                              TOKEN_TOKEN, WSOL_MINT, classify,
                              normalize_swap, owner_balance_deltas)

OWNER = "Wa11etAddre55"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def tx(*, token_pre=(), token_post=(), sol_pre=1_000_000_000, sol_post=None,
       fee=5000, sig="sig1", ts=1_700_000_000, err=None, owner=OWNER):
    """Build a transaction with the owner at account index 0.

    `sol_post` defaults to `sol_pre - fee`, which is what the chain records
    for a fee payer whose only SOL movement was the fee. Defaulting both to
    zero made the fee add-back manufacture a phantom SOL GAIN, and a
    phantom priceable gain turns every USDC buy into a quote-to-quote
    conversion — a fixture artifact that would have hidden real behaviour.
    """
    if sol_post is None:
        sol_post = sol_pre - fee
    def bal(rows):
        return [{"owner": o, "mint": m,
                 "uiTokenAmount": {"uiAmount": a}} for o, m, a in rows]
    return {
        "transaction": {"signatures": [sig],
                        "message": {"accountKeys": [{"pubkey": owner},
                                                    {"pubkey": "other"}]}},
        "blockTime": ts,
        "meta": {"err": err, "fee": fee,
                 "preBalances": [sol_pre, 0], "postBalances": [sol_post, 0],
                 "preTokenBalances": bal(token_pre),
                 "postTokenBalances": bal(token_post)},
    }


class BalanceDeltaTests(unittest.TestCase):
    def test_only_the_owners_balances_count(self):
        t = tx(token_pre=[(OWNER, USDC, 500), ("someone_else", USDC, 900)],
               token_post=[(OWNER, USDC, 0), ("someone_else", USDC, 1400)])
        d = owner_balance_deltas(t, OWNER)
        self.assertAlmostEqual(d[USDC], -500)

    def test_the_fee_is_not_part_of_the_traded_amount(self):
        """Leaving it in makes every SOL-quoted trade look slightly worse."""
        fee = 5000
        t = tx(sol_pre=1_000_000_000, sol_post=1_000_000_000 - fee, fee=fee)
        self.assertEqual(owner_balance_deltas(t, OWNER), {},
                         "a fee-only transaction has no economic movement")

    def test_wsol_and_native_sol_are_one_asset(self):
        t = tx(token_pre=[(OWNER, WSOL_MINT, 0)],
               token_post=[(OWNER, WSOL_MINT, 2)],
               sol_pre=2_000_000_000, sol_post=0)
        d = owner_balance_deltas(t, OWNER)
        self.assertIn(NATIVE_SOL, d)
        self.assertNotIn(WSOL_MINT, d)


class NotATradeTests(unittest.TestCase):
    """Every pattern named in the audit as must-not-count."""

    def test_a_plain_transfer_out_is_not_a_sell(self):
        d = {USDC: -500.0}
        self.assertEqual(classify(d)["kind"], NOT_A_TRADE)

    def test_an_airdrop_in_is_not_a_buy(self):
        d = {BONK: 1_000_000.0}
        c = classify(d)
        self.assertEqual(c["kind"], NOT_A_TRADE)
        self.assertIn("nothing was paid", c["reason"])

    def test_ata_creation_is_not_a_trade(self):
        self.assertEqual(classify({})["kind"], NOT_A_TRADE)

    def test_wrapping_sol_alone_is_not_a_trade(self):
        t = tx(token_pre=[(OWNER, WSOL_MINT, 0)],
               token_post=[(OWNER, WSOL_MINT, 5)],
               sol_pre=5_000_000_000, sol_post=0)
        self.assertEqual(classify(owner_balance_deltas(t, OWNER))["kind"],
                         NOT_A_TRADE, "SOL and WSOL are the same asset")

    def test_staking_movement_is_not_a_trade(self):
        self.assertEqual(classify({NATIVE_SOL: -50.0})["kind"], NOT_A_TRADE)

    def test_an_lp_deposit_is_not_a_trade(self):
        """Two tokens leave and an LP mint arrives — a position change, and
        the LP mint is not a priceable quote."""
        c = classify({USDC: -500.0, NATIVE_SOL: -3.0, "LPmint": 12.0})
        self.assertIn(c["kind"], (NOT_A_TRADE, TOKEN_TOKEN))
        self.assertNotEqual(c["kind"], BUY)

    def test_a_failed_transaction_is_not_a_trade(self):
        row = normalize_swap(tx(err={"InstructionError": []}), OWNER)
        self.assertEqual(row["kind"], NOT_A_TRADE)
        self.assertIn("failed", row["reason"])

    def test_a_quote_to_quote_conversion_is_not_a_position(self):
        c = classify({USDC: -500.0, NATIVE_SOL: 3.0})
        self.assertEqual(c["kind"], NOT_A_TRADE)
        self.assertIn("conversion", c["reason"])

    def test_every_refusal_states_a_reason(self):
        for d in ({}, {USDC: -1.0}, {BONK: 1.0}):
            c = classify(d)
            self.assertTrue(c.get("reason"),
                            "an unexplained gap reads as a quiet wallet")


class RealSwapTests(unittest.TestCase):
    def test_a_buy_is_recognised(self):
        c = classify({USDC: -500.0, BONK: 1_000_000.0})
        self.assertEqual(c["kind"], BUY)
        self.assertEqual(c["quote_mint"], USDC)
        self.assertEqual(c["base_mint"], BONK)
        self.assertAlmostEqual(c["quote_amount"], 500.0)

    def test_a_sell_is_recognised(self):
        c = classify({BONK: -1_000_000.0, USDC: 650.0})
        self.assertEqual(c["kind"], SELL)
        self.assertEqual(c["base_mint"], BONK)
        self.assertAlmostEqual(c["quote_amount"], 650.0)

    def test_a_multi_hop_route_nets_to_its_economics(self):
        """USDC -> WSOL -> BONK. The intermediate leg cancels, so no route
        parsing is needed to get the right answer."""
        t = tx(token_pre=[(OWNER, USDC, 500), (OWNER, WSOL_MINT, 0),
                          (OWNER, BONK, 0)],
               token_post=[(OWNER, USDC, 0), (OWNER, WSOL_MINT, 0),
                           (OWNER, BONK, 1_000_000)])
        c = classify(owner_balance_deltas(t, OWNER))
        self.assertEqual(c["kind"], BUY)
        self.assertEqual(c["base_mint"], BONK)
        self.assertEqual(c["quote_mint"], USDC)

    def test_a_token_to_token_swap_is_a_trade_but_unvalued(self):
        c = classify({BONK: -1_000.0, "OtherMint": 55.0})
        self.assertEqual(c["kind"], TOKEN_TOKEN)

    def test_a_partial_exit_records_only_what_moved(self):
        t = tx(token_pre=[(OWNER, BONK, 1_000_000), (OWNER, USDC, 0)],
               token_post=[(OWNER, BONK, 400_000), (OWNER, USDC, 390)])
        c = classify(owner_balance_deltas(t, OWNER))
        self.assertEqual(c["kind"], SELL)
        self.assertAlmostEqual(c["base_amount"], 600_000)


class EntryPriceTests(unittest.TestCase):
    """Execution price from the wallet's OWN economics, not a market candle."""

    def test_entry_price_comes_from_the_balance_delta(self):
        t = tx(token_pre=[(OWNER, USDC, 500), (OWNER, BONK, 0)],
               token_post=[(OWNER, USDC, 0), (OWNER, BONK, 1_000)])
        row = normalize_swap(t, OWNER)
        self.assertEqual(row["kind"], BUY)
        # 500 USDC (peg) for 1,000 tokens -> $0.50 each.
        self.assertAlmostEqual(row["entry_price_usd"], 0.5, places=6)
        self.assertEqual(row["entry_price_source"], "EXECUTION_BALANCE_DELTA")

    def test_an_unpriceable_quote_leaves_the_value_null_not_guessed(self):
        t = tx(token_pre=[(OWNER, BONK, 1_000), (OWNER, "Xmint", 0)],
               token_post=[(OWNER, BONK, 0), (OWNER, "Xmint", 50)])
        row = normalize_swap(t, OWNER)
        self.assertEqual(row["kind"], TOKEN_TOKEN)
        self.assertIsNone(row["notional_usd"])
        self.assertIsNone(row["entry_price_usd"])
        self.assertTrue(row.get("unvalued_reason"))

    def test_provenance_travels_with_the_value(self):
        t = tx(token_pre=[(OWNER, USDC, 500), (OWNER, BONK, 0)],
               token_post=[(OWNER, USDC, 0), (OWNER, BONK, 1_000)])
        row = normalize_swap(t, OWNER)
        self.assertIsNotNone(row["price_quality"])
        self.assertIsNotNone(row["price_source"])
        self.assertEqual(row["ledger_version"], "swap_v1_balance_delta")


class CursorTests(unittest.TestCase):
    """Restart-safe and incremental — not the same shallow window forever."""

    def test_the_registry_carries_a_cursor(self):
        from app.database import WalletRegistry
        cols = {c.name for c in WalletRegistry.__table__.columns}
        for f in ("history_newest_signature", "history_oldest_signature",
                  "history_records_loaded", "history_backfill_complete",
                  "last_history_sync_at", "last_deep_backfill_at",
                  "history_status", "history_error"):
            self.assertIn(f, cols, f)

    def test_the_ledger_stores_quote_identity(self):
        from app.database import WalletTrade
        cols = {c.name for c in WalletTrade.__table__.columns}
        for f in ("quote_mint", "quote_amount", "quote_price_usd",
                  "price_quality", "ledger_version"):
            self.assertIn(f, cols, f)

    def test_a_failed_page_does_not_advance_the_cursor(self):
        """A provider failure must never look like the end of history."""
        import inspect

        from lib import wallet_swaps
        src = inspect.getsource(wallet_swaps.sync_wallet_history)
        self.assertIn('history_status = "FAILED"', src)
        # The failure branch returns before any cursor assignment.
        fail_at = src.index('history_status = "FAILED"')
        newest_at = src.index("history_newest_signature = newest_seen")
        self.assertLess(fail_at, newest_at)
        self.assertIn("return", src[fail_at:newest_at])


if __name__ == "__main__":
    unittest.main()
