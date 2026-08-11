"""Global trading kill switch — a system-wide control independent of any
per-user preference. When disabled, new live entries (execute_signals.py)
and new live signal generation (generate_signals.py's live track) are
blocked. Position management's hard stop-loss/take-profit enforcement is
NOT gated by this switch — an existing position must always stay protected,
mirroring the same philosophy already used for the paper Auto Trading toggle.
"""
from app.database import SystemState, get_db, now_iso

GLOBAL_STATE_ID = "global"


def get_kill_switch_state() -> dict:
    with get_db() as db:
        row = db.query(SystemState).filter(SystemState.id == GLOBAL_STATE_ID).first()
        if not row:
            row = SystemState(id=GLOBAL_STATE_ID, live_trading_enabled=True)
            db.add(row)
            db.flush()
        return {
            "live_trading_enabled": bool(row.live_trading_enabled),
            "paused_reason": row.paused_reason,
            "paused_at": row.paused_at,
            "updated_at": row.updated_at,
        }


def is_live_trading_enabled() -> bool:
    try:
        return get_kill_switch_state()["live_trading_enabled"]
    except Exception:
        # Fail closed on a broken read — never silently allow trading because
        # a state lookup errored.
        return False


def set_live_trading_enabled(enabled: bool, reason: str | None = None) -> dict:
    now = now_iso()
    with get_db() as db:
        row = db.query(SystemState).filter(SystemState.id == GLOBAL_STATE_ID).first()
        if not row:
            row = SystemState(id=GLOBAL_STATE_ID)
            db.add(row)
        row.live_trading_enabled = bool(enabled)
        row.paused_reason = None if enabled else (reason or "Manually paused")
        row.paused_at = None if enabled else now
        row.updated_at = now
    state = get_kill_switch_state()
    if not enabled:
        try:
            from lib.alert_engine import raise_alert
            raise_alert(
                source="kill_switch", severity="CRITICAL",
                title="Live trading paused", detail=state["paused_reason"] or "",
                dedup_key=f"kill_switch_pause_{state['paused_at']}", cooldown_minutes=1,
            )
        except Exception:
            pass
    return state
