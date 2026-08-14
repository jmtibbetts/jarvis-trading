# JARVIS v8 — Claude Implementation Plan
## Evidence-first profitability, risk correctness, data expansion, UI refactor, and operational hardening

**Target repository:** `jmtibbetts/jarvis-trading`  
**Audit baseline:** current `main` / JARVIS 8.0.0-era code reviewed on 2026-08-13  
**Primary goal:** make JARVIS more profitable *by making it harder for unsupported assumptions, duplicated semantics, stale data, and sizing shortcuts to reach capital.*

---

# 0. HOW CLAUDE MUST USE THIS PLAN

Do **not** rewrite JARVIS from scratch.

Preserve the working architecture, APIs, Svelte look-and-feel, data already collected, and existing safety/learning infrastructure. Make changes incrementally, with tests before promotion to the next phase.

Before any mutating work:

1. Create a dedicated branch.
2. Run the existing Python tests and record the baseline.
3. Run:
   - `frontend/npm run check`
   - `frontend/npm run build`
4. Force a non-trading development environment:
   - `JARVIS_DISABLE_SCHEDULER=1`
   - broker mode paper only
   - global live-trading kill switch paused
   - temporary/test database only
5. Never run a mutating test against `data/jarvis.db` or any operator database.
6. Never place a real broker order while implementing or testing this plan.
7. Never silently repair bad market data into a favorable trading value.
8. Never make an unavailable feature mean “neutral” unless neutral was actually measured.
9. Commit one phase at a time and keep the application runnable after every phase.

The first objective is not adding more indicators. The first objective is eliminating semantic contradictions in the current execution path.

---

# 1. NON-NEGOTIABLE AUTHORITY BOUNDARIES

JARVIS must distinguish these concepts permanently:

## 1.1 Observed evidence
What is true *now*:
- price
- structure
- RSI / MACD / ATR
- volume
- book state
- spread
- funding
- OI
- liquidation state
- news
- macro releases
- positioning
- regime

## 1.2 Statistical edge
What historically happened to genuinely comparable setups:
- calibrated probability
- sample size
- confidence interval
- average win R
- average loss R
- expected gross R
- expected cost R
- expected net R
- source mix: live vs replay
- OOS strategy state

## 1.3 Risk
How much the account can afford to lose:
- risk budget
- portfolio heat
- correlated exposure
- free cash
- maximum margin
- drawdown state
- stop distance
- liquidation buffer
- venue limits

## 1.4 Execution
How the trade can actually be placed:
- product
- venue
- quantity
- notional
- leverage
- order type
- initial stop
- target
- spread/slippage assumptions
- fees/funding/borrow
- broker constraints

## 1.5 LLM output
Qualitative reasoning and hypothesis generation only.

An LLM confidence value must **never** be interpreted as:
- a calibrated probability
- Kelly input
- position-size authority
- leverage authority
- fee authority
- liquidation authority
- a reason to override NO_TRADE

Composite scores must not silently change meaning between modules.

---

# 2. VERIFIED P0 LOGIC ERRORS — FIX BEFORE ADDING NEW ALPHA

These are current code-path problems, not feature requests.

---

## P0.1 — Live execution still gates and sorts on the measured-inverted composite score

### Current problem
`jobs/execute_signals.py` queries live candidates using:

- `coalesce(composite_score, confidence) >= live_min_score`
- descending sort by that same score

The learning system currently reports that the existing composite has been empirically inverted in important samples. The repo already has shadow variants and selection-bias measurement precisely because this is known.

### Why this matters
Even if expectancy later rejects some bad trades, the initial query has already:
- discarded low-score candidates that may contain more winners
- preferentially admitted high-score candidates that currently measure worse

That creates selection bias before the expectancy layer even sees the universe.

### Required change
Remove legacy composite score as a live-capital eligibility gate.

Create distinct fields/semantics:

```text
evidence_score          descriptive / diagnostics
calibrated_probability  statistical estimate with sample
expected_net_r          profitability estimate
decision                TRADE | WATCH | NO_TRADE | SHADOW
risk_budget_usd         deterministic
```

The live candidate query should initially gate only on hard validity:
- active/non-paper
- valid instrument
- valid side/levels
- sufficient data quality
- sufficient freshness
- not expired
- strategy permitted
- execution venue can trade it

Then run the statistical decision engine.

Composite score may remain:
- visible
- searchable
- a shadow-ranking feature
- input to experimental models

It must not regain live authority until a walk-forward/OOS promotion test demonstrates incremental net-R value.

---

## P0.2 — `risk_manager.py` turns “confidence” into Kelly probability

### Current problem
`calculate_position_size()` currently:
- reads `signal["confidence"]`
- clamps it to a pseudo win rate
- feeds it to Kelly
- applies a confidence multiplier

Worse, `execute_signals.py` currently writes `composite_score` into the temporary dict field named `confidence`, so `risk_manager` can unknowingly treat **composite evidence score as win probability**.

### Required change
Delete the confidence-derived Kelly path.

If Kelly is used at all:
- probability must come from calibrated historical evidence
- average win/loss must come from measured outcomes
- minimum sample must be enforced
- uncertainty must reduce—not increase—size
- use a conservative fractional Kelly cap
- if no statistically valid probability exists, Kelly contribution is zero

Raw/model confidence can stay in diagnostics but has no sizing effect.

---

## P0.3 — Live sizing fails open when risk sizing crashes

### Current problem
If `calculate_position_size()` raises, `execute_signals.py` creates a fallback dollar budget from “confidence.”

This means the safety layer can fail and the system trades anyway.

### Required change
For live capital:

```text
RISK ENGINE ERROR -> NO_TRADE
```

If an operator explicitly wants a deterministic emergency fallback, it must be:
- opt-in
- disabled by default
- fixed-fractional
- stop-risk bounded
- visibly labeled
- independently revalidated against the same invariants

Never fall back to model confidence.

---

## P0.4 — Live executor adds a second “conviction sizing” multiplier after risk sizing

### Current problem
After the risk manager returns a position budget, equities can receive another 1–2x multiplier based on the same score/confidence semantic.

This can enlarge a position *after* the risk decision.

### Required change
The risk engine must return the final maximum executable quantity/notional.

Downstream execution may reduce that amount for:
- cash
- venue limit
- liquidity
- integer-share rounding
- slippage
- portfolio constraints

Execution may **never increase it**.

Add an invariant test:

```text
executed_risk <= approved_risk
executed_notional <= approved_notional
```

---

## P0.5 — Strategy lifecycle multipliers are defined but not applied

### Current problem
`strategy_lifecycle.py` defines:
- ACTIVE = 1.00
- REDUCED = 0.50
- EXPERIMENTAL = 0.25
- SHADOW = 0
- DISABLED = 0

The live executor currently checks only whether the multiplier is <= 0. It does not apply 0.50 or 0.25 to live sizing.

### Required change
Apply lifecycle sizing to the **risk budget**, not as a post-hoc quantity multiplier.

Example:

```python
allowed_risk = base_allowed_risk * lifecycle_multiplier
```

Then solve quantity from that reduced risk.

Do not multiply an already-rounded order quantity afterward.

### Also fix
Lifecycle currently includes replay evidence by default without the same explicit weighting discipline used elsewhere.

For live-capital state:
- keep live and replay counts separate
- either judge live state from live evidence only after adequate sample
- or explicitly weight replay lower
- surface the evidence mix in UI

Unmeasured strategies should primarily generate evidence in Auto Sim / Paper. If live EXPERIMENTAL trading remains enabled, make it an explicit operator policy and keep the 0.25 multiplier real.

---

## P0.6 — Paper engine lets score/confidence “earn leverage”

### Current problem
`lib/leverage_policy.py` and `lib/paper_engine.py` still implement:

```text
high score -> higher leverage
```

The stop is then tightened to fit the chosen leverage.

This is backwards.

### Correct ordering

```text
market structure determines invalidation
        ↓
initial stop is fixed
        ↓
statistical edge determines TRADE / NO_TRADE
        ↓
account risk determines allowed loss
        ↓
qty is solved from stop distance
        ↓
venue/margin/liquidation constraints determine max leverage
        ↓
actual leverage is the minimum safe/allowed amount
```

The stop must not move simply because a score requested more leverage.

### Required replacement
Replace `score_leverage()` authority with `max_safe_leverage()`.

Inputs should include:
- entry
- stop
- qty/notional
- account risk
- margin available
- venue product
- venue leverage caps
- maintenance margin
- liquidation buffer
- spread/slippage
- volatility/gap allowance
- portfolio heat

Output:
- max safe leverage
- selected leverage
- limiting constraint
- liquidation-buffer estimate

`0x / NO_TRADE` must be valid.

---

## P0.7 — Paper risk sizing can explicitly reject, then the caller opens a flat-size position anyway

### Current problem
`open_paper_position()` calls `size_position()`.

If `size_position()` returns `ok=False`, the caller falls back to a flat margin/notional calculation and opens the position.

This bypasses reasons such as:
- one futures contract exceeds the risk budget
- insufficient free cash
- invalid stop-risk
- venue constraints

### Required change

```text
size_position().ok == False -> NO_TRADE / PAPER_REJECTED
```

Do not convert a deterministic rejection into a trade.

If a legacy/manual simulator requires flat sizing, create a separate explicit function:
`open_legacy_flat_sim_position()`
and never call it from the normal autonomous paper path.

---

## P0.8 — Non-futures paper sizing is still primarily margin-first, not stop-risk-first

For every asset class, enforce:

```python
loss_at_stop <= allowed_account_risk
```

Use contract multipliers where applicable.

For equities/crypto:

```python
risk_per_unit = abs(entry - stop)
qty_by_risk = allowed_risk / risk_per_unit
```

Then constrain by:
- cash
- margin
- venue max size
- liquidity
- minimum order
- notional cap
- portfolio heat

Margin is a financing constraint, not the definition of risk.

---

## P0.9 — Hard-coded equity market hours are wrong across DST, holidays, and half-days

### Current problem
`jobs/execute_signals.py` hard-codes approximately:

```text
13:30 UTC <= now < 20:00 UTC
weekdays only
```

That works during U.S. daylight time but not standard time, and it ignores:
- exchange holidays
- early closes
- special sessions

The check appears more than once in the same execution path.

### Required change
Create one authoritative `lib/market_clock.py`.

Preferred source:
- broker/exchange clock + calendar already available through the execution venue

Expose:

```python
market_status(asset_class, venue, now=None)
is_open(...)
next_open(...)
next_close(...)
session_id(...)
```

Tests must include:
- EDT day
- EST day
- weekend
- full holiday
- half-day close
- exact open boundary
- exact close boundary

Never hand-code UTC exchange hours again.

---

## P0.10 — `UNKNOWN` expectancy is allowed to reach live capital

### Current problem
The live executor deliberately allows expectancy verdict `UNKNOWN` to pass.

This is reasonable for evidence collection but wrong as a default *live-capital* policy.

### Required policy
Separate evidence generation from capital deployment:

```text
UNKNOWN EDGE
    -> Auto Sim = yes
    -> Paper = yes
    -> Live = no by default
```

Optional operator setting:

```text
ALLOW_EXPERIMENTAL_LIVE=false
EXPERIMENTAL_MAX_RISK_MULT=0.10 or 0.25
```

This solves the “if we never trade it we never learn” problem without making real/paper-broker capital the data-collection mechanism.

---

## P0.11 — Robust lower-bound expectancy is calculated but not used as the main live verdict

`lib/expectancy.py` calculates both:
- point-estimate net R
- lower confidence-bound net R
- `robust` boolean

Live decision should lean on the conservative estimate.

Suggested live rule:

```text
point net R >= minimum
AND
lower-bound net R >= robust minimum
```

Or create state tiers:

```text
ROBUST_TRADE
TENTATIVE -> paper/shadow
NO_TRADE
UNKNOWN
```

Do not calculate uncertainty and then ignore it at the capital boundary.

---

## P0.12 — Executed stop provenance is incomplete for learning

### Current problem
The live executor can modify the local stop before submitting the broker order.

`TradeOutcome` does not store an immutable initial executed stop.

`expectancy.py` and `strategy_lifecycle.py` derive R using the originating `TradingSignal.stop_loss`.

Those can diverge.

### Required schema
At the moment a trade is born, store immutable execution facts:

```text
planned_entry
actual_entry
initial_stop_loss
initial_target
initial_risk_per_unit
approved_qty
approved_notional
approved_risk_usd
product
venue
```

The existing `execution_samples.stop_loss` can participate, but the learning ledger needs a canonical immutable initial-risk field.

All R calculations must use:

```text
actual/approved entry + initial stop as placed
```

Never:
- trailed stop
- later model stop
- original signal stop when execution changed it

Backfill only rows where provenance is certain. Mark others `risk_basis_unknown`; do not invent.

---

## P0.13 — Unknown direction currently defaults to LONG

`trade_side.normalize_side()` defaults any unrecognized direction to long.

That is acceptable for reading legacy UI history, but not for order execution.

### Required change
Create strict parsing:

```python
parse_side_strict(direction) -> long | short | None
```

Live/paper order creation:
- unknown -> validation error / NO_TRADE

Legacy display/import:
- permissive normalization allowed only when explicitly requested

Also remove the duplicate direction/leverage alias system from `paper_engine.py` over time and make `trade_side.py` the sole side authority.

Direction and leverage must be separate concepts.

---

## P0.14 — Top HUD kill-switch cancel logic is broken

In `TopHud.svelte`, the prompt result is coalesced to a string before the code checks whether it is `null`, so pressing Cancel can still pause trading.

### Correct pattern

```ts
const raw = window.prompt(...)
if (raw === null) return
const reason = raw.trim() || "Manually paused"
await killSwitchStore.toggle(reason)
```

Add a UI test.

---

## P0.15 — Web API is unauthenticated, binds `0.0.0.0`, and allows wildcard CORS

The repo itself acknowledges the dashboard API has no authentication.

`main.py` currently:
- starts Uvicorn on `0.0.0.0`
- permits `allow_origins=["*"]`
- exposes state-changing trade endpoints

### Required safe default
Default:

```text
JARVIS_BIND_HOST=127.0.0.1
```

If the operator chooses a non-loopback host:
- require authentication for all mutating endpoints
- restrict CORS to configured origins
- add CSRF protection if using cookie sessions
- preserve Telegram-specific authorization independently
- do not expose Swagger mutation capability without auth

Classify endpoints:

```text
READ
CONTROL
TRADE_MUTATION
DESTRUCTIVE_ADMIN
```

Require progressively stronger authorization.

Do not rely on “it is probably behind a firewall.”

---

## P0.16 — Secret storage/history needs a permanent boundary

The pre-commit hook documents that a live key/secret was previously committed inside SQLite backup data and that provider credentials existed in plaintext DB content.

### Required changes
1. Verify all credentials exposed before the history rewrite have been rotated.
2. Never store provider secrets in exportable SQLite plaintext if avoidable.
3. Prefer:
   - OS credential store / Windows DPAPI / Keyring for local secret material
   - environment references for deployment
4. DB stores secret references/metadata, not the secret itself.
5. Keep the existing staged-secret scanner.
6. Add CI secret scanning as a second line of defense.

---

## P0.17 — Tests must be structurally incapable of touching the operator DB

A prior test accidentally reset the active paper book. Gating individual tests is not enough.

### Required test architecture
When `pytest` is running:
- database URL defaults to a temporary directory
- application DB path is rejected unless a very explicit integration flag is set
- scheduler disabled
- broker writes disabled
- network-mutation adapters mocked

Add a guard test that deliberately attempts to select `data/jarvis.db` and expects refusal.

---

# 3. REPLACE THE CURRENT DECISION PIPELINE WITH EXPLICIT TYPES

Do not pass ambiguous dicts where a field named `confidence` can mean raw LLM confidence in one module and composite score in another.

Introduce typed decision objects/dataclasses/Pydantic models.

Suggested structures:

```python
ObservedEvidence
    symbol
    instrument_id
    asset_class
    side
    timeframe
    timestamp_exchange
    timestamp_received
    timestamp_ingested
    data_quality
    freshness
    feature_schema
    ta
    regime
    microstructure
    derivatives
    macro
    news

StatisticalEdge
    model/version
    sample_live
    sample_replay_weighted
    p_win
    p_win_ci
    avg_win_r
    avg_loss_r
    gross_expected_r
    expected_cost_r
    net_expected_r
    net_expected_r_lower
    bucket
    oos_strategy_state

RiskDecision
    account_equity
    base_risk_pct
    drawdown_multiplier
    lifecycle_multiplier
    correlation_multiplier
    allowed_risk_usd
    stop_distance
    qty_by_risk
    max_notional
    max_safe_leverage
    limiting_constraint

ExecutionPlan
    venue
    product
    order_type
    qty
    notional
    leverage
    entry
    initial_stop
    target
    estimated_fees
    estimated_spread
    estimated_slippage
    estimated_funding
    liquidation_buffer

TradeDecision
    decision
    reasons[]
    evidence
    edge
    risk
    execution
```

A module should never infer probability from an arbitrary score merely because both happen to be 0–100.

---

# 4. SCORING REFACTOR — KEEP THE SCORE, REMOVE ITS FALSE AUTHORITY

JARVIS has already done the important scientific work: it measured that some intuitively appealing score components are inverted.

Do not hand-flip weights and call it solved.

## 4.1 Rename/reframe
In UI and APIs, call the current value something like:

```text
Evidence Composite
```

not “probability,” “confidence,” or automatic “opportunity.”

Display:
- current measured relationship to outcome
- score band sample
- whether the score version is predictive, neutral, or inverted

## 4.2 Stop historical double counting
Currently historical outcomes can influence:
- calibrated confidence
- timeframe edge
- strategy edge
- expectancy
- lifecycle
- potentially ranking

Refactor into:
- point-in-time evidence score = current market state only
- statistical edge = outcome history only
- risk = account state only

Do not add multiple historical-performance bonuses to one composite and then feed the same history into EV again.

## 4.3 Promote score variants scientifically
Keep A/B/C shadow variants.

Promotion requirements:
- chronological walk-forward
- multiple regimes
- enough calendar span
- after-cost net R improvement
- drawdown/tail-loss not worse beyond tolerance
- selection-frequency reported
- no leakage
- champion artifact immutable/versioned

A challenger that wins in-sample does not replace production.

---

# 5. UNIFY RISK / LEVERAGE / POSITION SIZING

Create one authoritative sizing service used by:
- live execution
- paper
- auto sim
- manual sizing calculator
- backtests/replays where appropriate

Suggested file:

`lib/risk_engine.py`

## Algorithm

### Step 1 — validate
- strict instrument identity
- strict side
- valid entry/stop/target geometry
- non-stale price
- product exists at venue

### Step 2 — freeze initial stop
Stop comes from:
- market structure
- strategy invalidation
- ATR sanity bounds

It is not changed to make desired leverage possible.

### Step 3 — compute risk
```python
risk_per_unit = stop_distance * contract_multiplier
```

### Step 4 — account risk budget
Base risk must be configurable and then only reduced by:
- drawdown
- lifecycle
- portfolio heat
- correlation
- volatility/gap risk
- liquidity

### Step 5 — quantity by risk
```python
qty = allowed_risk_usd / risk_per_unit
```

### Step 6 — constrain
Apply:
- cash
- margin
- venue min/max qty
- whole contracts/shares
- notional cap
- sector/correlation cap
- concentration
- liquidity/ADV
- spread/slippage
- product leverage max

Always round DOWN unless the venue's quantity rule requires another deterministic treatment.

### Step 7 — derive leverage
Leverage is a consequence of:
- notional
- committed margin
- venue rules
- liquidation-buffer requirement

Do not let a score request leverage.

### Step 8 — revalidate after rounding
Recompute:
- loss at stop
- expected costs
- net R
- margin
- liquidation buffer

If any invariant fails -> `NO_TRADE`.

---

# 6. MARKET / INSTRUMENT IDENTITY MUST BECOME CENTRALIZED

Several modules still infer asset class with string heuristics such as:
- contains `/`
- ends with `USD`
- ends with `=F`
- local symbol maps

Create `lib/instrument_registry.py` or extend the existing `lib/instruments.py`.

Canonical identity:

```text
instrument_id
display_symbol
venue_symbol
asset_class
product
quote_currency
contract_multiplier
tick_size
lot_size
min_qty
margin_model
session_calendar
venue
```

Use it everywhere.

This prevents another `BEAT`-style collision where a crypto token and equity ticker share a bare symbol.

No fallback from a fully-qualified crypto instrument to an unrelated bare equity ticker.

---

# 7. REGIME: REMOVE THE SPLIT-BRAIN IMPLEMENTATION

The repo now has:
- old SPY-centric `market_regime.py`
- newer multi-axis `regime_axes.py`

But live execution/risk still consumes the old single SPY risk label.

## Required direction
Keep `market_regime.py` only as:
- equity/global context
- backward-compatible display if necessary

Make `regime_axes` or a successor the statistical/risk interface.

Improve futures benchmarks further:
- energy -> energy-specific benchmark/context
- metals -> metals-specific
- grains -> agriculture-specific
- equity index futures -> equity benchmark
- rates -> rates curve/yield context
- FX -> DXY/rates differential context

Do not use SPY as the primary regime for crude oil, wheat, or BTC.

Each axis must:
- carry timestamp
- carry confidence
- abstain when unavailable

---

# 8. EXECUTION CORRECTNESS

## 8.1 One normalized order plan
The exact same normalized levels used for:
- EV
- risk sizing
- execution
- persistence

No local stop modification after sizing without re-running the decision engine.

## 8.2 Order-intent persistence
Before submit persist:
- intended order
- initial stop/target
- risk budget
- microstructure
- expected costs
- model/score versions
- evidence snapshot id

After fill persist:
- actual entry
- actual qty
- fill delay
- slippage
- partial fill
- actual bracket state

## 8.3 Execution failures
Classify:
- rejected by venue
- auth
- asset unavailable
- insufficient qty
- insufficient BP
- crossed/invalid market data
- timeout
- duplicate/position conflict
- partial fill

A failed order is training data.

## 8.4 Maker/taker policy
Do not always assume market if data eventually shows that spread/slippage consumes the edge.

Test in shadow:
- market
- marketable limit
- passive limit with timeout/reprice
- no-trade when spread exceeds threshold

Optimize **after-cost fill outcome**, not fill rate alone.

---

# 9. DATA PLATFORM — WHAT TO ADD NEXT

The repo already has a broad stack. Do not add another generic quote vendor simply because it exists.

Priority should be **new dimensions** of information.

---

## 9.1 CFTC Commitments of Traders — HIGH VALUE FOR FUTURES/MACRO

**Source:** official CFTC public reporting/API.

Use as slow positioning/regime context, not an intraday trigger.

Store:
- report market
- report date
- publication/availability timestamp
- producer/merchant
- swap dealer
- managed money
- other reportables
- nonreportable
- long/short/spreading where dataset supplies it

Derive point-in-time:
- net managed-money position
- weekly change
- 52w percentile
- z-score
- crowding
- commercial vs speculative divergence

Use for:
- crude
- natural gas
- gold
- copper
- grains
- equity index futures
- FX futures/rates where mapped

**Critical:** backtests must join on **release availability**, never future report content.

---

## 9.2 FINRA Daily Short Sale Volume — HIGH VALUE EQUITY FLOW CONTEXT

This is distinct from the existing semi-monthly short-interest dataset.

Store:
- trade date
- symbol
- FINRA short volume
- exempt volume
- total reported volume
- source/file

Derive:
- daily FINRA short-volume ratio
- rolling percentile
- 5d/20d change
- abnormal activity flag

Do **not** label it:
- short interest
- bearish positioning
- total-market short ratio

FINRA explicitly notes it covers reported off-exchange/publicly disseminated activity and is not the same as short interest.

Shadow it first and measure incremental outcome value.

---

## 9.3 SEC Fails-to-Deliver — MEDIUM/HIGH VALUE FOR EQUITY STRESS / SPECIAL SITUATIONS

Store:
- settlement date
- symbol/CUSIP
- fail quantity
- previous-day price
- availability date

Derive:
- fail notional
- rolling percentile
- persistence
- change
- fail intensity relative to known volume/shares where legitimately sourced

Use as:
- settlement stress
- crowding/special-situation context
- potential squeeze/catalyst feature

Do not use FTD alone as a directional buy signal.

Respect the semi-monthly publication lag in replay.

---

## 9.4 EIA OPEN DATA — HIGH VALUE FOR ENERGY FUTURES

Use the official EIA API/bulk releases.

For crude/refined products:
- commercial inventories
- refinery utilization
- production
- imports/exports
- product supplied
- gasoline/distillate stocks

For natural gas:
- storage
- injection/withdrawal
- region
- year-over-year
- seasonal percentile / five-year context

Derive only what the data supports:
- WoW change
- YoY change
- seasonal deviation
- inventory regime
- production trend
- storage tightness

Do not call a number an “inventory surprise” unless JARVIS has a timestamped consensus forecast source available before the release.

Release timestamp must be stored.

---

# 10. OPEN-SOURCE REPOS WORTH USING — AND HOW

Do not blindly add dependencies. Use each for the part it is better at than JARVIS.

## 10.1 `bmoscon/cryptofeed` — BEST FIT FOR RAW CRYPTO MARKET-DATA NORMALIZATION
Strong candidate for:
- standardized public WS adapters
- trades
- L1/L2/L3 where venue supports it
- funding
- OI
- liquidations
- candles
- normalized callbacks
- backend sinks

Recommended approach:
- prototype behind JARVIS's own canonical event interface
- keep current bespoke feeds until parity is verified
- do not replace working adapters in one cutover
- compare event counts, latency, sequence health and book parity
- promote per venue only after tests

This is more directly useful to JARVIS's raw-event plan than adding another REST quote library.

## 10.2 `ccxt/ccxt` — USEFUL FOR REST/METADATA/CONNECTOR BREADTH
Good for:
- venue discovery
- symbol metadata
- historical/public REST where suitable
- consistent exchange capability introspection
- future venue adapters

Do not automatically replace low-latency custom WS paths with generic abstractions.

## 10.3 `hummingbot/hummingbot` — ARCHITECTURE REFERENCE, NOT A DEPENDENCY MANDATE
Study:
- connector boundaries
- executor/order-lifecycle abstractions
- strategy vs execution separation
- CEX/DEX/perp product modeling

Do not graft Hummingbot wholesale into JARVIS.

## 10.4 `OpenBB-finance/OpenBB` — OPTIONAL RESEARCH/FUNDAMENTAL INTEGRATION LAYER
Potentially useful for:
- standardized public/proprietary research integrations
- fundamentals/macro breadth
- agent/research endpoints

It is not a low-latency execution feed and should not sit in the hot trading path.

---

# 11. RAW EVENT STORAGE — THE BIGGEST DATA-ASSET GAP

Current live feeds mostly retain only recent in-memory state or derived bars.

That means JARVIS cannot later reconstruct the microstructure that existed before a trade.

## Canonical event model
For every raw event, where source supplies it:

```text
event_id
provider
venue
instrument_id
event_type
exchange_timestamp
received_timestamp
ingested_timestamp
sequence
schema_version
payload_version
price
quantity
side
bid
ask
book_level
flags
```

## Book health
Maintain:

```text
VALID
STALE
GAP_DETECTED
RESYNCING
INVALID
```

If a sequence gap is detected:
- mark book INVALID
- stop emitting L2-derived trading features
- request/resubscribe snapshot
- only restore VALID after reconciliation

Never calculate “book imbalance” from known-corrupt state.

## Backpressure
Bound queues.
Persist drop counters.
If events are dropped, mark feature/data-quality windows incomplete.

Silent loss is not acceptable.

## Storage architecture
Keep responsibilities separated:

- SQLite: app state, configuration metadata, decisions, summaries
- immutable Parquet: training snapshots/datasets
- raw high-rate time-series store: introduce only after measuring bytes/day

Measure one Tier-1 symbol for a week before choosing ClickHouse/QuestDB/Postgres/other high-volume storage.

Do not prematurely migrate the whole app database.

---

# 12. THREE-CLOCK DISCIPLINE

Live Kraken trade flow currently records local receipt time; training-sensitive feeds need source/exchange timing too where supplied.

Every event should preserve:

```text
exchange_timestamp
received_timestamp
ingested_timestamp
```

Derive:
- transport latency
- ingest latency
- clock skew
- stale-source flags

Never backtest with `received_timestamp` while describing it as market/exchange time.

---

# 13. FEATURE VERSIONING / PROVENANCE

A feature must be immutable in meaning.

Bad:
```text
book_imbalance
```

Good:
```text
book_imbalance_top20_qty_v1
book_imbalance_10bps_notional_v1
```

Every prediction/decision must record:
- feature schema version
- model version
- score version
- cost-model version
- regime version
- execution-policy version

If the formula changes, the version changes.

---

# 14. UI / UX REFACTOR — KEEP THE LOOK, IMPROVE THE INFORMATION ARCHITECTURE

The visual language is already coherent. Do not redesign it into a generic SaaS dashboard.

The problem is hierarchy and growth.

---

## 14.1 Fix the navigation architecture

There are now nine actual destinations even though documentation still describes six.

Group them visually:

```text
TRADE
  Command
  Signals
  Positions

INTELLIGENCE
  World
  Smart Money
  Macro
  Crypto

REVIEW
  Performance

SYSTEM
  Ops
```

Keep current routes/hashes to preserve compatibility.

Use:
- separators
- grouped tooltip headers
- optional expanded rail labels
- keyboard shortcuts displayed in tooltips

---

## 14.2 Command Center should answer five questions first

At first glance:

1. Can JARVIS trade safely right now?
2. What capital is at risk?
3. What needs operator attention?
4. What are the best statistically supported opportunities?
5. What changed materially?

Primary top area:
- trading state
- data health
- equity/drawdown
- portfolio heat
- open risk
- decision queue
- alerts

Move secondary exploratory material into the dedicated desks rather than duplicating everything on Command.

---

## 14.3 Signal cards: change the visual hierarchy

Today the radial composite score is visually dominant.

New primary hierarchy:

```text
TRADE / WATCH / NO_TRADE / SHADOW
NET EV: +0.18R
ROBUST LOWER: +0.07R
RISK: $X at stop
SIZE: Y qty / $Z notional
```

Secondary:
- calibrated probability + sample
- strategy state
- timeframe
- R:R
- hold estimate

Diagnostic:
- Evidence Composite
- score version
- raw model confidence
- score breakdown

When a score is currently inverted/non-predictive, show that explicitly.

---

## 14.4 Stop calling unrelated numbers “confidence”

Use exact labels:
- `LLM stated confidence`
- `calibrated historical win rate`
- `evidence composite`
- `net expected R`
- `data quality`
- `freshness`

Do not show one generic “confidence” percentage.

---

## 14.5 Split `Intelligence.svelte`

The current component owns World, Smart Money, Macro and Crypto Desk logic.

Extract:

```text
sections/intelligence/WorldDesk.svelte
sections/intelligence/SmartMoneyDesk.svelte
sections/intelligence/MacroDesk.svelte
sections/intelligence/CryptoDesk.svelte
```

Shared subcomponents:
- SourceHealth
- FilterBar
- ExpandableTable
- MetricRow
- ProvenanceBadge

Keep the current route surface.

---

## 14.6 Break up other frontend monoliths

Refactor without changing behavior:

- `CommandCenter.svelte`
- `SignalsScanner.svelte`
- `PositionsPaper.svelte`
- `api.ts`

Create domain API modules:

```text
api/client.ts
api/signals.ts
api/positions.ts
api/intelligence.ts
api/performance.ts
api/ops.ts
api/types.ts
```

---

## 14.7 Shared UI primitives

The sections repeat local CSS for:
- page heads
- 12-column grids
- buttons
- form inputs
- tables
- small labels

Create reusable primitives/tokens.

Continue using the existing visual tokens and migrate opportunistically, but establish one source of truth for:
- spacing
- radius
- button variants
- table density
- focus states
- panel header behavior

---

## 14.8 Accessibility / discoverability fixes

- panel popout button must appear on keyboard focus, not hover only
- nav popout affordance must be usable on touch/keyboard
- icon-only navigation should expose reliable labels/tooltips
- destructive actions require explicit accessible confirmation
- preserve `prefers-reduced-motion`
- test keyboard navigation

---

## 14.9 Stale-build banner grid behavior

Verify the stale-build banner spans the full application grid and does not create an unintended grid row/column.

Use a dedicated header wrapper or `grid-column: 1 / -1`.

---

# 15. BACKEND MODULARIZATION

Do not change API URLs.

## `app/routes.py`
Split internally:

```text
app/routers/signals.py
app/routers/positions.py
app/routers/intelligence.py
app/routers/performance.py
app/routers/ops.py
app/routers/settings.py
app/routers/system.py
```

`app/routes.py` can remain a compatibility aggregator.

## `app/database.py`
Do not perform a risky all-at-once ORM rewrite.

First separate:
- model definitions
- migration registry
- repair/backfill functions
- seed functions

Consider Alembic only if it clearly improves safety over the existing idempotent local migration approach.

---

# 16. POLLING / REAL-TIME DATA FLOW

The app currently has a mixture of:
- 30-second polling
- WebSocket updates
- per-popout polling

Improve incrementally:

1. Keep server truth authoritative.
2. Add a small shared frontend request/cache layer.
3. De-duplicate identical in-flight reads.
4. Invalidate/reload domains when WebSocket events say relevant data changed.
5. Keep polling as a safety fallback.
6. Avoid creating a large state-management dependency unless measurements justify it.

---

# 17. FRONTEND TESTING

Current frontend scripts cover type-check/build but no dedicated component/E2E test runner.

Add:
- Vitest
- Svelte Testing Library
- Playwright smoke tests

Minimum critical tests:
- cancel on live-trading pause prompt does nothing
- confirm pause changes state
- stale-build banner
- nav route persistence
- signal decision badge rendering
- NO_TRADE cannot execute
- SHADOW cannot execute
- reset confirmation copy/behavior
- popout keyboard access
- API error != “no result”

---

# 18. BACKEND TESTS / PROPERTY TESTS

Add property/invariant tests.

## Risk
For random valid long/short geometries:

```text
loss_at_stop <= allowed_risk
```

Always.

## Failure
- risk engine exception -> no live order
- cost engine unavailable -> no live order unless conservative explicit policy
- unknown instrument -> no order
- unknown side -> no order
- invalid book -> L2 model abstains
- stale price -> no order

## Lifecycle
- REDUCED yields exactly 0.5x risk budget
- EXPERIMENTAL yields exactly configured reduced risk
- SHADOW/DISABLED yield zero
- live/replay evidence mix visible and tested

## Market clock
DST, holiday, early close.

## Learning provenance
R always uses immutable initial executed stop.

## Execution
final quantity cannot exceed approved quantity.

---

# 19. CI

Add GitHub Actions or equivalent:

### Python
- install Python 3.12
- unit tests
- no live network requirement for default suite

### Frontend
- `npm ci`
- `npm run check`
- `npm run build`
- component tests

### Safety
- secret scan
- no `.db` files
- no real `.env`
- no generated private credentials

Optional later:
- dependency audit
- lint/format only if introduced without turning the codebase into a formatting-only diff

---

# 20. DOCUMENTATION CLEANUP

The repo currently has multiple generations of docs/version numbers.

Create one status page:

`docs/STATUS.md`

It should say:
- current version
- active architecture
- active UI routes
- current score version
- current model status
- live/paper venue capabilities
- implemented vs planned data sources
- known experimental components

Update:
- README title/version
- FastAPI version
- banner
- user-agent version references where meaningful
- UI guide from six sections to actual current destinations

Archive or clearly mark superseded planning docs.

Do not delete useful historical research; mark it `SUPERSEDED BY ...`.

---

# 21. LEGACY STATIC FILE CLEANUP

The old Jinja/vanilla dashboard is documented as removed, but legacy static assets remain.

Audit references to:
- `static/css/jarvis.css`
- `static/js/settings_defs.js`
- any other pre-Svelte assets

If unreferenced:
- remove them
- add a test/build check that old dashboard assets are not accidentally served

Do not remove anything until reference search proves it dead.

---

# 22. DATA-SOURCE PRIORITY ORDER

After P0/P1 correctness is complete:

## Wave A — almost free, structurally new information
1. CFTC COT
2. FINRA Daily Short Sale Volume
3. SEC FTD
4. EIA energy fundamentals

## Wave B — improve existing live crypto microstructure
1. canonical raw event schema
2. BookHealth
3. three timestamps
4. persistence
5. evaluate Cryptofeed for adapter normalization
6. add additional legal/available venues only when they add independent information

## Wave C — only after ablation proves need
- more options-flow data
- paid social sentiment
- additional dark-pool vendors
- richer on-chain providers
- prediction-market probabilities

No source should enter production scoring because it sounds clever.

Every new source starts as cargo/shadow, then earns promotion.

---

# 23. HOW TO JUDGE EVERY NEW FEATURE

For each candidate feature:

```text
BASELINE
vs
BASELINE + FEATURE
```

Measure OOS:
- net expected R after costs
- realized net R
- profit factor
- max drawdown
- tail losses
- calibration
- MFE/MAE
- selection quality
- trade frequency
- latency
- missingness
- regime dependence

If it does not add incremental value:
- keep as operator context if useful
- otherwise remove/disable it

Never keep a feature in capital allocation merely because it is interesting.

---

# 24. IMPLEMENTATION ORDER FOR CLAUDE

## PHASE 0 — CAPITAL-SAFETY SEMANTICS
Implement and test:
- remove composite live gate
- remove confidence->Kelly
- remove confidence/conviction position enlargement
- apply lifecycle multipliers
- live UNKNOWN expectancy policy
- robust lower-bound policy
- paper fail-open removal
- risk-allows-leverage
- strict side
- market clock
- immutable initial executed stop
- kill-switch cancel
- localhost/CORS safe default
- hermetic DB test guard

**Do not start Phase 1 until every P0 test passes.**

## PHASE 1 — TYPED DECISION/RISK PIPELINE
- explicit evidence/edge/risk/execution models
- unified instrument registry
- unified risk engine
- normalized order plan
- remove ambiguous dict-field semantics
- remove duplicated direction/leverage parsing

## PHASE 2 — LEARNING CORRECTNESS
- eliminate historical double counting
- separate live/replay evidence
- exact stop provenance
- conservative expectancy tiers
- shadow score promotion framework
- selection-bias dashboard aligned to actual live gate

## PHASE 3 — RAW DATA FOUNDATION
- canonical raw events
- timestamps
- feature versions
- BookHealth
- queue/backpressure/drop counts
- measured storage volume
- immutable training snapshots

## PHASE 4 — FREE DIFFERENTIATED DATA
- CFTC
- FINRA Daily Short Sale Volume
- SEC FTD
- EIA
- all shadow-only at first
- release-aware replay tests

## PHASE 5 — CRYPTO FEED NORMALIZATION
- prototype Cryptofeed adapter
- compare against current feeds
- promote per venue only after parity
- optional CCXT metadata/REST adapter
- retain working custom paths until proven replaceable

## PHASE 6 — UI INFORMATION ARCHITECTURE
- grouped nav
- decision-first signal cards
- Command hierarchy
- split Intelligence
- shared primitives
- domain API modules
- accessibility
- frontend tests

## PHASE 7 — BACKEND MODULARIZATION / CI / DOCS
- split routers
- split database responsibilities carefully
- CI
- version single source
- docs status
- dead-asset cleanup
- secret-storage hardening

## PHASE 8 — MODEL PROMOTION
Only after the data/decision path is trustworthy:
- path_features_v2 challenger
- outcome/meta models
- residual/drift
- analogs
- execution model

No model promotes without chronological OOS evidence and after-cost improvement.

---

# 25. REQUIRED END-STATE INVARIANTS

Claude must leave tests proving:

```text
1. LLM confidence cannot increase qty.
2. Composite score cannot directly increase qty.
3. Composite score cannot directly grant leverage.
4. Stop geometry is validated strictly by side.
5. Unknown side cannot become a live long.
6. Stop is selected before leverage.
7. loss_at_stop <= allowed_risk for every executable trade.
8. Risk rejection cannot fall back into a trade.
9. Risk-engine exception cannot fall back into a live trade.
10. Downstream execution cannot exceed approved quantity/notional.
11. Lifecycle REDUCED/EXPERIMENTAL multipliers are actually applied.
12. SHADOW/DISABLED cannot place orders.
13. UNKNOWN statistical edge is paper/shadow by default.
14. Live R-multiples use immutable initial executed risk.
15. Market-open logic handles DST/holiday/half-day calendars.
16. Invalid/stale market data causes abstention.
17. Invalid L2 book cannot produce authoritative microstructure features.
18. Replay data is distinguishable from live evidence everywhere.
19. Feature/model/score/cost versions are persisted.
20. Mutating tests cannot access the operator DB.
21. The local server is not LAN-exposed by default.
22. A cancelled kill-switch prompt performs no mutation.
```

---

# 26. CLAUDE WORKING STYLE

For each phase, Claude should respond/operate in this order:

1. Inspect the exact current implementation.
2. State the behavioral defect being fixed.
3. Identify tests that must exist before/with the change.
4. Make the smallest coherent implementation.
5. Run targeted tests.
6. Run full affected test suites.
7. Run frontend check/build if UI touched.
8. Summarize:
   - files changed
   - behavior before
   - behavior after
   - tests
   - migrations
   - remaining risks
9. Commit the phase.
10. Proceed only if green.

Do not make a giant “cleanup” commit.

---

# 27. MASTER CLAUDE PROMPT

Copy the text below into Claude Code from the repo root.

---

You are the senior quantitative systems engineer, execution engineer, backend engineer, data engineer, and Svelte/TypeScript UI engineer responsible for hardening JARVIS Trading AI.

Your job is **not** to rewrite JARVIS. Your job is to preserve its working architecture while correcting the semantic, risk, execution, data, and UI problems described in `JARVIS_CLAUDE_IMPLEMENTATION_PLAN.md`.

First read the entire plan and inspect current HEAD. Treat the code as source of truth where old documentation is stale.

Before modifying anything:

- create a dedicated branch;
- capture baseline Python tests;
- run the frontend type-check/build;
- force `JARVIS_DISABLE_SCHEDULER=1`;
- use a temporary test database;
- ensure broker writes are disabled and the global kill switch is paused;
- never run a mutating test against the operator database;
- never place a real order.

Then implement **PHASE 0 only**.

Do not begin Phase 1 until Phase 0 is green.

The most important invariants are:

- LLM confidence never increases position size or leverage.
- Composite score never directly increases position size or leverage.
- Statistical probability must come from measured/calibrated outcomes with sample/uncertainty, never by renaming a 0–100 score.
- A deterministic NO_TRADE cannot be overridden by an LLM.
- A risk-engine error cannot fail open into a trade.
- A risk rejection cannot fall back into a flat-size position.
- Stop/invalidation is selected from the market setup before leverage.
- Leverage is constrained by risk, margin, venue rules, liquidation buffer, liquidity, costs and available capital.
- For every executable trade, `loss_at_stop <= allowed_account_risk`.
- Execution can reduce an approved position but can never enlarge it.
- Unknown directions, unknown instruments, stale prices, invalid levels, invalid books and unavailable critical data must abstain rather than invent a favorable default.
- REDUCED and EXPERIMENTAL lifecycle multipliers must actually reduce risk.
- SHADOW and DISABLED strategies cannot place orders.
- UNKNOWN statistical expectancy is paper/shadow by default unless an explicit operator policy says otherwise.
- R multiples use the immutable initial stop actually approved/placed for that trade.
- Market sessions come from a real broker/exchange calendar, not hard-coded UTC hours.
- Tests are hermetic and incapable of mutating the operator database.
- The server binds localhost by default; non-loopback mutation surfaces require authentication and restricted CORS.
- A cancelled trading-state prompt makes no change.

Do not “fix” the inverted composite by merely flipping it and making it authoritative. Keep existing shadow variants and judge replacements chronologically/OOS after costs.

Do not add new alpha/data providers during Phase 0.

After Phase 0:
- show me every changed file,
- show the exact tests proving each invariant,
- identify any DB migration,
- identify anything you could not prove,
- stop before Phase 1 unless all affected tests are green.

Once Phase 0 is accepted, proceed through the remaining phases in the exact order defined in the plan, one tested commit at a time.

