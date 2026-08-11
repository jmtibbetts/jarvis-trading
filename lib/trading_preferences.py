"""Per-user trading-horizon preferences shared by generation and alert routing."""

from app.database import DEFAULT_USER_ID, UserPreference, get_db, now_iso

VALID_TRADE_MODES = {"scalp", "longer", "all"}
SCALP_TIMEFRAMES = {"1m", "3m", "5m", "15m"}
LONGER_TIMEFRAMES = {"30m", "1H", "2H", "4H", "1D"}


def normalize_trade_mode(value: str | None) -> str:
    mode = str(value or "all").strip().lower()
    return mode if mode in VALID_TRADE_MODES else "all"


def horizon_for_timeframe(timeframe: str | None) -> str:
    if timeframe in SCALP_TIMEFRAMES:
        return "scalp"
    if timeframe in LONGER_TIMEFRAMES:
        return "longer"
    return "all"


def timeframe_allowed(timeframe: str | None, mode: str | None) -> bool:
    selected = normalize_trade_mode(mode)
    return selected == "all" or horizon_for_timeframe(timeframe) == selected


def get_user_preference(user_id: str = DEFAULT_USER_ID) -> dict:
    with get_db() as db:
        row = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
        if not row:
            row = UserPreference(user_id=user_id)
            db.add(row)
            db.flush()
        return {
            "user_id": row.user_id,
            "trade_mode": normalize_trade_mode(row.trade_mode),
            "min_confidence": float(row.min_confidence or 60),
            "telegram_enabled": bool(row.telegram_enabled),
            "auto_sim_enabled": bool(row.auto_sim_enabled),
            "paper_auto_trade_enabled": bool(row.paper_auto_trade_enabled),
            "live_min_score": float(row.live_min_score if row.live_min_score is not None else 55.0),
            "live_min_rr": float(row.live_min_rr or 0.0),
            "live_min_confidence": float(row.live_min_confidence or 0.0),
        }


def set_trade_mode(mode: str, user_id: str = DEFAULT_USER_ID) -> dict:
    selected = normalize_trade_mode(mode)
    if selected != str(mode or "").strip().lower():
        raise ValueError("trade_mode must be scalp, longer, or all")
    with get_db() as db:
        row = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
        if not row:
            row = UserPreference(user_id=user_id)
            db.add(row)
        row.trade_mode = selected
        row.updated_at = now_iso()
    return get_user_preference(user_id)


def set_execution_criteria(live_min_score: float | None = None,
                           live_min_rr: float | None = None,
                           live_min_confidence: float | None = None,
                           user_id: str = DEFAULT_USER_ID) -> dict:
    """Update the live (Alpaca) auto-execution gates. Bounds keep a typo from
    silently disabling the whole book (score 0-100, rr 0-10, conf 0-100)."""
    with get_db() as db:
        row = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
        if not row:
            row = UserPreference(user_id=user_id)
            db.add(row)
        if live_min_score is not None:
            row.live_min_score = max(0.0, min(100.0, float(live_min_score)))
        if live_min_rr is not None:
            row.live_min_rr = max(0.0, min(10.0, float(live_min_rr)))
        if live_min_confidence is not None:
            row.live_min_confidence = max(0.0, min(100.0, float(live_min_confidence)))
        row.updated_at = now_iso()
    return get_user_preference(user_id)
