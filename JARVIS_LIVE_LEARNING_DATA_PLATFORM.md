# CLAUDE.md — JARVIS Live-Learning Data Platform

> **Goal:** Turn JARVIS into a continuously learning trading research and execution platform by ingesting rich live stock, options, crypto CEX, crypto derivatives, DEX, and on-chain data; preserving the raw events; generating deterministic feature snapshots; resolving future outcomes for every candidate signal; and continuously producing training datasets for TA research, strategy discovery, signal generation, meta-labeling, path prediction, execution analysis, and CPU/NPU predictive models.
>
> **Primary principle:** Do not buy overlapping data merely to display more quotes. Use paid sources for information JARVIS cannot cheaply reconstruct, direct exchange feeds where practical, and store everything locally so the proprietary dataset becomes more valuable over time.

---

# 0. Chosen Starting Stack

Use this as the initial live-learning stack unless the user explicitly changes it:

```text
ALPACA ALGO TRADER PLUS       ~$99/month
│
├── U.S. stocks
├── ETFs
├── SIP market data
├── stock trades
├── stock quotes
├── OHLCV / bars
└── U.S. options via OPRA

BITQUERY PRO                  ~$79/month annual-equivalent
│
├── blockchains
├── DEX
├── swaps
├── pools
├── wallets
├── transfers
├── holders / contract activity where available
└── live streams

COINGLASS STARTUP             ~$79/month
│
├── funding
├── open interest
├── liquidations
├── long/short positioning
├── futures analytics
└── derivatives analytics

DIRECT EXCHANGE WEBSOCKETS     $0
│
├── Kraken
├── Coinbase
├── Binance.US
├── Crypto.com where appropriate
└── other public venues that are legally/technically viable
```

Approximate recurring total:

```text
~$257/month
```

Provider pricing, quotas, symbol coverage, redistribution rights, and endpoint availability change. Before implementation, Claude must verify the current official documentation and account entitlements for each provider. Do not silently assume the numbers above remain exact forever.

---

# 1. What Each Provider Is For

## 1.1 Alpaca Algo Trader Plus — live stock/options backbone

Use Alpaca for:

```text
U.S. stocks
ETFs
consolidated SIP market data
stock trades
stock quotes / NBBO
bars / OHLCV
real-time option trades/quotes through available OPRA feed
historical backfill available under account entitlements
```

JARVIS already contains Alpaca integration, so extend the existing adapter rather than creating a parallel stock stack.

Live stock flow:

```text
                  ALPACA SIP
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
     TRADES         QUOTES        BARS
       │             │             │
 exchange        bid/ask        1s / 1m
 price           sizes          OHLCV
 size            NBBO           volume
 conditions
 timestamp
       │             │             │
       └─────────────┼─────────────┘
                     ▼
               JARVIS DATABASE
                     │
           ┌─────────┼──────────┐
           ▼         ▼          ▼
          TA       SIGNALS    CPU/NPU
```

Options should contribute:

```text
option trades
option quotes
underlying
expiration
strike
call/put
bid/ask
size
OPRA timestamp
IV-derived features where available/derived
option activity
underlying/options relationships
```

Do not buy another paid stock feed initially unless Alpaca proves inadequate for a measured use case.

---

## 1.2 Direct exchange WebSockets — raw CEX crypto backbone

Do not pay for a normalized CEX feed until the direct-feed burden or coverage gap proves it is worth the cost.

JARVIS already has live exchange work including:

```text
Kraken
Coinbase L2
Binance.US L2
```

Extend this into standardized adapters for:

```text
Kraken
Coinbase
Binance.US
Crypto.com where appropriate
other public venues where legally/technically allowed
```

Collect whenever the venue exposes it:

```text
trades
quotes
ticker
best bid
best ask
spread
L2 snapshots
L2 deltas
book depth
aggressor side or inferred aggressor
trade size
candles
volume
sequence numbers
exchange timestamps
receive timestamps
```

Normalize locally:

```text
RAW EXCHANGES
│
├─ Kraken
├─ Coinbase
├─ Binance.US
└─ others
       │
       ▼
CANONICAL CRYPTO EVENT
       │
       ├── exchange
       ├── instrument
       ├── timestamp_exchange
       ├── timestamp_received
       ├── event_type
       ├── sequence
       ├── price
       ├── quantity
       ├── side
       ├── bid
       ├── ask
       └── payload/version
```

The long-term objective is to make JARVIS its own normalized crypto market-data warehouse.

---

## 1.3 Bitquery Pro — live blockchain/DEX behavioral data

Bitquery exists in this stack because direct CEX feeds cannot provide:

```text
DEX swaps
pool-level activity
wallet activity
wallet-to-wallet transfers
token transfers
pool liquidity changes
DEX buy/sell behavior
new pools
token flow
contract activity
holder changes where available
cross-chain activity
whale-sized on-chain transactions
```

Persist a blockchain event model conceptually like:

```text
BLOCKCHAIN EVENT
│
├── chain
├── block_number
├── block_hash
├── timestamp
├── tx_hash
├── event_index
├── token
├── token_address
├── pair
├── pool
├── dex
├── trader_wallet
├── side
├── quantity_token
├── quantity_quote
├── value_usd
├── pool_liquidity_usd
├── liquidity_delta_usd
├── transfer_from
├── transfer_to
└── event_type
```

The longer this feed runs, the more valuable JARVIS's local history becomes.

---

## 1.4 CoinGlass Startup — crypto derivatives state

CoinGlass should not be treated as the main price feed.

Its job is to give JARVIS information that ordinary candles do not contain:

```text
funding
open interest
OI changes
liquidations
long/short positioning
exchange-specific derivative metrics
futures statistics
options statistics where included
ETF/other derivatives-related metrics where available
```

Poll/store often enough to build your own time series while respecting provider limits.

Example:

```text
10:01 BTC OI = ...
10:02 BTC OI = ...
10:03 BTC OI = ...

↓ locally derived

oi_change_1m
oi_change_5m
oi_change_15m
oi_velocity
oi_acceleration
```

Do the same for:

```text
funding change
funding z-score
liquidation imbalance
liquidation acceleration
long/short ratio change
cross-exchange OI dispersion
cross-exchange funding dispersion
```

---

# 2. Why This Stack Is Designed for Training

The objective is not:

```text
live tick
↓
neural network changes its weights immediately
```

The objective is:

```text
LIVE DATA
   ↓
RAW DATABASE
   ↓
FEATURE SNAPSHOT
   ↓
WAIT FOR FUTURE OUTCOME
   ↓
LABEL SNAPSHOT
   ↓
TRAINING CORPUS
   ↓
RTX 5090
   ↓
CHALLENGER MODEL
   ↓
CHRONOLOGICAL / WALK-FORWARD VALIDATION
   ↓
SHADOW
   ↓
PROMOTION
   ↓
CPU OR NPU INFERENCE
```

This gives JARVIS continual learning without allowing live noise to corrupt production models.

---

# 3. Permanent Authority Boundaries

Existing deterministic systems remain the source of truth for known arithmetic and hard risk.

ML/LLM systems must never become authoritative for:

```text
fees
commissions
funding arithmetic
spread arithmetic
P&L
R calculations
liquidation price
position size
position quantity
leverage
hard risk limits
kill switches
portfolio heat
technical indicator arithmetic
strategy identity
```

Existing modules such as:

```text
expectancy.py
transaction_costs.py
calibration.py
strategy_lifecycle.py
regime_axes.py
strategies.py
risk manager
execution engine
llm_router.py
```

must remain authoritative.

Predictive models may estimate uncertain future quantities such as:

```text
conditional return distribution
MFE
MAE
stop-first probability
target-first probability
time-to-MFE
time-to-failure
conditional setup quality
slippage
fill probability
regime transition
meaningful timeframe conflict
drift
```

Absolute rule:

```text
hard deterministic NO_TRADE
cannot be changed to
TRADE
by ML or LLM
```

---

# 4. Data-Lake Architecture

Do not make SQLite the long-term market-event warehouse.

Recommended split:

```text
PostgreSQL
→ application state
→ user/configuration
→ orders
→ positions
→ model registry
→ durable business/trading records

ClickHouse
→ high-volume raw market events
→ quotes
→ trades
→ books
→ derivatives observations
→ on-chain events
→ feature snapshots
→ candidate signals
→ resolved labels
→ prediction logs

Parquet
→ immutable ML dataset snapshots
→ reproducible experiments
→ RTX 5090 training
```

High-level flow:

```text
                    INGESTION
                       │
        WebSockets / APIs / blockchain
                       │
                       ▼
                  EVENT BUFFER
                       │
                       ▼
                  CLICKHOUSE
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
 raw events       feature states     outcomes
       │               │                │
       └───────────────┼────────────────┘
                       ▼
                  PARQUET DATASETS
                       │
                       ▼
                    RTX 5090
```

---

# 5. Ingestion Service Boundaries

Use one adapter per provider/venue and normalize into shared contracts.

Suggested package concept:

```text
lib/datafeeds/
│
├── base.py
├── clock.py
├── symbols.py
├── buffer.py
├── health.py
│
├── alpaca_stream.py
├── kraken_stream.py
├── coinbase_stream.py
├── binance_us_stream.py
├── cryptocom_stream.py
├── bitquery_stream.py
└── coinglass_poller.py
```

If equivalent modules already exist, extend them instead of duplicating.

Each adapter owns:

```text
connection
authentication if needed
subscription lifecycle
reconnect
provider-specific parsing
sequence handling
rate-limit handling
provider heartbeat
provider health
```

Each adapter does NOT own:

```text
TA
strategy scoring
model inference
position sizing
risk
```

---

# 6. Canonical Event Types

Use explicit typed schemas.

## 6.1 Trade event

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TradeEvent:
    asset_class: str
    venue: str
    instrument: str

    ts_exchange: datetime
    ts_received: datetime

    price: float
    quantity: float

    side: str | None
    trade_id: str | None

    sequence: int | None
    conditions: tuple[str, ...] = ()
```

---

## 6.2 Quote event

```python
@dataclass(frozen=True)
class QuoteEvent:
    asset_class: str
    venue: str
    instrument: str

    ts_exchange: datetime
    ts_received: datetime

    bid: float
    bid_size: float | None

    ask: float
    ask_size: float | None

    sequence: int | None
```

Derived spread must be calculated centrally:

```python
def spread_bps(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    mid = (bid + ask) / 2.0
    return ((ask - bid) / mid) * 10_000.0
```

---

## 6.3 L2 update

```python
@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class BookDeltaEvent:
    venue: str
    instrument: str

    ts_exchange: datetime
    ts_received: datetime

    sequence: int | None

    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
```

Do not persist Python objects directly; serialize into a stable transport/storage schema.

---

## 6.4 Derivatives observation

```python
@dataclass(frozen=True)
class DerivativesObservation:
    source: str
    venue: str | None
    instrument: str

    ts_observed: datetime

    open_interest: float | None
    funding_rate: float | None

    long_liquidations_usd: float | None
    short_liquidations_usd: float | None

    long_short_ratio: float | None

    source_payload_version: str
```

---

## 6.5 On-chain event

```python
@dataclass(frozen=True)
class OnchainEvent:
    source: str
    chain: str

    block_number: int
    tx_hash: str
    event_index: int | None

    ts_block: datetime
    ts_received: datetime

    event_type: str

    token_address: str | None
    pool_address: str | None
    dex: str | None

    wallet_from: str | None
    wallet_to: str | None
    trader_wallet: str | None

    side: str | None

    token_quantity: float | None
    quote_quantity: float | None
    value_usd: float | None

    pool_liquidity_usd: float | None
```

---

# 7. Symbol Normalization

Never let model code reason directly over vendor-specific symbols.

Create a canonical instrument registry.

Example:

```text
canonical_id: crypto:BTC-USD
asset_class: crypto
base: BTC
quote: USD

venue aliases:
Kraken       XBT/USD
Coinbase     BTC-USD
Binance.US   BTCUSD / BTCUSDT as appropriate
```

For derivatives:

```text
canonical_id
venue
provider_symbol
contract_type
perpetual/future
expiry
contract_multiplier
quote_currency
settlement_currency
```

Keep futures/perpetual contract economics explicit.

---

# 8. Clock Discipline

Every event needs:

```text
provider/exchange timestamp
local receive timestamp
ingest timestamp
```

Never train latency-sensitive models from receive timestamps while pretending they are exchange timestamps.

Track:

```text
clock_skew_ms
network_delay_ms when inferable
provider_timestamp_precision
```

Synchronize the host clock with a reliable time source.

---

# 9. Sequence Gaps and Book Correctness

L2 data is only useful if the local book is correct.

For venues with sequence numbers:

```text
snapshot
↓
apply deltas sequentially
↓
detect gap
↓
mark book INVALID
↓
request/reload snapshot
↓
resume
```

Never continue producing "book imbalance" from a known-corrupted book.

Example state:

```python
@dataclass
class BookHealth:
    valid: bool
    last_sequence: int | None
    last_update_ts: float
    gap_count: int
    resync_count: int
```

Feature generation must abstain from L2-derived features while `valid == False`.

---

# 10. Backpressure and Latest-State-Wins

Do not allow market feeds to block on:

```text
database writes
TA
ML
LLM
```

Use bounded queues.

For state-like updates, use latest-state-wins.

For immutable event streams such as trades, preserve events but spill/batch efficiently.

Conceptual:

```text
WebSocket thread
     │
     ▼
bounded event buffer
     │
     ├── batch writer → ClickHouse
     ├── live feature aggregator
     └── health metrics
```

If storage falls behind:

```text
alert
degrade optional derived processing
never silently pretend dropped data was complete
```

Track drop counts per source/event type.

---

# 11. Raw Storage — Never Throw Away Useful Events

Raw tables should preserve enough information to rebuild later features.

Core logical tables:

```text
raw_stock_trade
raw_stock_quote
raw_option_trade
raw_option_quote

raw_crypto_trade
raw_crypto_quote
raw_crypto_book_delta
raw_crypto_book_snapshot

raw_derivatives_observation

raw_dex_trade
raw_chain_transfer
raw_pool_event
raw_onchain_event
```

Do not store only OHLCV.

OHLCV is derivable from trades.

Raw event history cannot be reconstructed from OHLCV.

---

# 12. ClickHouse Example Tables

Adapt datatypes after observing actual volumes.

## 12.1 Trades

```sql
CREATE TABLE IF NOT EXISTS market_trade
(
    asset_class LowCardinality(String),
    venue LowCardinality(String),
    instrument LowCardinality(String),

    ts_exchange DateTime64(9, 'UTC'),
    ts_received DateTime64(9, 'UTC'),

    trade_id String,

    price Float64,
    quantity Float64,

    side LowCardinality(Nullable(String)),

    sequence Nullable(UInt64),

    ingest_date Date DEFAULT toDate(ts_exchange)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts_exchange)
ORDER BY (instrument, venue, ts_exchange, trade_id);
```

---

## 12.2 Quotes

```sql
CREATE TABLE IF NOT EXISTS market_quote
(
    asset_class LowCardinality(String),
    venue LowCardinality(String),
    instrument LowCardinality(String),

    ts_exchange DateTime64(9, 'UTC'),
    ts_received DateTime64(9, 'UTC'),

    bid Float64,
    bid_size Nullable(Float64),

    ask Float64,
    ask_size Nullable(Float64),

    sequence Nullable(UInt64)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts_exchange)
ORDER BY (instrument, venue, ts_exchange);
```

---

## 12.3 Derivatives observations

```sql
CREATE TABLE IF NOT EXISTS derivatives_state
(
    source LowCardinality(String),
    venue LowCardinality(Nullable(String)),
    instrument LowCardinality(String),

    ts_observed DateTime64(3, 'UTC'),

    open_interest Nullable(Float64),
    funding_rate Nullable(Float64),

    long_liquidations_usd Nullable(Float64),
    short_liquidations_usd Nullable(Float64),

    long_short_ratio Nullable(Float64)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts_observed)
ORDER BY (instrument, source, venue, ts_observed);
```

---

## 12.4 Feature snapshots

```sql
CREATE TABLE IF NOT EXISTS feature_snapshot
(
    snapshot_id UUID,

    instrument LowCardinality(String),
    asset_class LowCardinality(String),
    timeframe LowCardinality(String),

    ts_observed DateTime64(3, 'UTC'),

    schema_version String,

    features_json String,

    missing_fraction Float32,

    source_freshness_json String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts_observed)
ORDER BY (instrument, timeframe, ts_observed);
```

JSON is acceptable initially for flexibility, but high-use features may later be promoted into typed columns/materialized views.

---

# 13. Storage Format Policy

Raw events:

```text
ClickHouse primary
```

Training snapshots:

```text
Parquet
```

Model metadata:

```text
PostgreSQL / current application DB
```

Do not train directly against an ever-changing live SQL query and call it reproducible.

Every training run should reference an immutable dataset snapshot:

```text
dataset_id
schema_version
created_at
time_start
time_end
source filters
feature list
label definitions
git commit
```

---

# 14. Feature Snapshot Engine

Raw data is not the training dataset.

Create deterministic snapshots at meaningful times.

Possible triggers:

```text
every 1 second for active microstructure models
every 5 seconds
every 1 minute
on candle close
on strategy candidate creation
on regime transition
on abnormal volume/liquidation event
on major on-chain event
```

Do not generate every possible feature at every tick if it provides no value.

---

# 15. Example Feature Snapshot

```json
{
  "timestamp": "...",
  "instrument": "crypto:SOL-USD",

  "price": 186.42,

  "ta": {
    "rsi": 63.2,
    "adx": 27.4,
    "atr_pct": 1.21
  },

  "structure": {
    "trend": "up",
    "bos": true
  },

  "microstructure": {
    "spread_bps": 1.8,
    "book_imbalance": 0.42,
    "tape_flow": 0.61
  },

  "derivatives": {
    "oi_change_5m": 2.8,
    "funding": 0.0001,
    "liquidations_5m": 814000
  },

  "onchain": {
    "dex_volume_change": 1.74,
    "pool_liquidity_change": 0.03,
    "whale_net_flow": 1200000
  }
}
```

Later attach labels:

```json
{
  "future_5m_return": 0.0081,
  "future_15m_return": 0.0193,

  "mfe_15m_r": 1.84,
  "mae_15m_r": 0.32,

  "stop_first": false,
  "tp1_first": true
}
```

---

# 16. Derived Crypto Microstructure Features

Examples:

```text
spread_bps
spread_percentile

book_imbalance_5bps
book_imbalance_10bps
book_imbalance_25bps

bid_depth_5bps
ask_depth_5bps

depth_slope_bid
depth_slope_ask

top_of_book_pressure

aggressive_buy_volume_1s
aggressive_sell_volume_1s

aggressive_buy_volume_10s
aggressive_sell_volume_10s

tape_flow_imbalance

large_trade_count
large_trade_volume

price_impact_per_notional

book_replenishment_bid
book_replenishment_ask

book_cancel_rate_bid
book_cancel_rate_ask
```

Do not blindly create hundreds of correlated features without ablation.

---

# 17. Derived Derivatives Features

Examples:

```text
oi_change_1m
oi_change_5m
oi_change_15m
oi_velocity
oi_acceleration

funding_current
funding_change
funding_zscore

long_liquidations_1m
short_liquidations_1m

long_liquidations_5m
short_liquidations_5m

liquidation_imbalance

liquidation_acceleration

long_short_ratio
long_short_ratio_change

price_oi_divergence

cross_exchange_oi_dispersion
cross_exchange_funding_dispersion
```

---

# 18. Derived On-chain / DEX Features

Examples:

```text
dex_volume_1m
dex_volume_5m
dex_volume_acceleration

dex_buy_volume
dex_sell_volume
dex_buy_sell_imbalance

pool_liquidity_usd
pool_liquidity_change_5m
pool_liquidity_change_1h

new_pool_count

new_wallet_activity

active_wallet_change

holder_change

whale_buy_usd
whale_sell_usd
whale_net_flow

exchange_wallet_inflow
exchange_wallet_outflow
exchange_net_flow

cex_dex_price_spread
cex_dex_volume_ratio

cross_chain_volume_shift
```

Wallet classification must carry confidence/provenance.

Do not declare an address "smart money" without a measurable definition.

---

# 19. Stock Features

Use Alpaca raw trade/quote/bars plus existing JARVIS intelligence.

Potential:

```text
returns
realized volatility
ATR
VWAP distance
relative volume
opening-range position
gap
pre-market volume
NBBO spread
quote size imbalance
trade-flow imbalance
relative strength vs SPY/QQQ/sector
earnings proximity
news/catalyst context
insider/institutional context from existing systems
```

Options-derived features may include:

```text
put/call activity
IV level
IV change
skew
term structure
call/put quote pressure
large option trade activity
underlying-option divergence
```

Only derive features supported by the actual subscribed data fields.

---

# 20. Train TA — Do Not Treat Indicator Defaults as Sacred

JARVIS should eventually test whether TA parameters have predictive value by:

```text
asset class
instrument
timeframe
regime
strategy
session
```

Examples to research:

```text
RSI length:
7
9
11
14
17
21

RSI thresholds:
20
25
30
35
40
60
65
70
75
80

MACD:
fast
slow
signal

EMA:
lengths
cross combinations

ATR:
length

Bollinger:
lookback
standard deviation

Donchian:
period

Supertrend:
ATR length
factor

volume:
relative-volume threshold

structure:
swing sensitivity
break tolerance
retest tolerance
```

Do not optimize on the entire history and deploy the winner.

Required:

```text
TRAIN
 ↓
VALIDATE
 ↓
WALK-FORWARD
 ↓
SHADOW
 ↓
PROMOTE
```

---

# 21. TA Experiment Schema

Conceptual:

```python
@dataclass(frozen=True)
class TAExperiment:
    experiment_id: str

    indicator: str
    parameters: dict

    asset_class: str
    instruments: tuple[str, ...]

    timeframe: str
    regime_filter: dict | None

    train_start: str
    train_end: str

    validation_start: str
    validation_end: str

    test_start: str
    test_end: str
```

Result should report:

```text
sample count
coverage
net expected R
win rate
MFE
MAE
drawdown
tail outcomes
calibration if probabilistic
stability by time window
stability by regime
```

Never rank solely by win rate.

---

# 22. Strategy Learning

JARVIS should test strategy conditions rather than only fixed global thresholds.

Example learned finding:

```text
BREAKOUT RETEST
Crypto
15m

Historically best when:

1H trend aligned
ATR percentile 55–80
volume ratio > 1.4
OI rising 1–5%
funding near neutral
book imbalance > 0.25
DEX volume accelerating
BTC relative strength positive

Historical:
n = 842
net EV = +0.37R
MFE p50 = 1.61R
MAE p50 = 0.41R
```

This is an example of the type of output, not a hard-coded trading rule.

Any discovered strategy refinement must move through:

```text
HYPOTHESIS
↓
BACKTEST
↓
WALK-FORWARD
↓
SHADOW
↓
LIFECYCLE
```

---

# 23. Save Every Signal Candidate

This is mandatory.

Do not save only executed trades.

Save:

```text
candidate A → traded
candidate B → rejected
candidate C → rejected
candidate D → traded
```

Resolve hypothetical outcomes for all candidates.

Otherwise JARVIS cannot learn whether its filters are discarding good opportunities.

---

# 24. Candidate Signal Table

Conceptual fields:

```text
candidate_id
created_at

instrument
asset_class
timeframe

strategy_id
strategy_version

direction

entry_reference
stop_reference
target_reference

feature_snapshot_id

base_score
base_expectancy
base_verdict

rejection_reason
executed boolean

model_predictions_at_creation
llm_context_version

eventual_outcome_status
```

Never overwrite the original candidate state after the fact.

---

# 25. Counterfactual Outcome Resolution

For rejected candidates, resolve:

```text
what happened after the candidate?
```

Possible labels:

```text
future return 1m
future return 5m
future return 15m
future return 1h

MFE
MAE

MFE in R
MAE in R

stop touched
TP1 touched
TP2 touched

stop-first
target-first
ambiguous

time-to-MFE
time-to-MAE
time-to-stop
time-to-target
```

This creates a future meta-label model:

```text
TA/strategy candidate
       ↓
ML asks
TAKE / SKIP / REDUCE / ABSTAIN
```

Selection bias must be explicitly monitored.

---

# 26. Path Labeling

Use future data only for labels.

Never leak it into features.

Example:

```python
def path_labels(
    *,
    entry: float,
    stop: float,
    direction: str,
    future_bars,
):
    risk = abs(entry - stop)

    if risk <= 0:
        return None

    is_short = direction.lower().startswith("short")

    max_favorable = 0.0
    max_adverse = 0.0

    mfe_bar = None
    mae_bar = None

    for i, (_, bar) in enumerate(
        future_bars.iterrows(),
        start=1,
    ):
        hi = float(bar["high"])
        lo = float(bar["low"])

        if is_short:
            favorable = entry - lo
            adverse = hi - entry
        else:
            favorable = hi - entry
            adverse = entry - lo

        if favorable > max_favorable:
            max_favorable = favorable
            mfe_bar = i

        if adverse > max_adverse:
            max_adverse = adverse
            mae_bar = i

    return {
        "mfe_r": max_favorable / risk,
        "mae_r": max_adverse / risk,
        "mfe_bar": mfe_bar,
        "mae_bar": mae_bar,
    }
```

If stop and target are both touched within the same OHLC bar, mark:

```text
AMBIGUOUS
```

unless a finer-grained event stream proves ordering.

---

# 27. Live Execution Labels

For executed trades, also store:

```text
signal creation timestamp
order submission timestamp
reference mid at submit
bid/ask at submit
L2 state at submit
requested price
requested quantity
fill price
fill quantity
fill timestamp
partial fills
cancel timestamp
realized slippage
fees
funding
borrow if relevant
```

These become future execution-model labels.

---

# 28. Execution Sample Example

```python
@dataclass
class ExecutionSample:
    submitted_at: datetime

    instrument: str
    venue: str
    side: str
    order_type: str

    reference_mid: float
    requested_price: float | None
    notional_usd: float

    spread_bps: float | None
    bid_depth: float | None
    ask_depth: float | None
    book_imbalance: float | None
    tape_flow: float | None
    realized_volatility: float | None

    filled_at: datetime | None
    avg_fill_price: float | None

    requested_qty: float | None
    filled_qty: float | None

    realized_slippage_bps: float | None
    fill_delay_ms: float | None
```

Do not train an execution model until there is enough diverse live data.

---

# 29. Continuous Dataset Builder

Never let training scripts invent joins ad hoc.

Create explicit dataset builders.

Suggested:

```text
ml/datasets/
├── build_path_dataset.py
├── build_outcome_dataset.py
├── build_meta_label_dataset.py
├── build_ta_research_dataset.py
├── build_strategy_dataset.py
├── build_execution_dataset.py
└── build_state_encoder_dataset.py
```

Each builder must:

```text
declare feature schema
declare label definition
declare source window
declare provenance
declare missing-data policy
declare normalization policy
write immutable Parquet
write manifest
```

---

# 30. Dataset Manifest

Example:

```json
{
  "dataset_id": "path_crypto_15m_2026_08_13_v1",
  "created_at": "...",

  "feature_schema": "predictive_features_v3",
  "label_schema": "path_labels_v2",

  "time_start": "...",
  "time_end": "...",

  "asset_class": "crypto",
  "timeframe": "15m",

  "rows": 1234567,

  "sources": [
    "kraken",
    "coinbase",
    "binance_us",
    "coinglass",
    "bitquery"
  ],

  "git_commit": "...",

  "leakage_checks": "passed"
}
```

---

# 31. Training Architecture

Use the RTX 5090 for training.

```text
ClickHouse
   ↓
dataset builder
   ↓
Parquet
   ↓
RTX 5090
   ↓
candidate/challenger model
   ↓
chronological validation
   ↓
shadow predictions
   ↓
promotion decision
   ↓
CPU or Intel NPU inference
```

The Intel NPU is not the training target.

---

# 32. CPU / NPU / RTX Responsibilities

```text
CPU
│
├── ingestion
├── normalization
├── database
├── TA
├── structure
├── deterministic strategies
├── expectancy
├── transaction costs
├── risk
├── execution
├── labeling
└── ML inference when CPU benchmarks better

Intel NPU
│
└── compact predictive inference when it improves
    resource isolation or full-system performance

RTX 5090
│
├── local LLM
├── model training
├── challenger training
└── heavier research
```

Do not force a model onto the NPU merely because it is neural.

Benchmark CPU vs NPU.

---

# 33. Models the Proprietary Dataset Should Eventually Support

## Path model

Predict:

```text
MFE distribution
MAE distribution
stop-first probability
target-first probability
time-to-MFE
time-to-failure
```

## Conditional outcome model

Predict:

```text
future return distribution
conditional directional probabilities
uncertainty
```

## Meta-label model

Predict:

```text
TAKE
SKIP
REDUCE
ABSTAIN
```

given a deterministic candidate.

## State encoder

Learn:

```text
compact latent representation of current market state
```

for:

```text
historical analog retrieval
conditional models
clustering
novelty/drift detection
```

## Residual learner

Learn:

```text
where baseline JARVIS systematically overestimates
or underestimates setup quality
```

## Future execution model

After sufficient live fills:

```text
slippage distribution
fill probability
fill delay
adverse selection
spread expansion
```

---

# 34. Historical Analog Engine

A useful joined dataset enables:

```text
current feature state
       ↓
state encoder
       ↓
embedding
       ↓
nearest historical states
       ↓
actual historical outcomes
```

Return interpretable information:

```text
neighbor count
similarity distribution
same strategy count
same regime count
median forward return
median MFE
median MAE
positive net-R rate
```

Never allow analog search in historical evaluation to see future observations.

---

# 35. Training Split Rules

Time-series data must use chronological validation.

Never use random train/test split as the final proof.

Minimum approach:

```text
oldest 60% → train
next 20%   → validation
newest 20% → test
```

Preferred:

```text
rolling walk-forward
```

Normalizers must fit on training data only.

---

# 36. Continuous Retraining

Do not retrain after every tick.

Trigger challenger training based on:

```text
minimum number of new labeled observations
time cadence
material feature drift
material residual drift
model degradation
strategy lifecycle change
```

Production model remains frozen while challenger trains.

---

# 37. Champion / Challenger

Statuses:

```text
CHAMPION
CHALLENGER
SHADOW
DEGRADED
DISABLED
```

Promotion must require improvement across relevant metrics, not just accuracy.

Potential:

```text
Brier score
calibration error
MFE/MAE error
incremental net expected R
drawdown
tail losses
trade rejection quality
live/replay consistency
```

---

# 38. Model Registry

Conceptual:

```python
@dataclass(frozen=True)
class ModelSpec:
    name: str
    version: str

    status: str

    feature_schema: str
    label_schema: str

    model_path: str

    preferred_device: str
    fallback_device: str

    trained_through: str

    train_rows: int
    validation_rows: int
    test_rows: int

    metrics: dict
```

Never overwrite champion artifacts in place.

---

# 39. Predictor Persistence

Every prediction must be stored so it can be resolved later.

```text
prediction_id
candidate_id
instrument
model_name
model_version
feature_schema
generated_at
feature_timestamp
device
latency_ms
prediction
uncertainty
trust
```

Later attach:

```text
resolved_at
actual outcome
residual
```

This is how JARVIS learns whether a model truly adds value.

---

# 40. TA Research Must Be Separate from Live Trading

Research process:

```text
candidate TA parameter set
↓
historical evaluation
↓
walk-forward
↓
shadow
↓
strategy lifecycle
↓
optional promotion
```

Never let a nightly optimizer directly rewrite live strategy settings.

---

# 41. Strategy Discovery Must Be Hypothesis-Driven

Allow ML/statistics to discover:

```text
interesting feature combinations
conditional performance pockets
regime dependence
timeframe dependence
asset dependence
```

But discovered associations become:

```text
HYPOTHESIS
```

not live rules.

They must pass:

```text
sample sufficiency
out-of-sample validation
cost-aware expectancy
shadow validation
```

---

# 42. Pattern Memory Safety

Tiny sample sizes cannot be described as reliable performance.

Use language tiers:

```text
n < 10
OBSERVED ONLY

10 <= n < 25
EARLY EVIDENCE

n >= 25
MEASURED CONTEXT
```

Even then, `calibration.py` and `expectancy.py` remain statistical authority.

Do not inject "3/3 = 100% win rate" into the LLM as if it were strong evidence.

---

# 43. Data Quality Flags

Every snapshot should carry quality metadata.

Examples:

```text
book_valid
book_age_ms
trade_feed_age_ms
quote_feed_age_ms
derivatives_age_s
onchain_age_s
missing_fraction
provider_degraded
sequence_gap_recent
clock_skew_ms
```

Models must be able to abstain when required inputs are stale or incomplete.

---

# 44. Provider Health

Track per source:

```text
connected
last_event_at
events_per_second
reconnect_count
error_count
rate_limit_count
dropped_event_count
sequence_gap_count
write_lag_ms
```

Expose health to the UI/API.

---

# 45. Retention Strategy

Do not delete high-value raw data casually.

Possible tiering:

```text
hot ClickHouse
→ recent raw events

compressed ClickHouse
→ older raw events

Parquet/archive
→ immutable long-term snapshots
```

Book deltas can become enormous.

If storage becomes a constraint, compress/archive before dropping.

---

# 46. Storage Planning

Record actual observed bytes/day by source before over-engineering.

Track:

```text
rows/day
bytes/day
compressed bytes/day
writes/sec
read latency
partition sizes
```

Use this to decide retention and whether every symbol needs full L2.

---

# 47. Watchlist Tiers

Do not collect the same depth for every instrument.

Example:

```text
TIER 1 — actively traded / high-priority
full trades
quotes
L2
derivatives
on-chain
1s features

TIER 2 — monitored
trades
quotes
bars
derivatives
5s/1m features

TIER 3 — broad universe
bars
volume
basic market/chain state
candidate scanning
```

This dramatically controls storage while preserving deep learning data where it matters.

---

# 48. Data Provenance

Every stored observation should answer:

```text
where did this come from?
when was it observed?
what parser/schema version produced it?
```

Store:

```text
source
source_version/schema
ingest_version
feature_schema
```

Do not silently mix metrics from different provider definitions.

---

# 49. Provider-Specific Semantics

"Open interest", "liquidation", "volume", or "long/short ratio" may have different definitions across providers/venues.

Do not normalize values merely because names look similar.

Document:

```text
provider definition
units
aggregation window
venue aggregation
update cadence
```

---

# 50. Avoid Double Counting

Example:

CoinGlass may aggregate multiple exchanges while direct exchange feeds also provide venue-specific metrics.

Do not create:

```text
aggregate OI
+
all component venue OI
```

as independent additive evidence without accounting for overlap.

Mark:

```text
aggregate
venue-specific
source
coverage
```

---

# 51. Cross-Market Features

Once feeds are stable, derive:

```text
BTC vs ETH relative strength
SOL vs BTC relative strength

crypto spot vs perp
spot vs futures basis

CEX vs DEX price spread
CEX vs DEX volume ratio

stock vs index relative strength
stock vs sector relative strength

underlying vs options activity

BTC vs crypto equities
crypto market vs broad risk assets
```

These can become high-value predictive inputs.

---

# 52. Event-Driven Snapshots

Not all snapshots should be clock-driven.

Create event-triggered examples around:

```text
volume shock
liquidation burst
OI shock
funding extreme
book imbalance extreme
large whale transfer
DEX liquidity withdrawal
new high/low
breakout
breakdown
regime transition
news/catalyst arrival
strategy candidate creation
```

Store the pre-event state and future labels.

---

# 53. Negative Examples Matter

Training datasets must include:

```text
failed breakouts
fake momentum
bad volume spikes
liquidation events that did not continue
whale flows that did not matter
DEX surges that did not propagate to CEX
high-confidence signals that failed
rejected signals that would have won
```

Otherwise the model learns only success stories.

---

# 54. Prevent Survivorship Bias

For crypto universes, retain delisted/dead assets where data permits.

For stocks, account for corporate actions and historical ticker membership where relevant.

Do not train only on assets that survived to the current date.

---

# 55. Cost-Aware Labels

A prediction can be directionally correct and economically useless.

Where possible evaluate labels:

```text
gross move
estimated spread
fees
funding
slippage
net R
```

Existing deterministic `transaction_costs.py` remains authority.

---

# 56. Live Data → Feature Store Example

```python
async def on_trade(event: TradeEvent):
    await raw_writer.enqueue(event)

    state = market_state[event.instrument]
    state.apply_trade(event)

    if feature_scheduler.should_snapshot(
        instrument=event.instrument,
        ts=event.ts_exchange,
    ):
        snapshot = feature_builder.build(
            instrument=event.instrument,
            state=state,
        )

        await feature_writer.enqueue(snapshot)

        label_scheduler.register(snapshot)
```

Do not run model training inside this callback.

---

# 57. Label Scheduler Concept

```python
@dataclass(frozen=True)
class PendingLabel:
    snapshot_id: str
    instrument: str
    observed_at: datetime

    horizons: tuple[str, ...]


class LabelScheduler:
    def register(self, snapshot):
        ...

    async def resolve_due(self, now):
        ...
```

A snapshot may need labels at:

```text
1m
5m
15m
1h
4h
```

Resolve each horizon independently.

---

# 58. Feature Versioning

Never silently change the meaning of a feature.

Bad:

```text
book_imbalance
```

changes formula next week.

Good:

```text
book_imbalance_10bps_v1
book_imbalance_10bps_v2
```

or maintain a schema version that pins the formula.

Models must record the exact feature schema they were trained on.

---

# 59. Experiment Reproducibility

Every model experiment must log:

```text
git commit
dataset ID
feature schema
label schema
hyperparameters
random seed
training period
validation period
test period
hardware
metrics
artifact hash
```

---

# 60. Suggested Repository Structure

Adapt to current repo conventions.

```text
lib/
├── datafeeds/
│   ├── base.py
│   ├── symbols.py
│   ├── buffer.py
│   ├── health.py
│   ├── alpaca_stream.py
│   ├── kraken_stream.py
│   ├── coinbase_stream.py
│   ├── binance_us_stream.py
│   ├── bitquery_stream.py
│   └── coinglass_poller.py
│
├── datastore/
│   ├── clickhouse.py
│   ├── postgres.py
│   ├── parquet.py
│   └── retention.py
│
├── features/
│   ├── builder.py
│   ├── schema.py
│   ├── microstructure.py
│   ├── derivatives.py
│   ├── onchain.py
│   └── cross_asset.py
│
└── predictive/
    ├── runtime.py
    ├── path_model.py
    ├── outcome_model.py
    ├── state_encoder.py
    ├── analogs.py
    ├── drift.py
    ├── residual_model.py
    └── meta_filter.py

ml/
├── datasets/
├── training/
├── evaluation/
├── experiments/
└── export/

jobs/
├── resolve_labels.py
├── export_training_dataset.py
├── train_challengers.py
└── evaluate_challengers.py
```

Do not create duplicates of modules already present.

---

# 61. Initial Implementation Sequence

## Phase A — Provider/account setup

```text
Alpaca Plus entitlement
Bitquery Pro
CoinGlass Startup
direct exchange adapters
```

Verify current official documentation and credentials.

## Phase B — Storage foundation

```text
ClickHouse
PostgreSQL integration
Parquet export
```

## Phase C — Canonical events

```text
trade
quote
book
derivatives
on-chain
```

## Phase D — Ingestion

```text
Alpaca
Kraken
Coinbase
Binance.US
Bitquery
CoinGlass
```

## Phase E — Health/backpressure

```text
reconnect
sequence gaps
lag
rate limits
queue pressure
drop metrics
```

## Phase F — Feature snapshots

```text
TA
structure
microstructure
derivatives
on-chain
cross-asset
```

## Phase G — Candidate persistence

Save all signals, including rejected candidates.

## Phase H — Label resolution

```text
returns
MFE
MAE
first-touch
duration
```

## Phase I — Dataset builders

Immutable Parquet + manifests.

## Phase J — TA and strategy research

Walk-forward only.

## Phase K — Predictive models

```text
path model first
outcome model
state encoder/analogs
residual learner
drift/trust
meta-label
```

## Phase L — Execution model

Only after enough live fills.

---

# 62. Minimum Test Suite

## Feed tests

```text
connect
authenticate
subscribe
reconnect
heartbeat
sequence gap
snapshot resync
rate-limit response
duplicate event
out-of-order event
clock precision
```

## Storage tests

```text
batch write
duplicate idempotency
partition rollover
schema compatibility
ClickHouse outage
recovery
```

## Feature tests

```text
known book imbalance
known spread
known tape flow
known OI change
known funding change
known DEX volume change
missing source
stale source
```

## Label tests

```text
long MFE
short MFE
long MAE
short MAE
stop-first
target-first
ambiguous same bar
no touch
```

## Leakage tests

```text
future feature blocked
future analog blocked
normalizer test leakage blocked
post-fill data excluded from submit-time features
```

## Safety tests

```text
ML cannot set leverage
ML cannot set quantity
ML cannot override hard risk
ML cannot resurrect NO_TRADE
```

---

# 63. Observability UI

Add a compact "Data & Learning" system page.

Show:

```text
PROVIDERS
Alpaca       connected
Kraken       connected
Coinbase     connected
Binance.US   connected
Bitquery     connected
CoinGlass    connected

EVENT RATES
stock trades/sec
stock quotes/sec
crypto trades/sec
book deltas/sec
on-chain events/sec

STORAGE
rows today
GB today
write lag
ClickHouse health

FEATURES
snapshots today
missing-data rate
stale-data rate

LABELS
pending
resolved
failed

TRAINING
latest dataset
latest challenger
champion version
shadow observations
```

---

# 64. What Not to Do

Do NOT:

```text
store only OHLCV
train only from executed trades
discard rejected candidates
random split time series
allow future leakage
allow corrupted L2 books to generate features
silently drop data
let a model train on unversioned features
let live noise modify production weights immediately
let LLM output become numeric authority
let ML override hard risk
run every model on every tick
put every symbol on full L2 without measuring storage
buy duplicate live price feeds without a measured need
```

---

# 65. What Makes the Dataset Proprietary

After sufficient runtime, JARVIS should possess joined records no vendor sells directly:

```text
market state
+
TA state
+
strategy state
+
cross-timeframe state
+
L2/tape state
+
derivatives state
+
on-chain/DEX state
+
candidate decision
+
rejection reason
+
model predictions
+
LLM reasoning metadata
+
actual execution
+
actual fees/slippage
+
MFE/MAE
+
eventual outcome
```

That joined dataset is the core training asset.

---

# 66. One-Year Target

Conceptually:

```text
          JARVIS PROPRIETARY DATASET

12 months of:
billions of market events
+
TA states
+
order flow
+
derivatives
+
on-chain activity
+
strategy candidates
+
rejected signals
+
executed signals
+
MFE
+
MAE
+
actual fills
+
actual costs
+
model predictions
+
eventual outcomes
```

The exact row count depends on subscription universe, L2 depth, watchlist tiers, and retention policy.

---

# 67. Final Architectural Principle

The purpose of the paid feeds is not to make JARVIS permanently dependent on external APIs for every historical question.

Use them to create:

```text
LIVE INFORMATION
       ↓
LOCAL RAW HISTORY
       ↓
DETERMINISTIC FEATURE HISTORY
       ↓
RESOLVED OUTCOMES
       ↓
PROPRIETARY TRAINING CORPUS
       ↓
BETTER TA / STRATEGIES / SIGNALS / ML
```

The best long-term asset is not the API subscription.

It is the **timestamp-aligned, provenance-preserving, continuously labeled JARVIS dataset built from those feeds**.

---

# 68. Claude Code Execution Rules

When Claude implements this guide:

1. Inspect the current repository first.
2. Reuse existing Alpaca, Kraken, Coinbase, order-book, TA, strategy, regime, expectancy, cost, risk, and learning modules.
3. Do not create parallel duplicate systems.
4. Verify current official provider APIs before coding provider-specific calls.
5. Keep provider-specific parsing inside adapters.
6. Normalize into canonical events.
7. Store raw events before relying on derived features.
8. Preserve timestamps and provenance.
9. Implement bounded queues/backpressure.
10. Detect sequence gaps and invalidate corrupt books.
11. Save every signal candidate, not only trades.
12. Resolve counterfactual outcomes for rejected candidates.
13. Build immutable training datasets.
14. Use chronological/walk-forward validation.
15. Train challengers on RTX 5090.
16. Shadow before promotion.
17. Benchmark CPU vs NPU for each compact production model.
18. Keep deterministic numeric/risk authority intact.
19. Do not train an execution model until live fill data is sufficient.
20. Report measured improvements against baseline JARVIS.


---

# APPENDIX A — SOURCE NOTES FROM THE CHOSEN LIVE-LEARNING OPTION

The following source notes are retained so implementation details from the original chosen option are not accidentally lost while using the structured guide above.

# 1. Alpaca Algo Trader Plus is almost perfect for the stock side

Since JARVIS already supports Alpaca, this is the obvious first upgrade.

For **$99/month**, Algo Trader Plus currently gives you:

-  all U.S. stocks and ETFs 
- **all U.S. exchanges via SIP** 
-  real-time WebSocket data 
-  unlimited stock WebSocket symbol subscriptions 
-  historical data since 2016 
-  up to 10,000 historical API calls/min 
-  real-time U.S. options via **OPRA** 
-  1,000 options quote subscriptions 
-  crypto data too.  

That's a monster amount of useful data for $99.

The stock feed comes from the consolidated CTA/UTP feeds, meaning you're getting essentially 100% of reported U.S. exchange volume rather than IEX alone. 

Your live stock data pipeline becomes:

```
```

```
                  ALPACA SIP
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
     TRADES         QUOTES        BARS
       │             │             │
 exchange        bid/ask        1s / 1m
 price           sizes          OHLCV
 size            NBBO           volume
 conditions
 timestamp
       │             │             │
       └─────────────┼─────────────┘
                     ▼
               JARVIS DATABASE
                     │
           ┌─────────┼──────────┐
           ▼         ▼          ▼
          TA       SIGNALS    CPU/NPU
```

Alpaca's WebSocket streams are specifically intended for continuously updated trading-strategy data. 

And the options feed adds:

```
```

```
calls/puts
quotes
trades
OPRA
IV-derived features
option activity
underlying/options relationships
```

So I wouldn't buy another stock provider initially.

---

# 2. Use FREE direct exchange WebSockets for CEX crypto

This is the big change I'd make.

**Don't pay CoinAPI $79 right now.**

You're building your own data warehouse, and direct exchange streams are better raw material.

JARVIS already has:

```
```

```
Kraken WS
Coinbase L2
Binance.US L2
```

and I'd expand that idea.

Build standardized adapters for:

```
```

```
Kraken
Coinbase
Binance.US
Crypto.com

plus other publicly accessible venues
where their terms/location permit
```

For every exchange collect:

```
```

```
trades
quotes
ticker

L2 snapshots
L2 deltas

best bid
best ask

spread
depth

aggressor side
trade size

candles
volume
```

Then normalize locally:

```
```

```
RAW EXCHANGES
│
├─ Kraken
├─ Coinbase
├─ Binance.US
└─ others
       │
       ▼
CANONICAL CRYPTO EVENT
       │
       ├── exchange
       ├── symbol
       ├── timestamp
       ├── event_type
       ├── price
       ├── quantity
       ├── side
       ├── bid
       ├── ask
       ├── depth
       └── sequence
```

That becomes **your own CoinAPI**.

And unlike paying CoinAPI every month, you keep accumulating the database.

CoinAPI Startup is $79, but its current pricing documentation makes higher-volume streaming progressively expensive; a 100 GB/day example would make Startup more than $1,000/month after overages. 

For your architecture, direct feeds make more sense.

---

# 3. Bitquery **Pro**, not Personal

This is where my previous cheap recommendation was wrong for your clarified goal.

Personal does **not include streams**.

Bitquery Pro does.

Pro currently includes:

```
```

```
1M API points/month

90 requests/minute

100 concurrent streams

100,000 stream-minutes

5 GB streaming allocation

live DEX + price streams

9 core chains
```

and is specifically positioned for trading bots and live dashboards. 

Bitquery's WebSocket API can push blockchain/DEX events directly into JARVIS as they happen. 

That's what you want.

Now you're continuously building datasets like:

```
```

```
BLOCKCHAIN EVENT DB
│
├── chain
├── block
├── timestamp
│
├── token
├── pair
├── pool
├── DEX
│
├── trader wallet
├── buy/sell
├── quantity
├── USD value
│
├── pool liquidity
├── liquidity change
│
├── wallet transfer
├── holder activity
└── contract activity
```

And the longer JARVIS runs, the more valuable that becomes.

---

# 4. CoinGlass provides the missing derivatives dimension

Direct exchange data gives you market microstructure.

Bitquery gives you blockchain behavior.

CoinGlass gives you **leveraged positioning**.

Hobbyist is $29/month and currently exposes 80+ endpoints with updates up to roughly one minute. 

Feed your database:

```
```

```
funding

open interest
OI change

liquidations

long/short positioning

exchange-specific derivatives

futures statistics

options statistics
```

Then create your own higher-resolution time series by polling continuously.

For example:

```
```

```
2026-08-13 10:01
BTC OI = ...

10:02
BTC OI = ...

10:03
BTC OI = ...

             ↓

JARVIS DERIVED

oi_change_1m
oi_change_5m
oi_change_15m
oi_velocity
oi_acceleration
```

This is exactly what the learning system needs.

---

# I'd consider CoinGlass Startup instead

If you want **more crypto information immediately**, I'd probably spend the extra $50.

Current:

```
```

```
Hobbyist
$29
80+ endpoints
30 req/min
```

versus:

```
```

```
Startup
$79
130+ endpoints
80 req/min
```

Both update up to approximately once per minute. 

Then your total becomes:

```
```

```
Alpaca Plus       $99
Bitquery Pro      $79
CoinGlass Startup $79
                 ────
                 $257/month
```

**This is probably the package I'd choose.**

---

# What the system should store

This part matters more than the API choices.

Don't just save OHLCV.

Build several layers.

```
```

```
                    JARVIS DATA LAKE
                           │
      ┌────────────────────┼─────────────────────┐
      ▼                    ▼                     ▼
    RAW EVENTS         FEATURE STATE          OUTCOMES
      │                    │                     │
 trades                 RSI                  direction
 quotes                 MACD                 MFE
 books                  ATR                  MAE
 blockchain             structure            P&L
 OI                     regime               stop-first
 funding                flows                TP-first
 liquidations           correlations         duration
 options                derivatives          slippage
      │                    │                     │
      └────────────────────┼─────────────────────┘
                           ▼
                    TRAINING DATASETS
```

## RAW layer

Never throw this away.

```
```

```
raw_stock_trade
raw_stock_quote
raw_option_quote

raw_crypto_trade
raw_crypto_quote
raw_crypto_book

raw_dex_trade
raw_chain_transfer

raw_open_interest
raw_funding
raw_liquidation
```

---

# Then generate feature snapshots

Every relevant interval:

```
```

```
{
  "timestamp": "...",
  "symbol": "SOL/USD",

  "price": 186.42,

  "ta": {
    "rsi": 63.2,
    "adx": 27.4,
    "atr_pct": 1.21
  },

  "structure": {
    "trend": "up",
    "bos": true
  },

  "microstructure": {
    "spread_bps": 1.8,
    "book_imbalance": 0.42,
    "tape_flow": 0.61
  },

  "derivatives": {
    "oi_change_5m": 2.8,
    "funding": 0.0001,
    "liquidations_5m": 814000
  },

  "onchain": {
    "dex_volume_change": 1.74,
    "pool_liquidity_change": 0.03,
    "whale_net_flow": 1200000
  }
}
```

Then **later** attach:

```
```

```
{
  "future_5m_return": 0.0081,
  "future_15m_return": 0.0193,

  "mfe_15m_r": 1.84,
  "mae_15m_r": 0.32
}
```

Now you have real supervised training data.

---

# This is how JARVIS should continuously learn

Not:

```
```

```
new tick
↓
change neural weights immediately
```

Instead:

```
```

```
LIVE DATA
   ↓
DATABASE
   ↓
FEATURE SNAPSHOTS
   ↓
WAIT FOR FUTURE OUTCOME
   ↓
LABEL SNAPSHOT
   ↓
TRAINING CORPUS
   ↓
RTX 5090
   ↓
CHALLENGER MODEL
   ↓
WALK-FORWARD
   ↓
SHADOW
   ↓
PROMOTE
   ↓
CPU / NPU
```

That gives you continual learning **without corrupting the live model**.

---

# And train TA itself, not just predictions

This is where your idea gets much more powerful.

JARVIS can eventually answer:

> Does RSI actually matter for this strategy?

Instead of hard-coding:

```
```

```
RSI = 14 periods
oversold = 30
overbought = 70
```

your research/training system can measure:

```
```

```
RSI length:
7
9
11
14
17
21

threshold:
20
25
30
35
40

by:
asset
timeframe
regime
strategy
```

Then test them out-of-sample.

Same with:

```
```

```
MACD parameters

EMA lengths

ATR lengths

Supertrend factors

Donchian periods

Bollinger parameters

volume thresholds

structure sensitivity
```

Do **not** let it optimize directly against the entire history and deploy the best result—that's curve fitting.

Use:

```
```

```
TRAIN
 ↓
VALIDATE
 ↓
WALK-FORWARD
 ↓
SHADOW
```

---

# Strategy learning becomes much better too

You can have the research engine discover things like:

```
```

```
BREAKOUT RETEST

Crypto
15m

Best when:

1H trend aligned
ATR percentile 55–80
volume ratio > 1.4
OI rising 1–5%
funding near neutral
book imbalance > 0.25
DEX volume accelerating
BTC relative strength positive

Historical:
n = 842
net EV = +0.37R
MFE p50 = 1.61R
MAE p50 = 0.41R
```

That's **real learning**.

---

# Signal generation can train from all candidates

This is crucial.

Do not only save trades you take.

Save:

```
```

```
EVERY SIGNAL CANDIDATE
```

including rejected ones.

```
```

```
candidate A → traded
candidate B → rejected
candidate C → rejected
candidate D → traded
```

Then resolve outcomes for **all four**.

Otherwise you create selection bias.

You need to know:

> What would have happened if JARVIS had taken the signal it rejected?

That lets your future meta-label model learn:

```
```

```
TA generated signal
       ↓
ML asks
"TAKE / SKIP / REDUCE?"
```

That's one of the most valuable learning systems you can build.

---

# Database architecture I'd use

For your volume, I would not make SQLite the long-term training warehouse.

Something like:

```
```

```
                    INGESTION
                       │
      WebSockets / APIs / blockchain
                       │
                       ▼
                 EVENT BUFFER
                       │
                       ▼
                 CLICKHOUSE
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
 raw events       feature states     outcomes
       │               │                │
       └───────────────┼────────────────┘
                       ▼
                  PARQUET DATASETS
                       │
                       ▼
                    RTX 5090
```

PostgreSQL can remain good for application state.

ClickHouse is fantastic for billions of time-series/event rows.

Parquet is great for model training.

I would ultimately split:

```
```

```
Postgres
→ application/business/trade state

ClickHouse
→ enormous market/event/time-series data

Parquet
→ immutable ML training datasets
```

---

# My actual recommendation

Start with:

### **$257/month**

```
```

```
ALPACA ALGO TRADER PLUS       $99
│
├── stocks
├── ETFs
├── SIP
├── stock trades
├── quotes
├── OHLCV
└── options OPRA

BITQUERY PRO                  $79
│
├── blockchains
├── DEX
├── swaps
├── pools
├── wallets
└── live streams

COINGLASS STARTUP             $79
│
├── funding
├── OI
├── liquidations
├── positioning
└── derivatives

DIRECT EXCHANGE WS             $0
│
├── Kraken
├── Coinbase
├── Binance.US
└── other viable feeds
```

That is a **much better live-learning foundation** than spending $250 on historical subscriptions.

And after a year, you won't just have API access.

You'll have something far more valuable:

```
```

```
          JARVIS PROPRIETARY DATASET

12 months of:
billions of market events
+
TA states
+
order flow
+
derivatives
+
on-chain activity
+
strategy candidates
+
rejected signals
+
executed signals
+
MFE
+
MAE
+
actual fills
+
actual costs
+
model predictions
+
eventual outcomes
```