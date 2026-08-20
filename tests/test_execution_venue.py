"""Only the adapter changes when live trading arrives.

THE DESIGN REQUIREMENT. The same canonical chain — TradeDecision ->
RiskDecision -> OrderPlan — must reach a virtual venue today and a real one
later without the strategy layer being rebuilt. If enabling Kraken means
touching signal generation, sizing or management, the architecture has
failed and the rewrite happens under time pressure with real money on the
other side.

THREE GATES, AND THE ORDER MATTERS:

    1. PLATFORM MODE     may this program place a real order at all?
    2. VENUE CAPABILITY  can this venue execute this product by API?
    3. PLAN vs RISK      is the plan still inside what risk authorised?

Mode first because it is cheapest and most consequential. Risk LAST,
immediately before submission, because everything between sizing and here
is an opportunity for a plan to drift.
"""
import os
import unittest

from lib.decision_types import OrderPlan, RiskDecision
from lib.execution_venue import (LIVE_KRAKEN, REFUSED_CAPABILITY,
                                 REFUSED_RISK, REFUSED_MODE,
                                 REFUSED_NOT_IMPLEMENTED, VIRTUAL_CEX,
                                 VIRTUAL_DEX, KrakenAdapter, adapter_for,
                                 registry, submit)
from lib.virtual_orders import Quote


def plan(qty=10.0, product="EQUITY_SPOT", symbol="NVDA", **kw):
    base = dict(symbol=symbol, venue="kraken", side="long",
                order_type="market", qty=qty, entry=100.0,
                initial_stop=98.0, product=product, notional=qty * 100.0)
    base.update(kw)
    return OrderPlan(**base)


def risk(qty=10.0):
    return RiskDecision(allowed_risk_usd=200.0, stop_distance=2.0, qty=qty,
                        notional=qty * 100.0, margin=qty * 100.0, leverage=1.0)


def quote():
    return Quote(bid=99.95, ask=100.05, as_of="now", source="test")


def setUpModule():
    """The DEX adapter now requires a funded wallet: a caller-supplied
    balance can no longer stand in for one. These boundary tests are about
    PLAN SHAPE reaching every adapter, so the wallet is funded to keep them
    measuring that rather than wallet authority (covered elsewhere)."""
    from lib import dex_wallet as DW
    if not DW.initialized():
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.SOL_MINT, quantity=5.0,
            reason="venue boundary fixture"))


class _Mode:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("JARVIS_PLATFORM_MODE")
        os.environ["JARVIS_PLATFORM_MODE"] = self.value
        return self

    def __exit__(self, *_):
        if self.old is None:
            os.environ.pop("JARVIS_PLATFORM_MODE", None)
        else:
            os.environ["JARVIS_PLATFORM_MODE"] = self.old


class VirtualExecutionTests(unittest.TestCase):
    def test_a_plan_reaches_the_virtual_cex_and_fills(self):
        r = submit(plan(), venue_family=VIRTUAL_CEX, risk=risk(), quote=quote())
        self.assertTrue(r.accepted, r.detail)
        self.assertIsNotNone(r.execution.fill_price)

    def test_the_fill_is_worse_than_the_mid(self):
        """The adapter must not quietly become a better venue than the
        fill model it wraps."""
        r = submit(plan(), venue_family=VIRTUAL_CEX, risk=risk(), quote=quote())
        self.assertGreater(r.execution.fill_price, r.execution.decision_mid)

    def test_a_market_order_without_a_quote_is_refused_not_filled(self):
        r = submit(plan(), venue_family=VIRTUAL_CEX, risk=risk())
        self.assertFalse(r.accepted)
        self.assertIn("half the spread", r.detail)

    def test_an_unknown_adapter_is_refused(self):
        self.assertFalse(submit(plan(), venue_family="NOPE").accepted)


class Gate1_PlatformModeTests(unittest.TestCase):
    def test_a_live_adapter_is_unreachable_in_virtual_only(self):
        with _Mode("VIRTUAL_ONLY"):
            r = submit(plan(product="CRYPTO_SPOT", symbol="BTC/USD"),
                       venue_family=LIVE_KRAKEN, risk=risk())
            self.assertFalse(r.accepted)
            self.assertEqual(r.reason, REFUSED_MODE)

    def test_mode_is_checked_before_capability(self):
        """A live venue must be refused for MODE even when the product is
        one it could otherwise execute — otherwise the ordering is
        accidental."""
        with _Mode("VIRTUAL_ONLY"):
            r = submit(plan(product="CRYPTO_SPOT", symbol="BTC/USD"),
                       venue_family=LIVE_KRAKEN, risk=risk(), venue="kraken")
            self.assertEqual(r.reason, REFUSED_MODE)

    def test_mode_is_checked_before_risk(self):
        """An oversized plan to a live venue in VIRTUAL_ONLY must report
        MODE, not risk — the cheaper and more consequential gate wins."""
        with _Mode("VIRTUAL_ONLY"):
            r = submit(plan(qty=999, product="CRYPTO_SPOT", symbol="BTC/USD"),
                       venue_family=LIVE_KRAKEN, risk=risk(qty=10))
            self.assertEqual(r.reason, REFUSED_MODE)

    def test_virtual_adapters_are_unaffected_by_mode(self):
        with _Mode("VIRTUAL_ONLY"):
            self.assertTrue(submit(plan(), venue_family=VIRTUAL_CEX,
                                   risk=risk(), quote=quote()).accepted)


class Gate2_CapabilityTests(unittest.TestCase):
    def test_the_dex_adapter_refuses_a_non_dex_product(self):
        r = submit(plan(product="EQUITY_SPOT"), venue_family=VIRTUAL_DEX,
                   risk=risk())
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, REFUSED_CAPABILITY)

    def test_the_cex_adapter_refuses_a_dex_product(self):
        r = submit(plan(product="DEX_SPOT"), venue_family=VIRTUAL_CEX,
                   risk=risk())
        self.assertEqual(r.reason, REFUSED_CAPABILITY)

    def test_a_live_venue_refuses_a_ui_only_product_when_mode_permits(self):
        """Kraken equity is UI_ONLY. Even with live execution enabled, the
        adapter must not route to an endpoint that does not exist."""
        with _Mode("LIVE_ENABLED"):
            r = submit(plan(product="EQUITY_SPOT"), venue_family=LIVE_KRAKEN,
                       risk=risk(), venue="kraken")
            self.assertFalse(r.accepted)
            self.assertEqual(r.reason, REFUSED_CAPABILITY)


class Gate3_RiskTests(unittest.TestCase):
    def test_a_plan_exceeding_its_risk_is_refused(self):
        r = submit(plan(qty=999), venue_family=VIRTUAL_CEX,
                   risk=risk(qty=10), quote=quote())
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, REFUSED_RISK)

    def test_it_is_refused_rather_than_clamped(self):
        """A silent clamp hides the defect that produced the oversized
        plan."""
        r = submit(plan(qty=999), venue_family=VIRTUAL_CEX,
                   risk=risk(qty=10), quote=quote())
        self.assertIsNone(r.execution)
        self.assertIn("never enlarge", r.detail)

    def test_a_plan_within_risk_passes(self):
        self.assertTrue(submit(plan(qty=5), venue_family=VIRTUAL_CEX,
                               risk=risk(qty=10), quote=quote()).accepted)

    def test_risk_is_checked_LAST(self):
        """An oversized plan for an unsupported product must report
        CAPABILITY — proving risk is not evaluated before it."""
        r = submit(plan(qty=999, product="DEX_SPOT"),
                   venue_family=VIRTUAL_CEX, risk=risk(qty=10), quote=quote())
        self.assertEqual(r.reason, REFUSED_CAPABILITY)


class KrakenBoundaryTests(unittest.TestCase):
    def test_the_adapter_is_declared_but_not_implemented(self):
        """Building untested live submission before it is needed would be a
        liability sitting in the repo waiting for a flag."""
        with _Mode("LIVE_ENABLED"):
            r = submit(plan(product="CRYPTO_SPOT", symbol="BTC/USD"),
                       venue_family=LIVE_KRAKEN, risk=risk(), venue="kraken")
            self.assertFalse(r.accepted)
            self.assertEqual(r.reason, REFUSED_NOT_IMPLEMENTED)

    def test_the_registry_says_which_adapters_are_live(self):
        reg = registry()
        self.assertTrue(reg["adapters"][LIVE_KRAKEN]["is_live"])
        self.assertFalse(reg["adapters"][LIVE_KRAKEN]["implemented"])
        self.assertFalse(reg["adapters"][VIRTUAL_CEX]["is_live"])

    def test_it_reports_capability_from_the_shared_registry(self):
        """Not a second opinion about what Kraken supports."""
        a = adapter_for(LIVE_KRAKEN)
        self.assertTrue(a.supports("CRYPTO_SPOT"))
        self.assertFalse(a.supports("EQUITY_SPOT"))


class BoundaryIntegrityTests(unittest.TestCase):
    def test_one_plan_shape_reaches_every_adapter(self):
        """THE requirement: strategy builds one OrderPlan, and adapters
        differ without it knowing."""
        p = plan(product="CRYPTO_SPOT", symbol="BTC/USD")
        results = {
            VIRTUAL_CEX: submit(p, venue_family=VIRTUAL_CEX, risk=risk(),
                                quote=quote()),
            VIRTUAL_DEX: submit(plan(product="DEX_SPOT", symbol="SOL/USDC"),
                                venue_family=VIRTUAL_DEX, risk=risk(),
                                reserve_usd=500_000.0, gas_balance_sol=1.0),
        }
        for fam, r in results.items():
            self.assertEqual(r.venue_family, fam)
            self.assertTrue(r.accepted, f"{fam}: {r.detail}")

    def test_the_dex_adapter_enforces_gas(self):
        """Two authorities, one rule: no gas, no swap.

        An UNFUNDED wallet is refused earlier and differently
        (WALLET_NOT_FUNDED) — a caller cannot invent a balance. Here the
        wallet exists and honestly holds nothing, so the refusal comes from
        the ledger.
        """
        from app.database import DexBalance, DexFundingEvent, get_db
        from lib import dex_wallet as DW
        with get_db() as db:
            db.query(DexBalance).delete()
            db.query(DexFundingEvent).delete()
        # A funded wallet that holds no SOL: real ledger, empty of gas.
        DW.fund_wallet(DW.issue_test_fixture_grant(
            mint=DW.USDC_MINT, quantity=100.0,
            reason="gas-test fixture"))
        r = submit(plan(product="DEX_SPOT", symbol="SOL/USDC"),
                   venue_family=VIRTUAL_DEX, risk=risk(),
                   reserve_usd=500_000.0, gas_balance_sol=0.0)
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "INSUFFICIENT_GAS")
        self.assertEqual(r.provenance["gas_authority"], "PERSISTED_WALLET")

    def test_a_refusal_is_always_a_result_never_an_exception(self):
        for fam in (VIRTUAL_CEX, VIRTUAL_DEX, LIVE_KRAKEN):
            r = submit(plan(product="OPTIONS"), venue_family=fam, risk=risk())
            self.assertFalse(r.accepted)
            self.assertTrue(r.reason)


if __name__ == "__main__":
    unittest.main()
