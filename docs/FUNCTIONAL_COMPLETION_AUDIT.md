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
