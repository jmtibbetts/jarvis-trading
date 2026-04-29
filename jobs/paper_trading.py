"""
Job: Paper Trading v4.0
Changes from v3.0:
- Removed hard 20-position cap — positions limited only by available virtual cash
- Paper positions now go through full LLM + TA evaluation before opening
- Same analysis pipeline as real trades: multi-timeframe TA, news sentiment, LLM scoring
- Low-confidence signals (LLM score < 55) are skipped in paper just like real trading
- One paper position per symbol max — deduplication prevents double-opens
- Long/Bounce signals open as paper Long, Short/Leveraged as paper Short/Leveraged
"""
import logging
from datetime import datetime, timezone
from app.database import get_db, TradingSignal, MarketAsset, PaperPosition, NewsItem, ThreatEvent

logger = logging.getLogger(__name__)

PAPER_MIN_CONFIDENCE = 55   # skip signals scoring below this in LLM re-eval


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


def _get_all_pending_signals(db) -> list:
    """
    Fetch ALL active signals to mirror in paper.
    Excludes PaperExecuted (already has a paper position).
    """
    eligible_statuses = ["Active", "Executed", "PendingApproval"]
    signals = db.query(TradingSignal).filter(
        TradingSignal.status.in_(eligible_statuses),
    ).order_by(TradingSignal.generated_at.desc()).limit(100).all()

    seen_symbols = set()
    result = []
    for s in signals:
        sym = (s.asset_symbol or "").upper().strip()
        if not sym or sym in seen_symbols:
            continue
        seen_symbols.add(sym)
        direction = s.direction or "Long"
        result.append({
            "id":            s.id,
            "asset_symbol":  sym,
            "asset_name":    s.asset_name or sym,
            "asset_class":   s.asset_class or "Equity",
            "direction":     direction,
            "paper_direction": direction,
            "entry_price":   float(s.entry_price) if s.entry_price else 0.0,
            "target_price":  float(s.target_price) if s.target_price else 0.0,
            "stop_loss":     float(s.stop_loss) if s.stop_loss else 0.0,
            "confidence":    float(s.confidence) if s.confidence else 50.0,
            "reasoning":     s.reasoning or "",
            "signal_status": s.status,
        })
    return result


def _evaluate_signal_with_ai(sig: dict, current_price: float) -> dict:
    """
    Run full LLM + TA evaluation on a signal before paper-opening.
    Returns {"approved": bool, "score": float, "reasoning": str}
    """
    sym = sig["asset_symbol"]

    # ── TA analysis ────────────────────────────────────────────────────────────
    ta_summary = ""
    try:
        from lib.ta_engine import analyze_symbol
        ta_data = analyze_symbol(sym)
        if ta_data:
            ta_summary = (
                f"RSI(14)={ta_data.get('rsi_14', 'N/A')} | "
                f"EMA20={ta_data.get('ema_20', 'N/A')} | EMA50={ta_data.get('ema_50', 'N/A')} | "
                f"MACD={ta_data.get('macd', 'N/A')} | Signal={ta_data.get('macd_signal', 'N/A')} | "
                f"BB_upper={ta_data.get('bb_upper', 'N/A')} BB_lower={ta_data.get('bb_lower', 'N/A')} | "
                f"ATR={ta_data.get('atr', 'N/A')} | Trend={ta_data.get('trend', 'N/A')} | "
                f"Regime={ta_data.get('regime', 'N/A')}"
            )
    except Exception as e:
        logger.debug(f"[PaperTrading] TA failed for {sym}: {e}")
        ta_summary = "TA unavailable"

    # ── News context ───────────────────────────────────────────────────────────
    news_lines = []
    try:
        with get_db() as db:
            news = db.query(NewsItem).filter(
                NewsItem.title.ilike(f"%{sym.replace('/USD','').replace('USD','')}%")
            ).order_by(NewsItem.published_at.desc()).limit(5).all()
            threats = db.query(ThreatEvent).order_by(ThreatEvent.created_date.desc()).limit(3).all()
            news_lines = [f"[{n.sentiment or 'neutral'}] {n.title}" for n in news]
            threat_lines = [f"[{t.severity}] {t.title}" for t in threats]
    except Exception as e:
        logger.debug(f"[PaperTrading] News fetch failed for {sym}: {e}")
        threat_lines = []

    news_block = "\n".join(news_lines) if news_lines else "No recent news."
    threat_block = "\n".join(threat_lines) if threat_lines else "No active threats."

    # ── LLM prompt ────────────────────────────────────────────────────────────
    prompt = f"""You are evaluating a paper trade entry for {sym} ({sig['asset_class']}).

SIGNAL DETAILS:
- Direction: {sig['direction']}
- Original signal confidence: {sig['confidence']:.0f}%
- Original entry: ${sig['entry_price']:.4f} | Target: ${sig['target_price']:.4f} | Stop: ${sig['stop_loss']:.4f}
- Current market price: ${current_price:.4f}
- Original reasoning: {sig['reasoning'][:300]}

TECHNICAL ANALYSIS (current):
{ta_summary}

RECENT NEWS FOR {sym}:
{news_block}

MACRO THREATS:
{threat_block}

TASK: Re-evaluate this signal right now using current TA and news. 
- Is the setup still valid at current price ${current_price:.4f}?
- Does TA confirm the {sig['direction']} direction?
- Is news sentiment supportive or a headwind?

Respond ONLY with valid JSON (no markdown):
{{"approved": true/false, "score": 0-100, "reasoning": "1-2 sentence summary"}}

approved=true means enter the paper trade. approved=false means skip it.
Score below {PAPER_MIN_CONFIDENCE} should set approved=false."""

    try:
        from lib.lmstudio import call_lm_studio
        import json, re
        raw = call_lm_studio(
            prompt,
            system="You are a precise trading analyst. Respond only with the JSON object, no markdown.",
            max_tokens=150
        )
        # Strip markdown if present
        cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
        result = json.loads(cleaned)
        approved = bool(result.get("approved", False))
        score = float(result.get("score", 50))
        reasoning = result.get("reasoning", "")
        if score < PAPER_MIN_CONFIDENCE:
            approved = False
        return {"approved": approved, "score": score, "reasoning": reasoning}
    except Exception as e:
        logger.warning(f"[PaperTrading] LLM eval failed for {sym}: {e} — using original confidence")
        # Fallback: use original signal confidence
        approved = sig["confidence"] >= PAPER_MIN_CONFIDENCE
        return {"approved": approved, "score": sig["confidence"], "reasoning": "LLM unavailable — using original signal confidence"}


def run():
    logger.info("[PaperTrading] v4.0 Starting paper trading job...")
    from lib.paper_engine import mark_to_market, open_paper_position, get_paper_summary

    # ── Step 1: Mark-to-market all open positions ──────────────────────────────
    prices = _get_all_prices()
    logger.info(f"[PaperTrading] Price cache: {len(prices)} symbols loaded")
    mtm = mark_to_market(prices)
    logger.info(f"[PaperTrading] MTM: updated={mtm['updated']} | auto-closed={len(mtm['closed'])}")
    for c in mtm.get("closed", []):
        logger.info(f"[PaperTrading] Auto-closed {c['symbol']} via {c['reason']} | P&L=${c.get('pnl', 0):.2f}")

    # ── Step 2: Fetch all signals eligible for paper mirroring ────────────────
    with get_db() as db:
        sig_list = _get_all_pending_signals(db)

    # Filter out symbols already open in paper
    open_syms = _get_open_paper_symbols()
    sig_list = [s for s in sig_list if s["asset_symbol"] not in open_syms]

    if not sig_list:
        logger.info("[PaperTrading] No new signals to mirror in paper")
    else:
        logger.info(f"[PaperTrading] Evaluating {len(sig_list)} candidate signals via LLM+TA...")

    executed = 0
    skipped_no_price = 0
    skipped_ai = 0

    for sig in sig_list:
        sym = sig["asset_symbol"]

        price = _get_current_price(sym)
        if not price:
            price = sig.get("entry_price") or 0.0
        if not price or price <= 0:
            logger.warning(f"[PaperTrading] No price for {sym} — skipping")
            skipped_no_price += 1
            continue

        # ── LLM + TA evaluation ───────────────────────────────────────────────
        eval_result = _evaluate_signal_with_ai(sig, price)
        if not eval_result["approved"]:
            logger.info(
                f"[PaperTrading] ❌ AI rejected {sym} paper trade — "
                f"score={eval_result['score']:.0f} | {eval_result['reasoning']}"
            )
            skipped_ai += 1
            continue

        logger.info(
            f"[PaperTrading] ✅ AI approved {sym} {sig['paper_direction']} — "
            f"score={eval_result['score']:.0f} @ ${price:.4f} | {eval_result['reasoning']}"
        )

        result = open_paper_position(sig, current_price=price)

        if result.get("ok"):
            executed += 1
        elif "already open" in (result.get("error") or ""):
            logger.debug(f"[PaperTrading] {sym} already has paper position — skipping")
        else:
            logger.warning(f"[PaperTrading] Could not open {sym}: {result.get('error')}")

    summary = get_paper_summary()
    port = summary["portfolio"]
    open_count = len(summary['positions'])
    logger.info(
        f"[PaperTrading] Done — new={executed} | ai_rejected={skipped_ai} | no_price={skipped_no_price} | "
        f"open={open_count} | Equity=${port['equity']:.0f} | Cash=${port['cash']:.0f} | "
        f"Realized=${port['realized_pnl']:.2f} | Win%={port['win_rate']}% | Total={port['total_trades']}"
    )
    return {
        "ok": True,
        "mtm": mtm,
        "new_positions": executed,
        "ai_rejected": skipped_ai,
        "skipped_no_price": skipped_no_price,
        "summary": port,
    }
