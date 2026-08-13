# Feature Engine + Strategy Layer — implementation plan

Measured against `tradingupgradep1.md` (feature engine) and
`tradingupgradep2.md` (strategy/regime/confluence layer), by grepping the
actual codebase rather than assuming.

---

## What we already have

Genuinely done, and in some cases ahead of what the docs ask for:

| Area | State |
|---|---|
| Squeeze / volatility engine | **4/4** — BB bandwidth, Keltner, squeeze, expansion |
| Provenance & data quality | **3/3** — `data_quality`, `freshness`, estimated-vs-observed labelling |
| Strategy invalidation | present — every signal carries an invalidation clause |
| Cost-aware filtering | `min_viable_stop_pct`, `cost_r`, venue fees, funding, spread — **rebuilt today and now correct** |
| Strategy performance tracking | `lib/calibration.py` scores by strategy, timeframe, score band, symbol |
| Named strategies | 5 built today: breakout, trend_continuation, mean_reversion, range_fade, momentum |
| Core indicators | MACD, RSI, ADX/DMI, BB, Keltner, Donchian, Supertrend, Stoch, CCI, Williams %R, MFI, OBV, VWAP, pivots, S/R, swings, BOS/CHoCH |
| EV scaffolding | `lib/ev_model.py` — bucket keys, Wilson intervals, avg win/loss R |
| Derivatives collection | funding rate, OI, long/short ratio, liquidations (OKX) |

The docs say "do not rewrite the TA engine" — correct. The indicator
coverage is good. **What is missing is not indicators; it is the layer that
makes indicators comparable, and the layer that interprets them.**

---

## What is missing, by measured gap

### P1 — feature engine

| Area | Coverage | Verdict |
|---|---|---|
| ATR normalization | **0/7** | **Critical.** Foundational to everything else |
| Divergence engine | 0/4 | High value, moderate cost |
| Relative strength | 0/4 | High value, low cost (we have the data) |
| Structure engine (sweeps, level age/touches) | 2/7 | Unlocks 4 P2 strategies |
| Derivatives *states* | 2/6 | Data collected, never combined into states |
| Anchored VWAP | 0/5 | Moderate |
| Volume profile | 1/6 | Moderate |
| Microstructure | 0/6 | Only honest for 8 Kraken-streamed symbols |
| CVD / order flow | 0/5 | **Only honest for those same 8** — see below |
| Stock / forex / futures specifics | ~0 | Low priority for how this desk trades |

### P2 — strategy / regime layer

| Area | Coverage | Verdict |
|---|---|---|
| Contradiction engine | 0/2 | **Highest value in either document** |
| Multi-dimensional regime | 3/6 | Current regime is a single label; needs 4 axes |
| Net EV after costs | 2/5 | Pieces exist, never assembled |
| Multi-timeframe hierarchy | 0/3 | High value, low cost |
| Confluence by category | 0/2 | Fixes correlated double-counting |
| Setup state machine | 1/4 | Would stop re-signalling the same setup every cycle |
| 10 further strategies | 0 | Gated behind P1 features |
| Meta-labeling | 0/3 | **Defer** — P2 itself says not without sample size |
| Walk-forward / OOS | 1/3 | Needed before trusting any of this |

---

## Judgments where I disagree with the docs, or would sequence differently

**CVD must stay narrow.** P1 says do not fabricate CVD from OHLCV, and
that constraint bites hard here: true aggressor-side data exists only for
the 8 symbols on the Kraken WebSocket. Building CVD across the whole
universe would mean `cvd_quality = ESTIMATED` on almost everything — a
number that looks like order flow and is not. Build it for the 8, label it,
and let strategies that require it simply not fire elsewhere.

**Meta-labeling is premature.** P2 says "do not introduce ML unless sample
size is sufficient". We have 8,899 outcomes but they are pre-fix — produced
while fees were wrong, stops fired on noise, and confidence was inverted.
Training a model on that history would learn the bugs. Revisit after a few
thousand post-fix outcomes.

**Session strategies (FX/futures/stock-open) are low priority.** They are a
large surface area and this desk trades crypto and equities opportunistically,
not the London open. Build the framework so they can be added; do not build
them now.

**ATR normalization goes first, ahead of everything.** The docs list it as
section 2 of P1; I would treat it as the single most important item in
either document. Today's session was a string of bugs where a quantity
measured in one unit was consumed in another — per-contract vs percentage
fees, units vs contracts, a $15 cap against $12,000 of notional. Expressing
every distance in ATR is the same fix applied to the feature layer, and
it makes BTC, SHIB and NVDA comparable without raw price corrupting
interpretation.

**The contradiction engine is the highest-value single item.** P2 §22.
Today the system reported "conf 83%" on a setup whose measured win rate was
~30%, because disagreeing evidence was averaged away. Exposing
`supporting_evidence` and `contradicting_evidence` separately, and letting
contradiction reduce confidence, directly attacks the failure mode this
codebase keeps producing.

---

## Phases

Each phase ships independently, with tests, and is verifiable against live
data before the next begins.

### Phase 1 — ATR normalization + relative strength *(foundation)*

- `atr_5`, `atr_20`, `atr_100`, `atr_percentile`, `atr_ratio_short_long`
- `volatility_expanding` / `contracting`
- All distances in ATR: to VWAP, EMA20/50, support, resistance, stop, target
- Relative strength: alt vs BTC/ETH, stock vs SPY/QQQ/sector
- `rs_slope`, `rs_breakout`

**Why first:** everything downstream compares across assets, and nothing
else in either document works properly without it. Cheap — the inputs all
exist.

**Done when:** a 2% move on SHIB and a 2% move on NVDA produce different
normalized distances, and RS is available for every tracked symbol.

### Phase 2 — contradiction + confluence by category

- Group evidence into independent categories: structure, trend, momentum,
  volume, flow, volatility, relative strength, derivatives
- Category-level confidence rather than per-indicator votes (RSI, Stoch,
  CCI and Williams %R largely describe the same thing and currently count
  four times)
- `supporting_evidence` / `contradicting_evidence` exposed, never averaged
- Contradiction reduces calibrated confidence and EV

**Why second:** it needs no new data, and it attacks the exact failure
mode measured today.

**Done when:** a signal with bullish price and bearish flow shows both, and
scores lower than one with neither contradiction.

### Phase 3 — structure engine + divergence

- Liquidity sweeps, sweep-and-reclaim, failed breakout / breakdown
- Level age, touch count, rejection strength, break volume, break distance in ATR
- Generic divergence detector on confirmed swings only: regular and hidden,
  bullish and bearish, over RSI / MACD hist / OBV / MFI, with strength and age

**Why third:** unlocks four P2 strategies (breakout retest, failed
breakout, liquidity sweep reversal, absorption) that cannot be built
without it.

### Phase 4 — multi-dimensional regime + multi-timeframe hierarchy

- Four independent axes: trend, volatility, liquidity, flow — replacing the
  single label
- Per-asset-class regime inputs (BTC/ETH trend and crypto breadth for
  crypto; SPY/QQQ/VIX/breadth for equities) — not SPY for everything
- Explicit `higher_timeframe_context` / `setup_timeframe` /
  `execution_timeframe`, without requiring every timeframe to agree

**Why fourth:** strategies need to know when they are appropriate. Mean
reversion in a strong trend is the classic way to lose money with a
technically valid setup.

### Phase 5 — net EV and the NO_TRADE gate

- Per (strategy × asset class × direction × regime × timeframe), with
  hierarchical fallback: P(win), avg win R, avg loss R, gross EV, expected
  costs, **net EV**, sample size, confidence interval
- A beautiful setup with negative net expectancy returns NO_TRADE

**Why fifth:** needs Phases 1-4 to have something worth bucketing by, and
the cost model it depends on is already correct as of today.

### Phase 6 — the remaining strategies

Built on Phases 1-4: breakout retest, failed breakout, liquidity sweep
reversal, squeeze expansion, momentum ignition, VWAP/AVWAP reclaim,
relative-strength breakout, funding/positioning squeeze, liquidation
cascade, absorption reversal.

Each with its own deterministic invalidation, each scored independently by
the calibration table.

### Phase 7 — setup state machine + lifecycle

- WATCHING → APPROACHING → BREAKING → CONFIRMED → RETESTING → ENTRY_READY →
  INVALIDATED, instead of an independent signal every cycle
- Strategy lifecycle: ACTIVE / REDUCED / EXPERIMENTAL / SHADOW / DISABLED,
  driven by out-of-sample expectancy

### Phase 8 — walk-forward and out-of-sample validation

- Walk-forward over the backtester, explicit lookahead prevention
- Shadow mode: a strategy runs and is scored without trading
- Thresholds never optimized against the full dataset

**Deferred indefinitely:** meta-labeling (insufficient clean sample),
FX/futures/stock-open session strategies (large surface, little use here),
full-universe CVD (dishonest without aggressor data).

---

## Anchoring principle, from both documents

> Indicators are measurements. Strategies decide how those measurements
> should be interpreted.

and

> A high-quality NO_TRADE decision is as valuable as finding a trade.

Both are the same discipline this codebase spent today learning the
expensive way: **do not let a number claim more than it measured.**
