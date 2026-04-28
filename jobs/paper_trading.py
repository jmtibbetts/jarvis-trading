"""
Job: Paper Trading v1.0
- Routes signals with paper_mode=True to the paper engine
- Generates paper signals for Short / Leveraged directions
- Runs mark-to-market on open paper positions every cycle
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, MarketAsset

logger = logging.getLogger(__name__)

PAPER_DIRECTIONS = ["Short", "Short_Leveraged", "Long_Leveraged"]

def _get_current_price(symbol: str) -> float:
    """Get current price from market_assets cache."""
    with get_db() as db:
        asset = db.query(MarketAsset).filter(MarketAsset.symbol == symbol).first()
        if asset and asset.price:
            return float(asset.price)
    return 0.0

def _get_all_prices() -> dict:
    """Return all current prices from market_assets cache."""
    with get_db() as db:
        assets = db.query(MarketAsset).all()
        return {a.symbol: float(a.price) for a in assets if a.price}

def run():
    logger.info("[PaperTrading] Starting paper trading job...")
    from lib.paper_engine import mark_to_market, open_paper_position, get_paper_summary

    # Step 1: Mark-to-market all open positions
    prices = _get_all_prices()
    mtm = mark_to_market(prices)
    logger.info(f"[PaperTrading] MTM: updated={mtm['updated']} | auto-closed={len(mtm['closed'])}")

    for c in mtm.get("closed", []):
        logger.info(f"[PaperTrading] Auto-closed {c['symbol']} via {c['reason']} | P&L=${c.get('pnl', 0):.2f}")

    # Step 2: Check for new Active signals that should have paper positions
    with get_db() as db:
        # Get signals flagged for paper trading that don't have a paper position yet
        active_paper = db.query(TradingSignal).filter(
            TradingSignal.status == "Active",
            TradingSignal.paper_mode == True,
        ).order_by(TradingSignal.generated_at.desc()).limit(20).all()

        sig_list = [
            {
                "id": s.id, "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
                "direction": s.direction, "paper_direction": s.paper_direction,
                "entry_price": s.entry_price, "target_price": s.target_price,
                "stop_loss": s.stop_loss,
            }
            for s in active_paper
        ]

    executed = 0
    for sig in sig_list:
        sym   = sig["asset_symbol"]
        price = _get_current_price(sym) or sig.get("entry_price")
        if not price:
            logger.warning(f"[PaperTrading] No price for {sym} — skipping")
            continue

        result = open_paper_position(sig, current_price=price)
        if result.get("ok"):
            executed += 1
            logger.info(f"[PaperTrading] Opened paper position: {sym} @ ${price:.4f}")
            # Mark signal as PaperExecuted
            with get_db() as db:
                s = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if s:
                    s.status = "PaperExecuted"
                    s.updated_date = datetime.now(timezone.utc).isoformat()
        else:
            logger.warning(f"[PaperTrading] Could not open {sym}: {result.get('error')}")

    summary = get_paper_summary()
    port = summary["portfolio"]
    logger.info(
        f"[PaperTrading] Summary — Equity=${port['equity']:.0f} | Cash=${port['cash']:.0f} | "
        f"Open={len(summary['positions'])} | Realized P&L=${port['realized_pnl']:.2f} | "
        f"Win%={port['win_rate']}% | Total={port['total_trades']}"
    )
    return {"ok": True, "mtm": mtm, "new_positions": executed, "summary": port}
