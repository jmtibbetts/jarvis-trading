"""P18 — autonomous DEX trading, and what it must refuse.

The DEX surface was manual: an operator typed a mint, a pool reserve and a
price. Useful for development, useless for training — a set of hand-picked
trades measures the operator, not the system.

Two rules this pipeline exists to enforce.

WALLET INTELLIGENCE IS A SENSOR, NOT AN OVERRIDE. Verified smart-money
buying raises conviction. It is never permission to skip liquidity,
impact, sizing or the cost gate. A wallet can be genuinely excellent and
the trade still economically untradeable for JARVIS — the wallet was
earlier, cheaper, and may have taken the size that made the pool worse.

EVERY REFUSAL NAMES ITSELF. A single "skipped" bucket cannot distinguish
"found nothing", "liquidity refused it" and "economics refused it", and
those call for completely different responses.
"""
import os
import unittest

from lib.dex_autotrade import (DISABLED, IMPACT_TOO_HIGH, INSUFFICIENT_GAS,
                               MIN_NET_R, NEGATIVE_NET_EXPECTANCY,
                               NETWORK_FEE_REFUSED, NETWORK_FEE_UNKNOWN,
                               NO_ROUTE, POOL_TOO_THIN, copyability_gap)
from lib.dex_autotrade import evaluate_candidate as _evaluate_candidate


# THE COST GATE NOW MEASURES THE LIVE FEE MARKET, so these tests must supply
# one. They are about autotrade ECONOMICS — which refusal fires, and on what
# grounds — not about fee estimation, which test_solana_fee_units_and_authority
# and test_dex_canonical_fee_integration cover directly.
#
# A hermetic test must never reach real Helius, so the estimator is injected
# rather than defaulted. 1,000 micro-lamports/CU on the 400k-CU budget is 400
# lamports of priority plus the 5,000-lamport base = 0.0000054 SOL, far inside
# every operator ceiling, so the fee is never what refuses these candidates.
def _fee_fetch(micro_per_cu=1_000.0):
    def go(method, params):
        if method == "getPriorityFeeEstimate":
            return {"priorityFeeEstimate": micro_per_cu}
        return [{"prioritizationFee": micro_per_cu}]
    return go


def evaluate_candidate(candidate, **kw):
    """Defaults an injected estimator and a real SOL price.

    A SOL price is now load-bearing rather than decorative: an entry may not
    skip the percentage caps its own policy declares, so a fee it cannot
    value in USD is a refusal. `sol_price_usd=0.0` describes a world where
    SOL is worthless, which is not the world these tests mean to describe.
    """
    kw.setdefault("fee_fetch", _fee_fetch())
    kw.setdefault("sol_price_usd", 200.0)
    return _evaluate_candidate(candidate, **kw)


def setUpModule():
    """These tests are about autotrade ECONOMICS, and a funded wallet is
    now their precondition.

    `gas_balance_sol=1.0` used to BE the balance; it is now only a
    conservative cap on a real ledger, so an unfunded wallet refuses before
    any economics are computed. Funding here keeps each test measuring what
    it was written to measure — wallet authority itself is covered in
    test_dex_invariants.
    """
    from lib import dex_wallet as DW
    if not DW.initialized():
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=5.0,
            reason="autotrade economics fixture"))


class _Enabled:
    def __enter__(self):
        self.old = os.environ.get("DEX_AUTOTRADE_ENABLED")
        os.environ["DEX_AUTOTRADE_ENABLED"] = "1"
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop("DEX_AUTOTRADE_ENABLED", None)
        else:
            os.environ["DEX_AUTOTRADE_ENABLED"] = self.old


def cand(**kw):
    base = {"mint": "Mint111", "symbol": "TEST", "reserve_usd": 500_000.0,
            "price_usd": 1.0, "dex": "raydium",
            "depth_confidence": "VERIFIED", "gross_expected_r": 0.60}
    base.update(kw)
    return base


class DisabledByDefaultTests(unittest.TestCase):
    def test_it_does_nothing_unless_explicitly_enabled(self):
        os.environ.pop("DEX_AUTOTRADE_ENABLED", None)
        out = evaluate_candidate(cand(), gas_balance_sol=1.0, cash_usd=100_000)
        self.assertFalse(out["eligible"])
        self.assertEqual(out["reason"], DISABLED)


class RefusalsAreDistinctTests(unittest.TestCase):
    def test_no_gas_is_refused_before_anything_else(self):
        """A wallet that cannot pay for a transaction cannot make one, and
        discovering that after sizing wastes the evaluation."""
        with _Enabled():
            out = evaluate_candidate(cand(), gas_balance_sol=0.0,
                                     cash_usd=100_000)
            self.assertEqual(out["reason"], INSUFFICIENT_GAS)

    def test_a_thin_pool_is_refused_as_thin(self):
        with _Enabled():
            out = evaluate_candidate(cand(reserve_usd=5_000.0),
                                     gas_balance_sol=1.0, cash_usd=100_000)
            self.assertEqual(out["reason"], POOL_TOO_THIN)

    def test_an_unpriceable_token_is_refused(self):
        with _Enabled():
            out = evaluate_candidate(cand(price_usd=0.0),
                                     gas_balance_sol=1.0, cash_usd=100_000)
            self.assertEqual(out["reason"], NO_ROUTE)

    def test_thin_edge_is_refused_on_ECONOMICS_not_liquidity(self):
        """And the message says it is a VENUE result, because the same
        thesis may well clear on a CEX."""
        with _Enabled():
            out = evaluate_candidate(cand(gross_expected_r=0.02),
                                     gas_balance_sol=1.0, cash_usd=100_000)
            self.assertEqual(out["reason"], NEGATIVE_NET_EXPECTANCY)
            self.assertIn("CEX", out["detail"])

    def test_the_refusal_reasons_are_all_different(self):
        with _Enabled():
            reasons = {
                evaluate_candidate(cand(), gas_balance_sol=0.0,
                                   cash_usd=100_000)["reason"],
                evaluate_candidate(cand(reserve_usd=5_000.0),
                                   gas_balance_sol=1.0,
                                   cash_usd=100_000)["reason"],
                evaluate_candidate(cand(gross_expected_r=0.02),
                                   gas_balance_sol=1.0,
                                   cash_usd=100_000)["reason"],
            }
            self.assertEqual(len(reasons), 3, reasons)


class DepthAffectsEligibilityTests(unittest.TestCase):
    def test_modelled_depth_sizes_smaller_than_verified(self):
        with _Enabled():
            v = evaluate_candidate(cand(depth_confidence="VERIFIED"),
                                   gas_balance_sol=1.0, cash_usd=100_000)
            m = evaluate_candidate(cand(depth_confidence="MODELLED_ESTIMATE"),
                                   gas_balance_sol=1.0, cash_usd=100_000)
            self.assertTrue(v["eligible"], v.get("detail"))
            if m["eligible"]:
                self.assertLess(m["size_usd"], v["size_usd"])

    def test_uncertain_depth_weights_the_impact_up(self):
        with _Enabled():
            m = evaluate_candidate(cand(depth_confidence="MODELLED_ESTIMATE"),
                                   gas_balance_sol=1.0, cash_usd=100_000)
            if m["eligible"]:
                self.assertGreater(m["effective_impact_pct"], m["impact_pct"])


class EligiblePathTests(unittest.TestCase):
    def test_a_good_candidate_passes_with_its_economics_shown(self):
        with _Enabled():
            out = evaluate_candidate(cand(), gas_balance_sol=1.0,
                                     cash_usd=100_000)
            self.assertTrue(out["eligible"], out.get("detail"))
            self.assertGreater(out["size_usd"], 0)
            self.assertIsNotNone(out["cost_r"])
            self.assertGreaterEqual(out["net_r"], MIN_NET_R)

    def test_the_gate_is_applied_to_NET_not_gross(self):
        with _Enabled():
            out = evaluate_candidate(cand(), gas_balance_sol=1.0,
                                     cash_usd=100_000)
            self.assertLess(out["net_r"], out["gross_r"])


class CopyabilityTests(unittest.TestCase):
    """Wallet execution price is not JARVIS execution price."""

    def test_the_two_prices_are_reported_separately(self):
        g = copyability_gap(wallet_price=1.00, wallet_at=1000.0,
                            jarvis_detected_at=1180.0, jarvis_quote=1.14)
        self.assertEqual(g["wallet_execution_price"], 1.00)
        self.assertEqual(g["hypothetical_jarvis_fill"], 1.14)
        self.assertNotEqual(g["wallet_execution_price"],
                            g["hypothetical_jarvis_fill"])

    def test_detection_latency_is_measured(self):
        g = copyability_gap(wallet_price=1.0, wallet_at=1000.0,
                            jarvis_detected_at=1180.0, jarvis_quote=1.1)
        self.assertAlmostEqual(g["detection_latency_s"], 180.0)

    def test_entry_decay_shows_jarvis_paying_more(self):
        """The flattering error this prevents: reporting the wallet's
        return as if JARVIS could have had it."""
        g = copyability_gap(wallet_price=1.00, wallet_at=1000.0,
                            jarvis_detected_at=1180.0, jarvis_quote=1.20)
        self.assertAlmostEqual(g["entry_decay_pct"], 20.0)
        self.assertLess(g["captured_fraction"], 1.0)

    def test_an_instant_detection_has_no_decay(self):
        g = copyability_gap(wallet_price=1.0, wallet_at=1000.0,
                            jarvis_detected_at=1000.0, jarvis_quote=1.0)
        self.assertAlmostEqual(g["entry_decay_pct"], 0.0)
        self.assertAlmostEqual(g["captured_fraction"], 1.0)


class SchedulerWiringTests(unittest.TestCase):
    def test_the_job_is_registered_and_reports_health(self):
        from app.scheduler import create_scheduler, job_status
        sched = create_scheduler()
        try:
            self.assertIn("dex_autotrade", {j.id for j in sched.get_jobs()})
            self.assertIn("dex_autotrade", job_status)
        finally:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass

    def test_the_autotrade_pass_does_not_write_surge_history(self):
        """One surge engine. An autotrade pass must not become a second
        writer of the baseline the sampler owns."""
        import inspect

        from lib import dex_autotrade
        src = inspect.getsource(dex_autotrade.run_once)
        self.assertIn("persist=False", src)


if __name__ == "__main__":
    unittest.main()
