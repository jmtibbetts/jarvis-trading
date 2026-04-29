"""
Job: Paper Trading v3.0
Changes from v2.0:
- Now mirrors ALL active signals to paper (Long, Bounce, Short, Leveraged)
- Long/Bounce signals open as paper Long positions (same as Alpaca would)
- Short/Leveraged signals open as paper Short/Leveraged (can't go to Alpaca)
- One paper position per symbol max — deduplication prevents double-opens
- Signals already Executed on Alpaca still get a paper mirror (tracks virtual vs real)
- Cap at 20 concurrent open paper positions to avoid over-loading the virtual account
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, MarketAsset, PaperPosition

logger = logging.getLogger(__name__)

MAX_PAPER_POSITIONS = 20   # cap on concurrent virtual positions


def _get_current_price(symbol: str) -> float:
    """Get current price from market_assets cache. Tries multiple symbol formats."""
    variants = [
        symbol,
        symbol.replace("/", ""),
        symbol.replace("/USD", ""),
        symbol + "/USD" if "/" not in symbol else symbol,
    ]
    with get_db() as db:
        for v in variants:
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == v).first()
            if asset and asset.price and float(asset.price) > 0:
                logger.debug(f"[PaperTrading] Price for {symbol} via '{v}': ${asset.price:.4f}")
                return float(asset.price)
    logger.warning(f"[PaperTrading] No market price for {symbol} (tried: {variants})")
    return 0.0


def _get_all_prices() -> dict:
    """Return all current prices from market_assets cache."""
    with get_db() as db:
        assets = db.query(MarketAsset).all()
        prices = {}
        for a in assets:
            if a.price and float(a.price) > 0:
                prices[a.symbol] = float(a.price)
                if "/" in a.symbol:
                    prices[a.symbol.replace("/", "")] = float(a.price)
                elif a.symbol.endswith("USD") and len(a.symbol) > 3:
                    base = a.symbol[:-3]
                    prices[f"{base}/USD"] = float(a.price)
        return prices


def _get_open_paper_symbols() -> set:
    """Return set of symbols that already have an open paper position."""
    with get_db() as db:
        rows = db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all()
        return {r.symbol for r in rows}


def _count_open_paper_positions() -> int:
    """Return count of currently open paper positions."""
    with get_db() as db:
        return db.query(PaperPosition).filter(PaperPosition.status == "Open").count()


def _get_all_pending_signals(db) -> list:
    """
    Fetch ALL active signals to mirror in paper:
    - Active signals (not yet executed anywhere)
    - Executed signals (already ran on Alpaca — mirror them in paper too)
    Excludes PaperExecuted (already has a paper position).
    One position per symbol — deduplication handled by open_paper_positions check.
    """
    eligible_statuses = ["Active", "Executed", "PendingApproval"]

    signals = db.query(TradingSignal).filter(
        TradingSignal.status.in_(eligible_statuses),
    ).order_by(TradingSignal.generated_at.desc()).limit(50).all()

    seen_symbols = set()
    result = []
    for s in signals:
        sym = (s.asset_symbol or "").upper().strip()
        if not sym or sym in seen_symbols:
            continue
        seen_symbols.add(sym)

        # Determine paper direction — use signal direction as-is
        # Long/Bounce → paper Long, Short → paper Short, Leveraged → paper Leveraged
        direction = s.direction or "Long"

        result.append({
            "id":            s.id,
            "asset_symbol":  sym,
            "asset_class":   s.asset_class or "Equity",
            "direction":     direction,
            "paper_direction": direction,
            "entry_price":   float(s.entry_price) if s.entry_price else 0.0,
            "target_price":  float(s.target_price) if s.target_price else 0.0,
            "stop_loss":     float(s.stop_loss) if s.stop_loss else 0.0,
            "signal_status": s.status,
        })
    return result


def run():
    logger.info("[PaperTrading] v3.0 Starting paper trading job...")
    from lib.paper_engine import mark_to_market, open_paper_position, get_paper_summary

    # ── Step 1: Mark-to-market all open positions ──────────────────────────────
    prices = _get_all_prices()
    logger.info(f"[PaperTrading] Price cache: {len(prices)} symbols loaded")
    mtm = mark_to_market(prices)
    logger.info(f"[PaperTrading] MTM: updated={mtm['updated']} | auto-closed={len(mtm['closed'])}")
    for c in mtm.get("closed", []):
        logger.info(f"[PaperTrading] Auto-closed {c['symbol']} via {c['reason']} | P&L=${c.get('pnl', 0):.2f}")

    # ── Step 2: Check position cap ─────────────────────────────────────────────
    open_count = _count_open_paper_positions()
    if open_count >= MAX_PAPER_POSITIONS:
        logger.info(f"[PaperTrading] At cap ({open_count}/{MAX_PAPER_POSITIONS} open) — skipping new opens")
        summary = get_paper_summary()
        return {"ok": True, "mtm": mtm, "new_positions": 0, "cap_hit": True, "summary": summary["portfolio"]}

    # ── Step 3: Fetch all signals eligible for paper mirroring ────────────────
    with get_db() as db:
        sig_list = _get_all_pending_signals(db)

    # Filter out symbols already open in paper
    open_syms = _get_open_paper_symbols()
    sig_list = [s for s in sig_list if s["asset_symbol"] not in open_syms]

    slots_available = MAX_PAPER_POSITIONS - open_count
    sig_list = sig_list[:slots_available]  # Don't exceed cap

    if not sig_list:
        logger.info("[PaperTrading] No new signals to mirror in paper")
    else:
        logger.info(f"[PaperTrading] Mirroring {len(sig_list)} signals to paper (slots: {slots_available})")

    executed = 0
    skipped_no_price = 0

    for sig in sig_list:
        sym = sig["asset_symbol"]

        price = _get_current_price(sym)
        if not price:
            price = sig.get("entry_price") or 0.0
        if not price or price <= 0:
            logger.warning(f"[PaperTrading] No price for {sym} — skipping")
            skipped_no_price += 1
            continue

        logger.info(f"[PaperTrading] Opening paper {sig['paper_direction']} on {sym} @ ${price:.4f} (signal was: {sig['signal_status']})")
        result = open_paper_position(sig, current_price=price)

        if result.get("ok"):
            executed += 1
            logger.info(f"[PaperTrading] ✅ Paper {sig['paper_direction']} opened on {sym} @ ${price:.4f}")
        elif "already open" in (result.get("error") or ""):
            logger.debug(f"[PaperTrading] {sym} already has paper position — skipping")
        else:
            logger.warning(f"[PaperTrading] ❌ Could not open {sym}: {result.get('error')}")

    summary = get_paper_summary()
    port = summary["portfolio"]
    logger.info(
        f"[PaperTrading] Done — new={executed} | no_price={skipped_no_price} | "
        f"Equity=${port['equity']:.0f} | Cash=${port['cash']:.0f} | "
        f"Open={len(summary['positions'])} | Realized=${port['realized_pnl']:.2f} | "
        f"Win%={port['win_rate']}% | Total={port['total_trades']}"
    )
    return {
        "ok": True,
        "mtm": mtm,
        "new_positions": executed,
        "skipped_no_price": skipped_no_price,
        "summary": port,
    }
