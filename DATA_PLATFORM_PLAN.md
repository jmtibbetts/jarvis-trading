# Live-Learning Data Platform — integrated build plan

Source: `JARVIS_LIVE_LEARNING_DATA_PLATFORM.md`, assessed 2026-08-13 against the
running system and merged with `CLAUDE_JARVIS_PREDICTIVE_GUIDE.md` and
`UPGRADE_PLAN.md`.

Paid stack is assumed and funded — Alpaca Algo Trader Plus, Bitquery Pro,
CoinGlass Startup, plus direct exchange WebSockets. Nothing below is designed
around a free tier.

---

## Verdict

Useful, and unusually well-aligned. Three of its prescriptions were implemented
today *before* the document was read, arriving at the same thresholds and the
same rationale:

| Doc section | Independently built | Match |
|---|---|---|
| §42 pattern-memory tiers `n<10 / 10–25 / ≥25` | `lib/learning_engine.py` Phase 0, commit `31fda44` | exact, including thresholds |
| §26 path labels + `AMBIGUOUS` same-bar rule | `lib/signal_replay.py` Phase 1 | exact, including rationale |
| §32 benchmark CPU vs NPU, don't force neural onto NPU | measured: CPU 21× faster, NPU offload 1.00× free | confirmed empirically |
| §35 chronological splits, no random split | Phase 4 training run | already enforced |
| §3 deterministic authority boundaries | `llm_router` / `expectancy` / `transaction_costs` | already enforced |

Convergence on the parts already built is the reason to trust it on the parts
not yet built.

---

## What it adds that the system genuinely lacks

Ranked by measured value, highest first.

### 1. Save every candidate and resolve counterfactuals (§23, §24, §25)

The single highest-value item in the document.

Today JARVIS persists 39,405 signals but does not systematically resolve *what
would have happened* to the ones it rejected, nor store the rejection reason in
a form that can be trained against. Without that, the filters cannot be
evaluated — the system can only ever learn from trades it already agreed to
take, which is the definition of selection bias.

This is what makes a meta-label model (`TAKE / SKIP / REDUCE / ABSTAIN`)
possible at all. It is also cheap: the replay engine already walks forward
bar-by-bar and now emits MFE/MAE/first-touch. It needs the candidate table and
the rejection reason, not new machinery.

### 2. Store raw events, not just OHLCV (§11, §12)

> OHLCV is derivable from trades. Raw event history cannot be reconstructed
> from OHLCV.

Correct, and currently violated — `data/ohlcv_cache.db` stores only bars. Every
microstructure feature in the predictive guide (book imbalance, tape flow,
aggressive volume, price impact per notional) is unreconstructable after the
fact. Once the paid feeds are live, discarding raw events is discarding the
proprietary asset.

### 3. Book correctness and abstention (§9, §43)

Sequence-gap detection, `INVALID` book state, and mandatory abstention from
L2-derived features while invalid. `lib/orderbook_stream.py` exists but does not
carry a `BookHealth` contract. Producing "book imbalance" from a known-corrupt
book is worse than producing nothing, because it is indistinguishable from a
real reading downstream.

### 4. Clock discipline (§8)

Three timestamps per event — exchange, received, ingested — and never training
latency-sensitive models on receive time while calling it exchange time. Cheap
to add now, impossible to retrofit onto data already collected.

### 5. Feature versioning (§58)

`book_imbalance_10bps_v1` rather than `book_imbalance` whose formula quietly
changes. This codebase has already been bitten by silent redefinition
(`round(x, 6)` across 28 call sites; `outcome='win'` matching zero of 8,899
rows). Pinning feature meaning to a schema version is the same lesson.

### 6. Predictor persistence (§39)

Every prediction stored with model version, feature schema, device, latency, and
resolved later against the actual outcome. This is the only way to answer
whether a model adds value, and it is the input to the residual learner.

### 7. Provider semantics and double-counting (§49, §50)

CoinGlass aggregates venues; direct feeds report per-venue. Adding aggregate OI
to component-venue OI as independent evidence would double-count. The document
is right to call this out explicitly — it is exactly the class of error that
produced the units-vs-contracts loss of $440,371 on a 0.35% move.

### 8. Watchlist tiers (§47) and measured storage (§46)

Tier 1 full L2, Tier 3 bars only, and *record actual bytes/day before
over-engineering*. This is the correct trigger discipline for the ClickHouse
decision below.

---

## Where I disagree, or would sequence differently

### The live-vs-historical framing is a false dichotomy

The document closes with:

> That is a much better live-learning foundation than spending $250 on
> historical subscriptions.

For the *long-term* asset, agreed. For the *current blocker*, no.

Phase 4's path model was rejected this morning for a measured reason: 9,246
labels spanning one usable day, which made the chronological split meaningless.
Live feeds accumulate calendar span at one day per day. On a live-only stack the
path, outcome and analog models stay blocked for months — the document's own §66
is honest about this, calling it a *One-Year Target*.

Alpaca Algo Trader Plus reportedly includes historical data back to 2016. If so,
the paid stack already resolves this and no separate historical purchase is
needed — the two goals are complementary, not competing. **Verify the actual
entitlement depth at intraday resolution before assuming it.** Daily bars are
not sufficient; path labels need 5m/15m.

### ClickHouse is premature *until* raw ingestion starts, and mandatory after

The current DB is 132 MB of SQLite and holds fine. SQLite is not the problem
today. But L2 book deltas are the volume driver, and the moment Tier-1 raw
ingestion begins, SQLite will not hold it.

Do not migrate first. Follow the document's own §46: instrument bytes/day on one
Tier-1 symbol, measure for a week, then migrate on evidence. Build the storage
interface behind an abstraction now so the migration is a swap, not a rewrite.

### Do not build `lib/datafeeds/` as a greenfield package

§60 proposes a structure that partially duplicates working modules.
`lib/orderbook_stream.py` and `lib/crypto_derivatives.py` already exist and work.
§68 rule 3 says not to create parallel duplicates — that rule should win over the
suggested tree. Extend; do not re-scaffold.

---

## Integrated phase order

Phases P0–P2 below slot *before* the remaining predictive-guide work, because
they unblock it. Everything already shipped is unchanged.

### P0 — Candidate persistence and counterfactual resolution *(highest value, no new data required)*

Uses only data already flowing. Independent of every subscription.

- `candidate_signals` table per §24: entry/stop/target reference, feature
  snapshot id, base score, base expectancy, **base verdict and rejection
  reason**, executed boolean. Never overwritten after the fact.
- Resolve counterfactual outcomes for rejected candidates through the existing
  replay engine, which already emits MFE/MAE/first-touch as of Phase 1.
- Selection-bias monitoring: measured rate at which rejected candidates would
  have won.
- Tests per §62: long/short MFE and MAE, stop-first, target-first, ambiguous
  same-bar, no-touch.

Unblocks the meta-label model and makes the filters measurable for the first
time.

### P1 — Provenance, clocks, and feature versioning *(cheap now, impossible to retrofit)*

- Three timestamps per event (§8); `clock_skew_ms` tracked.
- `source`, `source_schema_version`, `ingest_version`, `feature_schema` on every
  stored observation (§48).
- Feature schema pinning (§58) wired into the existing
  `lib/predictive/schemas.py`.
- `BookHealth` contract and mandatory abstention while `valid == False` (§9),
  added to `lib/orderbook_stream.py`.

### P2 — Raw event storage behind a storage abstraction

- Canonical event dataclasses (§6): trade, quote, book delta, derivatives
  observation, on-chain event.
- Bounded queues and backpressure (§10); drop counts per source; never silently
  pretend dropped data was complete.
- Storage interface with a SQLite implementation first, ClickHouse behind the
  same interface.
- Instrument bytes/day on one Tier-1 symbol (§46). **Migration decision is
  made on that measurement, not in advance.**
- Watchlist tiers (§47) so Tier 3 never pays L2 storage cost.

### P3 — Provider adapters, extending existing modules

Alpaca SIP + OPRA, Kraken, Coinbase, Binance.US, Bitquery, CoinGlass. One
adapter per provider, provider-specific parsing stays inside the adapter,
normalization into the P2 canonical events. Provider health per §44 exposed to
the existing Ops UI.

Double-counting guard (§50): aggregate vs venue-specific sources tagged, never
summed as independent evidence.

### P4 — Feature snapshots and label scheduler

Event-driven as well as clock-driven (§52). Label scheduler resolving each
horizon independently (§57). Quality flags and abstention (§43).

### P5 — Re-run the path model

With P0 candidates, P2 raw history, and — if the Alpaca entitlement delivers
intraday depth — years of calendar span rather than one day. This is the point
at which Phase 4 gets an honest second evaluation.

### P6 onward — unchanged from the predictive guide

Outcome model, state encoder and analogs, residual learner, drift, meta-filter,
then the execution model *last*, only after live fills accumulate (§28, §61
Phase L). Slippage capture began at commit `bf6c4c2` today; that clock has
started.

---

## Non-negotiables carried forward

Unchanged from §3, §64, and already enforced in code:

- ML and LLM never authoritative for fees, P&L, R, liquidation, position size,
  leverage, hard risk limits, kill switches, indicator arithmetic, or strategy
  identity.
- A deterministic `NO_TRADE` cannot be turned into `TRADE` by any model.
- No random splits on time series; walk-forward or nothing.
- Normalizers fit on training data only.
- Unavailable prediction means abstain, never a favorable default.
- Never train on unversioned features.
- Champion artifacts never overwritten in place.

---

## Immediate next actions

1. **Verify Alpaca entitlement depth at 5m/15m**, not just daily. This single
   fact decides whether P5 happens in days or months.
2. **Build P0.** It requires no subscription, no new data, and unblocks the
   highest-value model class. It is the correct thing to do while provider
   accounts are being set up.
3. Confirm current pricing and quotas per §0's own instruction before
   committing spend — the document explicitly declines to guarantee its numbers.
