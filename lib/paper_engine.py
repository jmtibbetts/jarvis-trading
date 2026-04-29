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

def _get_portfolio_cash(db):
    """Fetch the paper portfolio record. init_db() guarantees it exists."""
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
        logger.warning("[Paper] Portfolio row was missing — created with $100k starting capital")
    return p

def open_paper_position(signal: dict, current_price: float = None) -> dict:
    """
    Open a new paper position from a trading signal.
    direction can be: Long, Long_Leveraged, Short, Short_Leveraged
    """
    sym      = signal.get("asset_symbol", "")
    direction = signal.get("paper_direction") or signal.get("direction") or "Long"
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
        existing = db.query(PaperPosition).filter(
            PaperPosition.symbol == sym,
            PaperPosition.status == "Open"
        ).first()
        if existing:
            return {"error": f"Paper position already open for {sym}"}

        portfolio = _get_portfolio_cash(db)
        margin_required = notional / leverage
        logger.info(f"[Paper] Cash available: ${portfolio.cash:.2f} | margin required: ${margin_required:.2f}")
        if portfolio.cash < margin_required:
            logger.warning(f"[Paper] Insufficient cash — have ${portfolio.cash:.2f}, need ${margin_required:.2f}")
            return {"error": f"Insufficient paper cash (${portfolio.cash:.0f}) for margin ${margin_required:.0f}. Use /api/paper/reset to restore $100k."}

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
        portfolio.cash -= margin_required
        portfolio.updated_at = _now()

        # Extract all needed values before session closes
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
    # Capture all values as locals inside the session block
    result = {}
    log_symbol = ""
    log_direction = ""
    log_pnl = 0.0
    log_pct = 0.0

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}

        # Extract all ORM values while session is open
        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at
        pos_notional  = float(pos.notional or 0)
        pos_margin    = float(pos.margin_used or 0)

        entry  = float(pos.entry_price)
        qty    = float(pos.qty)
        side   = 1 if pos_side == "long" else -1
        lev    = float(pos.leverage)

        raw_pnl       = (close_price - entry) * qty * side
        pnl_pct       = (raw_pnl / (entry * qty)) * 100 * side * lev if entry and qty else 0
        leveraged_pnl = raw_pnl * lev

        portfolio = _get_portfolio_cash(db)
        portfolio.cash        += pos_margin + leveraged_pnl
        portfolio.realized_pnl = (portfolio.realized_pnl or 0) + leveraged_pnl
        portfolio.total_trades = (portfolio.total_trades or 0) + 1
        if leveraged_pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        from app.database import new_id
        trade = PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos_symbol,
            asset_class  = pos_asset_cls,
            direction    = pos_direction,
            side         = pos_side,
            leverage     = lev,
            qty          = qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = pos_notional,
            realized_pnl = leveraged_pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        )
        db.add(trade)

        pos.status        = "Closed"
        pos.current_price = close_price
        pos.unrealized_pnl= leveraged_pnl
        pos.updated_at    = _now()

        # Build result dict from local vars (not ORM attrs) while session is still open
        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(leveraged_pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price
        }
        log_symbol    = pos_symbol
        log_direction = pos_direction
        log_pnl       = leveraged_pnl
        log_pct       = pnl_pct

    # Safe to log now — using local vars, not ORM attributes
    logger.info(f"[Paper] Closed {log_symbol} ({log_direction}) @ ${close_price:.4f} | P&L=${log_pnl:.2f} ({log_pct:.1f}%) | {reason}")
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
                "id": p.id, "symbol": p.symbol, "entry_price": float(p.entry_price or 0),
                "qty": float(p.qty or 0), "side": p.side or "long",
                "leverage": float(p.leverage or 1.0),
                "target_price": float(p.target_price or 0),
                "stop_loss": float(p.stop_loss or 0),
                "notional": float(p.notional or 0),
                "margin_used": float(p.margin_used or 0),
                "direction": p.direction or "Long",
            }
            for p in positions
            if p.entry_price and p.qty
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

        reason = None
        if side == 1:  # long
            if price <= pos["stop_loss"]:      reason = "stop_loss"
            elif price >= pos["target_price"]: reason = "take_profit"
        else:          # short
            if price >= pos["stop_loss"]:      reason = "stop_loss"
            elif price <= pos["target_price"]: reason = "take_profit"

        equity_in_pos = pos["margin_used"] + pnl
        if equity_in_pos < pos["notional"] * MARGIN_CALL_THRESHOLD:
            reason = "margin_call"

        if reason:
            result = close_paper_position(pos["id"], price, reason)
            closed.append({"symbol": sym, "reason": reason, "pnl": result.get("pnl")})
        else:
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
                "id": p.id, "symbol": p.symbol, "direction": p.direction or "Long",
                "side": p.side or "long", "leverage": float(p.leverage or 1.0),
                "qty": float(p.qty or 0),
                "entry_price": float(p.entry_price or 0),
                "current_price": float(p.current_price or p.entry_price or 0),
                "target_price": float(p.target_price or 0), "stop_loss": float(p.stop_loss or 0),
                "notional": float(p.notional or 0), "unrealized_pnl": float(p.unrealized_pnl or 0),
                "unrealized_pct": float(p.unrealized_pct or 0),
                "opened_at": p.opened_at, "asset_class": p.asset_class or "Equity",
            }
            for p in positions
            if p.entry_price
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
    # margin_in is the cash already deducted from portfolio.cash when positions were opened.
    # equity = cash (which includes locked margin) + unrealized P&L on those positions.
    # Do NOT add margin_in again — it would double-count.
    margin_in = sum(p["notional"] / (p["leverage"] or 1.0) for p in pos_list if p["notional"])
    equity    = p_data["cash"] + open_pnl
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


def reset_paper_portfolio() -> dict:
    """Reset the paper portfolio back to $100k starting capital.
    Hard-deletes all positions and trades, then recreates a clean portfolio row.
    This guarantees cash is always exactly $100k — no stale deductions.
    """
    from app.database import new_id
    with get_db() as db:
        from app.database import PaperTrade, PaperPosition, PaperPortfolio
        db.query(PaperTrade).delete()
        db.query(PaperPosition).delete()
        db.query(PaperPortfolio).delete()
        db.flush()
        db.add(PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        ))

    logger.info("[Paper] Portfolio hard-reset to $100k — all positions/trades cleared")
    return {"ok": True, "message": "Paper portfolio reset to $100k", "cash": PAPER_STARTING_CAPITAL}
