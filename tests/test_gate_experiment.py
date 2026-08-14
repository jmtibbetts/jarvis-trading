"""Both gates, one candidate stream — and only v8 executes.

The composite score is measured inverted against outcomes, so it lost
live authority; but it was demoted to a RECORDED arm, not deleted. These
tests pin the experiment's ground rules: legacy records faithfully, v8
decides by measured expectancy with its robust lower bound, UNKNOWN and
TENTATIVE stay out of live capital, and the executor consults gate_v8
rather than any score threshold.
"""
import inspect
import unittest
from unittest.mock import patch

from lib.gate import (NO_TRADE, TENTATIVE, TRADE, UNKNOWN, gate_legacy,
                      gate_v8, record_both)

LONG_SIG = {"asset_symbol": "BTC/USD", "asset_class": "Crypto",
            "direction": "Long", "timeframe": "4H",
            "entry_price": 60000.0, "stop_loss": 58000.0,
            "target_price": 64000.0, "strategy": "breakout"}


def _ev(verdict, net=None, net_lower=None, robust=False, reason="r"):
    return {"verdict": verdict, "reason": reason, "robust": robust,
            "net": {"net_expected_r": net},
            "net_lower": {"net_expected_r": net_lower}}


class LegacyArmTests(unittest.TestCase):
    def test_replicates_the_old_coalesce(self):
        self.assertTrue(gate_legacy({"composite_score": 55.0})["take"])
        self.assertFalse(gate_legacy({"composite_score": 54.9})["take"])
        # coalesce: falls back to confidence when composite is missing
        self.assertTrue(gate_legacy({"confidence": 70})["take"])
        self.assertFalse(gate_legacy({})["take"])


class V8ArmTests(unittest.TestCase):
    def test_robust_trade_takes(self):
        with patch("lib.expectancy.evaluate",
                   return_value=_ev("TRADE", 0.2, 0.08, robust=True)):
            g = gate_v8(LONG_SIG)
        self.assertEqual(g["decision"], TRADE)
        self.assertTrue(g["take"])

    def test_tentative_does_not_reach_live(self):
        """Point estimate clears, lower bound doesn't: the edge is inside
        the noise. Computing uncertainty and ignoring it at the capital
        boundary was P0.11's exact complaint."""
        with patch("lib.expectancy.evaluate",
                   return_value=_ev("TRADE", 0.2, -0.01, robust=False)):
            g = gate_v8(LONG_SIG)
        self.assertEqual(g["decision"], TENTATIVE)
        self.assertFalse(g["take"])

    def test_no_trade_refuses(self):
        with patch("lib.expectancy.evaluate",
                   return_value=_ev("NO_TRADE", -0.1, -0.2)):
            g = gate_v8(LONG_SIG)
        self.assertEqual(g["decision"], NO_TRADE)
        self.assertFalse(g["take"])

    def test_unknown_is_paper_by_default(self):
        with patch("lib.expectancy.evaluate", return_value=_ev("UNKNOWN")):
            g = gate_v8(LONG_SIG)
        self.assertEqual(g["decision"], UNKNOWN)
        self.assertFalse(g["take"])

    def test_unknown_live_needs_the_explicit_override(self):
        with patch("lib.expectancy.evaluate", return_value=_ev("UNKNOWN")), \
             patch.dict("os.environ", {"ALLOW_EXPERIMENTAL_LIVE": "1"}):
            g = gate_v8(LONG_SIG)
        self.assertTrue(g["take"])
        self.assertIn("override", g["reason"])

    def test_validity_failures_never_reach_expectancy(self):
        bad_levels = dict(LONG_SIG, stop_loss=61000.0)   # long stop above entry
        g = gate_v8(bad_levels)
        self.assertEqual(g["decision"], NO_TRADE)
        self.assertIn("levels", g["reason"])
        g2 = gate_v8(dict(LONG_SIG, direction="sideways"))
        self.assertEqual(g2["decision"], NO_TRADE)
        self.assertIn("unparseable", g2["reason"])

    def test_expectancy_crash_is_not_permission_to_trade(self):
        with patch("lib.expectancy.evaluate", side_effect=RuntimeError("db gone")):
            g = gate_v8(LONG_SIG)
        self.assertFalse(g["take"])


class RecordBothTests(unittest.TestCase):
    def test_both_verdicts_in_one_payload(self):
        with patch("lib.expectancy.evaluate",
                   return_value=_ev("TRADE", 0.2, 0.1, robust=True)):
            r = record_both(dict(LONG_SIG, composite_score=80.0))
        self.assertTrue(r["gate_legacy_take"])
        self.assertEqual(r["gate_v8_decision"], TRADE)
        self.assertTrue(r["gate_v8_take"])
        self.assertEqual(r["gate_v8_net_r"], 0.2)


class ExecutorWiringTests(unittest.TestCase):
    def test_the_score_gate_is_gone_from_the_live_query(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertNotIn("score_expr >=", src)
        self.assertIn("gate_v8", src)

    def test_ranking_is_by_net_r_not_score(self):
        from jobs import execute_signals
        src = inspect.getsource(execute_signals)
        self.assertIn('d.get("net_r")', src)
        self.assertNotIn("order_by(score_expr.desc())", src)

    def test_candidates_record_both_arms(self):
        from lib import candidates
        self.assertIn("record_both", inspect.getsource(candidates))


class StratifiedScoreboardTests(unittest.TestCase):
    """The 2026-08-15 audit: TRADE picks were 87% 1D futures while
    NO_TRADEs were mostly intraday, and same-day repeats (five ZC=F rows
    in one session) inflate raw n. The scoreboard must stratify by
    timeframe and report effective_n (distinct symbol-days), or the
    judgment window's verdict compares daily corn to 15-minute crypto.
    """

    def setUp(self):
        from app.database import init_db
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        from app.database import CandidateSignal, get_db
        with get_db() as db:
            db.query(CandidateSignal).filter(
                CandidateSignal.symbol.like("TEST-STRAT%")).delete(
                synchronize_session=False)
            db.commit()

    def test_gate_rows_stratify_and_count_symbol_days(self):
        from datetime import datetime, timezone

        from app.database import CandidateSignal, get_db
        from lib.candidates import selection_bias_summary

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            # Three same-day 1D TRADE rows on ONE symbol: n=3, eff_n=1.
            for i in range(3):
                db.add(CandidateSignal(
                    dedup_hash=f"TEST-STRAT-T{i}", symbol="TEST-STRAT-ZC",
                    timeframe="1D", direction="Long", verdict="persisted",
                    gate_v8_decision="TRADE", resolved=True,
                    pnl_pct=1.0, mfe_r=0.5, created_at=now))
            # Two 15m NO_TRADEs on different symbols: n=2, eff_n=2.
            for i in range(2):
                db.add(CandidateSignal(
                    dedup_hash=f"TEST-STRAT-N{i}", symbol=f"TEST-STRAT-{i}",
                    timeframe="15m", direction="Long", verdict="rejected",
                    gate_v8_decision="NO_TRADE", resolved=True,
                    pnl_pct=-0.5, mfe_r=0.1, created_at=now))
            db.commit()

        rows = selection_bias_summary()["by_gate_decision"]
        trade_1d = next(r for r in rows if r["decision"] == "TRADE"
                        and r["timeframe"] == "1D")
        nt_15m = next(r for r in rows if r["decision"] == "NO_TRADE"
                      and r["timeframe"] == "15m")
        self.assertGreaterEqual(trade_1d["n"], 3)
        self.assertLess(trade_1d["effective_n"], trade_1d["n"])
        self.assertGreaterEqual(nt_15m["effective_n"], 2)
        # Stratification means no pooled TRADE row without a timeframe.
        self.assertTrue(all("timeframe" in r for r in rows))


if __name__ == "__main__":
    unittest.main()
