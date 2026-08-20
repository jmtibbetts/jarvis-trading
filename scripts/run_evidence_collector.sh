#!/usr/bin/env bash
# Launcher for the EVIDENCE_ONLY collector.
#
# Kept as a script rather than inlined into the unit so the environment is
# inspectable and the service file carries NO secrets — provider keys come
# from the repo .env exactly as every other JARVIS process loads them.
set -euo pipefail

REPO="/home/nullcode/jarvis-trading"
cd "$REPO"

# ONE CHECKED PATH FOR BOTH LAUNCHERS. The same helper start_jarvis.sh
# uses, with the other posture -- so "which modes am I running" has a
# single implementation and cannot drift between the two entry points.
#
# EVIDENCE_ONLY is the whole point here: economic mutation raises rather
# than returning a flag, so a bug cannot quietly open a position. An
# inherited FULL_VIRTUAL is a conflict and is fatal, not something to
# silently overwrite.
PYTHON="$REPO/.venv/bin/python"
# shellcheck source=scripts/_common.sh
source "$REPO/scripts/_common.sh"
jarvis_establish_modes VIRTUAL_ONLY EVIDENCE_ONLY

# The SHADOW RESEARCH database. Never the operator DB.
export JARVIS_DB_PATH="$REPO/data/forward_evidence.db"
export JARVIS_EVENTS_DB_PATH="$REPO/data/events.db"

# The autonomous economic scheduler stays OFF. Read-only market data and the
# evidence runtime are deliberately NOT gated on it.
export JARVIS_DISABLE_SCHEDULER="1"
unset JARVIS_DISABLE_MARKET_DATA || true
unset JARVIS_DISABLE_EVIDENCE_RUNTIME || true

exec "$REPO/.venv/bin/python" -u -m jobs.evidence_collector
