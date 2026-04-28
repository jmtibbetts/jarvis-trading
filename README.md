# 🤖 Jarvis Trading AI v6.1

> **Python Edition** — FastAPI + APScheduler + TA-Lib + Alpaca  
> Auto-trading: equities, crypto, commodities + geopolitical threat intelligence

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

All settings can also be managed in the **Settings** tab in the dashboard UI.

---

## 🏗️ Architecture

```
main.py                     — FastAPI entry point + APScheduler startup
app/
  database.py               — SQLAlchemy models + init_db()
  routes.py                 — All API endpoints (/api/*)
  scheduler.py              — APScheduler job definitions
jobs/
  fetch_market_data.py      — Alpaca market data ingestion
  fetch_threat_news.py      — RSS/news threat intelligence
  generate_signals.py       — LLM signal generation
  execute_signals.py        — Alpaca order execution
  manage_positions.py       — Active position management (rules + LLM)
  telegram_bot.py           — Telegram alerts + commands
lib/
  alpaca_client.py          — Alpaca REST wrapper
  ta_engine.py              — TA-Lib multi-timeframe analysis
  ohlcv_cache.py            — SQLite OHLCV cache
  lmstudio.py               — LM Studio LLM client
  market_regime.py          — SPY-based regime detection
  risk_manager.py           — Position sizing + portfolio risk
  signal_scorer.py          — Signal quality scoring
static/                     — CSS + JS frontend assets
templates/index.html        — Single-page dashboard
```

---

## 🔄 Job Schedule

| Job | Interval | Description |
|---|---|---|
| Fetch Market Data | 15 min | OHLCV + price updates from Alpaca/yfinance |
| Fetch Threat News | 15 min (offset 7m) | RSS geopolitical intelligence |
| Generate Signals | 30 min | LLM signal generation from TA + news |
| Execute Signals | 30 min (+3m offset) | Submit pending signals to Alpaca |
| Manage Positions | 5 min | Rules engine + LLM position review |
| Telegram Bot | 5 min | Inbound command polling + proactive alerts |

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
- Fixed in v6.1 — symbols are normalized to no-slash format (BTCUSD not BTC/USD)

**"order qty must be >= minimal qty"**
- Fixed in v6.1 — dust guard skips positions below Alpaca minimum order sizes

**"DetachedInstanceError" (SQLAlchemy)**
- Fixed in v6.1 — ORM objects converted to dicts inside session blocks

---

## 📦 Changelog

### v6.1
- Fix: `close_position()` symbol normalization (crypto 404 → slash stripped)
- Fix: Cancel bracket order legs before closing (insufficient qty)
- Fix: Dust guard — skip crypto positions below min order qty (e.g. ETH < 0.001)
- Fix: SQLAlchemy DetachedInstanceError in performance analytics
- New: OS-specific install scripts for Windows / macOS / Linux

### v6.0
- Migration from Node.js to Python/FastAPI
- TA-Lib C-backed technical analysis
- APScheduler replacing node-cron
- SQLite OHLCV cache with yfinance fallback
- Multi-provider settings UI
- Proactive Telegram alerts

---

## 📄 License

MIT — use freely, trade responsibly.
