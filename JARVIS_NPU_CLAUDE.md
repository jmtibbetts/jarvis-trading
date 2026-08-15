# JARVIS NPU Predictive Intelligence Upgrade

## Mission

Upgrade JARVIS with an Intel NPU-backed predictive intelligence layer that adds capabilities the repository does **not already have**.

Do not rebuild working deterministic systems as neural networks. The NPU is not a second trading engine and is not an alternative source of truth for fees, P&L, position sizing, leverage, liquidation, technical indicators, strategy identity, or lifecycle state.

The purpose of the NPU is to convert JARVIS's existing feature, market-data, execution, and outcome streams into continuously updated **probabilistic forecasts** that improve the existing deterministic decision architecture.

Core principle:

> Neural models predict uncertain future quantities. Deterministic code owns arithmetic, accounting, constraints, and final safety gates.

The target architecture is:

```text
Market feeds / TA / structure / strategies / derivatives / L2 / tape
                              |
                              v
                    Canonical Feature Vector
                              |
                              v
                         Intel NPU
       +----------------------+-----------------------+
       |                      |                       |
       v                      v                       v
 Market-State Encoder   Outcome/MFE/MAE         Execution Model
       |                 Forecast Models              |
       |                      |                       |
       +-----------+----------+-----------------------+
                   |
                   v
             Drift / Trust Layer
                   |
          +--------+---------+
          |                  |
          v                  v
 Historical Analogs   Adaptive Control Signals
          |                  |
          +--------+---------+
                   v
             Existing JARVIS
  expectancy / transaction costs / lifecycle /
  risk manager / LLM router / execution engine
```

---

# 1. NON-NEGOTIABLE ARCHITECTURE RULES

Before modifying code, inspect the current repository and reuse existing interfaces wherever possible.

## Never replace these deterministic authorities

The following existing modules remain authoritative:

- `lib/expectancy.py` — historical/net expectancy and TRADE/NO_TRADE logic
- `lib/calibration.py` — empirical calibration
- `lib/strategy_lifecycle.py` — ACTIVE/REDUCED/EXPERIMENTAL/SHADOW/DISABLED
- `lib/transaction_costs.py` — spread/fees/slippage/funding/borrow arithmetic in R
- `lib/strategies.py` — deterministic strategy classification
- `lib/regime_axes.py` — deterministic measured regime axes
- `lib/llm_router.py` — FAST/AUTO/DEEP routing and LLM numeric-authority boundaries
- risk/position sizing/liquidation/P&L modules already present in the repository

Do not create competing implementations of these concepts.

NPU outputs are **features/evidence**, not unquestioned truth.

Every model output must include:

- model name
- model version
- feature schema version
- inference timestamp
- inference latency
- confidence/uncertainty where applicable
- freshness/age of source features
- whether NPU, CPU fallback, or unavailable path produced the result

No prediction may silently default to a favorable value when unavailable.

Unavailable prediction = abstain.

---

# 2. HARDWARE TARGET AND RUNTIME

Primary target is the Intel NPU available on the JARVIS host.

Use OpenVINO as the preferred inference runtime unless repository/runtime inspection demonstrates a better supported Intel NPU path.

Requirements:

1. Detect NPU capability at startup.
2. Prefer NPU for supported inference graphs.
3. Support CPU inference fallback for correctness/testing.
4. Never move these workloads to the RTX GPU by default; preserve GPU capacity for local LLM workloads.
5. NPU failure must never stop market data, execution, risk management, or the API.
6. Models must be independently loadable/reloadable.
7. Use bounded queues and backpressure. Never allow stale inference requests to accumulate indefinitely.
8. Measure p50/p95/p99 inference latency per model.
9. Reject/abstain on feature-schema mismatch rather than guessing.

Create a central runtime abstraction such as:

`lib/npu/runtime.py`

with an interface conceptually similar to:

```python
runtime.status()
runtime.load_model(name, path, schema_version)
runtime.infer(name, features)
runtime.reload(name)
runtime.health()
```

Do not force callers to know OpenVINO details.

---

# 3. CANONICAL FEATURE PIPELINE

Create one canonical feature contract rather than allowing every model to independently scrape JARVIS state.

Suggested files:

```text
lib/npu/__init__.py
lib/npu/runtime.py
lib/npu/features.py
lib/npu/schemas.py
lib/npu/normalization.py
```

The feature builder should consume existing outputs rather than recompute indicators.

Candidate inputs include, where available:

### Price / TA
- normalized returns
- ATR and ATR percentile
- realized volatility
- EMA distances/slopes
- ADX
- RSI
- MACD state
- VWAP distance
- Bollinger position
- Donchian state
- volume ratio
- OBV / MFI / other existing flow indicators

### Structure
- support/resistance distances in ATR
- break state
- break age
- held/failed/sweep state
- break volume ratio
- market structure

### Strategy
- deterministic strategy ID
- deterministic match score
- strategy evidence count
- lifecycle state
- OOS expectancy
- historical expectancy

### Regime
Use the existing independent axes rather than inventing a second regime classifier:

- trend state/score/confidence
- volatility state/score/confidence
- liquidity state/score/confidence
- flow state/score/confidence

### Relative/cross-asset state
- relative strength
- benchmark returns
- BTC/ETH or relevant benchmark state
- correlation/beta features already available

### Crypto derivatives
- funding rate
- funding percentile if available
- open interest
- OI change
- basis if available
- liquidation-related public metrics if already collected

### Microstructure
Consume existing live streams:

- spread bps
- bid/ask depth
- order-book imbalance
- Kraken tape flow imbalance
- trade-size distribution
- recent aggressive buy/sell volume
- depth changes
- spread changes

### Execution context
- venue
- order type
- notional bucket
- stop distance
- leverage bucket
- expected holding horizon
- recent measured slippage

### Time
- time-of-day encoding
- day-of-week encoding
- market-session encoding
- time since major catalyst where available

All numerical features should have explicit units and normalization rules.

Never normalize using future data.

Persist the feature schema and normalization metadata with every trained model.

---

# 4. MARKET-STATE ENCODER

Implement a compact learned representation of the current market state.

Suggested file:

`lib/npu/state_encoder.py`

Goal:

Compress the canonical feature vector into approximately 16–32 latent dimensions useful for downstream models and similarity retrieval.

Do not expose latent dimensions as human-interpretable trading signals.

They are internal representations.

Training must be chronological and leakage-safe.

Possible architectures:

- compact autoencoder
- supervised representation network
- multi-task encoder shared by forward-return/MFE/MAE heads

Prefer a multi-task representation if validation shows it predicts trading-relevant quantities better than a reconstruction-only autoencoder.

Outputs:

```json
{
  "embedding": [0.12, -0.44, 0.08],
  "dimension": 24,
  "model_version": "...",
  "schema_version": "...",
  "latency_ms": 1.7
}
```

Acceptance criterion:

The encoder is useful only if downstream OOS performance or historical-neighbor quality improves relative to raw-feature baselines.

Do not keep it merely because dimensionality reduction sounds sophisticated.

---

# 5. HISTORICAL ANALOG ENGINE

Create:

`lib/npu/analogs.py`

Use market-state embeddings to retrieve historically similar states.

The analog engine should answer:

> What happened after market states most similar to the current one?

Store/index embeddings with:

- timestamp
- symbol
- asset class
- timeframe
- strategy
- regime axes
- forward returns
- MFE
- MAE
- eventual outcome
- realized execution cost when available

Similarity must never search observations later than the timestamp being evaluated during backtests/replay.

For live inference, search only historical observations.

Return interpretable statistics such as:

```json
{
  "neighbors": 143,
  "median_similarity": 0.91,
  "positive_return_rate": 0.63,
  "median_forward_return_pct": 0.41,
  "median_mfe_r": 1.28,
  "median_mae_r": 0.37,
  "median_net_r": 0.29,
  "strategy_subset_n": 48
}
```

Use minimum-sample requirements and abstain when neighbors are too sparse or too dissimilar.

Do not present nearest-neighbor results as probabilities unless calibrated as probabilities.

---

# 6. FORWARD OUTCOME DISTRIBUTION MODEL

Create:

`lib/npu/outcome_model.py`

This is one of the highest-priority NPU workloads.

The model should estimate the conditional distribution of short-horizon outcomes from the **current exact feature state**, complementing the historical bucket expectancy in `lib/expectancy.py`.

Initially prioritize horizons where JARVIS has sufficient clean data, likely including combinations around:

- 5m
- 15m
- 1H

Do not blindly train every timeframe. Require sufficient samples and OOS validation.

Possible output classes:

```text
large_down
small_down
flat
small_up
large_up
```

Also provide expected forward return and uncertainty when statistically defensible.

Example output:

```json
{
  "horizon": "15m",
  "p_large_down": 0.08,
  "p_small_down": 0.17,
  "p_flat": 0.14,
  "p_small_up": 0.38,
  "p_large_up": 0.23,
  "expected_return_pct": 0.31,
  "uncertainty": 0.18,
  "calibration_error": 0.04
}
```

Probability outputs must be calibrated using a held-out chronological validation set.

Measure at minimum:

- log loss
- Brier score
- calibration error
- directional accuracy
- expectancy when used as a filter
- incremental expectancy relative to existing JARVIS alone

Accuracy alone is not sufficient.

The model is successful only if it improves net trading decisions after transaction costs.

---

# 7. MFE / MAE / PATH MODEL

Create:

`lib/npu/path_model.py`

This should predict trade-path characteristics rather than only final direction.

Targets:

- expected MFE in R
- expected MAE in R
- probability stop is reached before TP1
- probability TP1 is reached before stop
- probability TP2 is reached before stop where applicable
- expected/median time to MFE
- expected/median time to failure
- edge half-life / expected useful holding duration where supportable

Example:

```json
{
  "expected_mfe_r": 1.76,
  "expected_mae_r": 0.43,
  "p_stop_first": 0.22,
  "p_tp1_first": 0.66,
  "median_time_to_mfe_min": 19,
  "median_time_to_failure_min": 11
}
```

Integrate this conservatively.

Initial mode should be SHADOW/OBSERVE ONLY.

Do not allow the model to directly move stops or targets with live money until its predictions have been validated out-of-sample and against live observed trades.

Later integration may use path forecasts to:

- reject trades whose predicted MAE is inconsistent with the stop
- avoid targets beyond realistic MFE
- improve holding windows
- improve trailing-stop activation
- distinguish a high-quality setup from one with the same historical strategy expectancy but poor current path forecast

The deterministic risk manager remains final authority.

---

# 8. EXECUTION / SLIPPAGE MODEL

Create:

`lib/npu/execution_model.py`

This is the other highest-priority workload.

Do not replace `lib/transaction_costs.py`.

Predict uncertain execution inputs and pass them into the deterministic cost engine.

Targets may include:

- expected market-order slippage bps
- slippage distribution/upper quantile
- limit fill probability over short windows
- expected fill delay
- adverse-selection probability
- expected spread expansion immediately after submission

Inputs should include:

- current spread
- L2 depth
- imbalance
- recent depth change
- tape flow
- volatility
- order size relative to visible depth
- venue
- symbol
- time of day
- recent measured fills

Example:

```json
{
  "expected_slippage_bps": 3.8,
  "p90_slippage_bps": 8.4,
  "limit_fill_probability_5s": 0.29,
  "limit_fill_probability_30s": 0.76,
  "adverse_selection_probability": 0.31
}
```

Integration:

```text
NPU expected slippage
        |
        v
transaction_costs.estimate_costs(... slippage_pct=...)
        |
        v
net expected R
        |
        v
existing expectancy TRADE / NO_TRADE
```

When model confidence is low or features are stale, use the existing conservative deterministic slippage behavior.

Never substitute zero.

---

# 9. CROSS-HORIZON / MULTI-TIMEFRAME MODEL

Create:

`lib/npu/timeframe_model.py`

Current deterministic contradiction counts are useful but do not distinguish harmless disagreement from historically meaningful disagreement.

Learn relationships such as:

```text
1D trend bullish
4H trend bullish
1H structure bearish
15m momentum bearish
5m tape strongly positive
```

Outputs can include:

- alignment score
- pullback probability
- continuation probability
- reversal probability
- conflict severity

Feed these outputs into existing scoring/LLM routing as evidence.

Do not replace the deterministic timeframe signals.

A learned conflict severity can become an additional AUTO trigger in `llm_router.py`, but the router remains deterministic.

---

# 10. DRIFT / MODEL TRUST LAYER

Create:

```text
lib/npu/drift.py
lib/npu/model_registry.py
```

The drift layer is an early-warning system, not a replacement for `strategy_lifecycle.py`.

Monitor:

- feature distribution drift
- embedding distribution drift
- prediction residual drift
- calibration drift
- MFE/MAE residual drift
- slippage residual drift
- strategy-conditioned prediction degradation
- regime-conditioned degradation

Output something like:

```json
{
  "concept_drift_probability": 0.68,
  "execution_drift_probability": 0.21,
  "prediction_degradation_probability": 0.59,
  "trust_score": 0.46
}
```

Use explicit thresholds and minimum sample requirements.

Possible responses:

- lower model influence
- mark model DEGRADED
- route to CPU baseline/fallback
- queue retraining
- temporarily reduce strategy/model contribution
- expose warning in UI/API

Do not automatically promote a retrained model.

Use champion/challenger evaluation.

A challenger must beat the champion chronologically OOS and pass calibration/cost-aware checks before promotion.

---

# 11. ADAPTIVE CONTROL / NEUROSTATE

Create only after predictive models are functional:

`lib/npu/neurostate.py`

Do **not** train separate neural networks merely to imitate neurotransmitter names.

Compute interpretable controller variables from measured system state.

Suggested semantics:

### Dopamine / prediction error

```text
realized outcome - predicted outcome
```

Use as a reward-prediction-error style signal for learning diagnostics and association strength.

### Norepinephrine

Derived from:

- surprise
- regime transition probability
- drift
- major catalyst
- abnormal volatility

Can increase processing priority and contribute to LLM AUTO escalation.

### Acetylcholine

Derived from:

- uncertainty
- novelty
- anomaly
- importance

Can increase data/model attention and evidence collection.

### GABA

Derived from:

- tail risk
- drawdown
- execution risk
- model degradation
- liquidity deterioration

Can make the system more conservative, e.g. increase minimum acceptable net R or reduce model influence.

### Glutamate

Derived from convergent independent evidence and favorable validated forecasts.

Never use it to bypass risk gates.

### Serotonin

Derived from longer-term calibration stability, strategy stability, model health, and execution stability.

Low stability should reduce trust, not cause emotional-style arbitrary behavior.

All neurostate values must be traceable back to their numerical causes.

---

# 12. FIX STATISTICAL AUTHORITY CONFLICTS

Audit existing learning/pattern-memory code.

Any mechanism that surfaces strong conclusions from tiny samples must be brought into line with the statistical discipline used by expectancy/calibration.

In particular, inspect pattern-memory logic that may treat approximately three observations as sufficient evidence.

Pattern memory should be descriptive/contextual only unless it satisfies explicit minimum-sample and uncertainty rules.

Do not allow an LLM prompt to say effectively:

> this exact setup won 3/3 times

while deterministic calibration correctly says there is insufficient evidence.

Statistical authority should flow through calibrated, sample-aware systems.

---

# 13. DATASET AND LABEL GENERATION

Create a reproducible dataset builder rather than training directly from production tables ad hoc.

Suggested structure:

```text
ml/
  datasets/
  training/
  evaluation/
  export/
  README.md
```

Possible scripts:

```text
ml/datasets/build_feature_dataset.py
ml/datasets/build_execution_dataset.py
ml/datasets/build_path_dataset.py
ml/training/train_state_encoder.py
ml/training/train_outcome_model.py
ml/training/train_path_model.py
ml/training/train_execution_model.py
ml/evaluation/walk_forward.py
ml/export/export_openvino.py
```

Dataset records must preserve:

- observation timestamp
- feature timestamp/freshness
- symbol
- asset class
- timeframe
- strategy
- feature schema version
- label horizon
- source type: live/replay/backtest/etc.

Never leak future information into features.

Do not random-split time series for final evaluation.

Use walk-forward or chronological train/validation/test splits.

Keep replay/simulated outcomes distinguishable from observed live outcomes.

Weight them appropriately rather than pretending they are equivalent.

---

# 14. MODEL REGISTRY

Create a lightweight model registry.

Each model version needs metadata including:

```json
{
  "name": "outcome_15m",
  "version": "2026-08-13.1",
  "schema_version": "3",
  "trained_through": "...",
  "training_samples": 0,
  "validation_samples": 0,
  "test_samples": 0,
  "metrics": {},
  "device": "NPU",
  "status": "CHALLENGER"
}
```

Statuses:

```text
CHAMPION
CHALLENGER
SHADOW
DEGRADED
DISABLED
```

Do not overwrite models in place.

Model versions must be reproducible and rollbackable.

---

# 15. SAFE INTEGRATION ORDER

Implement incrementally.

## Phase A — infrastructure

- NPU runtime
- device detection
- feature schema
- feature builder
- metrics/health
- CPU fallback
- tests

No trading behavior changes.

## Phase B — execution predictor

- build execution dataset
- train slippage/fill model
- run shadow predictions
- compare predictions to actual fills
- calibrate

Only after validation, allow predicted slippage to feed `transaction_costs.py`.

Existing deterministic fallback must remain.

## Phase C — MFE/MAE/path predictor

- derive leakage-safe labels
- shadow inference
- compare expected vs actual path
- measure calibration/errors

No automatic stop/target modification yet.

## Phase D — forward outcome model

- train probability distribution model
- calibrate probabilities
- run shadow
- test incremental net expectancy

Use as a filter/evidence source only after OOS validation.

## Phase E — state encoder + analog engine

- train representation
- build historical embedding index
- expose analog statistics
- test whether analog-conditioned expectancy adds value

## Phase F — drift/champion-challenger

- monitor residuals/distributions
- automatic retraining may create challengers
- promotion remains evidence-based

## Phase G — adaptive neurostate

Only now connect predictive uncertainty/drift/surprise into adaptive controller behavior.

---

# 16. INTEGRATION WITH EXISTING EXPECTANCY

Do not replace historical expectancy with neural expected return.

They answer different questions.

Historical expectancy:

> What has this class of setup actually earned?

NPU conditional forecast:

> Given the exact current state, does this instance look better or worse than the class baseline?

Design a conservative integration layer such as:

`lib/npu/meta_filter.py`

Initially it should return evidence rather than alter the trade verdict:

```json
{
  "historical_net_expected_r": 0.22,
  "conditional_forecast": {},
  "path_forecast": {},
  "execution_forecast": {},
  "analog_stats": {},
  "model_trust": 0.81,
  "recommendation": "SUPPORTS_BASELINE"
}
```

Possible recommendation labels:

```text
STRONGLY_SUPPORTS
SUPPORTS
NEUTRAL
CONFLICTS
STRONGLY_CONFLICTS
ABSTAIN
```

Only after shadow/OOS measurement should this affect score or gating.

When it eventually does, cap its influence.

The NPU must not turn a deterministic `NO_TRADE` caused by negative net expectancy or a hard risk violation into `TRADE`.

It may veto or reduce confidence before it is ever allowed to positively amplify risk.

---

# 17. API AND UI OBSERVABILITY

Expose NPU health and predictions through existing API patterns.

Suggested endpoints, adjusted to current project conventions:

```text
GET /api/npu/status
GET /api/npu/models
GET /api/npu/predictions/{symbol}
GET /api/npu/analogs/{symbol}
GET /api/npu/drift
```

UI should show useful information, not raw ML internals.

Useful cards/panels:

### NPU Health
- device
- loaded models
- inference p50/p95
- queue depth
- fallback count
- errors

### Conditional Forecast
- directional distribution
- expected MFE/MAE
- stop-first / TP-first probability
- uncertainty

### Execution Forecast
- expected slippage
- p90 slippage
- limit-fill probability
- adverse-selection risk

### Historical Analogs
- neighbor count
- similarity
- historical forward outcome
- median MFE/MAE/net R

### Model Trust
- champion version
- calibration
- drift status
- trust score

Do not display latent embedding dimensions.

---

# 18. TESTING REQUIREMENTS

Every NPU integration requires tests.

At minimum:

### Feature tests
- deterministic feature order
- schema hash/version
- no NaN/inf leakage
- stale-data handling
- missing-feature handling
- normalization reproducibility

### Runtime tests
- NPU available
- NPU unavailable
- CPU fallback
- corrupt model
- schema mismatch
- inference timeout
- queue overflow

### Leakage tests
Construct synthetic timestamps proving that future bars/outcomes cannot enter training features or analog retrieval.

### Model integration tests
- prediction abstention
- low-trust behavior
- stale prediction rejection
- deterministic fallback
- NPU cannot override hard risk gate
- NPU cannot author fees/P&L/leverage/liquidation

### Walk-forward evaluation
Compare:

```text
JARVIS baseline
vs
JARVIS + execution model
vs
JARVIS + execution + path model
vs
JARVIS + all validated NPU models
```

Measure net outcomes after costs.

---

# 19. PERFORMANCE REQUIREMENTS

This layer exists partly because the NPU can run small models continuously without consuming LLM GPU capacity.

Targets should be measured rather than assumed.

Aim for:

- single-model inference comfortably below the relevant market-data cadence
- bounded total inference latency
- no blocking of WebSocket consumers
- no synchronous NPU calls on critical execution/risk threads
- batch inference where it improves throughput without making data stale

Latest data wins.

If five stale inference requests are queued for the same symbol/model, drop/coalesce them and process the newest state.

---

# 20. WHAT NOT TO DO

Do not:

- build a second TA engine for the NPU
- let neural models calculate fees
- let neural models calculate P&L
- let neural models choose leverage directly
- let neural models calculate liquidation
- replace deterministic strategy classification
- replace strategy lifecycle
- replace regime axes with an opaque regime label
- replace calibration with model confidence
- treat model confidence as probability without calibration
- random-split time-series data for final evaluation
- train on post-entry information that was unavailable at decision time
- treat replay fills as identical to live fills
- use tiny sample pattern memory as statistical authority
- silently use stale predictions
- default failed predictions to bullish/favorable values
- allow an NPU outage to halt JARVIS
- consume RTX GPU capacity for models intended for the Intel NPU unless explicitly configured
- add complexity that cannot demonstrate OOS benefit

---

# 21. DEFINITION OF SUCCESS

The project is not successful because the NPU utilization graph moves.

It is successful only if at least one of the following improves out-of-sample and then survives live shadow validation:

- net expectancy after actual costs
- slippage prediction accuracy
- avoided bad fills
- stop/target path prediction
- drawdown
- calibration
- tail-loss frequency
- false-breakout filtering
- strategy degradation detection speed
- latency/resource isolation from the LLM GPU

Every claimed improvement must have a baseline comparison.

Prefer deleting an NPU model that adds no measurable value over keeping it for architectural novelty.

---

# 22. IMPLEMENTATION WORKFLOW FOR CLAUDE CODE

When executing this upgrade:

1. Inspect the entire current repository before coding.
2. Map existing modules and database models relevant to each phase.
3. Identify reusable data sources and interfaces.
4. Write a concise implementation plan referencing actual repository files.
5. Implement one phase at a time.
6. Add tests with each phase.
7. Run the relevant test suite before moving on.
8. Keep NPU features behind configuration flags until validated.
9. Default all predictive models to SHADOW mode initially.
10. Record predictions so they can later be joined to realized outcomes.
11. Do not claim an improvement without chronological OOS evidence.
12. Do not rewrite unrelated working systems.

When uncertain whether something belongs in ML or deterministic code, use this rule:

> If there is one mathematically correct answer from known inputs, keep it deterministic. If the task estimates an uncertain future quantity from historical patterns, it may belong in ML.

The goal is not to make JARVIS more neural.

The goal is to make JARVIS **more predictive, more measurable, more adaptive, and harder to fool by its own confidence** while preserving the deterministic controls that already make the system trustworthy.
