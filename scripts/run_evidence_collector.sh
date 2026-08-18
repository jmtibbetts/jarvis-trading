#!/usr/bin/env bash
# Launcher for the EVIDENCE_ONLY collector.
#
# Kept as a script rather than inlined into the unit so the environment is
# inspectable and the service file carries NO secrets — provider keys come
# from the repo .env exactly as every other JARVIS process loads them.
set -euo pipefail

REPO="/home/nullcode/jarvis-trading"
cd "$REPO"

# EVIDENCE_ONLY is the whole point: economic mutation raises rather than
# returning a flag, so a bug cannot quietly open a position.
export JARVIS_RUNTIME_MODE="EVIDENCE_ONLY"

# The SHADOW RESEARCH database. Never the operator DB.
export JARVIS_DB_PATH="$REPO/data/forward_evidence.db"
export JARVIS_EVENTS_DB_PATH="$REPO/data/events.db"

# The autonomous economic scheduler stays OFF. Read-only market data and the
# evidence runtime are deliberately NOT gated on it.
export JARVIS_DISABLE_SCHEDULER="1"
unset JARVIS_DISABLE_MARKET_DATA || true
unset JARVIS_DISABLE_EVIDENCE_RUNTIME || true

exec "$REPO/.venv/bin/python" -u -m jobs.evidence_collector
