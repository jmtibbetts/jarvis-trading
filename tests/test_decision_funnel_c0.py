"""C0 — every material terminal decision leaves exactly ONE observation,
and EVIDENCE_ONLY is structurally unable to touch the book.
"""
import os
import unittest

from app.database import DecisionObservation, get_db
from lib import decision_funnel as DF
from lib import decision_observation as DO
from lib import runtime_mode as RM


def _clear():
    with get_db() as db:
        db.query(DecisionObservation).delete()


def _sig(sid="sig-c0", symbol="BTC/USD", direction="long"):
    return {"id": sid, "asset_symbol": symbol, "asset_class": "crypto",
            "paper_direction": direction, "timeframe": "15m",
            "entry_price": 100.0, "stop_loss": 95.0, "target_price": 110.0,
            "generated_at": "2026-08-18T00:00:00+00:00"}


def _rows(oid=None):
    with get_db() as db:
        q = db.query(DecisionObservation)
        if oid:
            q = q.filter(DecisionObservation.observation_id == oid)
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns}
                for r in q.all()]


class FunnelCoverageTests(unittest.TestCase):
    """The two paths that used to count a candidate and discard it."""

    def setUp(self):
        _clear()

    def test_ai_rejection_leaves_exactly_one_observation(self):
        oid = DF.observe_terminal_refusal(
            _sig("sig-ai"), decision="NO_TRADE",
            reason=DF.AI_REJECTED_ENTRY, decision_price=100.0,
            gates={"ai_entry_review": "FAIL"})
        self.assertIsNotNone(oid)
        rows = _rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_decision"], "NO_TRADE")
        self.assertEqual(rows[0]["binding_reason"], DF.AI_REJECTED_ENTRY)

    def test_missing_price_is_a_data_refusal_not_a_thesis_verdict(self):
        DF.observe_terminal_refusal(
            _sig("sig-nop"), decision="ABSTAIN",
            reason=DF.NO_DECISION_PRICE, venue_failure=True)
        rows = _rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["final_decision"], "ABSTAIN")
        self.assertTrue(rows[0]["venue_data_failure"])
        # A venue/data failure must never land against the thesis as EDGE.
        self.assertNotEqual(rows[0]["binding_constraint"], DO.EDGE)

    def test_one_event_cannot_vote_twice(self):
        s = _sig("sig-dupe")
        first = DF.observe_terminal_refusal(
            s, decision="NO_TRADE", reason=DF.AI_REJECTED_ENTRY,
            decision_price=100.0)
        second = DF.observe_terminal_refusal(
            s, decision="NO_TRADE", reason=DF.AI_REJECTED_ENTRY,
            decision_price=100.0)
        self.assertEqual(first, second)
        self.assertEqual(len(_rows()), 1)

    def test_a_recording_failure_never_kills_the_cycle(self):
        # a signal with nothing usable must not raise into the caller
        self.assertIsNone(DF.observe_terminal_refusal(
            {}, decision="NO_TRADE", reason="X")) if False else None
        try:
            DF.observe_terminal_refusal({}, decision="NO_TRADE", reason="X")
        except Exception as e:
            self.fail(f"refusal recording raised into the caller: {e}")

    def test_the_paper_job_actually_calls_the_funnel_observer(self):
        """AST: the wiring exists, not merely the helper."""
        import ast
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text(encoding="utf-8")
        calls = [n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "observe_terminal_refusal"]
        self.assertGreaterEqual(len(calls), 2)


class EvidenceOnlySemanticsTests(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        os.environ.pop("JARVIS_RUNTIME_MODE", None)

    def test_default_mode_is_full_virtual(self):
        os.environ.pop("JARVIS_RUNTIME_MODE", None)
        self.assertEqual(RM.current_mode(), RM.FULL_VIRTUAL)
        self.assertFalse(RM.is_evidence_only())

    def test_evidence_only_refuses_every_economic_mutation(self):
        os.environ["JARVIS_RUNTIME_MODE"] = RM.EVIDENCE_ONLY
        from lib import paper_engine
        for fn, args in (("prepare_entry", ({"asset_symbol": "BTC/USD"},)),
                         ("close_paper_position", ("x", 1.0)),
                         ("partial_close_paper_position", ("x", 0.5, 1.0))):
            with self.assertRaises(RM.EconomicMutationForbidden):
                getattr(paper_engine, fn)(*args)

    def test_the_guard_lives_at_the_mutation_not_the_caller(self):
        """AST: each economic entry point calls the guard itself."""
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("lib/paper_engine.py").read_text(encoding="utf-8"))
        need = {"prepare_entry", "settle_position_entry",
                "close_paper_position", "partial_close_paper_position"}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in need:
                calls = {n.func.id for n in ast.walk(node)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                self.assertIn("forbid_economic_mutation", calls,
                              f"{node.name} does not guard itself")
                need.discard(node.name)
        self.assertEqual(need, set(), f"never checked: {need}")

    def test_evidence_only_refusal_carries_the_evidence_only_source(self):
        os.environ["JARVIS_RUNTIME_MODE"] = RM.EVIDENCE_ONLY
        DF.observe_terminal_refusal(_sig("sig-eo"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0)
        self.assertEqual(_rows()[0]["source"], DO.FORWARD_EVIDENCE_ONLY)

    def test_suppressed_is_terminal_and_never_becomes_settled(self):
        with self.assertRaises(DO.LifecycleConflict):
            DO.next_execution_state(DO.EXEC_SUPPRESSED, DO.EXEC_SETTLED)

    def test_suppressed_is_not_settlement_failure(self):
        """Nothing failed — execution was deliberately not permitted."""
        self.assertNotEqual(DO.EXEC_SUPPRESSED, DO.EXEC_SETTLEMENT_FAILED)

    def test_evidence_only_trade_never_calibrates_fills(self):
        row = {"source": DO.FORWARD_EVIDENCE_ONLY, "final_decision": DO.TRADE,
               "execution_id": "e", "execution_state": DO.EXEC_SETTLED,
               "position_id": "p"}
        self.assertFalse(DO.is_execution_calibration_eligible(row))
        self.assertNotIn(DO.FORWARD_EVIDENCE_ONLY, DO.FORWARD_EXECUTED_SOURCES)


class ThresholdReconciliationTests(unittest.TestCase):
    """C0.2 — 0.05R and ~0.50R are DIFFERENT QUANTITIES, not two thresholds.

    `MIN_NET_R = 0.05` is a NET-expectancy floor applied AFTER costs. The
    historical ~0.50R is a modelled ROUND-TRIP COST in R, not a gate — the
    replay found max gross edge ~0.42R against a cost floor near 0.50R, i.e.
    costs exceeded the best available gross edge. Calling it "the threshold"
    invited someone to lower a number that does not exist.
    """

    def test_current_expectancy_authority_is_a_net_r_floor_of_0_05(self):
        from lib.expectancy import MIN_NET_R
        self.assertAlmostEqual(MIN_NET_R, 0.05, places=6)

    def test_the_cost_matrix_shares_that_same_net_floor(self):
        from lib.edge_cost_matrix import MIN_NET_R as CM_MIN
        from lib.expectancy import MIN_NET_R as EX_MIN
        self.assertAlmostEqual(CM_MIN, EX_MIN, places=6)

    def test_no_module_defines_a_0_50r_edge_threshold(self):
        """The number people call 'the 0.50R threshold' is not a gate."""
        import lib.edge_cost_matrix as ECM
        import lib.expectancy as EX
        for mod in (EX, ECM):
            for name in dir(mod):
                if name.startswith("MIN") or "THRESH" in name.upper():
                    v = getattr(mod, name)
                    if isinstance(v, (int, float)):
                        self.assertNotAlmostEqual(
                            float(v), 0.50, places=6,
                            msg=f"{mod.__name__}.{name} == 0.50 — if this is "
                                f"real, the reconciliation is wrong")

    def test_cost_limited_and_edge_limited_are_distinct_verdicts(self):
        from lib.edge_cost_matrix import LIMIT_COST, LIMIT_EDGE
        self.assertNotEqual(LIMIT_COST, LIMIT_EDGE)


if __name__ == "__main__":
    unittest.main()


class T0EdgeArtifactTests(unittest.TestCase):
    """C0.3 — the stored numbers are the ones the decision actually used."""

    def setUp(self):
        _clear()

    def _edge(self, net=0.12, lower=0.07, thr=0.05, robust=True):
        from lib.decision_types import MeasuredEdge
        return MeasuredEdge.from_expectancy({
            "verdict": "TRADE", "robust": robust, "threshold_used": thr,
            "net": {"net_expected_r": net},
            "net_lower": {"net_expected_r": lower},
            "costs": {"cost_r": 0.09},
            "expectancy": {"bucket": "crypto/15m", "raw_sample": 40,
                           "sample": 38.5, "gross_expected_r": 0.21},
        })

    def test_expectancy_freezes_the_threshold_into_its_result(self):
        from lib.expectancy import MIN_NET_R
        e = self._edge()
        self.assertAlmostEqual(e.threshold_used, 0.05, places=6)
        self.assertAlmostEqual(e.threshold_used, MIN_NET_R, places=6)

    def test_original_net_is_stored_not_recomputed_from_gross_minus_cost(self):
        """gross - cost would give 0.12; the artifact says 0.99. Artifact wins."""
        e = self._edge(net=0.99)
        DF.observe_terminal_refusal(_sig("sig-net"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=e)
        r = _rows()[0]
        self.assertAlmostEqual(r["expected_net_r"], 0.99, places=6)

    def test_lower_bound_and_robust_survive_into_the_row(self):
        DF.observe_terminal_refusal(_sig("sig-lb"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0,
                                    edge=self._edge(lower=0.07, robust=True))
        r = _rows()[0]
        self.assertAlmostEqual(r["net_expected_r_lower"], 0.07, places=6)
        self.assertTrue(r["robust"])

    def test_point_pass_with_failing_lower_bound_is_not_robust(self):
        e = self._edge(net=0.12, lower=0.01, robust=False)
        DF.observe_terminal_refusal(_sig("sig-unc"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=e)
        r = _rows()[0]
        self.assertFalse(r["robust"])
        self.assertGreater(r["distance_to_threshold_r"], 0)          # point clears
        self.assertLess(r["robust_distance_to_threshold_r"], 0)      # bound does not

    def test_distances_come_from_the_stored_threshold(self):
        DF.observe_terminal_refusal(_sig("sig-d"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=self._edge())
        r = _rows()[0]
        self.assertAlmostEqual(r["distance_to_threshold_r"], 0.07, places=6)
        self.assertAlmostEqual(r["robust_distance_to_threshold_r"], 0.02, places=6)

    def test_expectancy_provenance_is_kept(self):
        DF.observe_terminal_refusal(_sig("sig-prov"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=self._edge())
        r = _rows()[0]
        self.assertEqual(r["expectancy_bucket"], "crypto/15m")
        self.assertEqual(r["expectancy_raw_sample"], 40)
        self.assertEqual(r["expectancy_verdict"], "TRADE")

    def test_a_measured_edge_is_diagnostic_not_binding_by_default(self):
        """The paper path never used the edge gate to refuse anything."""
        DF.observe_terminal_refusal(_sig("sig-role"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=self._edge())
        self.assertEqual(_rows()[0]["edge_gate_role"], DO.EDGE_DIAGNOSTIC)

    def test_no_edge_at_all_is_not_evaluated_never_diagnostic(self):
        DF.observe_terminal_refusal(_sig("sig-none"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0)
        self.assertEqual(_rows()[0]["edge_gate_role"], DO.EDGE_NOT_EVALUATED)

    def test_diagnostic_edge_must_not_be_reported_as_binding_edge(self):
        """Phase C may not blame EDGE for a refusal edge never caused."""
        DF.observe_terminal_refusal(_sig("sig-blame"), decision="NO_TRADE",
                                    reason=DF.AI_REJECTED_ENTRY,
                                    decision_price=100.0, edge=self._edge())
        r = _rows()[0]
        self.assertEqual(r["edge_gate_role"], DO.EDGE_DIAGNOSTIC)
        self.assertNotEqual(r["binding_constraint"], DO.EDGE)

    def test_the_edge_mapping_exists_in_exactly_one_place(self):
        import pathlib
        src = pathlib.Path("lib/decision_funnel.py").read_text(encoding="utf-8")
        self.assertNotIn("def _edge_fields", src)


class RemainingFunnelHoleTests(unittest.TestCase):
    """C0.4 — the two paths that still deleted candidates."""

    def setUp(self):
        _clear()

    def tearDown(self):
        os.environ.pop("JARVIS_RUNTIME_MODE", None)

    def test_the_silent_open_symbol_prefilter_is_gone(self):
        """The list comprehension that deleted already-open candidates."""
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text(encoding="utf-8")
        self.assertNotIn("not in open_syms]", src)

    def test_already_open_is_account_state_not_a_thesis_verdict(self):
        DF.observe_terminal_refusal(_sig("sig-open"), decision="ABSTAIN",
                                    reason=DF.ALREADY_OPEN)
        r = _rows()[0]
        self.assertEqual(r["binding_reason"], DF.ALREADY_OPEN)
        self.assertNotEqual(r["binding_constraint"], DO.EDGE)

    def test_evidence_only_still_evaluates_an_already_held_symbol(self):
        """667 legacy positions must not filter a clean research epoch."""
        import ast
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text(encoding="utf-8")
        self.assertIn("if sym in open_syms and not RM.is_evidence_only():", src)
        ast.parse(src)

    def test_auto_trade_disabled_leaves_evidence_in_full_virtual(self):
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text(encoding="utf-8")
        self.assertIn("AUTO_TRADE_DISABLED", src)
        self.assertIn("if not auto_trade_enabled and not _evidence_only:", src)

    def test_do_not_trade_does_not_mean_do_not_think(self):
        """In EVIDENCE_ONLY the economic switch must not empty the list."""
        import pathlib
        src = pathlib.Path("jobs/paper_trading.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "sig_list = _get_pending_signals(db) if auto_trade_enabled else []",
            src)


if __name__ == "__main__":
    unittest.main()
