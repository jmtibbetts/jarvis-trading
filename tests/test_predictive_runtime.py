"""Inference that fails closed, on whichever device the measurements favour.

Every gate here is a way a prediction can be wrong while looking fine:

  A schema mismatch has the right dimension and the wrong meaning. The model
  consumes it happily and returns confident nonsense.

  Stale features describe a market that has moved on. Nothing about the
  array says so.

  Zero standing in for a missing feature is a claim — "funding is exactly
  neutral" — and is indistinguishable from the real value once it is in
  the vector.

  A dead device that silently returns a default turns a broken model into a
  losing trade, and nothing downstream can tell it from a working one.

The rule the whole module exists to enforce: an unavailable prediction is
ABSTAIN, never a number.
"""
import unittest

import numpy as np

from lib.predictive import device_policy as policy
from lib.predictive.features import build
from lib.predictive.runtime import (ABSTAIN, OK, DEFAULT_MAX_AGE_S,
                                    PredictiveRuntime)
from lib.predictive.schemas import (CURRENT_SCHEMA, FeatureVector, SCHEMAS,
                                    dimension, feature_names, schema_hash)

HAVE_OV = True
try:
    import openvino as ov
    import openvino.opset13 as ops
except ImportError:                                    # pragma: no cover
    HAVE_OV = False


def tiny_model(dim):
    """A model of the real shape: dim -> 16 -> 3."""
    rng = np.random.default_rng(0)
    x = ops.parameter([1, dim], ov.Type.f32, name="features")
    w1 = ops.constant(rng.normal(0, .1, (dim, 16)).astype(np.float32))
    w2 = ops.constant(rng.normal(0, .1, (16, 3)).astype(np.float32))
    h = ops.relu(ops.matmul(x, w1, False, False))
    y = ops.matmul(h, w2, False, False)
    return ov.Model([y], [x], "test_model")


def fv(values=None, *, hash_override=None, age=0.0, mask=None):
    d = dimension()
    return FeatureVector(
        values=values if values is not None else [0.1] * d,
        mask=mask if mask is not None else [1.0] * d,
        schema_version=CURRENT_SCHEMA,
        schema_hash=hash_override or schema_hash(),
        max_source_age_s=age,
    )


class SchemaIsIdentityNotJustSizeTests(unittest.TestCase):
    """Same dimension, different meaning, is the dangerous case."""

    def test_the_hash_covers_order_not_only_names(self):
        import dataclasses
        original = SCHEMAS[CURRENT_SCHEMA]
        h1 = schema_hash()
        try:
            SCHEMAS[CURRENT_SCHEMA] = tuple(reversed(original))
            h2 = schema_hash()
        finally:
            SCHEMAS[CURRENT_SCHEMA] = original
        self.assertNotEqual(h1, h2, "reordering features left the hash unchanged")

    def test_the_hash_covers_bounds(self):
        import dataclasses
        original = SCHEMAS[CURRENT_SCHEMA]
        h1 = schema_hash()
        try:
            changed = list(original)
            changed[0] = dataclasses.replace(changed[0], hi=original[0].hi + 5)
            SCHEMAS[CURRENT_SCHEMA] = tuple(changed)
            h2 = schema_hash()
        finally:
            SCHEMAS[CURRENT_SCHEMA] = original
        self.assertNotEqual(h1, h2)

    def test_names_are_unique(self):
        names = feature_names()
        self.assertEqual(len(names), len(set(names)))

    def test_the_vector_knows_whether_it_matches(self):
        self.assertTrue(fv().matches(schema_hash()))
        self.assertFalse(fv().matches("deadbeefdeadbeef"))
        self.assertTrue(fv().matches(None))


@unittest.skipUnless(HAVE_OV, "openvino not installed")
class RuntimeFailsClosedTests(unittest.TestCase):

    def setUp(self):
        self.rt = PredictiveRuntime()
        self.model = tiny_model(dimension())
        self.rt.load("t", self.model, version="1", schema_hash=schema_hash(),
                     outputs=("y",), devices=("CPU",))

    def test_a_healthy_call_succeeds_and_reports_its_provenance(self):
        p = self.rt.infer("t", fv())
        self.assertEqual(p.status, OK)
        self.assertEqual(p.device_used, "CPU")
        self.assertIsNotNone(p.latency_ms)
        self.assertEqual(p.model_version, "1")
        self.assertIsNotNone(p.predicted_at)

    def test_an_unloaded_model_abstains(self):
        p = self.rt.infer("nope", fv())
        self.assertEqual(p.status, ABSTAIN)
        self.assertFalse(p.ok)
        self.assertEqual(p.outputs, {})

    def test_a_schema_mismatch_abstains_rather_than_running(self):
        p = self.rt.infer("t", fv(hash_override="0000000000000000"))
        self.assertEqual(p.status, ABSTAIN)
        self.assertIn("schema", p.reason.lower())

    def test_stale_features_abstain(self):
        p = self.rt.infer("t", fv(age=DEFAULT_MAX_AGE_S + 1))
        self.assertEqual(p.status, ABSTAIN)
        self.assertIn("old", p.reason.lower())

    def test_too_many_missing_features_abstain(self):
        d = dimension()
        p = self.rt.infer("t", fv(mask=[0.0] * d))
        self.assertEqual(p.status, ABSTAIN)
        self.assertIn("missing", p.reason.lower())

    def test_a_few_missing_features_are_tolerated(self):
        d = dimension()
        mask = [1.0] * d
        mask[0] = 0.0
        self.assertEqual(self.rt.infer("t", fv(mask=mask)).status, OK)

    def test_no_abstention_ever_carries_outputs(self):
        for bad in (fv(hash_override="x" * 16), fv(age=1e9),
                    fv(mask=[0.0] * dimension())):
            p = self.rt.infer("t", bad)
            self.assertEqual(p.status, ABSTAIN)
            self.assertEqual(p.outputs, {}, "an abstention carried a value")

    def test_a_device_that_raises_abstains_rather_than_defaulting(self):
        class Exploding:
            def infer(self, *_a, **_k):
                raise RuntimeError("device gone")

        self.rt._models["t"].requests = {"CPU": Exploding()}
        p = self.rt.infer("t", fv())
        self.assertEqual(p.status, ABSTAIN)
        self.assertEqual(p.outputs, {})


@unittest.skipUnless(HAVE_OV, "openvino not installed")
class DevicePlacementTests(unittest.TestCase):

    def setUp(self):
        self.rt = PredictiveRuntime()
        self.model = tiny_model(dimension())

    def test_both_devices_produce_the_same_answer(self):
        """HARDWARE-ONLY. Not required runtime coverage.

        Its sole purpose is cross-device PARITY — it compares NPU output
        against CPU output and therefore cannot run without two physical
        devices. On the supported runtime (WSL2 / Ubuntu 24.04) OpenVINO
        enumerates ['CPU'] only, so this skips by construction.

        That is not a coverage gap: every behaviour that governs a trading
        decision — abstention, schema mismatch, stale features, missing
        features, a device that raises — is exercised on CPU by the tests
        above. This one asks a narrower question, about silicon.

        NPU runs fp16 internally, so exact equality is not expected; a
        material divergence is what would matter.
        """
        if "NPU" not in self.rt.available_devices():
            self.skipTest(
                "hardware-only: no NPU on this host (CPU is the required "
                "baseline; this test compares two devices and needs both)")
        self.rt.load("t", self.model, version="1", schema_hash=schema_hash(),
                     outputs=("y",), devices=("CPU", "NPU"))
        m = self.rt._models["t"]
        vec = np.asarray([fv().values], dtype=np.float32)
        got = {}
        for dev in ("CPU", "NPU"):
            m.requests[dev].infer({0: vec})
            got[dev] = np.array(m.requests[dev].get_output_tensor(0).data).ravel()
        self.assertLess(float(np.abs(got["CPU"] - got["NPU"]).max()), 1e-2)

    def test_it_falls_back_when_the_preferred_device_is_missing(self):
        self.rt.load("t", self.model, version="1", schema_hash=schema_hash(),
                     outputs=("y",), devices=("CPU",))
        p = self.rt.infer("t", fv())
        self.assertEqual(p.status, OK)
        self.assertEqual(p.device_used, "CPU")

    def test_latency_percentiles_are_recorded(self):
        self.rt.load("t", self.model, version="1", schema_hash=schema_hash(),
                     outputs=("y",), devices=("CPU",))
        for _ in range(30):
            self.rt.infer("t", fv())
        m = self.rt.metrics()
        self.assertTrue(any(k.startswith("t:") for k in m))
        stat = next(v for k, v in m.items() if k.startswith("t:"))
        self.assertGreaterEqual(stat["p99_ms"], stat["p50_ms"])


class DevicePolicyTests(unittest.TestCase):
    """The policy encodes measurements, not intuition: on this host the CPU
    is ~20x faster for these models, and the NPU's value is that it costs
    the CPU nothing."""

    def test_decision_path_models_run_on_cpu(self):
        self.assertEqual(policy.preferred_device("meta_filter"), policy.CPU)
        self.assertEqual(policy.preferred_device("execution"), policy.CPU)

    def test_background_models_run_on_the_npu(self):
        self.assertEqual(policy.preferred_device("path_intraday"), policy.NPU)
        self.assertEqual(policy.preferred_device("state_encoder"), policy.NPU)

    def test_the_fallback_is_always_cpu(self):
        for m in ("path_intraday", "meta_filter", "unknown_model"):
            self.assertEqual(policy.fallback_device(m), policy.CPU)

    def test_an_unknown_model_defaults_to_background(self):
        self.assertEqual(policy.role_of("something_new"), policy.BACKGROUND)

    def test_an_env_override_wins(self):
        import os
        os.environ["PREDICTIVE_DEVICE_PATH_INTRADAY"] = "CPU"
        try:
            self.assertEqual(policy.preferred_device("path_intraday"), policy.CPU)
        finally:
            del os.environ["PREDICTIVE_DEVICE_PATH_INTRADAY"]

    def test_force_cpu_overrides_everything(self):
        import os
        os.environ["PREDICTIVE_FORCE_CPU"] = "1"
        try:
            self.assertEqual(policy.preferred_device("path_intraday"), policy.CPU)
        finally:
            del os.environ["PREDICTIVE_FORCE_CPU"]

    def test_unknown_cpu_load_is_not_read_as_quiet(self):
        """Refusing the NPU on a measurement we could not take would
        disable it permanently and silently."""
        original = policy.cpu_load_fraction
        policy.cpu_load_fraction = lambda: None
        try:
            self.assertFalse(policy.npu_likely_starved())
        finally:
            policy.cpu_load_fraction = original

    def test_a_saturated_cpu_starves_the_npu(self):
        original = policy.cpu_load_fraction
        policy.cpu_load_fraction = lambda: 0.99
        try:
            self.assertTrue(policy.npu_likely_starved())
        finally:
            policy.cpu_load_fraction = original

    def test_degradation_is_relative_to_a_measured_baseline(self):
        self.assertTrue(policy.health_degraded(10.0, 1.0))
        self.assertFalse(policy.health_degraded(1.2, 1.0))
        self.assertFalse(policy.health_degraded(10.0, None))


class MissingIsNotZeroTests(unittest.TestCase):
    """A model handed 0.0 for an absent feature reads a specific claim."""

    def test_an_absent_feature_is_masked_not_defaulted(self):
        v = build(ta={}, signal={})
        self.assertEqual(len(v.values), dimension())
        self.assertGreater(v.missing_fraction, 0.5)
        for value, m in zip(v.values, v.mask):
            if m == 0.0:
                self.assertEqual(value, 0.0)

    def test_an_abstained_regime_axis_is_missing_not_neutral(self):
        """regime_axes distinguishes 'could not measure' from 'measured
        neutral'; collapsing that at the last step throws it away."""
        regime = {"axes": {"flow": {"abstained": True, "score": 0.0},
                           "trend": {"abstained": False, "score": 0.8}}}
        v = build(ta={}, signal={}, regime=regime)
        names = feature_names()
        flow_i = names.index("regime_flow_score")
        trend_i = names.index("regime_trend_score")
        self.assertEqual(v.mask[flow_i], 0.0, "abstained axis was recorded as observed")
        self.assertEqual(v.mask[trend_i], 1.0)

    def test_values_are_clipped_to_their_declared_bounds(self):
        v = build(ta={"rsi": 9999, "atr": {"pct": -500}},
                  signal={"rr_ratio": 1e9})
        for value, f in zip(v.values, SCHEMAS[CURRENT_SCHEMA]):
            self.assertGreaterEqual(value, f.lo, f.name)
            self.assertLessEqual(value, f.hi, f.name)

    def test_booleans_do_not_masquerade_as_measurements(self):
        v = build(ta={"rsi": True}, signal={})
        self.assertEqual(v.mask[feature_names().index("rsi")], 0.0)

    def test_nan_and_inf_are_treated_as_missing(self):
        v = build(ta={"rsi": float("nan"), "mfi": float("inf")}, signal={})
        names = feature_names()
        self.assertEqual(v.mask[names.index("rsi")], 0.0)
        self.assertEqual(v.mask[names.index("mfi")], 0.0)

    def test_a_break_that_did_not_happen_is_observed_not_missing(self):
        """"We looked and there was no break" is data."""
        v = build(ta={"structure": {"breaks": []}}, signal={})
        names = feature_names()
        self.assertEqual(v.mask[names.index("break_age_bars")], 1.0)
        self.assertEqual(v.values[names.index("break_held")], 0.0)

    def test_the_three_break_outcomes_are_separate_features(self):
        """held / failed / swept are different trades, so they must not be
        one ordered integer a model would read as a scale."""
        for outcome, flag in (("held", "break_held"), ("failed", "break_failed"),
                              ("sweep", "break_swept")):
            v = build(ta={"structure": {"breaks": [
                {"outcome": outcome, "bars_ago": 2, "break_volume_ratio": 1.4}]}},
                signal={})
            names = feature_names()
            self.assertEqual(v.values[names.index(flag)], 1.0, outcome)

    def test_junk_input_does_not_raise(self):
        for ta in ({"structure": "nope"}, {"atr": None}, {"emas": 5}):
            self.assertEqual(len(build(ta=ta, signal={}).values), dimension())


if __name__ == "__main__":
    unittest.main()
