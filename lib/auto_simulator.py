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


# ── Transaction costs ───────────────────────────────────────────────────────
# Auto Sim used to price every trade as free — `_pnl` was the raw price move,
# and nothing charged a fee or crossed a spread. That is not a small
# understatement: over 166 trades under the current leverage policy the book
# showed +$128 while the venue fees it never paid came to $6,864. The sign of
# the result was decided entirely by the missing side of the ledger.
#
# Costs are charged the way they are actually incurred:
#   entry  — you cross half the spread getting in (fill worse than mid)
#   exit   — you cross the other half getting out
#   fees   — the full round trip is reserved at OPEN, so an untouched
#            position immediately shows what it would cost to unwind. That
#            keeps unrealized and realized P&L on one basis; a position is
#            never allowed to look flat when closing it would lose money.

def _fill_price(symbol: str, price: float, side: str, *, entering: bool) -> tuple[float, float]:
    """(fill, half_spread_pct). The spread always moves against the trade:
    buying fills above mid, selling fills below."""
    try:
        from lib.transaction_costs import estimate_spread_pct
        spread, _ = estimate_spread_pct(symbol)
    except Exception:
        return float(price), 0.0
    half = float(spread or 0.0) / 2.0
    buying = (side != "short") if entering else (side == "short")
    return float(price) * (1.0 + half if buying else 1.0 - half), half


def _round_trip_fee(symbol: str, notional: float, leverage: float,
                    price: float) -> tuple[float, str]:
    """Venue cost of opening AND closing. A lookup failure charges the
    conservative default rather than zero — a failed measurement must never
    make a trade look cheaper than it is."""
    try:
        from lib.paper_engine import venue_round_trip_fee
        fee, why = venue_round_trip_fee(symbol, notional, leverage, price)
        if fee is not None and fee >= 0:
            return float(fee), str(why)
    except Exception as e:
        logger.debug(f"[AutoSim] fee lookup failed for {symbol} ({e})")
    from lib.transaction_costs import fee_pct
    return abs(notional) * fee_pct(symbol) * 2.0, "fallback: default taker fee, both sides"


def _gross_pnl(position, price: float) -> float:
    """Price move only, before costs."""
    move = price - float(position.entry_price or 0)
    if position.side == "short":
        move *= -1
    return move * float(position.qty or 0)


def _pnl(position, price: float) -> float:
    """Net of the round-trip fee reserved at open. This is the number the
    portfolio, the stop/liquidation checks and the UI all use."""
    return _gross_pnl(position, price) - float(getattr(position, "fees", 0.0) or 0.0)


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
    # The trigger is evaluated at the market price; the FILL crosses the
    # spread. A stop does not fill at the stop.
    exit_price, _ = _fill_price(position.symbol, price, position.side, entering=False)
    gross = _gross_pnl(position, exit_price)
    fees = float(position.fees or 0.0)
    pnl = gross - fees
    margin = float(position.margin_used or MARGIN_PER_SIGNAL)
    pnl_pct = pnl / margin * 100 if margin else 0.0
    db.add(AutoSimTrade(
        id=new_id(), user_id=position.user_id, signal_id=position.signal_id,
        symbol=position.symbol, asset_class=position.asset_class,
        direction=position.direction, side=position.side, leverage=position.leverage,
        qty=position.qty, entry_price=position.entry_price, exit_price=exit_price,
        gross_pnl=round(gross, 6), fees=round(fees, 6),
        fee_basis=position.fee_basis,
        realized_pnl=round(pnl, 6), pnl_pct=round(pnl_pct, 4),
        close_reason=reason, opened_at=position.opened_at, closed_at=now_iso(),
    ))
    price = exit_price
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
            # Fill crosses half the spread; the stop is measured from the price
            # actually paid, not from mid.
            entry, half_spread = _fill_price(signal.asset_symbol, entry, side, entering=True)
            stop = leverage_capped_stop(entry, signal.stop_loss, side, leverage, signal.timeframe)
            qty = MARGIN_PER_SIGNAL * leverage / entry
            fees, fee_basis = _round_trip_fee(signal.asset_symbol, qty * entry, leverage, entry)
            db.add(AutoSimPosition(
                id=new_id(), user_id=user_id, signal_id=signal.id,
                symbol=signal.asset_symbol, asset_class=signal.asset_class,
                direction=signal.direction, side=side, leverage=leverage,
                qty=qty, entry_price=entry, current_price=entry,
                target_price=signal.target_price, stop_loss=stop,
                margin_used=MARGIN_PER_SIGNAL, fees=round(fees, 6),
                fee_basis=fee_basis, entry_slippage_pct=round(half_spread, 8),
                unrealized_pnl=round(-fees, 6),
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
        # Costs are reported, not just netted out. The gap between these two
        # numbers is what the book used to keep for free.
        fees_closed = sum(float(row.fees or 0) for row in trades)
        fees_open = sum(float(row.fees or 0) for row in positions)
        pnl_before_costs = realized + unrealized + fees_closed + fees_open
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
                "total_fees": round(fees_closed + fees_open, 2),
                "fees_realized": round(fees_closed, 2),
                "fees_reserved_open": round(fees_open, 2),
                "pnl_before_costs": round(pnl_before_costs, 2),
                # Only meaningful when the book made money before costs —
                # "costs are 573% of gross" against a NEGATIVE gross reads as
                # a ratio but means nothing. When gross is a loss, costs
                # simply deepened it, and the two figures beside it say so.
                "cost_drag_pct": round(
                    (fees_closed + fees_open) / pnl_before_costs * 100, 1
                ) if pnl_before_costs > 1e-9 else None,
            },
            "positions": [{
                "id": row.id, "signal_id": row.signal_id, "symbol": row.symbol,
                "asset_class": row.asset_class, "direction": row.direction,
                "leverage": row.leverage, "entry_price": row.entry_price,
                "current_price": row.current_price, "target_price": row.target_price,
                "stop_loss": row.stop_loss, "unrealized_pnl": row.unrealized_pnl,
                "fees": row.fees, "fee_basis": row.fee_basis,
                "entry_slippage_pct": row.entry_slippage_pct,
                "opened_at": row.opened_at,
            } for row in positions],
            "trades": [{
                "id": row.id, "signal_id": row.signal_id, "symbol": row.symbol,
                "asset_class": row.asset_class, "direction": row.direction,
                "leverage": row.leverage, "entry_price": row.entry_price,
                "exit_price": row.exit_price, "realized_pnl": row.realized_pnl,
                "gross_pnl": row.gross_pnl, "fees": row.fees, "fee_basis": row.fee_basis,
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
