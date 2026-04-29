# 🤖 Jarvis Trading AI v6.4

> **Python Edition** — FastAPI + APScheduler + TA-Lib + Alpaca  
> Autonomous trading across equities, crypto, and commodities with geopolitical threat intelligence, multi-factor signal scoring, AI position management, and a parallel paper trading engine for shorts and leverage.

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

# App
PORT=3000
LOG_LEVEL=INFO
```

All settings can also be managed in the **Settings** tab in the dashboard UI — including multiple LLM providers, crypto exchanges, and brokerage configurations.

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
  lmstudio.py               — LM Studio LLM client + sequential lock
  market_regime.py          — SPY-based market regime detection
  risk_manager.py           — Kelly criterion + regime-adjusted position sizing
  signal_scorer.py          — Multi-factor composite signal scoring (0–100)
  paper_engine.py           — Virtual portfolio: Long/Short/Leveraged P&L tracking
  earnings_calendar.py      — Yahoo Finance earnings calendar (IV crush guard)
static/                     — CSS + JS frontend assets
templates/index.html        — Single-page dashboard
```

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

Every signal is scored across 7 factors before execution:

| Factor | Weight |
|---|---|
| LLM Confidence | 30% |
| TA Confluence (multi-timeframe agreement) | 20% |
| Risk:Reward Ratio | 20% |
| Volume Confirmation | 10% |
| Market Regime Alignment | 15% |
| Signal Freshness | 5% |
| Earnings Risk Penalty | −25 pts |

Signals below the composite threshold are automatically rejected.

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
| Long | Long | 1× |
| Long_Leveraged | Long | 2× |
| Short | Short | 1× |
| Short_Leveraged | Short | 2× |

- $100k virtual starting capital
- Max 3× leverage
- Margin call liquidation at < 20% equity
- Automatic mark-to-market every job cycle
- Performance tracked separately from live Alpaca positions

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

---

## 📦 Changelog

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
