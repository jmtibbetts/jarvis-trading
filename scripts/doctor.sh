#!/usr/bin/env bash
#
# Report the state of this JARVIS machine. Diagnose only.
#
#     scripts/doctor.sh
#
# READ-ONLY, WITHOUT EXCEPTION. It creates no directories, installs
# nothing, starts nothing, and opens no operator database for writing. A
# diagnostic that repairs things cannot tell you what was wrong, and the one
# time a probe here opened the operator's DB without JARVIS_DB_PATH it
# destroyed a table.
#
# It also does not fail. Every check reports; the exit code is 0 unless a
# check could not be performed at all. Deciding what is acceptable is the
# operator's job, and a doctor that exits 1 on a cosmetic difference stops
# being read.
#
# WHAT IS NOT A PROBLEM, and is therefore not reported as one:
#
#   * No Intel NPU. The supported runtime is WSL2/Ubuntu 24.04, where
#     OpenVINO enumerates ['CPU'] and no NPU is passed through. CPU is the
#     required baseline and the NPU is optional — measured, it is not even
#     faster (CPU 0.121 ms vs NPU 0.376 ms), it is spare capacity. Printing
#     a warning for its absence trains the reader to ignore warnings.
#
#   * A discovered LM Studio address that differs from any config. It is
#     supposed to differ; see lib/lmstudio.py.

source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

# A failing probe must not abort the report — the rest of it is still what
# the reader came for.
set +e

note() { printf '    %-22s %s\n' "$1" "$2"; }

step "Machine"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  note "os" "${PRETTY_NAME:-unknown}"
fi
note "kernel" "$(uname -r)"
if uname -r | grep -qi microsoft; then
  note "wsl" "yes (${WSL_DISTRO_NAME:-distro name unset})"
  gw="$(awk '$2=="00000000" && $3!="00000000" {print $3; exit}' /proc/net/route 2>/dev/null)"
  if [[ -n "$gw" ]]; then
    ip=$(printf '%d.%d.%d.%d' \
      $((0x${gw:6:2})) $((0x${gw:4:2})) $((0x${gw:2:2})) $((0x${gw:0:2})))
    note "windows host" "$ip (per-boot; never stored)"
  fi
else
  note "wsl" "no"
fi
note "cpu cores" "$(nproc 2>/dev/null || echo '?')"
note "memory" "$(free -h 2>/dev/null | awk '/^Mem:/{print $2" total, "$7" available"}')"

step "Repository"
note "path" "$JARVIS_ROOT"
case "$JARVIS_ROOT" in
  /mnt/*) warn "on a Windows mount — SQLite WAL locking and small-file IO both suffer here" ;;
  *)      note "filesystem" "Linux-native (correct)" ;;
esac
if command -v git >/dev/null 2>&1; then
  note "commit" "$(git -C "$JARVIS_ROOT" log --oneline -1 2>/dev/null)"
  dirty="$(git -C "$JARVIS_ROOT" status --porcelain 2>/dev/null | wc -l)"
  note "working tree" "$( ((dirty)) && echo "$dirty file(s) modified" || echo clean )"
fi
note "disk" "$(df -h "$JARVIS_ROOT" 2>/dev/null | awk 'NR==2{print $4" free of "$2}')"

step "Python"
PY="$JARVIS_ROOT/.venv/bin/python"
if [[ -x "$PY" ]]; then
  note "interpreter" "$PY"
  note "version" "$("$PY" --version 2>&1)"
  "$PY" -m pip check >/dev/null 2>&1 \
    && note "pip check" "clean" \
    || note "pip check" "reports conflicts (run: $PY -m pip check)"
  "$PY" - <<'PY' 2>/dev/null
import importlib
for m in ("sqlalchemy", "fastapi", "uvicorn", "httpx", "apscheduler",
          "order_book", "cryptofeed"):
    try:
        importlib.import_module(m)
        print(f"    {'import ' + m:<22} ok")
    except Exception as e:
        print(f"    {'import ' + m:<22} FAILED: {type(e).__name__}: {e}")
PY
else
  warn "no virtualenv at $PY — run scripts/bootstrap_ubuntu.sh"
fi

step "Node"
node_bin="$(command -v node 2>/dev/null)"
if [[ -z "$node_bin" ]]; then
  note "node" "not installed"
elif [[ "$node_bin" == /mnt/* ]]; then
  warn "node resolves to the WINDOWS binary at $node_bin — native modules will build for the wrong platform"
else
  note "node" "$(node --version) at $node_bin"
fi

# npm is a SEPARATE resolution from node. A Linux node with a Windows npm
# still installs Windows-built native modules into the Linux tree.
npm_bin="$(command -v npm 2>/dev/null)"
if [[ -z "$npm_bin" ]]; then
  note "npm" "not installed"
elif [[ "$npm_bin" == /mnt/* ]]; then
  warn "npm resolves to the WINDOWS binary at $npm_bin — native modules will build for the wrong platform"
else
  note "npm" "$(npm --version 2>/dev/null) at $npm_bin"
fi

step "Compute"
# THE SUPPORTED COMPUTE MODEL, stated so a reader does not have to infer it
# from what is missing.
#
#   CPU          everything JARVIS calculates: TA, risk, sizing, execution
#                simulation, DEX economics, accounting, calibration,
#                expectancy, attribution, walk-forward, and OpenVINO
#                predictive inference
#   RTX 5090     LLM inference ONLY, reached through LM Studio's HTTP API
#   CUDA Python  not a runtime requirement at all
#
# An absent CUDA stack is therefore reported as a FACT, never as a warning
# or a degraded state. It is only needed for optional offline GPU training,
# and the one model ever trained that way was rejected on its own numbers.
note "deterministic compute" "CPU (authoritative baseline)"
note "LLM acceleration" "external - Windows RTX 5090 via LM Studio HTTP API"
if [[ -x "$PY" ]]; then
  if "$PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('torch') else 1)" 2>/dev/null; then
    torch_ver="$("$PY" -c "import torch; print(torch.__version__)" 2>/dev/null || echo '?')"
    if [[ "$torch_ver" == *"+cu"* ]]; then
      note "CUDA Python runtime" "installed ($torch_ver) - optional GPU training extras present"
    else
      note "CUDA Python runtime" "not installed; CPU torch $torch_ver present (optional research extras)"
    fi
  else
    note "CUDA Python runtime" "not installed (optional; JARVIS does not need it)"
  fi
fi

step "Predictive runtime"
# The whole point of this section: CPU is the baseline and must work. The
# NPU is a bonus and its absence is a fact, not a finding.
if [[ -x "$PY" ]]; then
  # Captured, then branched on. A `|| { ... }` written between a heredoc's
  # opening line and its body does not do what it looks like: the body
  # starts at the next line, so the fallback becomes heredoc content and
  # the block is never closed.
  ov_report="$("$PY" - <<'PY' 2>/dev/null
try:
    import openvino as ov
except Exception:
    raise SystemExit(1)
core = ov.Core()
devices = core.available_devices
print(f"    {'openvino':<22} {ov.__version__}")
print(f"    {'devices':<22} {devices}")
if "CPU" in devices:
    print(f"    {'cpu baseline':<22} present (required)")
else:
    print(f"    {'cpu baseline':<22} MISSING - this one IS a problem")
print(f"    {'npu':<22} "
      + ("present (optional bonus)" if "NPU" in devices
         else "absent - expected on WSL2, not a problem"))
PY
  )" || ov_report=""

  if [[ -n "$ov_report" ]]; then
    printf '%s\n' "$ov_report"
  else
    # Still says something about the NPU. A section that goes silent leaves
    # the reader unable to tell "no NPU" from "did not look", and those two
    # want different responses.
    note "openvino" "not installed (optional; the predictive layer abstains without it)"
    note "cpu baseline" "not checked - openvino absent"
    note "npu" "not checked - openvino absent; still not a problem"
  fi
fi

step "Databases"
# Sizes and integrity only, and only through a READ-ONLY connection.
# Opening the operator DB read-write from a probe is how dex_portfolios was
# destroyed once already.
# forward_evidence.db was MISSING from this list while holding 1.2G of
# pre-cutover evidence — a store the report did not mention is a store
# nobody checks. Enumerate every .db in data/ instead of naming a fixed
# few, so a new store cannot go unreported again.
shopt -s nullglob
for f in "$JARVIS_ROOT"/data/*.db; do
  db="$(basename "$f" .db)"
  size="$(du -h "$f" | cut -f1)"
  if command -v sqlite3 >/dev/null 2>&1; then
    mode="$(sqlite3 "file:$f?mode=ro" 'PRAGMA journal_mode;' 2>/dev/null)"
    # quick_check, not integrity_check: this is a health report that must
    # stay fast on a 5G cache. quick_check omits only the index-content
    # cross-check, and a full run is one command away when it matters.
    chk="$(sqlite3 "file:$f?mode=ro" 'PRAGMA quick_check(1);' 2>&1 | head -1)"
    wal=""
    [[ -f "$f-wal" ]] && wal=", wal=$(du -h "$f-wal" | cut -f1) uncheckpointed"
    if [[ "$chk" == "ok" ]]; then
      note "$db.db" "$size, journal=${mode:-unknown}, integrity=ok$wal"
    else
      warn "$db.db INTEGRITY: $chk"
    fi
  else
    note "$db.db" "$size"
  fi
done
shopt -u nullglob

step "Mode"
# WHICH MODE IS A DECISION, NOT A DEFAULT. runtime_mode falls back to
# FULL_VIRTUAL when JARVIS_RUNTIME_MODE is unset, so an unset variable
# silently permits economic mutation. Report the resolved value AND
# whether it was chosen or inherited.
rm_raw="${JARVIS_RUNTIME_MODE:-}"
rm_eff="$("$PY" -c 'from lib import runtime_mode as R; print(R.current_mode())' 2>/dev/null || echo unknown)"
if [[ -z "$rm_raw" ]]; then
  warn "JARVIS_RUNTIME_MODE is UNSET — resolved to $rm_eff by default, not by decision"
else
  note "runtime mode" "$rm_eff (explicitly set)"
fi
note "platform mode" "${JARVIS_PLATFORM_MODE:-unset}"
note "economic jobs" "$([[ "$rm_eff" == "EVIDENCE_ONLY" ]] && echo 'withheld (EVIDENCE_ONLY)' || echo 'PERMITTED')"

step "Network"
if getent hosts api.kraken.com >/dev/null 2>&1; then
  note "dns" "resolves"
else
  warn "dns cannot resolve api.kraken.com"
fi
if curl -sS -o /dev/null -m 8 -w '%{http_code}' https://api.kraken.com/0/public/Time 2>/dev/null | grep -q '^2'; then
  note "https egress" "ok"
else
  warn "https egress failed"
fi

step "Server"
mapfile -t PIDS < <(jarvis_pids)
if ((${#PIDS[@]} == 0)); then
  note "process" "not running"
else
  for pid in "${PIDS[@]}"; do
    sched="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^JARVIS_DISABLE_SCHEDULER=' | cut -d= -f2-)"
    note "process" "pid $pid, scheduler $( [[ "$sched" == 1 ]] && echo DISABLED || echo ENABLED )"
  done
fi
PORT_NUM="$(jarvis_port)"
curl -fsS --max-time 3 "http://127.0.0.1:$PORT_NUM/api/health" >/dev/null 2>&1 \
  && note "api" "healthy on :$PORT_NUM" \
  || note "api" "not answering on :$PORT_NUM"

step "LLM endpoint"
# Resolved live rather than read from config, because it is not IN any
# config — that is the design. Uses the same resolver the server uses.
if [[ -x "$PY" ]]; then
  cd "$JARVIS_ROOT" && "$PY" - <<'PY' 2>/dev/null || note "resolver" "could not run"
from lib import lmstudio as L
res = L.resolve_endpoint()
print(f"    {'selected':<22} {res.url or '(none)'}  [{res.provenance}]  {res.status}")
for c in res.candidates:
    print(f"    {'  tried':<22} {c.url}  [{c.provenance}]  {c.status}"
          + (f"  models={len(c.models)}" if c.models else "")
          + (f"  {c.detail}" if c.detail else ""))
PY
fi

step "Done"
info "This report changed nothing. Acting on it is the operator's call."
exit 0
