# JARVIS NPU Predictive Intelligence Upgrade

## Mission

Upgrade JARVIS with an Intel NPU-backed predictive intelligence layer that adds capabilities the repository does **not already have**.

Do not rebuild working deterministic systems as neural networks. The NPU is not a second trading engine and is not an alternative source of truth for fees, P&L, position sizing, leverage, liquidation, technical indicators, strategy identity, or lifecycle state.

The purpose of the NPU is to convert JARVIS's existing feature, market-data, execution, and outcome streams into continuously updated **probabilistic forecasts** that improve the existing deterministic decision architecture.

Core principle:

> Neural models predict uncertain future quantities. Deterministic code owns arithmetic, accounting, constraints, and final safety gates.

The target architecture is:

```text
                           CPU
                    FEATURE ENGINE
                          │
        ┌─────────────────┼──────────────────┐
        │                 │                  │
      TA/Structure      Kraken             Regime
      Strategies        Tape/L2             Axes
        │                 │                  │
        └─────────────────┼──────────────────┘
                          ▼
                    CANONICAL FEATURE
                         VECTOR
                          │
                          ▼
                    INTEL NPU
                          │
             ┌────────────┼────────────┐
             │            │            │
             ▼            ▼            ▼
         STATE        FORECAST       EXECUTION
        ENCODER       MODELS          MODEL
             │            │            │
             │       Return Dist.      │
             │       MFE / MAE         │
             │       Tail Risk         │
             │       Time-to-Move      │
             │                         │
             └────────────┼────────────┘
                          ▼
                     DRIFT MODEL
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
       HISTORICAL ANALOGS       ADAPTIVE CONTROL
                                 neurostate
                                      │
                                      ▼
                    EXISTING JARVIS SYSTEMS
                                      │
             ┌────────────────────────┼───────────┐
             ▼                        ▼           ▼
        expectancy.py          llm_router.py    risk
             │                        │            │
       transaction_costs             LLM      sizing/limits
             │                                     │
          lifecycle                                 │
             │                                      │
             └──────────────────┬───────────────────┘
                                ▼
                            execution
                                │
                                ▼
                             brokers
```

The important separation is:

```text
CPU = facts, arithmetic, risk, execution
NPU = uncertain prediction
RTX 5090 = large-model reasoning + model training
```

A second hardware/process view:

```text
                         ┌────────────────────┐
                         │    RTX 5090        │
                         │                    │
                         │ Qwen / LLM         │
                         │ Deep reasoning     │
                         │ Model training     │
                         │ Challenger builds  │
                         └─────────┬──────────┘
                                   │
                                   │ validated model exports
                                   ▼
┌────────────────────┐   ┌────────────────────┐   ┌────────────────────┐
│       CPU          │   │     INTEL NPU      │   │   MARKET/BROKERS   │
│                    │   │                    │   │                    │
│ feeds              │──▶│ state encoder      │   │ Kraken             │
│ TA/structure       │   │ outcome model      │   │ Alpaca             │
│ expectancy         │◀──│ path model         │   │ future adapters    │
│ costs              │   │ execution model    │   │                    │
│ risk               │   │ drift inference    │   └─────────▲──────────┘
│ sizing             │   │                    │             │
│ lifecycle          │   └────────────────────┘             │
│ execution          │──────────────────────────────────────▶│
└────────────────────┘
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

---

# 23. DETAILED WORKING TREES

These diagrams are implementation requirements. Preserve the separation of responsibilities shown here unless repository inspection proves a concrete reason to change it.

## 23.1 Live feature-to-trade path

```text
                    LIVE SOURCES
                         │
       ┌─────────────────┼──────────────────┐
       │                 │                  │
       ▼                 ▼                  ▼
  OHLCV / TA         KRAKEN WS          OTHER DATA
                     tape/book          derivatives
       │                 │                  │
       └──────────────┬──┴──────────────────┘
                      ▼
             EXISTING FEATURE SYSTEMS
                      │
          ┌───────────┼───────────────┐
          ▼           ▼               ▼
      TA Engine    Structure      Regime Axes
          │           │               │
          ├───────────┼───────────────┤
          ▼           ▼               ▼
      Strategies  Relative Str.   Cost Inputs
          │           │               │
          └───────────┼───────────────┘
                      ▼
             NPU FEATURE BUILDER
                      │
              schema + freshness
                      │
                      ▼
               NPU MODEL BUNDLE
                      │
      ┌───────────────┼───────────────────┐
      ▼               ▼                   ▼
  State Encoder   Outcome/Path        Execution
                      │                   │
      └───────────────┼───────────────────┘
                      ▼
                Model Trust
                      │
              Historical Analogs
                      │
                      ▼
                 META FILTER
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
   expectancy.py  llm_router.py   risk
          │           │            │
          └───────────┼────────────┘
                      ▼
                  EXECUTION
```

## 23.2 Training and promotion path

```text
           OBSERVED LIVE DATA + LABELS
                       │
                       ▼
                DATASET BUILDER
                       │
          chronological / leakage-safe
                       │
                       ▼
                   RTX 5090
                       │
             train candidate model
                       │
                       ▼
               WALK-FORWARD OOS
                       │
                 pass / fail
                 │         │
               fail       pass
                 │         │
                 ▼         ▼
             discard    CHALLENGER
                            │
                            ▼
                      SHADOW LIVE
                            │
                   compare residuals,
                   calibration, net EV
                            │
                     pass / fail
                     │         │
                   fail       pass
                     │         │
                     ▼         ▼
                  retain    PROMOTION
                   old        │
                 champion     ▼
                         OpenVINO IR
                              │
                              ▼
                           INTEL NPU
```

## 23.3 Execution-model path

```text
        CURRENT ORDER INTENT
               │
               ├── symbol
               ├── venue
               ├── order type
               ├── notional
               ├── stop distance
               └── urgency
               │
               ▼
        CURRENT MICROSTRUCTURE
               │
               ├── spread
               ├── depth
               ├── imbalance
               ├── tape flow
               ├── volatility
               └── recent fill quality
               │
               ▼
          NPU EXECUTION MODEL
               │
       ┌───────┼─────────────┐
       ▼       ▼             ▼
 slippage   fill prob.   adverse selection
       │       │             │
       └───────┼─────────────┘
               ▼
       transaction_costs.py
               │
          total cost in R
               │
               ▼
          expectancy.py
               │
          TRADE / NO_TRADE
```

## 23.4 Path-model path

```text
       VALID STRATEGY CANDIDATE
                 │
                 ▼
           NPU PATH MODEL
                 │
     ┌───────────┼────────────┐
     ▼           ▼            ▼
  MFE dist.   MAE dist.   first-touch
                              │
                        stop vs TP1/TP2
                 │
                 ▼
        PATH QUALITY EVIDENCE
                 │
       ┌─────────┼───────────┐
       ▼         ▼           ▼
   reject?    hold-time   target realism
       │         │           │
       └─────────┼───────────┘
                 ▼
        EXISTING RISK/EXIT LOGIC
```

## 23.5 Drift-control path

```text
      LIVE PREDICTIONS + REALIZED OUTCOMES
                       │
                       ▼
                 RESIDUAL STORE
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
 feature drift   calibration drift  execution drift
       │               │                │
       └───────────────┼────────────────┘
                       ▼
                  TRUST SCORE
                       │
         ┌─────────────┼──────────────┐
         ▼             ▼              ▼
       normal       degraded       severe
         │             │              │
         ▼             ▼              ▼
   normal weight   reduce weight   abstain/fallback
                       │              │
                       └──────┬───────┘
                              ▼
                      RETRAIN CHALLENGER
```

## 23.6 Neurostate/control path

```text
 predictive residuals      model drift       tail/execution risk
         │                     │                    │
         ▼                     ▼                    ▼
      dopamine          norepinephrine            GABA
         │                     │                    │
         ├─────────────────────┼────────────────────┤
         │                     │                    │
 uncertainty/novelty       stability         evidence convergence
         │                     │                    │
         ▼                     ▼                    ▼
 acetylcholine           serotonin          glutamate
         │                     │                    │
         └─────────────────────┼────────────────────┘
                               ▼
                     BOUNDED CONTROL STATE
                               │
            ┌──────────────────┼───────────────────┐
            ▼                  ▼                   ▼
       processing         LLM AUTO          stricter filters
        priority          escalation          when needed
```

The neurostate is a controller. It is not an order generator.

---

# 24. RECOMMENDED REPOSITORY TREE AFTER IMPLEMENTATION

Use the existing project conventions where they differ, but keep responsibility boundaries equivalent to this:

```text
jarvis-trading/
│
├── lib/
│   ├── expectancy.py                  # EXISTING authority
│   ├── calibration.py                 # EXISTING authority
│   ├── strategy_lifecycle.py          # EXISTING authority
│   ├── transaction_costs.py           # EXISTING authority
│   ├── strategies.py                  # EXISTING authority
│   ├── regime_axes.py                 # EXISTING authority
│   ├── llm_router.py                  # EXISTING authority
│   │
│   └── npu/
│       ├── __init__.py
│       ├── config.py
│       ├── runtime.py
│       ├── queue.py
│       ├── schemas.py
│       ├── features.py
│       ├── normalization.py
│       ├── prediction_store.py
│       ├── model_registry.py
│       ├── health.py
│       │
│       ├── state_encoder.py
│       ├── outcome_model.py
│       ├── path_model.py
│       ├── execution_model.py
│       ├── timeframe_model.py
│       ├── analogs.py
│       ├── drift.py
│       ├── neurostate.py
│       └── meta_filter.py
│
├── ml/
│   ├── README.md
│   ├── datasets/
│   │   ├── build_feature_dataset.py
│   │   ├── build_path_dataset.py
│   │   └── build_execution_dataset.py
│   │
│   ├── training/
│   │   ├── train_encoder.py
│   │   ├── train_outcome.py
│   │   ├── train_path.py
│   │   └── train_execution.py
│   │
│   ├── evaluation/
│   │   ├── walk_forward.py
│   │   ├── calibration.py
│   │   ├── ablation.py
│   │   └── champion_challenger.py
│   │
│   └── export/
│       ├── export_openvino.py
│       └── quantize.py
│
├── models/
│   └── npu/
│       ├── registry.json
│       ├── state_encoder/
│       ├── outcome_5m/
│       ├── outcome_15m/
│       ├── outcome_1h/
│       ├── path/
│       └── execution/
│
├── data/
│   └── npu/
│       ├── embeddings/
│       ├── predictions/
│       └── residuals/
│
└── tests/
    ├── test_npu_runtime.py
    ├── test_npu_features.py
    ├── test_npu_leakage.py
    ├── test_npu_models.py
    ├── test_npu_analogs.py
    ├── test_npu_drift.py
    └── test_npu_integration.py
```

Do not force this exact physical layout if JARVIS's current layout has a cleaner existing convention. The ownership boundaries matter more than the directory names.

---

# 25. PERTINENT CODE EXAMPLES

These examples define intended interfaces and safety behavior. Adapt imports, database classes, and repository-specific details after inspecting the current code.

## 25.1 OpenVINO NPU runtime with explicit CPU fallback

Current OpenVINO supports device discovery through `ov.Core().available_devices` and direct NPU compilation through `core.compile_model(model, "NPU")`.

```python
# lib/npu/runtime.py
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
    value: object | None
    device: str
    model_name: str
    latency_ms: float
    fallback: bool
    ok: bool
    error: str | None = None


class NPURuntime:
    def __init__(self, cache_dir: str = "cache/openvino"):
        self.core = ov.Core()
        self.devices = tuple(self.core.available_devices)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # OpenVINO model caching is supported by device plugins.
        # Keep this best-effort: a cache problem must never disable inference.
        try:
            self.core.set_property({"CACHE_DIR": str(self.cache_dir)})
        except Exception as exc:
            logger.warning("OpenVINO cache configuration failed: %s", exc)

        self._compiled: dict[tuple[str, str], object] = {}

    @property
    def npu_available(self) -> bool:
        return any(str(d).upper().startswith("NPU") for d in self.devices)

    def _compile(self, model_name: str, model_path: str, device: str):
        key = (model_name, device)
        if key in self._compiled:
            return self._compiled[key]

        compiled = self.core.compile_model(model_path, device)
        self._compiled[key] = compiled
        return compiled

    def infer(
        self,
        *,
        model_name: str,
        model_path: str,
        tensor: np.ndarray,
        allow_cpu_fallback: bool = True,
    ) -> InferenceResult:
        preferred = "NPU" if self.npu_available else "CPU"
        started = time.perf_counter()

        try:
            compiled = self._compile(model_name, model_path, preferred)
            result = compiled([tensor])
            output = next(iter(result.values()))
            return InferenceResult(
                value=np.asarray(output),
                device=preferred,
                model_name=model_name,
                latency_ms=(time.perf_counter() - started) * 1000,
                fallback=(preferred != "NPU"),
                ok=True,
            )
        except Exception as npu_exc:
            if preferred == "CPU" or not allow_cpu_fallback:
                return InferenceResult(
                    value=None,
                    device=preferred,
                    model_name=model_name,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    fallback=(preferred != "NPU"),
                    ok=False,
                    error=str(npu_exc),
                )

            logger.warning(
                "NPU inference failed for %s; trying CPU: %s",
                model_name,
                npu_exc,
            )

            try:
                compiled = self._compile(model_name, model_path, "CPU")
                result = compiled([tensor])
                output = next(iter(result.values()))
                return InferenceResult(
                    value=np.asarray(output),
                    device="CPU",
                    model_name=model_name,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    fallback=True,
                    ok=True,
                )
            except Exception as cpu_exc:
                return InferenceResult(
                    value=None,
                    device="CPU",
                    model_name=model_name,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    fallback=True,
                    ok=False,
                    error=f"NPU={npu_exc}; CPU={cpu_exc}",
                )
```

Important:

- Failed inference returns `None`, not a favorable zero.
- Risk/execution threads must not synchronously depend on NPU availability.
- Model output must be validated before use.

## 25.2 Feature schema with deterministic ordering

```python
# lib/npu/schemas.py
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
    "structure_support_atr",
    "structure_resistance_atr",
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


def schema_hash(names=FEATURES_V1) -> str:
    blob = json.dumps(list(names), separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass(frozen=True)
class FeatureSchema:
    version: str
    names: tuple[str, ...]
    hash: str


SCHEMA_V1 = FeatureSchema(
    version="1",
    names=FEATURES_V1,
    hash=schema_hash(FEATURES_V1),
)
```

Never depend on arbitrary dict iteration order.

## 25.3 Feature vector carrying freshness and missingness

A missing feature and a neutral feature are different states.

```python
# lib/npu/features.py
from dataclasses import dataclass
from datetime import datetime, timezone
import numpy as np

from .schemas import SCHEMA_V1


@dataclass(frozen=True)
class FeatureVector:
    symbol: str
    observed_at: datetime
    values: np.ndarray
    missing_mask: np.ndarray
    schema_version: str
    schema_hash: str
    max_source_age_s: float


def build_feature_vector(symbol: str, raw: dict) -> FeatureVector:
    vals = []
    missing = []
    source_ages = []

    now = datetime.now(timezone.utc)

    for name in SCHEMA_V1.names:
        item = raw.get(name)

        if isinstance(item, dict):
            value = item.get("value")
            ts = item.get("timestamp")
        else:
            value = item
            ts = None

        if value is None:
            vals.append(0.0)       # numeric placeholder ONLY because mask exists
            missing.append(1.0)
        else:
            vals.append(float(value))
            missing.append(0.0)

        if ts is not None:
            age = max(0.0, (now - ts).total_seconds())
            source_ages.append(age)

    return FeatureVector(
        symbol=symbol,
        observed_at=now,
        values=np.asarray(vals, dtype=np.float32),
        missing_mask=np.asarray(missing, dtype=np.float32),
        schema_version=SCHEMA_V1.version,
        schema_hash=SCHEMA_V1.hash,
        max_source_age_s=max(source_ages, default=0.0),
    )
```

Consider including missingness masks as model inputs if validation shows they help.

## 25.4 Latest-state-wins async coalescing

Do not queue every market update.

```python
# lib/npu/queue.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class PendingInference:
    model: str
    symbol: str
    payload: object


class LatestOnlyInferenceQueue:
    def __init__(self):
        self._latest: dict[tuple[str, str], PendingInference] = {}
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def submit(self, item: PendingInference):
        async with self._lock:
            self._latest[(item.model, item.symbol)] = item
            self._event.set()

    async def pop_batch(self, max_items: int = 64) -> list[PendingInference]:
        while True:
            await self._event.wait()

            async with self._lock:
                if not self._latest:
                    self._event.clear()
                    continue

                keys = list(self._latest)[:max_items]
                items = [self._latest.pop(k) for k in keys]

                if not self._latest:
                    self._event.clear()

                return items
```

This prevents a fast WebSocket from turning NPU inference into a stale backlog.

## 25.5 Model registry

```python
# lib/npu/model_registry.py
from dataclasses import dataclass
from pathlib import Path
import json


VALID_STATES = {
    "CHAMPION",
    "CHALLENGER",
    "SHADOW",
    "DEGRADED",
    "DISABLED",
}


@dataclass(frozen=True)
class ModelSpec:
    name: str
    version: str
    path: str
    schema_version: str
    schema_hash: str
    status: str
    trained_through: str
    metrics: dict


class ModelRegistry:
    def __init__(self, path="models/npu/registry.json"):
        self.path = Path(path)

    def load(self) -> list[ModelSpec]:
        if not self.path.exists():
            return []

        data = json.loads(self.path.read_text(encoding="utf-8"))
        out = []

        for row in data.get("models", []):
            status = row["status"]
            if status not in VALID_STATES:
                raise ValueError(f"Invalid model status: {status}")
            out.append(ModelSpec(**row))

        return out

    def champion(self, name: str) -> ModelSpec | None:
        for spec in self.load():
            if spec.name == name and spec.status == "CHAMPION":
                return spec
        return None
```

## 25.6 Compact multi-task market-state encoder

Train on the RTX 5090; export the encoder/heads for NPU inference.

```python
# ml/training/model_defs.py
import torch
from torch import nn


class MarketStateMultiTaskNet(nn.Module):
    def __init__(self, input_dim: int, latent_dim: int = 24):
        super().__init__()

        self.encoder = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.GELU(),
            nn.LayerNorm(128),
            nn.Linear(128, 64),
            nn.GELU(),
            nn.Linear(64, latent_dim),
        )

        # Five forward-return classes.
        self.return_head = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.GELU(),
            nn.Linear(32, 5),
        )

        # MFE and MAE in R.
        self.path_head = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.GELU(),
            nn.Linear(32, 2),
        )

        # Example: expected slippage bps + adverse-selection logit.
        self.execution_head = nn.Sequential(
            nn.Linear(latent_dim, 32),
            nn.GELU(),
            nn.Linear(32, 2),
        )

    def forward(self, x):
        z = self.encoder(x)
        return {
            "embedding": z,
            "return_logits": self.return_head(z),
            "path": self.path_head(z),
            "execution": self.execution_head(z),
        }
```

Do not assume shared representation is best. Benchmark separate models against multi-task sharing.

## 25.7 Export PyTorch model to OpenVINO

Current OpenVINO supports converting PyTorch `torch.nn.Module` objects with `ov.convert_model`, and using an example input is appropriate for traced conversion.

```python
# ml/export/export_openvino.py
from pathlib import Path

import openvino as ov
import torch


def export_model(model, input_dim: int, output_path: str):
    model = model.eval().cpu()
    example = torch.zeros((1, input_dim), dtype=torch.float32)

    ov_model = ov.convert_model(
        model,
        example_input=example,
    )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    ov.save_model(ov_model, str(output))
```

Always run parity checks between the PyTorch and OpenVINO outputs before registration.

## 25.8 Export parity test

```python
import numpy as np
import openvino as ov
import torch


def assert_export_parity(torch_model, xml_path: str, sample: np.ndarray):
    torch_model.eval()

    with torch.no_grad():
        torch_out = torch_model(torch.from_numpy(sample))

    core = ov.Core()
    compiled = core.compile_model(xml_path, "CPU")
    ov_out = compiled([sample])

    # Map actual output names deliberately in production.
    # Do not assume dictionary order across differently exported graphs.
    first_ov = np.asarray(next(iter(ov_out.values())))

    first_torch = next(iter(torch_out.values())).detach().cpu().numpy()

    np.testing.assert_allclose(
        first_ov,
        first_torch,
        rtol=1e-3,
        atol=1e-4,
    )
```

Production export validation must map named outputs explicitly.

## 25.9 INT8/PTQ flow

Quantization must be validated, not assumed to improve the system.

Illustrative OpenVINO/NNCF flow:

```python
import nncf
import openvino as ov


core = ov.Core()
model = core.read_model("models/npu/outcome_fp.xml")

# calibration_dataset should yield representative model inputs.
dataset = nncf.Dataset(
    calibration_rows,
    transform_func=lambda row: row.astype("float32"),
)

quantized_model = nncf.quantize(
    model,
    dataset,
)

ov.save_model(
    quantized_model,
    "models/npu/outcome_int8.xml",
)
```

Then compare FP and quantized models on the same chronological OOS dataset.

Reject quantization if it materially worsens:

- probability calibration
- tail recall
- MFE/MAE error
- slippage prediction
- incremental net expectancy

## 25.10 Outcome-probability postprocessing

```python
import numpy as np


RETURN_CLASSES = (
    "large_down",
    "small_down",
    "flat",
    "small_up",
    "large_up",
)


def softmax(x: np.ndarray) -> np.ndarray:
    x = x - np.max(x)
    exp = np.exp(x)
    return exp / exp.sum()


def decode_return_logits(logits: np.ndarray) -> dict:
    p = softmax(np.asarray(logits).reshape(-1))

    if len(p) != len(RETURN_CLASSES):
        raise ValueError("Unexpected return-model output width")

    out = dict(zip(RETURN_CLASSES, map(float, p)))
    out["p_up"] = out["small_up"] + out["large_up"]
    out["p_down"] = out["small_down"] + out["large_down"]
    return out
```

Model probabilities are not trustworthy until calibration is measured.

## 25.11 MFE/MAE label generation without lookahead leakage

Future data is used only for labels, never features.

```python
def path_labels(
    *,
    entry: float,
    stop: float,
    direction: str,
    future_bars,
):
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    short = direction.lower().startswith("short")

    max_favorable = 0.0
    max_adverse = 0.0

    for _, bar in future_bars.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])

        if short:
            favorable = entry - low
            adverse = high - entry
        else:
            favorable = high - entry
            adverse = entry - low

        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)

    return {
        "mfe_r": max_favorable / risk,
        "mae_r": max_adverse / risk,
    }
```

The dataset builder must select `future_bars` strictly after the observation timestamp.

## 25.12 Stop-first / target-first labels

Bar data cannot reveal touch ordering if stop and target occur inside the same bar.

Use conservative labeling.

```python
AMBIGUOUS = "AMBIGUOUS"


def first_touch(entry, stop, target, direction, future_bars):
    short = direction.lower().startswith("short")

    for _, bar in future_bars.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])

        if short:
            touched_stop = high >= stop
            touched_target = low <= target
        else:
            touched_stop = low <= stop
            touched_target = high >= target

        if touched_stop and touched_target:
            return AMBIGUOUS

        if touched_stop:
            return "STOP_FIRST"

        if touched_target:
            return "TARGET_FIRST"

    return "NEITHER"
```

Do not resolve ambiguous bars in the favorable direction.

## 25.13 Execution training record

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ExecutionTrainingRow:
    submitted_at: datetime
    symbol: str
    venue: str
    side: str
    order_type: str

    reference_mid: float
    submitted_price: float | None
    notional_usd: float

    spread_bps: float | None
    bid_depth: float | None
    ask_depth: float | None
    imbalance: float | None
    tape_flow: float | None
    realized_volatility: float | None

    filled_at: datetime | None
    avg_fill_price: float | None

    realized_slippage_bps: float | None
    fill_delay_ms: float | None
    filled: bool
```

Store the market snapshot that existed **at submission time**, not after the fill.

## 25.14 Slippage calculation

```python
def realized_slippage_bps(
    *,
    side: str,
    reference_mid: float,
    avg_fill_price: float,
) -> float:
    if reference_mid <= 0:
        raise ValueError("reference_mid must be positive")

    if side.lower() == "buy":
        adverse_move = avg_fill_price - reference_mid
    else:
        adverse_move = reference_mid - avg_fill_price

    return adverse_move / reference_mid * 10_000.0
```

Positive means worse than reference.

## 25.15 Historical analog retrieval

The NPU produces embeddings. Similarity retrieval itself can remain on CPU.

```python
# lib/npu/analogs.py
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class Analog:
    index: int
    similarity: float


def cosine_neighbors(
    current: np.ndarray,
    history: np.ndarray,
    *,
    top_k: int = 100,
    min_similarity: float = 0.80,
) -> list[Analog]:
    current = np.asarray(current, dtype=np.float32).reshape(-1)
    history = np.asarray(history, dtype=np.float32)

    c_norm = np.linalg.norm(current)
    h_norm = np.linalg.norm(history, axis=1)

    denom = h_norm * c_norm
    valid = denom > 0

    sims = np.full(len(history), -1.0, dtype=np.float32)
    sims[valid] = history[valid] @ current / denom[valid]

    idx = np.argsort(sims)[::-1][:top_k]

    return [
        Analog(int(i), float(sims[i]))
        for i in idx
        if sims[i] >= min_similarity
    ]
```

For very large histories, move to an ANN index after measuring the need. Do not introduce another database/vector dependency prematurely.

## 25.16 Time-safe analog retrieval

```python
def historical_candidates(rows, *, observation_time):
    return [
        row
        for row in rows
        if row["timestamp"] < observation_time
    ]
```

Backtests must enforce this before similarity search.

## 25.17 Population Stability Index example

```python
import numpy as np


def population_stability_index(
    expected: np.ndarray,
    actual: np.ndarray,
    bins: int = 10,
    eps: float = 1e-6,
) -> float:
    expected = np.asarray(expected)
    actual = np.asarray(actual)

    edges = np.quantile(
        expected,
        np.linspace(0.0, 1.0, bins + 1),
    )
    edges[0] = -np.inf
    edges[-1] = np.inf

    exp_hist, _ = np.histogram(expected, bins=edges)
    act_hist, _ = np.histogram(actual, bins=edges)

    exp_pct = np.maximum(exp_hist / max(1, exp_hist.sum()), eps)
    act_pct = np.maximum(act_hist / max(1, act_hist.sum()), eps)

    return float(np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct)))
```

Do not hard-code industry PSI thresholds as truth. Calibrate alert thresholds against JARVIS's own false alarms and performance deterioration.

## 25.18 Model trust

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TrustInputs:
    calibration_score: float
    residual_score: float
    feature_stability: float
    freshness_score: float
    sample_score: float


def model_trust(x: TrustInputs) -> float:
    # Example bounded transparent blend.
    # Replace weights only after empirical validation.
    raw = (
        0.30 * x.calibration_score
        + 0.25 * x.residual_score
        + 0.20 * x.feature_stability
        + 0.15 * x.freshness_score
        + 0.10 * x.sample_score
    )
    return max(0.0, min(1.0, raw))
```

Do not train another opaque network merely to compute trust unless it proves superior.

## 25.19 Adaptive neurostate

```python
# lib/npu/neurostate.py
from dataclasses import dataclass


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


@dataclass
class NeuroState:
    dopamine: float = 0.50
    norepinephrine: float = 0.50
    acetylcholine: float = 0.50
    gaba: float = 0.50
    glutamate: float = 0.50
    serotonin: float = 0.50


def toward_baseline(value: float, baseline: float, rate: float) -> float:
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
) -> NeuroState:
    # Homeostasis first.
    for field in state.__dataclass_fields__:
        setattr(
            state,
            field,
            toward_baseline(getattr(state, field), 0.50, 0.03),
        )

    # Bounded interpretable updates.
    state.dopamine = clamp01(
        state.dopamine + 0.08 * reward_prediction_error_r
    )

    state.norepinephrine = clamp01(
        state.norepinephrine + 0.12 * surprise
    )

    state.acetylcholine = clamp01(
        state.acetylcholine + 0.10 * uncertainty
    )

    state.gaba = clamp01(
        state.gaba
        + 0.08 * tail_risk
        + 0.06 * execution_risk
    )

    state.glutamate = clamp01(
        state.glutamate + 0.08 * evidence_convergence
    )

    state.serotonin = clamp01(
        state.serotonin + 0.06 * (model_stability - 0.5)
    )

    return state
```

These coefficients are placeholders until JARVIS validates them. They must not silently become permanent tuning constants.

## 25.20 Neurostate to control signals

```python
@dataclass(frozen=True)
class AdaptiveControls:
    processing_priority: float
    min_ev_multiplier: float
    model_weight_multiplier: float
    deep_reasoning_pressure: float


def controls_from_state(s: NeuroState) -> AdaptiveControls:
    processing = clamp01(
        0.45 * s.acetylcholine
        + 0.35 * s.norepinephrine
        + 0.20 * s.glutamate
    )

    # Can only make EV requirements stricter.
    min_ev_multiplier = 1.0 + 0.5 * s.gaba

    model_weight_multiplier = clamp01(
        0.5 * s.serotonin
        + 0.5 * (1.0 - s.norepinephrine)
    )

    deep_reasoning_pressure = clamp01(
        0.5 * s.norepinephrine
        + 0.3 * s.acetylcholine
        + 0.2 * s.gaba
    )

    return AdaptiveControls(
        processing_priority=processing,
        min_ev_multiplier=min_ev_multiplier,
        model_weight_multiplier=model_weight_multiplier,
        deep_reasoning_pressure=deep_reasoning_pressure,
    )
```

`min_ev_multiplier` must never lower the baseline requirement below 1.0.

## 25.21 Integration with existing transaction cost model

```python
def costs_with_npu_slippage(signal: dict, execution_prediction: dict | None):
    from lib.transaction_costs import estimate_costs

    predicted_slippage_pct = None

    if execution_prediction:
        trusted = execution_prediction.get("trust", 0.0) >= 0.70
        fresh = execution_prediction.get("age_ms", 10_000) <= 2_000

        if trusted and fresh:
            bps = execution_prediction.get("expected_slippage_bps")
            if bps is not None:
                predicted_slippage_pct = float(bps) / 10_000.0

    return estimate_costs(
        signal["asset_symbol"],
        signal["entry_price"],
        signal["stop_loss"],
        quoted_spread_pct=signal.get("spread_pct"),
        slippage_pct=predicted_slippage_pct,
        venue=signal.get("venue"),
        leveraged=bool(signal.get("leverage")),
        is_short=str(signal.get("direction", "")).lower().startswith("short"),
        hold_hours=signal.get("expected_hold_hours", 0.0),
    )
```

If the NPU prediction is untrusted/stale, `slippage_pct=None` deliberately falls back to JARVIS's existing conservative behavior.

## 25.22 Meta-filter integration

```python
# lib/npu/meta_filter.py
from dataclasses import dataclass


@dataclass(frozen=True)
class MetaEvidence:
    recommendation: str
    reasons: tuple[str, ...]
    max_positive_influence: float
    veto: bool


def evaluate_meta_evidence(
    *,
    base_expectancy: dict,
    outcome_prediction: dict | None,
    path_prediction: dict | None,
    execution_prediction: dict | None,
    analogs: dict | None,
    trust: float,
) -> MetaEvidence:
    if trust < 0.50:
        return MetaEvidence(
            recommendation="ABSTAIN",
            reasons=("NPU trust below minimum",),
            max_positive_influence=0.0,
            veto=False,
        )

    reasons = []
    negative = 0
    positive = 0

    if outcome_prediction:
        if outcome_prediction.get("p_down", 0) > 0.65:
            negative += 1
            reasons.append("conditional outcome distribution opposes trade")
        elif outcome_prediction.get("p_up", 0) > 0.65:
            positive += 1
            reasons.append("conditional outcome distribution supports trade")

    if path_prediction:
        if path_prediction.get("p_stop_first", 0) > 0.55:
            negative += 1
            reasons.append("path model sees stop-first risk")
        if path_prediction.get("expected_mfe_r", 0) >= 1.0:
            positive += 1
            reasons.append("path model sees usable MFE")

    if execution_prediction:
        if execution_prediction.get("p90_slippage_bps", 0) > 20:
            negative += 1
            reasons.append("execution tail cost is high")

    if negative >= 2:
        return MetaEvidence(
            recommendation="STRONGLY_CONFLICTS",
            reasons=tuple(reasons),
            max_positive_influence=0.0,
            veto=True,
        )

    if negative == 1:
        label = "CONFLICTS"
    elif positive >= 2:
        label = "STRONGLY_SUPPORTS"
    elif positive == 1:
        label = "SUPPORTS"
    else:
        label = "NEUTRAL"

    return MetaEvidence(
        recommendation=label,
        reasons=tuple(reasons),
        # Positive amplification must remain small until proven OOS.
        max_positive_influence=0.10 if positive else 0.0,
        veto=False,
    )
```

The thresholds above are examples, not production constants. Claude must fit/validate them or leave them configurable/shadowed.

## 25.23 NPU can veto but cannot resurrect hard NO_TRADE

```python
def combine_with_base_verdict(base: dict, meta: MetaEvidence) -> str:
    verdict = base.get("verdict")

    # Absolute rule.
    if verdict == "NO_TRADE":
        return "NO_TRADE"

    if meta.veto:
        return "NO_TRADE"

    return verdict or "UNKNOWN"
```

A neural layer may make JARVIS more conservative before it is ever allowed to make JARVIS more aggressive.

## 25.24 LLM AUTO escalation using NPU evidence

Do not replace `lib/llm_router.py`. Add transparent context fields.

```python
llm_context = {
    "leverage": signal.get("leverage"),
    "timeframe": signal.get("timeframe"),
    "contradiction_count": signal.get("contradiction_count"),

    # New learned evidence:
    "npu_conflict_severity": npu.get("timeframe", {}).get("conflict_severity"),
    "npu_uncertainty": npu.get("outcome", {}).get("uncertainty"),
    "npu_drift": npu.get("drift", {}).get("concept_drift_probability"),
    "npu_tail_risk": npu.get("path", {}).get("tail_risk"),
}
```

Then add deterministic thresholds inside `llm_router.py`, for example:

```python
if _num(ctx, "npu_conflict_severity") >= 0.75:
    fired.append("NPU sees historically meaningful timeframe conflict")

if _num(ctx, "npu_uncertainty") >= 0.80:
    fired.append("NPU conditional forecast is highly uncertain")

if _num(ctx, "npu_drift") >= 0.70:
    fired.append("NPU model/market drift elevated")
```

The NPU provides evidence. The router still owns the decision.

## 25.25 Prediction persistence

Every prediction should be joinable to its eventual result.

```python
@dataclass
class StoredPrediction:
    id: str
    signal_id: str | None
    symbol: str
    model_name: str
    model_version: str
    schema_version: str
    generated_at: str
    feature_timestamp: str
    device: str
    output_json: str
    trust: float | None
```

Persist enough information to answer:

> Did this model improve outcomes when it disagreed with baseline JARVIS?

## 25.26 Champion/challenger comparison

```python
def challenger_wins(champion: dict, challenger: dict) -> tuple[bool, list[str]]:
    reasons = []

    # Examples only; thresholds must be configured and validated.
    if challenger["brier"] >= champion["brier"]:
        reasons.append("Brier score did not improve")

    if challenger["calibration_error"] > champion["calibration_error"]:
        reasons.append("calibration worsened")

    if challenger["net_expected_r_filter"] <= champion["net_expected_r_filter"]:
        reasons.append("net expectancy did not improve")

    if challenger["max_drawdown"] > champion["max_drawdown"] * 1.10:
        reasons.append("drawdown materially worsened")

    return (len(reasons) == 0, reasons)
```

Do not promote based on accuracy alone.

## 25.27 Health/status payload

```python
def npu_status(runtime, registry, metrics) -> dict:
    return {
        "available_devices": list(runtime.devices),
        "npu_available": runtime.npu_available,
        "models": [
            {
                "name": m.name,
                "version": m.version,
                "status": m.status,
            }
            for m in registry.load()
        ],
        "latency_ms": metrics.latency_summary(),
        "fallback_count": metrics.fallback_count,
        "errors": metrics.recent_errors(limit=10),
    }
```

---

# 26. DATABASE / PERSISTENCE ADDITIONS

Before creating tables, inspect current ORM/database conventions and reuse them.

Conceptually add storage for:

```text
npu_predictions
npu_model_versions
npu_model_metrics
npu_execution_samples
npu_embeddings
npu_residuals
```

Suggested prediction fields:

```text
id
signal_id
symbol
asset_class
timeframe
model_name
model_version
feature_schema_version
feature_timestamp
predicted_at
device
latency_ms
trust
output_json
```

Suggested residual fields:

```text
prediction_id
resolved_at
actual_return
actual_mfe_r
actual_mae_r
actual_slippage_bps
actual_fill_delay_ms
residual_json
```

Do not duplicate data already represented cleanly in existing tables.

---

# 27. MODEL-SPECIFIC TRAINING OBJECTIVES

## Outcome model

Use classification loss for calibrated return buckets.

Candidate:

```text
cross-entropy
+
calibration evaluation
```

Do not optimize directly on raw win rate.

## Path model

MFE/MAE distributions are often skewed.

Benchmark:

```text
Huber loss
quantile loss
log-transformed targets where appropriate
```

Quantile outputs may be more useful than a single mean:

```text
MFE p25 / p50 / p75
MAE p25 / p50 / p75
```

## Execution model

Slippage is heavy-tailed.

Prefer predicting:

```text
median
p75
p90
```

rather than only mean slippage.

For fill probability:

```text
binary classification
+
calibration
```

For fill delay:

consider survival/time-to-event methods if the simple regression target performs poorly.

---

# 28. BETTER PATH OUTPUT SHAPE

Prefer this once data supports it:

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
  "p_tp1_first": 0.63,
  "p_tp2_first": 0.39,
  "time_to_mfe_min": {
    "p50": 18,
    "p75": 31
  }
}
```

This gives the deterministic exit/risk system usable distributions rather than fake precision.

---

# 29. NPU MODEL FREQUENCY

Do not run every model on every tick.

Recommended event/cadence structure:

```text
L2/tape update
    │
    ├── update CPU microstructure features
    │
    └── execution model only when:
          - candidate trade exists
          - active order exists
          - meaningful book change occurs

1s–5s cadence
    │
    ├── state encoder for watched active candidates
    └── short-horizon outcome model

new strategy candidate
    │
    ├── state encoder
    ├── outcome model
    ├── path model
    ├── analog lookup
    └── meta filter

model-resolution event
    │
    └── update residual/drift statistics

minutes/hours
    │
    └── drift scans / model health

trade close
    │
    ├── reward prediction error
    ├── residual storage
    └── neurostate update
```

This is more useful than trying to keep NPU utilization artificially near 100%.

---

# 30. DATA QUALITY GATING

Before inference:

```python
def can_infer(feature_vector, *, max_age_s: float, max_missing_pct: float) -> tuple[bool, str]:
    missing_pct = float(feature_vector.missing_mask.mean())

    if feature_vector.max_source_age_s > max_age_s:
        return False, "stale features"

    if missing_pct > max_missing_pct:
        return False, "too many missing features"

    return True, "ok"
```

A model should abstain when the market data required for its training distribution is unavailable.

Examples:

- no L2 => execution model may use a reduced-feature model or abstain
- no derivatives => crypto outcome model must know those features are missing
- stale Kraken tape => do not reuse old tape flow as current flow

---

# 31. MODEL FAMILIES BY ASSET CLASS

Do not assume one universal model will dominate.

Benchmark:

```text
shared model + asset-class feature
vs
crypto-specific model
vs
equity-specific model
vs
futures-specific model
```

Likewise for horizons.

A reasonable early structure may be:

```text
crypto_outcome_5m
crypto_outcome_15m
crypto_path_intraday
crypto_execution_kraken

equity_outcome_15m
equity_outcome_1h
equity_path_intraday
equity_execution_alpaca
```

Only split models when sample size supports it.

---

# 32. EXECUTION MODEL SHOULD LEARN VENUE-SPECIFIC BEHAVIOR

Keep venue identity explicit.

```text
same symbol
same signal
different venue
=
different spread/depth/fill/slippage process
```

The model should be able to distinguish:

```text
Kraken
Alpaca
future venues
```

If data eventually supports multiple crypto venues, the execution model can later provide inputs to an execution router, but do not implement smart routing before there is enough measured cross-venue data.

---

# 33. HISTORICAL ANALOGS SHOULD BE EXPLAINABLE

When displaying analog output, provide enough context to audit it:

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

Avoid UI statements such as:

```text
"AI found the same pattern 84 times."
```

unless the similarity threshold and definition are visible somewhere in diagnostics.

---

# 34. MEASURE INCREMENTAL INFORMATION, NOT DUPLICATED INFORMATION

For each NPU feature/model, run ablation tests.

Example:

```text
BASELINE
TA + structure + strategy + regime

+ state encoder
+ analogs
+ outcome model
+ path model
+ execution model
```

If adding an NPU model provides no improvement once existing deterministic features are present, it is redundant.

This is especially important because many technical indicators encode the same underlying price history.

---

# 35. FIX PATTERN MEMORY AUTHORITY

Inspect `lib/learning_engine.py`.

Pattern observations at tiny sample sizes may be useful as:

```text
"seen before"
```

but not:

```text
"historically reliable"
```

Refactor pattern context to include uncertainty.

Example:

```python
def describe_pattern(total: int, wins: int) -> str:
    if total < 10:
        return (
            f"Pattern seen {total} times. "
            "Sample is too small for a performance conclusion."
        )

    rate = wins / total
    return (
        f"Pattern seen {total} times; observed win rate {rate:.1%}. "
        "Use calibrated expectancy for statistical authority."
    )
```

Better still, route pattern performance through the same sample-aware/hierarchical statistical layer where practical.

---

# 36. FINAL IMPLEMENTATION CHECKLIST

Claude Code must not finish merely because files compile.

Before declaring the NPU upgrade complete, verify:

```text
[ ] NPU detected on target host
[ ] CPU fallback tested
[ ] OpenVINO model loads
[ ] PyTorch/OpenVINO parity passes
[ ] schema mismatch fails closed
[ ] stale data causes abstention
[ ] missing data is distinct from zero
[ ] async inference cannot block market-data loop
[ ] queue coalesces stale states
[ ] all predictions persisted
[ ] predictions join to realized outcomes
[ ] outcome probabilities calibrated
[ ] path labels leakage-safe
[ ] execution labels use submit-time state
[ ] replay vs live evidence remains distinct
[ ] analog lookup is time-safe
[ ] drift monitors residuals
[ ] champion/challenger rollback works
[ ] NPU cannot override hard NO_TRADE
[ ] NPU cannot set leverage/size/liquidation
[ ] no duplicate TA/regime/strategy engines created
[ ] pattern-memory tiny samples no longer claim authority
[ ] baseline vs NPU ablation report generated
[ ] all new tests pass
```

The intended finished architecture is:

```text
                           JARVIS
                              │
             ┌────────────────┼────────────────┐
             │                │                │
             ▼                ▼                ▼
            CPU          INTEL NPU          RTX 5090
             │                │                │
      deterministic       predictive       generative /
        authority        inference          training
             │                │                │
             │          ┌─────┴─────┐          │
             │          ▼           ▼          │
             │       future       execution    │
             │       outcome      uncertainty  │
             │          │           │          │
             │          └─────┬─────┘          │
             │                ▼                │
             │           model trust           │
             │                │                │
             └────────────────┼────────────────┘
                              ▼
                    evidence + constraints
                              │
                              ▼
                         TRADE / NO_TRADE
                              │
                              ▼
                           EXECUTION
```

That is the goal: **use the NPU to add conditional prediction and execution intelligence that JARVIS does not already possess, while preserving the deterministic systems that already work.**
