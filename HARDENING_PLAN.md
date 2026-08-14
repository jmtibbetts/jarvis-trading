# v8 Hardening Plan — the agreed, integrated sequence

Reviewed 2026-08-14. Sources: `JARVIS_CLAUDE_IMPLEMENTATION_PLAN.md` (every
load-bearing claim verified at HEAD — see Verification below),
`DATA_PLATFORM_PLAN.md`, `CLAUDE_JARVIS_PREDICTIVE_GUIDE.md`, plus the
operator decisions recorded in this file. This is the working plan of
record; the source documents remain reference detail.

---

## RESTORE POINT — how to get back to exactly this state

The commit this plan ships in is tagged **`pre-hardening-baseline`** and
pushed to GitHub. It represents the last state BEFORE any Phase 0
behavior change. If the upgrades don't earn their keep, this is the
return address:

```bash
# See what the baseline was
git log pre-hardening-baseline -1

# Nuclear option: put main back exactly here (destroys later history!)
git checkout main
git reset --hard pre-hardening-baseline
git push --force-with-lease jarvis main

# Surgical option (preferred): revert the hardening commits but keep history
git revert <first-hardening-commit>..HEAD
git push jarvis main
```

Also restore the database schema only if a migration proves problematic —
migrations in this plan are additive (new columns/tables), so old code
runs against the new schema without harm. Data collected during the
experiment (candidates, shadow scores, outcomes) survives a code revert
and remains usable evidence either way.

**Judgment window:** run the hardened system ≥2 weeks or ≥300 resolved
candidates per gate arm, whichever comes later. The side-by-side
scoreboard (below) is the evidence for keep-vs-revert — not vibes.

---

## Verification (claims checked against code before acceptance)

| claim | verified at | status |
|---|---|---|
| Live gate on measured-inverted composite | `execute_signals.py:228` | CONFIRMED |
| Composite fed to Kelly as win probability | `:367` + `risk_manager.py` | CONFIRMED |
| Risk-engine crash fails OPEN into a trade | `:479-482` | CONFIRMED |
| Post-risk 1–2× conviction multiplier | `:493-495` | CONFIRMED |
| Lifecycle 0.50/0.25 never applied | `:308` (only `<=0` checked) | CONFIRMED |
| Score earns leverage | `leverage_policy.py:121` | CONFIRMED |
| Hard-coded market hours **break Nov 1 (DST)** | `:221` | CONFIRMED |
| Unknown side defaults LONG | `trade_side.py:33` | CONFIRMED |
| `0.0.0.0` bind + wildcard CORS | `main.py:163,323` | CONFIRMED |

Already done before this plan: paper-side immutable stops (f2a0592),
secret pre-commit hook, history rewrite, shadow variants A/B/C,
candidate + counterfactual pipeline, R-multiple repair, LLM router,
Kraken read-only sync, v2 feature datasets (building).

---

## Operator decisions on record

1. **The inverted gate loses live authority** — resolved by the operator
   supplying the implementation doc and confirming after the plain-language
   consequences were stated. Live trade count will drop and stay
   conservative until evidence earns it back. (Note: the "live" executor
   currently trades the Alpaca *paper-tier* account, so the drop costs no
   real dollars today.)
2. **Old vs new is a measured experiment, not a replacement** — the legacy
   gate is demoted to a recorded shadow arm, not deleted (design below).
3. **Rotate the pre-rewrite Alpaca key** — operator action; code cannot
   do it. Standing until done.

---

## The gate experiment (operator-requested side-by-side)

Not a dual-executor design — two executors on one account would fight
over budget, collide on symbols, and trip wash-trade guards, making the
arms incomparable. Instead, both gates run as **verdict recorders over
the same candidate stream**:

- Every candidate gets two verdicts at birth, stored immutably:
  - `gate_legacy`: would `coalesce(composite,confidence) >= live_min_score`
    have taken it?
  - `gate_v8`: does validity + measured-expectancy take it?
- The executed book follows `gate_v8`. The legacy arm's picks are judged
  by the same counterfactual resolver that already grades rejected
  candidates (same rules, same stop-first conservatism).
- **Scoreboard** (Learning tab + API): per arm — trades taken, win rate,
  avg P&L, avg MFE/MAE, net R. Same market, same candidates, same
  resolution rules. Known bias stated on-panel: non-executed picks
  resolve with perfect fills; this applies to both arms' shadow picks
  equally, so the head-to-head stays fair.
- **Promotion/demotion is symmetric**: if legacy out-selects v8
  out-of-sample over the judgment window, it earns authority back through
  the same §4.3 promotion test; if v8 wins, legacy retires with a receipt.
- Tie-breaker if ever needed: point Auto Sim at the legacy arm for
  managed-execution realism on both sides.

Also mechanical bugs are **not** part of either arm: fail-open sizing,
composite→Kelly, conviction multiplier, side-default, DST clock get fixed
unconditionally — otherwise the comparison measures bugs, not gates.

---

## PHASE 0 — capital-safety semantics + the experiment *(next)*

Order chosen so safety rails exist before risky edits:

1. Hermetic test guard: pytest → temp DB; operator DB refused
   structurally (P0.17; this week's fixture leak proved the need).
2. Kill-switch cancel fix (P0.14) + localhost bind / restricted CORS
   default (P0.15). Small, isolated.
3. Strict side parsing — unknown ≠ long for any order path (P0.13).
4. `lib/market_clock.py` from Alpaca's clock+calendar API (holidays,
   half-days), crypto 24/7, cached; replaces the hand-coded UTC check
   **before it silently breaks on Nov 1** (P0.9).
5. Gate experiment plumbing: dual verdict columns + scoreboard endpoint
   (design above) — BEFORE flipping authority, so day one of the new
   gate is day one of the comparison.
6. Live gate flip: composite demoted to diagnostics/shadow; eligibility =
   validity gates; decision = measured expectancy with robust lower bound
   (P0.1 + P0.11); UNKNOWN expectancy → paper/sim only, with explicit
   opt-in env override (P0.10).
7. Delete confidence→Kelly; Kelly (if kept) feeds from calibrated
   outcomes with sample floors, uncertainty shrinks size, fractional cap
   (P0.2). Fail CLOSED on risk-engine error (P0.3). Remove conviction
   multiplier — execution may only ever reduce approved size (P0.4).
8. Lifecycle multipliers applied to risk budget (0.50/0.25 real), live
   state judged on live-weighted evidence (P0.5).
9. Paper: rejection never becomes a flat-size trade (P0.7); stop-risk-first
   sizing for every asset class (P0.8); `max_safe_leverage()` replaces
   score-earned leverage — stop set from structure first, leverage
   derived, `0x/NO_TRADE` valid (P0.6).
10. Live-side immutable stop provenance: planned/actual entry, initial
    stop as placed, approved qty/notional/risk persisted at birth;
    R and learning read only these (completes P0.12).
11. **UI decision-truth (pulled forward from Phase 6 by agreement):**
    signal cards lead with `TRADE / WATCH / NO_TRADE / SHADOW`, net EV,
    robust lower bound, risk-at-stop; exact labels replace the generic
    "confidence" everywhere (`LLM stated confidence`, `calibrated win
    rate (n)`, `evidence composite`); inverted/non-predictive scores say
    so on the card; gate scoreboard panel ships with the flip. The UI
    must tell the truth about the engine the day the engine changes.

Every item lands with its invariant test; the §25 list (22 invariants)
is the Phase 0 exit checklist. One item per commit series, app runnable
after each.

## PHASE 1 — typed decision/risk pipeline
Explicit Evidence/Edge/Risk/Execution types so `confidence` can never
mean three things again; single instrument registry (extend `get_spec`);
one risk engine both books call; normalized order plan; ccxt adopted
narrowly for venue/symbol metadata only.

## PHASE 2 — learning correctness
Historical double-count elimination; live/replay evidence split
everywhere lifecycle/expectancy read; conservative expectancy tiers
(ROBUST_TRADE / TENTATIVE / NO_TRADE / UNKNOWN); shadow-promotion
framework formalized over the running variants; selection-bias dashboard
aligned to the actual live gate.

## PHASE 3 — raw data foundation *(per DATA_PLATFORM_PLAN, merged)*
Canonical events, three timestamps, feature versioning, BookHealth with
abstention, bounded queues + drop counts, measured bytes/day before any
storage migration, immutable training snapshots.

## PHASE 4 — Wave A data *(amended by agreement)*
- **cryptofeed prototype** (pulled forward from Phase 5): normalized WS
  behind our canonical interface — trades/L2/funding/OI/liquidations,
  Kraken + Kraken Futures included; run beside existing OKX/Crypto.com
  paths; promote per-venue only on measured parity.
- **CFTC COT** (weekly positioning; release-availability joins).
- **FINRA Daily Short Sale Volume** (daily flow ratio; never labeled
  "short interest").
- SEC FTD: cheap, shadow context, third.
- **Deferred**: EIA until energy futures actually enter the book;
  OpenBB skipped (research breadth already covered 4 ways);
  hummingbot = architecture reference only.
All shadow-first; §23 feature-judgment test before any authority.

## PHASE 5 — remaining crypto feed promotion
Per-venue cutover of whatever cryptofeed proved at parity; retire
bespoke adapters only after the comparison, venue by venue.

## PHASE 6 — UI structure *(the refactors, after frontend tests exist)*
Grouped nav (TRADE/INTELLIGENCE/REVIEW/SYSTEM, hashes preserved);
Command Center reorganized around the five operator questions; split
Intelligence.svelte into four desk components + split api.ts into domain
modules — behavior-neutral, landed behind the §17 frontend tests, never
sharing a commit series with behavior changes; primitive/token migration
opportunistic (Phase H already delivered DataGrid/Modal/tokens);
accessibility fixes (§14.8); stale-build banner verify (§14.9).

## PHASE 7 — backend modularization / CI / docs
routes.py split by domain; database.py responsibilities separated
carefully; CI = pytest + frontend check/build + secret scan; version
single-source; docs status headers; legacy static cleanup.

## PHASE 8 — model promotion
v2 challenger evaluation is already running (read-only, allowed);
promotion of ANY model/scoring variant requires chronological OOS,
after-cost improvement, then shadow, then the same gate-experiment
treatment the legacy score is getting. No exceptions — the v1 null and
the flattery bug in our own harness are the reasons why.

---

## How the whole plan is judged

- **Primary**: the gate scoreboard after the judgment window — measured
  net R and selection quality of v8 vs legacy on identical candidates.
- **Secondary**: the 22 invariants stay green; no fail-open path exists;
  R/learning numbers derived only from immutable initial risk.
- **If the upgrades don't do anything**: the scoreboard will say so in
  numbers, and `pre-hardening-baseline` is one command away. Data
  collected meanwhile keeps its value either way — evidence is never a
  loss.
