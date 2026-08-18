# JARVIS TRADING — canonical handoff

**This project is JARVIS TRADING. It is not EIDOS. Do not rename it.**

Read this file first. It is the single durable state document; do not create
competing handoff files.

    repository   jmtibbetts/jarvis-trading
    runtime      Windows 11 host -> WSL2 Ubuntu 24.04
    active repo  /home/nullcode/jarvis-trading   (the Windows checkout at
                 C:\jarvis-trading-ai-python is a DEAD rollback copy — never
                 work there, never push from it)
    interpreter  .venv/bin/python   (never bare python3)
    remote       `origin` in the WSL tree; a bare `git push` is correct

    HEAD         see §10 — Evidence Phase B landed on top of 7092d47
    scheduler    OFF — do not restart without explicit operator approval
    platform     VIRTUAL_ONLY

---

## 1. What JARVIS is

    REAL market data + REAL analysis + VIRTUAL execution + FAKE money
    + REAL learning from forward observation

    read the real world      YES
    analyse it               YES
    learn from it            YES
    simulate execution       YES

    change a real account    NO
    send a real order        NO
    transfer funds           NO

**THE GOLDEN RULE: the bot must never make money because the simulator is
wrong.** Every defect below was a violation of it.

---

## 2. Operator state — do not disturb

    operator DB      data/jarvis.db   UNCHANGED
                     21,194 trade_outcomes
                     667 legacy open positions
                     cash $63,550.84
                     `decision_observations` table ABSENT from it (nothing
                     has initialised it there yet — expected)
    real actions     Kraken orders 0 · Alpaca orders 0 · Bitnomial orders 0
                     · transfers 0

The 667 legacy positions stay untouched until the canonical-epoch archive
step. Never run tests or ad-hoc probes against the operator DB — always
export `JARVIS_DB_PATH` to a temp path first.

---

## 3. The canonical chain — do not create parallel types

    ObservedEvidence -> TradeDecision -> RiskDecision -> OrderPlan
      -> ExecutionVenue -> VirtualCexAdapter -> ExecutionResult
      -> Settlement -> RealizedOutcome -> Learning

Four identities stay separate and are never collapsed:
**asset_class · product · venue · instrument**.

**PRODUCT IS NEVER INFERRED FROM LEVERAGE.** A 1x perpetual is still
`CRYPTO_PERP`.

Execution authority today:

| product | authority |
|---|---|
| CRYPTO_SPOT | Kraken spot book (`wss://ws.kraken.com/v2`) |
| CRYPTO_PERP (US) | Bitnomial public perpetual book |
| EQUITY | Alpaca read-only quote |
| FUTURES / FOREX | none — **fail closed** |

Cross-venue evidence may inform analysis. It may never silently become
target-product execution authority.

---

## 4. Completed work

### Canonical entry (A1–A10) — COMPLETE
- **A3/A4** — the venue was handed a nominal 1 unit and sizing happened
  after; attribution was off by the whole position size. Order is now
  signal → sizing → authorized qty → execute → fill → settle.
  `open_paper_position` split into `prepare_entry` + `settle_position_entry`.
- **A5** — entry called `virtual_orders.execute_market()` directly, skipping
  all three boundary gates. Now `OrderPlan → execution_venue.submit → …`.
  AST-guarded.
- **A9** — `product` held `"crypto"`, an asset class. Vocabulary now lives in
  `lib/product_router`.
- **A8** — `futures_fee_for()` returns None under `VENUE_REGION=us` and
  execution fell into the SPOT schedule: 0.25%/side vs 0.05%/side, a **5x
  overcharge**. `lib/fee_authority.py` returns DOLLARS, never
  percentage-shaped. **Never reintroduce a perp → spot fee fallback.**
- **A2** — entry leg priced at the executed size and debited at entry;
  `PaperPosition.fees` stays 0 so the exit cannot double-charge.
  `cost_model = per_leg_v2`.
- **A10** — perp fills were priced off the SPOT book. Fixed by A10.1.

### Bitnomial US perpetual book (A10.1) — COMPLETE
- Public, unauthenticated: `wss://bitnomial.com/exchange/ws`,
  REST `https://bitnomial.com/exchange/api/v1/prod`.
- 17 active perpetuals discovered; all 17 contract sizes matched the
  hand-written `US_PERP_CONTRACTS` registry.
- **Book semantics (verified live):** asks ascend, bids descend, index 0 is
  best; `quantity == 0` REMOVES a level; **one `ack_id` can span several
  `level` messages — equal ids are one atomic batch and must ALL be applied,
  never discarded as duplicates.**
- Sequence integrity: a gap/reconnect/malformed message INVALIDATES the book
  (`BOOK_DESYNCED`) until a fresh snapshot.
- **Freshness: perp 15s, spot 10s.** Measured over 90s / 4 products /
  n=4,363: median 0.00s, p95 0.39s, p99 1.43s, **max 11.12s** (a genuine
  quiet period). Do not tighten below observed quiet periods.
- **Price scale: 16/17 verified** as `price_usd = raw × price_increment`.
  **SHIB is the exception** — the published increment implies ~$0.00446
  against an observed ~$0.00000447, a ~1000x gap with no published
  explanation. SHIB is `UNVERIFIED_PRICE_SCALE` and **fails closed**.
  **Never divide by 1000 because it looks right.**
- Depth is RECORDED but NOT consumed; execution is top-of-book. Do not claim
  depth-aware impact until it is built.

### Decision observation (Phase A + A.1 + A.1.1) — COMPLETE
`decision_observations` — **every material decision is an observation**,
traded or refused. Immutable T0 audit trail; not a second opinion.

- Identity anchor priority: explicit `market_event_id` → `candidate_id` →
  signal's own timestamp → venue event time → wall-clock, the last recorded
  as `UNSTABLE_WALL_CLOCK`. Same event/retry → same `observation_id`; a new
  evaluation → a new one. Arms of one event share identity.
- Decision (`TRADE`/`NO_TRADE`/`ABSTAIN`) is separate from execution
  lifecycle (`NOT_APPLICABLE`/`SIMULATED_FILLED`/`SETTLED`/
  `SETTLEMENT_FAILED`). **The lifecycle is monotonic; terminal is terminal.**
- **A.1.1 atomic guarantee:** position creation, margin debit, entry fee,
  execution provenance AND observation linkage (execution_id, position_id,
  SETTLED, settlement_at) all commit in ONE transaction, or everything rolls
  back. Conflicting execution_id/position_id raises `LifecycleConflict`.
- **An accepted canonical trade fails closed** if its T0 observation cannot
  be persisted (`DECISION_OBSERVATION_PERSIST_FAILED`) — no position, no
  margin, no fee.
- Execution-calibration eligibility is a **predicate**, not source
  membership: forward executed source AND decision TRADE AND execution_id
  AND `SETTLED` AND position_id.
- `risk_budget_usd` is deliberately **NULL** — `size_position` overwrites the
  pre-solve budget with `decision.margin`, so storing it would be a
  mislabel. `authorized_risk_usd` carries the real approved money-at-stop.

**Field coverage:** 62 populated from T0 authority; 15 legitimately null
today (`spread_cost_r`, `slippage_cost_r`, `fee_cost_r`, `funding_cost_r`,
`borrow_cost_r`, `expected_move_pct`, `liquidation_price`,
`concentration_after_pct`, `deployment_after_pct`, `expected_hold_hours`,
`decision_id`, `contributing_reasons`, `risk_budget_usd`, `settlement_at`,
`settlement_failure_reason`). The cost breakdown exists in `estimate_costs`
but is not threaded into canonical entry — real wiring, not null-filling.

---

## 5. Invariants — do not weaken

- **Committed margin is not a loss.** At entry free cash falls by margin +
  fee, but economic equity falls by **the fee alone**.
  `equity = free_cash + committed_margin + unrealized_pnl + …`
- **Per-leg costs.** Entry fee charged once at entry; `fees` stays 0 on
  canonical positions. Spread/slippage/impact live INSIDE the fill price —
  never subtract them again.
- **Catastrophic product gate ≠ trade expectancy ≠ risk.** Three different
  questions. The structural product test uses `fee / notional` or
  `per-contract fee / contract value` — **never `fee / margin`.**
- **Cheap-coin sanity (permanent):** `0.043241 → 0.044241` ≈ **+2.3126%**.
  At $1,000 margin / 20x / ~$20,000 exposure that is ≈ **+$462** gross. A
  small dollar delta is not a small economic move. Leverage applies ONCE.
- **Whole contracts FLOOR to the authorization**, never ceil into more
  exposure. The conservative ceil-ing helper is planning-only and is
  AST-asserted never to be used as an executable quantity.
- **A refusal is not a loss.** Venue/data/capability refusals must never
  land against the thesis.

---

## 6. Research findings — preserve, do not re-litigate

**Historical counterfactual replay is deliberately DEFERRED.** Do not
resurrect it.

    population                     11,952 historical cost-gate rejections
    old modelled median cost       ~0.373R
    corrected median cost          ~0.217R    <- old economics WERE wrong
    judgeable (forward outcome)    177  of 11,952
    unjudgeable                    11,775
    old NO -> new YES              2   (INJ/USD TARGET, OP/USD STOP)

One win / one loss is noise. The blocker is **data absence**, not analysis
difficulty: no historical Bitnomial book was ever stored.

    median gross edge  ~0.109R
    max gross edge     ~0.419R
    modelled ROUND-TRIP COST  ~0.50R      <- A COST, NOT A THRESHOLD

**CORRECTED 2026-08-18 (C0.2). There is no 0.50R threshold and there never
was.** Earlier revisions of this file called ~0.50R the "edge threshold" and
said not to lower it. That was a mislabel, and it invited someone to tune a
number that does not exist. ~0.50R is the MODELLED ROUND-TRIP COST in R
(`edge_cost_matrix` docstring: `gross 0.42R - cost 0.50R = -0.08R`; the
replay report: "near 0.42R against a cost floor around 0.50R").

So the historical finding is: **the best available gross edge (~0.42R) was
smaller than the cost of trading it (~0.50R)** — the venue ate the setup.
That is a COST-limited result, and `edge_cost_matrix` keeps `LIMIT_COST` and
`LIMIT_EDGE` as distinct verdicts precisely so the two are never conflated.

The ACTUAL live gate is a NET-expectancy floor applied AFTER costs:

    lib.expectancy.MIN_NET_R        = 0.05R   <- the live authority
    lib.edge_cost_matrix.MIN_NET_R  = 0.05R   (same bar, shared on purpose)
    lib.dex_autotrade.MIN_NET_R     = 0.05R   (separate DEX constant)

`expectancy.evaluate` returns NO_TRADE when `net_expected_r < 0.05`, and
`robust` additionally requires the LOWER CONFIDENCE BOUND to clear the same
0.05 — so a point pass with a failing lower bound is an UNCERTAINTY result,
not a robust TRADE. Never write "the threshold" again: name it
`expectancy_min_net_r` or `historical_modelled_round_trip_cost_r`.

Preliminary reports (gitignored, do not delete):
`data/reports/perp_false_negative_replay_20260818T012836Z.{json,md}`

---

## 7. Test / CI state

    online   3,288 passed / 16 skipped   exit code 0
    offline  identical                   exit code 0
    CI       run 32093026804 — all five green

    SKIP CATEGORIES  EXTERNAL_INTEGRATION 3/4 · OPTIONAL_HARDWARE 1/1
                     REAL_PROVIDER_READ_ONLY 12/12 · undeclared 0

**ALWAYS capture the real process exit status.** `conftest`'s
`pytest_sessionfinish` sets `exitstatus = 1` for undeclared skips, so
"3,288 passed" can sit directly above a failing run. This cost two red CI
runs already.

**Shell trap:** `wsl.exe -- bash -lc '... ; echo $?'` expands `$?` in the
OUTER shell and printed a false `EXIT: 0` next to 7 failures. Use a literal
heredoc (`bash -s <<'EOF'`) and capture the code inside.

Hermetic policy: core CI never touches the internet. `tests/kraken_twin.py`
serves `lib.venues` HTTP from captured payloads via an autouse fixture; an
unknown URL raises by name. Real provider checks are classified
`REAL_PROVIDER_READ_ONLY` and skipped unless `JARVIS_REAL_PROVIDER_TESTS=1`.

Performance baseline (early, re-measure at scale): observation write median
1.42ms / p95 2.77ms; indexed decision query 2.25ms; **~750 bytes marginal
per row** (empty schema baseline is 1.15MB, so a naive divide overstates it)
→ ~0.45MB/day, ~165MB/year at 600 decisions/day.

---

## 8. Remaining roadmap — IN ORDER

### Evidence Phase B — forward outcome observer  **LARGELY LANDED**
**Read §10 first** — what shipped, what is measured, what is still open.
The specification below is kept because it is still the contract.

Turn observations into the evidence the old system lost.
- Audit existing structures first (`SignalEvaluation`, `CandidateSignal`,
  shadow/control, execution samples, events.db, OHLCV cache) before adding
  `decision_observation_outcomes`.
- Key: `observation_id + horizon`, unique. `due_at = decision_at + horizon`
  (never observer wake-up). Indexed due-query, bounded batch.
- Product-correct forward pricing: perp → Bitnomial, spot → Kraken,
  equity → Alpaca. Never score a perp from spot.
- **HARD INVARIANT: MFE/MAE cannot come from T0 + one endpoint quote.**
  They need interval evidence. Absent it: NULL with
  `INSUFFICIENT_RANGE_DATA`. **Zero is not missing.**
- Needs a SHARED per-instrument range collector — one BTC stream serving
  many pending observations, not a book copy per decision. Persist
  sample_count, max_sample_gap, first/last sample; a restart gap must LOWER
  quality, never be hidden.
- Side-aware close reference: LONG → bid, SHORT → ask. Midpoint is market
  direction, **not an executable exit**.
- Touch ordering: `TARGET_FIRST` / `STOP_FIRST` / `AMBIGUOUS_INTRABAR`.
  Never pick the profitable ordering.
- Observer must create no PaperPosition/PaperTrade/RealizedOutcome, move no
  cash, touch no counters. Rejected evidence never enters fill/slippage
  calibration or portfolio P&L.
- ~40 tests specified in the operator prompt.

### Evidence Phase C — decision quality analytics  NOT STARTED
Counts by decision / binding_constraint / binding_reason / product /
timeframe / leverage / price band. **`FAVORABLE_AFTER_REJECTION` is not
`FALSE_NEGATIVE`.** Threshold research (0.20R…0.60R) uses **stored T0
values only** — never rerun modern model code and call it what JARVIS knew.
Every headline stat shows numerator / usable denominator / total. No policy
change.

### Execution Pass B — canonical exits  NOT STARTED
`trigger → exit qty authorization → OrderPlan → ExecutionVenue →
VirtualCexAdapter → ExecutionResult → exit-leg fee → funding → atomic
settlement → PositionSettlement → ONE RealizedOutcome`.
Mark may TRIGGER, mark may not FILL. Perp long close crosses the Bitnomial
**bid**, short close the **ask**. Partial exits: proportional margin
release, funding for that quantity over that interval (the known legacy
defect), exit fee once, and **partials produce no final learning vote — one
position votes once.** Cash identity:
`final = initial + Σgross − entry fee − Σexit fees − Σfunding`.
The fail-closed legacy-close guard is removed by BUILDING the exit, never by
weakening it.

### Canonical epoch preparation  NOT STARTED
Archive the operator DB immutably (checksum + integrity + verified counts);
preserve `events.db` and `ohlcv_cache.db`; build a NEW active DB with zero
positions and a new engine epoch; explicit COPY/RESET/ARCHIVE_ONLY manifest
per table. Never delete or mutate the old DB in place.

### Controlled activation  NOT STARTED
Final state is **READY_FOR_CONTROLLED_ACTIVATION**, not running. The
scheduler stays OFF pending explicit operator approval.

---

## 9. Working rules

- Stop only at clean / tested / pushed / **CI-green** boundaries. Do not
  half-build a phase.
- Update this file **before** stopping for context, not after.
- Provider reads stay OUTSIDE DB write transactions; mutations are short and
  atomic.
- Python owns deterministic math (P&L, risk, fees, sizing, liquidation,
  contract arithmetic, statistics). The LLM owns synthesis and narrative —
  never canonical arithmetic.
- No new live-trading capability. JARVIS stays virtual-first.


---

## 10. Evidence Phase B — landed 2026-08-18

    52b348a  B1-B3  read-only market-data runtime + Bitnomial stream lifecycle
    e422c06  B4-B8  shared quote samples + forward outcome observer

    online   3,341 passed / 16 skipped   TRUE exit 0
    offline  identical (unshare -rn)     TRUE exit 0
    skips    UNCHANGED 3/4 + 1/1 + 12/12, undeclared 0 — no budget was touched

### THE DISCOVERY THAT REORDERED THE PHASE

`bitnomial_market_data.start_stream()` **had no runtime caller anywhere.**
The provider, its book semantics and its verified price scales were all
implemented and tested; the stream was simply never switched on. So the
perpetual books were permanently empty in production and every CRYPTO_PERP
quote refused for want of data nothing was collecting. Confirmed alongside
it: **events.db and ohlcv_cache.db contain no Bitnomial history at all**
(OHLCV sources are twelvedata/alpaca/okx/mexc/kraken/kucoin/coinbase/
coingecko — all spot or generic). There was no path to perp forward evidence
until the feed had an owner.

**SCHEDULER OFF ≠ MARKET DATA OFF.** `lib/market_data_runtime` owns
read-only feeds and `main.py` starts it OUTSIDE the scheduler branch, so
this state is now expressible and is the intended one:

    autonomous trading scheduler   OFF
    Bitnomial public market data   ON (when the runtime is enabled)
    real orders / account changes  STRUCTURALLY IMPOSSIBLE

Feeds have their own switch, `JARVIS_DISABLE_MARKET_DATA`, named for what it
controls. **conftest forces it to 1** — the scheduler pin does not cover it,
precisely because the runtime is independent of the scheduler.

### EXISTING-STRUCTURE AUDIT (done before anything was built)

| structure | why it could not carry Phase B |
|---|---|
| `signal_evaluations` | signal-keyed, ONE terminal evaluation not per-horizon, generic OHLCV, and it writes `mfe_pct=0.0` on bad data — zero-for-missing |
| `feature_labels` | **the right PATTERN and it was reused**: per-horizon rows, due_at from birth, abstain-with-reason, grace. But clock-driven BTC/ETH only, bar-based, no product authority |
| `candidate_signals` | scoring stage, resolved once; the population Phase A already superseded |
| `execution_samples` | orders actually sent — a NO_TRADE never appears |
| `events.db` | append-only JSON; deriving extrema means parsing every row, and it holds no Bitnomial |
| `ohlcv_cache.db` | 37.9M bars but zero perp — cannot price a CRYPTO_PERP outcome |
| `execution_snapshot` | **REUSED as the sole market face** — already refuses perp→spot and already fails SHIB closed |

### SHAPE CHOSEN, AND THE ONE I REJECTED

First draft bucketed per-minute high/low. **Rejected**: buckets prove a level
was reached but not WHICH came first, and that ordering is the whole
difference between a win and a loss. It cannot be precomputed either —
every observation carries its own stop/target, so a shared market row cannot
know which levels will matter to a decision not yet made. Storage is
`instrument_quote_samples`: SHARED, timestamped, keyed by instrument+time.

    shared, 20 instruments    ~38 MB/day    ~13.9 GB/year
    naive per-decision, 600/d               ~416 GB/year      30x worse

**Sampling is change-triggered with a HEARTBEAT FLOOR (30s).** This is what
makes a quiet market distinguishable from a dead feed: calm-but-healthy keeps
emitting rows, a disconnect emits none, so only real downtime becomes
`GAP_PRESENT`. Gap arithmetic measures between-sample holes AND both edges,
so a late start or early stop cannot masquerade as full coverage.

### INVARIANTS NOW ENFORCED BY TEST

- **MFE/MAE never from endpoints.** No interval evidence → NULL +
  `INSUFFICIENT_RANGE_DATA`. Zero is not missing.
- **Side-aware**: long measured on the bid, short on the ask; midpoint is
  direction, never an executable exit.
- **AMBIGUOUS_INTRABAR is narrow and real.** One sample carries one price, so
  no sample straddles a decision — the earlier crossing genuinely came first
  *provided we were watching*. Ambiguity lives only in a blind interval
  before the first crossing. The profitable ordering is never chosen.
- **Perp never scored from spot** — resolves INSUFFICIENT_DATA instead.
- **Observer mutates nothing**: no position, trade, cash, counter or learning
  vote. AST-proven it cannot import the execution surface.
- Lifecycle monotonic, PENDING the only non-terminal state; retry idempotent.

### TWO DEFECTS FOUND WHILE BUILDING — both would have failed SILENTLY

1. Resolution returned ORM rows across a closed session, so every horizon
   raised `DetachedInstanceError` into a caught warning and sat PENDING
   forever. The evidence layer would have looked healthy and collected
   nothing. Data is now detached at the boundary.
2. `SessionLocal` is **autoflush=False**, so rows added by
   `schedule_for_observation` were invisible to any caller sharing the
   transaction. Now flushed explicitly.

### MIGRATION SAFETY

Proven on a COPY of the real 402MB schema, never the operator DB:
21,194 outcomes / 667 positions / cash $63,550.8371643338 **byte-identical
before and after**, `integrity_check ok`, `init_db()` idempotent on rerun.
New tables arrive via `create_all`; the operator DB still does not have them.

### B9 — LIVE VENUE MEASUREMENT (2026-08-18) — the assumption was 20x wrong

Deliberate `REAL_PROVIDER_READ_ONLY` session, 900s, disposable DB, operator
DB never opened. Reports (gitignored) in `data/reports/`:
`bitnomial_market_data_measurement_*`, `evidence_storage_measurement_*`,
`bitnomial_recovery_verification_*`.

    active products      16   (17 discovered; SHIB fails closed LIVE on its
                         unverified price scale — the guard holds in reality)
    provider messages    173,617  = 192.9/s  (level 172,032 · book 1,568
                         snapshots · status 16 · trade 1)
    top-of-book changes   34,625  =  38.5/s  = 144 per product per minute
    rows persisted        26,328  =  29.3/s  = 110 per product per minute
                         99.7% change-triggered · 0.3% heartbeat
    reconnects 0 · stale 0 · desynced 0 · all 16 books healthy

**The sampler had assumed ~6 changes per product per minute. The venue does
~144.** A 1Hz polling collector persisted only ~15/product/min — about 13% of
real book movement — and the missing 87% is exactly what MFE/MAE and touch
chronology are made of. **The collector is now EVENT-DRIVEN off the same
ingest** (`add_book_listener` -> bridge -> `note_quote` -> buffered writer),
routed through `execution_market_snapshot` so it inherits the perp/spot
refusal and the SHIB fail-closed rather than reimplementing them.

Persisted rows sit below top-of-book changes because a SIZE-only move is not
a price move — correct, not lossy.

**Measurement artefact found and fixed:** the snapshot API takes a DESK
symbol while `active_symbols()` returns venue product codes; passing one for
the other refused all 16 with NO_BITNOMIAL_PRODUCT.

### SQLITE COST AND QUERY SPEED — measured at 300k rows

    bytes/row        353.3   (assumed 220 — 60% under)
    table / index    64.8MB / 43.1MB  -> 66% index overhead
    insert           ~83,500 rows/s
    range queries    ALL use ix_quote_sample_window
                     1m 0.05ms · 1h 0.42ms · 4h 1.5ms · 1d 9.9ms (p50)
    chronological 1h scan (the touch-order query)   ~1ms

    => ~2.5M rows/day · ~0.89 GB/day · **~326 GB/year** for all 16 perps

### RECOVERY, PROVEN AGAINST THE LIVE VENUE

One deliberate local socket close (`force_disconnect`, this side only):

    disconnect            04:54:45.192
    books unusable        04:54:45.693   (+0.5s)
    reconnected+resubscribed+fresh snapshot+AVAILABLE   04:54:48.693
    blind interval        3.5s · reconnect_count 0->1 · 16/16 resubscribed

    PASS  stale book NOT executable after the drop
    PASS  reconnected and resubscribed
    PASS  quiet-but-healthy NOT flagged GAP_PRESENT
    PASS  outage window degraded (INSUFFICIENT_RANGE_DATA, 12.8s gap)

Note: a quiet healthy window can read `PARTIAL` (few changes, 30s heartbeat
leaves edges unattested). That is honest and is NOT `GAP_PRESENT`, which is
the distinction that matters.

### RETENTION DECISION — ARCHIVE, NEVER DELETE

~326 GB/year against ~8 TB NVMe + ~60 TB SATA is CASE 1 (<1 TB/year), about
4% of one drive. `lib/evidence_retention.py`:

    RAW_RETENTION_DAYS = None      DELETE_ENABLED = False   (test-enforced)

No pruning job. No 30-day delete. No compaction job. No storage-engine
migration — SQLite has two orders of magnitude of write headroom and
single-digit-ms queries, so replacing it now would be rebuilding the database
stack instead of capturing evidence. Warm 1-minute aggregates remain a
QUERY-SPEED option for later and never replace raw chronology.

**These rows are the only target-product history that has ever existed for
these instruments.** The old system lost 11,775 rejected-candidate outcomes
by discarding evidence before anyone asked; a 30-day rule reproduces that on
a timer.

### OBSERVER RUNTIME OWNERSHIP — RESOLVED

`lib/evidence_runtime.py`, started from `main.py` lifespan alongside the
market-data runtime and OUTSIDE the scheduler branch. A decision made at
09:00 with a 4-hour horizon comes due at 13:00 whether or not JARVIS is
trading; registering this on APScheduler would make the safe half of the
system depend on the half under review. Bounded batches (500), contained
exceptions, idempotent start, clean stop, and health that reports the honest
backlog signal — the OLDEST OVERDUE horizon, not just a pending count. AST
test proves it cannot import the execution surface.
`JARVIS_DISABLE_EVIDENCE_RUNTIME=1` in conftest so it never races tests.

### PHASE B IS COMPLETE

    online   3,363 passed / 16 skipped   TRUE exit 0
    offline  identical                   TRUE exit 0
    skips    UNCHANGED 3/4 + 1/1 + 12/12 — no new REAL_PROVIDER skip was
             added; the live exercise is a manual script, not a skipped test

### NEXT

**Evidence Phase C — decision quality analytics.** Counts by decision /
binding_constraint / binding_reason / product / timeframe / leverage / price
band; `FAVORABLE_AFTER_REJECTION`, never automatic `FALSE_NEGATIVE`;
threshold research 0.20R-0.60R from STORED T0 values only. **Do not change
the ~0.50R threshold.** Then Execution Pass B. Scheduler stays OFF.


---

## 11. Phase C0 — decision funnel COMPLETE (green, CI f777595)

    5e68fc1  C0.1 audit + C0.2 reconciliation + EVIDENCE_ONLY semantics
    f777595  C0.3 T0 edge artifact + C0.4 last two funnel holes

    online 3,394 passed / 16 skipped  TRUE exit 0 · offline identical
    CI     32110664372 — all five green

### C0.1 — THE FUNNEL HOLE

`DecisionObservation` is built in exactly ONE place,
`canonical_entry.open_canonical_position`, so anything terminating earlier
left NOTHING. Four such paths existed in `jobs/paper_trading.run()`:

    no usable price          -> counted, discarded
    AI rejected              -> counted, discarded   <- the serious one
    symbol already open      -> pre-filtered out before the loop body
    auto_trade_enabled=False -> candidate list emptied entirely

All four now produce exactly ONE observation via `lib/decision_funnel`. One
row per market event, never one per gate; the unique `observation_id` index
makes retries idempotent; recording never raises into the paper cycle. AST
tests assert the job really calls it and that the deleted pre-filter has not
come back.

### C0.2 — SEE §6. THERE IS NO 0.50R THRESHOLD.

`expectancy.MIN_NET_R = 0.05R` is the live NET floor after costs; ~0.50R was
a modelled ROUND-TRIP COST. Tests pin both, including that no module defines
a 0.50R threshold.

### C0.3 — THE T0 EDGE ARTIFACT

**The audit found something worse than an unpersisted artifact: the paper
path called NO edge gate at all** — no `gate.decide`, no
`expectancy.evaluate` — so every edge column was NULL because the number had
never been computed there.

Now measured ONCE per candidate at T0, and the SAME object goes to whichever
terminal route it takes (funnel refusal or canonical entry). Compute once,
carry forward, persist once.

**DIAGNOSTIC, not BINDING.** This path has never used the edge gate to
refuse, so `edge_gate_role` records the role and a test asserts a diagnostic
edge is never reported as the binding EDGE constraint. Making it binding
would have changed FULL_VIRTUAL behaviour, which C0 may not do.

**The threshold travels with the measurement.** `expectancy.evaluate` returns
`threshold_used`; `MeasuredEdge` carries it. A stored decision says what bar
it was judged against — reading the module constant later answers a different
question and is the temporal drift Phase C exists to avoid.

**Stored, not reconstructed.** `expected_net_r` was previously derived as
gross − cost whenever both were present, which disagrees with the number the
decision used the moment the cost model moves. The typed artifact now wins;
derivation survives only as a fallback for old callers. New columns:
`net_expected_r_lower`, `robust`, `robust_distance_to_threshold_r`,
`edge_gate_role`, `expectancy_verdict/bucket/sample/raw_sample`.

### C0.4 — ACCOUNT STATE IS NOT A THESIS VERDICT

Already-open is an explicit ACCOUNT_STATE branch in FULL_VIRTUAL; in
EVIDENCE_ONLY the thesis is still evaluated, so the 667 legacy positions
cannot act as a hidden filter on a clean prospective epoch.
`auto_trade_enabled=False` records AUTO_TRADE_DISABLED in FULL_VIRTUAL and
does not suppress research in EVIDENCE_ONLY — **"do not trade" is not "do not
think."**

### C0.6 — MUTATION GUARD (unchanged, preserved)

`lib/runtime_mode.py`: EVIDENCE_ONLY / FULL_VIRTUAL (default FULL_VIRTUAL).
The guard lives AT the mutation — `prepare_entry`, `settle_position_entry`,
`close_paper_position`, `partial_close_paper_position` — AST-tested.
Source `FORWARD_EVIDENCE_ONLY`, terminal lifecycle `EXECUTION_SUPPRESSED`
(never SETTLEMENT_FAILED — nothing failed).

---

## 12. EVIDENCE_ONLY IS LIVE — activated 2026-08-18 (commit 73325f5)

    service   systemctl --user status|start|stop|restart jarvis-evidence
    logs      tail -f data/evidence_collector.log
    unit      scripts/jarvis-evidence.service (copy of ~/.config/systemd/user/)
    launcher  scripts/run_evidence_collector.sh   (no secrets; .env as usual)

    DB        data/forward_evidence.db   SHADOW RESEARCH — never the epoch
    seeded    SQLite backup API from data/jarvis.db
              source sha256 bcb94dccd08ab9c3a7deebe18bb2b5a2 (unchanged after)
    epoch     FORWARD_EVIDENCE_20260818T075321Z
    boundary  only signals generated at/after epoch start are eligible
    cadence   market 15m · signals 30m · candidate evaluation 5m
    CI        32114925601 all five green · 3,394 passed / 16 skipped exit 0

### FIRST VERIFIED DECISIONS

    AMD       TRADE     EXECUTION_SUPPRESSED_BY_MODE   suppressed
    NVDA      NO_TRADE  AI_REJECTED_ENTRY
    PLTR      NO_TRADE  AI_REJECTED_ENTRY
    NEAR/USD  NO_TRADE  AI_REJECTED_ENTRY

    4 observations / 4 distinct observation_ids  (one event, one row)
    12 outcome horizons scheduled · 46,630 shared quote samples
    every row: source FORWARD_EVIDENCE_ONLY, edge_gate_role DIAGNOSTIC,
    frozen threshold 0.05R, point net, lower bound, robust flag

**AMD is the case `edge_gate_role` exists for:** net 0.02R against the 0.05R
bar with a negative lower bound — the edge did NOT clear — and the decision
is still TRADE, because on this path the edge gate has no refusal authority.
Phase C must therefore NOT report EDGE as its binding constraint.

### ZERO-MUTATION PROOF

Operator DB sha256 identical before/after; still has no
`decision_observations` table. In the evidence DB: positions 667, trades 664,
outcomes 21,194, cash $63,550.8371643338, total_trades 78, wins 21 —
unchanged. Evidence tables moved; economic tables did not. Real orders 0.

### THE DEFECT THAT ALMOST WENT UNNOTICED

The first collector ran both generators inline in one loop. The market
refresh warms an OHLCV cache over ~157 symbols and took 20+ minutes to
return, so `_generate_signals` — the ONLY step that calls the LLM — was never
entered. The service reported healthy, quote evidence accumulated, and the
GPU sat idle for two hours. The operator noticed; the health output did not.
Each generator now owns a thread.

**Lesson for future runtime work: "service active" and "evidence growing" are
not proof that every stage ran.** Health should assert each stage's last
success, not merely that the process lives.

### SAFE ALLOW-LIST

Only `fetch_market_data` and `generate_signals`, both audited for economic
surface (no open_paper_position / prepare_entry / settle_position_entry /
close_paper_position / submit_order / TradingClient). Deliberately absent:
paper_trading, execute_signals, position management, broker jobs.

### SAME BRAIN

The daemon calls `paper_trading.evaluate_pending_candidates` — the identical
function `run()` uses. Only execution differs; there is no research replica
to drift.

---

## 13. NEXT: PHASE C — decision quality analytics

Evidence is accumulating NOW; leave the service running. Build analytics
against the live epoch. Key contracts: DISTINCT observation_id for decision
counts (horizons are repeated measures), primary horizon chosen from T0 only,
FAVORABLE_AFTER_REJECTION never auto-labelled FALSE_NEGATIVE, stored T0 edge
values only (never rerun expectancy), and DIAGNOSTIC edge never reported as
binding EDGE. Then Execution Pass B.
