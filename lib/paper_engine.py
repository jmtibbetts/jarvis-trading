"""
Paper Trading Engine v1.0
Supports: Long, Long Leveraged, Short, Short Leveraged
Tracks a virtual account with P&L, mark-to-market, and margin simulation.
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, PaperPosition, PaperTrade, PaperPortfolio

logger = logging.getLogger(__name__)

PAPER_STARTING_CAPITAL = 100_000.0   # $100k virtual account
MAX_LEVERAGE           = 3.0          # Max leverage multiplier
MARGIN_CALL_THRESHOLD  = 0.20         # Liquidate if equity < 20% of notional
DEFAULT_POSITION_SIZE  = 2_000.0      # $2k notional per trade (2% of $100k)

DIRECTION_LEVERAGE = {
    "Long":             (1,  1.0),
    "Long_Leveraged":   (1,  2.0),
    "Short":            (-1, 1.0),
    "Short_Leveraged":  (-1, 2.0),
}

def _now(): return datetime.now(timezone.utc).isoformat()

def _get_portfolio_cash(db) -> float:
    """Get or initialize the paper portfolio record."""
    p = db.query(PaperPortfolio).first()
    if not p:
        from app.database import new_id
        p = PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        )
        db.add(p)
        db.flush()
    return p

def open_paper_position(signal: dict, current_price: float = None) -> dict:
    """
    Open a new paper position from a trading signal.
    direction can be: Long, Long_Leveraged, Short, Short_Leveraged
    """
    sym      = signal.get("asset_symbol", "")
    direction = signal.get("paper_direction") or signal.get("direction") or "Long"
    # Normalize direction key
    dir_key  = direction.replace(" ", "_").replace("-", "_")
    if dir_key not in DIRECTION_LEVERAGE:
        dir_key = "Long"

    side, leverage = DIRECTION_LEVERAGE[dir_key]
    entry = float(current_price or signal.get("entry_price") or 0)
    if not entry or entry <= 0:
        return {"error": f"No valid entry price for {sym}"}

    target    = float(signal.get("target_price") or (entry * 1.05))
    stop      = float(signal.get("stop_loss")    or (entry * 0.97))
    notional  = DEFAULT_POSITION_SIZE * leverage
    qty       = round(notional / entry, 6)

    with get_db() as db:
        # Check for existing open position
        existing = db.query(PaperPosition).filter(
            PaperPosition.symbol == sym,
            PaperPosition.status == "Open"
        ).first()
        if existing:
            return {"error": f"Paper position already open for {sym}"}

        portfolio = _get_portfolio_cash(db)

        # Margin check — need at least the margin (notional / leverage) in cash
        margin_required = notional / leverage
        if portfolio.cash < margin_required:
            return {"error": f"Insufficient paper cash (${portfolio.cash:.0f}) for margin ${margin_required:.0f}"}

        from app.database import new_id
        pos = PaperPosition(
            id           = new_id(),
            symbol       = sym,
            asset_class  = signal.get("asset_class", "Equity"),
            direction    = dir_key,
            side         = "long" if side == 1 else "short",
            leverage     = leverage,
            qty          = qty,
            entry_price  = entry,
            current_price= entry,
            target_price = target,
            stop_loss    = stop,
            notional     = notional,
            margin_used  = margin_required,
            unrealized_pnl = 0.0,
            unrealized_pct = 0.0,
            signal_id    = signal.get("id"),
            status       = "Open",
            opened_at    = _now(),
            updated_at   = _now(),
        )
        db.add(pos)

        # Debit margin from cash
        portfolio.cash -= margin_required
        portfolio.updated_at = _now()

        pos_id = pos.id
        pos_data = {
            "id": pos_id, "symbol": sym, "direction": dir_key,
            "side": pos.side, "leverage": leverage, "qty": qty,
            "entry_price": entry, "target": target, "stop": stop,
            "notional": notional, "margin_required": margin_required
        }

    logger.info(f"[Paper] Opened {dir_key} on {sym} @ ${entry:.4f} | notional=${notional:.0f} | margin=${margin_required:.0f}")
    return {"ok": True, "position": pos_data}


def close_paper_position(pos_id: str, close_price: float, reason: str = "manual") -> dict:
    """Close a paper position and record the trade."""
    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}

        entry  = float(pos.entry_price)
        qty    = float(pos.qty)
        side   = 1 if pos.side == "long" else -1
        lev    = float(pos.leverage)

        raw_pnl  = (close_price - entry) * qty * side
        pnl_pct  = (raw_pnl / (entry * qty)) * 100 * side * lev if entry and qty else 0
        leveraged_pnl = raw_pnl * lev

        # Return margin + realized P&L
        margin_returned = float(pos.margin_used or 0)

        portfolio = _get_portfolio_cash(db)
        portfolio.cash        += margin_returned + leveraged_pnl
        portfolio.realized_pnl = (portfolio.realized_pnl or 0) + leveraged_pnl
        portfolio.total_trades = (portfolio.total_trades or 0) + 1
        if leveraged_pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        # Record completed trade
        from app.database import new_id
        trade = PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos.symbol,
            asset_class  = pos.asset_class,
            direction    = pos.direction,
            side         = pos.side,
            leverage     = lev,
            qty          = qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = float(pos.notional or 0),
            realized_pnl = leveraged_pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos.signal_id,
            opened_at    = pos.opened_at,
            closed_at    = _now(),
        )
        db.add(trade)

        pos.status        = "Closed"
        pos.current_price = close_price
        pos.unrealized_pnl= leveraged_pnl
        pos.updated_at    = _now()

        result = {
            "ok": True, "symbol": pos.symbol, "pnl": round(leveraged_pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price
        }

    logger.info(f"[Paper] Closed {pos.symbol} ({pos.direction}) @ ${close_price:.4f} | P&L=${leveraged_pnl:.2f} ({pnl_pct:.1f}%) | {reason}")
    return result


def mark_to_market(prices: dict) -> dict:
    """
    Update unrealized P&L for all open paper positions.
    prices = {symbol: current_price}
    Auto-triggers stop-loss / take-profit / margin-call checks.
    """
    closed = []
    updated = []

    with get_db() as db:
        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = [
            {
                "id": p.id, "symbol": p.symbol, "entry_price": float(p.entry_price),
                "qty": float(p.qty), "side": p.side, "leverage": float(p.leverage),
                "target_price": float(p.target_price or 0),
                "stop_loss": float(p.stop_loss or 0),
                "notional": float(p.notional or 0),
                "margin_used": float(p.margin_used or 0),
                "direction": p.direction,
            }
            for p in positions
        ]

    for pos in pos_list:
        sym   = pos["symbol"]
        price = prices.get(sym) or prices.get(sym.replace("/USD","")) or prices.get(sym.replace("/","")+"USD")
        if not price:
            continue

        entry  = pos["entry_price"]
        qty    = pos["qty"]
        side   = 1 if pos["side"] == "long" else -1
        lev    = pos["leverage"]
        raw    = (price - entry) * qty * side
        pnl    = raw * lev
        pct    = (pnl / (entry * qty / lev)) * 100 if entry and qty else 0

        # Check stops & targets
        reason = None
        if side == 1:  # long
            if price <= pos["stop_loss"]:   reason = "stop_loss"
            elif price >= pos["target_price"]: reason = "take_profit"
        else:          # short
            if price >= pos["stop_loss"]:   reason = "stop_loss"
            elif price <= pos["target_price"]: reason = "take_profit"

        # Margin call: if equity < 20% of notional
        equity_in_pos = pos["margin_used"] + pnl
        if equity_in_pos < pos["notional"] * MARGIN_CALL_THRESHOLD:
            reason = "margin_call"

        if reason:
            result = close_paper_position(pos["id"], price, reason)
            closed.append({"symbol": sym, "reason": reason, "pnl": result.get("pnl")})
        else:
            # Just update mark
            with get_db() as db:
                p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                if p:
                    p.current_price  = price
                    p.unrealized_pnl = round(pnl, 2)
                    p.unrealized_pct = round(pct, 2)
                    p.updated_at     = _now()
            updated.append(sym)

    return {"updated": len(updated), "closed": closed}


def get_paper_summary() -> dict:
    """Return portfolio summary and open positions."""
    with get_db() as db:
        portfolio = _get_portfolio_cash(db)
        p_data = {
            "cash": round(portfolio.cash, 2),
            "total_trades": portfolio.total_trades or 0,
            "winning_trades": portfolio.winning_trades or 0,
            "realized_pnl": round(portfolio.realized_pnl or 0, 2),
            "updated_at": portfolio.updated_at,
        }

        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = [
            {
                "id": p.id, "symbol": p.symbol, "direction": p.direction,
                "side": p.side, "leverage": p.leverage, "qty": float(p.qty),
                "entry_price": float(p.entry_price), "current_price": float(p.current_price or p.entry_price),
                "target_price": float(p.target_price or 0), "stop_loss": float(p.stop_loss or 0),
                "notional": float(p.notional or 0), "unrealized_pnl": float(p.unrealized_pnl or 0),
                "unrealized_pct": float(p.unrealized_pct or 0),
                "opened_at": p.opened_at, "asset_class": p.asset_class,
            }
            for p in positions
        ]

        trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(50).all()
        trade_list = [
            {
                "id": t.id, "symbol": t.symbol, "direction": t.direction,
                "leverage": t.leverage, "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price), "realized_pnl": round(float(t.realized_pnl), 2),
                "pnl_pct": round(float(t.pnl_pct), 2), "close_reason": t.close_reason,
                "opened_at": t.opened_at, "closed_at": t.closed_at, "asset_class": t.asset_class,
            }
            for t in trades
        ]

    open_pnl  = sum(p["unrealized_pnl"] for p in pos_list)
    margin_in = sum(p["notional"] / p["leverage"] for p in pos_list)
    equity    = p_data["cash"] + margin_in + open_pnl
    win_rate  = round(p_data["winning_trades"] / p_data["total_trades"] * 100, 1) if p_data["total_trades"] > 0 else 0

    return {
        "portfolio": {
            **p_data,
            "open_pnl":    round(open_pnl, 2),
            "equity":      round(equity, 2),
            "margin_in_use": round(margin_in, 2),
            "win_rate":    win_rate,
            "starting_capital": PAPER_STARTING_CAPITAL,
            "total_return_pct": round((equity - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100, 2),
        },
        "positions": pos_list,
        "trades":    trade_list,
    }
