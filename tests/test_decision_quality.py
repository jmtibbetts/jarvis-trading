"""Phase C — the decision-quality contract.

The failure mode these guard against is a report that looks rigorous and
quietly answers an easier question: counting horizons as decisions, letting
hindsight pick the horizon, calling a favourable drift a missed trade, or
turning missing evidence into a loss.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lib import decision_quality as DQ

EPOCH = "TEST_EPOCH"
BOUNDARY = "2026-08-18T00:00:00+00:00"


def _db() -> str:
    """A disposable evidence DB with the two real tables."""
    p = Path(tempfile.mkdtemp()) / "ev.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE decision_observations (
        observation_id TEXT, source TEXT, engine_epoch TEXT, decision_at TEXT,
        symbol TEXT, asset_class TEXT, product TEXT, venue TEXT,
        instrument_id TEXT, side TEXT, timeframe TEXT,
        expected_hold_hours REAL, final_decision TEXT, binding_constraint TEXT,
        binding_reason TEXT, edge_gate_role TEXT, expected_net_r REAL,
        net_expected_r_lower REAL, edge_threshold_r REAL, robust INTEGER)""")
    c.execute("""CREATE TABLE decision_observation_outcomes (
        observation_id TEXT, horizon TEXT, status TEXT, product TEXT,
        instrument_id TEXT, observer_version TEXT, range_source TEXT,
        mfe_pct REAL, mae_pct REAL, direction_adjusted_mid_return_pct REAL,
        side_reference_return_pct REAL, touch_order TEXT)""")
    c.commit()
    c.close()
    return str(p)


def _obs(db, oid, **kw):
    d = dict(observation_id=oid, source=DQ.FORWARD_EVIDENCE_ONLY,
             engine_epoch=EPOCH, decision_at="2026-08-18T10:00:00+00:00",
             symbol="BTC/USD", asset_class="crypto", product="CRYPTO_PERP",
             venue="kraken_derivatives_us", instrument_id="PBTCUCZ50",
             side="long", timeframe="15m", expected_hold_hours=None,
             final_decision="NO_TRADE", binding_constraint="UNCLASSIFIED",
             binding_reason="AI_REJECTED_ENTRY", edge_gate_role="DIAGNOSTIC",
             expected_net_r=0.02, net_expected_r_lower=-0.01,
             edge_threshold_r=0.05, robust=0)
    d.update(kw)
    c = sqlite3.connect(db)
    c.execute(f"INSERT INTO decision_observations ({','.join(d)}) "
              f"VALUES ({','.join('?' * len(d))})", list(d.values()))
    c.commit(); c.close()


def _out(db, oid, horizon, **kw):
    d = dict(observation_id=oid, horizon=horizon, status="COMPLETE",
             product="CRYPTO_PERP", instrument_id="PBTCUCZ50",
             observer_version=DQ.STRICT_OBSERVER, range_source=DQ.STRICT_RANGE,
             mfe_pct=-0.06, mae_pct=-0.26,
             direction_adjusted_mid_return_pct=-0.21,
             side_reference_return_pct=-0.23, touch_order="NEITHER")
    d.update(kw)
    c = sqlite3.connect(db)
    c.execute(f"INSERT INTO decision_observation_outcomes ({','.join(d)}) "
              f"VALUES ({','.join('?' * len(d))})", list(d.values()))
    c.commit(); c.close()


def _report(db):
    return DQ.build_report(db, epoch=EPOCH, boundary=BOUNDARY)


class StatisticalUnitTests(unittest.TestCase):
    def test_many_horizons_are_one_decision(self):
        db = _db()
        _obs(db, "d1")
        for h in ("15m", "1h", "4h"):
            _out(db, "d1", h)
        r = _report(db)
        self.assertEqual(r["denominators"]["total_decisions"], 1)
        self.assertEqual(r["denominators"]["outcome_rows_total"], 3)
        for h in ("15m", "1h", "4h"):
            self.assertEqual(r["horizon_curve"][h]["distinct_decisions"], 1)


class PrimaryHorizonTests(unittest.TestCase):
    def test_it_comes_from_the_timeframe_not_from_outcomes(self):
        self.assertEqual(DQ.primary_horizon(timeframe="15m",
                                            expected_hold_hours=None), "15m")

    def test_expected_hold_wins_when_present(self):
        self.assertEqual(DQ.primary_horizon(timeframe="15m",
                                            expected_hold_hours=4.0), "4h")

    def test_an_unknown_timeframe_uses_one_explicit_fallback(self):
        self.assertEqual(DQ.primary_horizon(timeframe="wat",
                                            expected_hold_hours=None),
                         DQ.PRIMARY_HORIZON_FALLBACK)

    def test_a_better_later_horizon_cannot_become_primary(self):
        """The selection must not see outcomes at all."""
        db = _db()
        _obs(db, "d1", timeframe="15m")
        _out(db, "d1", "15m", mfe_pct=-0.5)          # the honest one
        _out(db, "d1", "4h", mfe_pct=99.0)           # the flattering one
        r = _report(db)
        self.assertEqual(r["excursions"]["mfe_pct"]["max"], -0.5)

    def test_the_policy_is_versioned_in_the_report(self):
        db = _db(); _obs(db, "d1")
        self.assertEqual(_report(db)["meta"]["primary_horizon_policy_version"],
                         DQ.PRIMARY_HORIZON_POLICY_VERSION)


class PopulationTests(unittest.TestCase):
    def test_legacy_and_replay_sources_are_excluded(self):
        db = _db()
        _obs(db, "fwd")
        _obs(db, "old", source="LEGACY_FORWARD_VIRTUAL")
        _obs(db, "replay", source="COUNTERFACTUAL_REPLAY")
        self.assertEqual(_report(db)["denominators"]["total_decisions"], 1)

    def test_another_epoch_is_excluded(self):
        db = _db(); _obs(db, "a"); _obs(db, "b", engine_epoch="OTHER")
        self.assertEqual(_report(db)["denominators"]["total_decisions"], 1)

    def test_rows_before_the_boundary_are_excluded(self):
        db = _db(); _obs(db, "a")
        _obs(db, "old", decision_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(_report(db)["denominators"]["total_decisions"], 1)

    def test_missing_routing_identity_stays_in_the_denominator(self):
        """The 95 pre-fix rows are visible, not quietly filtered away."""
        db = _db()
        _obs(db, "native")
        _obs(db, "legacy", product=None, venue=None, asset_class=None)
        r = _report(db)
        self.assertEqual(r["denominators"]["total_decisions"], 2)
        q = r["routing_identity_quality"]
        self.assertEqual(q[DQ.NATIVE_ROUTING_IDENTITY], 1)
        self.assertEqual(q[DQ.LEGACY_MISSING_ROUTING_IDENTITY], 1)


class DiagnosticEdgeTests(unittest.TestCase):
    def test_a_diagnostic_edge_can_never_be_the_binding_constraint(self):
        db = _db()
        _obs(db, "amd", binding_constraint="EDGE", edge_gate_role="DIAGNOSTIC")
        self.assertEqual(_report(db)["binding_constraints"].get("EDGE"), None)

    def test_a_binding_edge_still_reports_as_edge(self):
        db = _db()
        _obs(db, "e", binding_constraint="EDGE", edge_gate_role="BINDING")
        self.assertEqual(_report(db)["binding_constraints"]["EDGE"], 1)

    def test_the_amd_case_remains_a_valid_trade(self):
        """net 0.02R under a 0.05R floor, negative lower bound, still TRADE."""
        db = _db()
        _obs(db, "amd", final_decision="TRADE", binding_constraint="NONE",
             expected_net_r=0.02, net_expected_r_lower=-0.01)
        r = _report(db)
        self.assertEqual(r["verdicts"]["TRADE"], 1)
        self.assertIsNone(r["binding_constraints"].get("EDGE"))


class ResolutionSemanticsTests(unittest.TestCase):
    def test_strict_and_pre_strict_are_never_merged(self):
        db = _db()
        _obs(db, "strict"); _out(db, "strict", "15m")
        _obs(db, "pre"); _out(db, "pre", "15m",
                              observer_version="decision_outcome_observer_v1",
                              range_source="range_collector_v2_samples")
        q = _report(db)["resolution_quality"]
        self.assertEqual(q[DQ.STRICT_EXACT_INSTRUMENT], 1)
        self.assertEqual(q[DQ.PRE_STRICT_INSTRUMENT_RESOLUTION], 1)

    def test_a_perp_without_a_contract_is_flagged(self):
        db = _db()
        _obs(db, "x"); _out(db, "x", "15m", instrument_id=None)
        self.assertEqual(
            _report(db)["resolution_quality"][DQ.MISSING_EXACT_INSTRUMENT], 1)


class ExcursionTests(unittest.TestCase):
    def test_a_negative_mfe_is_preserved_not_clamped(self):
        """The live BTC proof: best excursion still below T0."""
        db = _db(); _obs(db, "d"); _out(db, "d", "15m", mfe_pct=-0.0613)
        self.assertEqual(_report(db)["excursions"]["mfe_pct"]["min"], -0.0613)

    def test_missing_excursions_stay_missing(self):
        db = _db(); _obs(db, "d")
        _out(db, "d", "15m", status="INSUFFICIENT_DATA",
             mfe_pct=None, mae_pct=None)
        self.assertEqual(_report(db)["excursions"]["mfe_pct"]["n"], 0)

    def test_unresolved_evidence_is_not_counted_as_a_loss(self):
        db = _db(); _obs(db, "d")
        _out(db, "d", "15m", status="INSUFFICIENT_DATA")
        r = _report(db)
        self.assertEqual(r["denominators"]["primary_horizon_complete"], 0)
        self.assertEqual(r["primary_horizon_states"]["INSUFFICIENT_DATA"], 1)


class FavorabilityTests(unittest.TestCase):
    def test_the_two_bases_are_reported_separately(self):
        db = _db(); _obs(db, "d")
        _out(db, "d", "15m", direction_adjusted_mid_return_pct=0.5,
             side_reference_return_pct=-0.2)
        f = _report(db)["favorable_after_rejection"]
        self.assertEqual(f["market_direction_favorable"], 1)
        self.assertEqual(f["side_reference_favorable"], 0)

    def test_the_report_never_emits_false_negative(self):
        import json
        db = _db(); _obs(db, "d"); _out(db, "d", "15m")
        emitted = json.dumps(_report(db))
        self.assertNotIn("FALSE_NEGATIVE", emitted)
        self.assertIn("FAVORABLE_AFTER_REJECTION", emitted)


class ThresholdTests(unittest.TestCase):
    def test_the_current_floor_is_in_the_grid_and_marked(self):
        db = _db(); _obs(db, "d")
        cur = [t for t in _report(db)["threshold_sensitivity"]
               if t["is_current_policy"]]
        self.assertEqual(len(cur), 1)
        self.assertAlmostEqual(cur[0]["threshold_r"], 0.05)

    def test_fifty_basis_is_never_the_current_threshold(self):
        db = _db(); _obs(db, "d")
        r = _report(db)
        self.assertAlmostEqual(r["meta"]["expectancy_min_net_r"], 0.05)
        self.assertAlmostEqual(
            r["meta"]["historical_modelled_round_trip_cost_r"], 0.50)
        half = [t for t in r["threshold_sensitivity"]
                if abs(t["threshold_r"] - 0.50) < 1e-9][0]
        self.assertFalse(half["is_current_policy"])

    def test_point_and_robust_are_separate_columns(self):
        db = _db()
        _obs(db, "d", expected_net_r=0.12, net_expected_r_lower=0.01)
        row = [t for t in _report(db)["threshold_sensitivity"]
               if abs(t["threshold_r"] - 0.05) < 1e-9][0]
        self.assertEqual(row["would_clear_point_edge_at_t"], 1)
        self.assertEqual(row["would_clear_robust_edge_at_t"], 0)

    def test_clearing_edge_is_not_called_would_have_traded(self):
        import inspect
        self.assertNotIn("WOULD_HAVE_TRADED", inspect.getsource(DQ))


class ReadOnlyTests(unittest.TestCase):
    def test_analytics_open_the_database_read_only(self):
        import inspect
        src = inspect.getsource(DQ)
        self.assertIn("?mode=ro", src)
        self.assertIn("PRAGMA query_only=ON", src)

    def test_analytics_import_no_orm(self):
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("lib/decision_quality.py").read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                self.assertFalse(n.module.startswith("app"), n.module)

    def test_no_policy_change_is_authorized(self):
        db = _db(); _obs(db, "d")
        self.assertFalse(_report(db)["meta"]["policy_changes_authorized"])


class CheapAssetSanityTests(unittest.TestCase):
    def test_a_small_dollar_move_is_not_a_small_percentage(self):
        pct = (0.044241 - 0.043241) / 0.043241 * 100
        self.assertAlmostEqual(pct, 2.3126, places=3)


if __name__ == "__main__":
    unittest.main()
