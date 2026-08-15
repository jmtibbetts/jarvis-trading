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

**Final: `npm run check` is at 0 errors, 0 warnings, 0 files.** See
"P0 finding 4" — treating the 30 as a baseline was itself the mistake.

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

### 4. The typecheck baseline was hiding real bugs — FIXED

The 30 typecheck errors were recorded as "pre-existing" and used as a
regression guard: *did this session add any?* That is a useful question
and the wrong one to stop at. A permanently-red check is a check nobody
reads, and §3 explicitly lists **"components rendering but containing
wrong data"** as a thing to hunt. Four of the 30 errors *were* that bug,
sitting in the report the whole time.

`Pill` takes a `label` prop and had no `children` snippet, but four call
sites passed content as children:

```svelte
<Pill tone="neutral">{payload.symbol}</Pill>
```

The content was silently discarded and the pill rendered as an empty
coloured box. **Confirmed in the running app** — the Charts header pill
measured 12px wide with `textContent === ""` where `BTC/USD` belonged.
Brief's "releases today" pills were empty for the same reason, as was its
"no scheduled releases today" fallback. `tone="info"` also matched no CSS
rule, so those pills were unstyled on top of being blank. After the fix
the same pill measures 53px and reads `BTC/USD`.

The rest, and why each mattered:

| Count | Error | What it actually was |
|---|---|---|
| 4 | `'children' does not exist` / `"info"` not assignable | The blank-pill bug above. |
| 9 | `Duplicate identifier 'CalibrationRow'`, dup object key | **Two** type declarations and **two** `api.calibration()` entries for one endpoint. In an object literal the last key wins, so one was dead code; and the duplicate name made TS resolve `CalibrationRow` to the narrow shape, so `LearningPanel`'s reads of `.timeframe` / `.strategy` / `.band` had *no* checking. Verified against the live payload and consolidated onto the shape the server actually returns. |
| 12 | `'ev' is of type 'unknown'` | `promo` was `$state<any>`. Typing it from the live payload immediately caught a **real mismatch**: the panel renders `champion.variant`, but `champion` is a promotion artifact object, not a string. |
| 4 | `Property 'resolved' does not exist` | Spreading an index-signature type into an object literal drops it, so `resolved`/`pending`/`abstained` were unchecked in the Feature Corpus panel. |
| 1 | `string \| null` not assignable | A nullable `direction` rendered a blank pill instead of saying "unknown". |

No runtime behaviour changed for the `CalibrationRow` duplicate — both
`api.calibration()` definitions hit the same URL and type parameters are
erased. The cost was the *checking* that stopped happening around it.

---

## Backend → UI parity gaps

All three P1 gaps are closed:

| Capability | Module | API route | UI |
|---|---|---|---|
| Wallet intelligence (whale, exchange flow, cluster, coordination) | `lib/wallet_intel.py` | `/wallet/intel` | Crypto Desk → Wallet Alpha |
| Token pricing / USD coverage | `lib/token_pricing.py` | same response | same panel (coverage strip) |
| Helius client health + per-endpoint metrics | `lib/helius_client.py` | `/helius/health` | Ops → Helius API |

`smart_money_score` and `copy_trade_candidate` are still unsurfaced —
both need a per-wallet trade history the collector does not yet retain
(`_store` keeps symbol/metric/value and drops counterparty and mint), so
a panel for them would have nothing honest to show yet.

`/wallet/intel` is on an explicit button, not a poll. It makes live
Helius calls — transfers per wallet, one batched identity call, one
funded-by per wallet — bounded at 12 wallets and 100 transfers each. Put
on a 30s timer it would spend the plan's quota on an unread tab.

Verified against the live API: 96 transfers across 2 wallets, 87.5%
priced (69 helius + 15 peg, 12 abstained rather than guessed), 21 whale
candidates, 8 exchange flows, 2 funder clusters, coordination 0.0 with
the reason given. Every record stamped `WALLET_ALPHA`, and the panel
prints raw wallet count *and* independent cluster count side by side
(§117) rather than the flattering one.

**A gap the P0 work missed, found here.** The SPA fallback answers any
unmatched path with `index.html` and a **200**, so a route the running
server does not have arrived as a success and `res.json()` threw a bare
`SyntaxError` about an unexpected `<`. `request()` now checks the
content-type and raises a proper `unsupported`, which renders as:
*"/helius/health is not a route on the running server (got text/html) —
it may need a restart to pick up new endpoints."* Confirmed on screen
against the operator's own running instance.

Surfaced earlier in this session:

| Capability | Route | UI |
|---|---|---|
| Concentration limits per book | `/concentration/status` | Positions → Concentration Limits |
| On-chain MVRV cycle gauge | `/onchain/context` | Intelligence → cryptodesk |
| DEX discovery | `/dex/discovery` | Intelligence → cryptodesk |
| Wallet-flow collector health | `/wallet/activity/status` | Ops → Wallet Flow |

---

## Still to do

§129 P2 downward: the Daily Brief command centre (§130), the Crypto Desk
IA (§131), the workstation interaction model (§49–67), and the
visualization primitives (§125).

Carried forward from the sections above, so it is not lost:

- KPI tiles still render `—` on a failed fetch (P0 note 3).
- `smart_money_score` / `copy_trade_candidate` have no surface, because
  the collector does not retain the per-wallet history they need.
- `HELIUS_WATCH_WALLETS` is **empty** on this deployment, so Wallet Alpha
  correctly reports NOT CONFIGURED. The pipeline was verified by
  temporarily pointing it at two public Solana addresses; nothing was
  written to the watchlist.
- Execution past `simulateTransaction` still needs a funded Solana
  keypair. Blocked on the operator by design — no signer in the repo.
