# J.A.R.V.I.S. — Architecture

FastAPI backend, Svelte 5 frontend, SQLite storage, APScheduler jobs.
One operator, one machine, real venue connections (Alpaca paper account,
Kraken read-only). This document maps the system's four planes and the
invariants that hold them together. File references are the source of
truth; when this document and the code disagree, the code and its tests
win, and this file has a bug.

The design temperament, in one line: **measure before believing, abstain
before guessing, and record what was actually done rather than what was
intended.** Nearly every module below exists because some earlier number
was silently wrong — the multiplier that overstated futures P&L 25,000x,
the composite score that selected losers at its top band, the R-multiple
divided by a trailed stop. The architecture is the accumulated defense.

---

## 1. Data plane — facts, with provenance

### Canonical events (`lib/market_events.py`)
Every market observation becomes a frozen dataclass with an `EventMeta`
carrying **three clocks**: `exchange_ts` (the venue's own stamp — None
when the venue sends none, never fetch-time in disguise), `ingest_ts`
(when we saw it), `process_ts` (when we used it). `clock_skew_ms` is
derived, and paid for itself on day one: it exposed a 2-second local
clock drift that had silently biased every staleness check (fixed via
`w32tm`; skew now ~±70ms of network latency).

Event kinds: `book_snapshot`, `trade`, `quote`, `derivatives`
(funding/OI/long-short), `official_stat` (COT/FINRA/EIA), and
`curve_snapshot`. Each carries `source_schema_version` — a feed's
meaning may never silently change (new parse = new version string).

### Event store (`lib/event_store.py`)
Append-only SQLite (`data/events.db`), separate from the operator DB.
Payloads are JSON with byte counts, so `bytes/day by kind` is a measured
number, not an estimate — the §46 rule: no storage migration until the
measured volume justifies one. `dedup_key` (unique partial index +
INSERT OR IGNORE) makes official-release re-syncs idempotent at the
storage layer; `append()` reports rows actually inserted, so a deduped
replay says 0, not a flattering `len(events)`.

**Watchlist tiers** gate persistence cost: Tier 1 (BTC, ETH) persists
book snapshots on a 5s cadence, trades per print, quotes throttled;
everything else stays in-memory only until it earns promotion.

### Adapters (provider parsing stays inside; canonical shapes leave)
- `lib/orderbook_stream.py` — Binance.US + Coinbase L2 books over WS,
  with `BookHealth` (crossed-book/staleness/sequence-gap abstention) and
  bounded drop-counting queues: **backpressure is a counted decision,
  never a silent loss**. One flusher drains every registered queue.
- `lib/kraken_stream.py` — Kraken v2 tape + quotes; trade events carry
  the venue's own timestamp (the first stream where skew is measurable).
- `lib/crypto_derivatives.py` — OKX + Crypto.com funding/OI/long-short;
  emission rides the existing fetch paths, throttled per
  (venue, symbol, metric). Funding intervals differ (1h vs 8h) and
  `funding_dispersion()` normalizes before comparing.
- `lib/official_data.py` — CFTC COT (nine markets incl. CME Bitcoin and
  Ether), FINRA daily short volume (universe = equities the desk
  actually considered recently), EIA storage (key-gated, reports its own
  absence). **`as_of` is what a stat describes; `exchange_ts` is when it
  became public.** COT differs by three days; joining on the wrong one
  hands replay a crystal ball. Release stamps are biased late on
  purpose: the join may believe data arrived later than it did, never
  earlier.
- `lib/futures_curve.py` — per-contract quotes (verified live via
  Yahoo's per-contract tickers) snapshotted into term structure with
  the front identified by the tradability rule, not by volume.

### Parity instrument (`lib/feed_parity.py`)
Two venues measuring one market should agree. Cross-venue median
mid-price gap in bps, bucketed and thresholded, serving the standing
rule from the plan: **per-venue adapter promotion on measured parity
only** — this instrument is the gate any replacement feed (cryptofeed
included, once its Windows build is resolved — see
`vendor/patches/`) must pass. Live baseline: coinbase↔kraken ~2 bps;
binance.US carries ~11 bps of thin-book basis.

---

## 2. Learning plane — outcomes, honestly attributed

### Candidates and counterfactuals (`lib/candidates.py`)
Every considered setup — accepted or rejected — is recorded at the
moment of judgment, deduped, then **counterfactually resolved** by the
same replay machinery that labels real outcomes (same stop-first
conservatism, same fee model). `selection_bias_summary()` answers the
forbidden question directly: are the filters discarding winners? Rows
are grouped by verdict, by rejection reason, and by the live gate's own
decision — if NO_TRADE resolves better than TRADE out of sample, the
gate is wrong and that row is where it shows first.

### The gate experiment (`lib/gate.py`)
Measured 2026-08-13 on 11k+ labelled outcomes: the composite score was
**monotonically inverted** — the 80+ band won 30% while the <60 band won
53%. Mechanism: the largest weight (ta_confluence, 0.20) sat on the
worst-measured component; the healthiest component (conflict ratio) was
penalized. "Every timeframe agrees" is what the END of a move looks
like.

Response: two gates run side by side on every candidate. `gate_legacy`
(byte-compatible with the old threshold) records what the old world
would have done; `gate_v8` (validity + measured expectancy + Wilson
lower bound → TRADE/TENTATIVE/NO_TRADE/UNKNOWN) decides capital.
Verdicts are immutable at candidate birth; the same resolver judges both
arms. Judgment window: ≥2 weeks or ≥300 resolved candidates per arm
(from 2026-08-14). Revert path: the `pre-hardening-baseline` tag.

### Measured expectancy (`lib/expectancy.py`, `lib/strategy_lifecycle.py`)
R-multiples are computed against the **stop as placed**
(`initial_stop_loss`, immutable at position open), falling back to the
signal's proposal only when no placement exists — the ledger measures
risk actually taken. Replayed outcomes weigh 0.5 against live fills
everywhere (expectancy, calibration, lifecycle), and every summary
surfaces `sample_live`/`sample_replay` so no verdict hides what it
stands on.

### Shadow scoring and promotion (`lib/score_variants.py`, `lib/promotion.py`)
Score variants (B = inverted diagnostic, C = component-recalibrated,
MS = market-state-only) are computed at candidate birth and stored —
never recomputed in hindsight. The §4.3 promotion framework judges any
challenger on resolved candidates that postdate its definition:
chronological folds, calendar span, after-cost net-R improvement, tail
not worse beyond tolerance, selection frequency reported, leakage
structurally impossible (stored-at-birth only). Champions are immutable
versioned rows; **a challenger that wins in-sample does not replace
production.**

### Feature corpus (`lib/feature_snapshots.py`, `lib/predictive/`)
Clock-driven snapshots every 15 minutes for Tier-1 symbols — the same
schema-hashed vector whether or not anything looked interesting, because
a corpus born only from moments the system chose answers "what happens
after we get interested?", not "what happens?". Labels are scheduled at
snapshot birth (1h/4h/1d) and resolve independently on their own
clocks; thin coverage abstains with its measured coverage as the reason.

The path model's null result is a recorded finding, not a failure:
527k examples of confirmation-TA features carry no exploitable path
signal (stop-first AUC ≈ 0.5 against honest baselines). The frontier is
inputs, not architecture — which is why the data plane above exists.

---

## 3. Decision plane — one authority per question

### Typed pipeline (`lib/decision_types.py`)
`ObservedEvidence → MeasuredEdge → RiskDecision → OrderPlan →
TradeDecision`, all frozen dataclasses. Strict side parsing
(`lib/trade_side.py`) — an unparseable direction is a rejection, never a
default to Long. `OrderPlan.within(risk)` is checked before submit;
violation blocks the order.

### Risk engine (`lib/risk_engine.py`)
The single sizing authority: validate → freeze the stop (market
structure, never adjusted to enable leverage) → qty = budget/risk-per-
unit → constraints only shrink → leverage derived from the stop distance
with a liquidation buffer (`LIQ_STOP_BUFFER=0.80`) → revalidate that
worst-case loss ≤ budget. No rounding inside the authority. Futures size
in whole contracts with exchange dollar margins; Kelly sizing draws only
on measured evidence (lower-bound win rate, quarter-Kelly). Cash
accounts pin 1x; `MAX_LEVERAGE=25.0` matches the operator's target
broker range.

### Identity and calendars
- `lib/instruments.py` — `canonical()`/`variants()`/`asset_class_of()`:
  one authority for "which instrument is this string" (the LINK incident:
  positions as `LINKUSD`, orders as `LINK/USD`, and a protective stop
  nearly cancelled). Contract specs are registry data — multiplier,
  tick, per-contract commission, exchange margin.
- `lib/futures_contracts.py` — root vs contract vs continuous. A
  continuous series is an analytical convenience; the thing held is a
  specific contract with a last-trade date and, for physicals, a
  first-notice date. `delivery_risk()` hard-blocks entries near the risk
  date and the front contract is the nearest one still *tradable*, not
  the highest-volume one. Unknown calendars fail closed.
- `lib/market_clock.py` — equity market hours from Alpaca's clock
  endpoint, cached, failing closed. Replaced five hard-coded UTC checks
  that would all have broken on the November DST shift.

### Venue routing
Alpaca automates equities and its 73 listed crypto pairs; everything
else crypto is paper-only via `crypto_requires_paper` (a failed listing
lookup is never a paper-flip — fail closed). Kraken modules are
read-only and test-pinned: no order-placement call exists, and a test
fails if one appears. Live Kraken trading would be a separately-keyed
explicit decision, not a config flip.

---

## 4. Ops plane — the system watches itself

- **Scheduler** (`app/scheduler.py`): every job self-seeds its status
  row; a static test walks the source and fails if a registered job
  isn't seeded (the 'candidates' job once KeyError'd invisibly on every
  firing for two server processes).
- **Hermetic tests** (`conftest.py`): both databases redirect to temp
  files before any app import; touching the operator DB requires an
  explicit env grant. 1,650+ tests; the suite IS the spec.
- **CI** (`.github/workflows/ci.yml`): Ubuntu, keyless, every push.
  Failure tails post as commit comments readable without login. Local
  Linux truth: a WSL venv reproduces the CI run in ~42s.
- **Data platform panel** (Ops UI): stream health, queue drops (shown
  only when nonzero — silence is health), store bytes/day by kind,
  parity verdicts, feature-corpus maturity.
- **Kill switch**: pauses all new live orders; existing positions keep
  their protective stops. Manual-trade margin instructions are honored
  margin-first; sizing rejections are errors, never silent flat entries.

---

## 5. Standing experiment governance

Two measurement windows are open and must not be contaminated:

1. **Gate experiment** — composite composition stays frozen until the
   window closes (≥2 weeks or ≥300 resolved per arm from 2026-08-14).
   New scoring ideas enter as shadow variants, never as edits to the
   live formula mid-window.
2. **Promotion framework** — challengers are judged only on candidates
   created after their definition. Backfilling a variant's history is
   leakage by construction and the API makes it impossible (variants are
   stored at birth, evaluation reads stored values only).

Restore point: tag `pre-hardening-baseline` (with revert commands in its
annotation) rolls the system back to the pre-experiment state if the
upgrades measure out flat.
