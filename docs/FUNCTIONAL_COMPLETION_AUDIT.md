# Functional completion audit

Re-audit from first principles, trusting nothing previously claimed. Every
item below was verified against the running system, the active database or
the real call graph — not against documentation, commit messages or tests.

**State:** `JARVIS_NOT_FUNCTIONALLY_COMPLETE` — substantial defects found and
fixed; activation gate not yet reached. Remaining scope is listed at the end.

---

## Phase 0 — baseline, verified

| Claim (from #39) | Verified | Result |
|---|---|---|
| active DB integrity / FK | yes | `ok`, 0 violations |
| 70 tables | yes | 70 |
| zero economic rows | yes | positions 0, trades 0, outcomes 0, headers 0, legs 0, realized 0 |
| cash 100,000 | yes | 100000.0 |
| disarmed | yes | `live_trading_enabled=0`, `paper_auto_trade_enabled=0`, `auto_sim_enabled=0`, `trade_mode=paper` |
| legacy archive 667/664/21,194, cash 63,550.8371643338 | yes | exact, both copies, integrity `ok` |
| 20 legacy positions still open | yes | 20, archived open — never closed |
| classification, zero UNKNOWN | yes | 70/70 classified |
| rollback still possible | yes | `LEGACY_ROLLBACK_STILL_LOGICALLY_POSSIBLE` |

### Correction to a prior claim

**`REAL_PROVIDER_READ_ONLY = UNAVAILABLE` was stale.** With the market-data
runtime running, Bitnomial produces usable two-sided books:

    PBTCUCZ50   64485.0 / 64510.0
    PETHUIZ50   1911.60 / 1912.40
    PSOLUSZ50     76.86 / 76.90

The #39 smoke gave up after 40 seconds. Crypto perpetuals are executable.

### One weaker-than-documented claim

The legacy archive is `0444`, but it is a WAL-mode database in a writable
directory, so a read-only reader still creates a `-shm` beside it — the
archive's own `-shm` is stamped two minutes after it was sealed, by
verification reads. The DATA is intact and logically verified; the word
"immutable" overstates what the file mode enforces. The dormant original is
the byte-preserved copy. Not yet remediated; recorded honestly.

---

## Defects found and fixed

### P0 — the automatic trading loop crashed on every run

`jobs/paper_trading.run()` referenced `skipped_ai`, `skipped_no_price` and
`skipped_no_execution` at its summary line. Those are locals of
`evaluate_pending_candidates` — a different function. The scheduler's only
paper-trading entry point raised `NameError` every time, **after entries had
already executed**, so the loop could mutate the book and then report
failure. The counts were in the returned dict all along; `run()` never read
it.

3,900 tests were green. None called `run()`.

### P1 — three live `NameError`s in the API layer

Invisible to any linter because the routers use
`from app.routers.common import *`:

| site | effect |
|---|---|
| `intel.get_portfolio_risk` | inside `except Exception: pass` — the analyst has **silently never received** the portfolio it was asked about |
| `intel.analyze` `sym` | `/analyze?generate_signal=true` → 500 |
| `trading.normalize_symbol` | `/signals/{id}/reverse` → 500 |

Guarded permanently by `tests/test_no_undefined_names.py`, which resolves the
router wildcard into explicit names so the ~7,000 lines of API code are
checked, with a control proving the checker works.

### P1 — the manual open created a second economy

`POST /api/paper/open` called `open_paper_position`, whose own docstring says
*"whatever it is handed becomes the fill … precisely the mark-as-fill
behaviour `lib/canonical_entry` exists to replace."* In the canonical epoch
that quietly created positions with no settlement header — priced by whoever
called the API. Now routes through `open_canonical_position`, caller price as
a **decision reference**, execution market as fill authority.

### P1 — missing market price became HTTP 500

Same endpoint: `market_assets` crosses a cutover without transient columns by
design, so `float(a.price)` raised `TypeError` on a fresh book. Now a named
`MARKET_PRICE_UNAVAILABLE` 400. Zero, negatives and the non-finites are
refused rather than accepted — `float(x or 0)` conflates "no price" with
"price of nothing".

### P1 — two schedulers could run against one book

No cross-process guard existed; APScheduler's `max_instances=1` is
per-process. Two `main.py` processes would both open entries, manage the same
positions and project learning, silently. `lib/scheduler_lease.py` now takes
a kernel advisory lock beside the **database** — released by the kernel on any
exit, unlike a lease row which needs a heartbeat and can look alive after a
crash. The second process serves the API in STANDBY; `/api/jobs/status`
reports `OWNER / STANDBY / DISABLED / UNCLAIMED`. If the guard cannot run,
the scheduler does **not** start.

### P2 — the "12 known Windows failures" hid nine real gaps

Nine were `read_text()` with no encoding — cp1252 choked on box-drawing
characters in comments, so those tests **never ran on Windows** and their
coverage was silently absent. Three genuinely compare a shell-derived path to
a pathlib one and are now explicitly skipped there. Windows is now green:
3,902 passed, 0 failed.

### P3 — unresolvable type annotation

`lib/gate.py` annotated `-> "TradeDecision"` with nothing importing it.

---

## Verified working

### Learning is actually consumed, not merely APPLIED

The #38 epoch unification proved the raw `engine_epoch` query counts an
outcome. That is a weaker claim than "the learners see it" — each consumer
adds filters, joins and sample floors. Now asked directly: **calibration**,
**expectancy** (bucketed by real asset class, not collapsed to "unknown") and
the paper job's **own evidence count** all see canonical outcomes, with a
control proving a retired-epoch outcome is still excluded.

### Full canonical lifecycle on a LIVE book

Disposable database, real read-only Bitnomial data, fake money:

    entry     29 CONTRACTS, unit CONTRACTS, multiplier 0.01,
              instrument PBTCUCZ50, fill 64635.45
    legs      ENTRY -> PARTIAL_EXIT -> FINAL_EXIT
    outcome   1 realized, 1 trade_outcome, 1 paper_trade   (one thesis, one vote)
    learning  APPLIED, epoch 2026-08-18-canonical-lifecycle-v1
    cash      100000.00 -> 99904.06

---

## Product acceptance (measured, not claimed)

| product | instrument | status | evidence |
|---|---|---|---|
| CRYPTO_PERP BTC | `PBTCUCZ50` | **PASS** | live book; full lifecycle settled |
| CRYPTO_PERP ETH | `PETHUIZ50` | **PASS** (book) | 1911.60/1912.40 |
| CRYPTO_PERP SOL | `PSOLUSZ50` | **PASS** (book) | 76.86/76.90 |
| CRYPTO_PERP LTC/BCH/LINK | `PLTCUSZ50` etc. | book observed | seen in quote samples |
| EQUITY / ETF | AAPL, SPY | **BLOCKED_SESSION** | real prices, `STALE_EXECUTION_DATA` — US market closed at probe time; needs re-measurement during session |

Crypto routes to perpetuals by design; there is no silent downgrade to spot.

---

## Remaining scope, not yet done

Honest list — none of these are claimed as verified:

- full frontend audit (fake data, dead controls, unit labelling)
- complete API endpoint-by-endpoint audit and error-model review
- risk system: portfolio-level policy traced to the last execution gate
- restart cases 1–4 (zero / open / post-partial / pending learning)
- equity and ETF acceptance during market hours
- whole-repo unit/multiplier sweep (Phase 3)
- provider capability matrix
- fill-model review: the live entry filled 135 points above the ask on a
  ~$18.7k notional, which is plausible on a thin book but unverified
- pre-activation gate, first canonical mutation, scheduler activation, soak

## Live safety, current

    live trading      OFF
    scheduler         OFF
    auto paper trade  OFF
    runtime           EVIDENCE_ONLY
    real orders       0
    transfers         0
    active book       zero economic rows, cash 100,000

---

# Milestone: fill-model evidence and evidence durability

## Bitnomial book semantics — PROVEN

| fact | value | how |
|---|---|---|
| quantity unit | **CONTRACTS** | official spec states `volume`, `open_interest`, `block_volume` in contracts; corroborated across three different multipliers |
| price encoding | integer ticks | `price_usd = raw × price_increment` — PBTC 5.0, PETH 0.2, PSOL 0.01 |
| visible depth | **top 10 levels only** | docs: *"Level updates are NOT sent when a level goes out of scope"* — visible depth is a FLOOR, deepest levels can be stale |
| snapshots | every ~10 s | self-heals a desynced book quickly |
| sequencing | `ack_id` only | **no `sequence_id` field exists**, contrary to the doc's advice to use one |

Measured over 587 live frames: **0 backward acks**, 272 repeated (one
`ack_id` is an atomic batch — correctly applied), 183 gapped (expected).

Contract sizes differ per product — **0.01 BTC, 0.5 ETH, 5.0 SOL** — which
is exactly why PBTC behaviour must never stand in for generic support.

## The 0.21% slippage — provenance resolved

**Category B, not C. It is NOT circular.** Traced to
`lib/execution_recorder`, called from `jobs/execute_signals` (`record_intent`)
and `jobs/manage_positions` (`record_fill`) — both Alpaca broker jobs. Those
50 fills are external broker observations, not simulator output.

The real objection is sharper: they were **equity and crypto-spot fills at a
retail broker**, now used as the assumption for **contract perpetuals on
Bitnomial**, applied as a flat percentage that does not move with order size.

## Champion/challenger, measured on the live book

`TOP_OF_BOOK_FIXED_SLIPPAGE_V1` (current behaviour, truthfully renamed — it
never reads depth) versus `DEPTH_VWAP_V1`:

| product | side | qty | fixed | depth |
|---|---|---|---|---|
| PBTC | BUY | 1–50 | 21.0 bps, filled | **0.0 bps**, filled |
| PBTC | BUY | 1000 | 21.0 bps, **filled 1000** | 7.4 bps, **filled 252 of 1000** |
| PETH | BUY | 200 | 21.0 bps, filled 200 | **92.6 bps**, filled 149 |
| PETH | SELL | 200 | 21.0 bps, filled 200 | **291.3 bps**, filled 177 |
| PSOL | BUY | 200 | 21.0 bps, filled 200 | **336.5 bps**, filled 119 |

Visible depth: PBTC 252/237, PETH 149/177, **PSOL 119/119** contracts.

**The fixed model is wrong in both directions.** It charges ~21 bps on small
orders that would fill entirely at the touch for nothing, and it reports a
clean full fill on orders the visible book cannot absorb — PSOL 200 lots at
21 bps modelled versus **336 bps and only 60% filled** in reality. The thin
products are far worse than PBTC, so sizing calibrated on BTC would be most
wrong exactly where it is least tested.

This is precisely "the bot makes money because the simulator is wrong", and
it is a **blocker for FULL_VIRTUAL activation on ETH/SOL at size**.

No model has been promoted. The active book remains untouched.

## Evidence durability

**Fixed (P0):** `flush_samples()` drained the buffer before the insert, so
any failure destroyed the batch — measured live, two `database is locked`
events, ~100 Bitnomial samples permanently lost. Batches now return to the
front of the buffer and retry; overflow bounded at 20k, shed rows counted.

**Remaining exposure, stated honestly:** the buffer is still RAM. A SIGKILL,
host reboot or WSL shutdown loses whatever is unflushed — at the observed
rate roughly one flush interval of samples. A durable spool is **not yet
implemented**; the crash-loss window is small but non-zero.

## Superseded

- "archive is immutable" — the 0444 file sits in a writable WAL directory
- "REAL_PROVIDER_READ_ONLY UNAVAILABLE" — Bitnomial books are live
- "ORDERBOOK_SIMULATED" as a name for the fixed model — it reads no book

## P0 — durable spool: measured, and deliberately NOT built

| measurement | value |
|---|---|
| flush cadence | **1.0 s** (`FLUSH_INTERVAL_S`) |
| ingestion rate | 7–25 rows/s |
| expected loss on SIGKILL | **~1 second ≈ 7–25 samples** |

A durable spool would add a second write path competing for the same SQLite
lock — the exact contention that caused the original loss — plus
checkpointing, replay and dedup, to protect one second of replaceable book
snapshots. **Not warranted.** The exposure is stated rather than engineered
away, and is now continuously visible instead of being a one-off claim.

**A second silent-drop path was found and closed.** `observe()` discarded
samples when the buffer hit `MAX_BUFFER = 5000` with no counter at all — and
that cap fired *before* the counted 20,000 cap added in the previous
milestone, so the counted one could never be reached. One limit now, and
every divergence between `received` and `persisted` has a name:

    received, persisted, retried, shed_on_append,
    shed_after_failure, backlog, oldest_backlog_at, unaccounted

`unaccounted` is asserted to be zero in tests on both a clean run and a
failed flush.

## P3 — the crash seam: measured, and it is not the hazard feared

Crashes injected at each boundary, book inspected after:

| boundary | measured result |
|---|---|
| after submit, before fee/carry/settlement | **book byte-identical** — no legs, no outcome, no cash change |
| abandoned attempt, then a later exit | settles normally, **exactly one** outcome |
| after settlement, second attempt | **refuses**; settled economics unchanged |

**Why this is safe, stated precisely:** virtual execution leaves nothing
resting at a venue, so an abandoned attempt is a genuine no-op rather than an
unreconciled fill. A later cycle re-prices against a fresh book — that is a
NEW decision, not a stale retry, and it is the correct outcome. Settlement is
a single B2A transaction, so SQLite rolls back any death mid-write; there is
no half-settled state.

**What this bounds:** execution ids are minted per attempt, so B2A's
idempotency protects against the same facts being submitted twice — not
against a fresh attempt at a new book. That distinction is the whole reason
the seam is safe here and would NOT be safe against a real venue.

**Consequence: a durable attempt ledger is not required for correctness
under virtual execution, and P3 does not block activation.** It becomes
mandatory the moment execution touches an external venue.
