"""Evaluate generated signals against bars that occurred after generation."""

import logging
from datetime import datetime, timedelta, timezone

from app.database import SignalEvaluation, TradingSignal, get_db
from lib.signal_evaluation import evaluate_signal_path

logger = logging.getLogger(__name__)


def _signal_dict(signal: TradingSignal) -> dict:
    return {
        "id": signal.id, "asset_symbol": signal.asset_symbol,
        "asset_class": signal.asset_class, "direction": signal.direction,
        "timeframe": signal.timeframe, "entry_price": signal.entry_price,
        "target_price": signal.target_price, "stop_loss": signal.stop_loss,
        "generated_at": signal.generated_at, "expires_at": getattr(signal, "expires_at", None),
        "signal_version": getattr(signal, "signal_version", None) or "legacy",
    }


def run():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with get_db() as db:
        signals = [
            _signal_dict(row) for row in db.query(TradingSignal).filter(
                TradingSignal.generated_at.isnot(None), TradingSignal.generated_at >= cutoff,
                TradingSignal.entry_price.isnot(None), TradingSignal.target_price.isnot(None),
                TradingSignal.stop_loss.isnot(None),
            ).order_by(TradingSignal.generated_at.desc()).limit(500).all()
        ]

    evaluated = skipped = 0
    now = datetime.now(timezone.utc)
    for signal in signals:
        try:
            timeframe = signal.get("timeframe") or "4H"
            generated = datetime.fromisoformat(signal["generated_at"].replace("Z", "+00:00"))
            generated = generated.replace(tzinfo=timezone.utc) if generated.tzinfo is None else generated.astimezone(timezone.utc)
            symbol = signal["asset_symbol"]
            try:
                from lib.crypto_market_data import is_crypto_symbol, normalize_crypto_symbol
                if is_crypto_symbol(symbol):
                    symbol = normalize_crypto_symbol(symbol)
            except Exception:
                pass
            from lib.ohlcv_cache import _get_cached_bars
            frame = _get_cached_bars(symbol, timeframe, generated, now)
            result = evaluate_signal_path(signal, frame)
            if not result:
                skipped += 1
                continue
            with get_db() as db:
                row = db.query(SignalEvaluation).filter(SignalEvaluation.signal_id == signal["id"]).first()
                if not row:
                    row = SignalEvaluation(signal_id=signal["id"])
                    db.add(row)
                row.symbol = signal["asset_symbol"]
                row.asset_class = signal.get("asset_class")
                row.direction = signal.get("direction")
                row.timeframe = timeframe
                row.signal_version = signal.get("signal_version")
                row.generated_at = signal.get("generated_at")
                row.entry_price = signal.get("entry_price")
                row.target_price = signal.get("target_price")
                row.stop_loss = signal.get("stop_loss")
                for key, value in result.items():
                    setattr(row, key, value)
                row.evaluated_at = now.isoformat()
            evaluated += 1
        except Exception as exc:
            logger.debug("[Evaluation] %s skipped: %s", signal.get("asset_symbol"), exc)
            skipped += 1
    logger.info("[Evaluation] Updated %d signals; %d had no usable forward bars", evaluated, skipped)
    return {"evaluated": evaluated, "skipped": skipped}
