#!/usr/bin/env bash
# Jarvis Trading AI v6.1 — macOS Install & Launch
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ============================================================"
echo "   Jarvis Trading AI v6.1  [macOS]"
echo "  ============================================================"
echo ""

# ── Homebrew check ────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    echo "  Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── Python 3.12 ───────────────────────────────────────────────
if ! command -v python3.12 &>/dev/null; then
    echo "  Installing Python 3.12 via Homebrew..."
    brew install python@3.12
fi
PY="python3.12"
echo "  Python: $($PY --version)"

# ── TA-Lib C library ──────────────────────────────────────────
if ! brew list ta-lib &>/dev/null 2>&1; then
    echo "  Installing TA-Lib C library via Homebrew..."
    brew install ta-lib
fi
echo "  TA-Lib C library OK"

# ── Virtual environment ───────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    $PY -m venv .venv
fi
source .venv/bin/activate

# ── Python dependencies ───────────────────────────────────────
echo "  Upgrading pip..."
pip install --upgrade pip --quiet

echo "  Installing Python dependencies..."
pip install -r requirements.txt --quiet

# TA-Lib Python binding (C lib must be installed first via brew)
python -c "import talib" 2>/dev/null || {
    echo "  Installing TA-Lib Python binding..."
    pip install TA-Lib --quiet || {
        echo "  TA-Lib binding failed — falling back to 'ta'..."
        pip install ta==0.11.0 --quiet
    }
}
echo "  TA-Lib OK"

# ── Data dir + .env ───────────────────────────────────────────
mkdir -p data
if [ ! -f ".env" ]; then
    [ -f ".env.example" ] && cp .env.example .env
    echo "  NOTE: .env created from template — edit with your API keys."
fi

# ── Launch ────────────────────────────────────────────────────
echo ""
echo "  Starting Jarvis at http://localhost:3000 ..."
echo "  Press Ctrl+C to stop."
echo ""

# Open browser after 3s
(sleep 3 && open "http://localhost:3000") &

python main.py
