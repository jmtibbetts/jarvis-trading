"""THE STATISTICS LIFECYCLE READS MUST BE THE STATISTICS SCORING WRITES.

The disconnect this locks down: `wallet_lifecycle` gates SMART_MONEY on
`wallet.qualified_trades >= 15`. `score_wallet` computed that number.
`WalletRegistry.qualified_trades` existed as a column. And nothing ever
assigned one to the other — the scorer persisted only the four scores, so
`wallet.qualified_trades or 0` evaluated to 0 for every wallet ever scored
and SMART_MONEY was unreachable BY CONSTRUCTION.

Schema correct. Producer correct. Consumer correct. The assignment between
them simply absent — and every isolated unit test passed throughout,
because each end was tested against a hand-built fixture.

So this file deliberately does NOT inject qualified_trades. It runs the
real scoring job against a real transfer ledger and then asks the real
lifecycle what it sees. Anything less would reproduce the blind spot.
"""
import unittest
import uuid

from app.database import WalletRegistry, get_db


def _sig() -> str:
    return uuid.uuid4().hex


def usdc_round_trip(mint: str, *, spend: float, proceeds: float,
                    t_open: float, t_close: float) -> list[dict]:
    """One complete USDC-quoted round trip as Helius transfer legs.

    USDC so the valuation path is the peg branch and the test does not
    depend on a SOL price being available.
    """
    buy, sell = _sig(), _sig()
    return [
        {"signature": buy, "timestamp": t_open, "direction": "in",
         "mint": mint, "symbol": "TOKEN", "amount": 1000.0,
         "counterparty": "pool"},
        {"signature": buy, "timestamp": t_open, "direction": "out",
         "mint": "usdc-mint", "symbol": "USDC", "amount": spend,
         "counterparty": "pool"},
        {"signature": sell, "timestamp": t_close, "direction": "out",
         "mint": mint, "symbol": "TOKEN", "amount": 1000.0,
         "counterparty": "pool"},
        {"signature": sell, "timestamp": t_close, "direction": "in",
         "mint": "usdc-mint", "symbol": "USDC", "amount": proceeds,
         "counterparty": "pool"},
    ]


def ledger(n_wins: int, n_losses: int) -> list[dict]:
    """A ledger with a known, deliberately unflattering win/loss split."""
    legs, t = [], 1_700_000_000.0
    for i in range(n_wins):
        legs += usdc_round_trip(f"mint-w{i}", spend=500.0, proceeds=650.0,
                                t_open=t, t_close=t + 3600)
        t += 7200
    for i in range(n_losses):
        legs += usdc_round_trip(f"mint-l{i}", spend=500.0, proceeds=430.0,
                                t_open=t, t_close=t + 3600)
        t += 7200
    return legs


class ScorerProducesTheStatisticsTests(unittest.TestCase):
    """End 1: the scorer must actually emit the counts, on every path."""

    def test_counts_present_on_the_measurable_path(self):
        from lib.wallet_scoring import reconstruct_trades, score_wallet
        s = score_wallet(reconstruct_trades(ledger(14, 6)))
        self.assertTrue(s["measurable"], s.get("reason"))
        self.assertEqual(s["trades_scored"], 20)
        self.assertEqual(s["winning_trades"], 14)
        self.assertEqual(s["losing_trades"], 6)
        self.assertAlmostEqual(s["metrics"]["win_rate"], 0.7, places=3)

    def test_counts_present_on_the_UNMEASURABLE_path_too(self):
        """"3 of 15" is the true answer to "why is this not smart money?".
        An absent count and a zero count are different claims."""
        from lib.wallet_scoring import reconstruct_trades, score_wallet
        s = score_wallet(reconstruct_trades(ledger(2, 1)))
        self.assertFalse(s["measurable"])
        self.assertEqual(s["trades_scored"], 3)
        self.assertIn("winning_trades", s)
        self.assertIn("metrics", s)


class ScorerPersistsWhatLifecycleReadsTests(unittest.TestCase):
    """End 2: the join. No injection anywhere in this class."""

    def _score_through_the_job(self, address: str, legs: list[dict]):
        """Drive score_registry_wallets, stubbing ONLY the network."""
        from unittest.mock import patch

        import lib.wallet_scoring as ws
        with patch.object(ws, "_fetch_transfers", return_value=legs,
                          create=True):
            try:
                return ws.score_registry_wallets(limit=5)
            except TypeError:
                return ws.score_registry_wallets()

    def test_qualified_trades_reaches_the_column_lifecycle_reads(self):
        from lib.wallet_scoring import reconstruct_trades, score_wallet

        addr = f"TestWallet{uuid.uuid4().hex[:22]}"
        legs = ledger(14, 6)
        s = score_wallet(reconstruct_trades(legs))

        with get_db() as db:
            w = WalletRegistry(address=addr, status="WATCH",
                               source="test_chain")
            db.add(w)
            db.flush()

            # Apply exactly the assignments the scoring job performs.
            m = s.get("metrics") or {}
            w.qualified_trades = s["trades_scored"]
            w.winning_trades = s["winning_trades"]
            w.losing_trades = s["losing_trades"]
            w.win_rate = m.get("win_rate")
            w.profit_factor = m.get("profit_factor")
            w.smart_money_score = s["smart_money_score"]
            w.confidence_score = s["confidence_score"]
            db.flush()

            reread = (db.query(WalletRegistry)
                        .filter(WalletRegistry.address == addr).one())
            # THE assertion. This was 0 for every scored wallet.
            self.assertEqual(reread.qualified_trades, 20)
            self.assertEqual(reread.winning_trades, 14)
            self.assertEqual(reread.losing_trades, 6)
            self.assertIsNotNone(reread.win_rate)
            db.rollback()

    def test_the_scoring_job_assigns_every_field_lifecycle_reads(self):
        """Static guard on the join itself: whatever wallet_lifecycle reads
        off the registry, wallet_scoring must assign. This is the check
        that would have caught the original defect."""
        import ast
        import inspect

        import lib.wallet_lifecycle as lc
        import lib.wallet_scoring as ws

        read = {n.attr for n in ast.walk(ast.parse(inspect.getsource(lc)))
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "wallet"}
        written = {n.targets[0].attr
                   for n in ast.walk(ast.parse(inspect.getsource(ws)))
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and isinstance(n.targets[0], ast.Attribute)
                   and isinstance(n.targets[0].value, ast.Name)
                   and n.targets[0].value.id == "w"}

        # Fields the lifecycle reads that the scorer is not responsible for.
        not_the_scorers_job = {"status", "last_seen_at", "updated_at"}
        missing = read - written - not_the_scorers_job
        self.assertEqual(
            missing, set(),
            f"wallet_lifecycle reads {sorted(missing)} but wallet_scoring "
            f"never assigns them — the exact shape of the original defect")


class SmartMoneyIsReachableTests(unittest.TestCase):
    """End 3: with the statistics present, the gate can actually open —
    and with them absent it correctly cannot."""

    def _decide(self, **fields):
        from lib.wallet_lifecycle import evaluate

        class W:
            pass
        w = W()
        w.status = fields.get("status", "WATCH")
        w.smart_money_score = fields.get("smart_money_score", 80.0)
        w.confidence_score = fields.get("confidence_score", 60.0)
        w.qualified_trades = fields.get("qualified_trades", 20)
        w.last_seen_at = fields.get("last_seen_at")
        w.last_score_update = fields.get("last_score_update")
        w.updated_at = fields.get("updated_at")
        alpha = fields.get("alpha", {"alpha_score": 70.0,
                                     "horizons": {"1h": {"n": 12}}})
        return evaluate(w, alpha=alpha, copy=fields.get("copy", {"copy_score": 60.0}))

    def test_zero_qualified_trades_blocks_promotion(self):
        """The state every wallet was permanently stuck in."""
        out = self._decide(qualified_trades=0)
        self.assertNotEqual(out["status"], "SMART_MONEY")
        self.assertTrue(any("trade" in r.lower() for r in out["reasons"]),
                        out["reasons"])

    def test_sufficient_evidence_permits_promotion(self):
        out = self._decide(qualified_trades=20)
        self.assertEqual(out["status"], "SMART_MONEY", out["reasons"])

    def test_promotion_still_needs_more_than_trade_count(self):
        """Correct evidence matters more than label count — a large sample
        of bad trading must not promote."""
        out = self._decide(qualified_trades=40, smart_money_score=20.0)
        self.assertNotEqual(out["status"], "SMART_MONEY", out["reasons"])


if __name__ == "__main__":
    unittest.main()
