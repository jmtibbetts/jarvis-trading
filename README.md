# Jarvis Trading AI — Python Edition (v5.0)

Complete rewrite of the Node.js stack in Python. Cleaner, faster, more stable on Windows.

## Stack
- **FastAPI** — REST API + auto-generated docs at `/docs`
- **APScheduler** — No more libuv timer crashes on Windows
- **pandas-ta** — 130+ TA indicators (EMA, RSI, MACD, BB, ATR, VWAP, Stoch, OBV, ADX)
- **alpaca-py** — Official Alpaca SDK
- **SQLAlchemy + SQLite** — WAL mode, proper ORM
- **Bootstrap 5** — Dark theme dashboard, no React/build step

## New Features (Python-only)
- **Market Regime Detection** — SPY trend + ADX + RSI → risk-on/off/bear classification
- **Kelly Criterion Sizing** — Half-Kelly position sizing based on win rate + R:R
- **Correlation/Sector Filter** — Prevents over-concentration in one sector
- **Portfolio Heat Monitor** — Blocks new entries when existing positions are losing
- **Earnings Calendar Awareness** — Penalizes signals before earnings
- **Multi-Factor Signal Scoring** — Composite score: LLM + TA confluence + R:R + volume + regime
- **ADX Trend Strength** — Filters out choppy/weak trends
- **Stochastic + OBV** — Additional entry confirmation indicators
- **FastAPI /docs** — Full interactive API explorer at `/docs`

## Setup

```
# Windows
start.bat

# Or manually
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys
python main.py
```

## API Keys (Settings tab in UI)

| Platform | Label | Fields |
|---|---|---|
| alpaca_paper | My Paper Account | API Key, Secret, extra_field_1="paper" |
| alpaca_live  | Live Account | API Key, Secret, extra_field_1="live" |
| lmstudio | Local LLM | api_url=http://localhost:1234/v1, extra_field_1=model-name |
| telegram | Bot | api_key=BOT_TOKEN, extra_field_1=CHAT_ID |

## Directory Structure
```
main.py              — FastAPI app entry point
app/
  database.py        — SQLAlchemy models + session
  routes.py          — All /api/* endpoints
  scheduler.py       — APScheduler job runner
jobs/
  generate_signals.py  — 4-track LLM signal generation
  execute_signals.py   — Alpaca bracket order execution
  manage_positions.py  — Profit protection + trailing stops
  fetch_threat_news.py — RSS + LLM threat analysis
  fetch_market_data.py — Alpaca market data snapshots
  telegram_bot.py      — Telegram command bot
lib/
  alpaca_client.py   — Alpaca SDK wrapper
  ohlcv.py           — Multi-timeframe OHLCV fetcher
  ta_engine.py       — pandas-ta multi-timeframe analysis
  lmstudio.py        — LM Studio LLM client
  market_regime.py   — SPY regime detection (NEW)
  risk_manager.py    — Kelly sizing + sector filter (NEW)
  signal_scorer.py   — Multi-factor composite scoring (NEW)
  earnings_calendar.py — Earnings risk awareness (NEW)
templates/
  index.html         — Bootstrap 5 dark dashboard
static/
  css/jarvis.css
  js/jarvis.js
data/
  jarvis.db          — SQLite database
  jarvis.log         — Log file
```
