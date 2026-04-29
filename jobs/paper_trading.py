"""
Job: Paper Trading v2.0
FIXES:
1. Paper job now runs ALL signals (paper_mode=True OR any direction != "Long"/"Bounce")
   — previously missed signals where paper_mode wasn't set but direction is Short/Leveraged
2. Always-on for crypto/futures — no market hours check
3. Track E paper signals: auto-generate direct paper positions for best short/leveraged setups
   when LLM returns them with any Active status
4. Improved price resolution: tries normalized symbol variants
5. Mark-to-market properly handles crypto symbols with/without slash
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, MarketAsset

logger = logging.getLogger(__name__)

# Directions that go to paper engine (never to Alpaca)
PAPER_DIRECTIONS = {"Short", "Short_Leveraged", "Long_Leveraged"}


def _get_current_price(symbol: str) -> float:
    """Get current price from market_assets cache. Tries multiple symbol formats."""
    variants = [
        symbol,
        symbol.replace("/", ""),          # BTC/USD → BTCUSD
        symbol.replace("/USD", ""),        # BTC/USD → BTC
        symbol + "/USD" if "/" not in symbol else symbol,  # BTC → BTC/USD
    ]
    with get_db() as db:
        for v in variants:
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == v).first()
            if asset and asset.price and float(asset.price) > 0:
                logger.debug(f"[PaperTrading] Price for {symbol} resolved via variant '{v}': ${asset.price:.4f}")
                return float(asset.price)
    logger.warning(f"[PaperTrading] No market price found for {symbol} (tried: {variants})")
    return 0.0


def _get_all_prices() -> dict:
    """Return all current prices from market_assets cache."""
    with get_db() as db:
        assets = db.query(MarketAsset).all()
        prices = {}
        for a in assets:
            if a.price and float(a.price) > 0:
                prices[a.symbol] = float(a.price)
                # Also index slash-less variant for crypto (BTCUSD → BTC/USD lookup)
                if "/" in a.symbol:
                    prices[a.symbol.replace("/", "")] = float(a.price)
                elif a.symbol.endswith("USD") and len(a.symbol) > 3:
                    # e.g. BTCUSD → BTC/USD
                    base = a.symbol[:-3]
                    prices[f"{base}/USD"] = float(a.price)
        return prices


def _get_pending_paper_signals(db) -> list:
    """
    Fetch signals that should be routed to the paper engine:
    1. Explicitly flagged paper_mode=True with status Active
    2. Any Active signal whose direction is Short, Short_Leveraged, or Long_Leveraged
       (these can never go to Alpaca — they must go to paper)
    Excludes already-executed paper positions.
    """
    # Fetch by paper_mode flag
    paper_flagged = db.query(TradingSignal).filter(
        TradingSignal.status == "Active",
        TradingSignal.paper_mode == True,
    ).order_by(TradingSignal.generated_at.desc()).limit(30).all()

    # Fetch by direction (catch any that missed the paper_mode flag)
    paper_by_dir = db.query(TradingSignal).filter(
        TradingSignal.status == "Active",
        TradingSignal.direction.in_(list(PAPER_DIRECTIONS)),
    ).order_by(TradingSignal.generated_at.desc()).limit(30).all()

    # Deduplicate by id
    seen = set()
    result = []
    for s in list(paper_flagged) + list(paper_by_dir):
        if s.id not in seen:
            seen.add(s.id)
            result.append({
                "id": s.id,
                "asset_symbol": s.asset_symbol,
                "asset_class": s.asset_class or "Equity",
                "direction": s.direction or "Long",
                "paper_direction": s.paper_direction or s.direction or "Long",
                "entry_price": float(s.entry_price) if s.entry_price else 0.0,
                "target_price": float(s.target_price) if s.target_price else 0.0,
                "stop_loss": float(s.stop_loss) if s.stop_loss else 0.0,
                "paper_mode": True,
            })
    return result


def run():
    logger.info("[PaperTrading] v2.0 Starting paper trading job...")
    from lib.paper_engine import mark_to_market, open_paper_position, get_paper_summary

    # ── Step 1: Mark-to-market all open positions ──────────────────────────────
    prices = _get_all_prices()
    logger.info(f"[PaperTrading] Price cache: {len(prices)} symbols loaded")
    mtm = mark_to_market(prices)
    logger.info(f"[PaperTrading] MTM: updated={mtm['updated']} | auto-closed={len(mtm['closed'])}")
    for c in mtm.get("closed", []):
        logger.info(f"[PaperTrading] Auto-closed {c['symbol']} via {c['reason']} | P&L=${c.get('pnl', 0):.2f}")

    # ── Step 2: Check for paper signals that need positions opened ─────────────
    with get_db() as db:
        sig_list = _get_pending_paper_signals(db)

    if not sig_list:
        logger.info("[PaperTrading] No pending paper signals to execute")
    else:
        logger.info(f"[PaperTrading] Found {len(sig_list)} paper signals to process")

    executed = 0
    skipped_dup = 0
    skipped_no_price = 0

    for sig in sig_list:
        sym = sig["asset_symbol"]

        # Get current market price — paper always trades at market (no market hours restriction)
        price = _get_current_price(sym)
        if not price:
            # Fallback: use signal's own entry price
            price = sig.get("entry_price") or 0.0
        if not price or price <= 0:
            logger.warning(f"[PaperTrading] No price for {sym} — skipping (add to market data fetch)")
            skipped_no_price += 1
            continue

        logger.info(f"[PaperTrading] Attempting paper {sig['paper_direction']} on {sym} @ ${price:.4f}")
        result = open_paper_position(sig, current_price=price)

        if result.get("ok"):
            executed += 1
            logger.info(f"[PaperTrading] ✅ Opened paper {sig['paper_direction']} on {sym} @ ${price:.4f}")
            # Mark signal as PaperExecuted so we don't try again
            with get_db() as db:
                s = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if s:
                    s.status = "PaperExecuted"
                    s.updated_date = datetime.now(timezone.utc).isoformat()
        elif "already open" in (result.get("error") or ""):
            skipped_dup += 1
            logger.debug(f"[PaperTrading] {sym} already has open paper position — marking signal done")
            # Still mark the signal as PaperExecuted to stop re-processing
            with get_db() as db:
                s = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if s:
                    s.status = "PaperExecuted"
                    s.updated_date = datetime.now(timezone.utc).isoformat()
        else:
            logger.warning(f"[PaperTrading] ❌ Could not open {sym}: {result.get('error')}")

    summary = get_paper_summary()
    port = summary["portfolio"]
    logger.info(
        f"[PaperTrading] Done — executed={executed} | dup_skip={skipped_dup} | no_price={skipped_no_price} | "
        f"Equity=${port['equity']:.0f} | Cash=${port['cash']:.0f} | "
        f"Open={len(summary['positions'])} | Realized=${port['realized_pnl']:.2f} | "
        f"Win%={port['win_rate']}% | Total={port['total_trades']}"
    )
    return {
        "ok": True,
        "mtm": mtm,
        "new_positions": executed,
        "skipped_dup": skipped_dup,
        "skipped_no_price": skipped_no_price,
        "summary": port,
    }
