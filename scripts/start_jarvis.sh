#!/usr/bin/env bash
#
# Start the JARVIS backend.
#
#     scripts/start_jarvis.sh                  API/UI only, no background jobs
#     scripts/start_jarvis.sh --with-scheduler background jobs ON (deliberate)
#     scripts/start_jarvis.sh --foreground     run in this terminal
#     scripts/start_jarvis.sh --dry-run        print the plan, launch nothing
#
# THE SCHEDULER IS OFF UNLESS ASKED FOR. main.py's own default is the
# opposite: anything other than JARVIS_DISABLE_SCHEDULER=1 starts
# APScheduler, which begins fetching data and, at T+3m, EXECUTING SIGNALS.
# A start script that inherited that default would turn "let me look at the
# dashboard" into live activity nobody asked for. So the flag is required,
# it is named, and the choice is printed either way.
#
# Everything is repo-relative and identity-checked; see scripts/_common.sh
# for why none of these paths may be inherited.

source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

WITH_SCHEDULER=0
FOREGROUND=0
DRY_RUN=0

usage() {
  sed -n '3,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while (($#)); do
  case "$1" in
    --with-scheduler) WITH_SCHEDULER=1 ;;
    --foreground|-f)  FOREGROUND=1 ;;
    --dry-run|-n)     DRY_RUN=1 ;;
    -h|--help)        usage 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

step "Preflight"
assert_linux_native
ok "repository: $JARVIS_ROOT"

PYTHON="$(jarvis_python)"
ok "interpreter: $PYTHON"
[[ -f "$JARVIS_ROOT/main.py" ]] || die "no main.py in $JARVIS_ROOT"

# ── Already running? ──────────────────────────────────────────────────────
# Checked by identity rather than by port: something else holding :3000 is a
# different problem with a different message, and a second JARVIS against
# the same SQLite file produces `database is locked` on the operator's
# running instance — which has happened twice.
step "Existing instance"
mapfile -t RUNNING < <(jarvis_pids)
if ((${#RUNNING[@]})); then
  for pid in "${RUNNING[@]}"; do
    info "pid $pid  $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
  done
  if ((DRY_RUN)); then
    warn "already running (pid ${RUNNING[*]}) — a real start would stop here"
  else
    die "JARVIS is already running (pid ${RUNNING[*]}). Stop it with scripts/stop_jarvis.sh — a second instance against the same SQLite file produces 'database is locked' on the one you already have."
  fi
else
  ok "none running for this repository"
fi

PORT_NUM="$(jarvis_port)"
if ((!DRY_RUN)) && ((${#RUNNING[@]} == 0)) \
   && command -v lsof >/dev/null 2>&1 \
   && lsof -nP -iTCP:"$PORT_NUM" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "port $PORT_NUM is already in use by something that is NOT a JARVIS instance for this repo"
  lsof -nP -iTCP:"$PORT_NUM" -sTCP:LISTEN | sed 's/^/      /' >&2
  die "refusing to start into an occupied port"
fi

# ── Runtime directories ───────────────────────────────────────────────────
require_within_repo "$JARVIS_RUN_DIR"
require_within_repo "$JARVIS_LOG_DIR"
((DRY_RUN)) || mkdir -p "$JARVIS_RUN_DIR" "$JARVIS_LOG_DIR"

# ── The one decision this script exists to make explicit ──────────────────
step "Scheduler"
if ((WITH_SCHEDULER)); then
  unset JARVIS_DISABLE_SCHEDULER
  warn "scheduler ENABLED by --with-scheduler"
  warn "background jobs fire immediately; signal EXECUTION begins at T+3m"
else
  export JARVIS_DISABLE_SCHEDULER=1
  ok "scheduler DISABLED (default) — API and UI only, no background jobs"
  info "pass --with-scheduler to enable it deliberately"
fi

# THE POSTURE THIS LAUNCHER RUNS, NAMED RATHER THAN INHERITED.
#
# Platform mode is still left to lib/platform_mode.py, which fails CLOSED to
# VIRTUAL_ONLY -- re-exporting a default there would only create a second
# place to get it wrong. Runtime mode is the opposite: it fails OPEN to
# FULL_VIRTUAL, so silence permits economic mutation. This is the normal
# runtime, so it says FULL_VIRTUAL out loud and refuses to start if
# something inherited disagrees.
step "Mode"
jarvis_establish_modes VIRTUAL_ONLY FULL_VIRTUAL

step "Starting"
info "bind:  ${JARVIS_BIND_HOST:-127.0.0.1}:$PORT_NUM"
info "log:   $JARVIS_SERVER_LOG"

cd "$JARVIS_ROOT"

if ((DRY_RUN)); then
  step "Dry run"
  ok "nothing was launched, and nothing was written"
  exit 0
fi

if ((FOREGROUND)); then
  ok "running in the foreground — Ctrl-C to stop"
  exec "$PYTHON" main.py
fi

nohup "$PYTHON" main.py >> "$JARVIS_SERVER_LOG" 2>&1 &
NEW_PID=$!
printf '%s\n' "$NEW_PID" > "$JARVIS_PID_FILE"

# ── Prove it came up ──────────────────────────────────────────────────────
# A PID is not a running server. Poll the health endpoint, and if it never
# answers, say so with the log tail rather than reporting a successful start
# because a process existed for a moment.
step "Waiting for health"
HEALTH="http://127.0.0.1:$PORT_NUM/api/health"
for _ in $(seq 1 60); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    warn "process $NEW_PID exited during startup"
    tail -n 30 "$JARVIS_SERVER_LOG" | sed 's/^/      /' >&2
    rm -f "$JARVIS_PID_FILE"
    die "JARVIS failed to start"
  fi
  if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
    ok "healthy at $HEALTH"
    ok "pid $NEW_PID (recorded in $JARVIS_PID_FILE)"
    step "Started"
    info "status: scripts/status_jarvis.sh"
    info "stop:   scripts/stop_jarvis.sh"
    exit 0
  fi
  sleep 1
done

warn "no health response after 60s"
tail -n 30 "$JARVIS_SERVER_LOG" | sed 's/^/      /' >&2
die "JARVIS started but never became healthy — pid $NEW_PID is still running; stop it with scripts/stop_jarvis.sh"
