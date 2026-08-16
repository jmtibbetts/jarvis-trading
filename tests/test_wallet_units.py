"""W4 — different monetary units are never summed.

The defect, and it was worse than the audit's description. Trades stored a
raw quote amount:

    500 USDC  ->  cost_basis = 500
    3 SOL     ->  cost_basis = 3

and `sum(t["pnl"] for t in wins)` added them and called the total dollars.
Worse: a wallet that bought with USDC and sold for SOL had
`proceeds - cost_basis` subtracting SOL from dollars INSIDE a single trade,
before any aggregation happened at all.

CHECKPOINT 2 from the audit, verbatim:

    trade A: 500 USDC
    trade B: 3 SOL when SOL = $150
    expected: $500 + $450 = $950
    NOT 503, and NOT 500 + 3 valued at today's SOL price
"""
import unittest
from unittest.mock import patch

from lib import quote_valuation
from lib.wallet_scoring import reconstruct_trades, score_wallet

SOL_AT_150 = 1_700_000_000.0      # an epoch we pin SOL to $150 at
SOL_AT_50 = 1_600_000_000.0       # ... and to $50 at, much earlier


def _fake_sol_price(ts: float):
    """SOL was $50 in the older window and $150 in the newer one."""
    return (150.0, "stub") if ts >= SOL_AT_150 else (50.0, "stub")


def leg(sig, mint, symbol, amount, direction, ts, cp="cp1"):
    return {"signature": sig, "mint": mint, "symbol": symbol,
            "amount": amount, "direction": direction, "timestamp": ts,
            "counterparty": cp}


def round_trip(sig_in, sig_out, *, mint, qty, quote_symbol,
               spent, proceeds, t_open, t_close):
    return [
        leg(sig_in, mint, "TOKEN", qty, "in", t_open),
        leg(sig_in, "quote-mint", quote_symbol, spent, "out", t_open),
        leg(sig_out, mint, "TOKEN", qty, "out", t_close),
        leg(sig_out, "quote-mint", quote_symbol, proceeds, "in", t_close),
    ]


class Checkpoint2Tests(unittest.TestCase):
    """The audit's named fixture."""

    def setUp(self):
        quote_valuation.reset_cache()
        self._p = patch.object(quote_valuation, "_sol_price_at", _fake_sol_price)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        quote_valuation.reset_cache()

    def test_usdc_and_sol_notionals_sum_in_dollars(self):
        legs = (
            # 500 USDC in, closed flat, so notional is exactly 500 USD
            round_trip("a1", "a2", mint="mintA", qty=10, quote_symbol="USDC",
                       spent=500, proceeds=500,
                       t_open=SOL_AT_150, t_close=SOL_AT_150 + 60)
            # 3 SOL in at $150 -> 450 USD
            + round_trip("b1", "b2", mint="mintB", qty=20, quote_symbol="SOL",
                         spent=3, proceeds=3,
                         t_open=SOL_AT_150, t_close=SOL_AT_150 + 60)
        )
        r = reconstruct_trades(legs)
        self.assertEqual(r["closed"], 2)
        total = sum(t["notional_usd"] for t in r["trades"])
        self.assertAlmostEqual(total, 950.0, places=2,
                               msg="500 USDC + 3 SOL must be $950, never 503")

    def test_the_raw_sum_is_not_what_we_produce(self):
        """Guard the failure mode explicitly: 500 + 3 = 503."""
        legs = (
            round_trip("a1", "a2", mint="mintA", qty=10, quote_symbol="USDC",
                       spent=500, proceeds=500,
                       t_open=SOL_AT_150, t_close=SOL_AT_150 + 60)
            + round_trip("b1", "b2", mint="mintB", qty=20, quote_symbol="SOL",
                         spent=3, proceeds=3,
                         t_open=SOL_AT_150, t_close=SOL_AT_150 + 60)
        )
        total = sum(t["notional_usd"] for t in reconstruct_trades(legs)["trades"])
        self.assertNotAlmostEqual(total, 503.0, places=1)

    def test_historical_price_not_current_price(self):
        """A 2023 SOL trade must not be valued at today's SOL price."""
        old = round_trip("o1", "o2", mint="mintOld", qty=10, quote_symbol="SOL",
                         spent=3, proceeds=3,
                         t_open=SOL_AT_50, t_close=SOL_AT_50 + 60)
        t = reconstruct_trades(old)["trades"][0]
        self.assertAlmostEqual(t["notional_usd"], 150.0, places=2,
                               msg="3 SOL at the OLD $50 price is $150, not $450")
        self.assertAlmostEqual(t["quote_price_usd"], 50.0, places=2)


class SingleTradeUnitTests(unittest.TestCase):
    """The defect that lived below aggregation."""

    def setUp(self):
        quote_valuation.reset_cache()
        self._p = patch.object(quote_valuation, "_sol_price_at", _fake_sol_price)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        quote_valuation.reset_cache()

    def test_buy_in_usdc_sell_in_sol_is_computed_in_dollars(self):
        """Bought for 500 USDC, sold for 5 SOL at $150 = $750. A +$250 win.

        The old arithmetic did `5 - 500 = -495` and called it a loss of 495
        dollars — subtracting SOL from USD inside one trade.
        """
        legs = [
            leg("s1", "mintX", "TOKEN", 100, "in", SOL_AT_150),
            leg("s1", "usdc-mint", "USDC", 500, "out", SOL_AT_150),
            leg("s2", "mintX", "TOKEN", 100, "out", SOL_AT_150 + 3600),
            leg("s2", "sol-mint", "SOL", 5, "in", SOL_AT_150 + 3600),
        ]
        t = reconstruct_trades(legs)["trades"][0]
        self.assertAlmostEqual(t["cost_basis_usd"], 500.0, places=2)
        self.assertAlmostEqual(t["proceeds_usd"], 750.0, places=2)
        self.assertAlmostEqual(t["pnl_usd"], 250.0, places=2)
        self.assertGreater(t["pnl_usd"], 0, "this is a winning trade")

    def test_sol_appreciation_alone_is_not_token_profit(self):
        """Bought with 3 SOL at $50 ($150), sold for 3 SOL at $150 ($450).

        The token went nowhere in SOL terms, but the SOL leg tripled — so in
        USD this genuinely IS a gain, and it must be visible as one rather
        than netting to zero.
        """
        legs = [
            leg("s1", "mintY", "TOKEN", 10, "in", SOL_AT_50),
            leg("s1", "sol-mint", "SOL", 3, "out", SOL_AT_50),
            leg("s2", "mintY", "TOKEN", 10, "out", SOL_AT_150),
            leg("s2", "sol-mint", "SOL", 3, "in", SOL_AT_150),
        ]
        t = reconstruct_trades(legs)["trades"][0]
        self.assertAlmostEqual(t["cost_basis_usd"], 150.0, places=2)
        self.assertAlmostEqual(t["proceeds_usd"], 450.0, places=2)
        self.assertAlmostEqual(t["pnl_usd"], 300.0, places=2)


class ProvenanceTests(unittest.TestCase):

    def test_every_trade_carries_its_pricing_provenance(self):
        legs = round_trip("a1", "a2", mint="m", qty=10, quote_symbol="USDC",
                          spent=500, proceeds=600, t_open=1, t_close=2)
        t = reconstruct_trades(legs)["trades"][0]
        for k in ("quote_symbol", "quote_amount", "quote_price_usd",
                  "price_source", "price_quality", "cost_basis_usd",
                  "proceeds_usd", "pnl_usd", "notional_usd"):
            self.assertIn(k, t)
        self.assertEqual(t["price_quality"], quote_valuation.ESTIMATED,
                         "a peg is an assumption, not a measurement")

    def test_an_unvaluable_quote_is_unpriced_not_invented(self):
        """A token-to-token swap has no dollar value. Say so."""
        legs = [
            leg("s1", "mintA", "TOKEN", 100, "in", 1000),
            leg("s1", "mintB", "OTHERTOKEN", 50, "out", 1000),
        ]
        r = reconstruct_trades(legs)
        self.assertEqual(r["closed"], 0)
        self.assertGreater(r["unpriced_legs"], 0)

    def test_a_yield_wrapper_is_not_worth_a_dollar(self):
        """sUSDe does not target par and must not be priced as a stable."""
        p = quote_valuation.quote_price_usd("sUSDe", 1_700_000_000)
        self.assertIsNone(p["price_usd"])
        self.assertEqual(p["quality"], quote_valuation.UNAVAILABLE)

    def test_a_stale_sol_bar_refuses_rather_than_guesses(self):
        quote_valuation.reset_cache()
        with patch.object(quote_valuation, "_load_sol_bars",
                          return_value=[(1_000_000_000.0, 20.0)]):
            p = quote_valuation.quote_price_usd("SOL", 1_700_000_000)
        self.assertIsNone(p["price_usd"])
        self.assertIn("stale", (p["reason"] or "").lower())


class ScoreAggregationTests(unittest.TestCase):

    def setUp(self):
        quote_valuation.reset_cache()
        self._p = patch.object(quote_valuation, "_sol_price_at", _fake_sol_price)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        quote_valuation.reset_cache()

    def test_equivalent_usd_notionals_score_equivalently(self):
        """The audit's requirement: mix USDC- and SOL-quoted trades and
        assert equivalent USD notionals produce equivalent size numbers."""
        def book(quote, spent, proceeds, n=10):
            legs = []
            for i in range(n):
                legs += round_trip(f"i{i}", f"o{i}", mint=f"m{i}", qty=10,
                                   quote_symbol=quote, spent=spent,
                                   proceeds=proceeds,
                                   t_open=SOL_AT_150, t_close=SOL_AT_150 + 60)
            return legs

        usdc = score_wallet(reconstruct_trades(book("USDC", 600, 900)))
        # 4 SOL at $150 = $600 in; 6 SOL = $900 out. Same dollars.
        sol = score_wallet(reconstruct_trades(book("SOL", 4, 6)))

        self.assertAlmostEqual(usdc["metrics"]["median_size_usd"],
                               sol["metrics"]["median_size_usd"], places=2)
        self.assertAlmostEqual(usdc["metrics"]["total_pnl_usd"],
                               sol["metrics"]["total_pnl_usd"], places=2)
        self.assertAlmostEqual(usdc["smart_money_score"],
                               sol["smart_money_score"], places=2)


class LegacyAlphaTests(unittest.TestCase):
    """The other half of the P0: alpha_score held realized return."""

    def test_alpha_score_is_vacated_until_it_is_measured(self):
        legs = []
        for i in range(12):
            legs += round_trip(f"i{i}", f"o{i}", mint=f"m{i}", qty=10,
                               quote_symbol="USDC", spent=500,
                               proceeds=600 if i % 2 else 400,
                               t_open=1000 + i, t_close=2000 + i)
        s = score_wallet(reconstruct_trades(legs))
        self.assertTrue(s["measurable"])
        self.assertIsNone(s["alpha_score"],
                          "post-entry alpha is not measured yet — NULL is the "
                          "honest answer, not the realized return")
        self.assertIsNotNone(s["legacy_alpha_score"])
        self.assertIn("NOT MEASURED", s["alpha_basis"])


if __name__ == "__main__":
    unittest.main()
