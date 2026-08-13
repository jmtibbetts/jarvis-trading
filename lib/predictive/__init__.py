"""Predictive inference — device-independent by design.

Named `predictive`, not `npu`. Measured on this host (Core Ultra 9 285K +
Intel AI Boost), the CPU runs these model sizes ~20x faster than the NPU,
so binding the package to one accelerator would have encoded a false
assumption into every import.

What the NPU actually provides, measured: capacity that costs the CPU
nothing. See lib/predictive/device_policy.py for the numbers and the rule
they imply.

Nothing here is imported by the trading path. openvino is optional: when it
is absent the runtime reports unavailable and every model abstains, which
is the correct behaviour rather than a failure.
"""
from lib.predictive.runtime import (ABSTAIN, OK, Prediction, get_runtime)
from lib.predictive.schemas import (CURRENT_SCHEMA, FeatureVector,
                                    dimension, feature_names, schema_hash)

__all__ = ["ABSTAIN", "OK", "Prediction", "get_runtime", "CURRENT_SCHEMA",
           "FeatureVector", "dimension", "feature_names", "schema_hash"]
