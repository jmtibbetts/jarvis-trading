#!/usr/bin/env bash
# Jarvis Trading AI v6.1 — Linux Install & Launch
# Supports: Ubuntu/Debian (apt), Fedora/RHEL (dnf), Arch (pacman)
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ============================================================"
echo "   Jarvis Trading AI v6.1  [Linux]"
echo "  ============================================================"
echo ""

# ── Detect package manager ────────────────────────────────────
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
else
    echo "  WARNING: Unknown package manager — manual dependency install may be needed."
    PKG_MGR="unknown"
fi
echo "  Package manager: $PKG_MGR"

# ── Python 3.12 ───────────────────────────────────────────────
PY=""
for py in python3.12 python3; do
    if command -v $py &>/dev/null; then
        VER=$($py -c "import sys; print(sys.version_info[:2])")
        if [[ "$VER" == "(3, 12)" ]] || [[ "$py" == "python3.12" ]]; then
            PY="$py"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "  Python 3.12 not found. Installing..."
    case $PKG_MGR in
        apt)
            sudo apt-get update -qq
            sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
            ;;
        dnf)
            sudo dnf install -y python3.12 python3.12-devel
            ;;
        pacman)
            sudo pacman -Sy --noconfirm python
            ;;
    esac
    PY="python3.12"
fi
echo "  Python: $($PY --version)"

# ── TA-Lib C library ──────────────────────────────────────────
if ! python3 -c "import talib" 2>/dev/null; then
    echo "  Installing TA-Lib C library..."
    case $PKG_MGR in
        apt)
            sudo apt-get install -y build-essential wget
            if ! dpkg -l | grep -q libta-lib; then
                cd /tmp
                wget -q https://sourceforge.net/projects/ta-lib/files/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz
                tar -xzf ta-lib-0.4.0-src.tar.gz
                cd ta-lib && ./configure --prefix=/usr && make -j$(nproc) && sudo make install
                cd "$SCRIPT_DIR"
            fi
            ;;
        dnf)
            sudo dnf install -y ta-lib ta-lib-devel 2>/dev/null || {
                sudo dnf install -y gcc make wget
                cd /tmp
                wget -q https://sourceforge.net/projects/ta-lib/files/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz
                tar -xzf ta-lib-0.4.0-src.tar.gz
                cd ta-lib && ./configure --prefix=/usr && make -j$(nproc) && sudo make install
                cd "$SCRIPT_DIR"
            }
            ;;
        pacman)
            sudo pacman -Sy --noconfirm ta-lib 2>/dev/null || {
                echo "  TA-Lib not in pacman — building from source..."
                sudo pacman -Sy --noconfirm base-devel wget
                cd /tmp
                wget -q https://sourceforge.net/projects/ta-lib/files/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz
                tar -xzf ta-lib-0.4.0-src.tar.gz
                cd ta-lib && ./configure --prefix=/usr && make -j$(nproc) && sudo make install
                cd "$SCRIPT_DIR"
            }
            ;;
    esac
fi

# ── Virtual environment ───────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    $PY -m venv .venv
fi
source .venv/bin/activate

pip install --upgrade pip --quiet
echo "  Installing Python dependencies..."
pip install -r requirements.txt --quiet

# TA-Lib Python binding
python -c "import talib" 2>/dev/null || {
    echo "  Installing TA-Lib Python binding..."
    pip install TA-Lib --quiet || pip install ta==0.11.0 --quiet
}

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

# Open browser if xdg-open available
(sleep 3 && (xdg-open "http://localhost:3000" 2>/dev/null || true)) &

python main.py
