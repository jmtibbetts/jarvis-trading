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

---

## 14. Collector hardening (2026-08-18) — service DOWN on purpose

    ba85586  cooperative cancellation + bounded worker joins
    d2f6afa  durable campaign identity + per-stage health
    CI 32121368093 all five green · 3,419 passed / 16 skipped · exit 0

### THE COLLECTOR IS STOPPED AND DISABLED

The operator is on a CELLULAR HOTSPOT (primary internet down). A deliberate
evidence GAP is accepted; it is NOT a new campaign. **Do not restart until the
operator says primary internet is restored.** No reduced/degraded live mode
was built — mixing full and intentionally-incomplete market evidence inside
one prospective campaign would need its own provenance semantics first.

    epoch     FORWARD_EVIDENCE_20260818T075321Z   (unchanged)
    boundary  2026-08-18T07:53:21.357402+00:00    (unchanged)
    dataset   45 observations · 45 distinct ids · 1 epoch

### THE ISP OUTAGE WAS NOT JARVIS

An earlier NAT/conntrack theory was speculative correlation and is dropped.
The operator confirmed the actual connection went down. Two REAL defects were
nonetheless exposed and are now fixed.

### DEFECT 1 — epoch minted per process (already fired 3x)

`os.environ.setdefault("JARVIS_EVIDENCE_EPOCH", epoch_name(now))` regenerated
identity on every start; three restarts produced three epochs. Identity now
lives in the `evidence_campaign` table inside the evidence DB — created once,
read after. A new campaign takes a deliberate `start_new_campaign()` call, and
new epochs are forced distinct (second-resolution names would otherwise merge
two campaigns started in the same second). Proven live: restart kept the epoch
and boundary, observations grew 32 to 36, rows == distinct ids.

### DEFECT 2 — SIGTERM ignored, systemd escalated to SIGKILL

Observed twice (`Result=timeout`). **The audit corrected two of my own
assumptions:** concurrency is already SEQUENTIAL (`max_workers=1`, plain
nested loop) so no semaphore was warranted, and provider calls are already
bounded by `_call_with_timeout` at 12s. The real defect was only
cancellation: ~157 symbols x 6 timeframes with no stop check, each iteration
ending in an uninterruptible `time.sleep`.

Fixed: `fetch_market_data.run` and `_warm_ohlcv_cache` take `cancel_event`,
checked per symbol and per timeframe, with the rate-limit pause becoming
`cancel_event.wait()` so it can be woken. The collector passes its OWN
`_stop` — one cancellation authority, not a second that could disagree.
`cancel_event=None` preserves existing behaviour for the scheduler and every
other caller. Shutdown now joins producers
BEFORE tearing down the runtimes (stopping them under a live worker was its
own latent bug), bounded at 20s, and a stuck worker is reported as DEGRADED
rather than silently called clean.

### PER-STAGE HEALTH — attempt vs success

The starvation bug hid because "active" and "evidence growing" were both true
while the signal stage had never run. Each stage now tracks attempts,
successes, failures, consecutive failures, durations, thread liveness and
in-progress state — and **a failure never refreshes the success clock**.
Verdicts are cadence-aware: STARTING / HEALTHY / RUNNING_LONG / STALE /
DEGRADED / FAILED / NEVER_RAN, with NEVER_RAN being the exact starvation
signature.

### Restoring the collector — install the REPO unit, never a stale one

The service unexpectedly started once while the operator intended it stopped.
`systemctl --user reset-failed` on a then-`Restart=on-failure` unit releasing a
queued restart job is the **suspected** trigger; that was never causally
proven, and the observed fact alone — it started when it should not have —
is enough to justify the policy. The committed unit is now `Restart=no`, so
an operator stop stays stopped and a crash is visible in status rather than
being papered over by a silent restart.

**The restore trap, and why it no longer exists.** An earlier draft of this
section said to restore the collector by moving a `.DISABLED_HOTSPOT` copy
back into place. That copy predated the policy change and still carried
`Restart=on-failure`, so following those words would have quietly
reintroduced the very behaviour the change removed. The stale file is gone —
the controlled restart installed the repository unit over it — and the
correct procedure installs from the repo, which is the only copy under
version control:

    install -m 0644 \
      /home/nullcode/jarvis-trading/scripts/jarvis-evidence.service \
      ~/.config/systemd/user/jarvis-evidence.service
    systemctl --user daemon-reload
    systemctl --user start jarvis-evidence

Never reinstate a moved-aside unit without reading its `Restart=` line first.

Consider changing `Restart=on-failure` to `Restart=no` for an evidence
daemon: an operator stop should stay stopped, and a genuine crash is better
surfaced than silently papered over.

### CONTROLLED RESTART PROCEDURE (when internet returns)

1. record epoch/boundary/counts · 2. 
3. prove SAME epoch and boundary · 4. watch per-stage health through one real
market refresh · 5.  must exit WITHOUT timeout/SIGKILL
6. restart and leave running.

### PHASE C — NOT STARTED

Deliberate: context, not blockers. It is mostly local work and must not
require the collector to run; the 45 real rows are read-only fixtures.

---

## 15. BLOCKING DEFECT FOUND BEFORE PHASE C — product identity is NULL

**Measured 2026-08-18 against the live campaign, 85 prospective observations:**

    product NULL   85 / 85
    venue   NULL   85 / 85

Every prospective DecisionObservation is being written without the product
and venue it was decided on. The consequence is already visible in the
outcome table:

    outcome rows            255
    PENDING                 140
    INSUFFICIENT_DATA       115
    COMPLETE                  0

Zero. Not a small number — none, across every symbol and every horizon that
has come due so far.

**This is not a data-volume problem, and waiting will not fix it.** The
forward evidence is present and plentiful: BTC/USD alone has **9,622 quote
samples** in `instrument_quote_samples` covering the exact intervals those
horizons span. The resolver cannot use them, because a forward outcome is
resolved through the product-correct authority and the observation does not
say which product it is. So the evidence sits beside a decision that cannot
be matched to it.

**Why this matters more than it looks.** Phase C built on this population
would run, pass its own tests, and report:

    usable MFE/MAE      0 / 85
    chronology          0 / 85
    product grouping    everything in one NULL bucket

— which reads as "analytics verified, sample still small" when the truth is
"the pipeline cannot produce a usable outcome at all". A green analytics
layer over a population that can never resolve is exactly the kind of
false-confidence artifact this project keeps removing. **Phase C must not be
built on top of this until it is fixed.**

**Where to look.** `decision_funnel.observe_terminal_refusal` and the
canonical-entry `_observe` both call `decision_observation.build(...)`, which
fills product/venue from `ready`/`authorization` artifacts. The evidence-only
path terminates BEFORE readiness is computed for most candidates — AI
rejection happens first — so those fields were never populated. The decision
knows its symbol and its intended side; product identity has to come from
`lib.product_router` at T0 rather than from an artifact the path never
reaches.

**Also note the population itself:** the live decisions are AMD, NVDA, PLTR,
BTC/USD and NEAR/USD. Only BTC/USD is a Bitnomial perpetual with collected
quote evidence. Equities have no range collector, and NEAR/USD is not a
listed US perpetual, so even after product identity is fixed those symbols
will resolve to INSUFFICIENT_DATA honestly rather than COMPLETE. Forward
outcome coverage is therefore bounded by which instruments the collector
actually samples — a scope question worth deciding deliberately before
reading anything into the coverage numbers.

**NEXT TASK IS THIS, NOT PHASE C:** populate product/venue on prospective
observations at T0, verify a BTC/USD horizon resolves to COMPLETE against the
9,622 samples already collected, then build Phase C on a population that can
actually answer.

### Collection PAUSED 2026-08-18 ~03:05Z — deliberate, same campaign

Stopped so the pipeline stops accumulating observations that provably cannot
resolve. This is a **COLLECTION GAP inside campaign
FORWARD_EVIDENCE_20260818T075321Z**, not a new campaign: the epoch and the
original boundary are untouched on disk, and the raw evidence already
collected is preserved.

State frozen at the pause:

    observations        95   (95 distinct ids, 1 epoch)
    verdicts            70 NO_TRADE / 25 TRADE
    outcome horizons    160 PENDING / 125 INSUFFICIENT_DATA / 0 COMPLETE
    quote samples       143,994
    stop                Result=success, 5.3s, no SIGKILL

The ten observations added between the defect being found and the pause carry
the same NULL product/venue as the other 85 — that is precisely why
collection is paused rather than left running.

**Resuming is not a matter of waiting.** The next window of work is the P0
routing-identity fix described in section 15; until product identity is
recorded at T0, every additional observation is another unresolvable row.

---

## 17. Routing identity and exact-contract evidence — both closed

### The two defects, in the order they had to be fixed

**Product identity was NULL on every prospective observation.**
`decision_observation.build()` read product/venue/asset_class/instrument only
from `ExecutionReadiness`, and the evidence funnel terminates at AI rejection
— three gates earlier. So `ready` was None and all four wrote NULL: 95 of 95
rows, 125 INSUFFICIENT_DATA against zero COMPLETE, while thousands of BTC
samples sat in the same database covering those very intervals.

`lib/routing_identity.py` now resolves a frozen `RoutingIdentity` ONCE per
candidate, before any branch can end it, from the classification authorities
already in `execution_policy`. It reads no market data — identity asks *what
product is this*, readiness asks *can it execute now*, and collapsing them is
why calling readiness earlier was the wrong fix. Proven natively on BOTH
verdict classes, which matters because a miss on one would have split the
dataset by verdict rather than emptying it visibly.

**The snapshot declared `instrument_id` and no reader ever assigned it.** The
perp reader put the contract in `provenance` instead, so 153,946 samples were
written with a NULL contract. Making the resolver require the instrument
first would have made BTC match zero samples forever — producer before
consumer.

### Boundaries — evidence facts, not commit timestamps

    INSTRUMENT_STAMP_BOUNDARY          2026-08-18T11:52:00Z
    STRICT_INSTRUMENT_LOOKUP_BOUNDARY  2026-08-18T12:38:14Z

The 153,946 pre-stamp samples remain exactly as collected. No backfill, no
inference from today's mappings. They simply cannot serve as exact-contract
evidence.

### Strict contract isolation, all eight seams

`requires_exact_instrument(CRYPTO_PERP) -> True` in one authority, consulted
by `note_quote`/`_LAST`, `record_sample`, `samples_between`, `range_over`,
`checkpoint_at`, `instruments_pending`, `collect_once` and `resolve_outcome`.
For these products a NULL instrument means the contract is UNKNOWN — never
"any contract". `Sample`, `RangeEvidence` and `Checkpoint` each carry
`instrument_id` so a proof can assert what was consumed rather than trusting
an upstream WHERE clause.

Versions: `range_collector_v3_instrument_key`,
`decision_outcome_observer_v2_instrument_key`. Terminal resolution now writes
the version of the resolver that made the CLAIM — `observer_version` is
stamped at scheduling, so a v1-scheduled row resolved by v2 would otherwise
credit v1 with a judgement it never made.

Query plans measured on real data: the existing `ix_quote_sample_window`
still serves both exact queries, 5,672 rows in 0.005ms. **No index added.**

### The 15m control case — keep it

Observation `90e90a0b5e520101f52c49151029e03b` (BTC/USD, CRYPTO_PERP,
kraken_derivatives_us, PBTCUCZ50, decision_at 11:52:04Z) had its 15m horizon
resolve COMPLETE under v1 at 12:07:50Z, from genuinely correct
PBTCUCZ50-only evidence — and v1 never filtered by contract. It was right by
accident, because nothing else was in that window.

**Left untouched on purpose.** It is the clearest demonstration in the
repository that accidentally correct and enforced-correct produce identical
output, and only one of them survives a second contract appearing. Classify
it `PRE_STRICT_INSTRUMENT_RESOLUTION`; it is not the acceptance proof.

### Live state

Collector RUNNING, campaign FORWARD_EVIDENCE_20260818T075321Z, original
activation boundary unchanged, one epoch. Scheduler OFF, real orders zero,
transfers zero. Operator DB untouched.

### Still open

- Operator DB immutability: content verified identical via read-only open,
  byte hash differed, **cause unproven**. Needs the structural /proc check
  and deterministic row fingerprints.
- Phase C analytics — blocked on nothing now except an accepted BTC outcome.
- The 95 pre-fix observations keep NULL routing identity; historical repair
  was deliberately deferred and must not use today's config as proof.

---

## 18. Strict BTC acceptance, operator isolation, Phase C — all green

### Strict-v2 BTC acceptance (the milestone §17 was waiting on)

Observation `90e90a0b5e520101f52c49151029e03b`, BTC/USD long TRADE, frozen as
crypto / CRYPTO_PERP / kraken_derivatives_us / PBTCUCZ50.

    horizon        1h, due 12:52:04.160080Z, resolved 12:52:58.159915Z
    observer       decision_outcome_observer_v2_instrument_key
    range source   range_collector_v3_instrument_key
    T0 reference   64299.425
    checkpoint     bid 64150.0 / ask 64180.0 / mid 64165.0
    samples used   6,491 PBTCUCZ50 (0 anonymous, 0 other contracts)
    coverage       first 11:52:04.204Z, last 12:52:00.189Z, max gap 20.057s
    quality        HIGH_FREQUENCY_SAMPLED
    returns        mid -0.2091%, dir-adj -0.2091%, side-ref -0.2324%
    excursions     MFE -0.0613% / -0.0287R, MAE -0.2635% / -0.1231R
    chronology     NEITHER
    status         COMPLETE

**The negative MFE is correct, not a bug.** The market never traded above T0
for that hour, so the best excursion was still a loss. Zero would have been a
lie; this is the "missing is not zero" contract holding under real data.

**The version trap fired as designed.** The row was SCHEDULED under v1 and
TERMINALIZED under v2, and it records v2 — `observer_version` is stamped at
scheduling, so without that fix the acceptance would have credited v1 with a
judgement only v2 made.

The 15m horizon stays untouched as `PRE_STRICT_INSTRUMENT_RESOLUTION`. It
resolved COMPLETE under v1 from genuinely contract-clean evidence while the
resolver filtered nothing — right by accident. Keep it: it is the clearest
demonstration here that accidentally-correct and enforced-correct produce
identical output, and only one survives a second contract appearing.

### Operator DB immutability gate — PASSED

Structural, from the live process rather than from its launcher script:
`JARVIS_DB_PATH` points at forward_evidence.db, and the collector's open file
descriptors are forward_evidence.db, ohlcv_cache.db and its log. **No
descriptor on data/jarvis.db, its -wal or its -shm** — and a scan of every
process on the host found none holding it open at all.

Logical, across a live collection interval:

    paper_positions   667    05499bb2...   unchanged
    paper_trades      664    8827c8e5...   unchanged
    paper_portfolio     1    f1e08d69...   unchanged
    trade_outcomes  21194    74cadca1...   unchanged
    cash            63550.83716433377      unchanged
    file sha256     bcb94dcc...            identical

while forward_evidence.db gained 3,230 quote samples in the same window.

`scripts/operator_db_fingerprint.py` refuses to import app.database, which
installs `journal_mode=WAL` on connect — a tool that proves immutability by
opening in a mode that can rewrite the header is not a proof. Stdlib sqlite3,
mode=ro, query_only=ON, one BEGIN for a single read snapshot. Row hashing is
typed and exact (floats via .hex(), NULL distinct from empty string) because
counts alone cannot detect a table whose every row changed.

**The earlier transient byte difference remains UNEXPLAINED.** Consistent with
WAL activity; not proven, and not claimed.

### Phase C — decision quality analytics, COMPLETE at `0847955`

`lib/decision_quality.py` + `scripts/report_decision_quality.py`, read-only by
construction (mode=ro, query_only=ON, no ORM import).

    report version   decision_quality_v1
    horizon policy   primary_horizon_v1_t0_only

Live population at first report: **215 decisions, 645 outcome rows** — that
gap is exactly the inflation that counting horizons as decisions would cause.

    verdicts        NO_TRADE 133 / TRADE 82
    routing         native 120 / legacy-missing 95 (kept in the denominator)
    primary state   COMPLETE 6, INSUFFICIENT_DATA 127, PENDING 80, PARTIAL 2
    resolution      STRICT_EXACT 7, PRE_STRICT 13, MISSING_EXACT 20

`policy_changes_authorized = false`. **Analytics verified; the prospective
sample is far too small for policy conclusions** — six complete primary
horizons.

Load-bearing behaviours pinned: a decision with an honest 15m (-0.5) and a
flattering 4h (99.0) reports -0.5, so hindsight cannot pick the horizon;
DIAGNOSTIC edge can never be reported as binding EDGE (the AMD case);
threshold labels are `WOULD_CLEAR_POINT_EDGE_AT_T`, never
`WOULD_HAVE_TRADED`; 0.05R is marked current policy and 0.50R appears only as
a sensitivity value, documented as the modelled round-trip COST it always was.

### Next: EXECUTION PASS B — canonical exits

Entry is canonical; exits still settle at the mark for canonical positions via
the legacy path, which is the last place the simulator can invent money.

**The fail-closed guard in `close_paper_position` is PERMANENT.** Pass B does
not remove it. Canonical callers get routed to `canonical_exit` and BYPASS the
legacy leaf; the leaf keeps refusing anything carrying a canonical venue-book
fill, as defence in depth against a caller that is missed or added later.

Live state at this checkpoint: collector RUNNING, campaign
FORWARD_EVIDENCE_20260818T075321Z, one epoch, original boundary, scheduler
OFF, real orders zero.

---

## 19. STOP POINT — Pass B, B0 partially complete

**Read this section first if you are continuing Pass B.**

    main            8b17781   clean, pushed, CI green
    branch          b02-sizing-tripwire @ e672e61
    tests on main   3,526 passed / 16 skipped / TRUE exit 0 / offline identical

### Where Pass B actually stands

Done:

- **Exit audit** (`docs/PASS_B_EXIT_AUDIT.md`) — all ten economic exit routes
  funnel through `close_paper_position` and `partial_close_paper_position`,
  and both already refuse canonical positions. All ten pass a MARK as the
  settlement price; that is the defect the phase removes.
- **Exact execution identity** — `instruments.resolve_for_execution()` returns
  contract-native semantics for perpetuals: `PBTCUCZ50`, `CONTRACTS`,
  multiplier **0.01**, step 1, min 1, `VERIFIED`/executable. Refuses on
  venue mismatch, contract mismatch, SHIB's unverified scale, and unlisted
  perps; preserves `EQUITY_SHORT`/`ETF_SPOT` rather than flattening them.
- **The dead seam is closed** — `canonical_entry` resolves that identity once
  after readiness and passes it to `EV.submit(instrument=...)`, proven by a
  test that patches `instruments.resolve` to RAISE and asserts execution still
  completes in CONTRACTS at 0.01.

**NOT done — B0 is not complete.** The call graph carries the INSTRUMENT but
not the SIZING. `solve_position` / `size_position` / `prepare_entry` still
derive units from the bare symbol, so risk speaks generic BTC coins at
multiplier 1.0 while execution speaks contracts at 0.01. One quantity, two
meanings, differing by 100x.

### The tripwire branch — start here

`b02-sizing-tripwire` holds `tests/test_executable_quantity_sizing.py`: 14
tests, **all failing on purpose** against 8b17781. They were written before
the implementation so they cannot become a confirmation of whatever gets
built. They are on a branch, not main, because deliberately-red tests must not
land on a green trunk.

The load-bearing cases:

    0.94 contracts        REJECT, never rounded up to 1
    2.94 contracts        2
    notional cap 3.7      3
    cash cap 2.73         2, and margin strictly BELOW the cap
    no constraint enlarges quantity
    generic get_spec() patched to raise -> exact sizing must still succeed

**Why the cash-cap case is the one to watch.** The current code assigns
`margin = cash_cap` after scaling quantity continuously, which makes 2.73
contracts look correct. A cap is a CEILING, not an obligation to spend.

**Why they had to exist first.** Every shrinking constraint scales quantity
continuously (`qty *= scale`). Threading an exact multiplier through without
re-normalising after EACH constraint yields 2.73 contracts — a number every
downstream stage agrees on and none can flag, precisely because they agree.
A visible seam is safer than a silent one.

### Exact next steps

1. `normalize_quantity_down(qty, instrument)` in `lib/instruments.py` —
   `Decimal` + `ROUND_FLOOR`, with an internal assertion that the result never
   exceeds the input. One authority; do not add normalizers in `risk_engine`,
   `paper_engine` or `canonical_entry`.
2. Thread optional `execution_instrument` through
   `prepare_entry` -> `size_position` -> `solve_position`. `None` must
   preserve legacy behaviour exactly.
3. Re-normalise and recompute economics (`notional`, `margin`,
   `loss_at_stop`) after EVERY shrinking constraint: initial solve, notional
   cap, cash cap, manual `margin_override`.
4. Keep QUANTITY MODEL separate from MARGIN MODEL — PBTC is discrete but does
   NOT use the CME fixed-margin path. Do not gate on `is_futures(symbol)`.
5. Both canonical sizing passes (quote mid, then actual fill) must receive the
   SAME instrument object.
6. Merge the branch when all 14 pass.

Then, still outstanding for B0: unit basis through
Authorization -> OrderPlan -> ExecutionResult (`OrderPlan.check` still
re-resolves the multiplier generically), and the exact executed per-contract
fee. Then B1's entry settlement ledger.

### Do not

- Build `canonical_exit` or reroute the ten exit callers yet.
- Remove or weaken `_refuse_legacy_close()` — it is PERMANENT. Canonical
  callers will BYPASS the legacy leaf; the lock stays on.
- Migrate the operator DB, start a new evidence epoch, or turn the scheduler
  on.

### Live state at this stop point

    collector       RUNNING, campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, original activation boundary unchanged
    operator DB     bcb94dcc... unchanged, structurally isolated
    scheduler       OFF
    real orders     0        transfers 0
    exit callers    all ten untouched

### The pattern that has cost the most time

Four defects this window were **wired but inert** — a guard reading a field
nothing populated, a signature missing the parameter its body used,
`snapshot.instrument_id` assigned by no reader across 153,946 rows, and
`canonical_entry` never calling the resolver it was built for. Structural
checks found none of them. What worked every time was making the OLD path
raise and proving the new one survives. Prefer that over asserting a
signature or a source string exists.


## 20. STOP POINT — B0.2 SIZING CORE COMPLETE

**Read this section first if you are continuing Pass B.** It supersedes §19.

    main            e5eeaff   clean, pushed
    branch          b02-sizing-tripwire, rebased and merged, now equal to main
    tests on main   3,549 passed / 16 skipped / TRUE exit 0 / offline identical

### What closed

The 100x seam. Execution has spoken `PBTCUCZ50` / `CONTRACTS` / multiplier
**0.01** since 8b17781; sizing still derived units from the bare symbol and
spoke generic BTC coins at 1.0. Risk and execution now share one basis.

- **`instruments.normalize_quantity_down(qty, instrument)`** — ONE shrink-only
  authority. `Decimal` + `ROUND_FLOOR`, ratio quantised at 1e-9 first so a
  decimal 3 that is binary 2.9999999999999996 floors to 3, not 2. Asserts
  against its own output rather than clamping. A minimum is an ELIGIBILITY
  FLOOR: 0.94 contracts is none, never one.
- **`solve_position(..., execution_instrument=None)`** — when supplied it is
  the authority for multiplier, unit, step and minimum, and
  `get_spec()` / `is_futures()` are not consulted at all. `None` preserves
  legacy exactly.
- **Every shrinking constraint re-normalises and recomputes** — initial solve,
  notional cap, cash cap, manual `margin_override`. The cash cap no longer
  sets `margin = cash_cap`: it bounds the notional, the quantity floors, and
  the margin is what that quantity costs. 2 contracts cost 1280.00 against a
  1747.20 ceiling, and landing below the cap is correct.
- **Quantity model stayed separate from margin model.** PBTC is discrete but
  does NOT take the CME fixed-margin path; discreteness gates on the
  instrument's own step, never on `is_futures(symbol)`.
- **The basis travels.** `RiskDecision` carries `quantity_unit` / `multiplier`;
  `prepare_entry` and `canonical_entry` thread the instrument through, and
  both canonical sizing passes share ONE object.

### Two seams the perp suite found, and they are the same shape

Both were a stage RE-DERIVING a basis that had already been established —
which is the defect pattern of this whole phase, one stage later:

1. `OrderPlan.check` re-resolved the multiplier from the bare symbol and read
   **$99,400.00 against an approved $994.00** — exactly 100.0 — refusing every
   perpetual order it had itself approved. It now prices the order in the
   units the risk was approved in, but ONLY when a basis was stated; legacy
   decisions still resolve, and still fail closed on unknown units.
2. `EntryAuthorization.risk_quantum` read the CONTINUOUS 1e-6 quantity step
   for an instrument that rounds to one contract. Re-pricing a 3-lot at a fill
   further from the stop cannot answer "hold slightly less" — only 3 or 2
   exist — so $966.00 -> $981.57 on an unchanged size was refused. The budget
   is still enforced exactly in `solve_position`; the quantum only ever
   governed the precision of the comparison.

### Tests

`tests/test_executable_quantity_sizing.py` — the 14 tripwire tests, calibrated
first. Four of them had been failing on the tripwire's OWN mistakes
(`d.quantity` vs `qty`, an invented `loss_at_stop`, a missing `free_cash`, an
invented `cash_cap_usd`). Each would have been "fixed" by widening the
production API to match the test; fixing the test kept one canonical field,
one arithmetic truth and one cash API. Load-bearing results:

    0.94 theoretical      REJECTED
    2.94                  2
    notional cap 3.7      3
    cash cap 2.73         2, margin 1280.00 < cap 1747.20

`tests/test_sizing_call_graph.py` — the half the tripwire cannot reach: that
the REAL chain carries the object. It poisons `get_spec` to 999.0 and proves
the exact answer does not move, **plus a control proving the same poison DOES
move the legacy answer** — without which that silence would be vacuous. All
nine fail against the pre-implementation `lib/`.

### Exact next steps

1. Unit basis onward through `OrderPlan` -> `ExecutionResult` agreement.
   `OrderPlan` still has no quantity-unit field of its own; `check` now
   borrows the decision's.
2. The exact executed per-contract fee.
3. Then B1's entry settlement ledger, then `canonical_exit`.

### Do not

- Build `canonical_exit` or reroute the ten exit callers yet.
- Weaken `_refuse_legacy_close()` — PERMANENT.
- Migrate the operator DB, start a new evidence epoch, or turn the scheduler
  on.

### Live state at this stop point

    collector       RUNNING (pid 244809), campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, boundary 2026-08-18T07:53:21Z unchanged
    runtime mode    EVIDENCE_ONLY
    operator DB     data/jarvis.db untouched (mtime 2026-08-17 15:38)
    scheduler       OFF (no app process running)
    real actions    0

### A note on trees

The canonical working tree is the WSL one, `~/jarvis-trading`, and it is where
the collector runs and where the 3,549/16 baseline is measured. The Windows
checkout at `C:\jarvis-trading-ai-python` shares the same remote but has its
own stale `data/`. Running app-importing code there OUTSIDE pytest opens that
tree's own database — harmless to live state, but `conftest.py`'s redirect
only protects code run UNDER pytest. Use pytest, or a read-only connection.

## 21. STOP POINT — B0 COMPLETE

**Read this section first if you are continuing Pass B.** It supersedes §20.

    main            380ffc6 + this docs commit   clean, pushed
    tests           3,593 passed / 16 skipped / TRUE exit 0 / offline identical
    CI              380ffc6 GREEN (workflow "tests": completed/success,
                    read from the Actions REST API for the exact SHA;
                    b631a7a also confirmed green retroactively)

### What closed — B0's completion condition, met

One real canonical PBTC path now proves, in captured production objects:

    RoutingIdentity -> InstrumentIdentity -> RiskDecision ->
    EntryAuthorization -> OrderPlan -> VirtualOrder -> ExecutionResult ->
    FeeQuote

all describe ONE economic quantity — whole PBTCUCZ50 contracts at 0.01 —
no stage can enlarge quantity, no stage can substitute a basis, and the
execution fee counts the contracts that actually filled.

- **Strict shrink-only.** The normaliser's 1e-9 HALF_EVEN pre-quantize is
  gone (it rescued 0.9999999996 into a contract risk never approved).
  `Decimal(str()) + ROUND_FLOOR`, strict `result <= input` assertion with
  NO tolerance — provable because `units*step <= Decimal(str(q))`, float()
  is monotone on Decimals, and str() round-trips. §20's "floors to 3, not
  2" note is superseded: a binary 2.9999999999999996 floors to 2 now, and
  upstream arithmetic that produces just-under values for exact amounts is
  upstream's defect to fix. Fuzzed 200k inputs, zero violations.
- **Exact budget boundary.** loss<=budget keeps the legacy 0.01% economic
  forgiveness only on the legacy path; the exact path's tolerance is
  representation-only (1e-9 relative) — pinned by a poisoned normaliser
  whose hair-sized enlargement the legacy tolerance would have approved.
- **Stated-but-broken refuses.** OrderPlan.check: stated & valid basis ->
  used; stated & broken (0/NaN/inf/negative/half-a-basis) -> REFUSE without
  consulting generic resolution; unstated -> legacy resolution unchanged.
- **The plan carries its own basis** (instrument_id/quantity_unit/
  multiplier, from the identity resolved once); the gate requires plan and
  approval to agree; the adapter refuses plan-vs-instrument disagreement
  BEFORE execute_market (REFUSED_UNIT_BASIS_MISMATCH); the VirtualOrder
  carries the unit.
- **An execution that contradicts its plan never settles.**
  `execution_disagreement()` runs before economic settlement; injected
  COINS / multiplier-1.0 / overfill corruptions are each proven refused by
  the ABSENCE of the settlement call, with an uncorrupted control settling.
- **The fee counts what filled.** `leg_fee` has an exact-execution path:
  planning still rounds UP (`contract_count_basis=PLANNING_ROUND_UP`);
  executed fees take the filled count exactly (`EXECUTED_EXACT`), require
  count x fill x multiplier == executed notional (else
  EXECUTION_UNIT_MISMATCH), and canonical_entry prices the execution that
  SURVIVED post-fill repricing — a forced 3->2 resubmission pays fees on 2.
  The catastrophic-product gate divides by the executed notional from the
  same ExecutionResult. Provenance adds requested_quantity,
  executed_notional_usd and entry_fee_contract_count_basis.

Where the proof lives: `tests/test_b0_execution_basis.py` (20 tests; 10 red
against the pre-B0-final lib/), plus the strict-normaliser and exact-budget
classes in `tests/test_executable_quantity_sizing.py` and the stated-basis
classes in `tests/test_risk_gate.py`. Two source-pin tests were updated for
the gate's denominator rename (`final.notional` -> `executed_notional`);
their invariant — never margin, never loss — is unchanged.

### NEXT = B1 ENTRY SETTLEMENT LEDGER

NOT Pass B complete. NOT canonical_exit. NOT caller routing. NOT scheduler.

### Do not

- Weaken `_refuse_legacy_close()` — PERMANENT.
- Migrate the operator DB, start a new evidence epoch, or turn the
  scheduler on.

### Live state at this stop point

    collector       RUNNING (pid 244809), campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, boundary unchanged, EVIDENCE_ONLY
    operator DB     data/jarvis.db untouched (mtime 2026-08-17 15:38,
                    md5 2cee5371d9e1505f12c9447230dbc941)
    scheduler       OFF
    real actions    0

### CI without gh

`gh` is not installed anywhere, deliberately. The Actions REST API works
with the repo credential already in the WINDOWS credential manager
(`git credential fill` — the WSL side has none, which is also why pushes
hang there):

    GET /repos/jmtibbetts/jarvis-trading/actions/runs?head_sha=<sha>

Never echo the token; feed it straight from `git credential fill` to curl.

## 22. STOP POINT — B1 ENTRY SETTLEMENT LEDGER COMPLETE

**Read this section first if you are continuing Pass B.** It supersedes §21.

    main            e5cd441 + this docs commit   clean, pushed
    tests           3,637 passed / 16 skipped / TRUE exit 0 / offline identical
    CI              e5cd441 GREEN (Actions REST, exact head_sha)

### What closed

A NEW canonical position can no longer exist as only a PaperPosition plus a
JSON document. `settle_position_entry` — still the ONE transaction — now
creates atomically, or not at all:

    PaperPosition
    PaperPositionSettlement          OPEN header, paper_settlement_v1
    PaperSettlementLeg (ENTRY)
    the cash/margin debit
    DecisionObservation SETTLED linkage

- **Canonical intent is the causal pair** (`observation_id` +
  `execution_id`, together or not at all). Half a pair fails CLOSED
  (`INCOMPLETE_CANONICAL_LINKAGE`); nothing downgrades to legacy. Legacy
  callers (neither id) are byte-for-byte unchanged and get ZERO ledger rows.
- **`lib/settlement_ledger.py`** — a pure validator (no session, no
  re-pricing) builds one fact set or refuses: one execution identity across
  the chain, current canonical models only, frozen identity never
  re-resolved, finite positive unit basis, quantities that agree and relate
  only downward, one fill, the fee that was actually charged (EXECUTED_EXACT
  count == settled fill), one arithmetic definition of R. The persister
  takes the EXISTING session and may not commit / get_db / call the fee
  authority / risk engine — proven by poisoning all four during a real
  settlement (success) plus a control where the same poison kills the
  pricing path (failure).
- **The ledger describes the cash that moved.** Header figures equal the
  position row's rounded, debited facts; C1 = C0 − margin − fee exactly.
  ENTRY legs are structurally not outcomes (zero gross/holding/released/
  hours, no PaperTrade, no counters, no learning). One `settlement_time`
  stamps position, portfolio, header, leg, observation.
- **Uniqueness is database-enforced**: `uq_pps_position`,
  `uq_pps_entry_execution`, `uq_psl_execution`, plus `ix_psl_position`;
  named `DUPLICATE_CANONICAL_EXECUTION` refusal ahead of the constraint, and
  the IntegrityError handler distinguishes a ledger race from a symbol
  collision. EXPLAIN QUERY PLAN pins B2's four lookups to index walks.
- **PBTC acceptance**: persisted CONTRACTS / 0.01 / PBTCUCZ50; the 3→2
  resubmission persists ONLY the surviving 2-contract execution (header 2,
  leg requested=filled=2, fee count 2 EXECUTED_EXACT, cash moved margin+0.30).
- **Rollback matrix** at five stages (ledger persist, header ctor, leg ctor,
  before_commit, observation linkage): the PROPERTY asserted is that no
  economic mutation survives — counts, cash, and observation state all
  unchanged.

Proof lives in `tests/test_b1_entry_ledger.py` (44 tests; 42 red against
pre-B1 code).

### Production-copy migration proof

On a copy of the operator book (667 positions / 664 trades / 21,194
outcomes / cash 63550.837...): `init_db()` applied the B1 schema; values
over the ORIGINAL columns are hash-identical for all four economic tables
(05499bb2…, 8827c8e5…, f1e08d69…, 74cadca1…); the standard pre-existing
column migration added `paper_positions.execution_provenance` as all-NULL
(so a WHOLE-ROW hash moves by exactly that NULL column — schema, not
values; B1 itself adds only tables); new tables created EMPTY; second
`init_db()` idempotent; `PRAGMA integrity_check` ok.

### Operator DB — deliberately NOT migrated

File sha `bcb94dcc…` identical before and after this continuation; all four
economic row-hashes unchanged; the fingerprint tool now classifies both
ledger tables as economic and reports them `NOT_PRESENT_IN_THIS_SCHEMA`
(ABSENT) on the operator DB truthfully — it never creates, fails, or
pretends a zero count.

### NEXT = B2 CANONICAL EXIT CORE

READY_FOR_CANONICAL_EXIT_CORE. NOT Pass B complete; NOT exit caller routing;
NOT scheduler activation. The ten audited exit callers still reach the two
legacy functions and `_refuse_legacy_close()` remains PERMANENT. Exit leg
KINDS (PARTIAL_EXIT / FINAL_EXIT) exist in the schema vocabulary; no
production path writes them. No RealizedOutcome row exists yet — B2 creates
the final settlement state, ONE canonical RealizedOutcome, ONE learning
application. `settlement_revision` starts at 0 (entry committed, zero exit
legs); each B2 exit leg checks and increments it atomically.

### Live state at this stop point

    collector       RUNNING (pid 244809), campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, boundary unchanged, EVIDENCE_ONLY
    operator DB     data/jarvis.db untouched (sha bcb94dcc…, cash 63550.84)
    scheduler       OFF
    real actions    0

## 23. STOP POINT — B2A EXIT SETTLEMENT CORE COMPLETE

**Read this section first if you are continuing Pass B.** It supersedes §22.

    main            5676454 + this docs commit   clean, pushed
    tests           3,678 passed / 16 skipped / TRUE exit 0 / offline identical
    CI              5676454 GREEN (Actions REST, exact head_sha)

### What closed

The FINANCIAL half of canonical exit, deliberately without the market-facing
orchestrator: `lib/canonical_settlement.settle_prepared_exit(facts)` takes an
exit execution that has ALREADY been established and answers "what may the
account book mutate?" — provable end to end without a provider, quote
stream, or venue.

- **`lib/holding_cost_authority.py`** — carry grew a type. HoldingCostQuote
  either establishes an amount with provenance or refuses (UNAVAILABLE,
  amount None — never zero: zero is six facts wearing one number). Funding
  is a SIGNED TRANSFER (longs pay positive, shorts receive; no abs());
  perp quality is LATEST_RATE_EXTRAPOLATED even off a "measured" snapshot —
  a rate measurement is not an interval measurement; DEFAULT_BASELINE /
  MEASURED_BORROW_RATE / DEFAULT_GENERAL_COLLATERAL / DEFAULT_HARD_TO_BORROW
  / NOT_APPLICABLE complete the vocabulary. Version holding_cost_v1.
- **The contracts**: frozen identity (header's symbol/product/venue/
  instrument only — no router/resolver/config at settlement); unit basis
  immutable; side must REDUCE (long SELLs, short BUYs — never inferred from
  P&L); `expected_revision` is the concurrency authority
  (STALE_SETTLEMENT_REVISION; a re-prepare is a NEW authorization);
  execution_id idempotent (IDEMPOTENT_ALREADY_SETTLED, zero mutation);
  fills cannot exceed remaining; PARTIAL vs FINAL is DERIVED; scaled_out is
  not accounting authority.
- **Margin release is return of capital.** Proportional on partials; FINAL
  releases ALL remaining margin (dust dies). Sealing invariants: exit legs
  must total original_quantity and released margin must total
  committed_margin, or the final settlement rolls back.
- **realized_pnl audited and pinned (§27)**: legacy is cumulative-as-
  realized in lockstep with paper_trades. Canonical: each exit leg accrues
  its net when it settles; the ENTRY FEE accrues at FINAL — so a completed
  position's Δrealized_pnl == canonical net == the ONE aggregate
  PaperTrade.realized_pnl.
- **One final truth**: `realized_outcome.build_from_settlement()` — gross is
  SUM of exit-leg gross (ledger authority; VWAP is display; decision VWAP is
  None if ANY leg lacks one); fees map by basis with the entry fee ONCE;
  returns on COMMITTED MARGIN (`return_pct_basis="MARGIN"`); R on header
  initial risk. Persisted as `paper_realized_outcomes` (UNIQUE position_id),
  stamped **outcome_v2_settlement** (v1 rows never relabelled),
  `learning_state=PENDING` — `record_trade_outcome()` is NOT called; it is
  not an idempotency boundary, and a learning failure must never unwind a
  correct exit. One aggregate PaperTrade per POSITION on FINAL (loss-streak
  guard, morning brief, learning history read that table). Counters vote
  once, on NET.
- **Schema**: leg exit facts (exit_reason; trigger_price ≠ decision_price ≠
  fill_price, never collapsed; holding type/source/quality/version;
  remaining qty/margin after), header final links (final_execution_id,
  realized_outcome_id), composite (position_id, settlement_revision) index —
  ordered ledger reads walk it with no sort — and revision-ordered
  reconstruction (timestamps are not a concurrency authority).

Proof: `tests/test_b2a_exit_settlement.py` (28) + `tests/
test_holding_cost_authority.py` (13); 26 red against pre-B2A code. Long and
short direct proofs exact at 1e-9 (the short RECEIVES positive funding — the
sign class that once burned this codebase is pinned); partial→final and
2/3/5 partials with per-leg carry (4 contracts pay 8h, 6 pay 24h — never 10
twice, never 4 free); rollback matrix; EVIDENCE_ONLY / legacy / hybrid
refusals; the poisoned-world proof (fee authority, risk engine, resolvers,
router, readiness, venue, fill model, funding lookup all explode — prepared
facts still settle) with a live-poison control.

### Production-copy migration proof

On the operator copy: all four economic tables value-identical over original
columns; three ledger tables created EMPTY; B2A columns present; double
init_db idempotent; integrity ok. Operator DB untouched (sha `bcb94dcc…`
before and after; fingerprint reports all three ledger tables ABSENT,
truthfully).

### NEXT = B2B CANONICAL EXIT ORCHESTRATOR

READY_FOR_CANONICAL_EXIT_ORCHESTRATOR. The market-facing
`lib/canonical_exit.py` comes next: read the immutable position snapshot,
close the session, do market readiness / execution / fee quote /
holding-cost quote, then hand `ExitSettlementFacts` to this core. After
that: the idempotent learning projection (keyed on PaperRealizedOutcome,
PENDING→APPLIED), then caller routing. NOT Pass B complete; the ten exit
callers still reach the legacy leaves; `_refuse_legacy_close()` PERMANENT;
scheduler OFF.

### Live state at this stop point

    collector       RUNNING (pid 244809), campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, EVIDENCE_ONLY
    operator DB     data/jarvis.db untouched (sha bcb94dcc…, cash 63550.84)
    scheduler       OFF
    real actions    0

## 24. STOP POINT — B2B CANONICAL EXIT ORCHESTRATOR COMPLETE

**Read this section first if you are continuing Pass B.** It supersedes §23.

    main            158923c + this docs commit   clean, pushed
    tests           3,720 passed / 16 skipped / TRUE exit 0 / offline identical
    CI              158923c GREEN (Actions REST, exact head_sha)

### P0 hardening (done FIRST, red-test-first against fc3754d)

- **P0.1 carry binding**: ExitSettlementFacts carries the HoldingCostQuote's
  own symbol/product/notional/rate; validator requires symbol+product match
  and a structurally possible kind (perp=FUNDING, equity short=BORROW,
  spot=NOT_APPLICABLE); settlement proves notional ==
  filled x entry_fill x multiplier and interval == settled_at − opened_at.
  A 10-contract quote can no longer settle a 2-contract exit.
- **P0.2 idempotency**: same id + other position → EXECUTION_ID_COLLISION;
  same position + different economics → IDEMPOTENCY_CONFLICT; only a true
  retry is idempotent. expected_revision deliberately excluded.
- **P0.3 contract pinning**: execution_market_snapshot takes
  `instrument_id`, records `requested_instrument_id` (stated ≠ confirmed),
  Bitnomial refuses EXECUTION_INSTRUMENT_MISMATCH before touching a drifted
  book, a central post-reader check backstops all readers, and readiness
  threads the frozen contract in and re-verifies on the way out.

### B2B — lib/canonical_exit.py

`close_canonical_position(position_id, requested_qty | close_fraction,
exit_reason, trigger_price, decision_price, max_age_s)` — NOT wired to the
ten production callers. The chain: immutable B1-header snapshot (session
closed before provider work) → frozen RoutingIdentity
(PERSISTED_CANONICAL_ENTRY; the position already knows what it is) → exact
readiness → resolve_for_execution → normalize-DOWN reduction (full-close of
a fractional remainder refuses as corrupted state) → REDUCE RiskDecision +
reduce-only OrderPlan (existing types extended: intent/position_id/
position_side/reduce_only; OrderPlan.check grew a REDUCE branch — no faked
stop arithmetic, side must reduce) → ExecutionVenue.submit → shared
execution agreement (`lib/execution_consistency`, re-exported from
canonical_entry) → exact fee at the ACTUAL fill and EXECUTION side (equity
regulatory fees are sell-side facts) → one settled_at, explicit hours →
established carry model (leg qty x entry fill x multiplier) → B2A as the
only economic mutation. STALE_SETTLEMENT_REVISION returns
`reprepare_required` — a fill prepared for revision N is never replayed.

Named refusal vocabulary: NOT_CANONICAL_POSITION,
EXIT_MARKET_DATA_UNAVAILABLE, EXIT_EXACT_INSTRUMENT_UNAVAILABLE,
EXIT_INVALID_QUANTITY, EXIT_REDUCTION_RISK_REFUSED, EXIT_EXECUTION_REFUSED,
EXIT_EXECUTION_CONTRADICTION, EXIT_FEE_UNAVAILABLE,
EXIT_HOLDING_COST_UNAVAILABLE, plus B2A's stale/idempotent/conflict/
collision set.

### Proofs (tests/test_b2b_canonical_exit.py, 31; P0 regressions, 11)

Long SELLs the bid / short BUYs the ask on a 60k/61k book; the 64,000 stop
gaps to a 62,500 book and fills THERE, trigger recorded as a trigger, tail
loss booked; trigger ≠ decision ≠ fill persisted distinct; partial→final
votes once; 7 × 0.5 → 3 with theoretical 3.5 in provenance; stale/desync/
halt/closed/legacy refuse by name with zero mutation; contract drift refuses
before the fill model; config drift cannot change identity; router poison +
control; favorable marks move reference fields only; corrupt carry
notional / unavailable carry / unavailable fee / corrupted ExecutionResult /
instrument swap all leave the position OPEN; a rival settlement stales the
prepared fill; EVIDENCE_ONLY settles nothing; learning stays PENDING with
record_trade_outcome poisoned uncalled.

### NEXT = B2C IDEMPOTENT LEARNING PROJECTION

READY_FOR_IDEMPOTENT_LEARNING_PROJECTION: consume PENDING
PaperRealizedOutcome rows, keyed on outcome id / position_id, mark APPLIED
once — then caller routing, then (much later) scheduler. NOT Pass B
complete; ten exit callers untouched; `_refuse_legacy_close()` PERMANENT.

### Live state at this stop point

    collector       RUNNING (pid 244809), campaign FORWARD_EVIDENCE_20260818T075321Z
    epochs          1, EVIDENCE_ONLY
    operator DB     data/jarvis.db untouched (sha bcb94dcc…, cash 63550.84;
                    all three ledger tables truthfully ABSENT)
    scheduler       OFF
    real actions    0
