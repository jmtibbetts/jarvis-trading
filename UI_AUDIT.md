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

### 3. Silent-catch audit — OPEN

`.catch(() => null)` is used throughout the section `loadAll()` functions.
It collapses **request failed** into **genuinely empty**, which §4 calls
out as particularly dangerous in a trading dashboard and §29/§64 require
to be distinguishable. `PositionsPaper.svelte` already does the right
thing for the live book (`liveLoadFailed` keeps last-good data on screen
rather than flashing an empty state that reads as "everything closed") —
that pattern is the model to generalize.

Not yet swept. Next session's first task.

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
