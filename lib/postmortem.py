"""
Signal postmortems — the failure memory. Classifies every terminally
failed/cancelled signal into a deterministic reason taxonomy, aggregates
the counts, and feeds a bounded penalty back into new-signal scoring so
recurring failure modes actually cost future signals points.

Reason taxonomy (deterministic — classification is rules over recorded
facts, never an LLM judgment):

  STOP_HIT              evaluation shows the stop was struck first
  EXPIRED_UNFILLED_ORDER  order submitted, entry never filled (true entry-
                          placement failure)
  EXPIRED_UNEXECUTED      expired without ever reaching execution — gating,
                          slot limits, or overproduction, not entry placement
  AMBIGUOUS_BAR         target and stop struck within one bar (untradeable
                        volatility for the chosen timeframe)
  DATA_ERROR            evaluation flagged INVALID_DATA (symbol/exchange
                        mapping problems, bad entry vs first bar)
  REJECTED_BY_USER      explicitly denied (dashboard or Telegram)
  NOT_TRADABLE          routed to paper because the venue doesn't list it
  DEGENERATE_LEVELS     entry/target/stop spacing below 0.1% — levels that
                        cannot survive spread+slippage (the "1.00/1.00"
                        class of signal)
  EXPIRED_OTHER         expired for reasons not distinguishable above

The feedback loop (get_failure_adjustment) is deliberately modest and
transparent: it activates only at MIN_SAMPLE failures per (symbol,
setup_type) bucket, caps at MAX_PENALTY points, and returns the reason
breakdown alongside the number so a penalized signal can show WHY.
Successes already flow through the existing win-rate calibration in
lib/signal_scorer — this is the failure-reason half of that loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MIN_SAMPLE = 5
MAX_PENALTY = 12.0
LOOKBACK_DAYS = 45
DEGENERATE_SPACING_PCT = 0.1

REASON_CODES = (
    "STOP_HIT", "EXPIRED_UNFILLED_ORDER", "EXPIRED_UNEXECUTED", "AMBIGUOUS_BAR", "DATA_ERROR",
    "REJECTED_BY_USER", "NOT_TRADABLE", "DEGENERATE_LEVELS", "EXPIRED_OTHER",
)


def classify(signal: dict, evaluation: dict | None) -> tuple[str, str] | None:
    """(reason_code, detail) for a terminal signal, or None when the signal
    isn't in a failure state this taxonomy covers (wins return None — they
    are the learning engine's other half)."""
    status = (signal.get("status") or "").strip()
    outcome = (evaluation or {}).get("outcome")

    entry = float(signal.get("entry_price") or 0)
    target = float(signal.get("target_price") or 0)
    stop = float(signal.get("stop_loss") or 0)
    if entry > 0 and target > 0 and stop > 0:
        spacing = min(abs(entry - target), abs(entry - stop)) / entry * 100
        if spacing < DEGENERATE_SPACING_PCT:
            return "DEGENERATE_LEVELS", (
                f"level spacing {spacing:.3f}% of price — below the "
                f"{DEGENERATE_SPACING_PCT}% floor that spread+slippage consumes"
            )

    if outcome == "STOP_HIT":
        mae = (evaluation or {}).get("mae_pct")
        return "STOP_HIT", f"stop struck (MAE {mae}%)" if mae is not None else "stop struck"
    if outcome == "AMBIGUOUS":
        return "AMBIGUOUS_BAR", "target and stop struck within a single bar"
    if outcome == "INVALID_DATA":
        return "DATA_ERROR", (evaluation or {}).get("data_issue") or "evaluation flagged invalid data"

    if status == "Rejected":
        return "REJECTED_BY_USER", "explicitly denied via dashboard or Telegram"
    if status == "Expired":
        # Split by whether a broker order ever existed — measured on real data
        # (226/226 expired signals in one window had NO order): the dominant
        # mode is signals never submitted at all (gating/slots/overproduction),
        # which is a different problem from a submitted limit that never
        # filled (true entry placement).
        if signal.get("alpaca_order_id"):
            return "EXPIRED_UNFILLED_ORDER", "order submitted but the entry never filled before expiry"
        if outcome in (None, "OPEN", "EXPIRED"):
            return "EXPIRED_UNEXECUTED", "expired without ever reaching execution (gate/slots/overproduction)"
        return "EXPIRED_OTHER", f"expired with evaluation outcome {outcome}"
    if signal.get("paper_mode") and (signal.get("notes") or "").startswith("not_tradable"):
        return "NOT_TRADABLE", signal.get("notes") or "venue does not list this symbol"
    return None


def aggregate_reasons(postmortems: list[dict]) -> dict:
    """Counts by reason_code plus per-bucket dominant reasons."""
    by_reason: dict[str, int] = {}
    by_bucket: dict[tuple, dict[str, int]] = {}
    for pm in postmortems:
        code = pm.get("reason_code") or "EXPIRED_OTHER"
        by_reason[code] = by_reason.get(code, 0) + 1
        bucket = (pm.get("symbol"), pm.get("setup_type"))
        by_bucket.setdefault(bucket, {})
        by_bucket[bucket][code] = by_bucket[bucket].get(code, 0) + 1
    return {"by_reason": by_reason, "by_bucket": by_bucket, "total": len(postmortems)}


def get_failure_adjustment(symbol: str, setup_type: str | None,
                           postmortems: list[dict]) -> dict | None:
    """Scoring penalty for a NEW signal in a (symbol, setup_type) bucket with
    a recurring failure history. None when the bucket has fewer than
    MIN_SAMPLE failures in the lookback — sparse history is not evidence.

    Penalty scales with failure count (2 points per failure past the floor,
    capped at MAX_PENALTY) and the dominant reason is named so the penalty
    is explainable on the signal's score breakdown."""
    relevant = [
        pm for pm in postmortems
        if pm.get("symbol") == symbol
        and (setup_type is None or pm.get("setup_type") == setup_type)
    ]
    if len(relevant) < MIN_SAMPLE:
        return None
    counts: dict[str, int] = {}
    for pm in relevant:
        code = pm.get("reason_code") or "EXPIRED_OTHER"
        counts[code] = counts.get(code, 0) + 1
    dominant = max(counts, key=counts.get)
    penalty = min(MAX_PENALTY, (len(relevant) - MIN_SAMPLE + 1) * 2.0)
    return {
        "penalty": round(penalty, 1),
        "failures": len(relevant),
        "dominant_reason": dominant,
        "reasons": counts,
        "note": (
            f"{len(relevant)} failed signals for {symbol}"
            + (f"/{setup_type}" if setup_type else "")
            + f" in the last {LOOKBACK_DAYS}d, mostly {dominant}"
        ),
    }


def load_recent_postmortems(days: int = LOOKBACK_DAYS) -> list[dict]:
    """Recent postmortems as plain dicts (session-safe)."""
    from app.database import SignalPostmortem, get_db
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as db:
        rows = db.query(SignalPostmortem).filter(SignalPostmortem.collected_at >= cutoff).all()
        return [{
            "signal_id": r.signal_id, "symbol": r.symbol, "asset_class": r.asset_class,
            "direction": r.direction, "setup_type": r.setup_type,
            "signal_source": r.signal_source, "terminal_status": r.terminal_status,
            "reason_code": r.reason_code, "reason_detail": r.reason_detail,
            "regime_label": r.regime_label, "collected_at": r.collected_at,
        } for r in rows]
