"""Four scores, kept separate, and refused when the sample is too thin.

The gap between them is the useful part. A wallet can be genuinely skilled
and completely unfollowable — enormous returns from positions too small or
too fast for anyone else to copy. Measured on synthetic-but-realistic
input: smart_money 75.0 against copy 30.25. Averaging those into one
number destroys the only thing worth knowing.
"""
import unittest

from lib.wallet_scoring import (FULL_CONFIDENCE_TRADES, MIN_TRADES_FOR_SCORE,
                                reconstruct_trades, score_wallet, transfer_key)


def buy(i, mint, qty, spent, ts):
    return [{"signature": f"s{i}", "direction": "in", "mint": mint, "symbol": "TOK",
             "amount": qty, "counterparty": "pool", "timestamp": ts},
            {"signature": f"s{i}", "direction": "out", "mint": "usdc", "symbol": "USDC",
             "amount": spent, "counterparty": "pool", "timestamp": ts}]


def sell(i, mint, qty, proceeds, ts):
    return [{"signature": f"s{i}", "direction": "out", "mint": mint, "symbol": "TOK",
             "amount": qty, "counterparty": "pool", "timestamp": ts},
            {"signature": f"s{i}", "direction": "in", "mint": "usdc", "symbol": "USDC",
             "amount": proceeds, "counterparty": "pool", "timestamp": ts}]


def book(n, win_every=3, size=5000, hold=2000):
    legs = []
    for i in range(n):
        won = i % win_every != 0
        legs += buy(100 + i * 2, f"m{i}", 100, size, 1000 + i)
        legs += sell(101 + i * 2, f"m{i}", 100, size * (1.2 if won else 0.92), 1000 + i + hold)
    return legs


class IdentityTests(unittest.TestCase):
    def test_identity_needs_all_four_parts(self):
        """One signature moves several mints between several counterparties;
        observed live as signature 5M92C1p5… appearing twice."""
        base = {"signature": "s", "mint": "m", "counterparty": "c", "direction": "in"}
        self.assertNotEqual(transfer_key(base), transfer_key({**base, "mint": "m2"}))
        self.assertNotEqual(transfer_key(base), transfer_key({**base, "counterparty": "c2"}))
        self.assertNotEqual(transfer_key(base), transfer_key({**base, "direction": "out"}))

    def test_duplicate_legs_do_not_double_count(self):
        legs = buy(1, "m", 100, 1000, 10) + sell(2, "m", 100, 1300, 20)
        self.assertEqual(reconstruct_trades(legs + legs)["closed"], 1)


class ReconstructionTests(unittest.TestCase):
    def test_a_round_trip_produces_pnl_and_hold_time(self):
        r = reconstruct_trades(buy(1, "m", 100, 1000, 1000) + sell(2, "m", 100, 1300, 2000))
        t = r["trades"][0]
        self.assertEqual(t["pnl"], 300.0)
        self.assertEqual(t["return_pct"], 30.0)
        self.assertEqual(t["hold_seconds"], 1000.0)

    def test_token_to_token_is_unpriced_not_invented(self):
        """No price for either leg means no dollar value. Guessing one
        would corrupt every score built on top."""
        legs = [{"signature": "x", "direction": "in", "mint": "a", "symbol": "AAA",
                 "amount": 5, "counterparty": "p", "timestamp": 1},
                {"signature": "x", "direction": "out", "mint": "b", "symbol": "BBB",
                 "amount": 7, "counterparty": "p", "timestamp": 1}]
        r = reconstruct_trades(legs)
        self.assertEqual(r["closed"], 0)
        self.assertGreater(r["unpriced_legs"], 0)

    def test_an_unclosed_buy_is_open_not_a_trade(self):
        r = reconstruct_trades(buy(1, "m", 100, 1000, 10))
        self.assertEqual(r["closed"], 0)
        self.assertEqual(r["still_open"], 1)

    def test_empty_input_is_handled(self):
        r = reconstruct_trades([])
        self.assertEqual(r["closed"], 0)


class SampleSizeTests(unittest.TestCase):
    def test_a_short_winning_streak_is_not_scored(self):
        """2 trades at 100% must never outrank 167 at 71%."""
        legs = []
        for i in range(3):
            legs += buy(10 + i * 2, f"m{i}", 100, 1000, 1) + sell(11 + i * 2, f"m{i}", 100, 2000, 2)
        s = score_wallet(reconstruct_trades(legs))
        self.assertFalse(s["measurable"])
        self.assertIsNone(s["smart_money_score"])
        self.assertIn(str(MIN_TRADES_FOR_SCORE), s["reason"])

    def test_enough_trades_produces_scores(self):
        s = score_wallet(reconstruct_trades(book(20)))
        self.assertTrue(s["measurable"])
        for k in ("smart_money_score", "alpha_score", "copy_score", "confidence_score"):
            self.assertIsNotNone(s[k])

    def test_confidence_scales_with_sample_and_pulls_toward_neutral(self):
        small = score_wallet(reconstruct_trades(book(10)))
        large = score_wallet(reconstruct_trades(book(FULL_CONFIDENCE_TRADES)))
        self.assertLess(small["confidence_score"], large["confidence_score"])
        self.assertLess(abs(small["smart_money_score"] - 50.0),
                        abs(large["smart_money_score"] - 50.0))


class ScoreSeparationTests(unittest.TestCase):
    def test_a_skilled_wallet_can_be_uncopyable(self):
        """THE distinction: tiny, fast trades nobody else can follow."""
        tiny = score_wallet(reconstruct_trades(book(20, size=40, hold=2)))
        self.assertGreater(tiny["smart_money_score"], 60)
        self.assertLess(tiny["copy_score"], tiny["smart_money_score"] * 0.6)

    def test_a_normal_wallet_keeps_a_reasonable_copy_score(self):
        normal = score_wallet(reconstruct_trades(book(20, size=5000, hold=2000)))
        self.assertGreater(normal["copy_score"], normal["smart_money_score"] * 0.8)

    def test_whale_is_capital_and_independent_of_skill(self):
        """A whale is not automatically smart money."""
        s = score_wallet(reconstruct_trades([]), portfolio_usd=5_000_000)
        self.assertIsNotNone(s["whale_score"])
        self.assertIsNone(s["smart_money_score"])

    def test_a_losing_wallet_scores_below_a_winning_one(self):
        good = score_wallet(reconstruct_trades(book(20, win_every=3)))
        bad = score_wallet(reconstruct_trades(book(20, win_every=1)))
        self.assertGreater(good["smart_money_score"], bad["smart_money_score"])

    def test_metrics_are_reported_alongside_the_scores(self):
        s = score_wallet(reconstruct_trades(book(20)))
        for k in ("win_rate", "profit_factor", "median_return_pct",
                  "median_size_usd", "total_pnl"):
            self.assertIn(k, s["metrics"])


if __name__ == "__main__":
    unittest.main()
