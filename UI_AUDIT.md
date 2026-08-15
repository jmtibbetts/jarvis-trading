# UI_AUDIT.md — in progress

Deliverable required by §42/§136 of
`JARVIS_MASTER_UI_UX_HELIUS_WALLET_ALPHA_PROMPT.md`. Started 2026-08-15.
**This file is a work log, not a finished audit** — the sections below are
filled as each is actually verified, because an audit that asserts more
than it checked is worse than none.

---

## Baseline recorded

| | |
|---|---|
| Branch | `main` (working directly; the operator pushes here) |
| Commit at start | `6c6c9b5` |
| Python suite | **1,841 passed, 17 skipped** |
| `npm run check` | **30 errors, 0 warnings, 6 files** — all pre-existing |
| `npm run build` | clean, ~1.0s |

Re-measured after the silent-catch sweep: Python **1,853 passed, 17
skipped**; `npm run check` **30 errors, 6 files** — the same 30, zero
added; `npm run build` clean.

The 30 typecheck errors predate this work and live in `api.ts` (3),
`LearningPanel.svelte` (18), `Ops.svelte` (4), `Brief.svelte` (2),
`Charts.svelte` (2), `PositionsPaper.svelte` (1). None were introduced by
recent changes; each new panel was checked against this baseline and added
zero.

---

## P0 findings

### 1. Cluster counts read as independent wallet counts — FIXED (`9318971`)

§117, and listed under P0 in §129. `coordination_score` counted DISTINCT
WALLETS, so one actor splitting a position across three addresses
manufactured its own consensus signal. Now gated and scaled on
independent clusters, reporting both numbers. Pinned by a test where a
three-address single actor scores exactly 0.0.

### 2. Research boundary was documented but not enforced — FIXED (`9318971`)

§116. `WALLET_ALPHA` records could have entered `CRYPTO_MAJORS`
populations with nothing to stop them. Records are now stamped and
`assert_not_majors_population()` raises at the boundary rather than
filtering — silently dropping contaminated records would hide the wiring
mistake that produced them.

### 3. Silent-catch audit — FIXED

**89 `.catch()` sites across 14 files.** Every silent one is gone; the
remaining nine all name what failed. `PositionsPaper`'s `liveLoadFailed`
was the right idea for one feed and is now the mechanism for all of them.

What was added:

| Piece | File | Role |
|---|---|---|
| `ApiError` | `frontend/src/lib/api.ts` | Carries `status`; `status 0` = never reached the server. Every request goes through one `request()` helper. |
| `DataState` + `classify()` | `frontend/src/lib/dataState.svelte.ts` | §29/§64 vocabulary, and the mapping from status to state. |
| `FeedTracker` | same | Per-feed state, keeps last-good, ages it. |
| `StateBadge` | `components/StateBadge.svelte` | Header badge, silent when READY. |
| `StateNote` | `components/StateNote.svelte` | Body line; drop-in for `<div class="empty">`. |
| `status` prop | `components/Panel.svelte` | One prop puts a badge on any panel. |

**States claimed, and only these:** `loading` `ready` `empty` `stale`
`degraded` `error` `not_configured` `unsupported`. §64 also lists LIVE /
CONNECTING / FRESH / DELAYED / PLAN_UNAVAILABLE — deliberately **not**
implemented, because they describe streaming feeds and this is polled
REST. A state the loader cannot detect is a new lie, not a fix.

Status mapping (follows how `app/routers/intel.py` actually raises):

| HTTP | State | Reasoning |
|---|---|---|
| 0 (no response) | `error` | "API unreachable", distinct from any server answer |
| 401 / 403 | `not_configured` | credentials missing or rejected |
| 404 / 501 | `unsupported` | not on this deployment |
| 429 / 503 | `degraded` | upstream provider had nothing — not a JARVIS fault |
| other | `error` | |
| payload `configured: false` | `not_configured` | backend says so explicitly |

**Verified in the running app**, not just typechecked. Forcing
`/api/positions/with-signals` to 503:

- with no prior success → `DEGRADED — Positions unavailable: broker
  unreachable · N consecutive failures`. Previously: *"No open live
  positions"*, which on a trading desk reads as **you are flat**.
- with a prior success → `STALE · 13s ago`, all 25 positions still on
  screen and correctly labelled as not current.
- Open Orders, equity and the other feeds kept loading — one dead
  endpoint no longer blanks its neighbours.

**Regression found and fixed during verification.** The first cut passed
each panel's current `$state` into the loader as its "previous value".
That read the state synchronously inside an `$effect` that then wrote it
back, so every load re-fired the effect: **34 requests in 10 seconds
against a 20-second poll** — a §3 request storm introduced by the fix
itself. `FeedTracker` now holds last-good in a plain `Map` outside the
reactive graph. Re-measured: **1 request in 36 seconds.**

Two known gaps, deliberately not claimed as done:

- The KPI tiles above Live Positions still render `—` for equity/cash
  when the fetch fails. §29 reserves `—` for genuinely inapplicable
  fields. The panel directly beneath them now says DEGRADED, so the
  screen is not misleading, but the tiles themselves are not yet stateful.
- `Intelligence.svelte` has ~30 body-level empty states. The asserting
  ones ("X unavailable", which claimed a cause they could not know) were
  replaced; the genuinely-empty ones were left alone, and every panel in
  the file now carries a header badge.

---

## Backend → UI parity gaps

Confirmed present in the backend with **no UI surface at all**:

| Capability | Module | API route | UI |
|---|---|---|---|
| Wallet intelligence (whale, smart-money, cluster, coordination, copy-candidate) | `lib/wallet_intel.py` | none | none |
| Token pricing / USD coverage | `lib/token_pricing.py` | none | none |
| Helius client health + per-endpoint metrics | `lib/helius_client.py` | none | none |

Surfaced during this session:

| Capability | Route | UI |
|---|---|---|
| Concentration limits per book | `/concentration/status` | Positions → Concentration Limits |
| On-chain MVRV cycle gauge | `/onchain/context` | Intelligence → cryptodesk |
| DEX discovery | `/dex/discovery` | Intelligence → cryptodesk |
| Wallet-flow collector health | `/wallet/activity/status` | Ops → Wallet Flow |

---

## Still to do

Everything from §129 P1 downward: Helius/wallet-alpha surfaces, the Daily
Brief command centre (§130), the Crypto Desk IA (§131), the workstation
interaction model (§49–67), and the visualization primitives (§125).
