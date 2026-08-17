#!/usr/bin/env bash
#
# Stop the JARVIS backend belonging to THIS repository.
#
#     scripts/stop_jarvis.sh              graceful, then escalate if needed
#     scripts/stop_jarvis.sh --timeout 60 allow longer for a clean shutdown
#
# WHAT THIS SCRIPT MUST NEVER DO. `pkill python`. `pkill -f main.py`.
# `kill $(pgrep -f jarvis)`. Any of them would also match, on this machine:
# Claude Code, the hermes agent, LM Studio's runtime, and every pytest run
# in flight. A pattern that feels specific is not identity, and the blast
# radius only becomes visible after the fact.
#
# Instead every candidate PID must satisfy all three of: its argv[0]
# resolves to this repo's venv interpreter, its argv[1] is main.py, and its
# cwd is this repository. See jarvis_pid_matches in scripts/_common.sh.
#
# The identity check is re-run immediately before EVERY signal, because a
# process that exits between the check and the kill frees its PID for
# somebody else.

source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

TIMEOUT=30

while (($#)); do
  case "$1" in
    --timeout) shift; need_arg "--timeout" "${1:-}"; TIMEOUT="$1" ;;
    -h|--help) sed -n '3,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout must be a whole number of seconds, got '$TIMEOUT'"

# signal_if_ours SIGNAL PID — re-verify, then signal. Never signal blind.
signal_if_ours() {
  local sig="$1" pid="$2"
  if ! jarvis_pid_matches "$pid"; then
    return 1
  fi
  kill "-$sig" "$pid" 2>/dev/null || return 1
  return 0
}

step "Finding JARVIS"
info "repository: $JARVIS_ROOT"

TARGETS=()
if RECORDED="$(jarvis_pid_from_file)"; then
  TARGETS+=("$RECORDED")
  ok "pid file names $RECORDED, and it is still ours"
elif [[ -e "$JARVIS_PID_FILE" ]]; then
  # Not an error: the instance may have been hand-launched, or the file may
  # be left over from a process that died. Either way the file is now a
  # claim we have disproved, so it is ignored rather than acted on.
  warn "pid file $JARVIS_PID_FILE is stale — the pid in it is not a JARVIS server for this repo"
fi

# Hand-launched instances have no pid file at all, so scan regardless and
# merge. This is what retires a process started before these scripts existed.
while read -r pid; do
  [[ -n "$pid" ]] || continue
  [[ " ${TARGETS[*]} " == *" $pid "* ]] || TARGETS+=("$pid")
done < <(jarvis_pids)

if ((${#TARGETS[@]} == 0)); then
  ok "nothing to stop — no JARVIS server is running for this repository"
  [[ -e "$JARVIS_PID_FILE" ]] && { rm -f "$JARVIS_PID_FILE"; info "removed stale pid file"; }
  exit 0
fi

for pid in "${TARGETS[@]}"; do
  info "pid $pid  $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
done

# ── Graceful first ────────────────────────────────────────────────────────
# SIGTERM reaches uvicorn, which runs main.py's lifespan shutdown: the
# scheduler stops, LLM calls abort on _shutdown_event, and SQLite closes its
# WAL cleanly. SIGKILL skips all of that.
step "Stopping (SIGTERM, up to ${TIMEOUT}s)"
for pid in "${TARGETS[@]}"; do
  if signal_if_ours TERM "$pid"; then
    ok "SIGTERM -> $pid"
  else
    warn "pid $pid vanished or is no longer ours — not signalling"
  fi
done

REMAINING=()
for pid in "${TARGETS[@]}"; do
  for _ in $(seq 1 "$TIMEOUT"); do
    jarvis_pid_matches "$pid" || break
    sleep 1
  done
  if jarvis_pid_matches "$pid"; then
    REMAINING+=("$pid")
  else
    ok "pid $pid stopped"
  fi
done

# ── Escalate only if it is still, verifiably, ours ────────────────────────
if ((${#REMAINING[@]})); then
  step "Escalating (SIGKILL)"
  warn "these did not exit within ${TIMEOUT}s: ${REMAINING[*]}"
  for pid in "${REMAINING[@]}"; do
    if signal_if_ours KILL "$pid"; then
      ok "SIGKILL -> $pid"
    else
      warn "pid $pid is no longer ours — not signalling"
    fi
  done
  sleep 2
  for pid in "${REMAINING[@]}"; do
    jarvis_pid_matches "$pid" && die "pid $pid survived SIGKILL — investigate before retrying"
  done
fi

if [[ -e "$JARVIS_PID_FILE" ]]; then
  require_within_repo "$JARVIS_PID_FILE"
  rm -f "$JARVIS_PID_FILE"
fi

step "Stopped"
ok "no JARVIS server is running for $JARVIS_ROOT"
