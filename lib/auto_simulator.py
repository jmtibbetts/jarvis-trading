"""Paper-only simulator that follows every eligible signal immediately."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, OperationalError

from app.database import (
    AutoSimPortfolio, AutoSimPosition, AutoSimTrade, DEFAULT_USER_ID,
    MarketAsset, TradingSignal, UserPreference, get_db, new_id, now_iso,
)
from lib import trade_side

logger = logging.getLogger(__name__)

MARGIN_PER_SIGNAL = 1000.0
# Auto Sim followed EVERY signal with no capital check whatsoever: 228 open
# positions x $1,000 committed $228,000 of margin against a $100,000 account.
# A book that can deploy 2.28x the money it has cannot be compared against
# the broker account, which is the whole point of running it. Bound it the
# same way the paper engine is bounded.
MAX_DEPLOYED_PCT   = 60.0    # total committed margin, % of starting capital
MAX_OPEN_POSITIONS = 60
ELIGIBLE_STATUSES = {"Active", "PendingApproval"}
_AUTO_SIM_LOCK = threading.Lock()


def _side(direction: str | None) -> str:
    return trade_side.normalize_side(direction)


def _leverage(direction: str | None) -> float:
    value = str(direction or "")
    explicit = re.search(r"(?:^|_)(\d+)x(?:_|$)", value, re.I)
    if explicit:
        return min(20.0, max(1.0, float(explicit.group(1))))
    return 2.0 if "leverag" in value.lower() else 1.0


# ── Leverage (virtual books only) ───────────────────────────────────────────
# Never applies to the broker account — Alpaca caps equities at 2x and crypto
# at 1x. The ladder itself now lives in lib/leverage_policy.py and is shared
# with the paper engine; the old 5x-100x step list here was removed because
# two books on different leverage scales cannot be compared, which is the
# entire reason Auto Sim exists.
#
# A position is liquidated when the price moves margin/notional against it —
# i.e. a 1/L move at leverage L. The sim enforces the stop BEFORE that point:
# max loss per position is capped at this fraction of margin, and the stop
# tightens with leverage (a "2-3% price stop" is physically impossible at
# 100x — the position would liquidate at 1%).
MAX_MARGIN_LOSS_FRAC = 0.9


def score_leverage(composite_score: float | None, asset_class: str | None = None,
                   direction: str | None = None) -> float:
    """Same policy the paper engine uses — 1x at the operator's configured
    floor up to 25x, reduced by regime, realized win rate, losing streak and
    volatility. Auto Sim previously ran its own 5x-100x ladder, which made
    its results incomparable with the book it exists to be measured against."""
    try:
        from lib.paper_engine import score_leverage as _policy
        return float(_policy(composite_score, asset_class=asset_class, direction=direction))
    except Exception as e:
        logger.debug(f"[AutoSim] Leverage policy unavailable ({e}) — defaulting to 2x")
        return 2.0


# Max stop distance as a fraction of entry, by trade horizon: scalps get a
# tight 3% ceiling, longer trades up to 10%. The leverage liquidation cap
# can only tighten these further, never widen them.
HORIZON_STOP_CAP = {"scalp": 0.03, "longer": 0.10, "all": 0.10}


def leverage_capped_stop(entry: float, signal_stop: float | None, side: str,
                         leverage: float, timeframe: str | None = None) -> float:
    """The tightest of: the signal's own stop, the horizon cap (3% scalp /
    10% longer), and the price distance at which the position would lose
    MAX_MARGIN_LOSS_FRAC of its margin (0.9/L — at 100x that is 0.9%)."""
    from lib.trading_preferences import horizon_for_timeframe
    horizon_cap = HORIZON_STOP_CAP.get(horizon_for_timeframe(timeframe), 0.10)
    max_move = entry * min(MAX_MARGIN_LOSS_FRAC / max(1.0, leverage), horizon_cap)
    if side == "short":
        cap = entry + max_move
        sig = float(signal_stop or 0)
        return min(sig, cap) if sig > 0 else cap
    cap = entry - max_move
    sig = float(signal_stop or 0)
    return max(sig, cap) if sig > 0 else cap


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
            # Liquidation: losing the full margin at this leverage closes the
            # position regardless of where the stop sits (mirrors how a real
            # perp would liquidate before a too-wide stop could trigger).
            margin = float(position.margin_used or MARGIN_PER_SIGNAL)
            if reason is None and _pnl(position, price) <= -margin:
                reason = "liquidated"
            # Breakeven ratchet: once price has covered half the distance to
            # target, the stop moves to entry — winners can no longer turn
            # into losers. Tighten-only, never loosens.
            if reason is None and target > 0:
                entry_px = float(position.entry_price or 0)
                if entry_px > 0:
                    halfway = entry_px + (target - entry_px) * 0.5
                    crossed = price <= halfway if position.side == "short" else price >= halfway
                    improves = (stop > entry_px) if position.side == "short" else (stop < entry_px)
                    if crossed and improves:
                        position.stop_loss = entry_px
                        stop = entry_px
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
        # Capacity is measured ONCE against the positions that survived the
        # management pass above, then decremented as this run opens more.
        live_rows = [p for p in positions if p.status == "Open"]
        deployed = sum(float(p.margin_used or MARGIN_PER_SIGNAL) for p in live_rows)
        slots_left = MAX_OPEN_POSITIONS - len(live_rows)
        capital = float(portfolio.starting_cash or 100_000.0)
        deploy_cap = capital * (MAX_DEPLOYED_PCT / 100.0)
        if slots_left <= 0 or deployed >= deploy_cap:
            logger.info(
                f"[AutoSim] At capacity ({len(live_rows)}/{MAX_OPEN_POSITIONS} positions, "
                f"${deployed:,.0f}/${deploy_cap:,.0f} deployed) — {len(candidates)} signal(s) not followed"
            )
            candidates = []
        for signal in candidates:
            if slots_left <= 0 or deployed + MARGIN_PER_SIGNAL > deploy_cap:
                skipped += 1
                continue
            entry = prices.get(signal.asset_symbol, float(signal.entry_price or 0))
            recorded_entry = float(signal.entry_price or 0)
            if entry <= 0 or recorded_entry <= 0:
                skipped += 1
                continue
            ratio = entry / recorded_entry
            if ratio < 0.2 or ratio > 5.0:
                skipped += 1
                continue
            side = _side(signal.direction)
            leverage = score_leverage(signal.composite_score or signal.confidence,
                                      asset_class=signal.asset_class, direction=signal.direction)
            stop = leverage_capped_stop(entry, signal.stop_loss, side, leverage, signal.timeframe)
            qty = MARGIN_PER_SIGNAL * leverage / entry
            db.add(AutoSimPosition(
                id=new_id(), user_id=user_id, signal_id=signal.id,
                symbol=signal.asset_symbol, asset_class=signal.asset_class,
                direction=signal.direction, side=side, leverage=leverage,
                qty=qty, entry_price=entry, current_price=entry,
                target_price=signal.target_price, stop_loss=stop,
                margin_used=MARGIN_PER_SIGNAL, unrealized_pnl=0.0,
                signal_updated_at=signal.updated_date, opened_at=now.isoformat(),
                updated_at=now.isoformat(),
            ))
            opened += 1
            deployed += MARGIN_PER_SIGNAL
            slots_left -= 1
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
