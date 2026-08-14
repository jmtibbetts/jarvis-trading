# v8 Hardening Plan — MERGED plan of record (v2, pending operator approval)

Sources: `JARVIS_CLAUDE_IMPLEMENTATION_PLAN_UPDATED.md` (3,206 lines — the
original plan plus the futures expansion), the original reviewed plan, and
the completed work below. Merged 2026-08-14. **Status: DRAFT — awaiting
operator approval before any item from the new sections is implemented.**

---

## RESTORE POINT (unchanged)

Tag `pre-hardening-baseline` on GitHub. Judgment window for the gate
experiment: >=2 weeks or >=300 resolved candidates per arm. Revert
commands in the tag message. All migrations remain additive.

---

## STATUS — what is already done and verified

### PHASE 0 — capital-safety semantics: COMPLETE (9 commits, pushed)
All 17 P0s closed: hermetic tests (b826cad), kill-switch cancel +
loopback/CORS (0382a3b), strict side — including the "Bearish"-buys-a-long
bug (58a3673), venue market clock replacing the Nov-1 DST bomb (61dd358),
the gate flip with the legacy-vs-v8 experiment recording on every
candidate (577ace6), measured-Kelly / fail-closed / conviction removal /
real lifecycle multipliers (f7adc1d), risk-first paper sizing +
stop-derived leverage + no flat fallback (8212602), immutable approved
stops live-side (8532e55), decision-first UI cards + exact labels + gate
scoreboard (57f246f). 1,523 tests green. The 22 end-state invariants each
have a pinning test where implemented; the remainder land with their
phases.

### PHASE 1 — typed pipeline: IN PROGRESS
- DONE (e5704ec): decision types (ObservedEvidence / MeasuredEdge /
  RiskDecision / OrderPlan / TradeDecision), gate.decide() typed,
  OrderPlan.within() invariant check at the last gate — which killed the
  round-up-to-1-share enlargement on arrival.
- REMAINING: unified risk engine (one solve-qty-from-risk core for
  live/paper/sim), instrument identity registry (canonical/venue_symbol/
  asset_class replacing the normalize_symbol/_both_formats/
  normalize_crypto_symbol triplication), direction+leverage parsing dedup
  (trade_side as sole authority).
- SCOPE NOTE: the updated doc's §6.1 futures CONTRACT MASTER is a
  different, larger thing than Phase 1's identity registry — it belongs
  to Phase 4B below, exactly where the doc sequences it.

### Running continuously (no action)
Gate experiment scoreboard; candidate/counterfactual resolution; shadow
variants; Kraken read-only sync; execution/slippage sampling.

### Path-model v2 verdict (RESOLVED 2026-08-14 — null, protocol complete)
Both v2 datasets trained and judged by the beats-the-BEST-baseline rule:
- 1H v2: 208,062 rows, 5.6y span — stop-first AUC 0.505 (GB) / 0.496
  (MLP) vs 0.5 prior; MFE: nothing beats the median.
- 15m v2: 319,494 rows, 3.1y span — stop-first AUC 0.504 / 0.497; MLP
  MFE MAE 2.576 vs median 2.595 (+0.7%, under the 2% materiality bar).
Four consecutive honest nulls (v1 + v2 features, both timeframes). The
OHLCV-derived feature space — confirmation TA, extension/lateness, vol
ratio, tf-conflict, session — carries no exploitable intrabar path signal
under stop-first-conservative labeling. Phase 8's v2 challenger lane stays
open only for NEW information (order-flow/depth from Phase 3 raw capture,
positioning from 4A), not for re-arrangements of these inputs. The
measured edge remains in selection (expectancy gate), and effort follows
the evidence.

---

## MERGED FORWARD PLAN

### PHASE 1 (finish) — as above. No change from prior plan.

### PHASE 2 — learning correctness *(unchanged)*
Double-count elimination; live/replay separation; stop provenance in
every R read (the immutable columns exist; consumers migrate); expectancy
tiers; §4.3 promotion framework formalized; selection-bias dashboard
aligned to the actual live gate.

### PHASE 3 — raw data foundation *(unchanged, merged with DATA_PLATFORM_PLAN)*
Canonical events, three clocks, feature versioning, BookHealth +
abstention, backpressure with drop counts, measured bytes/day before any
storage migration, immutable snapshots.

### PHASE 4A — official differentiated data *(EXPANDED by the update)*
Original: CFTC COT, FINRA short-sale volume, SEC FTD, EIA.
Update adds: CFTC TFF/CIT report variants, USDA (WASDE/NASS/FAS) for
agriculture, NOAA/NWS/NHC weather for energy/ags, Treasury auction
context for rates, OPEC/USGS slow context. All shadow-only,
release-timestamp joins, replay never sees pre-release data.
**Adopted with staging**: CFTC + FINRA + EIA first (instruments the paper
book already trades), USDA/NOAA with 4C's agriculture/energy engines,
Treasury with the rates engine. No feed lands before its consumer exists.

### PHASE 4B — futures contract / curve foundation *(NEW — adopted)*
The strongest addition in the update, and it fixes a live correctness
hole: futures outcomes are currently recorded against yfinance
continuous-style symbols (HG=F) with NO contract identity, no roll
provenance, no expiry awareness — a standing violation of the doc's own
rule 3, feeding the learning ledger. Adopted in full:
- contract master (root vs contract vs continuous; expiry / first-notice /
  last-trade; multiplier/tick/margin as REGISTRY data — the HG=F
  25,000x-multiplier incident is exactly what rule 4 prevents);
- versioned continuous series (unadjusted / back-adjusted / ratio) with
  roll provenance; liquidity-aware per-product roll policy;
- curve snapshots; calendar-spread identity with combined-position risk
  (never leg-by-leg double risk);
- delivery-risk hard blocks and roll warnings (matters less while futures
  are paper-only, but the learning data must be right NOW);
- settlement vs last-trade kept distinct.

### PHASE 4C — futures intelligence *(NEW — adopted incrementally)*
Sector engines with source health, point-in-time releases, sector
snapshots, curve/positioning/fundamentals context, shadow features, OOS
ablation before promotion. **Staged by what the book actually trades**:
1. Energy (CL/NG — EIA), 2. Metals (GC/SI/HG — COT), 3. Equity index
(ES/NQ), then Rates / Agriculture / FX / Livestock / Softs as instruments
enter the universe. Eight engines exist in the doc; they earn
implementation order by book relevance, not by list order.

### PHASE 5 — crypto feed normalization *(kept, with the standing amendment)*
The update moves crypto microstructure to Wave D; the operator is
crypto-primary, so the PRIOR agreed amendment stands: the cryptofeed
prototype runs alongside Wave A, not after the futures waves. Per-venue
promotion on measured parity only. (Flagged as an explicit deviation from
the updated doc's wave order.)

### PHASE 6 — UI information architecture *(EXPANDED by the update)*
Original items (grouped nav, Command hierarchy, splits, primitives,
accessibility) plus the new Futures Desk: overview, sector tabs, curve
and spread views, COT/EIA panels, catalyst calendar, contract/roll
monitor, product-aware signal cards, source-health states, futures UI
tests. **Adopted with staging**: a UI view ships only when its engine
(4B/4C) feeds it real data — no empty desks. Decision-first cards and
exact labels are already live from Phase 0.

### PHASE 7 — backend modularization / CI / docs *(unchanged)*

### PHASE 8 — model promotion *(unchanged)*
v2 challenger evaluation may run (read-only); promotion only via
chronological OOS + after-cost improvement + shadow.

---

## Data-source waves (merged)

- Wave A: CFTC COT + FINRA SSV + EIA + **cryptofeed prototype**
  (deviation: operator is crypto-primary).
- Wave B: futures market structure (4B foundation data).
- Wave C: sector intelligence feeds (USDA/NOAA/Treasury) with their 4C
  engines.
- Wave D: remaining crypto microstructure depth.
- Wave E: paid/ablation-gated additions only after measured need.

## Deviations from the updated document (for approval)

1. **Cryptofeed stays in Wave A** (doc says Wave D) — the operator's book
   is crypto-primary; futures are paper-only today.
2. **4C sector engines staged by book relevance** (energy, metals, equity
   index first) rather than all eight in doc order.
3. **4A feeds land with their consumers** — USDA/NOAA/Treasury wait for
   their sector engines rather than ingesting unread data.
4. **Futures UI ships behind its engines** — no view without live data.
5. Everything else adopted as written.

## Unchanged non-negotiables

Authority boundaries (§1), the 22+ invariants (§25), working style (§26),
release-aware joins everywhere, shadow-first for every new feature, the
§23 judgment test: every addition must change a decision measurably or it
is decoration.
