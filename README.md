# 🤖 Jarvis Trading AI v6.5

> **Python Edition** — FastAPI + APScheduler + TA-Lib + Alpaca  
> Autonomous trading across equities, crypto, and commodities with geopolitical threat intelligence, multi-factor signal scoring, AI position management, a parallel paper trading engine for shorts and leverage, and a live Real vs Paper performance comparison dashboard.

---

## 📋 Requirements

| Requirement | Version |
|---|---|
| Python | 3.12 |
| LM Studio | Any (local LLM inference) |
| Alpaca Account | Paper or Live |

---

## 🚀 Quick Start

### Windows (recommended)

```bat
:: First time — double-click or run:
start.bat
```

That's it. `start.bat` handles everything:
1. Checks Python 3.12
2. Creates `.venv` if missing
3. Installs all dependencies
4. Downloads TA-Lib pre-built wheel (Python 3.12 / Win x64)
5. Copies `.env.example` → `.env` if no `.env` exists
6. Launches Jarvis + opens browser at `http://localhost:3000`

### Windows (PowerShell)

```powershell
# First time setup (creates .venv + Desktop shortcut):
.\setup.ps1

# Subsequent launches:
.\start.ps1
```

### macOS

```bash
chmod +x install_mac.sh
./install_mac.sh
```

Installs Python 3.12 and TA-Lib via Homebrew, creates `.venv`, launches Jarvis.

### Linux (Ubuntu/Debian/Fedora/Arch)

```bash
chmod +x install_linux.sh
./install_linux.sh
```

Auto-detects `apt` / `dnf` / `pacman`. Builds TA-Lib from source if not in package manager.

---

## 📁 Install Scripts

| File | Platform | Purpose |
|---|---|---|
| `start.bat` | Windows | **Main launcher** — install + start |
| `fresh_install.bat` | Windows | Nuke `.venv` and reinstall from scratch |
| `fresh_start.bat` | Windows | Clean DB + restart fresh |
| `watchdog.bat` | Windows | Auto-restart on crash |
| `stop.bat` | Windows | Force-kill Python process |
| `start.ps1` | Windows (PS) | PowerShell launcher |
| `setup.ps1` | Windows (PS) | Full setup + Desktop shortcut |
| `install_mac.sh` | macOS | Homebrew-based install + launch |
| `install_linux.sh` | Linux | apt/dnf/pacman install + launch |

---

## ⚙️ Configuration

Edit `.env` (auto-created from `.env.example` on first run):

```env
# Alpaca — get keys from https://alpaca.markets
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_MODE=paper          # paper or live

# LM Studio (local LLM — run LM Studio first)
LM_STUDIO_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=local-model

# Telegram (optional — for mobile alerts)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# News and threat intelligence
NEWS_MAX_ARTICLES=120
NEWS_ITEMS_PER_SOURCE=8
NEWS_DEDUP_DAYS=7
NEWS_MAX_AGE_HOURS=72
NEWS_AGGREGATOR_MODE=fallback
X_BEARER_TOKEN=
X_USERNAMES=realDonaldTrump,POTUS,WhiteHouse
TRUTH_API_URL=
TRUTH_API_TOKEN=
TRUTH_API_ACCOUNTS=realDonaldTrump,WhiteHouse
OHLCV_WARM_TIMEFRAMES=15m,30m,1H,2H,4H,1D
MIN_SIGNAL_DATA_QUALITY=35
MIN_SIGNAL_FRESHNESS=20

# Crypto market data (optional; public providers work keyless where allowed)
CRYPTO_DISCOVERY_LIMIT=150
CRYPTO_CACHE_LIMIT=60
CRYPTO_SCAN_LIMIT=150
CRYPTO_API_TIMEOUT=8
CRYPTO_TICKER_CACHE_SECONDS=30
ALLOW_YFINANCE_CRYPTO_FALLBACK=false
ALLOW_AGGREGATOR_SYMBOL_FALLBACKS=false
COINGECKO_API_KEY=
COINMARKETCAP_API_KEY=
CRYPTOCOMPARE_API_KEY=

# Trade management guardrails
MAX_LOSS_PER_TRADE_USD=15
PROFIT_LOCK_USD=10
MIN_DYNAMIC_TRAIL_PCT=0.75
TARGET_REWARD_MULTIPLIER=2.8
EXIT_ORDER_REPRICE_PCT=0.25
ALLOW_STANDALONE_TAKE_PROFIT=false

# App
PORT=3000
LOG_LEVEL=INFO
```

All settings can also be managed in the **Settings** tab in the dashboard UI — including multiple LLM providers, crypto exchanges, and brokerage configurations.

### News and threat intelligence

The 15-minute intelligence job ingests direct RSS feeds from global newspapers and television networks plus specialist coverage of crypto, defense, AI infrastructure, chips, data centers, utilities, energy, freight, and supply chains. Direct publishers are preferred. GDELT is used as a broad aggregator only when direct-source volume falls below `NEWS_AGGREGATOR_FALLBACK_THRESHOLD`; set `NEWS_AGGREGATOR_MODE=always` to query it every run or `off` to disable it.

X ingestion uses the official read-only API. Create an X developer app, place its bearer token in `X_BEARER_TOKEN`, and list accounts in `X_USERNAMES`. No browser scraping is used.

Truth Social's public site is not scraped. For commercial use, enter the endpoint and token supplied by TMTG's licensed Truth API or an authorized data vendor in `TRUTH_API_URL` and `TRUTH_API_TOKEN`. The adapter accepts common `data`, `posts`, and `statuses` response shapes; confirm the exact vendor response during onboarding.

Every fresh curated article is retained in News even when LM Studio is offline. Deterministic rules provide baseline sentiment, entity/ticker extraction, asset tags, and threat detection; the LLM enriches those fields when available. URL/title deduplication spans seven days by default, and category balancing prevents broad headline feeds from crowding out specialist AI and supply-chain coverage.

Each article now records canonical provenance, source type, publication and ingestion times, reliability, staleness, related entities, and corroboration. Social posts remain `unconfirmed_social` until an independent publisher reports the same event. `/api/intelligence/status` and `/api/intelligence/sources` expose ingestion and source health.

### Signal evidence and evaluation

Signals use a versioned evidence score rather than presenting LLM confidence as a standalone probability. The score includes calibrated historical confidence, multi-timeframe agreement, risk/reward, volume, regime, data quality, freshness, liquidity, volatility, news confidence, and explicit conflict penalties. Scanner and generated signals store the component breakdown, invalidation condition, market-data timestamp, and setup-specific expiration.

The forward evaluator runs every 15 minutes against cached bars strictly after each signal timestamp. It records target/stop outcomes, maximum favorable excursion, and maximum adverse excursion without using pre-signal bars. Bars that touch both target and stop remain `AMBIGUOUS`. Aggregate results are available at `/api/signals/performance`; these measurements describe historical simulation and do not guarantee fills or future returns.

### Trading horizon and automatic simulation

The Signals tab has `Scalp`, `Longer`, and `Both` modes. Scalp mode accepts 1m/3m/5m/15m setups; Longer accepts 30m/1H/2H/4H/1D; Both accepts the full ladder. The selected mode is stored in `user_preferences` and is enforced both in LLM prompting and again before generated/scanner signals are saved.

Fresh same-symbol, same-direction setups supersede only active or pending setups. Executed and closed history is never rewritten. A simultaneous long and short remain separate so hedged or conflicting theses stay visible.

The Auto Sim tab is a separate paper-only ledger that follows every eligible signal with `$1,000` virtual margin per setup. It tracks open and realized P/L, equity, gross profit/loss, wins, losses, and win rate. It has no broker client dependency and cannot place Alpaca orders. The API is `/api/auto-paper/summary`; `/api/auto-paper/run` performs an immediate virtual mark-to-market/open pass.

### Account and Telegram security foundation

Existing data is assigned to the backward-compatible `local` user during migration. Account preferences, hashed expiring Telegram link tokens, linked chats, deliveries, and callback receipts have a schema for separate ownership-scoped tables, and `lib/account_security.py` implements one-time token use, callback ownership, duplicate callback rejection, preference routing, and paper-only short/leverage actions with test coverage in `tests/test_account_security.py`. **This module is not yet wired into the running bot** — `jobs/telegram_bot.py` currently authorizes with a simpler single-tenant check (the incoming chat id must match the one configured chat id; state-changing actions are rejected outright if no chat id has been configured yet). Provider secrets are redacted from Settings API responses and blank edits preserve stored credentials.

Per-user outbound Telegram delivery is intentionally not enabled until authenticated web account ownership is added, and `lib/account_security.py` is connected in its place. The schema and authorization tests are ready, but enabling delivery before login/session enforcement would allow an unauthenticated browser session to choose another user's destination.

Telegram trade setup alerts use persistent inline controls. `Execute Paper` opens the signal only in Jarvis's virtual paper portfolio, while `Deny` rejects it. The `/paper` command lists each open paper position with `Take Profit`, `Close`, and `Auto Trading` controls. `Take Profit` refuses losing positions so a loss cannot be mislabeled as profit; `Close` is the explicit exit at either a gain or loss. Callback receipts (a DB-level unique constraint, not just an in-memory check) prevent a retried Telegram update from executing twice. Turning Auto Trading off disables automatic paper entries and discretionary TA/LLM management, but hard stop-loss and take-profit enforcement remains active.

The dashboard API itself has no authentication layer and CORS is currently open — anything reachable on the configured port can call trade-executing endpoints (`/api/signals/{id}/execute`, `/approve`, `/approve-all`). Treat the port as trusted-network-only (e.g. behind a firewall/VPN, not exposed to the public internet) until an API key or session layer is added.

### Crypto data providers

Jarvis uses exchange APIs first for tradable spot markets, then aggregator APIs for breadth and fallback:

| Provider | Role | Key |
|---|---|---|
| Binance | Prices, volume discovery, OHLCV | None |
| OKX | Prices, volume discovery, OHLCV | None |
| Bybit | Prices, volume discovery, OHLCV | None |
| Coinbase Exchange | Price/OHLCV fallback | None |
| Kraken | Price/OHLCV fallback | None |
| KuCoin | Prices, volume discovery, OHLCV | None |
| MEXC | Prices, volume discovery, OHLCV | None |
| CoinGecko | Aggregator discovery/OHLCV fallback | Optional |
| CoinPaprika | Aggregator discovery fallback | None |
| CryptoCompare | Aggregator discovery/prices/OHLCV fallback | Optional |
| CoinMarketCap | Aggregator discovery fallback | Optional/keyless public |

`CRYPTO_SCAN_LIMIT` controls how many discovered crypto symbols the 24/7 scanner evaluates each run. `CRYPTO_CACHE_LIMIT` controls how many of those symbols are warmed into the 15-minute OHLCV cache. `CRYPTO_TICKER_CACHE_SECONDS` limits how long batch ticker snapshots are reused. Short and leveraged scanner ideas are routed to the paper engine; live crypto remains spot-style long/bounce only.

Crypto symbols can collide across providers. For example, `BANK` or `BEAT` may refer to different tokens depending on the exchange or aggregator. Jarvis now uses exact exchange pairs first and keeps Yahoo/ambiguous aggregator symbol fallbacks disabled by default. Turn on `ALLOW_YFINANCE_CRYPTO_FALLBACK` or `ALLOW_AGGREGATOR_SYMBOL_FALLBACKS` only if you accept that symbol-only matches can map to the wrong token.

### Trade management guardrails

Approved/executed positions are checked by the position managers every cycle. `MAX_LOSS_PER_TRADE_USD` defaults to `$15` and is clamped to the `$10-$20` band. Stops ratchet tighter as price improves — never looser, even from an LLM-suggested tighten — and are clamped to a sane range so a bad LLM value can't install a near-worthless stop. Paper positions update their stored stop/target directly, and live Alpaca positions try to replace existing stop/take-profit orders when available. This reduces loss exposure, but exact fills still depend on market gaps, liquidity, slippage, broker support, and API availability.

Stop-loss distance is sanity-checked against the symbol's own ATR (`lib/signal_levels.py:clamp_stop_to_atr`) before a signal is saved — an LLM- or scanner-picked stop closer than 0.5× ATR or farther than 5× ATR is widened or tightened into that band, so a quiet stock and a volatile one aren't sized with the same flat distance.

At roughly the halfway point between entry and target ("TP1"), both live and paper positions scale out — closing half the position to lock in realized profit, moving the remaining runner's stop to breakeven, and leaving the original target in place for the rest. This fires once per position (`SCALE_OUT_ENABLED`, `SCALE_OUT_FRACTION` env vars) instead of every exit being all-or-nothing.

The gap between a live signal's intended entry price and Alpaca's actual fill price is recorded the first time each position is observed (`TradingSignal.slippage_pct`) and summarized at `GET /api/execution/slippage` — paper fills are excluded since they always fill exactly at the requested price.

### Kill switch

`GET/POST /api/system/trading-status` exposes a global, system-wide pause independent of any per-user setting — when disabled, `execute_signals` and the manual approve/execute routes refuse to submit new live orders. It does **not** touch existing positions: `manage_positions`'s hard stop-loss/take-profit enforcement keeps running regardless, the same "protection never turns off" philosophy already used for the paper Auto Trading toggle. Toggle it from the dashboard navbar, or via Telegram `/pause` and `/resume`.

### Performance analytics

`GET /api/performance/analytics` computes a real Sharpe ratio and max drawdown from the portfolio's daily equity-snapshot history (`lib/performance_analytics.py`), plus a win-rate/avg-P&L breakdown by originating signal source (watchlist LLM vs. `ta_fallback` vs. opportunistic scanner) so it's visible which source is actually performing. These are genuine computations over `PortfolioSnapshot`/`TradeOutcome` history, not placeholders.

### Backtesting

`POST /api/backtest/run` (`lib/backtester.py`) replays cached historical OHLCV bars through the same deterministic TA-fallback signal logic the live bot falls back to when the LLM is unavailable — never the LLM itself, so a multi-month backtest across several symbols runs in seconds instead of requiring hundreds of model calls. It walks forward one day at a time using only bars up to that checkpoint, builds a candidate signal with `build_ta_fallback_signals` + `score_signal` from the existing pipeline, and resolves each hypothetical trade with the same forward-only evaluator (`lib/signal_evaluation.py`) used for live signal grading — so there's no lookahead bias baked into the results. Runs in a background thread and return a `run_id` immediately; poll `GET /api/backtest/{run_id}` for status and results (win rate, R-multiple equity curve, Sharpe, max drawdown) or `GET /api/backtest` to list recent runs. Capped at 10 symbols per run, and the requested date range is silently clamped to what's actually backfillable per timeframe (`date_range_clamped` in the response) rather than failing outright — check that flag before trusting a long-range result. This evaluates the deterministic fallback strategy only; it does not simulate what the LLM would have generated historically.

---

## 🏗️ Architecture

```
main.py                     — FastAPI entry point + APScheduler startup
app/
  database.py               — SQLAlchemy models + init_db()
  routes.py                 — All API endpoints (/api/*)
  scheduler.py              — APScheduler job definitions
jobs/
  fetch_market_data.py      — Alpaca market data ingestion + OHLCV cache warm-up
  fetch_threat_news.py      — RSS/news threat intelligence
  generate_signals.py       — LLM signal generation (reads TA from cache)
  execute_signals.py        — Alpaca order execution + PendingApproval queue
  manage_positions.py       — Rules engine + LLM position review (v7.0)
  paper_trading.py          — Virtual paper portfolio mark-to-market + routing
  telegram_bot.py           — Telegram alerts + command polling
lib/
  alpaca_client.py          — Alpaca REST wrapper (equity + crypto)
  ta_engine.py              — TA-Lib multi-timeframe analysis (1H/2H/4H/1D)
  ohlcv.py                  — Multi-timeframe OHLCV fetcher (Alpaca IEX + crypto)
  ohlcv_cache.py            — SQLite OHLCV cache with yfinance fallback
  crypto_market_data.py     — Free exchange/aggregator crypto prices + OHLCV
  lmstudio.py               — LM Studio LLM client + sequential lock
  market_regime.py          — SPY-based market regime detection
  risk_manager.py           — Kelly criterion + regime-adjusted position sizing
  signal_scorer.py          — Multi-factor composite signal scoring (0–100)
  signal_levels.py          — Entry/stop/target validation + ATR-based stop clamping
  paper_engine.py           — Virtual portfolio: Long/Short/Leveraged P&L tracking
  earnings_calendar.py      — Yahoo Finance earnings calendar (IV crush guard)
  kill_switch.py            — Global live-trading pause, independent of per-user settings
  performance_analytics.py  — Sharpe ratio, max drawdown, per-signal-source win rate
static/                     — CSS + JS frontend assets (current dashboard, served at "/")
templates/index.html        — Current single-page dashboard (Jinja2 + vanilla JS)
frontend/                   — New Svelte dashboard rebuild, in progress — served at "/next"
app/ws.py                   — WebSocket push channel for frontend/ (job events, live updates)
```

### Dashboard rebuild (`frontend/`)

A ground-up Svelte + TypeScript rebuild of the dashboard is underway, served at `/next` alongside the current one at `/` — both work independently until the new one reaches feature parity. See the design plan for the full information architecture and visual direction.

```bash
cd frontend
npm install        # first time only
npm run dev        # local dev server on :5173, proxies /api and /ws to :3000
npm run build       # outputs to static/dist/, served by FastAPI at /next
npm run check       # type-check
```

`npm run build` output isn't committed (`static/dist/` is gitignored) — run it after pulling changes to `frontend/` to refresh what `/next` serves. Building requires Node.js; nothing at runtime does — the production app still starts with `start.bat`/`start.ps1` exactly as before, no Node process involved.

---

## 🔄 Job Schedule

| Job | Interval | Description |
|---|---|---|
| Fetch Market Data | 15 min | OHLCV + price updates from Alpaca/yfinance |
| Fetch Threat News | 15 min (offset 7m) | RSS geopolitical intelligence |
| Generate Signals | 30 min | LLM signal generation from TA cache + news |
| Execute Signals | 30 min (+3m offset) | Submit pending signals to Alpaca |
| Manage Positions | 5 min | Rules engine + LLM position review |
| Paper Trading | 5 min | Mark-to-market virtual paper portfolio |
| Telegram Bot | 5 min | Inbound command polling + proactive alerts |

---

## 📊 Signal Pipeline

Signals are generated in two tracks:

**Track A — Live (Long / Bounce)**  
Real orders submitted to Alpaca. Signals scored by composite score before execution. Equities queued as `PendingApproval` when markets are closed; approved automatically at open.

**Track B — Paper (Short / Short_Leveraged / Long_Leveraged)**  
Routed to the internal paper engine. Full virtual P&L tracking with mark-to-market and margin simulation. No brokerage connection required for shorts or leverage.

### Composite Signal Scoring (0–100)

Every signal is scored across 10 factors before execution (`lib/signal_scorer.py`, v7.2):

| Factor | Weight |
|---|---|
| Calibrated LLM Confidence | 18% |
| TA Confluence (multi-timeframe agreement) | 20% |
| Risk:Reward Ratio | 15% |
| Data Quality | 10% |
| Volume Confirmation | 8% |
| Market Regime Alignment | 8% |
| Signal Freshness | 7% |
| News Confidence | 5% |
| Liquidity | 5% |
| Volatility | 4% |

Earnings risk, staleness, and conflicting-signal penalties are applied on top of the weighted sum. Signals below the composite threshold — or below `MIN_SIGNAL_DATA_QUALITY` / `MIN_SIGNAL_FRESHNESS` — are automatically rejected before live execution.

---

## 📈 Position Management (v7.0)

Every open position is evaluated every 5 minutes against fresh TA and recent news. Deterministic tier rules fire first (no LLM latency on urgent exits), then the LLM reviews context for nuanced holds, tightenings, and exits.

### Tier Thresholds

**Crypto**

| Gain % | Action |
|---|---|
| ≥ +10% | Close — take profit |
| +5% to +10% | Trail tight (3%) |
| +2% to +5% | Trail moderate (5%) |
| ≤ −4% | Close — cut loss |

**Equity**

| Gain % | Action |
|---|---|
| ≥ +15% | Close — take profit |
| +10% to +15% | Trail tight (5%) |
| +5% to +10% | Trail moderate (8%) |
| ≤ −5% | Close — cut loss |

---

## 📄 Paper Trading Engine

Supports long, short, and leveraged virtual positions independent of broker support.

| Direction | Side | Leverage |
|---|---|---|
| Long / Bounce | Long | 1× |
| Long_Leveraged | Long | 2× |
| Long_5x / Long_10x / Long_20x | Long | 5× / 10× / 20× |
| Short | Short | 1× |
| Short_Leveraged | Short | 2× |
| Short_5x / Short_10x / Short_20x | Short | 5× / 10× / 20× |

- **$100k virtual starting capital**
- Up to 20× leverage on the highest tiers (`lib/paper_engine.py:MAX_LEVERAGE`) — at 20×, roughly a 4.25% adverse move against the position wipes out enough of the margin to trigger the liquidation threshold below, so treat the 5x/10x/20x tiers as high-risk even though this is paper money
- Margin per position is asset-class-based, not a flat amount: $3,000 equity / $2,000 crypto / $1,500 futures / $1,000 forex (`lib/paper_engine.py:ASSET_CLASS_MARGIN`)
- Margin call liquidation at < 15% equity loss on margin
- Automatic mark-to-market every job cycle
- Signal context (TA, LLM reasoning, key risks, trigger events) linked to every position
- Performance tracked separately from live Alpaca positions

### Paper Tab UI Features

- **KPI Dashboard** — Virtual Equity, Total Return, Realized P&L, Win Rate, Open P&L, Cash, Margin In Use, Total Trades
- **Real vs Paper Comparison Card** — side-by-side Alpaca vs virtual account with dual progress bars showing return vs $100k baseline
- **Expandable Position Rows** — click any paper position to reveal:
  - LLM composite score, direction, timeframe, R:R ratio
  - Entry / Target / Stop levels from the originating signal
  - Trade progress bar (% of way to target)
  - Full LLM reasoning text
  - Key risks identified by the LLM
  - Trigger event (news/threat that spawned the signal)
  - Signal source (watchlist vs opportunistic) and status
- **Trade History** — closed paper trades with P&L, close reason, and timing
- **Manual Open** — open any paper position manually with custom entry/target/stop

---

## 🛡️ Risk Management

- **Kelly Criterion** — half-Kelly position sizing per signal
- **Regime multiplier** — reduce size in bear/choppy markets (SPY EMA/RSI/ADX)
- **Earnings guard** — skips entries 3 days before earnings (via Yahoo Finance calendar)
- **Correlation filter** — avoids stacking correlated positions
- **Portfolio exposure limits** — max per-sector and total deployment caps
- **Crypto R:R floor** — 1.0 minimum (vs 1.5 for equities; 24/7 market adjustment)
- **Dust guard** — positions below Alpaca min order qty are skipped gracefully
- **Bracket orders** — all equity entries include take-profit + stop-loss legs

---

## 🛠️ Troubleshooting

**TA-Lib install fails on Windows**
- Only Python 3.12 is supported for the pre-built wheel
- Wheel URL: `https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl`
- Fallback: `pip install ta==0.11.0` (pure Python, slower)

**"py -3.12 not found"**
- Download Python 3.12 from https://www.python.org/downloads/release/python-3120/
- Check "Add Python to PATH" during install

**Alpaca 404 errors on crypto close**
- Fixed in v6.1 — symbols normalized to no-slash format (BTCUSD not BTC/USD)

**"order qty must be >= minimal qty"**
- Fixed in v6.1 — dust guard skips positions below Alpaca minimum order sizes

**"DetachedInstanceError" (SQLAlchemy)**
- Fixed in v6.1 — ORM objects converted to dicts inside session blocks

**LLM calls timing out**
- Sequential lock prevents concurrent LLM requests; jobs queue automatically
- Increase LM Studio context window if prompts are truncated

**Alpaca 429 rate limit on OHLCV fetch**
- Rate limiter uses 0.8s delay + exponential backoff with max 2 concurrent workers

**Paper tab shows 0% return with positions open**
- Fixed in v6.5 — equity now correctly calculated as `cash + margin_deployed + open_pnl`
- Margin is deployed capital, not lost capital

**Paper positions show no data**
- Fixed in v6.5 — null-safe KPI rendering prevents early crash before positions render
- `opened_at` datetime serialized via `.isoformat()` to ensure clean JSON

---

## 📦 Changelog

### v7.1
- New: **Global kill switch** — `/api/system/trading-status`, dashboard navbar toggle, Telegram `/pause` and `/resume`. Blocks new live orders without touching existing positions' stop-loss/take-profit enforcement.
- New: **Partial position scaling** — live and paper positions scale out 50% at the halfway point to target, move the remaining runner's stop to breakeven, and let it ride toward the original target.
- New: **ATR-based stop validation** — `lib/signal_levels.py:clamp_stop_to_atr` widens/tightens an LLM- or scanner-picked stop into a 0.5×–5× ATR band before the signal is saved.
- New: **Slippage tracking** — `GET /api/execution/slippage` compares each live signal's intended entry price to Alpaca's actual fill price.
- New: **Real performance analytics** — `GET /api/performance/analytics` computes an actual Sharpe ratio and max drawdown from equity-snapshot history (previously a hardcoded `0.0` stub), plus win-rate/avg-P&L by signal source, surfaced on the Performance tab.
- New: **Backtesting engine** — replay cached historical OHLCV through the deterministic TA-fallback signal logic (no LLM) to see historical strategy performance before risking capital.

### v6.5
- New: **Real vs Paper comparison card** — side-by-side Alpaca vs virtual account on Paper tab with dual return progress bars and delta label
- New: **Paper position detail panels** — click any paper position row to expand full LLM context (reasoning, key risks, TA score, R:R, progress bar, trigger event, signal source)
- New: **Signal join in paper summary** — `/api/paper/summary` now returns linked signal data for each open position
- Fix: **Paper equity formula** — equity = cash + margin_deployed + open_pnl (was incorrectly showing −15% when $15k margin was deployed)
- Fix: **Null-safe KPI rendering** — paper tab no longer crashes before positions render when fields are null
- Fix: **datetime serialization** — `opened_at`/`closed_at` now serialized via `.isoformat()` for consistent JSON output
- Fix: **Close button propagation** — paper position close button no longer triggers row expand toggle

### v6.4
- New: **Paper trading engine** — virtual Long/Short/Leveraged positions with full P&L tracking
- New: **Composite signal scoring** — 7-factor scoring system (TA, R:R, volume, regime, earnings, freshness)
- New: **Kelly criterion position sizing** — half-Kelly with regime multiplier
- New: **Earnings risk guard** — auto-skips entries near earnings via Yahoo Finance calendar
- New: **Market regime detection** — SPY EMA/RSI/ADX multi-factor regime classification
- New: **Event-driven signal generation** — threats and news trigger immediate signal evaluation
- New: **Position-aware signals** — LLM avoids re-entering already-held positions
- New: **Portfolio guardian** — cross-position correlation and sector exposure limits
- New: **AI position re-evaluation** — LLM reviews each position against fresh TA + news every cycle
- New: `fresh_start.bat` — clean DB + restart in one step
- Improve: Position management promoted to v7.0 with LLM + deterministic hybrid

### v6.1
- Fix: `close_position()` symbol normalization (crypto 404 → slash stripped)
- Fix: Cancel bracket order legs before closing (insufficient qty error)
- Fix: Dust guard — skip crypto positions below min order qty (e.g. ETH < 0.001)
- Fix: SQLAlchemy DetachedInstanceError in performance analytics
- New: OS-specific install scripts for Windows / macOS / Linux
- New: PendingApproval queue for after-hours equity signals

### v6.0
- Migration from Node.js to Python/FastAPI
- TA-Lib C-backed technical analysis (1H/2H/4H/1D multi-timeframe)
- APScheduler replacing node-cron (no Windows timer assertion crashes)
- SQLite OHLCV cache with yfinance fallback
- Multi-provider settings UI (LLM, crypto exchange, brokerage)
- Proactive Telegram alerts + command polling

---

## 📄 License

MIT — use freely, trade responsibly.
