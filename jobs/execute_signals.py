"""
Job: Execute Signals v6.2 — Kelly sizing + regime check + correlation filter.
v6.2: Crypto R:R floor 1.0, 6h signal age window, per-signal INFO logging.
"""
import logging
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal
from lib.alpaca_client import get_account, get_positions, submit_bracket_order, normalize_symbol, is_crypto
from datetime import date

logger = logging.getLogger(__name__)

def _normalize_held(positions):
    """Build a set of held symbols in BOTH formats: SOL/USD and SOLUSD."""
    held = set()
    for p in positions:
        sym = str(p.symbol).upper().strip()
        held.add(sym)  # as returned by Alpaca e.g. SOLUSD, BTCUSD, NVDA
        # Also add slash format for crypto
        if len(sym) > 3 and sym.endswith('USD') and sym[:-3].isalpha():
            held.add(f"{sym[:-3]}/USD")
    return held

def run():
    logger.info("[Execute] Starting execution job...")
    try:
        account   = get_account()
        equity    = float(account.equity)
        buying_power = float(account.buying_power)
        positions = get_positions()
    except Exception as e:
        logger.error(f"[Execute] Alpaca account fetch failed: {e}")
        return {"error": str(e)}

    held      = _normalize_held(positions)
    mv_held   = sum(float(p.market_value or 0) for p in positions)
    max_pos   = max(8, int(equity * 0.5 / 1000))
    slots     = max_pos - len(positions)

    # Real spendable cash — use actual buying power, not equity math
    budget = min(buying_power * 0.95, max(0, equity * 0.5 - mv_held))

    logger.info(f"[Execute] equity=${equity:.0f} | buying_power=${buying_power:.0f} | budget=${budget:.0f} | positions={len(positions)}/{max_pos} | slots={slots}")

    if slots <= 0:
        logger.info(f"[Execute] At max positions ({max_pos}) — skipping")
        return {"executed": 0, "reason": "at_max_positions"}

    if budget < 50:
        logger.info(f"[Execute] Insufficient buying power ${buying_power:.0f} — skipping")
        return {"executed": 0, "reason": "insufficient_budget"}

    regime = {"label": "Unknown", "risk": "medium"}
    try:
        from lib.market_regime import get_regime
        regime = get_regime()
        logger.info(f"[Execute] Regime: {regime['label']} | Risk: {regime['risk']}")
    except Exception as e:
        logger.warning(f"[Execute] Regime check failed: {e}")

    min_conf = 75 if regime.get("risk") == "high" else 55

    try:
        from lib.risk_manager import portfolio_heat
        heat = portfolio_heat(
            [{"market_value": float(p.market_value or 0),
              "unrealized_plpc": float(p.unrealized_plpc or 0) * 100} for p in positions],
            equity
        )
        if heat.get("status") == "hot":
            logger.warning("[Execute] Portfolio heat is HIGH — skipping execution")
            return {"executed": 0, "reason": "portfolio_hot"}
    except Exception as e:
        logger.debug(f"[Execute] Portfolio heat check skipped: {e}")

    # Crypto signals valid for 6h (24/7 market), equity 4h
    cutoff_equity = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    cutoff_crypto = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(
            TradingSignal.status == "Active",
            TradingSignal.generated_at >= cutoff_crypto,  # use wider window; equity filtered below
            TradingSignal.confidence >= min_conf
        ).order_by(TradingSignal.confidence.desc()).limit(50).all()

        sig_dicts = [{
            "id":           s.id,
            "asset_symbol": s.asset_symbol,
            "asset_class":  s.asset_class or "Equity",
            "direction":    s.direction or "Long",
            "confidence":   float(s.composite_score or s.confidence or 65),
            "entry_price":  s.entry_price,
            "target_price": s.target_price,
            "stop_loss":    s.stop_loss,
        } for s in sigs]

    logger.info(f"[Execute] {len(sig_dicts)} active signals qualify (conf>={min_conf}, age<4h)")

    candidates = sig_dicts
    try:
        from lib.risk_manager import filter_correlated
        candidates = filter_correlated(sig_dicts, held, max_per_sector=2)
        logger.info(f"[Execute] {len(candidates)} after correlation filter")
    except Exception as e:
        logger.debug(f"[Execute] Correlation filter skipped: {e}")

    executed = 0
    with get_db() as db:
        for sig in candidates:
            if executed >= slots or budget < 100:
                break

            sym_raw = sig["asset_symbol"]
            sym, crypto = normalize_symbol(sym_raw)

            logger.info(f"[Execute] Evaluating {sym} ({'crypto' if crypto else 'equity'}) conf={sig['confidence']:.0f}%")

            # Skip already held (check both formats)
            if sym in held or sym_raw in held:
                logger.info(f"[Execute] Skip {sym} — already held")
                continue

            # For equity: also enforce 4h age limit (crypto gets the full 6h)
            if not crypto:
                if sig.get("generated_at","") < cutoff_equity:
                    logger.info(f"[Execute] Skip {sym} — equity signal too old (>4h)")
                    continue

            # Skip equities when market is closed (weekends / outside 9:30-16:00 ET)
            if not crypto:
                now_utc = datetime.now(timezone.utc)
                weekday = now_utc.weekday()  # 0=Mon, 5=Sat, 6=Sun
                hour_et = (now_utc.hour - 4) % 24  # rough ET offset (EDT)
                market_open = weekday < 5 and 13 <= now_utc.hour < 20  # 9:30-16:00 ET = 13:30-20:00 UTC
                if not market_open:
                    # Queue for Monday morning approval instead of skipping silently
                    with get_db() as qdb:
                        rec = qdb.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                        if rec and rec.status == "Active":
                            rec.status = "PendingApproval"
                            rec.updated_date = datetime.now(timezone.utc).isoformat()
                            logger.info(f"[Execute] ⏳ {sym} → PendingApproval (market closed, queued for Monday)")
                    continue

            entry  = float(sig.get("entry_price")  or 0)
            target = float(sig.get("target_price") or 0)
            stop   = float(sig.get("stop_loss")    or 0)

            if not entry or not target or not stop:
                logger.warning(f"[Execute] Skip {sym} — missing price levels (entry={entry} tp={target} sl={stop})")
                continue
            if stop >= entry:
                logger.warning(f"[Execute] Skip {sym} — stop ${stop} >= entry ${entry}")
                continue
            if target <= entry:
                logger.warning(f"[Execute] Skip {sym} — target ${target} <= entry ${entry}")
                continue

            # Position sizing
            trade_budget = budget
            try:
                from lib.risk_manager import calculate_position_size
                sz = calculate_position_size(sig, equity, regime)
                if sz.rejection_reason:
                    logger.info(f"[Execute] Skip {sym} — risk mgr: {sz.rejection_reason}")
                    continue
                trade_budget = min(sz.dollar_size, budget)
            except Exception as e:
                conf = float(sig.get("confidence", 65))
                trade_budget = max(100, min(1500, 500 + (conf - 55) / 45 * 1000))
                trade_budget = min(trade_budget, budget)

            # Hard cap per trade: don't spend more than remaining buying power
            # Spread remaining budget evenly across remaining slots to avoid
            # blowing it all on the first trade
            remaining_slots = max(1, slots - executed)
            per_trade_cap = min(trade_budget, budget / remaining_slots, budget)
            per_trade_cap = max(50.0, per_trade_cap)

            # Qty: fractional for crypto, integer for equities
            if crypto:
                qty = round(per_trade_cap / entry, 6)
                if qty < 0.0001:
                    logger.warning(f"[Execute] Skip {sym} — qty too small ({qty}) per_trade_cap=${per_trade_cap:.0f} entry=${entry}")
                    continue
            else:
                qty = max(1, int(per_trade_cap / entry))
                cost = qty * entry
                if cost > budget:
                    logger.warning(f"[Execute] Skip {sym} — {qty} shares × ${entry} = ${cost:.0f} > budget ${budget:.0f}")
                    continue

            try:
                submit_bracket_order(
                    symbol=sym, qty=qty, entry_price=entry,
                    take_profit=target, stop_loss=stop
                )
                rec = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if rec:
                    rec.status = "Executed"
                    rec.updated_date = datetime.now(timezone.utc).isoformat()
                held.add(sym)
                budget -= qty * entry
                executed += 1
                logger.info(f"[Execute] ✓ {sym} {'(crypto)' if crypto else '(equity)'} x{qty} @ ${entry:.4f} TP=${target:.4f} SL=${stop:.4f} | budget left=${budget:.0f}")
            except Exception as e:
                rec = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if rec:
                    rec.status = "Rejected"
                    rec.updated_date = datetime.now(timezone.utc).isoformat()
                logger.error(f"[Execute] ✗ {sym}: {type(e).__name__}: {e}")

    # Count pending approval signals
    with get_db() as db:
        pending_count = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").count()

    logger.info(f"[Execute] Done — {executed} executed | {pending_count} pending approval | budget=${budget:.0f}")
    return {"executed": executed, "pending_approval": pending_count, "regime": regime.get("label"), "budget_remaining": round(budget, 2)}
