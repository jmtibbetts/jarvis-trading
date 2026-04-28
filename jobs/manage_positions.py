"""
Job: Manage Positions v6.4
- Separate tier tables for crypto vs equity
- Crypto: stop at -4%, trail at +2%/+5%, take profit at +10%
- Equity: stop at -5%, trail at +5%/+10%, take profit at +15%
- Crypto trails use tighter 3%/5% vs equity 5%/8%
"""
import logging, uuid
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, Position, PortfolioSnapshot
from lib.alpaca_client import get_positions, get_account, close_position, get_trading_client, normalize_symbol

logger = logging.getLogger(__name__)

# ── Tier thresholds ────────────────────────────────────────────────────────────
# Separate tiers for crypto (24/7, volatile) vs equity (session-based, calmer)
# Crypto: tighter stops, smaller profit targets (moves fast, can reverse fast)
# Equity: wider stops to survive intraday noise, bigger targets for trend plays

TIERS_CRYPTO = [
    {"min_gain": 10.0, "max_gain": None,  "action": "close",          "label": ">=10% -- take profit"},
    {"min_gain":  5.0, "max_gain": 10.0,  "action": "trail_tight",    "label": "5-10% -- trail 3%"},
    {"min_gain":  2.0, "max_gain":  5.0,  "action": "trail_moderate",  "label": "2-5% -- trail 5%"},
    {"min_gain": None, "max_gain": -4.0,  "action": "close",          "label": "<=-4% -- cut loss"},
]

TIERS_EQUITY = [
    {"min_gain": 15.0, "max_gain": None,  "action": "close",          "label": ">=15% -- take profit"},
    {"min_gain": 10.0, "max_gain": 15.0,  "action": "trail_tight",    "label": "10-15% -- trail 5%"},
    {"min_gain":  5.0, "max_gain": 10.0,  "action": "trail_moderate",  "label": "5-10% -- trail 8%"},
    {"min_gain": None, "max_gain": -5.0,  "action": "close",          "label": "<=-5% -- cut loss"},
]

# Legacy alias — not used directly anymore but keep for any external references
TIERS = TIERS_EQUITY

def _tier(plpc: float, is_crypto: bool = False):
    """Return the matching tier dict, or None if in the hold zone."""
    tiers = TIERS_CRYPTO if is_crypto else TIERS_EQUITY
    for t in tiers:
        mg, xg = t["min_gain"], t["max_gain"]
        if mg is not None and xg is not None:
            if mg <= plpc < xg: return t
        elif mg is not None and xg is None:
            if plpc >= mg: return t
        elif mg is None and xg is not None:
            if plpc <= xg: return t
    return None


def _is_crypto(sym: str) -> bool:
    return "/" in sym or sym.endswith("USD")


def _cancel_open_orders(client, sym: str):
    """Cancel any existing open orders for a symbol before placing new protective order."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]))
        for o in open_orders:
            try:
                client.cancel_order_by_id(o.id)
                logger.debug(f"[Positions] Cancelled open order {o.id} for {sym}")
            except Exception as ce:
                logger.debug(f"[Positions] Could not cancel order {o.id}: {ce}")
    except Exception as ce:
        logger.debug(f"[Positions] Cancel orders check failed for {sym}: {ce}")


def _set_trailing_stop_equity(client, sym: str, qty: float, trail_pct: float) -> bool:
    """Place a native trailing stop order for equities."""
    try:
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = TrailingStopOrderRequest(
            symbol=sym,
            qty=max(1, int(qty)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=trail_pct,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] ✓ Equity trailing stop set — {sym} trail={trail_pct}% | order_id={order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Equity trailing stop failed for {sym}: {e}")
        return False


def _set_crypto_limit_stop(client, sym: str, qty: float, current_price: float, trail_pct: float) -> bool:
    """
    Crypto only supports market/limit orders — no trailing stops.
    Simulate a trailing stop floor by placing a limit sell at current_price * (1 - trail_pct/100).
    Cancels any existing open orders first to avoid stacking.
    """
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        limit_price = round(current_price * (1.0 - trail_pct / 100.0), 6)
        # Round qty to reasonable crypto precision
        qty_rounded = round(qty, 8)

        req = LimitOrderRequest(
            symbol=sym,
            qty=qty_rounded,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=limit_price,
        )
        order = client.submit_order(req)
        logger.info(
            f"[Positions] ✓ Crypto limit-stop set — {sym} floor=${limit_price:.4f} "
            f"(trail sim {trail_pct}% below ${current_price:.4f}) | order_id={order.id}"
        )
        return True
    except Exception as e:
        logger.warning(f"[Positions] Crypto limit-stop failed for {sym}: {e}")
        return False


def _set_protective_order(sym: str, qty: float, trail_pct: float, current_price: float):
    """Dispatch to correct order type based on asset class."""
    try:
        client = get_trading_client()
        _cancel_open_orders(client, sym)

        if _is_crypto(sym):
            return _set_crypto_limit_stop(client, sym, qty, current_price, trail_pct)
        else:
            return _set_trailing_stop_equity(client, sym, qty, trail_pct)
    except Exception as e:
        logger.warning(f"[Positions] Protective order failed for {sym}: {e}")
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
            sym          = str(pos.symbol)
            qty          = float(pos.qty or 0)
            avg          = float(pos.avg_entry_price or 0)
            mv           = float(pos.market_value or 0)
            pl           = float(pos.unrealized_pl or 0)
            plpc         = float(pos.unrealized_plpc or 0) * 100
            side         = str(pos.side)
            current_price = float(pos.current_price or avg or 0)

            logger.info(f"[Positions] {sym:12} {plpc:+6.1f}% | MV=${mv:>10,.0f} | P&L=${pl:>+8,.0f} | qty={qty}")

            db.add(Position(
                symbol=sym, qty=qty, avg_entry=avg,
                market_value=mv, unrealized_pl=pl, unrealized_plpc=plpc,
                side=side,
                asset_class="Crypto" if _is_crypto(sym) else "Equity",
                updated_at=now_iso
            ))

            tier = _tier(plpc, is_crypto=_is_crypto(sym))
            if tier is None:
                logger.info(f"[Positions] {sym:12} holding — no tier action ({plpc:+.1f}%)")
                continue

            if tier["action"] == "close":
                try:
                    close_position(sym)
                    logger.info(f"[Positions] ✓ Closed {sym} @ {plpc:+.1f}% | {tier['label']}")
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
                # Crypto uses tighter trails; equity uses wider ones
                if _is_crypto(sym):
                    trail_pct = 3.0 if tier["action"] == "trail_tight" else 5.0
                else:
                    trail_pct = 5.0 if tier["action"] == "trail_tight" else 8.0
                logger.info(
                    f"[Positions] ⟳ Trail {sym} @ {plpc:+.1f}% — "
                    f"{'limit-stop' if _is_crypto(sym) else 'trailing stop'} {trail_pct}% | {tier['label']}"
                )
                ok = _set_protective_order(sym, qty, trail_pct, current_price)
                if ok:
                    trailing += 1

        db.add(PortfolioSnapshot(
            id=str(uuid.uuid4()),
            equity=equity, cash=cash,
            market_value=total_mv, unrealized_pl=total_pl,
            position_count=len(positions),
            snapshot_at=now_iso
        ))

    logger.info(f"[Positions] Done -- {closed} closed, {trailing} protective orders set | equity=${equity:,.2f}")
    return {"closed": closed, "trailing": trailing, "total": len(positions), "equity": equity}
