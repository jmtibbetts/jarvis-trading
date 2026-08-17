"""Which device runs which model, decided by measurement.

Named `predictive`, not `npu`, and that is the whole point. Measured on
this machine (Core Ultra 9 285K + Intel AI Boost):

    64->32->5 MLP, single inference p50
        CPU   0.019 ms      38,840/s
        NPU   0.376 ms       8,575/s

The CPU is roughly 20x faster. Splitting one model across both devices is
worse than either alone — MULTI:NPU,CPU measured 5,976/s, below both. The
models are too small for the scheduling to pay for itself, and HETERO
(splitting one graph across chips) fails for the same reason.

So the NPU is not a speed win here and must never be assumed to be one.
What it IS, measured: free capacity. While the NPU ran 3,536 inferences
continuously, CPU inference latency moved 0.0201 -> 0.0200 ms — 1.00x. The
NPU absorbs sustained work at no cost to the cores the trading loop needs.

That gives a clean rule:

    on the decision path        -> CPU   (fastest, and immune to load)
    sustained background work   -> NPU   (free, 0.4 ms is irrelevant there)

One caveat with teeth. The NPU is stable up to ~16 busy cores and then
falls off a cliff:

    busy cores    CPU p50     NPU p50
        0         0.020 ms    0.474 ms
        8         0.023 ms    0.573 ms
       16         0.020 ms    0.521 ms
       22         0.020 ms    7.499 ms      <-- 15x

NPU dispatch runs on CPU threads, so saturating the CPU starves the
accelerator. The runtime watches for this and falls back rather than
blocking; see health_degraded().
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CPU = "CPU"
NPU = "NPU"
DEVICES = (CPU, NPU)

# What a model is FOR, which is what should decide its device — not a
# guess about which chip sounds more appropriate.
LATENCY_CRITICAL = "latency_critical"   # on the path to a trade decision
BACKGROUND = "background"               # sustained, nobody is waiting
ROLES = (LATENCY_CRITICAL, BACKGROUND)

# CPU IS THE REQUIRED BASELINE, NOT MERELY THE FALLBACK.
#
# The supported JARVIS runtime is WSL2 / Ubuntu 24.04, where OpenVINO
# enumerates ['CPU'] and no Intel NPU is passed through. Mapping BACKGROUND
# to NPU by default made the supported runtime take the fallback path on
# every background model — working, but reaching its normal placement by
# way of a warning, which is not a default anyone should have to read logs
# to understand.
#
# The NPU remains REACHABLE and is still the right device for sustained
# background work on hardware that has one (measured: it absorbs that work
# at no cost to the cores the trading loop needs). It is now reached by an
# explicit operator override rather than by default:
#
#     PREDICTIVE_DEVICE_<model>=NPU
#
# A DEVICE IS NOT A MODEL. Changing placement must never change schema,
# feature semantics, abstention, probability interpretation or trading
# logic — only where the arithmetic happens.
ROLE_DEVICE = {
    LATENCY_CRITICAL: CPU,
    BACKGROUND: CPU,
}

# Per-model overrides, so the policy can be argued with in one place rather
# than rediscovered at each call site. Anything absent falls back to its
# declared role.
MODEL_ROLE: dict[str, str] = {
    # Consulted while deciding whether to place a trade.
    "meta_filter": LATENCY_CRITICAL,
    "execution": LATENCY_CRITICAL,
    # Scored per candidate, off the hot path — nobody blocks on these.
    "path_intraday": BACKGROUND,
    "outcome_15m": BACKGROUND,
    "outcome_1h": BACKGROUND,
    "state_encoder": BACKGROUND,
    "drift": BACKGROUND,
}

# Above this the NPU is being starved of dispatch threads and its latency
# collapses. Fraction of logical cores busy.
NPU_STARVATION_LOAD = 0.85

# An NPU inference this far above its measured baseline means the device is
# degraded, whatever the cause. Fall back rather than wait.
DEGRADED_LATENCY_FACTOR = 5.0


def _env_device(model: str) -> str | None:
    """PREDICTIVE_DEVICE_<MODEL> overrides everything, for debugging a
    single model without editing code."""
    raw = os.getenv(f"PREDICTIVE_DEVICE_{model.upper()}")
    if raw and raw.strip().upper() in DEVICES:
        return raw.strip().upper()
    return None


def role_of(model: str) -> str:
    return MODEL_ROLE.get(model, BACKGROUND)


def preferred_device(model: str) -> str:
    """The device this model should run on, absent a measured reason not to."""
    override = _env_device(model)
    if override:
        return override
    if os.getenv("PREDICTIVE_FORCE_CPU", "").strip().lower() in ("1", "true", "yes"):
        return CPU
    return ROLE_DEVICE.get(role_of(model), CPU)


def fallback_device(model: str) -> str:
    """Always CPU. It is the fastest device here and the one that cannot be
    unavailable — falling back to it is not a degraded mode, it is simply a
    different placement."""
    return CPU


def cpu_load_fraction() -> float | None:
    """Fraction of logical cores currently busy, or None if unmeasurable.

    None matters: an unknown load must not be treated as a quiet machine.
    """
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.0)) / 100.0
    except Exception:
        return None


def npu_likely_starved() -> bool:
    """True when the CPU is loaded enough that NPU dispatch will suffer.

    Conservative: unknown load returns False, because refusing the NPU on
    a measurement we could not take would quietly disable it forever.
    """
    load = cpu_load_fraction()
    if load is None:
        return False
    return load >= NPU_STARVATION_LOAD


def health_degraded(observed_ms: float, baseline_ms: float | None) -> bool:
    """Whether a device has fallen far enough below its own measured
    baseline to stop trusting it for this model."""
    if not baseline_ms or baseline_ms <= 0 or observed_ms <= 0:
        return False
    return observed_ms > baseline_ms * DEGRADED_LATENCY_FACTOR


def describe() -> dict:
    """The active policy, for /api/predictive/status."""
    return {
        "roles": dict(MODEL_ROLE),
        "role_device": dict(ROLE_DEVICE),
        "resolved": {m: preferred_device(m) for m in MODEL_ROLE},
        "npu_starvation_load": NPU_STARVATION_LOAD,
        "degraded_latency_factor": DEGRADED_LATENCY_FACTOR,
        "cpu_load": cpu_load_fraction(),
        "npu_likely_starved": npu_likely_starved(),
        "note": ("CPU is ~20x faster for these model sizes; the NPU is used "
                 "for sustained background work because it costs the CPU "
                 "nothing (measured 1.00x)."),
    }
