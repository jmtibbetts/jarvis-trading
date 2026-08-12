"""Forward-only signal evaluation helpers with no pre-signal bar access."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from lib import trade_side


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def evaluate_signal_path(signal: dict, frame: pd.DataFrame) -> dict | None:
    """Evaluate only bars strictly after generated_at to avoid look-ahead bias."""
    generated_at = _parse_time(signal.get("generated_at"))
    if generated_at is None or frame is None or frame.empty:
        return None
    data = frame.copy()
    data.columns = [str(column).lower() for column in data.columns]
    data.index = pd.to_datetime(data.index, utc=True)
    data = data[data.index > pd.Timestamp(generated_at)].sort_index()
    if data.empty:
        return None

    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    stop = float(signal.get("stop_loss") or 0)
    if entry <= 0 or target <= 0 or stop <= 0:
        return None
    short = trade_side.is_short(signal.get("direction"))
    expires_at = _parse_time(signal.get("expires_at"))

    first_bar = data.iloc[0]
    first_price = float(first_bar.get("open") or first_bar.get("close") or 0)
    price_ratio = first_price / entry if entry else 0
    if first_price <= 0 or price_ratio < 0.2 or price_ratio > 5.0:
        first_at = data.index[0].isoformat()
        return {
            "first_bar_at": first_at,
            "last_bar_at": first_at,
            "bars_observed": 0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "outcome": "INVALID_DATA",
            "target_hit_at": None,
            "stop_hit_at": None,
            "data_issue": (
                f"Entry price {entry:g} does not match first forward bar "
                f"{first_price:g} (ratio {price_ratio:.2f}); check symbol and exchange mapping."
            ),
        }

    observed = []
    outcome = "OPEN"
    target_hit_at = stop_hit_at = None
    for timestamp, bar in data.iterrows():
        if expires_at and timestamp.to_pydatetime() > expires_at:
            outcome = "EXPIRED"
            break
        observed.append((timestamp, bar))
        if short:
            target_hit = float(bar.get("low", 0)) <= target
            stop_hit = float(bar.get("high", 0)) >= stop
        else:
            target_hit = float(bar.get("high", 0)) >= target
            stop_hit = float(bar.get("low", 0)) <= stop
        if target_hit and stop_hit:
            outcome = "AMBIGUOUS"
            target_hit_at = stop_hit_at = timestamp.isoformat()
            break
        if target_hit:
            outcome = "TARGET_HIT"
            target_hit_at = timestamp.isoformat()
            break
        if stop_hit:
            outcome = "STOP_HIT"
            stop_hit_at = timestamp.isoformat()
            break

    if not observed:
        return None
    observed_frame = pd.DataFrame([bar for _, bar in observed], index=[timestamp for timestamp, _ in observed])
    if short:
        mfe = (entry - float(observed_frame["low"].min())) / entry * 100
        mae = (entry - float(observed_frame["high"].max())) / entry * 100
    else:
        mfe = (float(observed_frame["high"].max()) - entry) / entry * 100
        mae = (float(observed_frame["low"].min()) - entry) / entry * 100

    return {
        "first_bar_at": observed[0][0].isoformat(),
        "last_bar_at": observed[-1][0].isoformat(),
        "bars_observed": len(observed),
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "outcome": outcome,
        "target_hit_at": target_hit_at,
        "stop_hit_at": stop_hit_at,
        "data_issue": None,
    }


def summarize_evaluations(rows: list[dict]) -> dict:
    valid = [row for row in rows if row.get("outcome") != "INVALID_DATA"]
    decided = [row for row in valid if row.get("outcome") in {"TARGET_HIT", "STOP_HIT"}]
    wins = sum(row.get("outcome") == "TARGET_HIT" for row in decided)
    losses = sum(row.get("outcome") == "STOP_HIT" for row in decided)
    mfe = [float(row.get("mfe_pct") or 0) for row in valid]
    mae = [float(row.get("mae_pct") or 0) for row in valid]
    expectancy = (
        sum(float(row.get("mfe_pct") or 0) if row.get("outcome") == "TARGET_HIT"
            else float(row.get("mae_pct") or 0) for row in decided)
        / len(decided) if decided else 0.0
    )
    gross_win = sum(max(0, float(row.get("mfe_pct") or 0)) for row in decided if row.get("outcome") == "TARGET_HIT")
    gross_loss = abs(sum(min(0, float(row.get("mae_pct") or 0)) for row in decided if row.get("outcome") == "STOP_HIT"))
    return {
        "total": len(rows), "decided": len(decided), "wins": wins, "losses": losses,
        "hit_rate": round(wins / len(decided) * 100, 2) if decided else 0.0,
        "expectancy_pct": round(expectancy, 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "avg_mfe_pct": round(sum(mfe) / len(mfe), 4) if mfe else 0.0,
        "avg_mae_pct": round(sum(mae) / len(mae), 4) if mae else 0.0,
        "open": sum(row.get("outcome") == "OPEN" for row in rows),
        "expired": sum(row.get("outcome") == "EXPIRED" for row in rows),
        "ambiguous": sum(row.get("outcome") == "AMBIGUOUS" for row in rows),
        "invalid_data": sum(row.get("outcome") == "INVALID_DATA" for row in rows),
    }
