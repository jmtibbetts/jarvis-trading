#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 (WSL2) machine into a complete JARVIS
# runtime. Idempotent: running it twice must not break the machine.
#
#     ./scripts/bootstrap_ubuntu.sh
#     ./scripts/bootstrap_ubuntu.sh --skip-frontend
#
# MACHINE PROVISIONING ONLY. This never copies, opens or migrates an
# operator database — see scripts/snapshot_operator_db.py for that. It is
# safe to run before any operator data exists.
#
# WHY THIS EXISTS. Native Windows Python is no longer the supported
# runtime: order_book's C extension does not build under MSVC without a
# vendored patch, and that patch stopped applying to the upstream sdist.
# Linux builds it normally. Ubuntu 24.04 under WSL2 is now the supported
# JARVIS runtime, and this is the one way to construct it.
#
# FAILS CLOSED. `set -euo pipefail` plus an explicit trap: there is no
# warn-and-continue and no partial success. The Windows CI bug this
# replaces was precisely a failing install followed by a succeeding one,
# which reported green.

set -euo pipefail

REPO_DIR="${JARVIS_REPO_DIR:-$HOME/jarvis-trading}"
VENV="$REPO_DIR/.venv"
NODE_MAJOR="${JARVIS_NODE_MAJOR:-20}"
SKIP_FRONTEND=0
[[ "${1:-}" == "--skip-frontend" ]] && SKIP_FRONTEND=1

step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[0;32mok\033[0m  %s\n' "$*"; }
warn()  { printf '    \033[0;33m!!\033[0m  %s\n' "$*"; }
die()   { printf '\n\033[0;31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'die "aborted at line $LINENO — nothing further will run, because a half-provisioned machine must not be tested against"' ERR

# ── 0. Sanity ────────────────────────────────────────────────────────────
step "Environment"
[[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
. /etc/os-release
echo "    ${PRETTY_NAME:-unknown}"
[[ "${VERSION_ID:-}" == "24.04" ]] || warn "expected Ubuntu 24.04, found ${VERSION_ID:-unknown}"
uname -r | grep -qi microsoft && ok "WSL2 kernel: $(uname -r)" || warn "not a WSL kernel — continuing"

# THE REPOSITORY MUST NOT LIVE UNDER /mnt/c.
# NTFS through the 9p/drvfs translation layer is an order of magnitude
# slower for the small-file access SQLite, pip and node_modules all do,
# and it does not honour POSIX locking the way SQLite's WAL expects.
case "$REPO_DIR" in
  /mnt/*) die "$REPO_DIR is on a Windows mount. The active runtime must be on the Linux filesystem (e.g. \$HOME/jarvis-trading)." ;;
esac
ok "repository path is Linux-native: $REPO_DIR"

# ── 1. System packages ───────────────────────────────────────────────────
step "System packages"
sudo apt-get update -qq

# Discovered from what the project actually needs, not copied from a list:
#   build-essential/gcc/make  order_book's C extension
#   python3-dev               Python.h for any source build
#   libssl-dev/libffi-dev     cryptography, aiohttp
#   libsqlite3-dev + sqlite3  the DB and its CLI for integrity checks
#   pkg-config                configure steps for the above
#   jq                        used by the doctor script
PKGS=(
  build-essential gcc g++ make pkg-config
  git curl wget ca-certificates gnupg
  python3 python3-dev python3-venv python3-pip
  libssl-dev libffi-dev zlib1g-dev
  sqlite3 libsqlite3-dev
  jq unzip file lsof procps
)
MISSING=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if ((${#MISSING[@]})); then
  echo "    installing: ${MISSING[*]}"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
else
  ok "all present"
fi
ok "sqlite3 $(sqlite3 --version | awk '{print $1}')"

# ── 2. Python ────────────────────────────────────────────────────────────
step "Python runtime"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    python3 $PYV"
[[ "$PYV" == "3.12" ]] || warn "project targets 3.12; found $PYV"

[[ -d "$REPO_DIR" ]] || die "repository not found at $REPO_DIR — clone it first"
cd "$REPO_DIR"

if [[ ! -x "$VENV/bin/python" ]]; then
  step "Creating virtualenv"
  python3 -m venv "$VENV"
fi
ok "venv: $VENV"

# Never `sudo pip`. Everything below is inside the venv.
"$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
ok "pip $("$VENV/bin/python" -m pip --version | awk '{print $2}')"

step "Project requirements"
# NO WINDOWS PATCH HERE. order_book builds from source under Linux with
# build-essential present; the MSVC patch is Windows-only and applying it
# on Linux would be an unreviewed edit to source that compiles fine.
"$VENV/bin/python" -m pip install --quiet -r requirements.txt
"$VENV/bin/python" -m pip check
ok "pip check clean"

step "Smoke imports"
"$VENV/bin/python" - <<'PY'
import importlib, sys
mods = ["sqlalchemy", "fastapi", "uvicorn", "pydantic",
        "order_book", "cryptofeed", "httpx", "apscheduler"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
        print(f"    ok  {m}")
    except Exception as e:
        bad.append(f"{m}: {type(e).__name__}: {e}")
        print(f"    FAIL {m}: {e}")
if bad:
    sys.exit("critical modules failed to import: " + "; ".join(bad))
PY

# ── 3. Node ──────────────────────────────────────────────────────────────
if ((SKIP_FRONTEND)); then
  warn "skipping frontend by request"
else
  step "Node runtime"
  # WSL puts the Windows PATH on Ubuntu's PATH, so `npm` frequently
  # resolves to /mnt/c/Program Files/nodejs/npm — the WINDOWS binary —
  # while `node` is absent entirely. Running that against a Linux tree
  # builds native modules for the wrong platform. Demand a Linux node.
  NODE_BIN="$(command -v node || true)"
  if [[ -z "$NODE_BIN" || "$NODE_BIN" == /mnt/* ]]; then
    [[ "$NODE_BIN" == /mnt/* ]] && warn "ignoring Windows node at $NODE_BIN"
    echo "    installing Node ${NODE_MAJOR}.x (NodeSource)"
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash -
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
    hash -r
  fi
  NODE_BIN="$(command -v node)"
  case "$NODE_BIN" in
    /mnt/*) die "node still resolves to the Windows binary at $NODE_BIN" ;;
  esac
  ok "node $(node --version) at $NODE_BIN"
  ok "npm  $(npm --version) at $(command -v npm)"

  step "Frontend"
  cd "$REPO_DIR/frontend"
  # `npm ci` not `npm install`: the lockfile is the contract, and ci is
  # the only one that honours it exactly.
  [[ -f package-lock.json ]] || die "no package-lock.json — refusing to npm install"
  npm ci --no-audit --no-fund
  npm run check
  npm run build
  ok "typecheck 0 errors, build succeeded"
  cd "$REPO_DIR"
fi

# ── 4. Runtime directories ───────────────────────────────────────────────
step "Runtime directories"
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"
ok "data/ and logs/ present (no operator database was created or touched)"

step "Done"
cat <<EOF

    Machine provisioned. NOT yet migrated — this script deliberately does
    not touch operator data.

    Next:
      scripts/doctor.sh                 read-only environment report
      scripts/snapshot_operator_db.py   verified DB snapshot (run on the source)
      scripts/start_jarvis.sh           start the backend

EOF
