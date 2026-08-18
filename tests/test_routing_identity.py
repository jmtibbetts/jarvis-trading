"""Frozen T0 routing identity — every terminal route, and the conflict guard.

The defect these pin: 95 of 95 live observations had NULL product and venue,
so 9,622 BTC/USD samples could not be joined to the decisions beside them.
Four separate funnel routes each had to be threaded; missing any ONE splits
the prospective dataset by verdict, which is harder to notice than all of it
being NULL.
"""
import unittest

from app.database import DecisionObservation, get_db
from lib import decision_funnel as DF
from lib import routing_identity as RI


def _clear():
    with get_db() as db:
        db.query(DecisionObservation).delete()


def _sig(sid, symbol="BTC/USD", **kw):
    d = {"id": sid, "asset_symbol": symbol, "paper_direction": "long",
         "entry_price": 100.0, "stop_loss": 95.0, "target_price": 110.0,
         "generated_at": "2026-08-18T10:00:00+00:00"}
    d.update(kw)
    return d


def _row():
    with get_db() as db:
        r = db.query(DecisionObservation).first()
        return {c.name: getattr(r, c.name) for c in r.__table__.columns}


class ResolutionTests(unittest.TestCase):
    def test_btc_resolves_to_the_listed_perpetual_contract(self):
        i = RI.resolve_execution_identity("BTC/USD", "crypto")
        self.assertEqual(i.asset_class, "crypto")
        self.assertEqual(i.product, "CRYPTO_PERP")
        self.assertEqual(i.venue, "kraken_derivatives_us")
        self.assertEqual(i.instrument_id, "PBTCUCZ50")

    def test_equity_resolves_to_alpaca_spot(self):
        i = RI.resolve_execution_identity("AMD", "equity")
        self.assertEqual(i.product, "EQUITY_SPOT")
        self.assertEqual(i.venue, "alpaca")

    def test_an_unlisted_perp_keeps_its_intended_product(self):
        """NEAR is not listed; capability refuses LATER, identity holds."""
        i = RI.resolve_execution_identity("NEAR/USD", "crypto")
        self.assertEqual(i.product, "CRYPTO_PERP")
        self.assertIsNone(i.instrument_id)
        self.assertNotEqual(i.product, "CRYPTO_SPOT")   # no capability fallback

    def test_no_market_data_is_read(self):
        """Identity is classification, not readiness."""
        import ast
        import pathlib
        # AST, not text: the module DOCSTRING legitimately names
        # execution_readiness while explaining why it is kept separate, and a
        # substring scan would fail on the explanation rather than the code.
        tree = ast.parse(pathlib.Path("lib/routing_identity.py").read_text())
        banned = {"execution_snapshot", "kraken_stream",
                  "bitnomial_market_data", "range_collector"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported |= {b for b in banned if b in node.module}
                imported |= {a.name for a in node.names} & banned
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported |= {b for b in banned if b in a.name}
        self.assertEqual(imported, set())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("execution_readiness", called)

    def test_an_unknown_symbol_is_unresolved_not_guessed(self):
        i = RI.resolve_execution_identity("ZZZZ_NOT_A_SYMBOL", None)
        self.assertIn(i.identity_status, (RI.UNRESOLVED, RI.PARTIAL))
        self.assertNotEqual(i.product, "EQUITY_SPOT")


class ConflictGuardTests(unittest.TestCase):
    """The guard that was dead code: it read ready.instrument_id, but
    ExecutionReadiness names the field `instrument`."""

    def _ident(self):
        return RI.resolve_execution_identity("BTC/USD", "crypto")

    def test_a_different_contract_is_a_conflict(self):
        with self.assertRaises(RI.RoutingIdentityConflict):
            self._ident().assert_agrees_with(instrument_id="DIFFERENT_CONTRACT")

    def test_the_same_contract_agrees(self):
        self._ident().assert_agrees_with(instrument_id="PBTCUCZ50")

    def test_absence_on_one_side_is_not_a_conflict(self):
        self._ident().assert_agrees_with(instrument_id=None)
        RI.RoutingIdentity(symbol="X").assert_agrees_with(instrument_id="ANY")

    def test_perp_settled_as_spot_is_a_conflict(self):
        with self.assertRaises(RI.RoutingIdentityConflict):
            self._ident().assert_agrees_with(product="CRYPTO_SPOT")

    def test_a_different_venue_is_a_conflict(self):
        with self.assertRaises(RI.RoutingIdentityConflict):
            self._ident().assert_agrees_with(venue="kraken")

    def test_build_reads_the_attribute_that_actually_exists(self):
        """Regression: getattr(ready, 'instrument_id') was always None."""
        import pathlib
        src = pathlib.Path("lib/decision_observation.py").read_text()
        self.assertIn('getattr(ready, "instrument", None)', src)
        self.assertNotIn('instrument_id=getattr(ready, "instrument_id"', src)


class EveryTerminalRouteCarriesIdentityTests(unittest.TestCase):
    """Missing ONE route splits the dataset by verdict."""

    def setUp(self):
        _clear()

    def _assert_identity(self, expect_product="CRYPTO_PERP"):
        r = _row()
        self.assertEqual(r["asset_class"], "crypto")
        self.assertEqual(r["product"], expect_product)
        self.assertEqual(r["venue"], "kraken_derivatives_us")
        self.assertEqual(r["instrument_id"], "PBTCUCZ50")
        self.assertIn("routing_identity", r["provenance"] or "")
        return r

    def _ident(self):
        return RI.resolve_execution_identity("BTC/USD", "crypto")

    def test_ai_rejection_carries_identity(self):
        DF.observe_terminal_refusal(
            _sig("r-ai"), decision="NO_TRADE", reason=DF.AI_REJECTED_ENTRY,
            decision_price=100.0, routing_identity=self._ident())
        self.assertEqual(self._assert_identity()["final_decision"], "NO_TRADE")

    def test_evidence_only_trade_carries_identity(self):
        """The hole that would have split the data by verdict."""
        from lib import decision_observation as DO
        DF.observe_terminal_refusal(
            _sig("r-eo"), decision="TRADE",
            reason="EXECUTION_SUPPRESSED_BY_MODE", decision_price=100.0,
            source=DO.FORWARD_EVIDENCE_ONLY,
            execution_state=DO.EXEC_SUPPRESSED,
            routing_identity=self._ident())
        r = self._assert_identity()
        self.assertEqual(r["final_decision"], "TRADE")
        self.assertEqual(r["execution_state"], DO.EXEC_SUPPRESSED)
        self.assertIsNone(r["position_id"])
        self.assertIsNone(r["execution_id"])

    def test_auto_trade_disabled_carries_identity(self):
        DF.observe_terminal_refusal(
            _sig("r-atd"), decision="ABSTAIN",
            reason=DF.AUTO_TRADE_DISABLED, routing_identity=self._ident())
        self._assert_identity()

    def test_already_open_carries_identity(self):
        DF.observe_terminal_refusal(
            _sig("r-open"), decision="ABSTAIN", reason=DF.ALREADY_OPEN,
            routing_identity=self._ident())
        self._assert_identity()

    def test_no_price_carries_identity(self):
        DF.observe_terminal_refusal(
            _sig("r-np"), decision="ABSTAIN", reason=DF.NO_DECISION_PRICE,
            venue_failure=True, routing_identity=self._ident())
        self._assert_identity()


class WiringTests(unittest.TestCase):
    """AST: the call sites actually pass it, not just accept it."""

    def test_every_funnel_call_in_the_job_passes_routing_identity(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("jobs/paper_trading.py").read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "observe_terminal_refusal"]
        self.assertGreaterEqual(len(calls), 4)
        for c in calls:
            kws = {k.arg for k in c.keywords}
            self.assertIn("routing_identity", kws,
                          "a terminal route drops identity and would split "
                          "the prospective dataset by verdict")

    def test_canonical_entry_receives_the_frozen_identity(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("jobs/paper_trading.py").read_text())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "open_canonical_position"):
                self.assertIn("routing_identity", {k.arg for k in n.keywords})
                return
        self.fail("open_canonical_position call not found")

    def test_the_display_fallback_is_not_routing_truth(self):
        """A missing class must not freeze unknown crypto as EQUITY_SPOT."""
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text()
        self.assertIn("routing_asset_class_hint", src)
        self.assertIn('s.asset_class or ("Futures" if is_futures else None)', src)


if __name__ == "__main__":
    unittest.main()


class ReadinessConsumesFrozenIdentityTests(unittest.TestCase):
    """Readiness answers WHETHER EXECUTABLE. It does not re-answer WHAT."""

    def setUp(self):
        from lib import execution_policy as EP
        self.EP = EP
        self.calls = []
        self._orig = (EP.resolve_product, EP.resolve_execution_venue,
                      EP._instrument_id)

        def spy(name, real):
            def f(*a, **k):
                self.calls.append(name)
                return real(*a, **k)
            return f
        EP.resolve_product = spy("resolve_product", self._orig[0])
        EP.resolve_execution_venue = spy("resolve_venue", self._orig[1])
        EP._instrument_id = spy("instrument_id", self._orig[2])

    def tearDown(self):
        (self.EP.resolve_product, self.EP.resolve_execution_venue,
         self.EP._instrument_id) = self._orig

    def test_a_supplied_identity_stops_all_re_resolution(self):
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        self.calls.clear()
        r = self.EP.execution_readiness("BTC/USD", "crypto",
                                        routing_identity=ident)
        self.assertEqual(self.calls, [], "readiness re-derived the identity")
        self.assertEqual(r.product, "CRYPTO_PERP")
        self.assertEqual(r.venue, "kraken_derivatives_us")
        self.assertEqual(r.instrument, "PBTCUCZ50")

    def test_the_legacy_path_still_resolves_normally(self):
        self.calls.clear()
        self.EP.execution_readiness("BTC/USD", "crypto")
        self.assertIn("resolve_product", self.calls)

    def test_config_drift_after_freezing_cannot_change_the_product(self):
        """The entire reason this artifact exists."""
        import os
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        self.assertEqual(ident.product, "CRYPTO_PERP")
        prior = os.environ.get("CRYPTO_PRODUCT")
        os.environ["CRYPTO_PRODUCT"] = "spot"
        try:
            r = self.EP.execution_readiness("BTC/USD", "crypto",
                                            routing_identity=ident)
            self.assertEqual(r.product, "CRYPTO_PERP")
            self.assertEqual(r.venue, "kraken_derivatives_us")
        finally:
            if prior is None:
                os.environ.pop("CRYPTO_PRODUCT", None)
            else:
                os.environ["CRYPTO_PRODUCT"] = prior

    def test_readiness_for_a_different_symbol_is_a_conflict(self):
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        with self.assertRaises(RI.RoutingIdentityConflict):
            self.EP.execution_readiness("ETH/USD", "crypto",
                                        routing_identity=ident)

    def test_a_different_asset_class_is_a_conflict(self):
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        with self.assertRaises(RI.RoutingIdentityConflict):
            self.EP.execution_readiness("BTC/USD", "equity",
                                        routing_identity=ident)

    def test_an_unlisted_perp_is_refused_not_downgraded(self):
        ident = RI.resolve_execution_identity("NEAR/USD", "crypto")
        r = self.EP.execution_readiness("NEAR/USD", "crypto",
                                        routing_identity=ident)
        self.assertEqual(r.product, "CRYPTO_PERP")
        self.assertNotEqual(r.product, "CRYPTO_SPOT")


class DisplayAssetClassIsNotRoutingTruthTests(unittest.TestCase):
    """The paper dict labels anything non-futures "Equity" for display.

    Handing that to readiness as an independent class would make the conflict
    guard fire on the DISPLAY field rather than on a real disagreement —
    refusing a correctly-frozen crypto decision for cosmetic reasons.
    """

    def test_canonical_entry_uses_the_frozen_class_not_the_display_one(self):
        import pathlib
        src = pathlib.Path("lib/canonical_entry.py").read_text()
        self.assertIn("readiness_asset_class", src)
        self.assertIn("routing_identity.asset_class", src)
        self.assertNotIn(
            "POL.execution_readiness(symbol, asset_class, signal=signal,", src)

    def test_a_crypto_identity_survives_an_equity_display_label(self):
        from lib import execution_policy as EP
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        # exactly the legacy shape: display says Equity, identity says crypto
        r = EP.execution_readiness("BTC/USD", ident.asset_class,
                                   signal={"asset_class": "Equity"},
                                   routing_identity=ident)
        self.assertEqual(r.product, "CRYPTO_PERP")
        self.assertEqual(r.asset_class, "crypto")

    def test_a_genuine_disagreement_still_conflicts(self):
        """The guard must stay live for real authoritative disagreement."""
        from lib import execution_policy as EP
        ident = RI.resolve_execution_identity("BTC/USD", "crypto")
        with self.assertRaises(RI.RoutingIdentityConflict):
            EP.execution_readiness("BTC/USD", "equity", routing_identity=ident)
