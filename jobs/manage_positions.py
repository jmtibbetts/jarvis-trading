"""
Job: Manage Positions v6.2
- Logs ALL positions every cycle (not just ones hitting a tier)
- Actually submits trailing stop adjustments to Alpaca (was only logging before)
- Cancels existing open orders for position before placing new trail
- Fixed: removed non-existent CancelOrderRequest import
"""
import logging, uuid
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, Position, PortfolioSnapshot
from lib.alpaca_client import get_positions, get_account, close_position, get_trading_client, normalize_symbol

logger = logging.getLogger(__name__)

TIERS = [
    {"min_gain": 15.0, "max_gain": None,  "action": "close",         "label": ">=15% -- close"},
    {"min_gain": 10.0, "max_gain": 15.0,  "action": "trail_tight",   "label": "10-15% -- trail 5%"},
    {"min_gain":  5.0, "max_gain": 10.0,  "action": "trail_moderate", "label": "5-10% -- trail 8%"},
    {"min_gain": None, "max_gain": -8.0,  "action": "close",         "label": "<=-8% -- cut loss"},
]

def _tier(plpc):
    for t in TIERS:
        mg, xg = t["min_gain"], t["max_gain"]
        if mg is not None and xg is not None:
            if mg <= plpc < xg: return t
        elif mg is not None and xg is None:
            if plpc >= mg: return t
        elif mg is None and xg is not None:
            if plpc <= xg: return t
    return None


def _set_trailing_stop(sym: str, qty: float, trail_pct: float):
    """
    Cancel any existing open orders for sym then place a new trailing stop.
    Works for both crypto and equity.
    Uses only alpaca-py methods that actually exist:
      - client.get_orders() with GetOrdersRequest
      - client.cancel_order_by_id(order_id)  <-- no CancelOrderRequest needed
      - client.submit_order() with TrailingStopOrderRequest
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest, TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
        client = get_trading_client()

        # Cancel any open orders for this symbol first
        try:
            open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]))
            for o in open_orders:
                try:
                    client.cancel_order_by_id(o.id)
                    logger.debug(f"[Positions] Cancelled open order {o.id} for {sym}")
                except Exception as ce:
                    logger.debug(f"[Positions] Could not cancel order {o.id}: {ce}")
        except Exception as ce:
            logger.debug(f"[Positions] Cancel orders check failed for {sym}: {ce}")

        # Submit trailing stop
        req = TrailingStopOrderRequest(
            symbol=sym,
            qty=round(qty, 8) if ("/" in sym or sym.endswith("USD")) else max(1, int(qty)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=trail_pct,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] \u2713 Trailing stop set \u2014 {sym} trail={trail_pct}% | order_id={order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Trailing stop failed for {sym}: {e}")
        return False


def run():
    logger.info("[Positions] Running position management...")
    try:
        positions = get_positions()
        account   = get_account()
        equity    = float(account.equity)
        cash      = float(account.cash)
    except Exception as e:
        logger.error(f"[Positions] Alpaca error: {e}")
        return {"error": str(e)}

    now_iso   = datetime.now(timezone.utc).isoformat()
    closed    = 0
    trailing  = 0
    total_mv  = sum(float(p.market_value or 0) for p in positions)
    total_pl  = sum(float(p.unrealized_pl or 0) for p in positions)

    logger.info(f"[Positions] {len(positions)} open positions | total MV=${total_mv:,.0f} | P&L=${total_pl:+,.0f}")

    with get_db() as db:
        db.query(Position).delete()

        for pos in positions:
            sym   = str(pos.symbol)
            qty   = float(pos.qty or 0)
            avg   = float(pos.avg_entry_price or 0)
            mv    = float(pos.market_value or 0)
            pl    = float(pos.unrealized_pl or 0)
            plpc  = float(pos.unrealized_plpc or 0) * 100
            side  = str(pos.side)

            logger.info(f"[Positions] {sym:12} {plpc:+6.1f}% | MV=${mv:>10,.0f} | P&L=${pl:>+8,.0f} | qty={qty}")

            db.add(Position(
                symbol=sym, qty=qty, avg_entry=avg,
                market_value=mv, unrealized_pl=pl, unrealized_plpc=plpc,
                side=side,
                asset_class="Crypto" if ("/" in sym or sym.endswith("USD")) else "Equity",
                updated_at=now_iso
            ))

            tier = _tier(plpc)
            if tier is None:
                logger.info(f"[Positions] {sym:12} holding \u2014 no tier action ({plpc:+.1f}%)")
                continue

            if tier["action"] == "close":
                try:
                    close_position(sym)
                    logger.info(f"[Positions] \u2713 Closed {sym} @ {plpc:+.1f}% | {tier['label']}")
                    sig = db.query(TradingSignal).filter(
                        TradingSignal.asset_symbol.in_([sym, sym.replace("/", "")]),
                        TradingSignal.status == "Executed"
                    ).first()
                    if sig:
                        sig.status = "Closed"
                        sig.updated_date = now_iso
                    closed += 1
                except Exception as e:
                    logger.error(f"[Positions] Close {sym} failed: {e}")

            else:
                trail_pct = 5.0 if tier["action"] == "trail_tight" else 8.0
                logger.info(f"[Positions] \u27f3 Trail {sym} @ {plpc:+.1f}% \u2014 submitting trail {trail_pct}% | {tier['label']}")
                ok = _set_trailing_stop(sym, qty, trail_pct)
                if ok:
                    trailing += 1

        db.add(PortfolioSnapshot(
            id=str(uuid.uuid4()),
            equity=equity, cash=cash,
            market_value=total_mv, unrealized_pl=total_pl,
            position_count=len(positions),
            snapshot_at=now_iso
        ))

    logger.info(f"[Positions] Done -- {closed} closed, {trailing} trailing stops set | equity=${equity:,.2f}")
    return {"closed": closed, "trailing": trailing, "total": len(positions), "equity": equity}
