"""
Job: Manage Positions v7.0
- Every position evaluated against fresh TA + recent news/threats every cycle
- LLM reviews each position and returns: HOLD | TIGHTEN_STOP | EXIT
- Hard deterministic rules still fire first (no LLM latency on urgent closes)
- Separate crypto/equity tier thresholds
- Crypto: -4% stop, trail at +2%/+5%, take profit at +10%
- Equity: -5% stop, trail at +5%/+10%, take profit at +15%
"""
import logging, uuid, json, math
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal, Position, PortfolioSnapshot, ThreatEvent, NewsItem
from lib.alpaca_client import get_positions, get_account, close_position, get_trading_client, normalize_symbol
from lib.lmstudio import call_lm_studio, parse_json
from lib.ta_engine import analyze_symbol, build_ta_prompt_block
from lib.ohlcv_cache import fetch_with_cache

logger = logging.getLogger(__name__)

# ── Tier thresholds ─────────────────────────────────────────────────────────────
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


def _tier(plpc: float, is_crypto: bool = False):
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
    return "/" in sym or sym.upper().endswith("USD")


def _alpaca_sym(sym: str) -> str:
    """Normalize symbol to Alpaca format for order submission (no slash)."""
    s = sym.upper().strip()
    if "/" in s:
        return s.replace("/", "")
    return s

def _sym_variants(sym: str) -> list:
    """Return all symbol forms for DB lookups: BTCUSD, BTC/USD, BTC, etc."""
    s = sym.upper().strip()
    variants = {s, s.replace("/", "")}
    # BTCUSD → BTC/USD
    if s.endswith("USD") and "/" not in s and len(s) > 3:
        variants.add(s[:-3] + "/USD")
    # BTC/USD → BTCUSD (already covered by replace above)
    # BTC → BTC/USD
    if "/" not in s and not s.endswith("USD") and len(s) <= 5:
        variants.add(s + "/USD")
    return list(variants)


def _cancel_open_orders(client, sym: str):
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]))
        for o in open_orders:
            try:
                client.cancel_order_by_id(o.id)
                logger.debug(f"[Positions] Cancelled order {o.id} for {sym}")
            except Exception as ce:
                logger.debug(f"[Positions] Could not cancel {o.id}: {ce}")
    except Exception as e:
        logger.debug(f"[Positions] Cancel check failed for {sym}: {e}")


def _set_trailing_stop_equity(client, sym: str, qty: float, trail_pct: float) -> bool:
    try:
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = TrailingStopOrderRequest(
            symbol=sym, qty=max(1, int(qty)),
            side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            trail_percent=trail_pct,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] ✓ Equity trailing stop — {sym} trail={trail_pct}% | {order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Equity trailing stop failed {sym}: {e}")
        return False


def _set_crypto_limit_stop(client, sym: str, qty: float, current_price: float, trail_pct: float) -> bool:
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        limit_price = round(current_price * (1.0 - trail_pct / 100.0), 6)
        # Truncate qty using floor — never round up, Alpaca rejects if qty > available balance
        # (their API returns e.g. 127.679999999 due to float precision, so 127.68 gets rejected)
        qty_safe = math.floor(qty * 1_000_000) / 1_000_000
        req = LimitOrderRequest(
            symbol=sym, qty=qty_safe,
            side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            limit_price=limit_price,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] ✓ Crypto limit-stop — {sym} floor=${limit_price:.4f} ({trail_pct}% below ${current_price:.4f}) | {order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Crypto limit-stop failed {sym}: {e}")
        return False


def _set_protective_order(sym: str, qty: float, trail_pct: float, current_price: float) -> bool:
    try:
        client = get_trading_client()
        _cancel_open_orders(client, sym)
        if _is_crypto(sym):
            return _set_crypto_limit_stop(client, sym, qty, current_price, trail_pct)
        else:
            return _set_trailing_stop_equity(client, sym, qty, trail_pct)
    except Exception as e:
        logger.warning(f"[Positions] Protective order failed {sym}: {e}")
        return False


def _fetch_ta(sym: str) -> dict:
    """Fetch multi-timeframe TA for a position symbol."""
    try:
        # Normalize to cache format (slash for crypto)
        cache_sym = sym
        if _is_crypto(sym) and "/" not in sym:
            base = sym[:-3] if sym.upper().endswith("USD") else sym
            cache_sym = f"{base}/USD"

        timeframes = ["1H", "4H", "1D"]
        bars_by_tf = {}
        for tf in timeframes:
            try:
                df = fetch_with_cache(cache_sym, tf, lookback_bars=100)
                if df is not None and len(df) >= 20:
                    bars_by_tf[tf] = df
            except Exception as e:
                logger.debug(f"[Positions] TA fetch {cache_sym}/{tf}: {e}")

        if not bars_by_tf:
            return {}

        return analyze_symbol(bars_by_tf)
    except Exception as e:
        logger.debug(f"[Positions] TA failed for {sym}: {e}")
        return {}


def _get_context(db) -> tuple[str, str]:
    """Pull recent threats and news for LLM context."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

    threats = db.query(ThreatEvent).filter(
        ThreatEvent.status == "Active"
    ).order_by(ThreatEvent.created_date.desc()).limit(6).all()

    news = db.query(NewsItem).filter(
        NewsItem.created_date >= cutoff
    ).order_by(NewsItem.created_date.desc()).limit(10).all()

    threat_ctx = "\n".join(
        f"[{t.severity}] {t.title}: {(t.description or '')[:120]}"
        for t in threats
    ) or "No active threats"

    news_ctx = "\n".join(
        f"[{n.sentiment or 'neutral'}] {n.title}: {(n.summary or '')[:120]}"
        for n in news
    ) or "No recent news"

    return threat_ctx, news_ctx


def _llm_evaluate_position(sym: str, plpc: float, avg: float, current_price: float,
                             mv: float, pl: float, ta_data: dict,
                             threat_ctx: str, news_ctx: str,
                             original_signal: dict) -> dict:
    """
    Ask the LLM to evaluate the position and return a structured decision.
    Returns: {"action": "HOLD"|"TIGHTEN_STOP"|"EXIT", "reason": str, "new_stop": float|None}
    """
    is_c = _is_crypto(sym)
    asset_type = "Crypto (24/7)" if is_c else "Equity"

    ta_block = build_ta_prompt_block(sym, ta_data) if ta_data else "TA data unavailable"

    orig_entry  = original_signal.get("entry_price", avg)
    orig_target = original_signal.get("target_price", "N/A")
    orig_stop   = original_signal.get("stop_loss", "N/A")
    orig_conf   = original_signal.get("confidence", "N/A")
    orig_reason = original_signal.get("reasoning", "N/A")

    prompt = f"""You are an active position manager for a trading AI. Evaluate this open position and decide what to do RIGHT NOW.

POSITION: {sym} ({asset_type})
  Current P&L:    {plpc:+.2f}%  (${pl:+.2f})
  Market Value:   ${mv:,.2f}
  Avg Entry:      ${avg:.4f}
  Current Price:  ${current_price:.4f}

ORIGINAL SIGNAL:
  Entry: ${orig_entry}  |  Target: ${orig_target}  |  Stop: ${orig_stop}
  Confidence: {orig_conf}%
  Thesis: {orig_reason}

TECHNICAL ANALYSIS (multi-timeframe):
{ta_block}

ACTIVE THREATS (last 6h):
{threat_ctx}

RECENT NEWS (last 6h):
{news_ctx}

DECISION RULES:
- HOLD: TA still supports the original thesis. No major contradicting news or threats. Position within normal volatility.
- TIGHTEN_STOP: Position is at risk but not at hard stop. TA is deteriorating OR negative news directly affects this asset. Move stop closer to protect capital. Provide new_stop_pct (% below current price to place stop, e.g. 2.0 means stop at current_price * 0.98).
- EXIT: TA has fully reversed OR news/threat directly invalidates the trade thesis OR risk/reward is now unfavorable. Close immediately.

Respond ONLY with valid JSON:
{{"action": "HOLD" | "TIGHTEN_STOP" | "EXIT", "reason": "1-2 sentence explanation", "new_stop_pct": <float or null>}}"""

    try:
        raw = call_lm_studio(prompt, system="You are a precise trading risk manager. Respond only with the JSON object, no markdown.", max_tokens=200)
        result = parse_json(raw)
        if isinstance(result, dict) and result.get("action") in ("HOLD", "TIGHTEN_STOP", "EXIT"):
            return result
        logger.warning(f"[Positions] LLM bad response for {sym}: {raw[:100]}")
    except Exception as e:
        logger.warning(f"[Positions] LLM eval failed for {sym}: {e}")

    return {"action": "HOLD", "reason": "LLM unavailable — defaulting to hold", "new_stop_pct": None}


def run():
    logger.info("[Positions] Running position management v7.0...")
    try:
        positions = get_positions()
        account   = get_account()
        equity    = float(account.equity)
        cash      = float(account.cash)
    except Exception as e:
        logger.error(f"[Positions] Alpaca error: {e}")
        return {"error": str(e)}

    if not positions:
        logger.info("[Positions] No open positions")
        return {"closed": 0, "trailing": 0, "total": 0, "equity": equity}

    now_iso  = datetime.now(timezone.utc).isoformat()
    closed   = 0
    trailing = 0
    total_mv = sum(float(p.market_value or 0) for p in positions)
    total_pl = sum(float(p.unrealized_pl or 0) for p in positions)

    logger.info(f"[Positions] {len(positions)} positions | MV=${total_mv:,.0f} | P&L=${total_pl:+,.0f} | equity=${equity:,.0f}")

    # Load shared context once (threats + news)
    with get_db() as db:
        threat_ctx, news_ctx = _get_context(db)

        # Load executed signals for original thesis lookup
        exec_sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Executed", "Active"])
        ).all()
        sig_map = {s.asset_symbol.upper().replace("/", ""): {
            "entry_price":  s.entry_price,
            "target_price": s.target_price,
            "stop_loss":    s.stop_loss,
            "confidence":   s.confidence,
            "reasoning":    s.reasoning or "",
        } for s in exec_sigs}

        # Rebuild positions table
        db.query(Position).delete()
        for pos in positions:
            sym  = str(pos.symbol)
            plpc = float(pos.unrealized_plpc or 0) * 100
            db.add(Position(
                symbol=sym,
                qty=float(pos.qty or 0),
                avg_entry=float(pos.avg_entry_price or 0),
                market_value=float(pos.market_value or 0),
                unrealized_pl=float(pos.unrealized_pl or 0),
                unrealized_plpc=plpc,
                side=str(pos.side),
                asset_class="Crypto" if _is_crypto(sym) else "Equity",
                updated_at=now_iso
            ))

        # Snapshot
        db.add(PortfolioSnapshot(
            id=str(uuid.uuid4()),
            equity=equity, cash=cash,
            market_value=total_mv, unrealized_pl=total_pl,
            position_count=len(positions),
            snapshot_at=now_iso
        ))

    # ── Evaluate each position ──────────────────────────────────────────────────
    for pos in positions:
        sym           = str(pos.symbol)
        qty           = float(pos.qty or 0)
        avg           = float(pos.avg_entry_price or 0)
        mv            = float(pos.market_value or 0)
        pl            = float(pos.unrealized_pl or 0)
        plpc          = float(pos.unrealized_plpc or 0) * 100
        current_price = float(pos.current_price or avg or 0)
        is_c          = _is_crypto(sym)
        alpaca_sym    = _alpaca_sym(sym)

        logger.info(f"[Positions] {sym:12} {plpc:+6.1f}% | MV=${mv:>10,.0f} | P&L=${pl:>+8,.0f} | qty={qty}")

        # ── STEP 1: Hard deterministic rules — no LLM, instant ────────────────
        tier = _tier(plpc, is_crypto=is_c)

        if tier is not None:
            label  = tier["label"]
            action = tier["action"]
            trail_pct = (3.0 if action == "trail_tight" else 5.0) if is_c else \
                        (5.0 if action == "trail_tight" else 8.0)

            if action == "close":
                try:
                    close_position(alpaca_sym)
                    logger.info(f"[Positions] ✓ [RULE] Closed {sym} @ {plpc:+.1f}% | {label}")
                    with get_db() as db:
                        sig = db.query(TradingSignal).filter(
                            TradingSignal.asset_symbol.in_(_sym_variants(sym)),
                            TradingSignal.status == "Executed"
                        ).first()
                        if sig:
                            sig.status = "Closed"
                            sig.updated_date = now_iso
                    closed += 1
                except Exception as e:
                    logger.error(f"[Positions] Close {sym} failed: {e}")
                continue

            else:
                logger.info(f"[Positions] ⟳ [RULE] Trail {sym} @ {plpc:+.1f}% — {trail_pct}% | {label}")
                ok = _set_protective_order(alpaca_sym, qty, trail_pct, current_price)
                if ok:
                    trailing += 1
                # Still fall through to LLM for deeper analysis even after setting stop
                # (LLM might escalate to EXIT if thesis is broken)

        # ── STEP 2: LLM evaluation — TA + news + threats ──────────────────────
        logger.info(f"[Positions] 🤖 LLM evaluating {sym}...")
        ta_data = _fetch_ta(sym)
        original_signal = sig_map.get(sym.upper().replace("/", ""), {})

        decision = _llm_evaluate_position(
            sym=sym, plpc=plpc, avg=avg,
            current_price=current_price, mv=mv, pl=pl,
            ta_data=ta_data,
            threat_ctx=threat_ctx, news_ctx=news_ctx,
            original_signal=original_signal
        )

        action = decision.get("action", "HOLD")
        reason = decision.get("reason", "")
        new_stop_pct = decision.get("new_stop_pct")

        logger.info(f"[Positions] 🤖 {sym} → {action} | {reason}")

        if action == "EXIT":
            # Don't double-close if hard rule already fired
            if tier and tier["action"] == "close":
                logger.info(f"[Positions] {sym} already closed by hard rule — skipping LLM EXIT")
                continue
            try:
                close_position(alpaca_sym)
                logger.info(f"[Positions] ✓ [LLM] Closed {sym} @ {plpc:+.1f}% | {reason}")
                with get_db() as db:
                    sig = db.query(TradingSignal).filter(
                        TradingSignal.asset_symbol.in_(_sym_variants(sym)),
                        TradingSignal.status == "Executed"
                    ).first()
                    if sig:
                        sig.status = "Closed"
                        sig.updated_date = now_iso
                closed += 1
            except Exception as e:
                logger.error(f"[Positions] LLM EXIT close {sym} failed: {e}")

        elif action == "TIGHTEN_STOP" and new_stop_pct:
            # Only tighten if LLM hasn't already been overridden by a hard trail
            try:
                stop_price = current_price * (1.0 - float(new_stop_pct) / 100.0)
                logger.info(f"[Positions] ⟳ [LLM] Tighten stop {sym} → ${stop_price:.4f} ({new_stop_pct}% below current) | {reason}")
                client = get_trading_client()
                _cancel_open_orders(client, alpaca_sym)
                if is_c:
                    _set_crypto_limit_stop(client, alpaca_sym, qty, current_price, float(new_stop_pct))
                else:
                    _set_trailing_stop_equity(client, alpaca_sym, qty, float(new_stop_pct))
                trailing += 1
            except Exception as e:
                logger.error(f"[Positions] LLM TIGHTEN_STOP {sym} failed: {e}")

        else:
            logger.info(f"[Positions] ✓ {sym} holding — {reason}")

    logger.info(f"[Positions] Done — {closed} closed, {trailing} protective orders | equity=${equity:,.2f}")
    return {"closed": closed, "trailing": trailing, "total": len(positions), "equity": equity}
