# CLAUDE.md — JARVIS Predictive Inference & Adaptive Intelligence Upgrade

> **Repository:** `jmtibbetts/jarvis-trading`
>
> **Purpose:** Add measurable predictive ML capability to JARVIS without duplicating or weakening the deterministic trading architecture that already exists.
>
> **Target hardware:** Intel Core Ultra 9 285K + Intel AI Boost NPU + RTX 5090
>
> **Primary rule:** The goal is **better trading decisions**, not NPU utilization.

---

# 0. READ THIS FIRST — VERIFIED REALITY OF THE TARGET SYSTEM

Before writing code, treat the following as established constraints unless fresh inspection of the machine or repository proves they changed.

## 0.1 Hardware/runtime facts verified on the target machine

OpenVINO 2026.3.0 enumerated:

```text
CPU
GPU.0
GPU.1
NPU
```

The NPU is usable and identified as Intel(R) AI Boost.

A real compact MLP representative of the intended forecasting models was compiled and executed on both NPU and CPU with close numerical parity.

Measured example:

```text
Model: compact 64 → 32 → 5 MLP

single inference p50
NPU ≈ 0.376 ms
CPU ≈ 0.019 ms

batch=256 per-sample
NPU ≈ 1.1 µs
CPU ≈ 0.2 µs
```

Therefore:

> **Do not assume NPU means lower latency.**

For these tiny models, fixed NPU dispatch overhead can dominate.

The NPU's likely advantages are:

```text
resource isolation
predictable dedicated inference capacity
keeping inference off CPU market-data/execution threads
keeping inference off the RTX 5090 used by LM Studio / LLMs
power/thermal characteristics under sustained workloads
future scaling across many symbols/models
```

The software architecture must therefore be device-independent.

Use:

```text
PREDICTIVE INFERENCE ENGINE
│
├── CPU
├── Intel NPU
└── future accelerator
```

NOT:

```text
everything neural → NPU
```

Every production model must be benchmarked on both CPU and NPU.

---

## 0.2 Current repository/data facts verified during assessment

At the time of this implementation review:

```text
trade_outcomes                           ≈ 19,446
trade_outcomes with exited_at            ≈ 19,446
trade_outcomes with entered_at           ≈ 570
replay outcomes                          ≈ 10,520
replay outcomes with usable entered_at   ≈ 0
signals                                  ≈ 39,821
signals with measured slippage           ≈ 4
persisted order-book/tape history        none sufficient for ML training
```

Implications:

### Path model

MFE/MAE cannot be reconstructed reliably from the existing `trade_outcomes` table for most records because entry timestamps are absent.

BUT:

`lib/signal_replay.py` already walks future OHLC bars.

Therefore path labels can be generated **inside the replay loop immediately**.

This must happen before training the path model.

### Execution model

Four measured slippage samples are not a training set.

Therefore:

> **Do NOT build or train a production execution/slippage model yet.**

Build the recorder/data pipeline now.

Train the execution model only after a meaningful number of real observed fills exists.

### Pattern memory

The existing pattern-memory path can surface strong language from approximately three observations, while newer calibration/expectancy systems require much larger samples.

Fix this before ML work because it is an immediate statistical-authority bug.

---

# 1. THE PERMANENT NUMERIC-AUTHORITY CONTRACT

This section is a permanent architectural rule for all future Claude sessions.

## 1.1 Deterministic code owns known arithmetic and hard constraints

LLMs and ML models must never become the source of truth for:

```text
fees
commissions
spread arithmetic
funding arithmetic
borrow arithmetic
P&L
R calculations
position quantity
position size
notional
leverage
liquidation price
margin
maintenance margin
portfolio heat
maximum account risk
hard drawdown limits
kill switches
technical indicator calculations
strategy identity
broker permissions
API credentials
withdrawal permissions
```

Existing deterministic modules remain authoritative.

Examples already present include:

```text
lib/expectancy.py
lib/calibration.py
lib/strategy_lifecycle.py
lib/transaction_costs.py
lib/strategies.py
lib/regime_axes.py
lib/llm_router.py
risk / sizing / execution modules already in the repo
```

Do not create competing versions.

---

## 1.2 ML may estimate uncertain future quantities

ML may estimate:

```text
conditional future-return distribution
MFE distribution
MAE distribution
stop-first probability
target-first probability
time-to-MFE
time-to-failure
conditional setup quality
slippage
fill probability
adverse selection
regime-transition probability
historically meaningful cross-timeframe conflict
model drift
concept drift
execution drift
```

These are forecasts, not arithmetic facts.

---

## 1.3 LLMs may reason about qualitative evidence

LLMs may handle:

```text
contradictory evidence
catalysts
news interpretation
macro/geopolitical effects
cross-asset qualitative synthesis
hypothesis generation
trade/post-trade explanation
```

LLMs must not be promoted into numeric authority simply because they can output numbers.

---

## 1.4 Absolute risk rule

The predictive layer may:

```text
veto a trade
reduce confidence
reduce model influence
raise the minimum EV requirement
increase scrutiny
escalate LLM reasoning
```

It may NOT:

```text
turn a hard deterministic NO_TRADE into TRADE
override a risk violation
raise leverage above deterministic limits
increase position size above deterministic limits
disable the kill switch
```

Initial live integration should be asymmetric:

```text
ML may make JARVIS more conservative
before
ML is ever allowed to make JARVIS more aggressive
```

---

# 2. FINAL TARGET ARCHITECTURE

```text
                           CPU
                    FEATURE ENGINE
                          │
        ┌─────────────────┼──────────────────┐
        │                 │                  │
        ▼                 ▼                  ▼
     TA/Structure      Kraken             Regime
     Strategies        Tape/L2             Axes
        │                 │                  │
        └─────────────────┼──────────────────┘
                          ▼
                 CANONICAL FEATURE VECTOR
                          │
                          ▼
               PREDICTIVE INFERENCE ENGINE
                          │
             ┌────────────┼────────────┐
             │            │            │
             ▼            ▼            ▼
            CPU          NPU       future device
             │            │
             └──────┬─────┘
                    ▼
              MODEL BUNDLE
                    │
       ┌────────────┼──────────────┐
       ▼            ▼              ▼
   STATE        OUTCOME/PATH     EXECUTION
  ENCODER         MODELS          MODEL*
       │            │              │
       │       Return Dist.        │
       │       MFE / MAE           │
       │       Tail Risk           │
       │       Time-to-Move        │
       │                           │
       └────────────┼──────────────┘
                    ▼
                DRIFT/TRUST
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
  HISTORICAL ANALOGS    ADAPTIVE CONTROL
                         neurostate
                              │
                              ▼
                    EXISTING JARVIS
                              │
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
 expectancy.py          llm_router.py              risk
       │                      │                      │
 transaction_costs            LLM              sizing/limits
       │                                             │
 lifecycle                                          │
       │                                             │
       └──────────────────────┬──────────────────────┘
                              ▼
                          execution
                              │
                              ▼
                           brokers
```

`* EXECUTION MODEL` is intentionally data-gated and must not be trained until sufficient observed fills exist.

---

# 3. HARDWARE RESPONSIBILITY MAP

```text
┌──────────────────────────────────────────────────────────────┐
│ CPU                                                          │
├──────────────────────────────────────────────────────────────┤
│ WebSockets                                                   │
│ REST APIs                                                    │
│ database                                                     │
│ TA calculations                                              │
│ market structure                                             │
│ strategy classification                                      │
│ regime axes                                                  │
│ expectancy arithmetic                                        │
│ transaction costs                                            │
│ P&L                                                          │
│ liquidation math                                             │
│ position sizing                                              │
│ portfolio risk                                               │
│ execution decisions                                          │
│ neurostate arithmetic                                        │
│ analog nearest-neighbor search initially                     │
│ ML inference if CPU is operationally superior                │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ INTEL NPU                                                    │
├──────────────────────────────────────────────────────────────┤
│ compact predictive inference WHEN benchmarked worthwhile     │
│ path model                                                   │
│ outcome model                                                │
│ state encoder                                                │
│ future execution model                                       │
│ other compatible small neural models                         │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ RTX 5090                                                     │
├──────────────────────────────────────────────────────────────┤
│ LM Studio / Qwen / large LLM                                 │
│ deep qualitative reasoning                                   │
│ model training                                               │
│ retraining                                                   │
│ research / experiments                                       │
│ challenger generation                                        │
└──────────────────────────────────────────────────────────────┘
```

Training on the RTX 5090 is a batch/research workload.

Predictive inference must not consume RTX capacity by default.

---

# 4. DO NOT LET DEVICE PLACEMENT DRIVE MODEL DESIGN

The first question is:

> Does this model add predictive/trading value?

Only then ask:

> Where should it run?

For every model evaluate:

```text
BASELINE JARVIS

BASELINE
+ model on CPU

BASELINE
+ identical model on NPU
```

Measure:

```text
model output parity
OOS model quality
p50 latency
p95 latency
p99 latency
throughput
CPU utilization
market-data loop jitter
WebSocket processing jitter
scheduler jitter
execution-loop latency
LLM latency
power/thermal behavior if practical
behavior at 10 / 50 / 100 / 500 watched symbols
```

Production device selection must be empirical and may differ per model.

---

# 5. IMPLEMENTATION ORDER — DO NOT REORDER WITHOUT EVIDENCE

## PHASE 0 — Fix pattern-memory statistical authority

Do this first.

Reason:

It is already affecting LLM context and can present tiny samples as if they are reliable evidence.

Target:

`lib/learning_engine.py`

Existing behavior includes a gate around approximately:

```python
if not row or row[0] < 3:
    return ""
```

which can lead to prompt text equivalent to:

```text
This exact TA setup has occurred 3 times — 3/3 wins (100% win rate)
```

This conflicts with the statistical discipline in newer calibration/expectancy modules.

### Required change

Use tiers such as:

```text
n < 10
OBSERVED_ONLY

10 <= n < 25
EARLY_EVIDENCE

n >= 25
MEASURED_CONTEXT
```

Even at `n >= 25`, pattern memory is descriptive context.

`calibration.py` / `expectancy.py` remain statistical authority.

Example:

```python
def pattern_context(total: int, wins: int, description: str) -> str:
    if total <= 0:
        return ""

    if total < 10:
        return (
            f"\nPATTERN MEMORY: Seen {total} time(s). "
            "Sample is too small for a performance conclusion. "
            f"Pattern: {description}\n"
        )

    rate = wins / total

    if total < 25:
        return (
            f"\nPATTERN MEMORY: Seen {total} times; observed wins "
            f"{wins}/{total} ({rate:.0%}). EARLY EVIDENCE ONLY — "
            "do not treat this as calibrated probability or expectancy. "
            f"Pattern: {description}\n"
        )

    return (
        f"\nPATTERN MEMORY: Seen {total} times; observed wins "
        f"{wins}/{total} ({rate:.0%}). "
        "Use calibration/expectancy as statistical authority. "
        f"Pattern: {description}\n"
    )
```

Prefer using existing constants where appropriate instead of scattering new magic numbers.

Add tests.

---

# 6. PHASE 1 — Upgrade signal replay to generate path labels

This phase unlocks the first useful model.

Target:

`lib/signal_replay.py`

The replay loop already walks bars.

Extend it to calculate:

```text
MFE
MAE
MFE in R
MAE in R
bar/time to MFE
bar/time to MAE
stop-first
target-first
ambiguous first touch
maximum favorable price
maximum adverse price
```

## 6.1 Core path tracking example

Adapt to actual current code rather than replacing working replay behavior.

```python
max_favorable = 0.0
max_adverse = 0.0

mfe_bar = None
mae_bar = None

first_touch = None
stop_bar = None
target_bar = None

for i, (_, bar) in enumerate(future.iterrows(), start=1):
    hi = float(bar["high"])
    lo = float(bar["low"])

    if is_short:
        favorable = entry - lo
        adverse = hi - entry

        stop_touched = hi >= stop
        target_touched = lo <= target
    else:
        favorable = hi - entry
        adverse = entry - lo

        stop_touched = lo <= stop
        target_touched = hi >= target

    if favorable > max_favorable:
        max_favorable = favorable
        mfe_bar = i

    if adverse > max_adverse:
        max_adverse = adverse
        mae_bar = i

    if first_touch is None:
        if stop_touched and target_touched:
            # OHLC cannot reveal intrabar ordering.
            first_touch = "AMBIGUOUS"
        elif stop_touched:
            first_touch = "STOP"
            stop_bar = i
        elif target_touched:
            first_touch = "TARGET"
            target_bar = i

    # Preserve the CURRENT replay engine's existing conservative
    # stop/target resolution behavior after recording path statistics.
```

## 6.2 Convert to R

```python
risk_distance = abs(entry - stop)

if risk_distance > 0:
    mfe_r = max_favorable / risk_distance
    mae_r = max_adverse / risk_distance
else:
    mfe_r = None
    mae_r = None
```

## 6.3 Persist labels

Replay output should contain fields conceptually like:

```json
{
  "mfe_r": 1.84,
  "mae_r": 0.37,
  "mfe_bar": 8,
  "mae_bar": 2,
  "first_touch": "TARGET",
  "stop_bar": null,
  "target_bar": 9,
  "path_source": "replay_ohlc"
}
```

Do not pretend OHLC replay contains tick-level ordering.

### Ambiguous bars

If both stop and target lie inside the same OHLC bar:

```text
first_touch = AMBIGUOUS
```

Do not select the profitable event.

### Preserve provenance

Always distinguish:

```text
LIVE_OBSERVED
REPLAY_OHLC
BACKTEST
```

Replay labels may bootstrap training but should not be weighted identically to live labels without validation.

---

# 7. PHASE 2 — Training environment

Do not mix training responsibilities into production inference code.

RTX 5090 training environment should include as needed:

```text
torch with CUDA support
numpy
pandas
scikit-learn
scipy
openvino
nncf when quantization is actually required
```

Keep environment installation reproducible.

Suggested:

```text
requirements-ml.txt
```

or the repository's existing dependency convention.

Verify CUDA training uses the RTX 5090.

Do not use the Intel NPU for training.

---

# 8. PHASE 3 — Predictive inference infrastructure

Create the abstraction around **predictive inference**, not around one device.

Suggested structure:

```text
lib/predictive/
│
├── __init__.py
├── config.py
├── runtime.py
├── device_policy.py
├── model_registry.py
├── queue.py
├── schemas.py
├── features.py
├── normalization.py
├── health.py
├── prediction_store.py
└── metrics.py
```

If `lib/npu/` already exists from prior work, do not create a duplicate package. Refactor it into a device-independent predictive layer or preserve compatibility.

---

# 9. DEVICE POLICY

Example configuration:

```yaml
predictive_inference:
  enabled: true
  default_preferred_device: NPU
  default_fallback_device: CPU
  shadow_only: true

models:
  path_intraday:
    preferred_device: NPU

  outcome_15m:
    preferred_device: NPU

  tiny_meta_model:
    preferred_device: CPU
```

Environment-variable equivalents are fine if they match project conventions.

---

# 10. OPENVINO RUNTIME

Current OpenVINO supports:

```python
core = ov.Core()
core.available_devices
core.compile_model(model, "NPU")
```

and model caching.

Build a single runtime abstraction.

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import time

import numpy as np
import openvino as ov

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferenceResult:
    ok: bool
    model_name: str
    device_requested: str
    device_used: str | None
    fallback: bool
    latency_ms: float
    value: object | None
    error: str | None = None


class PredictiveRuntime:
    def __init__(self, cache_dir: str = "cache/openvino"):
        self.core = ov.Core()
        self.devices = tuple(self.core.available_devices)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.core.set_property({"CACHE_DIR": str(self.cache_dir)})
        except Exception as exc:
            logger.warning("OpenVINO cache configuration failed: %s", exc)

        self._compiled = {}

    def available(self, device: str) -> bool:
        prefix = device.upper()
        return any(str(d).upper().startswith(prefix) for d in self.devices)

    def _compile(self, *, name: str, model_path: str, device: str):
        key = (name, model_path, device)

        compiled = self._compiled.get(key)
        if compiled is not None:
            return compiled

        compiled = self.core.compile_model(model_path, device)
        self._compiled[key] = compiled
        return compiled

    def infer(
        self,
        *,
        name: str,
        model_path: str,
        tensor: np.ndarray,
        preferred_device: str,
        fallback_device: str = "CPU",
    ) -> InferenceResult:
        started = time.perf_counter()

        requested = preferred_device.upper()

        if not self.available(requested):
            requested = fallback_device.upper()

        try:
            compiled = self._compile(
                name=name,
                model_path=model_path,
                device=requested,
            )

            raw = compiled([tensor])
            output = next(iter(raw.values()))

            return InferenceResult(
                ok=True,
                model_name=name,
                device_requested=preferred_device,
                device_used=requested,
                fallback=requested != preferred_device.upper(),
                latency_ms=(time.perf_counter() - started) * 1000.0,
                value=np.asarray(output),
            )

        except Exception as first_exc:
            fallback = fallback_device.upper()

            if requested == fallback:
                return InferenceResult(
                    ok=False,
                    model_name=name,
                    device_requested=preferred_device,
                    device_used=None,
                    fallback=requested != preferred_device.upper(),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    value=None,
                    error=str(first_exc),
                )

            try:
                compiled = self._compile(
                    name=name,
                    model_path=model_path,
                    device=fallback,
                )

                raw = compiled([tensor])
                output = next(iter(raw.values()))

                return InferenceResult(
                    ok=True,
                    model_name=name,
                    device_requested=preferred_device,
                    device_used=fallback,
                    fallback=True,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    value=np.asarray(output),
                )

            except Exception as fallback_exc:
                return InferenceResult(
                    ok=False,
                    model_name=name,
                    device_requested=preferred_device,
                    device_used=None,
                    fallback=True,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    value=None,
                    error=(
                        f"{requested}: {first_exc}; "
                        f"{fallback}: {fallback_exc}"
                    ),
                )
```

Failure returns `None`.

Never:

```python
tail_risk = 0
```

because inference failed.

---

# 11. STARTUP DEVICE BENCHMARK

Because CPU was materially faster for the tested tiny MLP, support optional startup/offline benchmarking.

Do not auto-switch devices every few seconds.

Choose a device using stable benchmark evidence.

Example:

```python
from statistics import median
import time


def benchmark(compiled_model, sample, runs: int = 500) -> dict:
    # Warm-up
    for _ in range(25):
        compiled_model([sample])

    times = []

    for _ in range(runs):
        started = time.perf_counter_ns()
        compiled_model([sample])
        elapsed = time.perf_counter_ns() - started
        times.append(elapsed / 1_000_000)

    times.sort()

    def pct(p):
        idx = min(len(times) - 1, int((len(times) - 1) * p))
        return times[idx]

    return {
        "p50_ms": median(times),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
    }
```

Benchmark under realistic concurrent JARVIS workload too.

A microbenchmark alone does not measure CPU contention.

---

# 12. LATEST-STATE-WINS INFERENCE QUEUE

Never allow stale market updates to pile up.

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class InferenceJob:
    model_name: str
    symbol: str
    observed_at: float
    payload: object


class LatestOnlyQueue:
    def __init__(self):
        self._latest = {}
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def submit(self, job: InferenceJob):
        key = (job.model_name, job.symbol)

        async with self._lock:
            current = self._latest.get(key)

            if current is None or job.observed_at >= current.observed_at:
                self._latest[key] = job

            self._event.set()

    async def pop_batch(self, max_items: int = 64):
        while True:
            await self._event.wait()

            async with self._lock:
                if not self._latest:
                    self._event.clear()
                    continue

                keys = list(self._latest.keys())[:max_items]
                jobs = [self._latest.pop(k) for k in keys]

                if not self._latest:
                    self._event.clear()

                return jobs
```

Do not synchronously block market-data consumers or execution threads on ML inference.

---

# 13. CANONICAL FEATURE CONTRACT

Do not let every model independently scrape repository state.

Create one feature builder.

Features should come from existing JARVIS outputs.

Potential categories:

```text
price/returns
TA
structure
strategy
regime axes
relative strength
derivatives
microstructure
execution context
time/session
portfolio context where appropriate
```

Do not recompute existing indicators inside the predictive subsystem.

---

# 14. FEATURE SCHEMA VERSIONING

```python
from dataclasses import dataclass
import hashlib
import json


FEATURES_V1 = (
    "ret_1",
    "ret_3",
    "ret_5",
    "atr_pct",
    "atr_percentile",
    "adx",
    "rsi",
    "volume_ratio",
    "vwap_distance_atr",
    "support_distance_atr",
    "resistance_distance_atr",
    "strategy_match",
    "regime_trend_score",
    "regime_volatility_score",
    "regime_liquidity_score",
    "regime_flow_score",
    "relative_strength",
    "funding_rate",
    "oi_change_pct",
    "spread_bps",
    "book_imbalance",
    "tape_flow_imbalance",
)


def hash_schema(names) -> str:
    encoded = json.dumps(
        list(names),
        separators=(",", ":"),
    ).encode()

    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class FeatureSchema:
    version: str
    names: tuple[str, ...]
    hash: str


SCHEMA_V1 = FeatureSchema(
    version="1",
    names=FEATURES_V1,
    hash=hash_schema(FEATURES_V1),
)
```

Never silently reorder features.

A model must refuse incompatible schema versions.

---

# 15. MISSINGNESS AND FRESHNESS

Missing does not mean zero.

Neutral does not mean missing.

Carry a missing mask.

```python
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np


@dataclass(frozen=True)
class FeatureVector:
    symbol: str
    observed_at: datetime
    values: np.ndarray
    missing_mask: np.ndarray
    schema_version: str
    schema_hash: str
    max_source_age_s: float


def can_infer(
    vector: FeatureVector,
    *,
    max_age_s: float,
    max_missing_fraction: float,
) -> tuple[bool, str]:

    missing_fraction = float(vector.missing_mask.mean())

    if vector.max_source_age_s > max_age_s:
        return False, "STALE_FEATURES"

    if missing_fraction > max_missing_fraction:
        return False, "TOO_MANY_MISSING_FEATURES"

    return True, "OK"
```

Do not reuse stale Kraken tape/L2 data as current market state.

---

# 16. PREDICTION PERSISTENCE

Every prediction must be joinable to realized results.

Conceptual record:

```python
from dataclasses import dataclass


@dataclass
class StoredPrediction:
    id: str
    signal_id: str | None
    symbol: str
    timeframe: str | None

    model_name: str
    model_version: str
    schema_version: str

    observed_at: str
    predicted_at: str

    device_requested: str
    device_used: str
    latency_ms: float

    trust: float | None
    output_json: str
```

Do not create duplicate tables if existing ORM structures can cleanly hold the data.

The essential requirement is that later analysis can answer:

```text
When this model disagreed with baseline JARVIS:
did it improve net expectancy?
```

---

# 17. PHASE 4 — FIRST REAL MODEL: PATH MODEL

Build this before the outcome model because replay can produce useful labels now.

Initial model goals:

```text
MFE quantiles
MAE quantiles
P(stop first)
P(target first)
time-to-MFE
time-to-failure
```

Prefer distributions/quantiles over a single mean.

Example output:

```json
{
  "mfe_r": {
    "p25": 0.42,
    "p50": 1.10,
    "p75": 1.88
  },
  "mae_r": {
    "p25": 0.11,
    "p50": 0.34,
    "p75": 0.71
  },
  "p_stop_first": 0.24,
  "p_target_first": 0.63,
  "time_to_mfe_bars_p50": 8,
  "uncertainty": 0.19
}
```

---

# 18. PATH MODEL ARCHITECTURE

Start simple.

Benchmark:

```text
linear / regularized baseline
gradient-boosted tree CPU baseline
small MLP
small temporal model only if sequence features justify it
```

Do not assume neural wins.

Example compact neural model:

```python
import torch
from torch import nn


class PathNet(nn.Module):
    def __init__(self, input_dim: int):
        super().__init__()

        self.body = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.GELU(),
            nn.LayerNorm(128),

            nn.Linear(128, 64),
            nn.GELU(),

            nn.Linear(64, 32),
            nn.GELU(),
        )

        # Example:
        # MFE q25/q50/q75
        # MAE q25/q50/q75
        self.quantiles = nn.Linear(32, 6)

        self.stop_first = nn.Linear(32, 1)
        self.target_first = nn.Linear(32, 1)

        self.time_to_mfe = nn.Linear(32, 1)
        self.time_to_failure = nn.Linear(32, 1)

    def forward(self, x):
        z = self.body(x)

        return {
            "quantiles": self.quantiles(z),
            "stop_first_logit": self.stop_first(z),
            "target_first_logit": self.target_first(z),
            "time_to_mfe": self.time_to_mfe(z),
            "time_to_failure": self.time_to_failure(z),
        }
```

---

# 19. QUANTILE LOSS

```python
import torch


def pinball_loss(
    prediction: torch.Tensor,
    target: torch.Tensor,
    q: float,
):
    error = target - prediction

    return torch.mean(
        torch.maximum(
            q * error,
            (q - 1.0) * error,
        )
    )
```

Evaluate whether Huber/regression targets work better.

Do not select the loss because it looks sophisticated.

---

# 20. CHRONOLOGICAL TRAINING

Never random-split the final evaluation.

Example:

```text
train     oldest 60%
validate  next 20%
test      newest 20%
```

Better:

```text
rolling walk-forward windows
```

All scalers/normalizers must be fit on the training window only.

---

# 21. WALK-FORWARD EXAMPLE

```python
def chronological_splits(rows, train_n, val_n, test_n):
    start = 0

    while start + train_n + val_n + test_n <= len(rows):
        train = rows[start : start + train_n]

        val_start = start + train_n
        val = rows[val_start : val_start + val_n]

        test_start = val_start + val_n
        test = rows[test_start : test_start + test_n]

        yield train, val, test

        start += test_n
```

Use timestamps, not accidental database order.

---

# 22. REPLAY VS LIVE WEIGHTING

Replay is useful bootstrap evidence.

Replay is not live evidence.

Record source.

Do not train/evaluate without being able to slice:

```text
replay only
live only
combined weighted
```

A model that only works on replay should not be promoted as a live model.

---

# 23. SHADOW MODE IS MANDATORY

The path model initially:

```text
predicts
stores prediction
does NOT alter trade
waits for realized result
measures residual
```

Only after sufficient OOS + live shadow evidence may it become a veto/filter input.

It must not directly move stops or targets initially.

---

# 24. PHASE 5 — BUILD THE DATA RECORDERS FOR FUTURE EXECUTION MODEL

Do this early enough that data starts accumulating.

Do not train execution ML yet.

Record:

```text
order intent
timestamp
venue
symbol
side
order type
notional
requested limit price
reference mid at submit
bid
ask
spread
L2 depth
book imbalance
tape flow
volatility
fill timestamp
average fill price
fill quantity
partial fills
cancel time
realized slippage
```

---

# 25. EXECUTION TRAINING RECORD

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ExecutionSample:
    submitted_at: datetime

    symbol: str
    venue: str
    side: str
    order_type: str

    reference_mid: float
    requested_price: float | None
    notional_usd: float

    spread_bps: float | None
    bid_depth: float | None
    ask_depth: float | None
    imbalance: float | None
    tape_flow: float | None
    realized_volatility: float | None

    filled_at: datetime | None
    avg_fill_price: float | None

    filled_qty: float | None
    requested_qty: float | None

    realized_slippage_bps: float | None
    fill_delay_ms: float | None
```

The feature snapshot must represent market state **when the order was submitted**.

Do not use post-fill book state as a training feature.

---

# 26. REALIZED SLIPPAGE

```python
def realized_slippage_bps(
    *,
    side: str,
    reference_mid: float,
    avg_fill_price: float,
) -> float:
    if reference_mid <= 0:
        raise ValueError("reference_mid must be positive")

    side = side.lower()

    if side == "buy":
        adverse = avg_fill_price - reference_mid
    elif side == "sell":
        adverse = reference_mid - avg_fill_price
    else:
        raise ValueError(f"Unsupported side: {side}")

    return adverse / reference_mid * 10_000.0
```

Positive = adverse slippage.

---

# 27. EXECUTION MODEL DATA GATE

Do not create an arbitrary "we have 100 samples so train" rule without evaluation.

But at minimum:

```text
4 samples → absolutely no model
```

Require:

```text
enough observations across
symbols
volatility regimes
order sizes
venues/order types
```

before training.

Until then:

```text
existing transaction_costs.py
+
existing conservative slippage fallback
```

remain authoritative.

---

# 28. PHASE 6 — CONDITIONAL FORWARD OUTCOME MODEL

After the path model infrastructure is proven, build a model that answers:

> Given this exact current state, what is the distribution of future returns?

This complements `expectancy.py`.

It does not replace it.

Historical expectancy asks:

```text
What has this class of setup earned?
```

Conditional model asks:

```text
Does this specific instance look better/worse than the class baseline?
```

---

# 29. RETURN-DISTRIBUTION OUTPUT

Initially use only horizons with adequate clean data.

Possible:

```text
5m
15m
1H
```

Do not force every timeframe.

Example:

```json
{
  "horizon": "15m",
  "p_large_down": 0.08,
  "p_small_down": 0.17,
  "p_flat": 0.14,
  "p_small_up": 0.38,
  "p_large_up": 0.23,
  "expected_return_pct": 0.31,
  "uncertainty": 0.18
}
```

Calibrate probabilities.

Measure:

```text
Brier score
log loss
ECE/calibration error
directional accuracy
conditional expectancy
incremental net expectancy after costs
```

Accuracy is not enough.

---

# 30. PHASE 7 — MARKET-STATE ENCODER

Only add this if it improves downstream models or analog retrieval.

Goal:

```text
large engineered feature vector
         │
         ▼
small latent market-state embedding
```

Example:

```text
128–300 features
       ↓
16–32 latent dimensions
```

Do not display latent dimensions as if they are named indicators.

---

# 31. MULTI-TASK ENCODER EXAMPLE

```python
import torch
from torch import nn


class MarketStateNet(nn.Module):
    def __init__(
        self,
        input_dim: int,
        latent_dim: int = 24,
    ):
        super().__init__()

        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.GELU(),
            nn.LayerNorm(128),

            nn.Linear(128, 64),
            nn.GELU(),

            nn.Linear(64, latent_dim),
        )

        self.return_head = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.GELU(),
            nn.Linear(32, 5),
        )

        self.path_head = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.GELU(),
            nn.Linear(32, 6),
        )

    def forward(self, x):
        z = self.encoder(x)

        return {
            "embedding": z,
            "return_logits": self.return_head(z),
            "path": self.path_head(z),
        }
```

Compare:

```text
separate models
vs
shared encoder
```

Do not assume multi-task learning is automatically better.

---

# 32. HISTORICAL ANALOG ENGINE

The NPU/CPU model creates embeddings.

Nearest-neighbor retrieval initially stays on CPU.

Do not add a vector database until scale requires it.

Input:

```text
current embedding
```

Search only observations earlier than the evaluation time.

Output:

```json
{
  "neighbors": 84,
  "median_similarity": 0.89,
  "same_strategy_neighbors": 31,
  "same_regime_neighbors": 47,
  "median_forward_15m_pct": 0.37,
  "median_mfe_r": 1.21,
  "median_mae_r": 0.32,
  "positive_net_r_rate": 0.61
}
```

---

# 33. COSINE ANALOG RETRIEVAL

```python
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class Neighbor:
    index: int
    similarity: float


def cosine_neighbors(
    query: np.ndarray,
    history: np.ndarray,
    *,
    top_k: int = 100,
    min_similarity: float = 0.80,
):
    query = np.asarray(query, dtype=np.float32).reshape(-1)
    history = np.asarray(history, dtype=np.float32)

    q_norm = np.linalg.norm(query)
    h_norm = np.linalg.norm(history, axis=1)

    denominator = q_norm * h_norm

    valid = denominator > 0

    similarity = np.full(
        len(history),
        -1.0,
        dtype=np.float32,
    )

    similarity[valid] = (
        history[valid] @ query
    ) / denominator[valid]

    order = np.argsort(similarity)[::-1][:top_k]

    return [
        Neighbor(
            index=int(i),
            similarity=float(similarity[i]),
        )
        for i in order
        if similarity[i] >= min_similarity
    ]
```

Time filtering happens before this function.

---

# 34. TIME-SAFE ANALOG FILTER

```python
def before_observation(rows, observation_time):
    return [
        row
        for row in rows
        if row["timestamp"] < observation_time
    ]
```

Backtest analog retrieval must never see the future.

---

# 35. CROSS-TIMEFRAME LEARNED CONFLICT

Do not replace deterministic contradiction counting.

Add a model only if it can learn which disagreement patterns matter historically.

Example input:

```text
1D trend
4H trend
1H structure
15m momentum
5m tape
relative strength
regime axes
```

Possible output:

```json
{
  "alignment": 0.42,
  "pullback_probability": 0.71,
  "continuation_probability": 0.58,
  "reversal_probability": 0.16,
  "conflict_severity": 0.81
}
```

Use as evidence.

---

# 36. LLM ROUTER INTEGRATION

Do not replace `lib/llm_router.py`.

Feed validated predictive evidence into its existing deterministic AUTO logic.

Only trusted/fresh model output may trigger escalation.

Example:

```python
npu_trust = _num(ctx, "predictive_trust")

conflict = _num(ctx, "predictive_conflict_severity")

if (
    npu_trust is not None
    and npu_trust >= 0.70
    and conflict is not None
    and conflict >= 0.75
):
    fired.append(
        "validated predictive model sees "
        "historically meaningful timeframe conflict"
    )
```

Potential future triggers:

```text
high conditional uncertainty
elevated drift
high tail/path uncertainty
historically abnormal state
```

Do not let untrusted predictions consume expensive DEEP reasoning.

---

# 37. PHASE 8 — DRIFT / TRUST

Do not wait for a strategy to lose 60 more trades before noticing its input-output relationship has changed.

But do not replace `strategy_lifecycle.py`.

The drift layer is early warning.

Monitor:

```text
feature drift
embedding drift
prediction residual drift
calibration drift
MFE/MAE residual drift
strategy-conditioned degradation
regime-conditioned degradation
future execution residual drift
```

---

# 38. SIMPLE PSI EXAMPLE

```python
import numpy as np


def population_stability_index(
    expected,
    actual,
    *,
    bins: int = 10,
    eps: float = 1e-6,
):
    expected = np.asarray(expected)
    actual = np.asarray(actual)

    edges = np.quantile(
        expected,
        np.linspace(0, 1, bins + 1),
    )

    edges[0] = -np.inf
    edges[-1] = np.inf

    e_hist, _ = np.histogram(expected, bins=edges)
    a_hist, _ = np.histogram(actual, bins=edges)

    e_pct = np.maximum(
        e_hist / max(1, e_hist.sum()),
        eps,
    )

    a_pct = np.maximum(
        a_hist / max(1, a_hist.sum()),
        eps,
    )

    return float(
        np.sum(
            (a_pct - e_pct)
            * np.log(a_pct / e_pct)
        )
    )
```

Do not copy generic internet PSI threshold folklore into trading decisions.

Calibrate thresholds against JARVIS's own false alarms and degradation.

---

# 39. MODEL TRUST

Trust should be explicit and interpretable before considering another ML model to compute it.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TrustInputs:
    calibration_quality: float
    residual_quality: float
    feature_stability: float
    freshness: float
    sample_support: float


def compute_trust(x: TrustInputs) -> float:
    value = (
        0.30 * x.calibration_quality
        + 0.25 * x.residual_quality
        + 0.20 * x.feature_stability
        + 0.15 * x.freshness
        + 0.10 * x.sample_support
    )

    return max(
        0.0,
        min(1.0, value),
    )
```

Weights above are placeholders.

Validate them or expose them as configuration.

---

# 40. CHAMPION / CHALLENGER

Statuses:

```text
CHAMPION
CHALLENGER
SHADOW
DEGRADED
DISABLED
```

A challenger must prove itself on chronological unseen data and live shadow data.

Do not promote based on one metric.

Possible checks:

```text
Brier score
calibration
path error
incremental net EV
tail-loss behavior
drawdown
live-vs-replay robustness
```

---

# 41. MODEL REGISTRY

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    version: str
    feature_schema: str

    status: str

    model_path: str

    preferred_device: str
    fallback_device: str

    trained_through: str

    training_samples: int
    validation_samples: int
    test_samples: int

    metrics: dict
```

Do not overwrite model files in place.

Make rollback possible.

---

# 42. OPENVINO EXPORT

Current OpenVINO supports PyTorch conversion using `ov.convert_model`.

Example:

```python
from pathlib import Path

import openvino as ov
import torch


def export_openvino(
    model,
    *,
    input_dim: int,
    output_path: str,
):
    model = model.eval().cpu()

    example = torch.zeros(
        (1, input_dim),
        dtype=torch.float32,
    )

    ov_model = ov.convert_model(
        model,
        example_input=example,
    )

    path = Path(output_path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ov.save_model(
        ov_model,
        str(path),
    )
```

---

# 43. EXPORT PARITY

Before registering any model:

```text
PyTorch output
vs
OpenVINO CPU output
vs
OpenVINO NPU output
```

must be within appropriate tolerances.

Do not assume quantized output should use the same tolerance as FP output.

---

# 44. QUANTIZATION

OpenVINO/NNCF support quantization and QAT.

But do not quantize simply because NPU exists.

For these small models, quantization may not matter operationally.

Evaluate:

```text
model size
latency
throughput
calibration
OOS expectancy
tail prediction
```

If quantization damages predictive quality:

```text
do not deploy it
```

---

# 45. PHASE 9 — ADAPTIVE NEUROSTATE

Build only after useful predictive signals exist.

Neurotransmitter names are interpretable controller variables.

They are not a pretend brain.

The arithmetic itself should run on CPU.

---

# 46. NEUROSTATE SEMANTICS

## Dopamine

Reward prediction error.

```text
realized R - predicted R
```

NOT:

```text
profit = dopamine
```

## Norepinephrine

Surprise / instability.

Derived from:

```text
prediction residual
regime-transition evidence
model drift
volatility shock
major catalyst
correlation disruption
```

## Acetylcholine

Attention / uncertainty.

Derived from:

```text
uncertainty
novelty
anomaly
importance
```

## GABA

Inhibition.

Derived from:

```text
tail risk
drawdown
bad liquidity
execution risk
model degradation
strategy instability
```

May only tighten decisions.

## Glutamate

Opportunity activation.

Derived from:

```text
independent evidence convergence
favorable validated conditional forecast
usable expected MFE
```

Does not mean BUY.

## Serotonin

System stability.

Derived from:

```text
calibration stability
strategy stability
model health
execution stability
```

---

# 47. NEUROSTATE EXAMPLE

```python
from dataclasses import dataclass


def clamp01(x):
    return max(0.0, min(1.0, float(x)))


@dataclass
class NeuroState:
    dopamine: float = 0.50
    norepinephrine: float = 0.50
    acetylcholine: float = 0.50
    gaba: float = 0.50
    glutamate: float = 0.50
    serotonin: float = 0.50


def homeostasis(
    value: float,
    *,
    baseline: float = 0.50,
    rate: float = 0.03,
):
    return value + (baseline - value) * rate


def update_neurostate(
    state: NeuroState,
    *,
    reward_prediction_error_r: float,
    surprise: float,
    uncertainty: float,
    tail_risk: float,
    execution_risk: float,
    evidence_convergence: float,
    model_stability: float,
):
    for field in state.__dataclass_fields__:
        setattr(
            state,
            field,
            homeostasis(
                getattr(state, field),
            ),
        )

    state.dopamine = clamp01(
        state.dopamine
        + 0.08 * reward_prediction_error_r
    )

    state.norepinephrine = clamp01(
        state.norepinephrine
        + 0.12 * surprise
    )

    state.acetylcholine = clamp01(
        state.acetylcholine
        + 0.10 * uncertainty
    )

    state.gaba = clamp01(
        state.gaba
        + 0.08 * tail_risk
        + 0.06 * execution_risk
    )

    state.glutamate = clamp01(
        state.glutamate
        + 0.08 * evidence_convergence
    )

    state.serotonin = clamp01(
        state.serotonin
        + 0.06 * (model_stability - 0.5)
    )

    return state
```

Coefficients are placeholders.

Do not hard-code them as permanent financial truths.

---

# 48. NEUROSTATE CONTROL OUTPUT

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AdaptiveControls:
    processing_priority: float
    min_ev_multiplier: float
    model_weight_multiplier: float
    deep_reasoning_pressure: float


def controls(state: NeuroState):
    processing = clamp01(
        0.45 * state.acetylcholine
        + 0.35 * state.norepinephrine
        + 0.20 * state.glutamate
    )

    # Never below 1.0.
    min_ev_multiplier = (
        1.0
        + 0.50 * state.gaba
    )

    model_weight = clamp01(
        0.50 * state.serotonin
        + 0.50 * (1.0 - state.norepinephrine)
    )

    deep_pressure = clamp01(
        0.50 * state.norepinephrine
        + 0.30 * state.acetylcholine
        + 0.20 * state.gaba
    )

    return AdaptiveControls(
        processing_priority=processing,
        min_ev_multiplier=min_ev_multiplier,
        model_weight_multiplier=model_weight,
        deep_reasoning_pressure=deep_pressure,
    )
```

Adaptive state may make JARVIS more cautious.

It may not loosen hard limits.

---

# 49. HEBBIAN/PATTERN LEARNING

Do not replace sample-aware expectancy with Hebbian weights.

Hebbian-like memory may help discover hypotheses.

Lifecycle:

```text
association discovered
        ↓
HYPOTHESIS
        ↓
historical evaluation
        ↓
walk-forward
        ↓
SHADOW
        ↓
possibly promoted
```

Never:

```text
association discovered
        ↓
live size increase
```

---

# 50. META-FILTER

Create a conservative integration layer only after models have proven value.

Concept:

```text
existing expectancy
+
conditional model
+
path model
+
analog history
+
model trust
=
meta evidence
```

Initial labels:

```text
STRONGLY_SUPPORTS
SUPPORTS
NEUTRAL
CONFLICTS
STRONGLY_CONFLICTS
ABSTAIN
```

---

# 51. NO-RESURRECTION RULE IN CODE

```python
def combine_verdict(
    base_verdict: str,
    *,
    predictive_veto: bool,
):
    if base_verdict == "NO_TRADE":
        return "NO_TRADE"

    if predictive_veto:
        return "NO_TRADE"

    return base_verdict
```

This is deliberate.

The ML layer does not get to "outvote" negative deterministic net expectancy or hard risk constraints.

---

# 52. INTEGRATION WITH TRANSACTION COSTS

Once a future execution model is actually validated:

```text
predicted slippage
        ↓
transaction_costs.py
        ↓
cost in R
        ↓
expectancy.py
```

Do not create a second cost engine.

If execution prediction is unavailable/stale/untrusted:

```text
use existing conservative fallback
```

---

# 53. EXECUTION PREDICTION INTEGRATION EXAMPLE

For future use only after data sufficiency:

```python
def estimate_with_predictive_slippage(
    signal,
    execution_prediction,
):
    from lib.transaction_costs import estimate_costs

    slippage_pct = None

    if execution_prediction:
        trust = execution_prediction.get("trust")
        age_ms = execution_prediction.get("age_ms")
        bps = execution_prediction.get(
            "expected_slippage_bps"
        )

        if (
            trust is not None
            and trust >= 0.70
            and age_ms is not None
            and age_ms <= 2_000
            and bps is not None
        ):
            slippage_pct = (
                float(bps) / 10_000.0
            )

    return estimate_costs(
        signal["asset_symbol"],
        signal["entry_price"],
        signal["stop_loss"],
        slippage_pct=slippage_pct,
        venue=signal.get("venue"),
        leveraged=bool(signal.get("leverage")),
        is_short=str(
            signal.get("direction", "")
        ).lower().startswith("short"),
    )
```

`None` intentionally triggers existing fallback behavior.

---

# 54. MODEL FREQUENCY

Do not run every model on every tick.

Suggested:

```text
L2/tape message
│
├── update CPU live features
└── do NOT necessarily infer

candidate trade created
│
├── build current feature vector
├── path model
├── conditional outcome model when available
├── state encoder when available
├── analog lookup when available
└── meta evidence

active order
│
└── future execution model at decision-relevant changes

trade close
│
├── resolve prediction
├── persist residuals
└── update adaptive controller

minutes/hours
│
└── drift/model-health scans
```

---

# 55. ASSET-CLASS MODEL POLICY

Do not assume one universal model wins.

Benchmark:

```text
shared model + asset-class features
vs
crypto-specific
vs
equity-specific
vs
futures-specific
```

Only split when sample size supports it.

Avoid creating dozens of tiny undertrained models.

---

# 56. PRODUCTION DEVICE SELECTION PER MODEL

Model registry should support:

```json
{
  "name": "path_intraday",
  "preferred_device": "NPU",
  "fallback_device": "CPU"
}
```

or:

```json
{
  "name": "tiny_meta",
  "preferred_device": "CPU",
  "fallback_device": "NPU"
}
```

Do not treat running on CPU as failure.

---

# 57. ABLATION — REQUIRED

For every model:

```text
A = baseline JARVIS
B = A + model output in SHADOW
C = A + model as veto/filter
```

For hardware:

```text
same exact model:
CPU
vs
NPU
```

For combinations:

```text
baseline
+ path
+ outcome
+ encoder/analogs
+ drift/trust
+ adaptive controller
```

Measure:

```text
net expectancy
profit factor
drawdown
tail losses
calibration
trade rejection quality
MFE/MAE error
signal frequency
execution costs
latency
resource contention
```

If a component adds no measurable value:

```text
disable/delete it
```

---

# 58. DO NOT CONFUSE PREDICTION QUALITY WITH PROFITABILITY

Example:

```text
model improves directional accuracy
but only on tiny moves
and costs consume the edge
```

Then the model may be useless for trading.

Always evaluate:

```text
after costs
```

---

# 59. NO-LOOKAHEAD TESTING

Add tests that intentionally attempt leakage.

Examples:

```text
future OHLC included in feature vector → test must fail
future analog available during historical replay → test must fail
normalizer fit on validation/test → test must fail
post-fill order book stored as submit feature → test must fail
```

---

# 60. TESTS FOR PATH LABELS

Cases:

```text
clean long target first
clean long stop first
clean short target first
clean short stop first
both touched same bar → AMBIGUOUS
no touch
zero risk distance
MFE before stop
MAE before target
```

Verify R calculations.

---

# 61. TESTS FOR PREDICTIVE RUNTIME

Cases:

```text
NPU available
NPU unavailable
CPU available
preferred CPU
preferred NPU
NPU compile failure
NPU inference failure
CPU fallback failure
schema mismatch
stale input
too much missing data
queue coalescing
prediction persistence
```

---

# 62. TEST HARD BOUNDARIES

Explicitly test:

```text
predictive model cannot set leverage
predictive model cannot set qty
predictive model cannot calculate fee authority
predictive model cannot set liquidation
predictive model cannot override NO_TRADE
neurostate cannot lower hard min EV below baseline
neurostate cannot disable risk checks
LLM router only consumes trusted/fresh predictive fields
```

---

# 63. OBSERVABILITY

Add status information without cluttering the main trading UI.

Useful:

```text
Predictive Engine
CPU/NPU availability
per-model device
model version
status
latency p50/p95/p99
fallback rate
queue depth
prediction count
shadow count
drift/trust
last error
```

Model panel:

```text
Path model:
CHAMPION / SHADOW
device: NPU
trust: 0.81
MFE calibration: ...
MAE error: ...
live observations: ...
```

---

# 64. SUGGESTED API

Adapt to existing route conventions.

Conceptually:

```text
GET /api/predictive/status
GET /api/predictive/models
GET /api/predictive/predictions/{symbol}
GET /api/predictive/analogs/{symbol}
GET /api/predictive/drift
```

No endpoint should leak credentials or sensitive broker configuration.

---

# 65. RECOMMENDED REPOSITORY STRUCTURE

Adapt to actual current conventions.

```text
jarvis-trading/
│
├── lib/
│   ├── expectancy.py
│   ├── calibration.py
│   ├── strategy_lifecycle.py
│   ├── transaction_costs.py
│   ├── strategies.py
│   ├── regime_axes.py
│   ├── llm_router.py
│   │
│   └── predictive/
│       ├── __init__.py
│       ├── config.py
│       ├── runtime.py
│       ├── device_policy.py
│       ├── queue.py
│       ├── schemas.py
│       ├── features.py
│       ├── normalization.py
│       ├── model_registry.py
│       ├── prediction_store.py
│       ├── health.py
│       ├── metrics.py
│       │
│       ├── path_model.py
│       ├── outcome_model.py
│       ├── state_encoder.py
│       ├── analogs.py
│       ├── timeframe_model.py
│       ├── drift.py
│       ├── neurostate.py
│       └── meta_filter.py
│
├── ml/
│   ├── README.md
│   │
│   ├── datasets/
│   │   ├── build_path_dataset.py
│   │   ├── build_feature_dataset.py
│   │   └── build_execution_dataset.py
│   │
│   ├── training/
│   │   ├── train_path.py
│   │   ├── train_outcome.py
│   │   └── train_encoder.py
│   │
│   ├── evaluation/
│   │   ├── walk_forward.py
│   │   ├── ablation.py
│   │   ├── calibration.py
│   │   └── device_benchmark.py
│   │
│   └── export/
│       ├── export_openvino.py
│       └── quantize.py
│
├── models/
│   └── predictive/
│       ├── registry.json
│       ├── path/
│       ├── outcome/
│       └── encoder/
│
└── tests/
    ├── test_predictive_runtime.py
    ├── test_predictive_features.py
    ├── test_predictive_leakage.py
    ├── test_path_labels.py
    ├── test_path_model.py
    ├── test_analogs.py
    ├── test_predictive_drift.py
    └── test_predictive_integration.py
```

If a `lib/npu` implementation already exists when this is run, inspect it before creating another layer.

Refactor rather than duplicate.

---

# 66. DATA FLOW FOR FIRST DEPLOYABLE VERSION

```text
                         EXISTING JARVIS
                               │
                               ▼
                       STRATEGY CANDIDATE
                               │
                               ▼
                      canonical features
                               │
                               ▼
                         PATH MODEL
                          CPU or NPU
                               │
                               ▼
                      prediction persisted
                               │
                    SHADOW MODE ONLY initially
                               │
                               ▼
                       EXISTING TRADE LOGIC
                               │
                               ▼
                         realized result
                               │
                               ▼
                        resolve prediction
                               │
                               ▼
                     residual/calibration
                               │
                               ▼
                      prove value or remove
```

This is the first milestone.

Do not jump directly to an elaborate neuro system.

---

# 67. DATA FLOW AFTER OUTCOME MODEL

```text
strategy candidate
       │
       ├──────────────▶ historical expectancy
       │                     existing
       │
       ├──────────────▶ path model
       │
       └──────────────▶ conditional outcome model
                             │
                             ▼
                      predictive evidence
                             │
                             ▼
                         meta filter
                             │
              ┌──────────────┼─────────────┐
              ▼              ▼             ▼
          neutral        conflict        veto
              │              │             │
              └──────────────┼─────────────┘
                             ▼
                       existing JARVIS
```

---

# 68. EVENTUAL FULL FLOW

```text
                           MARKET
                              │
                              ▼
                       EXISTING JARVIS
                              │
                              ▼
                      CANONICAL FEATURES
                              │
             ┌────────────────┼─────────────────┐
             ▼                ▼                 ▼
         PATH MODEL      OUTCOME MODEL      ENCODER
             │                │                 │
             │                │                 ▼
             │                │              ANALOGS
             │                │                 │
             └────────────────┼─────────────────┘
                              ▼
                         DRIFT/TRUST
                              │
                              ▼
                          NEUROSTATE
                              │
                 ┌────────────┼─────────────┐
                 ▼            ▼             ▼
              FILTER        LLM ROUTER   ATTENTION
                 │            │
                 └──────┬─────┘
                        ▼
                EXISTING EXPECTANCY/RISK
                        │
                        ▼
                     EXECUTION
```

Execution prediction is added only when actual data supports it.

---

# 69. DEFINITION OF SUCCESS

Do not report success because:

```text
NPU is active
models load
GPU trained successfully
loss decreased
accuracy improved
```

Success requires measurable improvement in at least one trading-relevant dimension without unacceptable degradation elsewhere.

Examples:

```text
higher net expected R
better rejection of poor setups
lower tail losses
improved MFE/MAE calibration
better stop/target realism
lower drawdown
earlier strategy degradation detection
better resource isolation
less CPU scheduling jitter under load
```

All claims need a baseline.

---

# 70. CLAUDE CODE EXECUTION INSTRUCTIONS

When implementing this specification:

1. Inspect the entire current repository first.
2. Do not rely on this document for exact current line numbers.
3. Confirm existing modules before creating new files.
4. Preserve working deterministic logic.
5. Implement Phase 0 first.
6. Add replay path labels before path-model training.
7. Build tests with each phase.
8. Build prediction persistence before model influence.
9. Default all ML models to SHADOW.
10. Benchmark both CPU and NPU.
11. Never treat CPU inference as failure.
12. Do not train execution ML from the current tiny fill dataset.
13. Begin execution-data collection immediately.
14. Use chronological OOS validation.
15. Preserve live/replay provenance.
16. Do not let a model use future information.
17. Do not let ML/LLM outputs become arithmetic authority.
18. Do not let predictive ML flip hard NO_TRADE to TRADE.
19. Make every model rollbackable.
20. Measure model value separately from device-placement value.
21. Do not build the neurostate until useful predictive models exist.
22. Do not rewrite unrelated working systems.
23. If a model adds no measurable OOS value, disable or remove it.
24. Report actual measured results, not assumptions.

---

# 71. COMPLETION CHECKLIST

Do not call the project complete until relevant boxes are satisfied.

## Statistical correctness

```text
[ ] tiny-sample pattern memory no longer claims authority
[ ] pattern memory labels uncertainty/sample size honestly
[ ] calibration/expectancy remain statistical authority
```

## Replay/path data

```text
[ ] MFE recorded
[ ] MAE recorded
[ ] MFE/MAE in R recorded
[ ] first-touch recorded
[ ] ambiguous OHLC touch handled honestly
[ ] bars/time to MFE recorded
[ ] replay provenance retained
```

## Predictive infrastructure

```text
[ ] CPU device works
[ ] NPU device works
[ ] model device configurable per model
[ ] CPU/NPU parity measured
[ ] CPU/NPU latency measured
[ ] fallback works
[ ] model failure returns abstain/None
[ ] schema mismatch fails closed
[ ] stale inputs abstain
[ ] missingness preserved
[ ] latest-only queue prevents stale backlog
```

## Path model

```text
[ ] chronological train/validation/test
[ ] replay/live sources separable
[ ] simple non-neural baseline tested
[ ] MLP tested
[ ] quantile outputs evaluated
[ ] first-touch calibration measured
[ ] PyTorch/OpenVINO parity tested
[ ] CPU/NPU deployment benchmarked
[ ] shadow predictions persisted
[ ] live residuals collected
```

## Execution data

```text
[ ] order-intent recorder exists
[ ] submit-time quote stored
[ ] submit-time L2/tape features stored
[ ] fill timestamps stored
[ ] partial fills stored
[ ] realized slippage computed
[ ] no execution model trained prematurely
```

## Safety

```text
[ ] predictive ML cannot set leverage
[ ] predictive ML cannot set quantity
[ ] predictive ML cannot calculate authoritative fees
[ ] predictive ML cannot set liquidation
[ ] predictive ML cannot override hard risk
[ ] predictive ML cannot resurrect NO_TRADE
[ ] neurostate can only tighten hard safety envelope
```

## Evaluation

```text
[ ] JARVIS baseline recorded
[ ] baseline + model compared
[ ] same model CPU vs NPU compared
[ ] OOS net expectancy compared
[ ] drawdown compared
[ ] tail losses compared
[ ] latency/resource impact compared
[ ] model promotion requires evidence
```

---

# FINAL PRINCIPLE

When deciding whether to put something into ML, use this test:

> **If there is one mathematically correct answer from known inputs, keep it deterministic. If the task estimates an uncertain future quantity from historical patterns, it may belong in ML.**

When deciding whether to put a model on the NPU, use this test:

> **Run the same validated model on CPU and NPU. Choose the device that improves the complete JARVIS system under realistic load—not the device that sounds more specialized.**

The finished system should be:

```text
more predictive
more measurable
more adaptive
more statistically disciplined
more resistant to stale relationships
more aware of path/risk
more efficient with compute resources
```

without making JARVIS less deterministic where determinism is a strength.
