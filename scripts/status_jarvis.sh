#!/usr/bin/env bash
#
# Report whether JARVIS is running, and what it is running as.
#
#     scripts/status_jarvis.sh
#     scripts/status_jarvis.sh --quiet    exit code only
#
# READ-ONLY. It starts nothing, stops nothing and writes nothing — a status
# command that has side effects cannot be used to investigate a problem.
#
# Exit codes:   0 running and healthy
#               1 not running
#               2 running but unhealthy (process up, API not answering)
#
# The scheduler line is not decoration. "Is it running?" and "is it TRADING?"
# are different questions, and the second one is the expensive one to get
# wrong, so it is answered from the process's own environment rather than
# from what someone intended to launch.

source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

QUIET=0
[[ "${1:-}" == "--quiet" || "${1:-}" == "-q" ]] && QUIET=1
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && {
  sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

say()      { ((QUIET)) || step "$@"; }
say_ok()   { ((QUIET)) || ok "$@"; }
say_info() { ((QUIET)) || info "$@"; }
say_warn() { ((QUIET)) || warn "$@"; }

say "JARVIS"
say_info "repository: $JARVIS_ROOT"

mapfile -t PIDS < <(jarvis_pids)

if ((${#PIDS[@]} == 0)); then
  say_warn "not running"
  if [[ -e "$JARVIS_PID_FILE" ]]; then
    say_info "a stale pid file remains at $JARVIS_PID_FILE (scripts/stop_jarvis.sh clears it)"
  fi
  exit 1
fi

for pid in "${PIDS[@]}"; do
  say_ok "pid $pid"
  if ((!QUIET)); then
    # Elapsed time and RSS come from ps, which is read-only here.
    ps -o pid=,etime=,rss=,stat= -p "$pid" 2>/dev/null \
      | awk '{printf "      up %s, rss %.0f MB, state %s\n", $2, $3/1024, $4}'
    info "cmd $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"

    # From the PROCESS's environment, not this shell's: a server started
    # before someone edited .env is running the old answer, and that is the
    # answer that matters.
    sched="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^JARVIS_DISABLE_SCHEDULER=' | cut -d= -f2- || true)"
    mode="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^JARVIS_PLATFORM_MODE=' | cut -d= -f2- || true)"
    if [[ "$sched" == "1" ]]; then
      ok "scheduler DISABLED — no background jobs, no signal execution"
    else
      warn "scheduler ENABLED — background jobs are firing, signals execute"
    fi
    info "platform mode: ${mode:-unset (defaults to VIRTUAL_ONLY)}"
  fi
done

if ((${#PIDS[@]} > 1)); then
  say_warn "MORE THAN ONE instance is running against this repository — expect 'database is locked'"
fi

# ── Is the API actually answering? ────────────────────────────────────────
PORT_NUM="$(jarvis_port)"
HEALTH="http://127.0.0.1:$PORT_NUM/api/health"
if curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1; then
  say_ok "API healthy at $HEALTH"
else
  say_warn "process is up but $HEALTH does not answer"
  exit 2
fi

# The LLM endpoint is reported because it is resolved at runtime and is not
# knowable from any config file — see lib/lmstudio.py.
if ((!QUIET)) && command -v python3 >/dev/null 2>&1; then
  llm="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT_NUM/api/llm/health" 2>/dev/null || true)"
  if [[ -n "$llm" ]]; then
    # Quoted heredoc, not python3 -c: an inline string would be parsed by
    # the shell first, which is the exact hazard scripts/_common.sh exists
    # to remove. The payload arrives by environment rather than by pipe
    # because `python3 -` reads its PROGRAM from stdin — piping data in as
    # well leaves the program reading an already-consumed stream, which
    # fails silently and prints nothing.
    JARVIS_LLM_JSON="$llm" python3 - <<'PY' || true
import json, os
try:
    h = json.loads(os.environ.get("JARVIS_LLM_JSON") or "")
except Exception:
    raise SystemExit(0)
mark = "ok " if h.get("ok") else "!! "
chosen = h.get("url")
print("    %s  LLM %s at %s [%s]" % (mark, h.get("status"), chosen, h.get("provenance")))
for t in h.get("tried") or []:
    if t.get("url") != chosen:
        print("         also tried %s [%s] -> %s"
              % (t.get("url"), t.get("provenance"), t.get("status")))
PY
  fi
fi

exit 0
