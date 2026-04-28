"""
Job: Execute Signals v6.5
- generate_signals sets status=Active when market is open, PendingApproval when closed
- execute promotes PendingApproval → Active at run-time if market has since opened
- Once status=Active, execute fires immediately — no manual approval needed
- No more duplicate PendingApproval writes from execute job
"""
import logging
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal
from lib.alpaca_client import get_account, get_positions, submit_bracket_order, normalize_symbol, is_crypto

logger = logging.getLogger(__name__)

def _normalize_held(positions):
    """Build a set of held symbols in BOTH formats: SOL/USD and SOLUSD."""
    held = set()
    for p in positions:
        sym = str(p.symbol).upper().strip()
        held.add(sym)
        if len(sym) > 3 and sym.endswith('USD') and sym[:-3].isalpha():
            held.add(f"{sym[:-3]}/USD")
    return held

def run():
    logger.info("[Execute] Starting execution job...")
    try:
        account      = get_account()
        equity       = float(account.equity)
        buying_power = float(account.buying_power)
        positions    = get_positions()
    except Exception as e:
        logger.error(f"[Execute] Alpaca account fetch failed: {e}")
        return {"error": str(e)}

    held     = _normalize_held(positions)
    mv_held  = sum(float(p.market_value or 0) for p in positions)
    max_pos  = max(8, int(equity * 0.5 / 1000))
    slots    = max_pos - len(positions)

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

    # Market hours check
    now_utc     = datetime.now(timezone.utc)
    weekday     = now_utc.weekday()
    market_open = weekday < 5 and (now_utc.hour > 13 or (now_utc.hour == 13 and now_utc.minute >= 30)) and now_utc.hour < 20
    logger.info(f"[Execute] Market: {'OPEN' if market_open else 'CLOSED'}")

    # Pull Active signals + PendingApproval equities (promote them when market opens)
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"]),
            TradingSignal.confidence >= min_conf
        ).order_by(TradingSignal.confidence.desc()).limit(100).all()

        # Promote equity PendingApproval → Active when market opens
        # Crypto is ALWAYS Active — should never be PendingApproval, but guard anyway
        promoted = 0
        for s in sigs:
            if s.status == "PendingApproval":
                _, is_c = normalize_symbol(s.asset_symbol or "")
                if is_c:
                    # Crypto somehow ended up in queue — force Active immediately
                    s.status = "Active"
                    s.updated_date = now_utc.isoformat()
                    promoted += 1
                    logger.warning(f"[Execute] Crypto {s.asset_symbol} was PendingApproval — forcing Active")
                elif market_open:
                    s.status = "Active"
                    s.updated_date = now_utc.isoformat()
                    promoted += 1
        if promoted:
            logger.info(f"[Execute] ↑ Promoted {promoted} signals → Active")

        # Equity signals expire after 4h (stale price levels)
        # Crypto signals expire after 24h — 24/7 market, valid overnight
        cutoff_equity = now_utc - timedelta(hours=4)
        cutoff_crypto = now_utc - timedelta(hours=24)

        sig_dicts = []
        for s in sigs:
            # Resolve generated_at — fall back to created_date, then treat as ageless
            gen_str = s.generated_at or s.created_date or None
            if gen_str:
                try:
                    gen_dt = datetime.fromisoformat(gen_str.replace("Z", "+00:00"))
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    gen_dt = None
            else:
                gen_dt = None  # NULL = treat as ageless

            sym_raw = s.asset_symbol or ""
            _, is_c = normalize_symbol(sym_raw)
            cutoff = cutoff_crypto if is_c else cutoff_equity

            # Only skip if we have a timestamp AND it's definitively stale
            if gen_dt is not None and gen_dt < cutoff:
                logger.debug(f"[Execute] Skip {sym_raw} — signal too old ({gen_dt.isoformat()})")
                continue

            sig_dicts.append({
                "id":           s.id,
                "asset_symbol": sym_raw,
                "asset_class":  s.asset_class or "Equity",
                "direction":    s.direction or "Long",
                "confidence":   float(s.composite_score or s.confidence or 65),
                "entry_price":  s.entry_price,
                "target_price": s.target_price,
                "stop_loss":    s.stop_loss,
                "generated_at": gen_str or "",
            })

    logger.info(f"[Execute] {len(sig_dicts)} active signals qualify (conf>={min_conf})")

    candidates = sig_dicts
    try:
        from lib.risk_manager import filter_correlated
        candidates = filter_correlated(sig_dicts, held, max_per_sector=2)
        logger.info(f"[Execute] {len(candidates)} after correlation filter")
    except Exception as e:
        logger.debug(f"[Execute] Correlation filter skipped: {e}")

    executed = 0
    now_utc  = datetime.now(timezone.utc)
    with get_db() as db:
        for sig in candidates:
            if executed >= slots or budget < 100:
                break

            sym_raw = sig["asset_symbol"]
            sym, crypto = normalize_symbol(sym_raw)

            logger.info(f"[Execute] Evaluating {sym} ({'crypto' if crypto else 'equity'}) conf={sig['confidence']:.0f}%")

            # Skip already held
            if sym in held or sym_raw in held:
                logger.info(f"[Execute] Skip {sym} — already held")
                continue

            # Market-hours gate is enforced by generate_signals (status=PendingApproval when closed).
            # By the time a signal reaches here with status=Active, it is safe to execute.
            # Extra guard: if somehow an equity Active signal exists but market is NOW closed, skip it.
            if not crypto:
                now_check = datetime.now(timezone.utc)
                wd = now_check.weekday()
                mkt_now = wd < 5 and (now_check.hour > 13 or (now_check.hour == 13 and now_check.minute >= 30)) and now_check.hour < 20
                if not mkt_now:
                    logger.debug(f"[Execute] Skip {sym} — equity, market just closed")
                    continue

            entry  = float(sig.get("entry_price")  or 0)
            target = float(sig.get("target_price") or 0)
            stop   = float(sig.get("stop_loss")    or 0)

            if not entry or not target or not stop:
                logger.warning(f"[Execute] Skip {sym} — missing price levels (entry={entry} tp={target} sl={stop})")
                continue
            if stop >= entry:
                logger.warning(f"[Execute] Skip {sym} — invalid: stop ${stop} >= entry ${entry}")
                continue
            if target <= entry:
                logger.warning(f"[Execute] Skip {sym} — invalid: target ${target} <= entry ${entry}")
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

            remaining_slots = max(1, slots - executed)
            per_trade_cap = min(trade_budget, budget / remaining_slots, budget)
            per_trade_cap = max(50.0, per_trade_cap)

            if crypto:
                qty = round(per_trade_cap / entry, 6)
                if qty < 0.0001:
                    logger.warning(f"[Execute] Skip {sym} — qty too small ({qty})")
                    continue
            else:
                qty = max(1, int(per_trade_cap / entry))
                cost = qty * entry
                if cost > budget:
                    logger.warning(f"[Execute] Skip {sym} — cost ${cost:.0f} > budget ${budget:.0f}")
                    continue

            try:
                submit_bracket_order(
                    symbol=sym, qty=qty, entry_price=entry,
                    take_profit=target, stop_loss=stop
                )
                rec = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if rec:
                    rec.status = "Executed"
                    rec.updated_date = now_utc.isoformat()
                held.add(sym)
                budget -= qty * entry
                executed += 1
                logger.info(f"[Execute] ✓ {sym} x{qty} @ ${entry:.4f} TP=${target:.4f} SL=${stop:.4f} | budget left=${budget:.0f}")
            except Exception as e:
                rec = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if rec:
                    rec.status = "Rejected"
                    rec.updated_date = now_utc.isoformat()
                logger.error(f"[Execute] ✗ {sym}: {type(e).__name__}: {e}")

    with get_db() as db:
        pending_count = db.query(TradingSignal).filter(TradingSignal.status == "PendingApproval").count()

    logger.info(f"[Execute] Done — {executed} executed | {pending_count} pending approval | budget=${budget:.0f}")
    return {"executed": executed, "pending_approval": pending_count, "budget_remaining": round(budget, 2)}
