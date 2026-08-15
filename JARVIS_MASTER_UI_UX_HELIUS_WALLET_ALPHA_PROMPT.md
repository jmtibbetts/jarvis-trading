# JARVIS UI AUDIT + DAILY BRIEF + CRYPTO DATA SURFACE EXPANSION

You are working in:

`jmtibbetts/jarvis-trading`

Your mission is to perform a **full UI correctness, completeness, data-parity, visualization, and UX audit of JARVIS**, then actually fix the problems you find and substantially improve the Morning/Daily Brief and Crypto Desk.

This is NOT a cosmetic redesign.

JARVIS already has a mature backend, extensive data infrastructure, trading/risk systems, learning systems, Svelte frontend, charts, market intelligence, crypto derivatives, order books, macro data, institutional data, DEX discovery, on-chain data, futures intelligence, and numerous APIs.

The current problem is that the **UI has not kept pace with the underlying platform**.

Your job is to make the frontend accurately expose the useful information JARVIS already possesses, repair anything that silently does not work, add missing visualizations, and create a Daily Brief that functions as the operator's true morning command center.

---

# 0. DO NOT START CODING IMMEDIATELY

First inspect the repository in depth.

Read and understand at minimum:

- `ARCHITECTURE.md`
- `JARVIS_CLAUDE_IMPLEMENTATION_PLAN_UPDATED.md`
- `JARVIS_LIVE_LEARNING_DATA_PLATFORM.md`
- `DATA_PLATFORM_PLAN.md`
- `HARDENING_PLAN.md`
- `docs/UI_GUIDE.md`
- recent git history
- `frontend/src/App.svelte`
- all files under `frontend/src/lib/sections/`
- all files under `frontend/src/lib/components/`
- all files under `frontend/src/lib/stores/`
- `frontend/src/lib/api.ts`
- `frontend/src/app.css`
- backend routers under `app/routers/`
- `app/ws.py`
- `app/scheduler.py`
- `lib/morning_brief.py`
- all relevant `lib/*` market/intelligence/data modules
- all relevant jobs
- tests corresponding to each subsystem

Do not assume that the documentation perfectly matches current code. The code and tests are authoritative.

Also inspect recent commits carefully because JARVIS has changed substantially over the last several days.

---

# 1. SAFE DEVELOPMENT REQUIREMENTS

Before mutations:

1. Pull current `main`.
2. Record the current commit SHA.
3. Create a branch such as:

`ui-audit-brief-crypto`

4. Run the complete Python test suite and record the actual baseline.
5. Run:

```
cd frontend
npm run check
npm run build

```

6. Work in a safe non-live environment:
   - disable scheduler where appropriate
   - paper-only execution
   - trading kill switch active/paused
   - never send a real broker or crypto order
   - never mutate the operator's production database for tests
   - use temporary/test data where mutation is required
7. Never expose secrets from `.env` in logs, screenshots, commits, reports, or frontend state.

Do not weaken trading safety to make a UI demonstration work.

---

# 2. BUILD A REAL BACKEND → API → UI PARITY MAP

Before deciding what is "missing," create an inventory of the data JARVIS actually has.

For every important subsystem, determine:

```
SOURCE / MODULE
    ↓
STORAGE
    ↓
BACKEND ROUTE
    ↓
frontend/src/lib/api.ts TYPE + CLIENT METHOD
    ↓
FRONTEND COMPONENT / SECTION
    ↓
ACTUALLY RENDERS CORRECTLY?

```

Create a working audit table with columns:

| CapabilityBackend ModuleAPI Routeapi.tsUI SurfaceFreshnessStateProblem |
| ---------------------------------------------------------------------- |

Use states such as:

- COMPLETE
- PARTIAL
- BACKEND ONLY
- API ONLY
- UI BROKEN
- UI MISLEADING
- STALE
- NOT CONFIGURED
- SILENT FAILURE
- DUPLICATED
- NEEDS VISUALIZATION

Do not assume that the existence of a Svelte component means it works.

Do not assume an API wrapper means the data is visible.

Do not assume an empty UI means the backend has no data.

---

# 3. AUDIT EVERY UI SURFACE

Inspect and run every current section:

- Morning Brief
- Command Center
- Signals & Scanner
- Positions & Paper
- Charts
- Intelligence
- Smart Money
- Macro Desk
- Crypto Desk
- Performance & Learning
- Ops

For every section:

### Navigation

- link works
- keyboard shortcut works
- hash routing works
- reload returns correctly
- pop-out works
- panel pop-outs work
- back/forward navigation behaves properly

### Interaction

Test every:

- button
- tab
- filter
- selector
- symbol picker
- timeframe picker
- modal
- expandable row
- refresh control
- export control
- pop-out
- tooltip
- keyboard action
- form
- toggle
- alert action
- confirmation
- dangerous action

Do not trigger real trading.

### Browser/runtime correctness

Watch:

- JavaScript console
- network requests
- HTTP failures
- WebSocket reconnects
- Svelte warnings
- unhandled promises
- repeated polling
- request storms
- stale responses
- unexpected `null`
- layout overflow
- components rendering but containing wrong data

Audit at common desktop widths, especially:

- \~1280 px
- \~1440 px
- \~1920 px+

JARVIS is a desktop trading workstation. Prioritize information density and multi-monitor usability over mobile-style whitespace.

---

# 4. ACTIVELY SEARCH FOR SILENT FAILURE PATTERNS

Search the frontend for patterns such as:

```
.catch(() => null)
.catch(() => {})
catch {}
ComingSoon
TODO
FIXME
disabled
placeholder
hard-coded values
fallback values

```

Silent failure is particularly dangerous in a trading dashboard.

For every silent catch, decide whether the UI needs to distinguish:

- genuinely empty
- unavailable
- request failed
- stale
- not configured
- not supported
- still loading

NEVER translate unavailable data into `0`.

NEVER translate unavailable data into neutral sentiment.

NEVER make a failed data source visually indistinguishable from "nothing happened."

A trading UI must distinguish:

```
0 liquidations

```

from:

```
liquidation feed unavailable

```

Those are completely different statements.

---

# 5. VERIFY DISPLAYED VALUES AGAINST RAW ENDPOINTS

For every major card/table/panel:

1. Query the underlying endpoint.
2. Capture representative values.
3. Compare them directly with what the UI renders.
4. Trace transformations through `api.ts`.
5. Verify formatting and units.
6. Verify timestamps.
7. Verify symbol normalization.
8. Verify direction semantics.
9. Verify percentages vs decimal fractions.
10. Verify stale-data rules.

This is mandatory.

JARVIS has already experienced bugs where a perfectly healthy-looking empty UI was caused by query semantics rather than a genuinely empty dataset.

The audit must catch that class of failure.

---

# 6. DAILY / MORNING BRIEF — MAJOR EXPANSION

The Morning Brief should become:

> **"What changed while I wasn't looking, what matters now, where is risk concentrated, and what deserves my attention next?"**

It must NOT become a wall of equally important cards.

Use information hierarchy.

The first screen should answer the highest-value questions immediately.

Lower-priority context belongs further down.

Retain the existing selectable windows such as:

- 12h
- 24h
- 48h
- 7d

but ensure every panel uses the selected window where doing so is semantically valid.

Do not force slow daily/weekly datasets into fake intraday changes.

---

# 7. DAILY BRIEF — TOP SUMMARY BAND

Create a high-information summary area at the top.

## A. "What Changed" summary

Build a deterministic summary of meaningful changes since the selected brief window.

Examples:

- BTC gained/lost X%
- volatility regime changed
- major funding extreme appeared
- open interest changed materially
- liquidation burst
- yield curve changed materially
- major economic release occurred
- new high-severity geopolitical threat
- new congressional/insider/institutional activity
- new DEX survivor
- major position P&L/risk change
- gate experiment gained meaningful resolutions
- feed/data source degraded
- concentration threshold warning

Do NOT manufacture LLM prose when deterministic facts are available.

A concise generated narrative may be layered on top later, but the numbers must remain traceable.

## B. Brief KPI strip

Consider:

- market regime
- BTC 24h
- ETH 24h
- SOL 24h
- portfolio equity
- today's P&L
- open risk
- gross exposure
- largest concentration
- kill-switch state
- data health
- critical alerts

Use conditional emphasis sparingly.

---

# 8. ADD REAL CHARTS TO THE BRIEF

The application already has working market chart infrastructure.

The Brief currently relies too heavily on text, tables, pills and static bars.

Add actual useful visualizations.

Do NOT add charts merely as decoration.

Reuse existing dependencies such as `lightweight-charts` wherever appropriate instead of adding a large visualization dependency without a compelling reason.

Create reusable compact visualization components where helpful.

Potential components:

```
MiniPriceChart.svelte
MiniTimeSeries.svelte
MetricTrend.svelte
BreadthBar.svelte
PercentileGauge.svelte
DistributionBar.svelte
HeatmapGrid.svelte

```

Avoid hundreds of lines of duplicate chart setup in every panel.

---

# 9. MARKET PULSE PANEL

Upgrade the current Market Pulse.

Show a useful cross-asset snapshot such as:

- major equity indices / ETFs
- BTC
- ETH
- SOL
- USD / DXY proxy if available
- major Treasury rates if appropriate
- crude
- gold
- major FX where available

For each relevant instrument:

- last
- selected-window change
- 24h/session change where applicable
- mini sparkline
- volume/volatility context where available

Use JARVIS's existing OHLCV cache.

Do not initiate expensive external fetches solely to draw a chart.

---

# 10. CRYPTO MARKET PULSE — NEW BRIEF PANEL

JARVIS already has broader crypto market data.

Build a proper Crypto Market Pulse using available data such as:

- price
- market cap
- 24h volume
- 1h %
- 24h %
- 7d %
- distance from ATH

Include:

### Core assets

- BTC
- ETH
- SOL
- other configured majors

### Movers

Show useful categories:

- strongest 24h
- weakest 24h
- strongest 7d
- unusual volume
- largest market-cap movers

Do not turn this into a memecoin leaderboard unless that is what the underlying configured universe represents.

Consider a compact performance heatmap.

---

# 11. CRYPTO DERIVATIVES — EXPAND THE BRIEF

Audit the existing derivatives implementation and UI thoroughly.

Current Brief coverage must not remain arbitrarily limited if JARVIS already has broader legitimate data.

Surface where supported:

- funding rate
- funding percentile/extreme
- open interest
- OI change
- long/short positioning
- account skew
- liquidation state
- liquidation imbalance
- perp basis if available
- crowding warnings
- stale age
- exchange/source

Core assets should include at least BTC/ETH/SOL when real data exists.

Do not fabricate unsupported SOL values merely for symmetry.

### Add trend visualization

If historical derivative snapshots are stored, add:

- funding trend
- OI trend
- liquidation trend

If history is NOT stored, do not fake a trend from one point.

Instead decide whether storing the real historical data is warranted and consistent with the architecture.

---

# 12. ORDER BOOK / MICROSTRUCTURE SNAPSHOT

JARVIS already has Level-2/order-book infrastructure.

Add a compact Brief microstructure panel for important crypto assets.

Useful information:

- Binance bid/ask spread where available
- Coinbase bid/ask spread
- top-book imbalance
- aggregate depth imbalance
- cross-venue disagreement
- snapshot age
- venue health

Highlight:

- unusually one-sided books
- spread deterioration
- stale stream
- cross-venue price disagreement

Do not pretend Level-2 predicts direction by itself.

Treat it as execution/microstructure context.

---

# 13. ON-CHAIN NETWORK STATE — MANDATORY AUDIT ITEM

The backend now contains Coin Metrics on-chain infrastructure.

Audit it thoroughly.

Current capabilities include BTC/ETH network context such as:

- MVRV
- MVRV trailing percentile
- active addresses
- active-address trailing percentile
- transaction count where granted/stored
- market cap where granted/stored
- supply where granted/stored
- hash rate where granted/stored

Determine which of these are actually stored and safely exposeable.

If there is not currently a clean read-only API route for these data:

**create one rather than duplicating calculations in Svelte.**

The backend remains the authority.

Build a Daily Brief / Crypto Desk **On-Chain Cycle & Network State** panel.

At minimum for BTC and ETH:

- MVRV
- 2-year MVRV percentile
- active addresses
- active-address percentile
- observation date
- release timestamp
- freshness

Add meaningful historical charts where real history exists:

- MVRV 30d / 90d / 1y
- active addresses trend

Clearly explain that these are slow network/cycle context, not intraday trade signals.

Preserve the backend's stale-data abstention discipline.

---

# 14. DEX DISCOVERY — MANDATORY UI ADDITION

Audit `/dex/discovery` and `lib/dex_discovery.py`.

This is an important new crypto capability and must be represented in the UI.

Add a proper `api.ts` type/client if missing.

Create a **DEX Discovery / Incubator** surface in Crypto Desk and a compact version in Daily Brief.

The headline should emphasize:

```
SCANNED
SURVIVED
REJECTED
DEGRADED/FETCH ERRORS

```

because the filtering is the useful part.

Add a rejection-reason visualization, for example:

- insufficient liquidity
- insufficient volume
- insufficient transactions
- too few buyers
- too young
- too old
- one-way flow
- missing creation time

A horizontal bar chart is appropriate.

For survivors show:

- network
- token/pool
- pool address
- age
- liquidity
- 24h volume
- transaction count
- buyers
- sell ratio
- FDV where available
- secondary confirmation
- DEX
- source disagreement if present

Respect the architecture:

**A surviving DEX discovery candidate is NOT a trading signal.**

The UI must label it something like:

- DISCOVERY
- WATCH
- INCUBATING

Never:

- BUY
- LONG
- TRADE

unless a completely separate measured strategy eventually authorizes it.

---

# 15. CRYPTO DESK — COMPLETE THE SURFACE

The Crypto Desk should become the operator's dedicated crypto intelligence page.

Inventory exactly what is already present and avoid duplication.

Potential logical groups:

## Market

- crypto market overview
- movers
- breadth
- relative strength

## Derivatives

- funding
- OI
- liquidations
- long/short
- crowding

## Microstructure

- Level-2
- spread
- depth
- imbalance
- feed freshness

## On-chain

- MVRV
- network activity
- cycle percentiles

## Discovery

- DEX scanner
- incubator
- survivor/rejection statistics

## Venue / execution context

- Kraken account/feed state
- venue comparison
- fee comparison
- product availability
- expected execution costs where existing infrastructure supports it

Do not duplicate the Positions page or turn Crypto Desk into another trade ticket.

---

# 16. VENUE / FEE / EXECUTION PANEL

Audit existing venue and fee-comparison APIs.

Create a useful read-only execution-context panel if the information is currently buried.

Show things such as:

- eligible venue
- product
- spot / perpetual / futures where appropriate
- configured maker/taker fees
- operator's known fee tier where available
- estimated round-trip friction
- availability
- leverage cap if legitimately known by JARVIS
- why a venue is or is not eligible

If JARVIS already determines best execution/venue routing, surface its reasoning.

Do not add live order placement to the Brief just because this information exists.

---

# 17. MACRO PULSE — BETTER VISUALIZATION

The Macro Desk and Brief should expose macro context visually.

Audit:

- FRED
- Treasury curve
- FX
- catalyst calendar
- sector fundamentals
- COT positioning

### Yield curve

If currently shown mainly as numbers, add an actual Treasury yield curve:

```
1M → 3M → 6M → 1Y → 2Y → 5Y → 10Y → 20Y → 30Y

```

Show:

- current curve
- inversion flags
- optionally prior comparable curve if safely available

### Macro dashboard

Compact cards for:

- Fed funds
- CPI
- unemployment
- payrolls
- GDP
- other configured high-value data

Always show:

- observation date
- release date/freshness

Don't present last month's CPI as if it is a real-time market tick.

---

# 18. FUTURES / SECTOR INTELLIGENCE

JARVIS now has significant futures and sector-specific infrastructure.

Ensure useful portions are visible without forcing the operator to hunt.

Consider a compact Daily Brief **Positioning & Curves** panel with:

- instrument
- COT spec net
- 3-year percentile
- crowding extreme
- curve state
- roll economics
- inventory/fundamental extreme where supported

Highlight true extremes, e.g.:

```
>= 90th percentile
<= 10th percentile

```

Do not invent one generic bullish/bearish score across fundamentally different products.

Keep provenance visible.

---

# 19. OPTIONS CONTEXT

Audit existing options-summary support.

Where meaningful, create a compact options panel for key equity/index instruments or open positions:

- ATM IV
- IV skew
- put/call context if genuinely available
- expected move
- nearest relevant expiry
- data age

Do not claim "unusual options flow" unless JARVIS actually has the necessary data.

The existing implementation's limitations must remain visible.

---

# 20. SMART MONEY SUMMARY ON THE DAILY BRIEF

The dedicated Smart Money page can remain deep.

The Brief only needs the most important new activity.

Create a compact ranked summary drawn from existing modules:

- notable insider buying/selling
- congressional disclosures
- institutional accumulation/distribution
- major FINRA ATS / dark-pool observations
- high squeeze-fuel names
- IPO activity

Use publication/reporting lag labels.

Never present delayed FINRA ATS or congressional disclosures as live order flow.

---

# 21. CATALYSTS — "WHAT COULD MOVE MARKETS TODAY?"

Create a focused catalyst panel.

Combine existing calendar infrastructure where appropriate:

- economic releases
- central-bank events
- earnings
- commodity releases
- relevant futures reports
- important known scheduled events

Sort by time.

Clearly show timezone.

Distinguish:

- scheduled
- released
- awaiting release
- stale/unavailable source

This should answer:

> "What can suddenly change the market today?"

---

# 22. PORTFOLIO RISK — BETTER MORNING VISIBILITY

The Brief should give a compact snapshot of actual portfolio risk.

Show appropriate fields such as:

- equity
- free cash
- today's P&L
- open positions
- gross notional
- net directional exposure
- margin used
- portfolio heat
- largest instrument concentration
- largest correlated bucket
- nearest stop
- leveraged exposure
- concentration warnings

Use the authoritative risk/book systems.

Do NOT calculate an alternative risk model in Svelte.

If different paper/Auto Sim/live books exist, label them clearly.

Do not accidentally combine them into a number whose meaning changes.

---

# 23. LEARNING / GATE EXPERIMENT — VISUALIZE CORRECTLY

Audit the Gate Experiment and learning surfaces.

The UI must preserve statistical meaning.

In particular:

- display effective N
- display raw N separately
- stratify by timeframe when required
- don't let pooled statistics imply a valid head-to-head comparison when compositions differ
- show uncertainty where supported

Create useful visualizations such as:

- win rate by arm/timeframe
- net R by arm/timeframe
- resolved sample growth
- calibration progression
- model comparison if sufficient data exists

Do not graph tiny samples as if they are conclusive.

Annotate thin evidence.

---

# 24. DATA HEALTH SHOULD BE VISIBLE, NOT BURIED

JARVIS depends on many feeds.

Create a compact data-health element in the Brief and retain detailed Ops diagnostics.

Surface:

- streams live/degraded
- sources healthy/failing
- last refresh
- oldest important feed
- queue drops where relevant
- WebSocket status
- major API failure
- scheduler/job failure
- stale critical dataset

A green dashboard built on stale data is worse than a broken dashboard.

---

# 25. THREAT / NEWS PANEL

Keep the full intelligence experience on Intelligence.

The Brief needs only high-signal changes:

- critical/high threats
- affected assets
- transmission hypothesis
- important new corroborated news
- source confidence
- freshness

Avoid flooding the Brief with headlines.

Prefer:

```
event → affected market → reason it could matter

```

---

# 26. IMPROVE THE CHARTS PAGE

The dedicated Charts page should remain the full price-analysis surface.

Audit whether everything Claude previously intended to implement actually exists and works.

Verify:

- candlesticks
- volume
- signal markers
- open-position overlays
- entry
- current stop
- initial stop
- target
- symbol picker
- available-timeframe picker
- historical analog chips
- jump-to-analog behavior
- symbol changes cleaning up old price lines
- chart resizing
- crosshair
- dark theme
- no stale previous-symbol state

Then consider useful additions that do not turn the chart into visual soup:

### Potential improvements

- OHLC/crosshair legend
- current price
- data timestamp/freshness
- optional RSI pane
- optional MACD pane
- ATR/volatility
- volume toggle
- signal filter by timeframe/status
- click a signal marker to open Signal Analysis
- position P&L annotation
- optional event markers for major threats/catalysts
- optional derivatives overlay for crypto

All indicators must come from JARVIS's own authoritative calculations/data.

Do not purchase/fetch redundant external indicator values just for the chart.

---

# 27. BUILD REUSABLE VISUALIZATION INFRASTRUCTURE

Do not create ten incompatible tiny chart systems.

Before building many new charts, identify recurring needs.

Examples:

### Sparkline

Input:

```
{
  points,
  positive,
  label?
}

```

### Metric trend

Useful for:

- funding
- OI
- MVRV
- active addresses
- rates
- portfolio equity

### Percentile gauge

Useful for:

- COT
- MVRV
- crowding
- active-address percentile
- squeeze percentile

### Small bar chart

Useful for:

- DEX rejection reasons
- long/short balance
- breadth
- signal counts

Use the existing visual language.

JARVIS should still look like JARVIS.

---

# 28. UI / UX DESIGN REQUIREMENTS

Preserve the current dark professional trading-workstation aesthetic.

Improve information architecture rather than replacing it.

Priorities:

1. information density
2. clear hierarchy
3. fast scanning
4. data provenance
5. freshness visibility
6. sensible alignment
7. consistent spacing
8. minimal wasted space
9. readable numeric tables
10. workstation-scale layouts

Avoid:

- giant cards containing one number
- enormous padding
- marketing-site layouts
- gradients everywhere
- gratuitous animations
- oversized typography
- mobile-first compromises that waste desktop space
- dozens of equally loud colors

Use a 12-column/grid mentality where useful.

Important panels can span more columns.

Secondary panels can be smaller.

Panels should naturally align.

---

# 29. EMPTY / STALE / ERROR STATES

Create consistent state handling.

Each data component should understand:

```
LOADING
READY
EMPTY
STALE
DEGRADED
ERROR
NOT_CONFIGURED
UNSUPPORTED

```

The operator should know which one occurred without opening DevTools.

Examples:

Good:

```
Coin Metrics — last observation Aug 15 · 1d frequency

```

Good:

```
FRED not configured — API key required

```

Good:

```
Order book unavailable — Coinbase stream reconnecting

```

Bad:

```
—

```

when `—` could mean ten different things.

Use `—` for genuinely inapplicable fields, not hidden failures.

---

# 30. TIMESTAMP / FRESHNESS STANDARD

Audit timestamp handling globally.

Every slow dataset should communicate its actual information clock.

Examples:

- price: seconds
- order book: milliseconds/seconds
- funding: exchange interval
- macro: release schedule
- on-chain: daily
- COT: weekly release
- FINRA ATS: delayed weekly
- Congress: disclosure delay
- 13F: quarterly filing
- short interest: semi-monthly

Do not visually normalize all of these into one "updated 4m ago" model if that misrepresents what they mean.

Where useful show both:

```
OBSERVATION DATE
RELEASE/AVAILABLE DATE
FETCHED AT

```

---

# 31. PERFORMANCE / POLLING AUDIT

The dashboard performs substantial polling.

Audit all intervals.

Look for:

- duplicate requests across nested components
- separate components polling the same endpoint
- requests continuing while section is hidden
- too-frequent polling of slow data
- unnecessary API load
- duplicate WebSocket subscriptions

Align refresh rate with data frequency.

Examples:

- order book: WebSocket
- prices: live/short interval
- Coin Metrics: daily
- FRED: does not need 30-second polling
- COT: does not need 30-second polling
- congressional disclosures: slower
- DEX scanner: respect external rate limits

Where practical, centralize shared state/cache rather than having six components independently request the same resource.

Do not make this refactor larger than necessary.

---

# 32. AUDIT FRONTEND API COVERAGE

Read every method/type in:

`frontend/src/lib/api.ts`

For every exported API method:

Ask:

1. Is anything using it?
2. If not, why?
3. Is it obsolete?
4. Is it a useful backend capability that never reached the UI?
5. Is its type accurate?
6. Are optional/null fields represented correctly?
7. Are errors handled correctly?
8. Is the backend response shape still the same?

Also inspect backend routes that have **no corresponding api.ts client method**.

Those are high-priority parity candidates.

Do NOT automatically expose administrative/debug endpoints merely because they exist.

Use judgment.

---

# 33. SPECIFIC CRYPTO PARITY CHECKLIST

Explicitly audit these categories:

```
crypto market data
BTC
ETH
SOL
tracked altcoins
market cap
volume
1h change
24h change
7d change
ATH distance
relative strength
regime
spot pricing
Kraken
crypto derivatives
funding
funding history
open interest
OI history/change
long/short ratio
liquidations
order books
spread
book imbalance
venue comparison
fees
perpetual/futures product metadata
safe leverage policy outputs
on-chain metrics
MVRV
active addresses
DEX discovery
DEX survivor filtering
DEX rejection reasons
incubator/backfill state
crypto alerts
crypto catalysts
crypto web news

```

For each, classify:

```
BACKEND ONLY
API ONLY
UI PARTIAL
UI COMPLETE

```

Then close the useful gaps.

---

# 34. DON'T DUPLICATE CALCULATIONS IN THE FRONTEND

If information requires financial interpretation or data calculation, backend should generally own it.

Frontend responsibilities:

- presentation
- sorting
- filtering
- harmless formatting
- user interaction

Backend responsibilities:

- percentiles
- risk
- P&L authority
- execution costs
- leverage safety
- statistics
- stale/fresh determinations where semantically important
- released-as-of logic
- financial calculations

Do not create divergent duplicate financial logic in TypeScript.

---

# 35. ACCESSIBILITY / QUALITY

Even though this is a trading workstation, basic accessibility matters.

Audit:

- keyboard navigation
- focus states
- button semantics
- `aria` labels where necessary
- Enter/Space on expandable rows
- contrast
- tooltips not containing essential information inaccessible otherwise
- tab order
- tables

Also audit:

- consistent decimal precision
- dollar formatting
- percentage formatting
- negative signs
- basis points
- large-number abbreviation
- UTC/local-time labels

---

# 36. DO NOT REMOVE USEFUL EXISTING FUNCTIONALITY

This project has substantial working functionality.

Do not solve UI complexity by deleting capabilities.

Do not rewrite:

- trading logic
- risk engine
- learning engine
- event store
- API architecture

unless a concrete UI-related correctness bug requires a small change.

Prefer incremental integration.

---

# 37. IMPLEMENTATION PRIORITY

After the audit, execute in approximately this order:

## P0 — incorrect or dangerous UI behavior

Examples:

- wrong values
- misleading values
- dangerous buttons
- live control behaving incorrectly
- position counts wrong
- stale data displayed as live
- failed feed shown as zero
- direction reversed
- unit errors
- data from wrong symbol
- duplicate stale chart overlays

Fix these first.

## P1 — backend data completely missing from UI

Priority examples:

- DEX discovery
- on-chain state
- important crypto derivative data
- alerts
- important risk state

## P2 — Daily Brief improvement

Build the richer Brief and charts.

## P3 — Crypto Desk completion

Build the complete crypto operator surface.

## P4 — visualization across existing sections

Add useful charts/gauges where they improve comprehension.

## P5 — polish

Spacing, layout, responsiveness, minor usability.

---

# 38. DAILY BRIEF INFORMATION HIERARCHY

Do NOT create twenty same-size panels.

A better structure is roughly:

```
────────────────────────────────────────────────────────────
WHAT CHANGED / CRITICAL ALERTS
────────────────────────────────────────────────────────────

MARKET / ACCOUNT KPI STRIP

────────────────────────────────────────────────────────────
MARKET PULSE             | CRYPTO PULSE
(price trends)            | movers/breadth
────────────────────────────────────────────────────────────

CRYPTO DERIVATIVES       | PORTFOLIO RISK
funding/OI/liquidations  | concentration/exposure
────────────────────────────────────────────────────────────

CATALYSTS / TODAY        | THREATS / NEWS
────────────────────────────────────────────────────────────

ON-CHAIN                 | POSITIONING / CURVES
────────────────────────────────────────────────────────────

DEX DISCOVERY            | SMART MONEY
────────────────────────────────────────────────────────────

GATE / LEARNING          | DATA HEALTH
────────────────────────────────────────────────────────────

INCUBATOR / SECONDARY CONTEXT
────────────────────────────────────────────────────────────

```

Adjust based on what real data is available.

Do not force this exact wireframe if repo architecture suggests something better.

---

# 39. USE CROSS-LINKING

The Brief should summarize, not duplicate entire specialist pages.

Where practical, clicking:

- BTC
- a signal
- a threat
- a position
- a DEX candidate
- a smart-money symbol
- a chart
- an alert

should navigate to or open the relevant deeper JARVIS surface.

Examples:

```
BTC → Charts / BTC
Position warning → Positions
Critical threat → Intelligence
Congress trade → Smart Money
DEX candidate → Crypto Desk
Gate anomaly → Performance
Feed failure → Ops

```

Use existing section/link store architecture rather than inventing a second router.

---

# 40. VERIFY "CLAUDE WAS SUPPOSED TO CREATE THIS"

Do not trust previous implementation statements.

Review recent git commits and compare their stated intentions against actual current behavior.

Examples to verify include:

- Morning Brief
- Brief v2
- Charts
- analogs
- chart overlays
- Crypto Desk
- sector desk
- data platform panels
- gate experiment visualization
- feed parity
- live forex
- DEX discovery
- on-chain cycle gauge

For each claimed UI feature:

```
PROMISED
IMPLEMENTED IN CODE?
EXPOSED IN API?
VISIBLE IN UI?
WORKING WITH REAL DATA?

```

A commit message saying something was implemented is not proof that the current UI works.

---

# 41. TESTS

After every major phase:

Run Python tests.

Run:

```
cd frontend
npm run check
npm run build

```

Add focused tests for backend/API changes.

If the project has no browser test framework, do not install a giant framework merely to satisfy this instruction unless its long-term value is justified.

At minimum manually/runtime verify all affected screens.

For every newly surfaced endpoint test:

- happy path
- empty
- stale
- unavailable/error
- malformed/partial data where relevant

For UI components, verify those states manually or with the project's existing testing infrastructure.

---

# 42. CREATE `UI_AUDIT.md`

Maintain a concise audit report during the work.

Include:

## Executive summary

## Broken functionality discovered

| SeveritySurfaceProblemRoot CauseFixVerified |
| ------------------------------------------- |

## Backend → UI parity gaps

| CapabilityBackendAPIUI BeforeUI After |
| ------------------------------------- |

## New panels / visualizations

## Polling/performance issues

## Remaining external dependency limitations

## Items intentionally not exposed

## Test results

Do not make this report a substitute for implementing the fixes.

---

# 43. SCREENSHOT / VISUAL VERIFICATION

Where your environment allows it, capture screenshots after implementation of:

- Morning Brief
- Crypto Desk
- Charts
- Macro
- Smart Money
- Performance
- Ops

Inspect them yourself.

Look for:

- clipping
- wasted space
- panels with enormous empty areas
- tables wider than containers
- inconsistent heights
- unreadable text
- charts too short to understand
- giant legends
- mismatched vertical alignment
- excessive scrolling
- panels visually dominating despite low importance

Fix what you observe.

---

# 44. IMPORTANT DATA-SEMANTICS RULES

Never turn these into marketing-style certainty.

### DEX discovery

"Qualified for observation" ≠ "trade."

### MVRV

Cycle/network valuation context ≠ entry signal.

### Order-book imbalance

Microstructure state ≠ guaranteed price direction.

### Funding

Crowding/carry context ≠ automatic contrarian trade.

### Dark pool

Delayed FINRA ATS information ≠ real-time dark-pool tape.

### Congress

Disclosure ≠ trade just occurred today.

### 13F

Quarterly holdings ≠ current position.

### COT

Weekly positioning ≠ intraday flow.

### Options expected move

Market-implied magnitude ≠ predicted direction.

### Threat transmission

Hypothesis ≠ causal certainty.

Retain disclaimers where needed without cluttering every card.

---

# 45. NEW IDEAS YOU SHOULD EVALUATE

After completing parity, look for additional opportunities based on the data already available.

Useful ideas include:

### Cross-asset correlation shock

Identify when historically correlated assets suddenly diverge.

Examples:

- BTC vs QQQ
- gold vs real-rate proxy
- crude vs energy equities
- copper vs cyclical equities

Only add if the existing data supports it cleanly.

### Crypto breadth

Across tracked coins:

- % green 1h
- % green 24h
- % green 7d
- median return
- volume-weighted breadth

### Risk-on / risk-off matrix

A compact factual cross-asset state grid, not a magical score.

### Funding heatmap

Major crypto assets by funding extremity.

### Liquidation pressure

Long vs short liquidation dominance over the selected window.

### Venue basis monitor

Cross-venue price/funding discrepancies where data exists.

### "Why this matters"

For unusual observations, one short deterministic/contextual explanation.

Example:

```
BTC funding: 97th percentile
→ leveraged longs unusually crowded relative to recent history

```

Do not turn every metric into AI commentary.

### Data confidence strip

For each Brief panel:

```
LIVE
FRESH
DELAYED
STALE
DEGRADED

```

### Change badges

Show genuinely new conditions:

```
NEW
ESCALATED
NORMALIZED
EXTREME

```

instead of making the operator remember yesterday's state.

---

# 46. DON'T STOP AT AN AUDIT

This is critical.

You are not being asked merely to write:

> "DEX discovery isn't in the UI."

If the data is useful and safe to expose:

**implement it.**

If a chart was promised but is missing:

**build it.**

If an endpoint exists but `api.ts` has no client:

**wire it.**

If the UI renders incorrect information:

**fix the root cause.**

If an empty state hides an API failure:

**fix it.**

If a useful backend capability is currently invisible:

**surface it intelligently.**

If something should intentionally remain backend-only:

document why.

---

# 47. FINAL VALIDATION

Before declaring the work complete:

1. Pull/compare against current branch base to ensure no accidental regressions.
2. Run the full Python test suite.
3. Run `npm run check`.
4. Run `npm run build`.
5. Start JARVIS safely.
6. Visit every section.
7. Exercise every affected interaction.
8. Check the console.
9. Check failed network requests.
10. Check WebSocket state.
11. Verify representative displayed numbers directly against API responses.
12. Verify stale/error states.
13. Verify Daily Brief at each time-window selection.
14. Verify multiple common desktop widths.
15. Verify panel pop-outs.
16. Confirm no live orders were submitted.
17. Confirm no production trading data was destructively modified.

---

# 48. FINAL RESPONSE TO ME

When complete, report:

## Problems found

Grouped by P0/P1/P2/P3.

## Problems fixed

Give exact files and short descriptions.

## Data previously available but not surfaced

List all meaningful backend/API capabilities that were missing or partial.

## Daily Brief improvements

List every new/changed panel and chart.

## Crypto Desk improvements

List every new capability.

## Charts added

State exactly what data each visualizes.

## Remaining gaps

Explain why each remains.

## Test results

Give actual counts/output, not "tests passed."

## Frontend validation

Give:

- `npm run check`
- `npm run build`
- runtime/browser verification

## Commits

List the commits created on the feature branch.

Do NOT merge the branch into `main` automatically.

---

# CORE PRINCIPLE

JARVIS should not merely have an enormous amount of data.

It should make that data **operationally intelligible**.

The UI should allow the operator to understand, within seconds:

```
WHAT MOVED?
WHAT CHANGED?
WHAT IS EXTREME?
WHAT IS CROWDED?
WHAT IS BREAKING?
WHAT IS STALE?
WHAT CAN MOVE MARKETS NEXT?
WHERE IS MY RISK?
WHAT DOES JARVIS ACTUALLY HAVE EVIDENCE FOR?
WHAT DESERVES MY ATTENTION?

```

Do that while preserving JARVIS's evidence-first architecture, risk boundaries, provenance, timing discipline, and current visual identity.
---

# 49. INTEGRATED UI/UX WORKSTATION UPGRADE — ADDITIVE REQUIREMENTS

The following sections are **additive** to everything above and take precedence where they are more specific.

Do not remove, weaken, or reinterpret the existing UI audit, Daily Brief, Crypto Desk, Charts, risk, evidence, provenance, timing, safety, or data-parity requirements.

The objective is now broader:

> **Turn JARVIS into a configurable, high-density professional trading and intelligence workstation — not merely a dashboard with more cards.**

The UI should help the operator move naturally between:

```text
MARKET
  ↓
SIGNAL
  ↓
CHART
  ↓
POSITION
  ↓
RISK
```

and:

```text
HELIUS EVENT
  ↓
WALLET
  ↓
IDENTITY / CLUSTER
  ↓
TOKEN
  ↓
WALLET CONSENSUS
  ↓
DEX / LIQUIDITY
  ↓
COPYABILITY
  ↓
TOKEN / EXECUTION RISK
```

The interface should make relationships obvious instead of forcing the operator to hunt through disconnected pages.


# 50. CONFIGURABLE WORKSTATION LAYOUTS

Where practical within the current Svelte architecture, upgrade key screens such as:

- Morning / Daily Brief
- Command Center
- Crypto Desk
- Wallet Alpha
- Charts
- Positions
- Intelligence
- Ops

to support workstation-style layout behavior.

Evaluate and implement useful support for:

- resizable panels
- reorderable panels
- collapsed / expanded panels
- panel maximize / focus mode
- consistent panel pop-outs
- persisted layout state
- saved workspace presets
- reset current layout
- reset all UI preferences

Potential workspace presets:

```text
DEFAULT
CRYPTO
SOLANA
SCALPING
MACRO
SMART MONEY
RISK
RESEARCH
```

Where reasonable allow user-created layouts.

A layout may persist:

- panel order
- panel size
- collapsed state
- selected tabs
- selected symbols
- selected timeframe
- density mode
- visible table columns
- column widths
- filters

Never persist secrets in frontend preferences.


# 51. PANEL RESIZING / REORDERING

Major analytical panels should be able to receive more or less screen real estate without requiring code changes.

Examples:

- Charts
- Wallet Alpha Live Tape
- Order Book
- Positions
- Signals
- DEX Discovery
- Wallet Graph
- Copyability
- Macro curves

Avoid forcing every panel into identical dimensions.

Use drag handles or explicit layout controls.

Do not make ordinary data rows draggable.

Persist layout safely.


# 52. PANEL FOCUS / MAXIMIZE MODE

Important panels should support a temporary focused view.

Examples:

- chart
- wallet graph
- order book
- DEX scanner
- wallet activity feed
- copyability chart
- Treasury curve
- provider pipeline diagnostics

The operator should be able to focus on one panel and return without losing page context.


# 53. MULTI-MONITOR / POPOUT UX

JARVIS already supports pop-out concepts.

Audit them and make them consistent.

Useful surfaces for pop-outs include:

- Charts
- Positions
- Signals
- Order Book
- Wallet Alpha
- Threats / News
- Provider Health

Ensure pop-outs:

- initialize with the correct current symbol/context
- do not create duplicate WebSocket subscriptions unnecessarily
- clean up subscriptions/listeners on close
- preserve theme
- preserve relevant filters
- do not leak stale state from another symbol


# 54. GLOBAL COMMAND PALETTE

Add a keyboard-first command palette if compatible with the current architecture.

Recommended shortcut:

```text
Ctrl/Cmd + K
```

Possible commands:

```text
Open BTC chart
Open ETH chart
Open SOL chart
Open wallet
Open token
Search mint
Search transaction
Go to Positions
Go to Wallet Alpha
Go to DEX Discovery
Go to Ops
Go to Macro
Open critical alerts
Toggle compact mode
Refresh current panel
Pause live tape
Resume live tape
```

This is primarily a navigation / lookup / workstation control feature.

Do not use it to bypass trading confirmations or risk controls.


# 55. GLOBAL INTELLIGENT SEARCH

Add global search capable of resolving:

- ticker
- crypto symbol
- Solana mint
- wallet address
- transaction signature
- position
- signal ID
- threat / event
- known wallet/entity label

Search results should be grouped, for example:

```text
MARKETS
TOKENS
WALLETS
POSITIONS
SIGNALS
EVENTS
TRANSACTIONS
```

Selecting a result should open the appropriate deep surface.

Examples:

```text
SOL → Charts / Crypto
7Wfg...92Aa → Wallet Profile
mint address → Token Intelligence
transaction signature → Event / transaction inspector
signal ID → Signal Analysis
```


# 56. MASTER / DETAIL + INSPECTOR DRAWERS

Do not force full-page navigation for every detail.

Use side drawers / inspectors where useful.

Examples:

```text
click wallet
    → Wallet Inspector

click token
    → Token Intelligence Inspector

click signal
    → Signal Detail

click position
    → Position / Risk Detail

click DEX candidate
    → DEX Candidate Detail

click Helius/provider error
    → Diagnostics Drawer
```

The user should be able to inspect something deeply without losing the page they were working from.


# 57. LINKED CONTEXT ACROSS PANELS

Add optional linked-panel behavior.

Example:

If the user selects BTC in a linked workspace:

```text
Chart        → BTC
Derivatives  → BTC
Order Book   → BTC
Signals      → BTC
News         → BTC
```

If the user selects a Solana token:

```text
Token Intelligence → selected mint
DEX Discovery      → selected mint/pool
Wallet Alpha       → related events
Chart              → selected token if chartable
Wallet Consensus   → selected token
```

Provide a visible:

```text
LINKED
```

toggle.

Do not force linked behavior.


# 58. DENSITY MODES

Support at least:

```text
COMPACT
COMFORTABLE
```

Default should remain appropriately dense for a trading workstation.

Compact mode should use:

- tighter row heights
- reduced panel padding
- tabular numerals
- sticky headers
- compact badges
- smaller but readable typography

Avoid consumer-app spacing.


# 59. TABLE UX STANDARD

Audit every major table.

Where useful add:

- sticky headers
- sorting
- filtering
- column visibility
- column resize
- compact density
- persistent preferences
- row selection
- keyboard navigation
- copy value/address
- pagination or virtualization for large datasets

Do not render thousands of live DOM rows.

Potential column presets:

### Wallet Alpha

```text
FLOW
PERFORMANCE
COPYABILITY
RISK
```

### DEX Discovery

```text
MARKET
FLOW
RISK
```

### Positions

```text
P&L
RISK
EXECUTION
```

Avoid horizontal-scroll hell.


# 60. LIVE FEED UX

For fast event streams such as Wallet Alpha or other live tapes:

Do **not** constantly reorder data while the operator is reading.

Support:

```text
AUTO FOLLOW
PAUSE
RESUME
```

When paused, backend collection continues.

Display a banner such as:

```text
23 NEW EVENTS
```

The operator can resume when ready.


# 61. RESTRAINED LIVE UPDATE VISUALS

A trading UI should feel live but not resemble a casino.

Use restrained temporary highlighting when values change.

Do not flash every updated number constantly.

Do not animate every Helius event.

Do not make normal updates visually compete with actual alerts.


# 62. CONSISTENT DESIGN SYSTEM

Audit and standardize:

- spacing
- panel headers
- panel chrome
- border treatment
- radius
- table row density
- table header style
- status badge size
- icon usage
- typography hierarchy
- tabular numeric styling
- tooltips
- loading states
- error states
- chart headers
- timestamp formatting
- source labels
- freshness labels

Prefer reusable UI primitives instead of dozens of slightly different implementations.


# 63. COLOR SEMANTICS

Colors must communicate consistent meaning.

Suggested semantic intent:

```text
GREEN  = healthy / confirmed / positive where directionally meaningful
RED    = failed / critical / negative where directionally meaningful
AMBER  = warning / degraded / elevated
BLUE   = informational / neutral live state
MUTED  = inactive / stale / unavailable
```

Do not use bullish/bearish colors for data that are not directional.

Example:

High Solana priority fees are execution friction.

They are not automatically "bearish."


# 64. DATA STATE STANDARD — EXPANDED

All important data components should understand:

```text
LOADING
CONNECTING
LIVE
READY
EMPTY
FRESH
DELAYED
STALE
DEGRADED
ERROR
NOT_CONFIGURED
UNSUPPORTED
PLAN_UNAVAILABLE
```

Never hide meaningful states behind a generic:

```text
—
```

Examples:

```text
No liquidations observed
```

and:

```text
Liquidation feed unavailable
```

must remain distinct.


# 65. ERROR UX SHOULD BE ACTIONABLE

Bad:

```text
ERROR 500
```

Better:

```text
HELIUS WSS DISCONNECTED

Last good message: 41s ago
Reconnect attempt: 3
Next retry: 3s

[DETAILS]
```

Provider/API errors should expose enough context to diagnose them without exposing secrets.


# 66. LOADING UX

Use subtle contained skeleton/loading states when structure is known.

Avoid dramatic layout movement when data arrives.

Examples:

```text
CONNECTING
LOADING HISTORY
WAITING FOR FIRST EVENT
```

are preferable to blank space.


# 67. SOURCE PROVENANCE

Users should be able to understand important data provenance.

Examples:

```text
Helius
Kraken
Coin Metrics
FRED
FINRA
SEC
GeckoTerminal
DEX Screener
```

Do not cover the application with provider logos.

Use subtle source labels/tooltips where appropriate.


# =====================================================================
# HELIUS / SOLANA / WALLET ALPHA
# =====================================================================

# 68. HELIUS BECOMES A FIRST-CLASS SOLANA DATA PLATFORM

Helius is NOT merely a webhook provider.

Audit and intelligently integrate useful current Helius capabilities into JARVIS.

Before coding, verify current official Helius documentation because:

- APIs evolve
- beta schemas can change
- plan requirements can change
- deprecated APIs may remain functional but should not anchor new architecture

Audit current support for:

- standard Solana RPC
- Wallet API
- webhook management
- enhanced / filtered WebSockets
- `transactionSubscribe`
- `getTransactionsForAddress`
- `getTransaction`
- DAS
- `getPriorityFeeEstimate`
- LaserStream
- Sender if relevant
- usage / credit accounting
- plan restrictions


# 69. HELIUS CAPABILITY PARITY MATRIX

Create a dedicated Helius section in `UI_AUDIT.md`.

Use:

| Capability | Current Helius Support | Backend Adapter | Storage | JARVIS API | api.ts | UI | Status | Notes |
|------------|------------------------|-----------------|---------|------------|--------|----|--------|------|

Audit at minimum:

```text
RPC
Wallet API
Wallet Identity
Batch Identity
Wallet Balances
Wallet History
Wallet Transfers
Wallet Funded-By
getTransactionsForAddress
DAS
getAsset
getAssetBatch
getAssetsByOwner
searchAssets
getTokenAccounts
Webhooks
Webhook management
WebSocket
transactionSubscribe
logsSubscribe
accountSubscribe
signatureSubscribe
slotSubscribe
programSubscribe
Priority Fee API
LaserStream
Provider usage
Provider latency
Provider health
```


# 70. HELIUS ENDPOINT FAMILIES

Verify exact current forms against official documentation before coding.

Architecturally recognize:

```text
Solana RPC:
https://mainnet.helius-rpc.com/?api-key=...

Devnet RPC:
https://devnet.helius-rpc.com/?api-key=...

WebSocket:
wss://mainnet.helius-rpc.com/?api-key=...

Wallet API:
https://api.helius.xyz/v1/wallet/...

Webhook management / enhanced services:
Helius mainnet API / RPC endpoints as currently documented
```

Do not hard-code endpoint assumptions in Svelte.

Backend provider configuration should own provider URLs.


# 71. HELIUS WALLET API ADAPTER

Audit/implement support for current Wallet API features such as:

- wallet identity
- batch identity
- balances
- history
- transfers
- original funding source / funded-by

The Wallet API may be beta.

Therefore:

- isolate provider response models
- normalize into JARVIS-owned schemas
- do not spread raw beta response types throughout the app
- tolerate missing fields
- tolerate provider additions
- surface provider errors cleanly
- test against fixtures
- preserve provenance


# 72. WALLET PROFILE — FIRST-CLASS UI

Any tracked Solana wallet should be drillable into a professional Wallet Profile.

Header:

```text
wallet address
copy button
resolved identity
JARVIS label
tier
status
cluster
first seen
last activity
last Helius event
```

Identity:

```text
entity name
entity type
category
tags
known exchange/protocol attribution
domains where available
```

Funding:

```text
original funder
funder identity
funding type
initial amount
funding date
wallet age
```

Portfolio:

```text
current USD value
SOL balance
top token holdings
allocation
concentration
token count
large recent balance changes
```

Activity:

```text
recent swaps
transfers
liquidity actions
transaction frequency
active days
protocol usage
counterparties
```

JARVIS forward research:

```text
forward sample
forward P&L
win rate
profit factor
max drawdown
median hold
Trader Quality
Copyability
quote success
median delayed-entry disadvantage
alpha half-life
```


# 73. WALLET PROFILE VISUAL STRUCTURE

Do not render Wallet Profile as one enormous table.

Use tabs/subviews such as:

```text
OVERVIEW
ACTIVITY
PORTFOLIO
PERFORMANCE
COPYABILITY
GRAPH
RAW EVENTS
```

Potential visualizations:

- portfolio allocation
- equity/P&L curve where valid
- activity timeline
- copyability delay curve
- funding/cluster graph
- token flow
- recent transaction table


# 74. PROVIDER HISTORY VS JARVIS FORWARD EVIDENCE

Provider-supplied historical metrics are discovery/context evidence only.

They must remain visually and statistically distinct from JARVIS forward observations.

Example headings:

```text
PROVIDER HISTORICAL CONTEXT

JARVIS FORWARD OBSERVATION
```

Never silently merge them into one performance statistic.


# 75. HELIUS TRANSACTION INTELLIGENCE

Prefer current supported historical transaction methods such as:

```text
getTransactionsForAddress
```

where appropriate for new research architecture.

Audit useful support for:

- pagination
- signatures
- full transaction retrieval
- success/failure
- time / slot filters where supported
- sorting where supported
- associated token account behavior

Normalize into JARVIS wallet events.

Use it to research:

- what the wallet traded
- DEX / protocol usage
- token rotations
- holding duration
- frequency
- recurring counterparties
- recurring profitable tokens
- lead / lag behavior
- regime-dependent behavior

Never infer BUY/SELL from a transfer when transaction structure does not support that conclusion.


# 76. WALLET ALPHA DESK

Create a dedicated Wallet Alpha sub-surface inside Crypto Desk.

It should function like a professional wallet-flow/intelligence terminal.

Top metrics:

```text
TRACKED WALLETS
ELITE
VERIFIED
WATCH
ACTIVE TODAY
HIGH COPYABILITY
EVENTS 1H
EVENTS 24H
INDEPENDENT CLUSTERS
```

Primary views:

```text
LIVE FLOW
WALLETS
CONSENSUS
TOKENS
COPYABILITY
GRAPH
ALERT HISTORY
```


# 77. WALLET ALPHA LIVE TAPE

Create a dense streaming tape.

Columns:

```text
TIME
WALLET
IDENTITY
TIER
ACTION
TOKEN IN
TOKEN OUT
USD VALUE
DEX / PROTOCOL
SLOT
DETECTION LAG
COPYABILITY
CLUSTER
STATUS
```

Filters:

```text
Tier A / B / C
wallet
token
cluster
minimum USD
SWAP
TRANSFER
LIQUIDITY
copyable only
elite only
```

Interactions:

```text
row click      → event inspector
wallet click   → Wallet Profile
token click    → Token Intelligence
transaction    → transaction details / explorer action
```


# 78. WALLET CONSENSUS PANEL

For each token show:

```text
elite buyers
elite sellers
verified buyers
verified sellers
raw wallet count
independent cluster count
observed notional
median trade size
median entry
latest activity
copyability
risk state
```

Sort options:

```text
independent consensus
notional
copyability
recency
```


# 79. CONSENSUS HEATMAP

Create a compact matrix:

```text
TOKEN
×
ELITE BUYERS
VERIFIED BUYERS
SELLERS
INDEPENDENT CLUSTERS
NOTIONAL
COPYABILITY
RISK
```

This should expose genuine multi-wallet concentration quickly.


# 80. WALLET FLOW / ROTATION

Visualize tracked-wallet rotations such as:

```text
SOL  → TOKEN
USDC → TOKEN
TOKEN → SOL
TOKEN → USDC
TOKEN A → TOKEN B
```

Show:

```text
gross inflow
gross outflow
net tracked-wallet flow
unique wallets
independent clusters
median trade size
largest trade
flow acceleration
```

Label explicitly:

```text
TRACKED WALLET FLOW
```

Never imply it represents total market flow.


# 81. FLOW VISUALIZATION

If practical without a bloated dependency, create a compact flow / Sankey-style visualization.

Example:

```text
SOL  ─────▶ JUP
USDC ─────▶ WIF
JUP  ─────▶ USDC
```

Node size:
observed notional

Edge width:
observed tracked-wallet flow

Allow only time ranges actually supported by history, e.g.:

```text
1H
6H
24H
7D
```


# 82. WALLET IDENTITY / FUNDING GRAPH

Use Helius identity, funded-by data, transaction relationships, and JARVIS research.

Potential nodes:

```text
wallet
funding wallet
entity
exchange
protocol
token
```

Potential edges:

```text
FUNDED_BY
SHARED_FUNDER
TRANSFERRED_TO
TRADED_WITH
USED_PROTOCOL
HOLDS
```

Interaction:

- zoom
- pan
- fit
- select
- highlight cluster
- click node → inspector

Do not install a huge graph framework unless justified.


# 83. CLUSTER INTELLIGENCE

Use deterministic evidence where available to identify possible related wallets.

Potential evidence:

- shared original funder
- recurring counterparties
- correlated transaction timing
- repeated shared protocol patterns
- known common entity identity

Label cautiously:

```text
POSSIBLE CLUSTER
```

Do not claim:

```text
SAME OWNER
```

without sufficient evidence.


# 84. COPYABILITY IS NOT TRADER QUALITY

Preserve these as separate concepts.

Example:

```text
Trader Quality = 96
Copyability    = 14
```

Meaning:

```text
great trader
poor realistic copy target
```

This distinction must remain visible everywhere.


# 85. COPYABILITY LATENCY WATERFALL

Where timestamps are available, track:

```text
blockchain event
Helius observation
gateway receipt
decode complete
JARVIS decision
first executable quote
```

Display a latency waterfall such as:

```text
CHAIN
  0 ms

HELIUS
+ 180 ms

INGRESS
+ 24 ms

DECODE
+ 31 ms

QUOTE
+ 116 ms

TOTAL
351 ms
```

Use real measurements only.


# 86. QUOTE OBSERVATION / DELAY CURVE

When a tracked wallet trade occurs, collect executable quote snapshots where the existing architecture supports it.

Potential delays:

```text
immediate
+1 sec
+2 sec
+5 sec
+15 sec
+30 sec
+60 sec
```

Persist where appropriate:

```text
wallet_trade_id
delay_ms
router
quoted_at
input_token
output_token
input_amount
output_amount
effective_price
price_impact
estimated_network_fee
priority_fee
router_fee
liquidity
quote_success
```

The research question is:

> If JARVIS followed this wallet after the real detection delay, was meaningful edge still available?


# 87. COPYABILITY PANEL

Display:

```text
Trader Quality
Copyability
forward sample
quote success
entry disadvantage
median slippage
P95 slippage
price impact
network fee
priority fee
router fee
all-in friction
after-cost return
after-cost R
alpha half-life
```

Visualize delayed-entry economics as a curve.


# 88. HELIUS WEBHOOK MONITORING

Use grouped Helius webhook subscriptions for tracked Solana wallets where appropriate.

Do NOT create one webhook per wallet.

Wallet tiers:

```text
TIER A — ELITE / copy research
TIER B — VERIFIED
TIER C — WATCH
```

Tier A may receive broader/faster coverage.

Tier C should receive only event classes necessary to identify useful behavior.

Relevant classes may include:

```text
SWAP
TRANSFER
ADD_LIQUIDITY
REMOVE_LIQUIDITY
WITHDRAW_LIQUIDITY
meaningful token balance changes
other financially relevant events
```

Do not ingest arbitrary noise merely because the provider can send it.


# 89. SECURE WEBHOOK INGRESS

The normal JARVIS API must remain private / loopback.

Correct conceptual architecture:

```text
INTERNET
   ↓
CLOUDFLARE EDGE
   ↓
NAMED CLOUDFLARE TUNNEL
   ↓
ISOLATED WEBHOOK GATEWAY
   ↓
AUTH
   ↓
VALIDATION
   ↓
DEDUPE
   ↓
DURABLE INBOX
   ↓
INTERNAL QUEUE
   ↓
JARVIS WORKER
```

Never expose the main dashboard/trading API merely to accept provider webhooks.


# 90. WEBHOOK GATEWAY LEAST PRIVILEGE

The public webhook process must not possess:

- exchange trading credentials
- broker trading credentials
- DEX private key
- seed phrase
- admin API token
- destructive database credentials

It is an ingest process, not an execution process.


# 91. HELIUS WEBHOOK AUTH

Use a high-entropy authorization value.

Validate with constant-time comparison.

Never log it.

Rotate separately from the provider API key.

UI can show:

```text
VALID
INVALID
NOT CONFIGURED
```

Never show the secret.


# 92. WALLET_QUEUE_URL / WALLET_QUEUE_SECRET

Audit these settings or their current equivalents carefully.

Keep these concepts separate:

```text
HELIUS_API_KEY
= provider API authentication

HELIUS_WEBHOOK_AUTH_SECRET
= Helius → webhook gateway authentication

WALLET_QUEUE_URL
= internal gateway → queue / worker endpoint

WALLET_QUEUE_SECRET
= internal service-to-service authentication
```

`WALLET_QUEUE_URL` is not automatically the Helius RPC URL.

`WALLET_QUEUE_SECRET` is not automatically the Helius API key.

Do not expose any of these values to frontend JSON.

Frontend may show only:

```text
CONFIGURED
NOT CONFIGURED
HEALTHY
UNREACHABLE
AUTH FAILED
```


# 93. DURABLE WEBHOOK INBOX / IDEMPOTENCY

Assume retries and duplicate delivery.

Receiver order:

```text
AUTHENTICATE
VALIDATE
DEDUPE
DURABLY PERSIST
ENQUEUE
RETURN 2XX
```

Heavy work occurs after acknowledgment.

Persist enough metadata to debug:

```text
provider
provider_event_id
chain
tx_signature
slot
event_index
tracked_wallet
provider_time
received_at
payload_hash
auth_valid
processing_state
retry_count
dedupe_key
```

A duplicate delivery must produce ONE logical JARVIS event.


# 94. HELIUS WEBSOCKETS

Audit current use of Helius WebSockets and currently supported subscriptions.

Potential useful methods include:

```text
logsSubscribe
accountSubscribe
signatureSubscribe
slotSubscribe
programSubscribe
transactionSubscribe
```

Verify exact current support and plan requirements.

Possible research architecture:

```text
TIER A
    Helius enhanced WebSocket + webhook redundancy

TIER B
    webhook

TIER C
    filtered webhook
```

Only use dual transport if duplicate logical transactions are deduplicated correctly.


# 95. MULTI-TRANSPORT LATENCY MEASUREMENT

Where WebSocket + webhook both observe the same Tier-A transaction, record:

```text
first_seen_source
websocket_seen_at
webhook_seen_at
slot_time
decode_complete_at
first_quote_at
```

Measure:

```text
WSS median latency
Webhook median latency
P95 latency
delivery reliability
duplicates
misses
```

Do not assume one transport is faster.

Measure it.


# 96. HELIUS STREAM HEALTH PANEL

Display:

```text
RPC status
WSS status
connection age
last message
last pong
last slot
subscriptions
Tier-A subscriptions
reconnect count
resubscription status
median lag
P95 lag
processing backlog
dropped events
```

Implement correct keepalive, reconnect, backoff, and resubscription behavior.


# 97. LASERSTREAM — AUDIT, BENCHMARK, DO NOT BLINDLY ENABLE

Audit current Helius LaserStream support and plan availability.

Possible state:

```text
PLAN_UNAVAILABLE
```

is valid.

If available, benchmark against existing webhook/WSS ingestion.

Measure:

- median latency
- P95 latency
- disconnect rate
- CPU
- memory
- data volume
- duplicate rate
- coverage
- operational complexity
- cost / plan impact

Only adopt if measured benefit justifies it.


# 98. HELIUS DAS / TOKEN DATA

Audit useful DAS capabilities.

Prioritize trading-relevant uses such as:

```text
getAsset
getAssetBatch
getAssetsByOwner
searchAssets
getTokenAccounts
```

Potential uses:

- token metadata
- mint identity
- Token-2022 identification
- wallet holdings
- portfolio reconstruction
- asset ownership context

Do not clutter JARVIS with NFT functionality unless it serves a real wallet/trading research need.


# 99. TOKEN INTELLIGENCE

Every Solana token/mint should use:

```text
MINT ADDRESS
```

as canonical identity.

Never rely on symbol alone.

Token Intelligence may display:

```text
mint
symbol
name
logo
token program
decimals
price
market cap
supply
liquidity
DEX pools
24h volume
pool age
tracked wallets holding
elite-wallet buys
elite-wallet sells
net tracked-wallet flow
wallet consensus
independent clusters
risk flags
recent events
```


# 100. HOLDER CONCENTRATION

Only show holder-distribution statistics if underlying data is sufficiently complete.

Do not calculate:

```text
Top 10 holders own 57%
```

from a partial page.

If completeness is uncertain, show:

```text
PARTIAL HOLDER DATA
```


# 101. SOLANA NETWORK / EXECUTION CONDITIONS

Create a compact Solana network panel.

Potential real metrics:

```text
current slot
block height
RPC latency
WSS latency
confirmation latency
recent performance
blockhash freshness
priority fees
network congestion
provider health
```

This is execution/environment context.

It is not directional SOL alpha.


# 102. PRIORITY FEE INTELLIGENCE

Audit current Helius `getPriorityFeeEstimate` support.

Where appropriate display current priority levels such as:

```text
MIN
LOW
MEDIUM
HIGH
VERY HIGH
```

or current provider equivalents.

For a specific prepared transaction, prefer account/transaction-aware estimates where possible.

Show:

```text
estimated priority fee
requested priority
timestamp
network condition
recent trend
```

Priority-fee impact must feed Copyability/all-in execution friction where relevant.


# 103. PRIORITY FEE HISTORY

If appropriate and cost-efficient, persist enough observations for short execution-condition charts such as:

```text
30M
2H
24H
```

This is particularly useful for:

- DEX trading
- Wallet Alpha copyability
- Solana congestion research


# 104. HELIUS PROVIDER HEALTH

Create a dedicated detailed provider panel in Crypto Desk and/or Ops.

Example:

```text
HELIUS

PLAN                 ...
RPC                  HEALTHY
WSS                  CONNECTED
WEBHOOK              HEALTHY
WALLET API           HEALTHY
DAS                  HEALTHY
PRIORITY FEE         HEALTHY

Last Event           ...
Last RPC             ...
Last WSS Message     ...
Subscriptions        ...
Wallets Monitored    ...

Tier A               ...
Tier B               ...
Tier C               ...

Events Today         ...
Accepted             ...
Filtered             ...
Duplicates           ...
Invalid Auth         ...

Queue Depth          ...
Oldest Queued Event  ...

Median Latency       ...
P95 Latency          ...
```


# 105. HELIUS USAGE / COST CONTROL

Measure provider use.

Create/verify a provider-usage model containing fields such as:

```text
provider
date
requests
credits
events_received
events_accepted
events_duplicate
events_filtered
invalid_auth
bytes_received
active_wallets
events_per_wallet
cost_estimate
projected_month_usage
plan
```

UI should expose:

```text
TODAY
7D AVG
MONTH PROJECTED
PLAN LIMIT
```

Warn before the plan is exhausted.


# 106. COST-AWARE QUERYING

Do not poll expensive provider endpoints merely because a panel is visible.

Suggested information-frequency logic:

```text
wallet identity     → heavy cache
funded-by           → very heavy cache
token metadata      → heavy cache
wallet balances     → moderate refresh
wallet history      → on-demand / pagination
live wallet events  → push transport
provider usage      → periodic
priority fee        → faster only where execution context requires it
```

Use batch APIs where useful.

The UI must not become a provider-credit-burning machine.


# 107. PROVIDER PIPELINE DIAGNOSTICS

Ops should provide a flow visualization:

```text
HELIUS
  ↓
CLOUDFLARE
  ↓
WEBHOOK GATEWAY
  ↓
DURABLE INBOX
  ↓
QUEUE
  ↓
DECODER
  ↓
WALLET ALPHA
```

Each stage should have:

```text
status
last event
latency
error count
```

Use clear GREEN / AMBER / RED semantics.

Click a stage to inspect diagnostics.


# 108. DAILY BRIEF — SOLANA / HELIUS SUMMARY

Do not dump the entire provider desk into the Brief.

Create a compact operator summary.

Example:

```text
SOLANA / WALLET ALPHA

SOL                  $...
24H                  ...

Network              NORMAL
Helius               HEALTHY
Priority Fees        MEDIUM

Tier-A Wallets       42
Elite Events 12h     18
High Copyability     3
Consensus Tokens     4
DEX Survivors        7
```

Click through to the full Solana / Wallet Alpha surface.


# 109. DAILY BRIEF — HIGH-SIGNAL WALLET EVENTS

Only show meaningful Wallet Alpha changes in the Daily Brief.

Examples:

```text
3 independent elite clusters accumulated TOKEN
High-copyability wallet entered TOKEN
Elite wallet exited TOKEN after 14d hold
Large tracked-wallet SOL → USDC rotation
Wallet consensus reversed
Copyability collapsed after price moved before detection
```

Do not show every raw transaction.


# 110. DAILY BRIEF — PROVIDER HEALTH

Compactly summarize critical providers.

Examples:

```text
Kraken
Helius
Coin Metrics
FRED
DEX sources
order-book streams
```

Normal providers remain compact.

Errors expand automatically.

Example:

```text
DATA HEALTH
11 / 12 healthy

HELIUS WSS DEGRADED
Last message 41s ago

[OPEN OPS]
```


# 111. DEX DISCOVERY + WALLET ALPHA INTEGRATION

DEX Discovery and Wallet Alpha should be operationally connected.

DEX candidate rows should show where supported:

```text
ELITE WALLET INTEREST
RAW WALLET COUNT
INDEPENDENT CLUSTERS
TRACKED-WALLET FLOW
COPYABILITY CONTEXT
TOKEN RISK
```

Wallet Alpha token views should show:

```text
DEX LIQUIDITY
POOL AGE
VOLUME
PRICE IMPACT
TOKEN RISK
```

Do not duplicate datasets.

Cross-link them.


# 112. NEW TOKEN SAFETY

Deterministic new-token safety may include, when reliably available:

```text
mint authority
freeze authority
owner privileges
transfer restrictions
Token-2022 extensions
liquidity concentration
liquidity lock / burn
top-holder concentration
developer supply
insider supply
sniper / bundler supply
pool age
liquidity
price impact
```

Unknown safety-critical state should lead to:

```text
LOWER CONFIDENCE
MANUAL ONLY
REJECT
```

depending on policy.

An LLM may explain a deterministic risk block.

It may NOT override it.


# 113. TELEGRAM + HUMAN-IN-THE-LOOP WALLET ALPHA

Where the current Telegram architecture supports it, use Telegram as a human-in-the-loop Wallet Alpha action surface.

Do not alert on every event.

Alert only after deterministic filtering and JARVIS scoring.

An alert may show:

```text
wallet
tier
Trader Quality
Copyability
forward sample
token
wallet entry
current quote
detection delay
entry disadvantage
pool liquidity
suggested notional
maximum allowed
price impact
network / priority fee
all-in cost
after-cost edge
wallet consensus
independent clusters
token risk
```

Potential safe actions:

```text
PASS
REFRESH QUOTE
OPEN DEX
PREPARE SIZE
```

Do not enable autonomous live DEX execution as part of this UI project.


# 114. TELEGRAM / MANUAL EXECUTION STATUS

Track and display where appropriate:

```text
ALERT SENT
VIEWED
QUOTE REFRESHED
DEX OPENED
PREPARED
SIGNED
CONFIRMED
PASSED
EXPIRED
REJECTED
```

A replayed callback must never create duplicate execution.


# 115. WALLET ALPHA RISK BOOK

Wallet Alpha must have a distinct risk budget / book.

Do not silently combine Wallet Alpha exposure with CRYPTO_MAJORS strategy statistics.

Portfolio-level risk can still aggregate exposures for overall safety, but strategy-level evidence and attribution must remain distinct.


# =====================================================================
# HARD ARCHITECTURAL BOUNDARY
# =====================================================================

# 116. WALLET ALPHA / CRYPTO MAJORS ARCHITECTURAL BOUNDARY

This is a **HARD architectural rule**.

Wallet Alpha, Helius, Token Intelligence, DEX Discovery, wallet consensus,
wallet clustering, copyability, and DEX liquidity SHOULD be visually and
operationally connected throughout the UI.

The intended investigation flow is:

```text
HELIUS EVENT
    ↓
WALLET
    ↓
IDENTITY / CLUSTER
    ↓
TOKEN
    ↓
WALLET CONSENSUS
    ↓
DEX / LIQUIDITY
    ↓
COPYABILITY
    ↓
TOKEN / EXECUTION RISK
```

These surfaces should cross-link naturally and share normalized data rather
than duplicating disconnected copies of the same information.

HOWEVER:

```text
WALLET_ALPHA
```

must remain statistically separate from:

```text
CRYPTO_MAJORS
```

Wallet Alpha observations must NEVER automatically enter:

- CRYPTO_MAJORS expectancy
- CRYPTO_MAJORS calibration
- CRYPTO_MAJORS Gate Experiment populations
- CRYPTO_MAJORS model training
- CRYPTO_MAJORS performance statistics
- CRYPTO_MAJORS signal confidence
- CRYPTO_MAJORS risk budget

until JARVIS has explicit scientific evidence and an intentional promotion
process proving that such integration is valid.

The key rule is:

> **UI CONNECTION != MODEL CONTAMINATION**

The UI should make the systems feel connected to the operator while the
research architecture keeps their:

- evidence
- calibration
- expectancy
- risk
- performance
- learning populations

appropriately isolated.

Do not turn Helius into a page full of raw provider data.

Helius data should become operational research context through:

```text
event
→ wallet
→ identity
→ cluster
→ token
→ liquidity
→ independent wallet consensus
→ delayed-entry economics
→ copyability
→ deterministic risk
```

This distinction is mandatory.


# 117. CLUSTER-AWARE CONSENSUS INVARIANT

Same-owner, same-funder, or otherwise strongly related wallet groups must not automatically count as independent evidence.

Always distinguish:

```text
RAW WALLET COUNT
```

from:

```text
INDEPENDENT CLUSTER COUNT
```

Example:

```text
Elite wallets buying      7
Independent clusters      3
```

The operator needs both numbers.


# 118. PROVIDER DATA ≠ FORWARD EDGE

Provider historical wallet rankings or P&L estimates must never be treated as equivalent to JARVIS forward-observed performance.

Use provider data to answer:

```text
WHO SHOULD WE WATCH?
```

Use JARVIS forward observation to answer:

```text
WHO ACTUALLY HAS REPEATABLE FORWARD EDGE?
```

Use Copyability research to answer:

```text
CAN JARVIS REALISTICALLY CAPTURE THAT EDGE AFTER DELAY AND COSTS?
```


# =====================================================================
# ADDITIONAL VISUALIZATION / WORKSTATION UPGRADES
# =====================================================================

# 119. CORRELATION / EXPOSURE MATRIX

If current backend data supports it cleanly, add a compact portfolio correlation / exposure matrix.

Goal:

Identify disguised concentration.

Example:

```text
BTC
ETH
SOL
COIN
MSTR
```

may all represent overlapping crypto beta.

Do not overstate statistical confidence with insufficient history.


# 120. CRYPTO FUNDING HEATMAP

Create a dense heatmap for major supported crypto assets using:

- funding
- funding percentile
- OI change
- liquidation pressure
- crowding

Allow useful windows only where real data exists.

Examples:

```text
1H
4H
24H
```


# 121. CROSS-ASSET CORRELATION SHOCK

Evaluate whether existing market data supports a panel that detects meaningful short-term divergence from historically correlated relationships.

Potential pairs:

```text
BTC vs QQQ
gold vs real-rate proxy
crude vs energy equities
copper vs cyclical equities
```

Only add if the backend can calculate it honestly.


# 122. RISK-ON / RISK-OFF MATRIX

If supported by existing data, add a factual cross-asset matrix.

Do not create a magical opaque score.

Show actual component states and the reason for classification.


# 123. CHANGE BADGES

Where history exists, display genuinely meaningful transitions:

```text
NEW
ESCALATED
NORMALIZED
RECOVERED
EXTREME
DEGRADED
```

This should reduce memory burden for the operator.


# 124. "WHY THIS MATTERS" CONTEXT

For unusual observations, provide one concise deterministic interpretation.

Examples:

```text
BTC funding: 97th percentile
→ leveraged longs unusually crowded relative to recent history
```

```text
Copyability: 18/100
→ observed wallet edge typically decays before realistic entry
```

Do not generate AI commentary for every metric.


# 125. REUSABLE VISUALIZATION PRIMITIVES — EXPANDED

Standardize reusable components where they help:

```text
MiniPriceChart
MiniTimeSeries
MetricTrend
BreadthBar
PercentileGauge
DistributionBar
HeatmapGrid
LatencyTrend
LatencyWaterfall
FlowChart
NetworkGraph
DepthChart
AllocationDonut
TreasuryCurve
Timeline
```

Reuse the existing visual language.

Do not create ten incompatible mini chart systems.


# 126. PERFORMANCE / SVELTE REACTIVITY

Audit whether high-frequency updates cause excessive page rerenders.

Prefer:

- derived stores
- selective subscriptions
- batched UI updates
- virtualized large lists
- cleanup of listeners
- shared data stores where appropriate

Do not throttle underlying safety/risk calculations.

Visual rendering may be throttled when provider frequency is much higher than a human can meaningfully perceive.


# 127. POLLING / TRANSPORT MATRIX

Explicitly document and audit transport choice.

Example:

| Data | Preferred Transport |
|------|---------------------|
| Order book | WebSocket |
| Helius Wallet events | Webhook / WSS |
| Price ticks | Stream / short refresh |
| Wallet identity | Cache |
| Wallet funded-by | Long cache |
| Wallet history | On-demand / paginated |
| Coin Metrics | Daily |
| FRED | Slow / release-aware |
| COT | Weekly |
| Congress | Slow |
| Provider usage | Periodic |

Prevent duplicate polling across components.


# 128. UI SECURITY REVIEW

As part of the UI audit, verify that no frontend route, state object, debug panel, API response, browser console message, screenshot, or source-map path exposes:

- API keys
- webhook auth secret
- wallet queue secret
- seed phrases
- private keys
- broker credentials
- admin tokens

A UI health panel should show state, never secret material.


# 129. UPDATED IMPLEMENTATION PRIORITY

Use approximately this priority:

## P0 — Dangerous / Incorrect

- wrong values
- stale values presented as live
- wrong position counts
- reversed directions
- unit errors
- secret exposure
- unsafe execution controls
- duplicate wallet events
- public API exposure
- cluster counts treated as independent wallet counts

## P1 — Major Backend Intelligence Missing From UI

Especially:

- Helius
- Wallet API
- Wallet Alpha
- DEX Discovery
- On-chain
- Derivatives
- Risk
- Provider health
- Copyability
- Solana network state

## P2 — Daily Brief Command Center

## P3 — Crypto / Solana / Wallet Alpha Desk

## P4 — Workstation Interaction Model

- saved layouts
- resizable panels
- inspectors
- global search
- command palette
- linked context

## P5 — Visualization / Polish / Accessibility


# 130. UPDATED DAILY BRIEF HIERARCHY

A strong layout is approximately:

```text
────────────────────────────────────────────────────────────
WHAT CHANGED / CRITICAL ALERTS
────────────────────────────────────────────────────────────

MARKET / ACCOUNT KPI STRIP

────────────────────────────────────────────────────────────
MARKET PULSE               | CRYPTO PULSE
────────────────────────────────────────────────────────────

CRYPTO DERIVATIVES         | PORTFOLIO RISK
────────────────────────────────────────────────────────────

SOLANA / HELIUS            | WALLET ALPHA
────────────────────────────────────────────────────────────

CATALYSTS                  | THREATS / NEWS
────────────────────────────────────────────────────────────

ON-CHAIN                   | POSITIONING / CURVES
────────────────────────────────────────────────────────────

DEX DISCOVERY              | SMART MONEY
────────────────────────────────────────────────────────────

GATE / LEARNING            | DATA / PROVIDER HEALTH
────────────────────────────────────────────────────────────

INCUBATOR / SECONDARY CONTEXT
```

Adjust based on the actual data and available screen width.

Do not force equal-height cards.


# 131. UPDATED CRYPTO DESK INFORMATION ARCHITECTURE

Potential Crypto Desk organization:

```text
OVERVIEW
MAJORS
DERIVATIVES
MICROSTRUCTURE
SOLANA
HELIUS
DEX DISCOVERY
WALLET ALPHA
COPYABILITY
WALLET GRAPH
ON-CHAIN
VENUES
PROVIDER HEALTH
```

Use tabs/subviews where preferable to endless vertical scrolling.


# 132. UPDATED CROSS-LINKING REQUIREMENTS

Add direct navigation where practical:

```text
BTC
→ Charts / BTC

Position warning
→ Positions / selected position

Critical threat
→ Intelligence / event

Congress disclosure
→ Smart Money / symbol

DEX candidate
→ Token Intelligence / DEX detail

Helius wallet event
→ Wallet Profile

Wallet Profile token
→ Token Intelligence

Token Intelligence
→ DEX pools

Token Intelligence
→ Wallet Consensus

Wallet Consensus
→ Copyability

Provider failure
→ Ops diagnostics
```

Use the existing routing / store architecture.


# 133. UPDATED SCREENSHOT / VISUAL VALIDATION

After implementation, capture and inspect where possible:

- Morning / Daily Brief
- Command Center
- Crypto Overview
- Derivatives
- Solana / Helius
- Wallet Alpha
- Wallet Profile
- Token Intelligence
- Copyability
- Wallet Graph
- DEX Discovery
- Charts
- Macro
- Smart Money
- Performance
- Ops

Verify at:

```text
1280px
1440px
1920px+
```

Look for:

- wasted space
- giant cards
- poor alignment
- clipping
- unreadable tables
- tiny charts
- table overflow
- huge legends
- excessive scrolling
- poor panel hierarchy
- pop-out inconsistencies
- layout persistence bugs


# 134. UPDATED TESTING REQUIREMENTS

In addition to all existing tests, add deterministic tests where appropriate for:

- Helius adapter normalization
- Wallet API partial/missing fields
- wallet identity
- wallet balances
- wallet funded-by
- transaction history
- webhook auth valid
- webhook auth invalid
- duplicate webhook delivery
- malformed payload
- unknown event type
- queue failure
- provider error
- WSS reconnect
- subscription restoration
- multi-transport dedupe
- provider latency measurement
- Copyability timing
- cluster-aware consensus
- partial holder data
- plan unavailable state
- provider usage projection
- frontend API types
- stale/degraded states

Use provider fixtures.

Do not require live provider access for deterministic unit tests.


# 135. PROVIDER FIXTURES

Create fake/test fixtures for representative Helius data such as:

```text
swap
transfer
liquidity event
duplicate event
unknown event
failed transaction
wallet identity
batch identity
balances
history
transfers
funded-by
DAS asset
priority fee estimate
rate limit
provider failure
partial beta response
```

Never include real credentials.


# 136. UPDATED UI_AUDIT.md STRUCTURE

Add sections:

```text
Executive Summary

P0 Issues

P1 Issues

Broken Functionality

Backend → API → UI Parity

Helius Capability Matrix

Wallet Alpha Matrix

DEX Matrix

New Panels

New Visualizations

New Drilldowns

Workstation / Layout Changes

Polling / Performance Fixes

Provider Usage / Cost

Security Findings

Remaining Limitations

Tests
```


# 137. FINAL VALIDATION — EXPANDED

Before completion:

1. Run full Python tests.
2. Run `npm run check`.
3. Run `npm run build`.
4. Start JARVIS in a safe environment.
5. Visit every section.
6. Exercise every changed interaction.
7. Test global search.
8. Test command palette.
9. Test saved layouts.
10. Test layout reset.
11. Test panel maximize.
12. Test pop-outs.
13. Test linked context.
14. Inspect console.
15. Inspect failed network requests.
16. Inspect WebSocket state.
17. Inspect Helius stream health.
18. Inspect webhook health.
19. Inspect internal queue health.
20. Verify representative UI numbers against raw APIs.
21. Verify stale/error states.
22. Verify Daily Brief windows.
23. Verify 1280/1440/1920+ layouts.
24. Verify no real broker order was submitted.
25. Verify no real DEX transaction was submitted.
26. Verify no production data was damaged.
27. Verify no secret reached frontend/logs/screenshots.
28. Verify normal JARVIS API remains private.


# 138. FINAL REPORT — EXPANDED

When finished, report:

```text
## P0 Problems Found

## P1 Problems Found

## Problems Fixed
Exact files changed.

## Backend Capabilities Previously Missing From UI

## UI / UX Improvements
- layouts
- panel resizing
- panel ordering
- focus mode
- popouts
- inspectors
- global search
- command palette
- linked context
- table improvements
- density modes
- performance changes

## Daily Brief Improvements

## Crypto Desk Improvements

## Helius Improvements
- RPC
- Wallet API
- webhooks
- WebSockets
- DAS
- priority fees
- provider health
- provider usage

## Wallet Alpha Improvements
- wallet profiles
- live tape
- consensus
- clusters
- flow
- copyability
- graphs
- Telegram state

## DEX Improvements

## Charts Added

## Security Findings / Fixes

## Polling / Performance Improvements

## Remaining Gaps

## Exact Python Test Results

## npm run check

## npm run build

## Runtime Validation

## Commits
```

Do not merge into `main` automatically.


# 139. FINAL PRODUCT STANDARD

Do not merely make JARVIS display more information.

Make it easier to **think with the information**.

Do not create a giant wall of cards.

Create:

- hierarchy
- relationships
- drill-down
- context
- provenance
- freshness
- useful visualization
- dense exact tables where numbers matter
- alerts where attention is warranted
- cross-linking between related domains
- transparent provider health
- statistically honest evidence boundaries

The target is not:

```text
MORE DASHBOARD
```

The target is:

> **A PROFESSIONAL, CONFIGURABLE, HIGH-INFORMATION TRADING AND INTELLIGENCE WORKSTATION BUILT AROUND THE ACTUAL CAPABILITIES JARVIS NOW HAS.**

The operator should be able to move naturally from:

```text
MARKET
→ SIGNAL
→ CHART
→ POSITION
→ RISK
```

and:

```text
HELIUS EVENT
→ WALLET
→ IDENTITY / CLUSTER
→ TOKEN
→ WALLET CONSENSUS
→ DEX LIQUIDITY
→ COPYABILITY
→ RISK
```

without hunting through disconnected screens.

Preserve JARVIS's:

- evidence-first architecture
- risk boundaries
- provenance
- timing discipline
- strategy isolation
- current visual identity

while making the frontend match the sophistication of the platform behind it.

---

# 140. OPERATOR ADDENDUM — 2026-08-15

Added from live operator review of the running desk. These supersede the
§129 ordering where they conflict: a book that cannot open a trade and a
brief that cannot be read are ahead of polish.

## 140.1 BOOK RESET MUST PRODUCE A USABLE BOOK — P0

Reported SIX times. "Reset to $100k while closing every open trade and
keeping the learning data" is what the buttons claim; what the operator
observes is the cash resetting and the book refilling immediately.

Requirements:

- Reset closes every OPEN position through the normal close path, so each
  lands in history as a real trade. Trade rows, outcomes, calibration and
  postmortems all survive — never a hard delete to move a number.
- A reset must leave a book the operator can actually inspect. Refilling
  within one scan cycle is indistinguishable from the reset not working.
  Reset therefore PAUSES automatic opening on the books it reset, and says
  so in its response and on the button.
- Resuming is an explicit, separate action.
- The reset must be idempotent and must not walk closed rows.

## 140.2 SIGNAL CARDS SHOW UNMEASURED FOR ALMOST EVERYTHING — P0

**Root cause MEASURED 2026-08-15, fix still outstanding.**

`UNMEASURED` renders when `gate_decision` is null. That value is
`CandidateSignal.gate_v8_decision`, joined to the signal by `signal_id`.

The numbers, on the live desk: **138 active signals, 95 with a verdict,
43 UNMEASURED.** The 43 carry the SAME `generated_at` as measured ones in
the same batch — `2026-08-15T13:08:17` on both — so the old tooltip
("signal predates the gate experiment") was simply false, and the card now
says the true thing instead.

The real defect: those 43 signals have **no linked `CandidateSignal`
row**, while 85 and 70 unlinked candidate rows exist for the very symbols
they name. The back-link is not being attached. `lib/candidates.py`
documents this exact failure as fixed on 2026-08-16 — "returning early
orphaned the link and left the signal card reading UNMEASURED forever" —
so either the fix is incomplete or a second path writes signals without
recording a candidate.

To do: find the path that creates a signal without linking its candidate
row, link it, and backfill the orphans by (symbol, timeframe, direction,
epoch). Then UNMEASURED means only what it says.

## 140.3 PANELS MUST NOT RE-DERIVE EVERYTHING AFTER A RESTART — P0

After a restart, a crash or a bluescreen the Morning Brief sits on
"assembling…" and the Intelligence panels come back slowly or empty. A
store exists; the requirement is that these panels READ it on cold start
and show the last good snapshot with its age, rather than blocking on a
full recomputation. Stale-with-a-timestamp beats empty.

## 140.4 MORNING BRIEF — NEWS THAT AFFECTS PRICE — P1

The brief needs a real news surface, not a headline count:

- CLICKABLE links to the source.
- Scoped to what can move a position: company and product announcements
  (e.g. a chip that raises AI throughput and cuts power), defense and
  conflict, sanctions and export controls, regulation and legislation,
  statements by heads of state and central bankers, elections.
- Each item tagged with the instruments or sectors it plausibly affects,
  and with the direction it argues for — as a hypothesis, never a
  prediction, in the same language §26 requires of exchange flow.
- Refreshed approximately three times a day, with the refresh clock and
  the age of the newest item both visible.

## 140.5 FOUR-CHART GRID — P1

The Charts page carries FOUR independent charts in a 2x2 grid, each with
its own symbol and timeframe, so four instruments can be read at once for
manual trading and for reviewing a produced signal against its peers.
Selections persist across reloads.

## 140.6 CONGRESSIONAL AND OFFICIAL DISCLOSURES — COVERAGE AND DRILL-DOWN — P1

Both the "Congressional Trade Disclosures" and "Trades by Official"
panels are truncated far below the real population — Congress, the
Senate and the executive together are hundreds of filers, not forty.
Requirements: full ingested coverage with the cap stated when one is
applied, and every row clickable through to that person's filings — what
was bought or sold, when, size band, and the price at the time.

## 140.7 INTELLIGENCE TAB — P1

Reported as never having worked. Audit every panel on it against its data
source, fix or honestly state the gap, and move anything that belongs on
the brief to the brief with broader scope and more functionality.

## 140.8 CRYPTO DESK — P1

Expand now that the Helius, DEX, on-chain and derivatives surfaces exist.
See §131 for the intended IA; the addendum is that the data is now
available and the desk should reflect it.

## 140.9 PERFORMANCE TAB — P2

Needs more information and more tools. Treat §125's visualization
primitives as the starting inventory.

## 140.10 MACRO DESK — USE WHAT IS PAID FOR — P1

The Macro tab shows a fraction of what the desk has access to. Providers
are configured and in several cases PAID FOR, and the surface does not
reflect them.

Requirement: inventory every configured provider and RPC — FRED, Twelve
Data, FMP, Bigdata, CoinGecko, Massive, Helius and the rest — list what
each actually returns on this plan, and surface the macro-relevant series
that are currently unused. Yields and the curve, inflation and its
components, employment, growth, policy rates and the expected path,
credit spreads, the dollar, commodities, liquidity. Each series carries
its own release schedule and its information clock (§30), because a
monthly print rendered next to a live quote without that label is a
misread waiting to happen.

Anything a provider does NOT return on the current plan is stated as
such, so an empty panel is never mistaken for a quiet market.
