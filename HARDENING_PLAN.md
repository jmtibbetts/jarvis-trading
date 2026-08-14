# v8 Hardening Plan — reviewed, verified, sequenced

Source: `JARVIS_CLAUDE_IMPLEMENTATION_PLAN.md` (1,978 lines, 26 sections),
reviewed 2026-08-14 against HEAD with every load-bearing claim spot-checked
in code before acceptance. This file is the working plan; the source doc
remains the reference for full detail.

---

## Verification results

The document's P0 claims were checked against the actual code, per its own
rule that code is the source of truth. **All verified claims confirmed:**

| claim | verified at | status |
|---|---|---|
| P0.1 live gate on inverted composite | `execute_signals.py:228` — `coalesce(composite_score, confidence) >= 55` | CONFIRMED |
| P0.2 composite → Kelly probability | `:367` writes composite into `"confidence"`; `risk_manager` Kelly consumes it | CONFIRMED |
| P0.3 risk-engine crash fails open | `:479-482` — exception → budget from confidence | CONFIRMED |
| P0.4 post-risk conviction multiplier | `:493-495` — 1–2× from score AFTER sizing | CONFIRMED |
| P0.5 lifecycle 0.50/0.25 never applied | `:308` — only `<= 0` checked | CONFIRMED |
| P0.6 score earns leverage | `leverage_policy.py:121` `conviction_leverage(score…)` | CONFIRMED |
| P0.9 hard-coded market hours | `:221` — 13:30–20:00 UTC; **breaks Nov 1 when DST ends** | CONFIRMED |
| P0.13 unknown side defaults LONG | `trade_side.py:33` | CONFIRMED |
| P0.15 `0.0.0.0` + wildcard CORS | `main.py:163,323` | CONFIRMED |
| P0.7/P0.14 | consistent with code; verified during implementation | ACCEPTED |

**Already done this week (the doc partially predates it):**
- P0.12 immutable initial stop — done on the PAPER side (`initial_stop_loss`,
  commit f2a0592); remains open for the live executor and TradeOutcome.
- P0.16 secret boundary — pre-commit content scanner exists; history was
  rewritten; rotation recommended. CI scanning still open.
- P0.17 hermetic tests — today's TEST-fixture leak into the dev DB proved
  the doc's exact point; per-test cleanup exists, the STRUCTURAL guard
  (pytest → temp DB, operator DB refused) is still open.
- §4.3 shadow promotion — variants A/B/C + selection-bias measurement are
  live and accumulating; the doc's promotion framework slots on top.
- §9/§11-13 data platform — largely overlaps `DATA_PLATFORM_PLAN.md`
  (raw events, three clocks, BookHealth, feature versioning already
  planned there). The genuinely new items: CFTC COT, FINRA short-sale
  volume, SEC FTD, EIA.
- Phase 8 groundwork — path_features_v2 datasets are building now;
  evaluation-only, promotes nothing, so it does not violate the ordering.

---

## What changes for live trading — stated plainly

Phase 0 is not a refactor; it changes what the bot trades. After it:

- The composite score stops gating live capital (it is measured inverted:
  80+ scores win 30% while <60 wins 53%). Eligibility becomes validity +
  measured expectancy.
- UNKNOWN expectancy stops reaching live capital (paper/sim only).
- Lifecycle REDUCED/EXPERIMENTAL actually shrink risk budgets.
- Risk-engine failure means NO trade, not a confidence-priced one.

Net effect: **live trade count drops substantially and becomes
conservative** until measured evidence earns it back. That is the point,
and it resolves the standing "the gate trades on an inverted score"
decision in the direction the operator's own document specifies.

---

## Sequencing — integrated with in-flight work

### PHASE 0 — capital-safety semantics *(next; one focused block)*
All 14 items, each with an invariant test, in this order:
1. Kill-switch cancel fix (P0.14) + localhost/CORS default (P0.15) — small, isolated.
2. Hermetic test guard (P0.17) — BEFORE the risky changes, so everything
   after is tested safely.
3. Strict side parsing (P0.13).
4. Market clock from the broker calendar — Alpaca's clock/calendar API,
   crypto 24/7 (P0.9). Time-boxed: the current code breaks on Nov 1.
5. Remove composite live gate → validity gates + expectancy decision (P0.1).
6. Delete confidence→Kelly; expectancy-fed fractional Kelly or nothing (P0.2).
7. Fail-closed on risk-engine error (P0.3). Remove conviction multiplier (P0.4).
8. Apply lifecycle multipliers to risk budget (P0.5).
9. UNKNOWN-expectancy live policy + robust lower-bound tiers (P0.10, P0.11).
10. Paper: no flat-size fallback after rejection (P0.7); risk-first sizing
    everywhere (P0.8); `max_safe_leverage()` replaces score-leverage (P0.6).
11. Live-side immutable stop provenance completing P0.12.

End state: the 22 invariants in §25, each as a test.

### PHASE 1 — typed decision pipeline *(after 0 is green)*
Explicit types so "confidence" can never silently mean three things again;
unified instrument registry (get_spec exists — extend, don't duplicate);
one risk engine; normalized order plan.

### PHASE 2 — learning correctness
Double-count elimination, live/replay separation in lifecycle, stop
provenance in R everywhere, conservative expectancy tiers, and the shadow
promotion framework formalized over the already-running variants.

### PHASE 3-4 — raw events + free differentiated data
Per `DATA_PLATFORM_PLAN.md` P1/P2 merged with the doc's Wave A: CFTC COT
(weekly, futures positioning), FINRA daily short-sale volume, SEC FTD,
EIA energy. All shadow-only; release-timestamp discipline so replay never
sees data before its publication time.

### PHASE 5 — cryptofeed prototype
Prototype beside the existing feeds, promote per-venue only on measured
parity. CCXT for metadata breadth if needed. No blind adoption.

### PHASE 6-7 — UI information architecture + backend modularization
Grouped nav, decision-first signal cards ("what/why/risk" hierarchy),
split the monolith components, split routes.py by domain, CI (tests +
build + secret scan), docs status headers, dead-asset cleanup.

### PHASE 8 — model promotion
Already partially in motion (v2 challenger datasets building). Nothing
promotes without chronological OOS + after-cost improvement — same bar the
v1 null was judged by.

---

## Deviations from the source document

1. **P0.9 implementation**: use Alpaca's own clock+calendar endpoints as
   primary (already authenticated, includes holidays/half-days) with a
   cached fallback; do not hand-build an exchange calendar.
2. **§9 data waves fold into the existing DATA_PLATFORM_PLAN** rather than
   forming a parallel track — one data roadmap, not two.
3. **Phase 8 evaluation is allowed to run early** (it already is) because
   it is read-only research; only PROMOTION waits for the trustworthy
   pipeline.
4. **P0.16 rotation**: flagged to the operator (the exposed Alpaca paper
   key from the pre-rewrite history); code cannot rotate it.

---

## Working style (per §26, adopted)

Each item: inspect → state defect → write the test → smallest coherent fix
→ targeted tests → full suite → commit. One phase per commit series, app
runnable after every commit, no giant cleanup commits.
