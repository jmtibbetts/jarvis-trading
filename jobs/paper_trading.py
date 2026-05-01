from app.routes import log_decision
from lib.futures_data import FUTURES_UNIVERSE, get_cached_futures_price, fetch_futures_multi_tf
"""
Job: Paper Trading v5.0
Changes from v4.0:
- Full AI position management: every open paper position evaluated each cycle
- LLM + TA + News/Threat context, same as real manage_positions.py
- Decisions: HOLD | TIGHTEN_STOP | EXIT (paper closes, not Alpaca)
- Hard deterministic rules fire first (same tier thresholds as real trading)
- New entries still go through LLM+TA evaluation before opening
- No position cap — limited only by available virtual cash
"""
import logging, json, re
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal, MarketAsset, PaperPosition, NewsItem, ThreatEvent

logger = logging.getLogger(__name__)

PAPER_MIN_CONFIDENCE = 55   # skip new entries scoring below this

# ── Same tier thresholds as real manage_positions.py ──────────────────────────
TIERS_CRYPTO = [
    {"min_gain": 10.0, "max_gain": None, "action": "close",          "label": ">=10% — take profit"},
    {"min_gain":  5.0, "max_gain": 10.0, "action": "trail_tight",    "label": "5-10% — trail 3%"},
    {"min_gain":  2.0, "max_gain":  5.0, "action": "trail_moderate", "label": "2-5% — trail 5%"},
    {"min_gain": None, "max_gain": -4.0, "action": "close",          "label": "<=-4% — cut loss"},
]
TIERS_EQUITY = [
    {"min_gain": 15.0, "max_gain": None, "action": "close",          "label": ">=15% — take profit"},
    {"min_gain": 10.0, "max_gain": 15.0, "action": "trail_tight",    "label": "10-15% — trail 5%"},
    {"min_gain":  5.0, "max_gain": 10.0, "action": "trail_moderate", "label": "5-10% — trail 8%"},
    {"min_gain": None, "max_gain": -5.0, "action": "close",          "label": "<=-5% — cut loss"},
]


def _is_crypto(sym: str) -> bool:
    return "/" in sym or sym.upper().endswith("USD")


def _tier(plpc: float, is_crypto: bool):
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


def _get_all_prices() -> dict:
    # Layer 1: Alpaca / MarketAsset DB (equities + crypto)
    with get_db() as db:
        assets = db.query(MarketAsset).all()
        prices = {}
        for a in assets:
            if a.price and float(a.price) > 0:
                prices[a.symbol] = float(a.price)
                if "/" in a.symbol:
                    prices[a.symbol.replace("/", "")] = float(a.price)
                elif a.symbol.endswith("USD") and len(a.symbol) > 3:
                    prices[a.symbol[:-3] + "/USD"] = float(a.price)

    # Layer 2: Futures / Forex via yfinance (cached, 5-min TTL)
    try:
        for sym in FUTURES_UNIVERSE:
            if sym not in prices:
                fd = get_cached_futures_price(sym)
                if fd and fd.get("price"):
                    prices[sym] = float(fd["price"])
        logger.debug(f"[PaperTrading] Price map: {len(prices)} symbols (Alpaca+Futures)")
    except Exception as _fe:
        logger.debug(f"[PaperTrading] Futures price layer error: {_fe}")

    return prices


def _get_current_price(symbol: str, prices: dict = None) -> float:
    # Check in-memory price map first (covers Alpaca + futures already loaded)
    if prices:
        for v in [symbol, symbol.replace("/",""), symbol.replace("/USD",""),
                  symbol+"/USD", symbol.upper()]:
            if v in prices:
                return float(prices[v])

    # DB lookup (Alpaca MarketAsset)
    variants = [symbol, symbol.replace("/",""), symbol.replace("/USD",""),
                symbol+"/USD" if "/" not in symbol else symbol]
    with get_db() as db:
        for v in variants:
            asset = db.query(MarketAsset).filter(MarketAsset.symbol == v).first()
            if asset and asset.price and float(asset.price) > 0:
                return float(asset.price)

    # Futures / Forex fallback via yfinance (for symbols like GC=F, EURUSD=X)
    try:
        if symbol in FUTURES_UNIVERSE:
            fd = get_cached_futures_price(symbol)
            if fd and fd.get("price"):
                return float(fd["price"])
    except Exception:
        pass

    return 0.0

def _get_open_paper_symbols() -> set:
    with get_db() as db:
        rows = db.query(PaperPosition.symbol).filter(PaperPosition.status == "Open").all()
        return {r.symbol for r in rows}


def _get_open_paper_positions() -> list:
    """Return all open paper positions as plain dicts."""
    with get_db() as db:
        rows = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        return [{
            "id":           str(p.id),
            "symbol":       p.symbol,
            "direction":    p.direction,
            "entry_price":  float(p.entry_price or 0),
            "qty":          float(p.qty or 0),
            "margin":       float(p.margin_used or 0),
            "leverage":     float(p.leverage or 1),
            "stop_loss":    float(p.stop_loss or 0),
            "target_price": float(p.target_price or 0),
            "opened_at":    str(p.opened_at or ""),
        } for p in rows]


def _get_context() -> tuple:
    """Pull recent threats and news for LLM context."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    with get_db() as db:
        threats = db.query(ThreatEvent).filter(
            ThreatEvent.status == "Active"
        ).order_by(ThreatEvent.created_date.desc()).limit(6).all()
        news = db.query(NewsItem).filter(
            NewsItem.created_date >= cutoff
        ).order_by(NewsItem.created_date.desc()).limit(10).all()

        threat_ctx = "\n".join(
            f"[{t.severity}] {t.title}: {(t.description or '')[:200]}" for t in threats
        ) or "No active threats"
        news_ctx = "\n".join(
            f"[{n.sentiment or 'neutral'}] {n.title}: {(n.summary or '')[:200]}" for n in news
        ) or "No recent news"
    return threat_ctx, news_ctx


def _fetch_ta(sym: str) -> dict:
    try:
        from lib.ta_engine import analyze_symbol
        bars_by_tf = {}

        # ── Futures / Forex path (yfinance) ──────────────────────────────────
        if sym in FUTURES_UNIVERSE:
            raw = fetch_futures_multi_tf(sym, ["1H", "4H", "1D"])
            for tf, df in raw.items():
                if df is not None and len(df) >= 20:
                    bars_by_tf[tf] = df
            if bars_by_tf:
                logger.debug(f"[PaperTrading] Futures TA ({len(bars_by_tf)} TFs) for {sym}")
                return analyze_symbol(bars_by_tf)

        # ── Equity / Crypto path (Alpaca OHLCV cache) ─────────────────────────
        from lib.ohlcv_cache import fetch_with_cache
        cache_sym = sym
        if _is_crypto(sym) and "/" not in sym:
            cache_sym = sym[:-3] + "/USD" if sym.upper().endswith("USD") else sym + "/USD"
        for tf in ["1H", "4H", "1D"]:
            try:
                df = fetch_with_cache(cache_sym, tf, lookback_bars=100)
                if df is not None and len(df) >= 20:
                    bars_by_tf[tf] = df
            except Exception:
                pass
        return analyze_symbol(bars_by_tf) if bars_by_tf else {}
    except Exception as e:
        logger.debug(f"[PaperTrading] TA failed for {sym}: {e}")
        return {}


# ────────────────────────────────────────────────────────────────────────────
# AI POSITION MANAGEMENT
# ────────────────────────────────────────────────────────────────────────────

def _manage_open_positions(prices: dict) -> dict:
    """
    Evaluate every open paper position with deterministic tiers + LLM + TA.
    Same logic as manage_positions.py but closes via close_paper_position().
    """
    from lib.paper_engine import close_paper_position
    from lib.lmstudio import call_lm_studio
    try:
        from lib.ta_engine import build_ta_prompt_block
    except Exception:
        build_ta_prompt_block = lambda s, d: str(d)

    positions = _get_open_paper_positions()
    if not positions:
        return {"evaluated": 0, "closed": 0, "held": 0}

    threat_ctx, news_ctx = _get_context()
    evaluated = 0
    closed = 0
    held = 0

    for pos in positions:
        sym = pos["symbol"]
        current_price = _get_current_price(sym, prices)
        if not current_price or current_price <= 0:
            logger.debug(f"[PaperTrading] No price for {sym} — skipping management")
            continue

        entry = pos["entry_price"]
        if entry <= 0:
            continue

        is_c = _is_crypto(sym)
        direction = pos["direction"].lower()
        side = -1 if direction == "short" else 1
        plpc = ((current_price - entry) / entry) * 100 * side
        pl_dollar = (current_price - entry) * pos["qty"] * side * pos["leverage"]
        evaluated += 1

        # ── Deterministic hard rules (same as real trading) ────────────────
        tier = _tier(plpc, is_c)
        if tier and tier["action"] == "close":
            logger.info(f"[PaperTrading] 🔒 Hard rule: {sym} {plpc:+.2f}% → {tier['label']}")
            log_decision('paper', 'EXIT', tier['label'], symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=tier["label"])
            closed += 1
            continue

        # ── LLM + TA evaluation ────────────────────────────────────────────
        ta_data = _fetch_ta(sym)
        ta_block = build_ta_prompt_block(sym, ta_data) if ta_data else "TA unavailable"

        # Symbol-specific news
        base_sym = sym.replace("/USD","").replace("USD","")
        with get_db() as db:
            sym_news = db.query(NewsItem).filter(
                NewsItem.title.ilike(f"%{base_sym}%")
            ).order_by(NewsItem.published_at.desc()).limit(5).all()
            sym_news_ctx = "\n".join(
                f"[{n.sentiment or 'neutral'}] {n.title}" for n in sym_news
            ) or "No symbol-specific news"

        tier_label = tier["label"] if tier else "No tier action"

        # Compact micro-prompt — keeps output under 60 tokens to beat LM Studio 150-token cap
        # (deterministic rules already handled the heavy lifting above)
        ta_snippet = ta_block[:150] if ta_data else "N/A"
        news_snippet = sym_news_ctx[:80]
        prompt = (
            f"Paper {pos['direction']} {sym} | PnL:{plpc:+.1f}% ${pl_dollar:+.0f} | "
            f"entry:{entry:.4f} price:{current_price:.4f} stop:{pos['stop_loss']:.4f} tp:{pos['target_price']:.4f} {pos['leverage']}x\n"
            f"TA:{ta_snippet}\nNews:{news_snippet}\n"
            f'Respond JSON only: {{"action":"HOLD","new_stop_pct":null,"reasoning":"brief"}} '
            f'action=HOLD|TIGHTEN_STOP|EXIT'
        )

        try:
            raw = call_lm_studio(
                prompt,
                system="Trading risk manager. JSON only: {action,new_stop_pct,reasoning}. No markdown.",
                max_tokens=300
            )
            cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
            result = json.loads(cleaned)
            action = str(result.get("action", "HOLD")).upper()
            reasoning = result.get("reasoning", "")
            new_stop_pct = result.get("new_stop_pct")
        except Exception as e:
            logger.warning(f"[PaperTrading] LLM eval failed for {sym}: {e} — defaulting to HOLD")
            action = "HOLD"
            reasoning = "LLM unavailable"
            new_stop_pct = None

        if action == "EXIT":
            logger.info(f"[PaperTrading] 🤖 LLM EXIT {sym} {plpc:+.2f}% | {reasoning}")
            log_decision("paper", "EXIT", reasoning, symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=f"AI EXIT: {reasoning[:80]}")
            closed += 1
        elif action == "TIGHTEN_STOP" and new_stop_pct:
            try:
                new_stop = round(current_price * (1.0 - float(new_stop_pct) / 100.0), 6)
                # Only tighten (raise stop), never loosen
                if new_stop > pos["stop_loss"]:
                    with get_db() as db:
                        p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                        if p:
                            p.stop_loss = new_stop
                    logger.info(f"[PaperTrading] 🤖 TIGHTEN_STOP {sym} stop → ${new_stop:.4f} ({new_stop_pct}% below ${current_price:.4f}) | {reasoning}")
                else:
                    logger.debug(f"[PaperTrading] TIGHTEN_STOP for {sym} ignored — new stop ${new_stop:.4f} not above current ${pos['stop_loss']:.4f}")
            except Exception as e:
                logger.warning(f"[PaperTrading] TIGHTEN_STOP update failed for {sym}: {e}")
            held += 1
        else:
            logger.info(f"[PaperTrading] 🤖 HOLD {sym} {plpc:+.2f}% | {reasoning}")
            log_decision("paper", "HOLD", reasoning, symbol=sym, pnl_pct=plpc, price=current_price)
            held += 1

    return {"evaluated": evaluated, "closed": closed, "held": held}


# ────────────────────────────────────────────────────────────────────────────
# NEW ENTRY EVALUATION
# ────────────────────────────────────────────────────────────────────────────

def _get_pending_signals(db) -> list:
    """
    Return signals eligible for paper execution:
      - paper_mode=True signals (Track E: leveraged/short equities/crypto)
      - Any signal whose symbol is in the futures universe (Track F)
    Live equity/crypto signals (paper_mode=False, not in futures universe)
    are handled exclusively by execute_signals.py — never touched here.
    """
    eligible_statuses = ["Active", "Executed", "PendingApproval"]
    signals = db.query(TradingSignal).filter(
        TradingSignal.status.in_(eligible_statuses),
    ).order_by(TradingSignal.generated_at.desc()).limit(200).all()
    seen = set()
    result = []
    for s in signals:
        sym = (s.asset_symbol or "").upper().strip()
        if not sym or sym in seen:
            continue

        is_futures = sym in FUTURES_UNIVERSE
        is_paper   = bool(getattr(s, 'paper_mode', False))

        # Only pick up signals that belong in the paper engine
        if not is_paper and not is_futures:
            continue

        seen.add(sym)
        # Use paper_direction when set; fall back to direction
        paper_dir = (getattr(s, 'paper_direction', None) or s.direction or "Long")
        result.append({
            "id":             s.id,
            "asset_symbol":   sym,
            "asset_name":     s.asset_name or sym,
            "asset_class":    s.asset_class or ("Futures" if is_futures else "Equity"),
            "direction":      s.direction or "Long",
            "paper_direction": paper_dir,
            "paper_mode":     True,
            "entry_price":    float(s.entry_price) if s.entry_price else 0.0,
            "target_price":   float(s.target_price) if s.target_price else 0.0,
            "stop_loss":      float(s.stop_loss) if s.stop_loss else 0.0,
            "confidence":     float(s.confidence) if s.confidence else 50.0,
            "reasoning":      s.reasoning or "",
            "signal_status":  s.status,
        })
    return result


def _evaluate_entry_with_ai(sig: dict, current_price: float, threat_ctx: str, news_ctx: str) -> dict:
    from lib.lmstudio import call_lm_studio
    sym = sig["asset_symbol"]
    ta_data = _fetch_ta(sym)
    try:
        from lib.ta_engine import build_ta_prompt_block
        ta_block = build_ta_prompt_block(sym, ta_data) if ta_data else "TA unavailable"
    except Exception:
        ta_block = str(ta_data) if ta_data else "TA unavailable"

    base_sym = sym.replace("/USD","").replace("USD","")
    with get_db() as db:
        sym_news = db.query(NewsItem).filter(
            NewsItem.title.ilike(f"%{base_sym}%")
        ).order_by(NewsItem.published_at.desc()).limit(5).all()
        sym_news_ctx = "\n".join(
            f"[{n.sentiment or 'neutral'}] {n.title}" for n in sym_news
        ) or "No symbol-specific news"

    # Compact micro-prompt for entry eval — minimal tokens needed
    ta_snippet = ta_block[:150] if ta_data else "N/A"
    news_snippet = sym_news_ctx[:80]
    prompt = (
        f"Eval entry: {sym} {sig['direction']} {sig['asset_class']} | conf:{sig['confidence']:.0f}% | "
        f"entry:{sig['entry_price']:.4f} target:{sig['target_price']:.4f} stop:{sig['stop_loss']:.4f} now:{current_price:.4f}\n"
        f"TA:{ta_snippet}\nNews:{news_snippet}\n"
        f'Respond JSON only: {{"approved":true,"score":75,"reasoning":"brief"}} min_score={PAPER_MIN_CONFIDENCE}'
    )

    try:
        raw = call_lm_studio(
            prompt,
            system="Trading analyst. JSON only: {approved,score,reasoning}. No markdown.",
            max_tokens=300
        )
        cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
        result = json.loads(cleaned)
        approved = bool(result.get("approved", False))
        score = float(result.get("score", 50))
        reasoning = result.get("reasoning", "")
        if score < PAPER_MIN_CONFIDENCE:
            approved = False
        return {"approved": approved, "score": score, "reasoning": reasoning}
    except Exception as e:
        logger.warning(f"[PaperTrading] Entry LLM eval failed for {sym}: {e} — using original confidence")
        approved = sig["confidence"] >= PAPER_MIN_CONFIDENCE
        return {"approved": approved, "score": sig["confidence"], "reasoning": "LLM unavailable — using original confidence"}


# ────────────────────────────────────────────────────────────────────────────
# MAIN JOB
# ────────────────────────────────────────────────────────────────────────────

def run():
    logger.info("[PaperTrading] v5.0 Starting paper trading job...")
    from lib.paper_engine import mark_to_market, open_paper_position, get_paper_summary

    # ── Step 1: Mark-to-market ────────────────────────────────────────────────
    prices = _get_all_prices()
    logger.info(f"[PaperTrading] Price cache: {len(prices)} symbols loaded")
    mtm = mark_to_market(prices)
    logger.info(f"[PaperTrading] MTM: updated={mtm['updated']} | auto-closed={len(mtm['closed'])}")
    for c in mtm.get("closed", []):
        logger.info(f"[PaperTrading] MTM auto-closed {c['symbol']} via {c['reason']} | P&L=${c.get('pnl', 0):.2f}")

    # ── Step 2: AI position management on all open positions ──────────────────
    mgmt = _manage_open_positions(prices)
    logger.info(
        f"[PaperTrading] Position mgmt: evaluated={mgmt['evaluated']} | "
        f"closed={mgmt['closed']} | held={mgmt['held']}"
    )

    # ── Step 3: Evaluate and open new positions ───────────────────────────────
    with get_db() as db:
        sig_list = _get_pending_signals(db)

    open_syms = _get_open_paper_symbols()
    sig_list = [s for s in sig_list if s["asset_symbol"] not in open_syms]

    if not sig_list:
        logger.info("[PaperTrading] No new signals to evaluate")
    else:
        logger.info(f"[PaperTrading] Evaluating {len(sig_list)} new entry candidates via LLM+TA...")

    threat_ctx, news_ctx = _get_context()
    executed = 0
    skipped_no_price = 0
    skipped_ai = 0

    for sig in sig_list:
        sym = sig["asset_symbol"]
        price = _get_current_price(sym, prices) or sig.get("entry_price") or 0.0
        if not price or price <= 0:
            logger.warning(f"[PaperTrading] No price for {sym} — skipping")
            skipped_no_price += 1
            continue

        eval_result = _evaluate_entry_with_ai(sig, price, threat_ctx, news_ctx)
        if not eval_result["approved"]:
            logger.info(
                f"[PaperTrading] ❌ AI rejected entry {sym} — "
                f"score={eval_result['score']:.0f} | {eval_result['reasoning']}"
            )
            log_decision('paper', 'REJECTED', eval_result['reasoning'], symbol=sym, price=price, score=eval_result['score'])
            skipped_ai += 1
            continue

        logger.info(
            f"[PaperTrading] ✅ AI approved entry {sym} {sig['paper_direction']} — "
            f"score={eval_result['score']:.0f} @ ${price:.4f} | {eval_result['reasoning']}"
        )
        log_decision('paper', 'APPROVED', eval_result['reasoning'], symbol=sym, price=price, score=eval_result['score'])
        result = open_paper_position(sig, current_price=price)
        if result.get("ok"):
            executed += 1
        elif "already open" in (result.get("error") or ""):
            logger.debug(f"[PaperTrading] {sym} already open — skipping")
        else:
            logger.warning(f"[PaperTrading] Could not open {sym}: {result.get('error')}")

    # ── Step 4: Summary ───────────────────────────────────────────────────────
    summary = get_paper_summary()
    port = summary["portfolio"]
    logger.info(
        f"[PaperTrading] Done — new={executed} | ai_rejected={skipped_ai} | no_price={skipped_no_price} | "
        f"mgmt_closed={mgmt['closed']} | open={len(summary['positions'])} | "
        f"Equity=${port['equity']:.0f} | Cash=${port['cash']:.0f} | "
        f"Realized=${port['realized_pnl']:.2f} | Win%={port['win_rate']}% | Total={port['total_trades']}"
    )
    return {
        "ok": True,
        "mtm": mtm,
        "position_management": mgmt,
        "new_positions": executed,
        "ai_rejected": skipped_ai,
        "skipped_no_price": skipped_no_price,
        "summary": port,
    }


