"""Inference that abstains rather than guesses, on whichever device suits.

Callers never mention OpenVINO or a device. They ask for a prediction and
get either a result or an honest abstention, and every result carries what
produced it: model, version, schema hash, device, latency, and whether a
fallback happened.

The rules that make this safe to put anywhere near a trading decision:

  A missing model, a stale feature vector, a schema mismatch, a dead device
  or a raised exception all produce ABSTAIN. None of them produce a number.

  Nothing here ever returns a default, a neutral value, or a zero on
  failure. A silently favourable default is how a broken model becomes a
  losing trade, and it would be indistinguishable from a working one.

  The device is chosen by lib/predictive/device_policy.py from measurements
  taken on this machine — not from an assumption that a neural accelerator
  must be the fast path. Here it is not: the CPU is ~20x quicker for these
  model sizes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ABSTAIN = "ABSTAIN"
OK = "OK"

# A feature vector older than this describes a market that has moved on.
DEFAULT_MAX_AGE_S = 90.0
# Above this fraction of absent features the model is being asked to
# extrapolate from padding.
DEFAULT_MAX_MISSING = 0.30


@dataclass(frozen=True)
class Prediction:
    status: str
    model: str
    reason: str = ""
    outputs: dict = field(default_factory=dict)
    model_version: str | None = None
    schema_hash: str | None = None
    device_requested: str | None = None
    device_used: str | None = None
    fallback: bool = False
    latency_ms: float | None = None
    feature_age_s: float | None = None
    missing_fraction: float | None = None
    predicted_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict:
        return {
            "status": self.status, "model": self.model, "reason": self.reason,
            "outputs": self.outputs, "model_version": self.model_version,
            "schema_hash": self.schema_hash,
            "device_requested": self.device_requested,
            "device_used": self.device_used, "fallback": self.fallback,
            "latency_ms": self.latency_ms, "feature_age_s": self.feature_age_s,
            "missing_fraction": self.missing_fraction,
            "predicted_at": self.predicted_at,
        }


def abstain(model: str, reason: str, **kw) -> Prediction:
    return Prediction(status=ABSTAIN, model=model, reason=reason, **kw)


@dataclass
class LoadedModel:
    name: str
    version: str
    schema_hash: str
    outputs: tuple
    compiled: dict = field(default_factory=dict)   # device -> compiled model
    requests: dict = field(default_factory=dict)   # device -> infer request
    baseline_ms: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class PredictiveRuntime:
    """One process-wide runtime. Models load once and are reused."""

    def __init__(self):
        self._models: dict[str, LoadedModel] = {}
        self._core = None
        self._devices: tuple = ()
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {}

    # ── device discovery ────────────────────────────────────────────────
    def _get_core(self):
        if self._core is not None:
            return self._core
        with self._lock:
            if self._core is None:
                try:
                    import openvino as ov
                    self._core = ov.Core()
                    self._devices = tuple(self._core.available_devices)
                    logger.info(f"[Predictive] devices: {self._devices}")
                except Exception as e:
                    # openvino absent is a normal state, not an error: the
                    # trading system must run without the ML stack.
                    logger.info(f"[Predictive] inference unavailable ({e}) — abstaining")
                    self._core = False
        return self._core

    def available_devices(self) -> tuple:
        self._get_core()
        return self._devices

    def has_device(self, device: str) -> bool:
        return device in self.available_devices()

    # ── loading ─────────────────────────────────────────────────────────
    def load(self, name: str, path: str, *, version: str, schema_hash: str,
             outputs: tuple, devices: tuple = ()) -> bool:
        core = self._get_core()
        if not core:
            return False
        from lib.predictive import device_policy as policy
        want = devices or (policy.preferred_device(name), policy.fallback_device(name))
        lm = LoadedModel(name=name, version=version, schema_hash=schema_hash,
                         outputs=tuple(outputs))
        for dev in dict.fromkeys(want):          # de-dup, keep order
            if dev not in self._devices:
                logger.warning(f"[Predictive] {name}: device {dev} unavailable")
                continue
            try:
                compiled = core.compile_model(path, dev)
                lm.compiled[dev] = compiled
                lm.requests[dev] = compiled.create_infer_request()
            except Exception as e:
                logger.warning(f"[Predictive] {name}: compile on {dev} failed: {e}")
        if not lm.requests:
            logger.error(f"[Predictive] {name}: no device could load it")
            return False
        with self._lock:
            self._models[name] = lm
        logger.info(f"[Predictive] loaded {name} v{version} on {list(lm.requests)}")
        return True

    def unload(self, name: str) -> bool:
        with self._lock:
            return self._models.pop(name, None) is not None

    def loaded(self) -> list:
        return sorted(self._models)

    # ── inference ───────────────────────────────────────────────────────
    def infer(self, name: str, fv, *, max_age_s: float = DEFAULT_MAX_AGE_S,
              max_missing: float = DEFAULT_MAX_MISSING) -> Prediction:
        """Run one model. Returns a Prediction that is either OK or ABSTAIN.

        Every gate below fails CLOSED. There is no path through this function
        that invents a value.
        """
        from datetime import datetime, timezone
        now = lambda: datetime.now(timezone.utc).isoformat()

        model = self._models.get(name)
        if model is None:
            return abstain(name, "model not loaded", predicted_at=now())

        # ── data quality, before touching a device ──────────────────────
        if not fv.matches(model.schema_hash):
            # The dangerous case: right dimension, wrong meaning. The model
            # would happily consume it.
            return abstain(name,
                           f"feature schema mismatch (model {model.schema_hash}, "
                           f"features {fv.schema_hash})",
                           model_version=model.version,
                           schema_hash=model.schema_hash, predicted_at=now())
        if fv.max_source_age_s > max_age_s:
            return abstain(name,
                           f"features {fv.max_source_age_s:.0f}s old (limit {max_age_s:.0f}s)",
                           model_version=model.version, feature_age_s=fv.max_source_age_s,
                           predicted_at=now())
        missing = fv.missing_fraction
        if missing > max_missing:
            return abstain(name,
                           f"{missing:.0%} of features missing (limit {max_missing:.0%})",
                           model_version=model.version, missing_fraction=missing,
                           predicted_at=now())

        from lib.predictive import device_policy as policy
        want = policy.preferred_device(name)
        # The NPU's dispatch path runs on CPU threads; a saturated CPU
        # starves it and latency collapses 15x. Measured, not theorised.
        if want == policy.NPU and policy.npu_likely_starved():
            want = policy.fallback_device(name)

        order = [d for d in (want, policy.fallback_device(name)) if d in model.requests]
        order = list(dict.fromkeys(order)) or list(model.requests)

        import numpy as np
        vec = np.asarray([fv.values], dtype=np.float32)

        last_error = None
        for dev in order:
            req = model.requests.get(dev)
            if req is None:
                continue
            try:
                with model.lock:
                    t0 = time.perf_counter()
                    req.infer({0: vec})
                    latency = (time.perf_counter() - t0) * 1000.0
                    raw = [np.array(req.get_output_tensor(i).data).ravel().tolist()
                           for i in range(len(model.outputs))]
            except Exception as e:
                last_error = str(e)[:160]
                logger.warning(f"[Predictive] {name} on {dev} failed: {last_error}")
                continue

            baseline = model.baseline_ms.get(dev)
            if baseline is None:
                model.baseline_ms[dev] = latency
            elif policy.health_degraded(latency, baseline):
                logger.warning(f"[Predictive] {name}: {dev} degraded "
                               f"({latency:.2f}ms vs {baseline:.2f}ms baseline)")

            self._record(name, dev, latency)
            return Prediction(
                status=OK, model=name,
                outputs={k: v for k, v in zip(model.outputs, raw)},
                model_version=model.version, schema_hash=model.schema_hash,
                device_requested=order[0], device_used=dev,
                fallback=dev != order[0], latency_ms=round(latency, 4),
                feature_age_s=fv.max_source_age_s, missing_fraction=missing,
                predicted_at=now(), reason="ok",
            )

        return abstain(name, f"every device failed ({last_error})",
                       model_version=model.version, predicted_at=now())

    # ── telemetry ───────────────────────────────────────────────────────
    def _record(self, name: str, device: str, ms: float):
        key = f"{name}:{device}"
        s = self._stats.setdefault(key, {"n": 0, "samples": []})
        s["n"] += 1
        s["samples"].append(ms)
        if len(s["samples"]) > 500:
            s["samples"] = s["samples"][-500:]

    def metrics(self) -> dict:
        out = {}
        for key, s in self._stats.items():
            lat = sorted(s["samples"])
            if not lat:
                continue
            out[key] = {
                "calls": s["n"],
                "p50_ms": round(lat[len(lat) // 2], 4),
                "p95_ms": round(lat[int(len(lat) * .95)], 4),
                "p99_ms": round(lat[min(len(lat) - 1, int(len(lat) * .99))], 4),
            }
        return out

    def status(self) -> dict:
        from lib.predictive import device_policy as policy
        core = self._get_core()
        return {
            "available": bool(core),
            "devices": list(self.available_devices()),
            "models": {
                n: {"version": m.version, "schema_hash": m.schema_hash,
                    "devices": list(m.requests),
                    "baseline_ms": {d: round(v, 4) for d, v in m.baseline_ms.items()}}
                for n, m in self._models.items()
            },
            "policy": policy.describe(),
            "metrics": self.metrics(),
        }


_RUNTIME: PredictiveRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> PredictiveRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = PredictiveRuntime()
    return _RUNTIME
