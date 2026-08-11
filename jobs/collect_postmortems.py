"""Sweep terminally failed/cancelled signals into signal_postmortems — the
failure memory lib/postmortem.py classifies and lib/signal_scorer.py learns
from. Deterministic classification only; runs on a schedule and is cheap
(pure DB reads/writes, no external calls, no LLM)."""
import logging
from datetime import datetime, timedelta, timezone

from app.database import SignalEvaluation, SignalPostmortem, TradingSignal, get_db
from lib.postmortem import classify

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = 48
BATCH_LIMIT = 500


def run():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()

    regime_label = None
    try:
        from lib.market_regime import get_regime
        regime_label = get_regime().get("label")
    except Exception:
        pass

    with get_db() as db:
        existing = {
            row[0] for row in db.query(SignalPostmortem.signal_id).all()
        }
        # Terminal statuses + evaluated failures. Superseded is deliberately
        # excluded: it's the newest-wins design working, not a failure.
        candidates = (
            db.query(TradingSignal)
            .filter(
                TradingSignal.updated_date >= cutoff,
                TradingSignal.status.in_(["Rejected", "Expired", "Closed"]),
            )
            .limit(BATCH_LIMIT)
            .all()
        )
        sig_dicts = [{
            "id": s.id, "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
            "direction": s.direction, "timeframe": s.timeframe,
            "setup_type": s.setup_type, "signal_source": s.signal_source,
            "composite_score": s.composite_score, "status": s.status,
            "entry_price": s.entry_price, "target_price": s.target_price,
            "stop_loss": s.stop_loss, "paper_mode": s.paper_mode,
            "notes": s.notes, "generated_at": s.generated_at,
        } for s in candidates if s.id not in existing]

        eval_by_id = {}
        if sig_dicts:
            for ev in db.query(SignalEvaluation).filter(
                SignalEvaluation.signal_id.in_([s["id"] for s in sig_dicts])
            ).all():
                eval_by_id[ev.signal_id] = {
                    "outcome": ev.outcome, "mae_pct": ev.mae_pct,
                    "mfe_pct": ev.mfe_pct, "data_issue": ev.data_issue,
                }

        saved = skipped = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for s in sig_dicts:
            result = classify(s, eval_by_id.get(s["id"]))
            if result is None:
                skipped += 1
                continue
            reason_code, detail = result
            db.add(SignalPostmortem(
                signal_id=s["id"], symbol=s["asset_symbol"], asset_class=s["asset_class"],
                direction=s["direction"], timeframe=s["timeframe"],
                setup_type=s["setup_type"], signal_source=s["signal_source"],
                composite_score=s["composite_score"], terminal_status=s["status"],
                reason_code=reason_code, reason_detail=detail,
                regime_label=regime_label, generated_at=s["generated_at"],
                collected_at=now_iso,
            ))
            saved += 1

    logger.info(f"[Postmortem] {saved} failure(s) recorded, {skipped} terminal signals not failure-classified")
