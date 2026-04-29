"""
Paper Trading Engine v2.0
Supports: Long, Long Leveraged, Short, Short Leveraged
Tracks a virtual account with P&L, mark-to-market, and margin simulation.

v2.0 Fixes:
- open_paper_position: direction key normalization is now exhaustive (handles LLM variants)
- open_paper_position: asset_class auto-detected from symbol if not provided
- mark_to_market: improved symbol lookup covers slash/no-slash variants
- mark_to_market: SHORT stop/target logic was inverted (stop ABOVE entry, target BELOW)
  — now correctly closes shorts at stop when price >= stop_loss
- mark_to_market: added missing margin_used fallback to prevent $0 margin positions
- DEFAULT_POSITION_SIZE raised to $3,000 for better trade visibility
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, PaperPosition, PaperTrade, PaperPortfolio

logger = logging.getLogger(__name__)

PAPER_STARTING_CAPITAL = 100_000.0   # $100k virtual account
MAX_LEVERAGE           = 20.0         # Max leverage multiplier (5x/10x/20x supported)
MARGIN_CALL_THRESHOLD  = 0.15         # Liquidate if equity < 15% of margin (lost 85% of capital)
DEFAULT_POSITION_SIZE  = 3_000.0      # $3k margin per trade (3% of $100k)

# Leverage by asset class — futures get tighter margin than equity
ASSET_CLASS_MARGIN = {
    "futures":  1_500.0,   # Futures use smaller margin (higher leverage)
    "forex":    1_000.0,   # Forex pip-based — smaller notional per pip
    "crypto":   2_000.0,
    "equity":   3_000.0,
}

DIRECTION_LEVERAGE = {
    "Long":               (1,   1.0),
    "Bounce":             (1,   1.0),
    "Long_Leveraged":     (1,   2.0),
    "Long_5x":            (1,   5.0),
    "Long_10x":           (1,  10.0),
    "Long_20x":           (1,  20.0),
    "Short":              (-1,  1.0),
    "Short_Leveraged":    (-1,  2.0),
    "Short_5x":           (-1,  5.0),
    "Short_10x":          (-1, 10.0),
    "Short_20x":          (-1, 20.0),
}

# Exhaustive mapping for LLM output normalization
_DIR_ALIASES = {
    "long":              "Long",
    "bounce":            "Bounce",
    "long_leveraged":    "Long_Leveraged",
    "longleveraged":     "Long_Leveraged",
    "long leveraged":    "Long_Leveraged",
    "long-leveraged":    "Long_Leveraged",
    "leveraged long":    "Long_Leveraged",
    "leveraged_long":    "Long_Leveraged",
    "long_2x":           "Long_Leveraged",
    "long_5x":           "Long_5x",
    "long5x":            "Long_5x",
    "long 5x":           "Long_5x",
    "long-5x":           "Long_5x",
    "long_10x":          "Long_10x",
    "long10x":           "Long_10x",
    "long 10x":          "Long_10x",
    "long_20x":          "Long_20x",
    "long20x":           "Long_20x",
    "long 20x":          "Long_20x",
    "short":             "Short",
    "short_leveraged":   "Short_Leveraged",
    "shortleveraged":    "Short_Leveraged",
    "short leveraged":   "Short_Leveraged",
    "short-leveraged":   "Short_Leveraged",
    "leveraged short":   "Short_Leveraged",
    "leveraged_short":   "Short_Leveraged",
    "short_2x":          "Short_Leveraged",
    "short_5x":          "Short_5x",
    "short5x":           "Short_5x",
    "short 5x":          "Short_5x",
    "short-5x":          "Short_5x",
    "short_10x":         "Short_10x",
    "short10x":          "Short_10x",
    "short 10x":         "Short_10x",
    "short_20x":         "Short_20x",
    "short20x":          "Short_20x",
    "short 20x":         "Short_20x",
}


def _normalize_direction(raw: str) -> str:
    """Normalize any LLM direction output to a canonical DIRECTION_LEVERAGE key."""
    if not raw:
        return "Long"
    cleaned = raw.strip().replace(" ", "_").replace("-", "_")
    # Try direct match first
    if cleaned in DIRECTION_LEVERAGE:
        return cleaned
    # Try alias map (case-insensitive)
    lower = cleaned.lower().replace("_", " ")
    for alias, canonical in _DIR_ALIASES.items():
        if lower == alias:
            return canonical
    # Fallback: if "short" anywhere → Short
    if "short" in cleaned.lower():
        return "Short_Leveraged" if "lever" in cleaned.lower() else "Short"
    if "lever" in cleaned.lower():
        return "Long_Leveraged"
    return "Long"


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


def _calc_pnl(entry: float, close_price: float, qty: float, side: int, leverage: float, margin: float):
    """
    Unified P&L calculation.
    - qty = notional / entry  (notional = margin * leverage)
    - raw_pnl = price_move * qty * side  →  already reflects full leveraged exposure
    - pnl_pct uses MARGIN (capital at risk) as the base, which gives the correct ROI
    """
    raw_pnl = (close_price - entry) * qty * side
    pnl_pct = (raw_pnl / margin) * 100 if margin else 0.0
    return raw_pnl, pnl_pct


def open_paper_position(signal: dict, current_price: float = None) -> dict:
    """
    Open a new paper position from a trading signal.
    direction can be: Long, Bounce, Long_Leveraged, Short, Short_Leveraged
    """
    sym = signal.get("asset_symbol", "").upper().strip()
    if not sym:
        return {"error": "No asset_symbol provided"}

    # Normalize direction — handle any LLM output variant
    raw_dir = signal.get("paper_direction") or signal.get("direction") or "Long"
    dir_key = _normalize_direction(raw_dir)

    side, leverage = DIRECTION_LEVERAGE[dir_key]
    entry = float(current_price or signal.get("entry_price") or 0)
    # Try futures price source if still no price
    if (not entry or entry <= 0):
        try:
            from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                fd = get_cached_futures_price(sym)
                if fd:
                    entry = float(fd.get("price") or 0)
        except Exception:
            pass
    if not entry or entry <= 0:
        return {"error": f"No valid entry price for {sym} (got: {current_price}, signal entry: {signal.get('entry_price')})"}

    # Auto-detect asset class (Equity | Crypto | Futures | Forex)
    asset_class_raw = (signal.get("asset_class") or "").lower()
    if "futures" in asset_class_raw or "commodity" in asset_class_raw:
        asset_class = "Futures"
    elif "forex" in asset_class_raw or "currency" in asset_class_raw:
        asset_class = "Forex"
    elif "/" in sym or sym.upper().endswith("USD"):
        asset_class = "Crypto"
    else:
        # Check the futures universe
        try:
            from lib.futures_data import FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                cat = FUTURES_UNIVERSE[sym]["category"]
                asset_class = "Forex" if cat == "Forex" else "Futures"
            else:
                asset_class = "Equity"
        except Exception:
            asset_class = "Equity"

    target = float(signal.get("target_price") or 0)
    stop   = float(signal.get("stop_loss") or 0)

    # Ensure stop/target are on the correct side of entry
    if side == 1:  # Long / Bounce / Long_Leveraged
        if not target or target <= entry:
            target = round(entry * 1.05, 4 if entry < 1 else 2)
        if not stop or stop >= entry:
            stop = round(entry * 0.97, 4 if entry < 1 else 2)
    else:  # Short / Short_Leveraged — stop ABOVE entry, target BELOW entry
        if not target or target >= entry:
            target = round(entry * 0.95, 4 if entry < 1 else 2)
        if not stop or stop <= entry:
            stop = round(entry * 1.03, 4 if entry < 1 else 2)

    # Margin from signal override or per-asset-class defaults
    override_margin = float(signal.get("margin_override") or 0)
    ac_lower = asset_class.lower()
    base_margin = (override_margin if override_margin > 0
                   else ASSET_CLASS_MARGIN.get(ac_lower, DEFAULT_POSITION_SIZE))
    margin   = base_margin
    notional = margin * leverage

    # For low-price instruments (forex pairs typically near 1.0-1.5), scale qty
    # to represent a reasonable notional exposure
    if entry < 2.0 and ac_lower == "forex":
        # Standard lot approach: each 'qty' unit = 1 mini lot ($10k notional)
        qty = round(notional / (entry * 1000), 2) if entry > 0 else 0.0
        qty = max(qty, 0.01)
    else:
        qty = round(notional / entry, 6)

    with get_db() as db:
        existing = db.query(PaperPosition).filter(
            PaperPosition.symbol == sym,
            PaperPosition.status == "Open"
        ).first()
        if existing:
            return {"error": f"Paper position already open for {sym}"}

        portfolio = _get_portfolio_cash(db)
        logger.info(f"[Paper] Cash available: ${portfolio.cash:.2f} | margin required: ${margin:.2f}")
        if portfolio.cash < margin:
            logger.warning(f"[Paper] Insufficient cash — have ${portfolio.cash:.2f}, need ${margin:.2f}")
            return {"error": f"Insufficient paper cash (${portfolio.cash:.0f}) for margin ${margin:.0f}. Use /api/paper/reset to restore $100k."}

        from app.database import new_id
        pos = PaperPosition(
            id            = new_id(),
            symbol        = sym,
            asset_class   = asset_class,
            direction     = dir_key,
            side          = "long" if side == 1 else "short",
            leverage      = leverage,
            qty           = qty,
            entry_price   = entry,
            current_price = entry,
            target_price  = target,
            stop_loss     = stop,
            notional      = notional,
            margin_used   = margin,
            unrealized_pnl= 0.0,
            unrealized_pct= 0.0,
            signal_id     = signal.get("id"),
            status        = "Open",
            opened_at     = _now(),
            updated_at    = _now(),
        )
        db.add(pos)
        portfolio.cash    -= margin
        portfolio.updated_at = _now()

        pos_id   = pos.id
        pos_data = {
            "id": pos_id, "symbol": sym, "direction": dir_key,
            "side": "long" if side == 1 else "short", "leverage": leverage, "qty": qty,
            "entry_price": entry, "target": target, "stop": stop,
            "notional": notional, "margin_required": margin, "asset_class": asset_class,
        }

    logger.info(
        f"[Paper] ✅ Opened {dir_key} on {sym} ({asset_class}) @ ${entry:.4f} | "
        f"qty={qty:.4f} | notional=${notional:.0f} | margin=${margin:.0f} | "
        f"target=${target:.4f} | stop=${stop:.4f}"
    )
    return {"ok": True, "position": pos_data}


def close_paper_position(pos_id: str, close_price: float, reason: str = "manual") -> dict:
    """Close a paper position and record the trade."""
    result = {}
    log_symbol = ""
    log_direction = ""
    log_pnl = 0.0
    log_pct = 0.0

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}

        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at
        pos_notional  = float(pos.notional or 0)
        pos_margin    = float(pos.margin_used or DEFAULT_POSITION_SIZE)

        entry  = float(pos.entry_price)
        qty    = float(pos.qty)
        lev    = float(pos.leverage or 1.0)
        side   = 1 if pos_side == "long" else -1

        pnl, pnl_pct = _calc_pnl(entry, close_price, qty, side, lev, pos_margin)

        portfolio = _get_portfolio_cash(db)
        portfolio.cash         += pos_margin + pnl
        portfolio.realized_pnl  = (portfolio.realized_pnl or 0) + pnl
        portfolio.total_trades  = (portfolio.total_trades or 0) + 1
        if pnl > 0:
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
            realized_pnl = pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        )
        db.add(trade)

        pos.status         = "Closed"
        pos.current_price  = close_price
        pos.unrealized_pnl = pnl
        pos.updated_at     = _now()

        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price
        }
        log_symbol    = pos_symbol
        log_direction = pos_direction
        log_pnl       = pnl
        log_pct       = pnl_pct

    logger.info(f"[Paper] Closed {log_symbol} ({log_direction}) @ ${close_price:.4f} | P&L=${log_pnl:.2f} ({log_pct:.1f}%) | {reason}")
    return result


def mark_to_market(prices: dict) -> dict:
    """
    Update unrealized P&L for all open paper positions.
    prices = {symbol: current_price}
    Auto-triggers stop-loss / take-profit / margin-call checks.
    """
    closed  = []
    updated = []

    with get_db() as db:
        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = [
            {
                "id":           p.id,
                "symbol":       p.symbol,
                "entry_price":  float(p.entry_price or 0),
                "qty":          float(p.qty or 0),
                "side":         p.side or "long",
                "leverage":     float(p.leverage or 1.0),
                "target_price": float(p.target_price or 0),
                "stop_loss":    float(p.stop_loss or 0),
                "notional":     float(p.notional or 0),
                "margin_used":  float(p.margin_used or DEFAULT_POSITION_SIZE),
                "direction":    p.direction or "Long",
            }
            for p in positions
            if p.entry_price and p.qty
        ]

    for pos in pos_list:
        sym = pos["symbol"]
        # Try multiple price lookup variants
        price = (
            prices.get(sym) or
            prices.get(sym.replace("/USD", "")) or
            prices.get(sym.replace("/", "") + "USD") or
            prices.get(sym.replace("/", ""))
        )
        # Futures/Forex fallback — covers GC=F, EURUSD=X, CL=F, etc.
        if not price:
            try:
                from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
                if sym in FUTURES_UNIVERSE:
                    fd = get_cached_futures_price(sym)
                    if fd and fd.get("price"):
                        price = float(fd["price"])
            except Exception:
                pass

        # Also check with and without = suffix (yfinance oddities)
        if not price:
            for alt in [sym.upper(), sym.replace("=X",""), sym.replace("=F","")]:
                if alt in prices and prices[alt]:
                    price = float(prices[alt])
                    break

        if not price:
            logger.debug(f"[Paper] No price in MTM for {sym}")
            continue

        entry  = pos["entry_price"]
        qty    = pos["qty"]
        lev    = pos["leverage"]
        margin = pos["margin_used"]
        side   = 1 if pos["side"] == "long" else -1

        pnl, pct = _calc_pnl(entry, price, qty, side, lev, margin)

        # Trigger checks — MUST respect side direction:
        # LONG:  stop when price falls BELOW stop_loss, profit when price rises ABOVE target
        # SHORT: stop when price rises ABOVE stop_loss, profit when price falls BELOW target
        reason = None
        stop   = pos["stop_loss"]
        target = pos["target_price"]

        if side == 1:   # LONG
            if stop  > 0 and price <= stop:    reason = "stop_loss"
            elif target > 0 and price >= target: reason = "take_profit"
        else:           # SHORT
            if stop  > 0 and price >= stop:    reason = "stop_loss"
            elif target > 0 and price <= target: reason = "take_profit"

        # Margin call: equity in position (margin + pnl) < 15% of original margin (lost 85%)
        equity_in_pos = margin + pnl
        if margin > 0 and equity_in_pos < margin * MARGIN_CALL_THRESHOLD:
            reason = "margin_call"

        if reason:
            result = close_paper_position(pos["id"], price, reason)
            closed.append({"symbol": sym, "reason": reason, "pnl": result.get("pnl")})
        else:
            with get_db() as db:
                p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                if p:
                    p.current_price   = price
                    p.unrealized_pnl  = round(pnl, 2)
                    p.unrealized_pct  = round(pct, 2)
                    p.updated_at      = _now()
            updated.append(sym)

    return {"updated": len(updated), "closed": closed}


def get_paper_summary() -> dict:
    """Return portfolio summary and open positions. Null-safe throughout."""
    with get_db() as db:
        portfolio = _get_portfolio_cash(db)
        p_data = {
            "cash":           round(float(portfolio.cash or 0), 2),
            "total_trades":   int(portfolio.total_trades or 0),
            "winning_trades": int(portfolio.winning_trades or 0),
            "realized_pnl":   round(float(portfolio.realized_pnl or 0), 2),
            "updated_at":     portfolio.updated_at,
        }

        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = []
        for p in positions:
            if not p.entry_price:
                continue
            try:
                pos_list.append({
                    "id":            p.id,
                    "symbol":        p.symbol or "",
                    "direction":     p.direction or "Long",
                    "side":          p.side or "long",
                    "leverage":      float(p.leverage or 1.0),
                    "qty":           float(p.qty or 0),
                    "entry_price":   float(p.entry_price or 0),
                    "current_price": float(p.current_price or p.entry_price or 0),
                    "target_price":  float(p.target_price or 0),
                    "stop_loss":     float(p.stop_loss or 0),
                    "notional":      float(p.notional or 0),
                    "margin_used":   float(p.margin_used or DEFAULT_POSITION_SIZE),
                    "unrealized_pnl":float(p.unrealized_pnl or 0),
                    "unrealized_pct":float(p.unrealized_pct or 0),
                    "opened_at":     p.opened_at.isoformat() if hasattr(p.opened_at, "isoformat") else (p.opened_at or ""),
                    "asset_class":   p.asset_class or "Equity",
                    "signal_id":     p.signal_id or "",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad position row {p.id}: {e}")

        trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(50).all()
        trade_list = []
        for t in trades:
            try:
                trade_list.append({
                    "id":           t.id,
                    "symbol":       t.symbol or "",
                    "direction":    t.direction or "Long",
                    "side":         t.side or "long",
                    "leverage":     float(t.leverage or 1.0),
                    "entry_price":  float(t.entry_price or 0),
                    "exit_price":   float(t.exit_price or 0),
                    "realized_pnl": round(float(t.realized_pnl or 0), 2),
                    "pnl_pct":      round(float(t.pnl_pct or 0), 2),
                    "close_reason": t.close_reason or "manual",
                    "opened_at":    t.opened_at.isoformat() if hasattr(t.opened_at, "isoformat") else (t.opened_at or ""),
                    "closed_at":    t.closed_at.isoformat() if hasattr(t.closed_at, "isoformat") else (t.closed_at or ""),
                    "asset_class":  t.asset_class or "Equity",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad trade row {t.id}: {e}")

    # Join signal data for paper positions (for TA/reasoning/news display)
    signal_ids = [p["signal_id"] for p in pos_list if p.get("signal_id")]
    signal_map = {}
    if signal_ids:
        try:
            from app.database import TradingSignal
            sigs = db.query(TradingSignal).filter(TradingSignal.id.in_(signal_ids)).all()
            for s in sigs:
                signal_map[s.id] = {
                    "id":             s.id,
                    "direction":      s.direction or "Long",
                    "confidence":     float(s.confidence or 0),
                    "composite_score":float(getattr(s, "composite_score", None) or s.confidence or 0),
                    "timeframe":      s.timeframe or "",
                    "reasoning":      s.reasoning or "",
                    "key_risks":      getattr(s, "key_risks", None) or "",
                    "momentum":       getattr(s, "momentum", None) or "",
                    "signal_source":  getattr(s, "signal_source", None) or "watchlist",
                    "generated_at":   s.generated_at.isoformat() if hasattr(s.generated_at, "isoformat") else (s.generated_at or ""),
                    "entry_price":    float(s.entry_price or 0),
                    "target_price":   float(s.target_price or 0),
                    "stop_loss":      float(s.stop_loss or 0),
                    "status":         s.status or "",
                    "trigger_event":  getattr(s, "trigger_event", None) or "",
                }
        except Exception as e:
            logger.warning(f"[Paper] Could not join signals: {e}")

    # Attach signal context to each position
    for p in pos_list:
        p["signal"] = signal_map.get(p.get("signal_id"), None)

    open_pnl  = sum(p["unrealized_pnl"] for p in pos_list)
    margin_in = sum(p["margin_used"] for p in pos_list)
    # Equity = cash on hand + margin deployed + any unrealized gains/losses
    # (margin is still your capital — just locked in positions, not lost)
    equity    = p_data["cash"] + margin_in + open_pnl
    total     = p_data["total_trades"]
    wins      = p_data["winning_trades"]
    win_rate  = round(wins / total * 100, 1) if total > 0 else 0.0

    return {
        "portfolio": {
            **p_data,
            "open_pnl":          round(open_pnl, 2),
            "equity":            round(equity, 2),
            "margin_in_use":     round(margin_in, 2),
            "win_rate":          win_rate,
            "starting_capital":  PAPER_STARTING_CAPITAL,
            "total_return_pct":  round((equity - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100, 2),
        },
        "positions": pos_list,
        "trades":    trade_list,
    }


def reset_paper_portfolio() -> dict:
    """Reset the paper portfolio back to $100k starting capital."""
    from app.database import new_id
    with get_db() as db:
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
    logger.info("[Paper] Portfolio hard reset to $100,000")
    return {"ok": True, "cash": PAPER_STARTING_CAPITAL}
