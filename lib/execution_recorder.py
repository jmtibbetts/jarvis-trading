"""Record what the book looked like when an order was sent, and what it cost.

Phase 4 failed for lack of data, not lack of model. This is the fix, and it
has to start now because no amount of later cleverness recovers a
measurement nobody took.

The state today: 4 signals out of 39,821 carry a measured slippage, and
nothing persists the order book or the tape at all. An execution model
trained on 4 samples is not a model.

The thing that makes this hard to bolt on afterwards is that the useful
features are the ones that existed AT SUBMIT TIME. Spread, depth and
imbalance a second after the fill are contaminated by the fill itself — the
order moved the book it is being measured against. So the snapshot is taken
before the order goes out and stored immediately, whether or not the order
ever fills. An unfilled order is a real observation about liquidity, and
discarding those would bias the dataset toward moments when trading was
easy.

Realised slippage is computed on resolution, signed so that positive always
means WORSE than intended regardless of direction — otherwise longs and
shorts average each other out into a comfortable zero.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Order lifecycle. PENDING rows are not failures; they are open questions,
# and how many of them there are is itself a fill-probability signal.
PENDING = "PENDING"
FILLED = "FILLED"
PARTIAL = "PARTIAL"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def capture_microstructure(symbol: str, venue: str = "alpaca") -> dict:
    """Book and tape as they stand right now.

    Every field is optional and absence is recorded as absence. A missing
    spread must not become 0.0 — that would read as a perfectly tight
    market, which is the opposite of what an unavailable quote implies.
    """
    snap: dict = {"venue": venue, "captured_at": _now()}
    try:
        from lib.venues import measured_spread_pct
        spread, src = measured_spread_pct(symbol, venue)
        if spread is not None:
            snap["spread_pct"] = round(float(spread), 8)
            snap["spread_source"] = src
    except Exception as e:
        logger.debug(f"[ExecRec] spread unavailable for {symbol}: {e}")

    try:
        from lib.orderbook_stream import get_book_snapshot
        book = get_book_snapshot(symbol)
        if book:
            bid_sz, ask_sz = _f(book.get("bid_size")), _f(book.get("ask_size"))
            snap["bid"] = _f(book.get("bid"))
            snap["ask"] = _f(book.get("ask"))
            snap["bid_size"] = bid_sz
            snap["ask_size"] = ask_sz
            if bid_sz is not None and ask_sz is not None and (bid_sz + ask_sz) > 0:
                # -1 all offer, +1 all bid.
                snap["book_imbalance"] = round((bid_sz - ask_sz) / (bid_sz + ask_sz), 6)
    except Exception as e:
        logger.debug(f"[ExecRec] book unavailable for {symbol}: {e}")

    try:
        from lib.kraken_stream import get_tape_stats
        tape = get_tape_stats(symbol)
        if tape:
            snap["tape_buy_volume"] = _f(tape.get("buy_volume"))
            snap["tape_sell_volume"] = _f(tape.get("sell_volume"))
            snap["tape_trade_count"] = _f(tape.get("trade_count"))
            b, s = snap.get("tape_buy_volume"), snap.get("tape_sell_volume")
            if b is not None and s is not None and (b + s) > 0:
                snap["tape_imbalance"] = round((b - s) / (b + s), 6)
    except Exception as e:
        logger.debug(f"[ExecRec] tape unavailable for {symbol}: {e}")
    return snap


def record_intent(*, signal_id: str | None, symbol: str, side: str,
                  order_type: str, intended_price: float, qty: float,
                  venue: str = "alpaca", stop_loss: float | None = None,
                  broker_order_id: str | None = None,
                  asset_class: str | None = None,
                  approved_notional: float | None = None) -> str | None:
    """Store the order and the market it was sent into. Returns a row id.

    Called BEFORE submission. Nothing here may raise into the execution
    path: a recorder that can block a trade is worse than no recorder.
    """
    try:
        import json

        from app.database import ExecutionSample, get_db, new_id
        snap = capture_microstructure(symbol, venue)
        row_id = new_id()
        with get_db() as db:
            db.add(ExecutionSample(
                id=row_id, signal_id=signal_id, symbol=symbol,
                asset_class=asset_class, venue=venue, side=side,
                order_type=order_type, intended_price=_f(intended_price),
                qty=_f(qty), stop_loss=_f(stop_loss),
                # Immutable at birth (P0.12): initial stop as approved,
                # and the risk/notional that approval implied.
                initial_stop_loss=_f(stop_loss),
                approved_risk_usd=(abs(_f(intended_price) - _f(stop_loss)) * _f(qty)
                                   if _f(stop_loss) and _f(intended_price) and _f(qty)
                                   else None),
                approved_notional=_f(approved_notional) or (
                    _f(intended_price) * _f(qty)
                    if _f(intended_price) and _f(qty) else None),
                broker_order_id=broker_order_id, status=PENDING,
                submitted_at=_now(), microstructure=json.dumps(snap, default=str),
                spread_pct_at_submit=snap.get("spread_pct"),
                book_imbalance_at_submit=snap.get("book_imbalance"),
            ))
            db.commit()
        return row_id
    except Exception as e:
        logger.warning(f"[ExecRec] intent not recorded for {symbol}: {e}")
        return None


def record_fill(row_id: str | None, *, fill_price: float | None,
                filled_qty: float | None = None, status: str = FILLED,
                broker_order_id: str | None = None) -> bool:
    """Close the loop: what actually happened to that order.

    Slippage is SIGNED so positive is always worse than intended. A buy
    filled above its limit and a sell filled below it are the same event,
    and averaging them unsigned would report a comfortable zero.
    """
    if not row_id:
        return False
    try:
        from app.database import ExecutionSample, get_db
        with get_db() as db:
            row = db.query(ExecutionSample).filter(ExecutionSample.id == row_id).first()
            if row is None:
                return False
            row.status = status
            row.resolved_at = _now()
            if broker_order_id:
                row.broker_order_id = broker_order_id
            fp, intended = _f(fill_price), _f(row.intended_price)
            if fp is not None and intended and intended > 0:
                row.fill_price = fp
                worse = (fp - intended) if str(row.side).lower().startswith("b") \
                    else (intended - fp)
                row.slippage_pct = round(worse / intended * 100.0, 6)
                row.slippage_bps = round(worse / intended * 10_000.0, 4)
            if filled_qty is not None:
                row.filled_qty = _f(filled_qty)
                q = _f(row.qty)
                if q:
                    row.fill_ratio = round(min(1.0, (_f(filled_qty) or 0) / q), 6)
            try:
                submitted = datetime.fromisoformat(str(row.submitted_at))
                row.fill_delay_ms = round(
                    (datetime.now(timezone.utc) - submitted).total_seconds() * 1000.0, 1)
            except Exception:
                pass
            db.commit()
        return True
    except Exception as e:
        logger.warning(f"[ExecRec] fill not recorded for {row_id}: {e}")
        return False


def readiness() -> dict:
    """Whether there is yet enough data to train an execution model.

    Deliberately blunt. The failure mode this guards is training on 4
    samples and believing the result — so the answer is a count and a plain
    verdict, not a score.
    """
    try:
        from sqlalchemy import text

        from app.database import engine
        with engine.connect() as c:
            q = lambda s: c.execute(text(s)).scalar() or 0
            total = q("SELECT COUNT(*) FROM execution_samples")
            filled = q(f"SELECT COUNT(*) FROM execution_samples WHERE status='{FILLED}'")
            with_slip = q("SELECT COUNT(*) FROM execution_samples WHERE slippage_bps IS NOT NULL")
            with_book = q("SELECT COUNT(*) FROM execution_samples "
                          "WHERE spread_pct_at_submit IS NOT NULL")
        return {
            "samples": total, "filled": filled,
            "with_slippage": with_slip, "with_book_state": with_book,
            "minimum_to_train": MIN_SAMPLES_TO_TRAIN,
            "ready": with_slip >= MIN_SAMPLES_TO_TRAIN and with_book >= MIN_SAMPLES_TO_TRAIN,
            "verdict": ("enough to attempt a model"
                        if with_slip >= MIN_SAMPLES_TO_TRAIN
                        else f"{with_slip} usable fills — collecting, not training"),
        }
    except Exception as e:
        return {"error": str(e), "ready": False}


# Below this, any "model" is a description of a handful of fills. Set from
# the same instinct as expectancy.MIN_SAMPLE but higher, because slippage is
# heavy-tailed and the tail is the part that matters.
MIN_SAMPLES_TO_TRAIN = 500
