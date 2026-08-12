"""
Job: Execute Signals v6.5
- generate_signals sets status=Active when market is open, PendingApproval when closed
- execute promotes PendingApproval → Active at run-time if market has since opened
- Once status=Active, execute fires immediately — no manual approval needed
- No more duplicate PendingApproval writes from execute job
"""
import logging, os
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal
from lib.alpaca_client import (get_account, get_positions, submit_bracket_order, normalize_symbol,
                               is_crypto, get_trading_client)
from sqlalchemy import or_, func

logger = logging.getLogger(__name__)

def _both_formats(sym: str) -> set:
    """A symbol in BOTH shapes Alpaca uses: SOL/USD and SOLUSD."""
    sym = str(sym).upper().strip()
    out = {sym}
    if len(sym) > 3 and sym.endswith("USD") and sym[:-3].isalpha():
        out.add(f"{sym[:-3]}/USD")
    if "/" in sym:
        out.add(sym.replace("/", ""))
    return out


def _normalize_held(positions):
    """Build a set of held symbols in BOTH formats: SOL/USD and SOLUSD."""
    held = set()
    for p in positions:
        held |= _both_formats(p.symbol)
    return held


def _symbols_with_pending_entries(client) -> set:
    """Symbols that already have an UNFILLED entry order working.

    A market buy that has not filled yet creates no position, so the
    held-set alone let a second run buy the same symbol again — observed
    live: two concurrent RENDER/USD market buys, which would have doubled
    the intended position on fill."""
    pending = set()
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) or []:
            side = str(getattr(o, "side", "")).lower()
            otype = str(getattr(o, "order_type", "")).lower()
            # Only ENTRY orders block a re-buy; protective sells must not.
            if "buy" in side and ("market" in otype or "limit" in otype):
                pending |= _both_formats(o.symbol)
    except Exception as e:
        logger.warning(f"[Execute] Could not read pending orders (duplicate guard degraded): {e}")
    return pending

def _reconcile_existing(sig: dict, sym: str, sym_raw: str, now_utc) -> None:
    """A signal arrived for a symbol already held or pending.

    Same direction -> tighten/refresh the protective orders (never loosen a
    stop). Opposite direction -> deep-verify the OPEN position and close it
    only when the evidence says the position itself is failing; otherwise
    keep it and alert. See lib/position_reconciler.py for the rules."""
    from lib.position_reconciler import classify, plan_amendment, evaluate_conflict
    from lib.alpaca_client import get_trading_client, cancel_open_orders_for_symbol

    client = get_trading_client()
    position = None
    for p in client.get_all_positions():
        if str(p.symbol).upper().replace("/", "") == sym_raw.upper().replace("/", ""):
            position = p
            break

    if position is None:
        # Held only by an unfilled entry order — nothing to amend yet.
        logger.info(f"[Execute] {sym}: entry already working, new signal noted (no duplicate placed)")
        return

    qty = float(position.qty or 0)
    pos_dict = {
        "qty": qty,
        "direction": "Short" if qty < 0 else "Long",
        "stop_loss": None,     # broker-side; read from working orders below
        "target_price": None,
        "avg_entry_price": float(position.avg_entry_price or 0),
        "current_price": float(position.current_price or 0),
    }
    # Recover current protective levels from the working orders.
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[position.symbol])) or []:
            otype = str(getattr(o, "order_type", "")).lower()
            if "stop" in otype and getattr(o, "stop_price", None):
                pos_dict["stop_loss"] = float(o.stop_price)
            elif "limit" in otype and getattr(o, "limit_price", None) and "stop" not in otype:
                pos_dict["target_price"] = float(o.limit_price)
    except Exception as e:
        logger.debug(f"[Execute] Could not read protective orders for {sym}: {e}")

    decision = classify(sig, pos_dict)

    if decision == "CONFLICT":
        from lib.signal_verification import deep_verify_signal
        verification = deep_verify_signal({
            "asset_symbol": sym, "asset_class": sig.get("asset_class"),
            "direction": pos_dict["direction"],
            "entry_price": pos_dict["avg_entry_price"],
            "target_price": pos_dict["target_price"] or pos_dict["avg_entry_price"] * (1.05 if qty > 0 else 0.95),
            "stop_loss": pos_dict["stop_loss"] or pos_dict["avg_entry_price"] * (0.97 if qty > 0 else 1.03),
            "timeframe": sig.get("timeframe"),
        })
        outcome = evaluate_conflict(verification)
        logger.warning(
            f"[Execute] {sym}: new {sig.get('direction')} contradicts open {pos_dict['direction']} — "
            f"{'CLOSING' if outcome['close'] else 'keeping'}: {outcome['reason']}"
        )
        try:
            from lib.alert_engine import raise_alert
            raise_alert(
                source="execution",
                severity="ACTIONABLE" if outcome["close"] else "WATCH",
                title=f"Signal conflict on {sym}",
                detail=(f"New {sig.get('direction')} signal against an open {pos_dict['direction']} "
                        f"position. {outcome['reason']}."),
                dedup_key=f"conflict:{sym}:{pos_dict['direction']}",
                extra={"symbol": sym},
            )
        except Exception:
            pass
        if outcome["close"]:
            from lib.alpaca_client import close_position
            cancel_open_orders_for_symbol(position.symbol)
            close_position(position.symbol)
            logger.warning(f"[Execute] {sym}: position closed on verified conflict — new signal may enter next run")
        return

    plan = plan_amendment(sig, pos_dict)
    if not plan:
        logger.info(f"[Execute] {sym}: new signal offers no better levels — position left as is")
        return

    changes = plan["changes"]
    if plan.get("notes"):
        logger.info(f"[Execute] {sym}: {plan['notes']}")

    # Re-issue protection at the improved levels: cancel the old legs, then
    # place the new stop (the sweep in manage_positions re-adds a take-profit).
    new_stop = changes.get("stop_loss")
    if new_stop:
        try:
            from jobs.manage_positions import _set_crypto_limit_stop, _set_trailing_stop_equity
            price = pos_dict["current_price"] or pos_dict["avg_entry_price"]
            cancel_open_orders_for_symbol(position.symbol)
            is_c = "/" in str(position.symbol) or str(position.symbol).upper().endswith("USD")
            if is_c and price > 0:
                stop_pct = abs(price - new_stop) / price * 100
                ok = _set_crypto_limit_stop(client, position.symbol, abs(qty), price, stop_pct)
            else:
                stop_pct = abs(price - new_stop) / price * 100 if price else 2.0
                ok = _set_trailing_stop_equity(client, position.symbol, abs(qty), stop_pct)
            logger.info(
                f"[Execute] {sym}: stop {'tightened' if ok else 'tighten FAILED'} to {new_stop:g} "
                f"from a fresh {sig.get('direction')} signal"
            )
        except Exception as e:
            logger.warning(f"[Execute] {sym}: stop amendment failed: {e}")

    # Record the amendment on the signal so the UI shows what happened.
    try:
        with get_db() as db:
            row = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
            if row:
                row.status = "Merged"
                row.notes = ((row.notes or "") + "\n[reconcile] amended open position: {}".format(changes)).strip()
                row.updated_date = now_utc.isoformat()
    except Exception as e:
        logger.debug(f"[Execute] Could not annotate signal {sig['id']}: {e}")


def run():
    logger.info("[Execute] Starting execution job...")

    from lib.kill_switch import get_kill_switch_state
    kill_state = get_kill_switch_state()
    if not kill_state["live_trading_enabled"]:
        logger.warning(f"[Execute] Live trading is paused ({kill_state.get('paused_reason')}) — skipping")
        return {"executed": 0, "reason": "trading_paused", "paused_reason": kill_state.get("paused_reason")}

    try:
        account      = get_account()
        equity       = float(account.equity)
        buying_power = float(account.buying_power)
        positions    = get_positions()
    except Exception as e:
        logger.error(f"[Execute] Alpaca account fetch failed: {e}")
        return {"error": str(e)}

    held     = _normalize_held(positions)
    # Unfilled entry orders count as "already committed" — otherwise a
    # pending market buy is invisible and the next run buys the symbol again.
    try:
        held |= _symbols_with_pending_entries(get_trading_client())
    except Exception as e:
        logger.warning(f"[Execute] Pending-entry guard unavailable: {e}")
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

    # Live-execution criteria: user-configurable floors (Ops → Execution
    # Criteria), with the high-risk regime bump as a floor that can raise but
    # never lower the user's setting. Auto Sim takes every approved signal
    # regardless — these gates only decide what reaches the broker account.
    from lib.trading_preferences import get_user_preference
    prefs = get_user_preference()
    user_min_score = float(prefs.get("live_min_score", 55.0))
    min_rr = float(prefs.get("live_min_rr", 0.0))
    min_ai_conf = float(prefs.get("live_min_confidence", 0.0))
    regime_floor = 75 if regime.get("risk") == "high" else 0
    min_conf = max(user_min_score, regime_floor)

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
    # Gate/order by the composite evidence score (10-factor, includes earnings/staleness/
    # conflict penalties) rather than raw LLM confidence — falls back to confidence only
    # when composite_score hasn't been computed for a signal yet.
    score_expr = func.coalesce(TradingSignal.composite_score, TradingSignal.confidence)
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"]),
            or_(TradingSignal.paper_mode == False, TradingSignal.paper_mode.is_(None)),
            score_expr >= min_conf
        ).order_by(score_expr.desc()).limit(100).all()

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
        gated_rr = gated_conf = 0
        for s in sigs:
            if min_rr > 0 and float(s.rr_ratio or 0) < min_rr:
                gated_rr += 1
                continue
            if min_ai_conf > 0 and float(s.confidence or 0) < min_ai_conf:
                gated_conf += 1
                continue
            expires_at = None
            if getattr(s, "expires_at", None):
                try:
                    expires_at = datetime.fromisoformat(s.expires_at.replace("Z", "+00:00"))
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    expires_at = None
            if expires_at and expires_at <= now_utc:
                s.status = "Expired"
                s.updated_date = now_utc.isoformat()
                logger.info("[Execute] Expired %s at its setup-specific deadline", s.asset_symbol)
                continue

            min_data_quality = float(os.getenv("MIN_SIGNAL_DATA_QUALITY", "35"))
            min_freshness = float(os.getenv("MIN_SIGNAL_FRESHNESS", "20"))
            if getattr(s, "data_quality_score", None) is not None and s.data_quality_score < min_data_quality:
                logger.info("[Execute] Skip %s - data quality %.1f < %.1f", s.asset_symbol, s.data_quality_score, min_data_quality)
                continue
            if getattr(s, "freshness_score", None) is not None and s.freshness_score < min_freshness:
                logger.info("[Execute] Skip %s - freshness %.1f < %.1f", s.asset_symbol, s.freshness_score, min_freshness)
                continue
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
                "timeframe":    s.timeframe,
                "generated_at": gen_str or "",
                "expires_at": getattr(s, "expires_at", None),
                "data_quality_score": getattr(s, "data_quality_score", None),
                "freshness_score": getattr(s, "freshness_score", None),
            })

    logger.info(
        f"[Execute] {len(sig_dicts)} active signals qualify "
        f"(score>={min_conf:g}, rr>={min_rr:g}, conf>={min_ai_conf:g}; "
        f"gated: {gated_rr} by R:R, {gated_conf} by confidence)"
    )

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

            # Already committed to this symbol (open position OR unfilled
            # entry order). A new signal here is an UPDATE, not a second
            # trade — reconcile instead of discarding the information.
            if sym in held or sym_raw in held:
                try:
                    _reconcile_existing(sig, sym, sym_raw, now_utc)
                except Exception as e:
                    logger.warning(f"[Execute] Reconcile failed for {sym}: {e}")
                continue

            # Crypto the signal pipeline covers but Alpaca doesn't list can
            # never fill — submitting anyway produced a day of repeated
            # 'asset not found' APIErrors (INJ/OP/SUI/BNB/ATOM). Mark such
            # signals paper-only so the candidate filter excludes them from
            # live execution permanently while the paper engine keeps them.
            if crypto:
                from lib.alpaca_client import get_tradable_crypto_symbols
                tradable = get_tradable_crypto_symbols()
                if tradable is not None and sym not in tradable:
                    row = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                    if row:
                        row.paper_mode = True
                        row.updated_date = now_utc.isoformat()
                    logger.info(f"[Execute] {sym} not tradable on Alpaca — routed to paper only")
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

            # Horizon stop cap (user spec): scalps risk at most 3% of entry,
            # longer trades at most 10%. A wider signal stop gets clamped —
            # the trade still happens, just with the risk ceiling enforced.
            from lib.trading_preferences import horizon_for_timeframe
            cap_pct = 0.03 if horizon_for_timeframe(sig.get("timeframe")) == "scalp" else 0.10
            min_allowed_stop = entry * (1.0 - cap_pct)
            if stop < min_allowed_stop:
                logger.info(
                    f"[Execute] {sym}: stop clamped {stop:.4g} -> {min_allowed_stop:.4g} "
                    f"({cap_pct:.0%} {horizon_for_timeframe(sig.get('timeframe'))} cap)"
                )
                stop = round(min_allowed_stop, 6)
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

            # Conviction sizing (Alpaca's realistic leverage band): the same
            # score->leverage idea the virtual books use at 5-100x, expressed
            # here as 1x-2x notional scaling. Equities only — Alpaca margin
            # covers 2x on stocks; crypto is non-marginable and stays 1x.
            score = float(sig.get("confidence", 55))
            conviction_mult = max(1.0, min(2.0, 1.0 + (score - 55.0) / 45.0))
            if not crypto and conviction_mult > 1.0:
                per_trade_cap = min(per_trade_cap * conviction_mult, budget)
                logger.debug(f"[Execute] {sym}: conviction x{conviction_mult:.2f} (score {score:.0f})")

            if crypto:
                qty = round(per_trade_cap / entry, 6)
                if qty < 0.0001:
                    logger.warning(f"[Execute] Skip {sym} — qty too small ({qty})")
                    continue
            else:
                raw_qty = per_trade_cap / entry
                if raw_qty < 1:
                    # Forcing a minimum of 1 share can blow well past the risk-sized
                    # allocation for expensive stocks — only round up to 1 when the
                    # overshoot is minor; otherwise the entry price is simply too high
                    # for this trade's risk budget and it should be skipped.
                    if entry > trade_budget * 1.25:
                        logger.warning(
                            f"[Execute] Skip {sym} — entry ${entry:.2f} exceeds risk-sized "
                            f"budget ${trade_budget:.0f} for even 1 share"
                        )
                        continue
                    qty = 1
                else:
                    qty = int(raw_qty)
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
