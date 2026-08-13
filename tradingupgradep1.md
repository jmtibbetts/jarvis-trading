I want you to substantially upgrade JARVIS's existing technical-analysis and
market-feature engine across ALL supported asset classes:

- Cryptocurrency spot
- Cryptocurrency margin/perpetuals/futures
- Stocks
- ETFs
- Forex
- Futures
- Commodities
- Indices

IMPORTANT:

Do NOT rewrite the existing TA engine from scratch unless absolutely necessary.

First inspect:
- lib/ta_engine.py
- lib/ta_extensions.py
- market data ingestion
- order-book code
- crypto derivatives code
- market regime code
- signal generation
- signal scoring
- signal fusion
- backtester
- learning engine
- existing tests

Preserve existing indicators and working functionality.

The goal is NOT to add hundreds of indicators.

The goal is to create a high-quality MARKET STATE FEATURE ENGINE that extracts
independent information from:

PRICE
TREND
MOMENTUM
VOLATILITY
VOLUME
ORDER FLOW
MARKET STRUCTURE
VALUE
DERIVATIVES
MICROSTRUCTURE
RELATIVE STRENGTH
REGIME

Indicators should primarily describe market state.

They should NOT simply vote BUY/SELL independently.

==================================================
1. EXISTING INDICATORS
==================================================

Preserve and improve:

VOLUME
- current
- avg_20
- surge_ratio
- surge
- dry

MACD
- macd
- signal
- histogram
- histogram slope
- histogram acceleration
- trend
- crossover
- zero-line position

OBV
- value
- slope
- accumulation
- distribution
- divergence

MFI
- value
- slope
- overbought
- oversold
- divergence

VWAP
- value
- pct_diff
- ATR-normalized distance
- above/below
- reclaim
- rejection

DONCHIAN
- upper
- lower
- midpoint
- breakout_up
- breakout_down
- channel_width
- normalized channel position

SUPERTREND
- direction
- level
- distance
- flipped_this_bar
- bars_since_flip

MARKET STRUCTURE
- swing highs
- swing lows
- HH
- HL
- LH
- LL
- BOS
- CHoCH

BOLLINGER / KELTNER
- band position
- bandwidth
- squeeze
- expansion
- contraction

ADX / DMI
- ADX
- +DI
- -DI
- DI spread
- rising/falling
- trend strength

STOCHASTIC
CCI
WILLIAMS %R
PIVOTS
SUPPORT/RESISTANCE

Do not double-count highly correlated oscillators.

==================================================
2. ATR / VOLATILITY NORMALIZATION
==================================================

Make ATR a foundational normalization mechanism.

Calculate:

atr
atr_pct
atr_percentile

atr_5
atr_20
atr_100

atr_ratio_short_long

volatility_expanding
volatility_contracting

Normalize distances:

distance_to_vwap_atr
distance_to_ema20_atr
distance_to_ema50_atr
distance_to_support_atr
distance_to_resistance_atr
distance_to_stop_atr
distance_to_target_atr

This allows features to be compared across:

BTC
SOL
SHIB
NVDA
EUR/USD
Gold
Crude Oil

without raw price differences corrupting interpretation.

==================================================
3. CUMULATIVE VOLUME DELTA / ORDER FLOW
==================================================

Add CVD where the available data genuinely supports aggressor-side inference.

Calculate:

cvd
cvd_delta_1
cvd_delta_5
cvd_delta_20

cvd_slope
cvd_acceleration

price_cvd_divergence

bullish_divergence
bearish_divergence

high_confirmation
low_confirmation

IMPORTANT:

Do NOT fabricate CVD from ordinary OHLCV when true trade-side information
is unavailable.

If only an approximation is possible:

mark:

cvd_quality = ESTIMATED

instead of:

cvd_quality = TRUE_TRADE_FLOW

==================================================
4. VOLUME PROFILE
==================================================

Implement volume profile where sufficient data exists.

Calculate:

POC
VAH
VAL

HVN
LVN

distance_to_poc
distance_to_vah
distance_to_val

inside_value_area

value_area_acceptance
value_area_rejection

Use volume-at-price rather than simply time-based volume.

==================================================
5. ANCHORED VWAP
==================================================

Add explicit VWAP types.

For equities:

session_vwap
weekly_vwap
anchored_vwap

For crypto:

UTC_session_vwap
rolling_vwap
weekly_vwap
anchored_vwap

For futures:

session VWAP must respect the actual futures session.

For forex:

VWAP should only be used when volume data is meaningful.

Do not treat broker tick volume as centralized exchange volume.

Potential AVWAP anchors:

major swing high
major swing low
BOS
CHoCH
major breakout
high-volume event
session open
week open
month open

Track:

price_vs_avwap
distance_atr
reclaim
rejection
cross
bars_since_cross

==================================================
6. GENERIC DIVERGENCE ENGINE
==================================================

Create a reusable divergence detector.

Support:

regular bullish
regular bearish
hidden bullish
hidden bearish

Potential sources:

RSI
MACD histogram
OBV
MFI
CVD

Use confirmed swing points.

Do not identify divergence from arbitrary adjacent candles.

Store divergence strength and age.

==================================================
7. RELATIVE STRENGTH
==================================================

This is NOT RSI.

Create benchmark-relative strength.

STOCKS:

stock vs SPY
stock vs QQQ where appropriate
stock vs sector ETF

Examples:

NVDA / SPY
NVDA / QQQ
NVDA / SOXX

CRYPTO:

alt vs BTC
alt vs ETH
asset vs crypto benchmark/basket

Examples:

SOL/BTC
AAVE/ETH

FUTURES:

contract vs relevant benchmark/complex where meaningful.

FOREX:

currency strength should be calculated across multiple pairs.

Example:

EUR strength should not depend solely on EUR/USD.

Construct currency strength from EUR exposure across:

EUR/USD
EUR/GBP
EUR/JPY
EUR/CHF
etc.

Track:

relative_strength_1
relative_strength_5
relative_strength_20

rs_slope
rs_acceleration
rs_breakout
rs_divergence

==================================================
8. MARKET MICROSTRUCTURE
==================================================

Where L2/L3/order-book data exists calculate:

spread
spread_bps
spread_percentile

bid_depth
ask_depth

depth_5bps
depth_10bps
depth_25bps
depth_50bps

orderbook_imbalance

Calculate imbalance at MULTIPLE depths.

Do not rely on a single top-of-book number.

Also calculate:

microprice
microprice_vs_mid

book_pressure
book_slope

liquidity_wall_above
liquidity_wall_below

replenishment
absorption

imbalance_persistence

Do not assume a large visible wall is real support/resistance.

Require persistence and/or executed-flow confirmation.

==================================================
9. CRYPTO DERIVATIVES FEATURES
==================================================

For crypto derivatives add:

funding_rate
funding_percentile
funding_zscore
funding_acceleration

open_interest
oi_change
oi_change_pct
oi_zscore

price_oi_relationship

basis
annualized_basis

long_short_ratio where trustworthy

liquidations_long
liquidations_short
liquidation_imbalance

liquidation_clusters_above
liquidation_clusters_below

distance_to_nearest_liquidation_cluster

Do not treat funding independently as BUY or SELL.

Create combined states.

Examples:

PRICE UP + OI UP
= new leveraged participation

PRICE UP + OI DOWN
= covering / position closure

PRICE DOWN + OI UP
= new short participation

PRICE DOWN + OI DOWN
= deleveraging / long liquidation

Then overlay funding and liquidations.

==================================================
10. STOCK-SPECIFIC FEATURES
==================================================

For stocks add where data exists:

relative volume by TIME OF DAY

premarket volume
premarket high/low

opening range

gap percentage

gap relative to ATR

previous day:
high
low
close

previous week:
high
low

earnings proximity

sector relative strength

index relative strength

market breadth context

Do not compare 9:35 AM volume naïvely against noon volume.

==================================================
11. FOREX-SPECIFIC FEATURES
==================================================

Forex requires different interpretation.

Add:

Asian session high/low
London session high/low
New York session high/low

session sweep

previous day high/low

ATR by session

currency strength

DXY context for USD pairs where useful

rate-differential context if existing macro data supports it

spread regime

rollover proximity

Do not treat decentralized FX tick volume as equivalent to centralized
exchange volume.

==================================================
12. FUTURES / COMMODITY FEATURES
==================================================

For futures:

respect contract specifications.

Track:

contract
front month
expiration
days_to_expiry

roll period
volume migration
open-interest migration

term structure where applicable

contango
backwardation

session structure

overnight high/low

regular-session high/low

settlement

previous settlement

For commodities where data permits:

inventory context
curve structure
calendar spreads

Never allow contract rollover to appear as a giant market move.

==================================================
13. MARKET STRUCTURE ENGINE
==================================================

Upgrade structure detection.

Track:

HH
HL
LH
LL

BOS_UP
BOS_DOWN

CHOCH_UP
CHOCH_DOWN

break strength
break volume
break distance ATR

failed breakout
failed breakdown

liquidity sweep high
liquidity sweep low

sweep_and_reclaim

support/resistance tests

level strength

level age

touch count

rejection strength

==================================================
14. SQUEEZE / VOLATILITY ENGINE
==================================================

Use:

Bollinger bandwidth
Keltner width
ATR percentile
realized volatility

Detect:

COMPRESSION
NORMAL
EXPANSION
EXTREME

A squeeze is not automatically directional.

Direction must come from other evidence.

==================================================
15. OUTPUT SCHEMA
==================================================

Return structured feature data.

Example:

{
  "trend": {...},
  "momentum": {...},
  "volume": {...},
  "order_flow": {...},
  "structure": {...},
  "volatility": {...},
  "value": {...},
  "relative_strength": {...},
  "microstructure": {...},
  "derivatives": {...},
  "asset_specific": {...},
  "data_quality": {...}
}

Every feature should contain enough provenance to determine:

source
timestamp
freshness
quality
estimated vs observed

==================================================
16. DATA AVAILABILITY
==================================================

Not every market provides every feature.

DO NOT fabricate missing information.

Examples:

No reliable aggressor trades
-> CVD unavailable

No centralized forex volume
-> do not pretend tick volume is exchange volume

No derivatives market
-> funding/OI unavailable

No L2
-> order-book imbalance unavailable

Missing features should be:

null / unavailable

not:

0

Zero is a measurement.

Unavailable means no measurement.

==================================================
17. TESTS
==================================================

Add comprehensive tests for:

ATR normalization
CVD
divergence
volume profile
VWAP session resets
anchored VWAP
market structure
BOS
CHoCH
liquidity sweeps
relative strength
order-book imbalance
microprice
funding/OI states
forex sessions
futures rollover
stock RVOL

No future data may enter calculations.

==================================================
18. PERFORMANCE
==================================================

Keep the live calculation path fast.

Use incremental calculations where practical.

Do not recompute entire history for every tick.

Cache expensive features.

Separate:

LIVE FEATURE ENGINE

from:

RESEARCH / BACKTEST FEATURE ENGINE

==================================================
FINAL OBJECTIVE
==================================================

JARVIS should describe the MARKET STATE accurately.

It should NOT make a trade merely because:

MACD bullish
RSI bullish
Supertrend bullish

Indicators are measurements.

Strategies decide how those measurements should be interpreted.

Do not implement arbitrary new BUY/SELL weights in this phase.