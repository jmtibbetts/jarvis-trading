"""Paper-only simulator that follows every eligible signal immediately."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, OperationalError

from app.database import (
    AutoSimPortfolio, AutoSimPosition, AutoSimTrade, DEFAULT_USER_ID,
    MarketAsset, TradingSignal, UserPreference, get_db, new_id, now_iso,
)

MARGIN_PER_SIGNAL = 1000.0
ELIGIBLE_STATUSES = {"Active", "PendingApproval"}
_AUTO_SIM_LOCK = threading.Lock()


def _side(direction: str | None) -> str:
    return "short" if "short" in str(direction or "").lower() else "long"


def _leverage(direction: str | None) -> float:
    value = str(direction or "")
    explicit = re.search(r"(?:^|_)(\d+)x(?:_|$)", value, re.I)
    if explicit:
        return min(20.0, max(1.0, float(explicit.group(1))))
    return 2.0 if "leverag" in value.lower() else 1.0


def _pnl(position, price: float) -> float:
    move = price - float(position.entry_price or 0)
    if position.side == "short":
        move *= -1
    return move * float(position.qty or 0)


def _unseen_candidates(signals, seen_signal_ids: set[str]):
    return [
        signal for signal in signals
        if signal.status in ELIGIBLE_STATUSES and signal.id not in seen_signal_ids
    ]


def _ensure_portfolio(db, user_id: str) -> AutoSimPortfolio:
    row = db.query(AutoSimPortfolio).filter(AutoSimPortfolio.user_id == user_id).first()
    if not row:
        row = AutoSimPortfolio(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def _close(db, position: AutoSimPosition, price: float, reason: str,
           portfolio: AutoSimPortfolio) -> None:
    pnl = _pnl(position, price)
    margin = float(position.margin_used or MARGIN_PER_SIGNAL)
    pnl_pct = pnl / margin * 100 if margin else 0.0
    db.add(AutoSimTrade(
        id=new_id(), user_id=position.user_id, signal_id=position.signal_id,
        symbol=position.symbol, asset_class=position.asset_class,
        direction=position.direction, side=position.side, leverage=position.leverage,
        qty=position.qty, entry_price=position.entry_price, exit_price=price,
        realized_pnl=round(pnl, 6), pnl_pct=round(pnl_pct, 4),
        close_reason=reason, opened_at=position.opened_at, closed_at=now_iso(),
    ))
    position.current_price = price
    position.unrealized_pnl = 0.0
    position.status = "Closed"
    position.updated_at = now_iso()
    portfolio.realized_pnl = float(portfolio.realized_pnl or 0) + pnl
    portfolio.total_trades = int(portfolio.total_trades or 0) + 1
    if pnl > 0:
        portfolio.wins = int(portfolio.wins or 0) + 1
    elif pnl < 0:
        portfolio.losses = int(portfolio.losses or 0) + 1
    portfolio.updated_at = now_iso()


def run_auto_simulator(user_id: str = DEFAULT_USER_ID) -> dict:
    """Run once per process; overlapping requests return a harmless busy result."""
    if not _AUTO_SIM_LOCK.acquire(blocking=False):
        return {"busy": True, "reason": "run_in_progress"}
    try:
        return _run_auto_simulator(user_id)
    except OperationalError as exc:
        if "database is locked" in str(exc).lower():
            return {"busy": True, "reason": "database_busy"}
        raise
    except IntegrityError as exc:
        if "uq_auto_sim_position_user_signal" in str(exc) or "auto_sim_positions.user_id" in str(exc):
            return {"busy": True, "reason": "signal_already_opened"}
        raise
    finally:
        _AUTO_SIM_LOCK.release()


def _run_auto_simulator(user_id: str = DEFAULT_USER_ID) -> dict:
    now = datetime.now(timezone.utc)
    with get_db() as db:
        preference = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
        if preference and not bool(preference.auto_sim_enabled):
            return {"skipped": True, "reason": "disabled"}

        portfolio = _ensure_portfolio(db, user_id)
        prices = {
            row.symbol: float(row.price)
            for row in db.query(MarketAsset).filter(MarketAsset.price.isnot(None)).all()
            if float(row.price or 0) > 0
        }
        all_signals = {
            row.id: row for row in db.query(TradingSignal).filter(
                TradingSignal.user_id == user_id
            ).all()
        }
        positions = db.query(AutoSimPosition).filter(
            AutoSimPosition.user_id == user_id, AutoSimPosition.status == "Open",
        ).all()
        seen_signal_ids = {
            signal_id for signal_id, in db.query(AutoSimPosition.signal_id).filter(
                AutoSimPosition.user_id == user_id,
                AutoSimPosition.signal_id.isnot(None),
            ).all()
        }
        seen_signal_ids.update(
            signal_id for signal_id, in db.query(AutoSimTrade.signal_id).filter(
                AutoSimTrade.user_id == user_id,
                AutoSimTrade.signal_id.isnot(None),
            ).all()
        )

        opened = closed = updated = skipped = 0
        for position in positions:
            signal = all_signals.get(position.signal_id)
            price = prices.get(position.symbol, float(position.current_price or position.entry_price or 0))
            if price <= 0:
                skipped += 1
                continue
            reason = None
            if not signal or signal.status == "Superseded":
                reason = "signal_replaced"
            elif signal.status in {"Rejected", "Expired"}:
                reason = signal.status.lower()
            elif signal.expires_at:
                try:
                    expiry = datetime.fromisoformat(signal.expires_at.replace("Z", "+00:00"))
                    expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry.astimezone(timezone.utc)
                    if expiry <= now:
                        reason = "expired"
                except ValueError:
                    pass
            target = float(position.target_price or 0)
            stop = float(position.stop_loss or 0)
            if position.side == "short":
                if target > 0 and price <= target:
                    reason = "take_profit"
                elif stop > 0 and price >= stop:
                    reason = "stop_loss"
            else:
                if target > 0 and price >= target:
                    reason = "take_profit"
                elif stop > 0 and price <= stop:
                    reason = "stop_loss"
            if reason:
                _close(db, position, price, reason, portfolio)
                closed += 1
            else:
                position.current_price = price
                position.unrealized_pnl = round(_pnl(position, price), 6)
                position.updated_at = now.isoformat()
                updated += 1

        candidates = _unseen_candidates(all_signals.values(), seen_signal_ids)
        for signal in candidates:
            entry = prices.get(signal.asset_symbol, float(signal.entry_price or 0))
            recorded_entry = float(signal.entry_price or 0)
            if entry <= 0 or recorded_entry <= 0:
                skipped += 1
                continue
            ratio = entry / recorded_entry
            if ratio < 0.2 or ratio > 5.0:
                skipped += 1
                continue
            leverage = _leverage(signal.paper_direction or signal.direction)
            qty = MARGIN_PER_SIGNAL * leverage / entry
            db.add(AutoSimPosition(
                id=new_id(), user_id=user_id, signal_id=signal.id,
                symbol=signal.asset_symbol, asset_class=signal.asset_class,
                direction=signal.direction, side=_side(signal.direction), leverage=leverage,
                qty=qty, entry_price=entry, current_price=entry,
                target_price=signal.target_price, stop_loss=signal.stop_loss,
                margin_used=MARGIN_PER_SIGNAL, unrealized_pnl=0.0,
                signal_updated_at=signal.updated_date, opened_at=now.isoformat(),
                updated_at=now.isoformat(),
            ))
            opened += 1
        return {"opened": opened, "closed": closed, "updated": updated, "skipped": skipped}


def get_auto_sim_summary(user_id: str = DEFAULT_USER_ID) -> dict:
    with get_db() as db:
        portfolio = _ensure_portfolio(db, user_id)
        positions = db.query(AutoSimPosition).filter(
            AutoSimPosition.user_id == user_id, AutoSimPosition.status == "Open",
        ).order_by(AutoSimPosition.opened_at.desc()).all()
        trades = db.query(AutoSimTrade).filter(
            AutoSimTrade.user_id == user_id
        ).order_by(AutoSimTrade.closed_at.desc()).all()
        unrealized = sum(float(row.unrealized_pnl or 0) for row in positions)
        wins = int(portfolio.wins or 0)
        losses = int(portfolio.losses or 0)
        decided = wins + losses
        realized = float(portfolio.realized_pnl or 0)
        starting = float(portfolio.starting_cash or 100000)
        gross_profit = sum(max(0, float(row.realized_pnl or 0)) for row in trades)
        gross_loss = sum(min(0, float(row.realized_pnl or 0)) for row in trades)
        return {
            "paper_only": True,
            "summary": {
                "starting_cash": starting, "equity": starting + realized + unrealized,
                "realized_pnl": realized, "unrealized_pnl": unrealized,
                "total_pnl": realized + unrealized, "total_trades": int(portfolio.total_trades or 0),
                "wins": wins, "losses": losses,
                "win_rate": round(wins / decided * 100, 2) if decided else 0.0,
                "gross_profit": gross_profit, "gross_loss": gross_loss,
                "open_positions": len(positions),
            },
            "positions": [{
                "id": row.id, "signal_id": row.signal_id, "symbol": row.symbol,
                "asset_class": row.asset_class, "direction": row.direction,
                "leverage": row.leverage, "entry_price": row.entry_price,
                "current_price": row.current_price, "target_price": row.target_price,
                "stop_loss": row.stop_loss, "unrealized_pnl": row.unrealized_pnl,
                "opened_at": row.opened_at,
            } for row in positions],
            "trades": [{
                "id": row.id, "signal_id": row.signal_id, "symbol": row.symbol,
                "asset_class": row.asset_class, "direction": row.direction,
                "leverage": row.leverage, "entry_price": row.entry_price,
                "exit_price": row.exit_price, "realized_pnl": row.realized_pnl,
                "pnl_pct": row.pnl_pct, "close_reason": row.close_reason,
                "opened_at": row.opened_at, "closed_at": row.closed_at,
            } for row in trades[:200]],
        }


def flatten_auto_simulator(user_id: str = DEFAULT_USER_ID) -> dict:
    """Close every open Auto Sim position at its last known price, preserving
    the trade history and win/loss record (unlike reset, which wipes it).
    Positions without a usable price are left open and reported."""
    closed = 0
    no_price = []
    with get_db() as db:
        portfolio = _ensure_portfolio(db, user_id)
        rows = db.query(AutoSimPosition).filter(
            AutoSimPosition.user_id == user_id,
            AutoSimPosition.status == "Open",
        ).all()
        for pos in rows:
            price = float(pos.current_price or pos.entry_price or 0)
            if price <= 0:
                no_price.append(pos.symbol)
                continue
            _close(db, pos, price, "manual flatten", portfolio)
            closed += 1
    return {"closed": closed, "skipped_no_price": no_price}


def reset_auto_simulator(user_id: str = DEFAULT_USER_ID) -> None:
    with get_db() as db:
        db.query(AutoSimTrade).filter(AutoSimTrade.user_id == user_id).delete()
        db.query(AutoSimPosition).filter(AutoSimPosition.user_id == user_id).delete()
        portfolio = _ensure_portfolio(db, user_id)
        portfolio.starting_cash = 100000.0
        portfolio.realized_pnl = 0.0
        portfolio.total_trades = 0
        portfolio.wins = 0
        portfolio.losses = 0
        portfolio.updated_at = now_iso()
