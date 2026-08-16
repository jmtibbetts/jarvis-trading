"""ZERO, UNKNOWN, INSUFFICIENT and PROVIDER_FAILURE are four states.

Collapsing them is how a system lies confidently:

  ZERO              we read the wallet and it has no trades
  INSUFFICIENT      it has 3 trades and we need 15
  NO_VERIFIED       it has transactions but none we could verify as swaps
  PROVIDER_FAILURE  we could not measure it AT ALL this run

The two defects this pins:

1. The measurability gate controlled EVIDENCE PERSISTENCE as well as SCORE
   ELIGIBILITY. A wallet with 3 real round trips continued past the gate
   before writing anything, so the diagnostic said "0 of 15" — which is
   false. The gate must decide whether a SCORE is supportable, never
   whether known facts get recorded.

2. A Helius failure must not overwrite a known qualified_trades=12 with 0.
   Zero means "we measured zero". Failure means "we could not measure this
   run". A scorer that writes 0 on timeout has destroyed evidence and
   called the result a measurement.
"""
import unittest
import uuid
from unittest.mock import patch

from app.database import WalletRegistry, get_db
from lib.wallet_scoring import MIN_TRADES_FOR_SCORE


def _rt(mint: str, spend: float, proceeds: float, t: float) -> list[dict]:
    buy, sell = uuid.uuid4().hex, uuid.uuid4().hex
    return [
        {"signature": buy, "timestamp": t, "direction": "in", "mint": mint,
         "symbol": "TOKEN", "amount": 1000.0, "counterparty": "pool"},
        {"signature": buy, "timestamp": t, "direction": "out",
         "mint": "usdc", "symbol": "USDC", "amount": spend,
         "counterparty": "pool"},
        {"signature": sell, "timestamp": t + 3600, "direction": "out",
         "mint": mint, "symbol": "TOKEN", "amount": 1000.0,
         "counterparty": "pool"},
        {"signature": sell, "timestamp": t + 3600, "direction": "in",
         "mint": "usdc", "symbol": "USDC", "amount": proceeds,
         "counterparty": "pool"},
    ]


def ledger(n: int) -> list[dict]:
    legs, t = [], 1_700_000_000.0
    for i in range(n):
        legs += _rt(f"m{i}", 500.0, 600.0, t)
        t += 7200
    return legs


class _Row:
    """Stand-in for a WalletRegistry row, so the state machine can be
    exercised without a DB round trip per case."""

    def __init__(self, **kw):
        self.address = kw.get("address", "W" + uuid.uuid4().hex[:30])
        self.status = kw.get("status", "CANDIDATE")
        for f in ("qualified_trades", "winning_trades", "losing_trades",
                  "win_rate", "profit_factor", "smart_money_score",
                  "confidence_score", "sample_count", "required_sample_count",
                  "measurable", "analysis_status", "measurability_reason",
                  "last_analysis_at", "analysis_error", "last_score_update",
                  "average_trade_size", "median_trade_size", "largest_trade",
                  "unpriced_trades", "alpha_score", "legacy_alpha_score",
                  "copy_score", "wallet_score_version", "whale_score"):
            setattr(self, f, kw.get(f))


def _run_scorer_over(row, transfers_result):
    """Drive the real scoring loop against one row."""
    import lib.wallet_scoring as ws

    class _Session:
        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def all(self):
            return [row]

    def _transfers(_addr, **_kw):
        if isinstance(transfers_result, Exception):
            raise transfers_result
        return transfers_result

    # `transfers` is imported INSIDE score_registry_wallets, so it resolves
    # against lib.helius_client at call time — patch it there, not here.
    # The session is injected through the function's own `db=` parameter,
    # so the real production body runs end to end.
    import lib.helius_client as hc
    with patch.object(hc, "transfers", _transfers):
        return ws.score_registry_wallets(limit=1, db=_Session())


class InsufficientIsNotZeroTests(unittest.TestCase):
    def test_three_trades_persist_as_three_not_zero(self):
        row = _Row()
        _run_scorer_over(row, ledger(3))
        self.assertEqual(row.qualified_trades, 3,
                         "the diagnostic would have read '0 of 15'")
        self.assertEqual(row.sample_count, 3)
        self.assertEqual(row.required_sample_count, MIN_TRADES_FOR_SCORE)
        self.assertIs(row.measurable, False)
        self.assertEqual(row.analysis_status, "INSUFFICIENT")
        self.assertEqual(row.measurability_reason,
                         "INSUFFICIENT_QUALIFIED_TRADES")

    def test_an_unsupported_score_is_still_refused(self):
        """Persisting evidence must not become persisting a score."""
        row = _Row()
        _run_scorer_over(row, ledger(3))
        self.assertIsNone(row.smart_money_score)
        self.assertIsNone(row.copy_score)

    def test_a_sufficient_sample_scores_and_is_marked_measured(self):
        row = _Row()
        _run_scorer_over(row, ledger(MIN_TRADES_FOR_SCORE + 4))
        self.assertEqual(row.qualified_trades, MIN_TRADES_FOR_SCORE + 4)
        self.assertIs(row.measurable, True)
        self.assertEqual(row.analysis_status, "MEASURED")
        self.assertIsNone(row.measurability_reason)
        self.assertIsNotNone(row.smart_money_score)


class ZeroIsAMeasurementTests(unittest.TestCase):
    def test_empty_history_records_zero_explicitly(self):
        row = _Row()
        _run_scorer_over(row, [])
        self.assertEqual(row.sample_count, 0)
        self.assertIs(row.measurable, False)
        self.assertEqual(row.analysis_status, "NO_VERIFIED_TRADES")
        self.assertIsNone(row.analysis_error,
                          "a successful empty read is not an error")
        self.assertIsNotNone(row.last_analysis_at)


class ProviderFailureNeverOverwritesEvidenceTests(unittest.TestCase):
    """The one Jon called out explicitly."""

    def test_failure_preserves_known_counts(self):
        row = _Row(qualified_trades=12, winning_trades=8, losing_trades=4,
                   win_rate=0.667, smart_money_score=73.0, sample_count=12,
                   measurable=True, analysis_status="MEASURED",
                   last_score_update="2026-08-16T00:00:00Z")
        _run_scorer_over(row, RuntimeError("helius timeout"))

        self.assertEqual(row.qualified_trades, 12, "evidence was destroyed")
        self.assertEqual(row.winning_trades, 8)
        self.assertEqual(row.losing_trades, 4)
        self.assertEqual(row.smart_money_score, 73.0)
        self.assertEqual(row.sample_count, 12)

    def test_failure_is_recorded_as_failure(self):
        row = _Row(qualified_trades=12, analysis_status="MEASURED")
        _run_scorer_over(row, RuntimeError("helius timeout"))
        self.assertEqual(row.analysis_status, "FAILED")
        self.assertIn("RuntimeError", row.analysis_error or "")
        self.assertIsNotNone(row.last_analysis_at)

    def test_failure_does_not_refresh_the_score_age(self):
        """last_score_update is when the SCORE changed. A failed run must
        leave it alone so stale evidence still reads as stale."""
        row = _Row(qualified_trades=12,
                   last_score_update="2026-08-16T00:00:00Z")
        _run_scorer_over(row, RuntimeError("boom"))
        self.assertEqual(row.last_score_update, "2026-08-16T00:00:00Z")

    def test_the_four_states_are_distinguishable(self):
        measured = _Row(); _run_scorer_over(measured, ledger(MIN_TRADES_FOR_SCORE + 2))
        insufficient = _Row(); _run_scorer_over(insufficient, ledger(3))
        zero = _Row(); _run_scorer_over(zero, [])
        failed = _Row(qualified_trades=9)
        _run_scorer_over(failed, RuntimeError("x"))

        seen = {measured.analysis_status, insufficient.analysis_status,
                zero.analysis_status, failed.analysis_status}
        self.assertEqual(len(seen), 4, f"states collapsed: {seen}")


if __name__ == "__main__":
    unittest.main()
