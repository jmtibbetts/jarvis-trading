# Jarvis Trading AI — Python v6.3

Fully local trading command center. FastAPI backend, Bootstrap dashboard, LM Studio LLM inference, Alpaca execution.

---

## Requirements

| Dependency | Version |
|---|---|
| Python | **3.12** |
| TA-Lib C library | See install steps below |
| LM Studio | Latest — https://lmstudio.ai |
| Alpaca account | Paper or Live |

---

## Python 3.12 Setup (Windows)

### 1. Install Python 3.12

Download and install from https://www.python.org/downloads/release/python-3120/

Make sure to check **"Add Python to PATH"** during install.

Verify:
```
python --version
# Python 3.12.x
```

### 2. Install TA-Lib (C library — required before pip install)

TA-Lib must be installed as a pre-built Windows wheel. Do NOT `pip install TA-Lib` directly — it will fail.

```
# Download the correct wheel for Python 3.12 (64-bit Windows):
# https://github.com/cgohlke/talib-build/releases
# File: TA_Lib-0.4.xx-cp312-cp312-win_amd64.whl

pip install TA_Lib-0.4.xx-cp312-cp312-win_amd64.whl
```

Replace `xx` with the latest version available on that page.

### 3. Clone the repo

```
git clone https://github.com/jmtibbetts/jarvis-trading-ai-python.git
cd jarvis-trading-ai-python
```

### 4. Create virtual environment

```
python -m venv .venv
.venv\Scripts\activate
```

### 5. Install dependencies

```
pip install -r requirements.txt
```

### 6. Configure environment

```
copy .env.example .env
```

Edit `.env` with your credentials (see Environment Variables below).

### 7. Start

```
start.bat
```

Or manually:
```
.venv\Scripts\activate
python main.py
```

Dashboard opens at: **http://localhost:3000**

---

## Environment Variables (`.env`)

```env
# Alpaca — paper or live
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_MODE=paper          # paper or live

# LM Studio (local LLM inference)
LM_STUDIO_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=local-model

# Telegram (optional — for signal/threat alerts)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# App
PORT=3000
LOG_LEVEL=INFO
```

> **Note:** API keys can also be added/managed from the **Settings tab** in the dashboard UI — no need to restart after changing them there.

### Settings tab key formats

| Platform | Label | API Key | API Secret | extra_field_1 |
|---|---|---|---|---|
| `alpaca_paper` | Paper Account | Key | Secret | `paper` |
| `alpaca_live` | Live Account | Key | Secret | `live` |
| `lmstudio` | Local LLM | — | — | Model name |
| `telegram` | Telegram Bot | Bot token | — | Chat ID |

---

## LM Studio Setup

1. Download LM Studio: https://lmstudio.ai
2. Download a model (recommended: **Qwen2.5-7B-Instruct** or **Mistral-7B-Instruct**)
3. Start the local server: **Local Server tab → Start Server**
4. Default URL: `http://localhost:1234/v1`
5. Set `LM_STUDIO_MODEL` in `.env` to match the loaded model name

---

## Architecture

```
main.py                  — FastAPI entry point (http://localhost:3000)
app/
  database.py            — SQLAlchemy models + SQLite (WAL mode)
  routes.py              — All /api/* endpoints
  scheduler.py           — APScheduler v2.0 (event-driven + guardian)
jobs/
  generate_signals.py    — 4-track position-aware LLM signal generation (v6.3)
  execute_signals.py     — Alpaca bracket order execution
  manage_positions.py    — Per-position LLM evaluation with TA + news (v7.0)
  fetch_threat_news.py   — RSS feeds + LLM threat classification
  fetch_market_data.py   — Alpaca price snapshots + OHLCV cache warm-up
  telegram_bot.py        — Proactive Telegram alerts + command bot
lib/
  alpaca_client.py       — Alpaca SDK wrapper + symbol normalization
  ohlcv_cache.py         — SQLite OHLCV cache with yfinance fallback
  ta_engine.py           — TA-Lib multi-timeframe analysis (1H/2H/4H/1D)
  lmstudio.py            — LLM client (LM Studio / OpenAI-compat / Anthropic)
  market_regime.py       — SPY trend + ADX + RSI regime detection
  risk_manager.py        — Kelly sizing + sector correlation filter
  signal_scorer.py       — Multi-factor composite scoring
  earnings_calendar.py   — Earnings risk awareness
templates/
  index.html             — Bootstrap 5 dark dashboard
static/
  css/jarvis.css
  js/jarvis.js
data/
  jarvis.db              — SQLite database (auto-created)
  jarvis.log             — Log file
```

---

## Job Schedule

| Job | Interval | Notes |
|---|---|---|
| Market Data | Every 15 min | Prices + OHLCV cache warm-up |
| Threat News | Every 15 min | RSS + LLM threat classification |
| Signal Generation | Every 30 min + event-driven | Fires within 2 min of new intel |
| Execute Signals | Every 30 min | Bracket orders, PendingApproval queue |
| Manage Positions | Every 5 min | Per-position TA + news + LLM evaluation |
| Portfolio Guardian | Every 5 min | Drawdown ceiling, regime shift, concentration |
| Telegram Bot | Every 1 min | Alerts + commands |

---

## Stack

| Component | Library |
|---|---|
| API server | FastAPI + Uvicorn |
| Scheduler | APScheduler (no libuv crashes on Windows) |
| Technical analysis | TA-Lib (C-backed, Python 3.12 wheels) |
| Data fallback | yfinance |
| Broker | alpaca-py |
| Database | SQLAlchemy + SQLite |
| LLM | LM Studio (local) / OpenAI-compat / Anthropic |
| Dashboard | Bootstrap 5 dark theme |
