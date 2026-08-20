#!/usr/bin/env bash
#
# scripts/_common.sh — guards every JARVIS shell script sources first.
#
# WHY THIS EXISTS. These scripts are usually launched from Windows, across a
# shell boundary, and that boundary has already produced real damage three
# separate times:
#
#     wsl.exe -- bash -c 'rm -rf $REPO/logs'
#
# Git Bash (or PowerShell) expands `$REPO` BEFORE wsl.exe is invoked. On the
# Windows side that variable does not exist, so bash inside WSL receives
# `rm -rf /logs` — or, with a trailing slash, `cp -r /`. Nothing is
# malformed, nothing errors, and the command runs with exactly the wrong
# argument. Surveys came back empty for the same reason: the pattern was
# eaten before it arrived.
#
# Two rules follow, and this file enforces both.
#
#   1. NO INLINE CODE ACROSS THE BOUNDARY. Real logic lives in scripts/*.sh
#      and is invoked by path, so there is no string for an outer shell to
#      rewrite. Callers on Windows should use scripts/wsl-run.ps1, which
#      refuses to pass inline code at all.
#
#   2. NO PATH IS EVER INHERITED. The repository is resolved from the
#      location of THIS FILE, never from $PWD and never from a variable an
#      outer shell could blank. A blanked variable is the whole failure
#      mode, so the fix is to not have one.
#
# `set -u` catches an UNSET variable. It does not catch an EMPTY one, and
# empty is what a cross-shell expansion produces — hence need_var below.

# Refuse to be executed; this file only makes sense sourced.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  printf 'scripts/_common.sh is a library — source it, do not run it\n' >&2
  exit 64
fi

# `sh script.sh` silently loses arrays, [[ ]] and BASH_SOURCE. Catch it here
# rather than as a confusing syntax error thirty lines into a caller.
if [[ -z "${BASH_VERSION:-}" ]]; then
  printf 'JARVIS scripts require bash, not sh\n' >&2
  exit 64
fi

set -euo pipefail

# ── Output ────────────────────────────────────────────────────────────────
# Colour only when a terminal is attached: these scripts are also read from
# log files and CI, where escape codes are noise.
if [[ -t 1 ]]; then
  _C_STEP=$'\033[1;36m'; _C_OK=$'\033[0;32m'; _C_WARN=$'\033[0;33m'
  _C_ERR=$'\033[0;31m';  _C_OFF=$'\033[0m'
else
  _C_STEP=''; _C_OK=''; _C_WARN=''; _C_ERR=''; _C_OFF=''
fi

step() { printf '\n%s==> %s%s\n' "$_C_STEP" "$*" "$_C_OFF"; }
ok()   { printf '    %sok%s   %s\n' "$_C_OK" "$_C_OFF" "$*"; }
warn() { printf '    %s!!%s   %s\n' "$_C_WARN" "$_C_OFF" "$*" >&2; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n%sFAILED: %s%s\n' "$_C_ERR" "$*" "$_C_OFF" >&2; exit 1; }

# ── The guards ────────────────────────────────────────────────────────────

# need_var NAME — fail unless the named variable is set AND non-empty.
#
# This is the guard `set -u` cannot provide. A cross-shell expansion does
# not unset a variable, it substitutes nothing for it, and "" then sails
# through every check that only asks whether the name exists.
need_var() {
  local name="${1:?need_var requires a variable name}"
  local value="${!name-}"
  [[ -n "$value" ]] || die "\$$name is empty or unset. If this ran from Windows, an outer shell almost certainly expanded it before WSL saw it — invoke scripts by path (see scripts/wsl-run.ps1), never inline with bash -c."
}

# need_arg DESCRIPTION VALUE — fail unless VALUE is non-empty.
# For values that arrive as positional arguments rather than variables.
need_arg() {
  local what="${1:?}"; shift
  [[ -n "${1:-}" ]] || die "$what is empty — refusing to continue with a blank argument"
}

# JARVIS_ROOT — the repository, derived from this file and nothing else.
#
# Deliberately NOT $PWD (a script must behave the same wherever it is run
# from) and NOT an environment variable (that is the thing that gets
# blanked). `pwd -P` resolves symlinks so the value can be compared safely.
JARVIS_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly JARVIS_ROOT

# A path that must never be the target of anything. Checked as a literal
# list because the point is to be dumb and unbypassable.
_FORBIDDEN_TARGETS=(
  "/" "/bin" "/boot" "/dev" "/etc" "/home" "/lib" "/mnt" "/opt" "/proc"
  "/root" "/run" "/sbin" "/srv" "/sys" "/tmp" "/usr" "/var" "$HOME"
)

# refuse_dangerous_path PATH — die on empty, root, or a system directory.
#
# Every one of these is what a collapsed variable actually produces. `""`
# and `/` are the two that cost real time.
refuse_dangerous_path() {
  local p="${1-}"
  [[ -n "$p" ]] || die "refusing to act on an empty path — a variable collapsed somewhere upstream"
  # Trailing slashes normalised so "/" and "//" and "/mnt/" all match.
  local norm="${p%/}"
  [[ -n "$norm" ]] && p="$norm" || p="/"
  local bad
  for bad in "${_FORBIDDEN_TARGETS[@]}"; do
    [[ -n "$bad" && "$p" == "$bad" ]] && die "refusing to act on $p — that is a system directory, not a JARVIS path"
  done
  return 0
}

# _resolve_absolute PATH — the absolute, symlink-free, ..-free form of PATH,
# whether or not it exists yet.
#
# Existence must not be a precondition: the whole point is to VET a path
# before creating it. `realpath -m` does exactly this; the fallback walks up
# to the deepest ancestor that does exist, resolves that, and re-appends the
# rest — which is the same answer without requiring coreutils.
_resolve_absolute() {
  local p="${1-}"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p" 2>/dev/null && return 0
  fi
  [[ "$p" == /* ]] || p="$PWD/$p"
  local tail="" head="$p"
  while [[ ! -d "$head" && "$head" != "/" && -n "$head" ]]; do
    tail="$(basename -- "$head")${tail:+/$tail}"
    head="$(dirname -- "$head")"
  done
  local base; base="$(cd -- "$head" 2>/dev/null && pwd -P)" || base="$head"
  printf '%s' "${base%/}${tail:+/$tail}"
}

# _resolve_logical PATH — absolute and ..-free, WITHOUT following symlinks.
#
# A different question from _resolve_absolute, and the difference matters.
# `.venv/bin/python` is a symlink to /usr/bin/python3.12, so resolving it
# physically answers "which binary", when what identifies a JARVIS process
# is "which path was it launched by". Containment checks want the physical
# form — a symlink pointing out of the repo is an escape. Identity wants
# this one.
_resolve_logical() {
  local p="${1-}"
  if command -v realpath >/dev/null 2>&1; then
    realpath -ms -- "$p" 2>/dev/null && return 0
  fi
  [[ "$p" == /* ]] || p="$PWD/$p"
  local -a out=() parts=()
  local part old_ifs="$IFS"
  IFS='/' read -ra parts <<< "$p"
  IFS="$old_ifs"
  for part in "${parts[@]}"; do
    case "$part" in
      ''|'.') ;;
      '..')   ((${#out[@]})) && unset 'out[-1]' ;;
      *)      out+=("$part") ;;
    esac
  done
  ((${#out[@]})) || { printf '/'; return 0; }
  printf '/%s' "${out[@]}"
}

# require_within_repo PATH — die unless PATH is inside JARVIS_ROOT.
#
# Anything a JARVIS script creates, moves or removes lives in the repo. A
# target that escaped it did so by accident, and by the time you can see the
# mistake the command has already run.
require_within_repo() {
  local p="${1-}"
  refuse_dangerous_path "$p"
  local resolved; resolved="$(_resolve_absolute "$p")"
  refuse_dangerous_path "$resolved"
  [[ "$resolved" == "$JARVIS_ROOT"/* ]] \
    || die "$p resolves to $resolved, which is outside the repository at $JARVIS_ROOT"
  return 0
}

# assert_linux_native — the runtime must not live on a Windows mount.
#
# NTFS through 9p/drvfs is an order of magnitude slower for the small-file
# access SQLite, pip and node_modules all do, and it does not honour POSIX
# locking the way SQLite's WAL expects.
assert_linux_native() {
  case "$JARVIS_ROOT" in
    /mnt/*) die "$JARVIS_ROOT is on a Windows mount. The active runtime must be on the Linux filesystem (e.g. \$HOME/jarvis-trading)." ;;
  esac
  return 0
}

# jarvis_python — the interpreter, which must be the repo's own venv.
#
# A bare `python3` picks up the system interpreter, or under WSL sometimes a
# Windows one off the inherited PATH. Neither has the project installed, and
# the resulting ImportError reads like a broken install rather than a
# broken PATH.
jarvis_python() {
  local py="$JARVIS_ROOT/.venv/bin/python"
  [[ -x "$py" ]] || die "no virtualenv at $py — run scripts/bootstrap_ubuntu.sh first"
  printf '%s' "$py"
}

# assert_no_windows_path_leak CMD — refuse a tool resolved from /mnt/c.
#
# WSL appends the entire Windows PATH, so `node`, `npm` and occasionally
# `python` resolve to Windows binaries that then build or run against a
# Linux tree.
assert_no_windows_path_leak() {
  local cmd="${1:?}" found
  found="$(command -v "$cmd" 2>/dev/null || true)"
  [[ -n "$found" ]] || die "$cmd not found on PATH"
  case "$found" in
    /mnt/*) die "$cmd resolves to the Windows binary at $found — the Linux one is required" ;;
  esac
  printf '%s' "$found"
}

# Standard locations, all derived, none inherited.
JARVIS_LOG_DIR="$JARVIS_ROOT/logs"
JARVIS_RUN_DIR="$JARVIS_ROOT/run"
JARVIS_PID_FILE="$JARVIS_RUN_DIR/jarvis.pid"
JARVIS_SERVER_LOG="$JARVIS_LOG_DIR/jarvis.log"
readonly JARVIS_LOG_DIR JARVIS_RUN_DIR JARVIS_PID_FILE JARVIS_SERVER_LOG

jarvis_port() { printf '%s' "${PORT:-3000}"; }

# ── Process identity ──────────────────────────────────────────────────────
#
# NEVER `pkill python`, `pkill -f main.py`, or any pattern match over the
# process table. This machine runs Claude Code, a hermes agent and LM
# Studio's own runtime, all of them Python or matching on "main". A pattern
# that is merely "specific enough" is how you take down the operator's
# tooling while trying to restart a web server.
#
# Identity here is a conjunction of three facts that nothing else on the box
# can satisfy at once:
#
#   1. argv[0] resolves to THIS repository's venv interpreter
#   2. argv[1] is main.py
#   3. the process's cwd is THIS repository
#
# (3) is what separates two checkouts of JARVIS from each other, which a
# command-line match alone never could.

# jarvis_pid_matches PID — true when PID is a JARVIS server for this repo.
jarvis_pid_matches() {
  local pid="${1-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  local dir="/proc/$pid"
  [[ -r "$dir/cmdline" ]] || return 1

  local cwd
  cwd="$(readlink -f "$dir/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$JARVIS_ROOT" ]] || return 1

  # argv is NUL-separated. Read it as a list, not as text: joining it first
  # would let `grep main.py` or an editor session match.
  local -a argv=()
  mapfile -d '' -t argv < "$dir/cmdline" 2>/dev/null || return 1
  ((${#argv[@]} >= 2)) || return 1

  # argv[0] is frequently relative (".venv/bin/python") because that is how
  # the process was launched. Resolve it against its own cwd.
  local exe="${argv[0]}"
  [[ "$exe" == /* ]] || exe="$cwd/$exe"
  [[ "$(_resolve_logical "$exe")" == "$JARVIS_ROOT/.venv/bin/python" ]] || return 1
  [[ "$(basename -- "${argv[1]}")" == "main.py" ]] || return 1
  return 0
}

# jarvis_pids — every JARVIS server PID belonging to this repository.
jarvis_pids() {
  local dir pid
  for dir in /proc/[0-9]*; do
    pid="${dir#/proc/}"
    jarvis_pid_matches "$pid" && printf '%s\n' "$pid"
  done
  return 0
}

# jarvis_pid_from_file — the recorded PID, but only if it is still OURS.
#
# A PID file is a claim, not evidence: PIDs are recycled, and a stale file
# pointing at whatever now holds that number is how a stop script kills a
# stranger. The number is only ever used after the identity check passes.
jarvis_pid_from_file() {
  [[ -r "$JARVIS_PID_FILE" ]] || return 1
  local pid
  pid="$(tr -dc '0-9' < "$JARVIS_PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  jarvis_pid_matches "$pid" || return 1
  printf '%s' "$pid"
}

# jarvis_establish_modes — declare the posture; never inherit it.
#
# DEFENCE IN DEPTH, DELIBERATELY REDUNDANT. lib/platform_mode.py already
# fails CLOSED to VIRTUAL_ONLY, and that boundary stays exactly as it is.
# This is a SECOND, independent check in front of it, because a library
# default protects against silence -- it does not protect against a
# conflicting value that someone or something actually set. An exported
# JARVIS_PLATFORM_MODE=LIVE_ENABLED in a shell, a unit file, or a stale
# profile would sail straight past a fail-closed default, because the
# default never runs when a value is present.
#
# The two variables fail in opposite directions and both are checked here:
#   platform_mode  fails CLOSED (unset -> VIRTUAL_ONLY, safe)
#   runtime_mode   fails OPEN   (unset -> FULL_VIRTUAL, permits mutation)
#
# For every supported launcher the answer is the same: the script NAMES the
# posture it exists to run. An inherited value that differs is a conflict,
# not an override -- a process whose posture contradicts the script that
# started it is the ambiguity this removes, so it dies rather than guessing.
#
# Usage: jarvis_establish_modes <PLATFORM_MODE> <RUNTIME_MODE>
jarvis_establish_modes() {
  local want_platform="$1" want_runtime="$2"

  case "$want_platform" in
    VIRTUAL_ONLY|LIVE_SHADOW|LIVE_LIMITED|LIVE_ENABLED) ;;
    *) die "internal: unknown platform mode requested: $want_platform" ;;
  esac
  case "$want_runtime" in
    FULL_VIRTUAL|EVIDENCE_ONLY) ;;
    *) die "internal: unknown runtime mode requested: $want_runtime" ;;
  esac

  _jarvis_fix_mode PLATFORM "$want_platform" "${JARVIS_PLATFORM_MODE:-}" || return 1
  export JARVIS_PLATFORM_MODE="$want_platform"
  _jarvis_fix_mode RUNTIME "$want_runtime" "${JARVIS_RUNTIME_MODE:-}" || return 1
  export JARVIS_RUNTIME_MODE="$want_runtime"

  # Confirm through the LIBRARY, not through the variable just exported.
  # Reading back the string this function set would only prove this function
  # can assign a variable; asking the library proves the process will
  # actually resolve the posture that was declared.
  local eff_platform eff_runtime
  eff_platform="$("$PYTHON" -c 'from lib.platform_mode import current_mode; print(current_mode())' 2>/dev/null || echo unknown)"
  eff_runtime="$("$PYTHON" -c 'from lib.runtime_mode import current_mode; print(current_mode())' 2>/dev/null || echo unknown)"
  [[ "$eff_platform" == "$want_platform" ]] || die     "platform mode resolved to $eff_platform but $want_platform was declared"
  [[ "$eff_runtime" == "$want_runtime" ]] || die     "runtime mode resolved to $eff_runtime but $want_runtime was declared"

  ok "platform mode: $eff_platform (declared and confirmed by the library)"
  ok "runtime mode:  $eff_runtime (declared and confirmed by the library)"
  if [[ "$eff_platform" != "VIRTUAL_ONLY" ]]; then
    warn "REAL MONEY IS REACHABLE — platform mode is $eff_platform"
  fi
}

# One variable: absent is fine, matching is fine, differing is fatal.
_jarvis_fix_mode() {
  local label="$1" want="$2" have="$3"
  if [[ -z "$have" ]]; then
    return 0
  elif [[ "$have" == "$want" ]]; then
    return 0
  fi
  die "JARVIS_${label}_MODE=$have was inherited from the environment, but
      this launcher runs $want. Refusing to start. A process whose posture
      disagrees with the script that started it is the ambiguity this check
      exists to remove — unset the variable, or use the launcher that
      matches the posture you want."
}
