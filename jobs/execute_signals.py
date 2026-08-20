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
    """A symbol in every shape Alpaca uses — delegated to the identity
    registry (lib/instruments.variants), the one authority for this."""
    from lib.instruments import variants
    return variants(sym)


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

    Same direction -> leave the working order completely alone (ongoing
    stop/target management belongs to manage_positions, not to a repeated
    signal). Direction SWAPPED -> close the position so the opposite side
    can enter; that is the only case allowed to change an order.

    Uses the FAST deterministic verify, not deep verify: this runs inside
    the execution loop and a 60-90s LLM round trip there would stall it."""
    from lib.position_reconciler import classify, evaluate_flip
    from lib.alpaca_client import get_trading_client, cancel_open_orders_for_symbol, close_position

    client = get_trading_client()
    position = None
    for p in client.get_all_positions():
        if str(p.symbol).upper().replace("/", "") == sym_raw.upper().replace("/", ""):
            position = p
            break

    if position is None:
        logger.info(f"[Execute] {sym}: entry already working, new signal noted (no duplicate placed)")
        return

    qty = float(position.qty or 0)
    pos_dir = "Short" if qty < 0 else "Long"
    decision = classify(sig, {"qty": qty, "direction": pos_dir})

    if decision == "HOLD":
        logger.info(
            f"[Execute] {sym}: new {sig.get('direction')} matches the open {pos_dir} — "
            f"order left as is (stops/targets are managed by the position sweep)"
        )
        return

    # ── Direction swapped: close the position, let the new side enter ────
    from lib.signal_verification import verify_signal
    avg_entry = float(position.avg_entry_price or 0)
    verification = verify_signal({
        "asset_symbol": sym, "asset_class": sig.get("asset_class"),
        "direction": pos_dir,
        "entry_price": avg_entry,
        "target_price": avg_entry * (1.05 if qty > 0 else 0.95),
        "stop_loss": avg_entry * (0.97 if qty > 0 else 1.03),
    })
    outcome = evaluate_flip(verification)
    logger.warning(
        f"[Execute] {sym}: signal flipped {pos_dir} -> {sig.get('direction')} — "
        f"{'CLOSING to reverse' if outcome['flip'] else 'holding'}: {outcome['reason']}"
    )
    try:
        from lib.alert_engine import raise_alert
        raise_alert(
            source="execution",
            severity="ACTIONABLE" if outcome["flip"] else "WATCH",
            title=f"Direction flip on {sym}",
            detail=(f"Open {pos_dir} position; new signal says {sig.get('direction')}. "
                    f"{outcome['reason']}."),
            dedup_key=f"flip:{sym}:{sig.get('direction')}",
            extra={"symbol": sym},
        )
    except Exception:
        pass

    if not outcome["flip"]:
        return

    try:
        n = cancel_open_orders_for_symbol(position.symbol)
        if n:
            import time as _t
            _t.sleep(1.5)   # let the broker release the reserved balance
        close_position(position.symbol)
        logger.warning(
            f"[Execute] {sym}: {pos_dir} closed for reversal — the {sig.get('direction')} "
            f"signal enters on the next run once the close settles"
        )
        with get_db() as db:
            row = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
            if row:
                row.notes = ((row.notes or "") +
                             "\n[flip] opposite position closed; awaiting entry").strip()
                row.updated_date = now_utc.isoformat()
    except Exception as e:
        logger.error(f"[Execute] {sym}: flip close failed: {e}")


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

    # Market hours from the venue's own calendar (holidays, half-days,
    # DST) — the hard-coded UTC window this replaced was only correct
    # during US daylight time and traded straight through holidays.
    now_utc     = datetime.now(timezone.utc)
    from lib.market_clock import is_equity_market_open
    market_open = is_equity_market_open()
    logger.info(f"[Execute] Market: {'OPEN' if market_open else 'CLOSED'}")

    # Pull Active signals + PendingApproval equities (promote them when market opens).
    #
    # NO SCORE GATE. The composite this query used to filter and sort by is
    # measured INVERTED against outcomes (80+ band won 30.2% over 222
    # trades while <60 won 53.3% over 2,249) — so gating live capital on it
    # preferentially admitted the worst setups and discarded the best.
    # Eligibility is now hard validity only; the statistical decision
    # (lib/gate.gate_v8: measured expectancy + robust lower bound) runs
    # per-signal below and does the actual selection. The legacy gate still
    # RECORDS its verdict on every candidate for the side-by-side
    # experiment — it just doesn't execute anything. See HARDENING_PLAN.md.
    with get_db() as db:
        sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"]),
            or_(TradingSignal.paper_mode == False, TradingSignal.paper_mode.is_(None)),
        ).order_by(TradingSignal.generated_at.desc()).limit(200).all()

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
        gated_rr = gated_conf = gated_ev = gated_life = 0
        # Built once for the whole batch: the walk-forward split reads every
        # closed trade, and doing that per signal would dominate the run.
        try:
            from lib.strategy_lifecycle import evaluate_all
            lifecycle_cache = evaluate_all()
        except Exception as e:
            logger.debug(f"[Execute] lifecycle table unavailable: {e}")
            lifecycle_cache = {"strategies": {}}
        for s in sigs:
            if min_rr > 0 and float(s.rr_ratio or 0) < min_rr:
                gated_rr += 1
                continue
            # The v8 gate: validity + measured expectancy WITH its robust
            # lower bound, evaluated at the moment money would be committed
            # (spread, funding and the win/loss distribution all move
            # between generation and execution).
            #
            # This replaces the old policy of letting UNKNOWN pass. The
            # "if we never trade it we never learn" argument died when the
            # candidate/counterfactual pipeline shipped — rejected setups
            # are resolved and learned from WITHOUT spending capital, so
            # UNKNOWN generates its evidence in paper, not here
            # (ALLOW_EXPERIMENTAL_LIVE=1 is the explicit operator override).
            # TENTATIVE — point estimate clears, lower bound doesn't — is
            # paper too: computing uncertainty and ignoring it at the
            # capital boundary was P0.11's exact complaint.
            from lib.gate import gate_v8
            g = gate_v8({
                "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
                "direction": s.direction, "timeframe": s.timeframe,
                "entry_price": s.entry_price, "stop_loss": s.stop_loss,
                "target_price": s.target_price,
                "strategy": getattr(s, "strategy", None),
            })
            if not g["take"]:
                gated_ev += 1
                logger.info("[Execute] %s %s — %s",
                            g["decision"], s.asset_symbol, g.get("reason"))
                continue
            s._gate_net_r = g.get("net_r")   # for net-R ordering below

            # Strategy lifecycle, judged OUT OF SAMPLE. A strategy that was
            # profitable on the trades used to rank it and is not on later
            # ones was curve-fitted; SHADOW and DISABLED size to zero so it
            # keeps being measured without risking money. An unmeasured
            # strategy is EXPERIMENTAL, not blocked — refusing the unknown
            # is how the system stops generating the evidence it needs.
            try:
                from lib.strategy_lifecycle import state_of
                life = state_of(getattr(s, "strategy", None), cache=lifecycle_cache)
                if life["size_multiplier"] <= 0:
                    gated_life += 1
                    logger.info("[Execute] %s blocked — strategy %s is %s: %s",
                                s.asset_symbol, getattr(s, "strategy", None),
                                life["state"], life["reason"])
                    continue
            except Exception as e:
                logger.debug(f"[Execute] lifecycle gate unavailable: {e}")
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
                # The actual LLM confidence, DIAGNOSTIC ONLY. This field
                # used to receive the composite score, which risk_manager
                # then clamped into a Kelly win probability — a number
                # measured inverted against outcomes, bet as if it were
                # p(win). Nothing sizes from this field anymore.
                "confidence":   float(s.confidence or 0),
                "entry_price":  s.entry_price,
                "target_price": s.target_price,
                "stop_loss":    s.stop_loss,
                "timeframe":    s.timeframe,
                "generated_at": gen_str or "",
                "expires_at": getattr(s, "expires_at", None),
                "data_quality_score": getattr(s, "data_quality_score", None),
                "freshness_score": getattr(s, "freshness_score", None),
                "strategy": getattr(s, "strategy", None),
                "net_r": getattr(s, "_gate_net_r", None),
            })

    logger.info(
        f"[Execute] {len(sig_dicts)} signals pass the v8 gate "
        f"(no score gate — ranked by measured net R; rr>={min_rr:g}, conf>={min_ai_conf:g}; "
        f"gated: {gated_rr} by R:R, {gated_conf} by confidence, "
        f"{gated_ev} by gate_v8 (NO_TRADE/TENTATIVE/UNKNOWN), "
        f"{gated_life} by strategy lifecycle)"
    )

    # Ranked by MEASURED net expectancy, best edge first — the old sort was
    # the composite score, i.e. the inverted number decided who got budget
    # when slots ran short. None sorts last.
    sig_dicts.sort(key=lambda d: (d.get("net_r") is None, -(d.get("net_r") or 0.0)))

    candidates = sig_dicts
    try:
        from lib.risk_manager import filter_correlated
        candidates = filter_correlated(sig_dicts, held, max_per_sector=2)
        logger.info(f"[Execute] {len(candidates)} after correlation filter")
    except Exception as e:
        logger.debug(f"[Execute] Correlation filter skipped: {e}")

    executed = 0
    now_utc  = datetime.now(timezone.utc)
    # WARM THE TRADABLE-SYMBOL CACHE BEFORE OPENING THE TRANSACTION.
    #
    # get_tradable_crypto_symbols() is memoised in a module-level set, but
    # the FIRST call performs an Alpaca round-trip. It was reached from
    # inside the session block below, which also writes (row.paper_mode),
    # so on a cold cache one HTTP request was held inside an open SQLite
    # write transaction while every other writer waited on the lock.
    #
    # Nothing about the logic changes: the in-block call now always hits a
    # warm cache. This is the collect-externally / then-persist shape, and
    # it costs one call that was going to happen anyway.
    try:
        from lib.alpaca_client import get_tradable_crypto_symbols as _warm
        _warm()
    except Exception as e:                                    # noqa: BLE001
        # A cold cache is not a reason to skip execution -- the in-block
        # call will simply pay the round-trip as it did before.
        logger.debug(f"[Execute] tradable-symbol prewarm skipped: {e}")

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
                if not is_equity_market_open():
                    logger.debug(f"[Execute] Skip {sym} — equity, market just closed")
                    continue

            # Strict side, before anything else touches this signal. The
            # live path is long-only (shorts route to paper at creation),
            # so the ONLY acceptable parse here is an affirmative LONG.
            # The old check accepted any direction starting with "long" or
            # the letter b — which mapped "Bounce" to buy, but also
            # "Bearish". Unknown is a validation failure, never a long.
            from lib.trade_side import LONG, parse_side_strict
            side_strict = parse_side_strict(sig.get("direction"))
            if side_strict != LONG:
                logger.warning(f"[Execute] Skip {sym} — direction "
                               f"{sig.get('direction')!r} is not an affirmative "
                               f"long (parsed: {side_strict}); live path is long-only")
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

            # Position sizing. FAIL CLOSED: if the risk engine errors, the
            # answer is no trade — the old fallback priced a budget from
            # "confidence" when sizing crashed, which meant the safety
            # layer could fail and the system traded anyway (P0.3).
            try:
                from lib.risk_manager import calculate_position_size
                from lib.strategy_lifecycle import state_of
                life_mult = 1.0
                try:
                    life_mult = float(state_of(sig.get("strategy"),
                                               cache=lifecycle_cache)["size_multiplier"])
                except Exception:
                    life_mult = 1.0   # unmeasured strategy ≠ blocked; sizing floor is P0.10's job
                sz = calculate_position_size(sig, equity, regime,
                                             lifecycle_multiplier=life_mult)
                if sz.rejection_reason:
                    logger.info(f"[Execute] Skip {sym} — risk mgr: {sz.rejection_reason}")
                    continue
                trade_budget = min(sz.dollar_size, budget)
            except Exception as e:
                logger.error(f"[Execute] RISK ENGINE ERROR for {sym} — NO_TRADE "
                             f"(fail closed, never priced from confidence): {e}")
                continue

            # Downstream may only REDUCE the approved size (P0.4) — slot
            # division and the global budget shrink it; nothing enlarges it.
            # The old "conviction multiplier" (1-2x from the score, applied
            # AFTER risk approval) is deleted: execution never increases a
            # risk decision (invariant #10).
            remaining_slots = max(1, slots - executed)
            per_trade_cap = min(trade_budget, budget / remaining_slots, budget)

            if crypto:
                qty = round(per_trade_cap / entry, 6)
                if qty < 0.0001:
                    logger.warning(f"[Execute] Skip {sym} — qty too small ({qty})")
                    continue
            else:
                raw_qty = per_trade_cap / entry
                if raw_qty < 1:
                    # One share costs more than the approved budget. The old
                    # code rounded UP to 1 with a 25% overshoot tolerance —
                    # execution enlarging a risk decision, exactly what
                    # invariant #10 forbids. The correct size for a trade
                    # too small to express is zero.
                    logger.warning(
                        f"[Execute] Skip {sym} — one share (${entry:.2f}) exceeds "
                        f"the approved budget ${per_trade_cap:.0f}"
                    )
                    continue
                qty = int(raw_qty)
                cost = qty * entry
                if cost > budget:
                    logger.warning(f"[Execute] Skip {sym} — cost ${cost:.0f} > budget ${budget:.0f}")
                    continue

            # ── The plan, checked against the approval (invariant #10) ────
            # Typed and explicit: whatever the paths above produced, the
            # order that leaves this function cannot exceed what the risk
            # engine approved. This is the last gate before the venue.
            from lib.decision_types import OrderPlan, RiskDecision
            approved = RiskDecision(
                allowed_risk_usd=float(getattr(sz, "dollar_size", 0) or 0),
                stop_distance=abs(entry - stop), qty=float(sz.dollar_size / entry),
                notional=float(sz.dollar_size), margin=float(sz.dollar_size),
                leverage=1.0)
            plan = OrderPlan(symbol=sym, venue="alpaca", side="long",
                             order_type="market", qty=float(qty), entry=entry,
                             initial_stop=stop, target=target,
                             notional=float(qty) * entry)
            gate = plan.check(approved)
            if not gate.ok:
                logger.error(f"[Execute] INVARIANT VIOLATION blocked for {sym}: "
                             f"{gate.reason} — skipping")
                continue

            try:
                # Book and tape as they stand BEFORE the order goes out.
                # Measured after the fill they are contaminated by the fill
                # itself — the order moves the book it would be judged
                # against. Recorded whether or not it fills: an order that
                # does not fill is a real observation about liquidity, and
                # keeping only the successful ones biases the dataset
                # toward moments when trading was easy.
                exec_row = None
                try:
                    from lib.execution_recorder import record_intent
                    exec_row = record_intent(
                        signal_id=sig.get("id"), symbol=sym,
                        side="buy",   # strict-parsed LONG above; this path is long-only
                        order_type="market", intended_price=entry, qty=qty,
                        stop_loss=stop, asset_class=sig.get("asset_class"),
                        venue="alpaca",
                    )
                except Exception as e:
                    logger.debug(f"[Execute] execution snapshot skipped for {sym}: {e}")

                order = submit_bracket_order(
                    symbol=sym, qty=qty, entry_price=entry,
                    take_profit=target, stop_loss=stop
                )
                broker_id = str((order or {}).get("id") or "") or None

                rec = db.query(TradingSignal).filter(TradingSignal.id == sig["id"]).first()
                if rec:
                    rec.status = "Executed"
                    rec.updated_date = now_utc.isoformat()
                    # The return value was previously discarded, so
                    # alpaca_order_id was never set by this path — only the
                    # manual /signals/{id}/execute route wrote it. Measured:
                    # 643 signals marked Executed, 7 with an order id.
                    #
                    # That is not cosmetic. _record_slippage gates on
                    # `if not sig.alpaca_order_id: return`, so every
                    # automatically-executed fill was skipped — which is why
                    # 4 of 39,821 signals carry a measured slippage, and why
                    # the execution recorder would have collected intents
                    # and never a single fill.
                    if broker_id:
                        rec.alpaca_order_id = broker_id

                # Pair the broker id onto the pre-submit snapshot so the
                # fill can later be joined to the book state that preceded it.
                if exec_row and broker_id:
                    try:
                        from app.database import ExecutionSample
                        es = db.query(ExecutionSample).filter(
                            ExecutionSample.id == exec_row).first()
                        if es is not None:
                            es.broker_order_id = broker_id
                    except Exception as e:
                        logger.debug(f"[Execute] could not tag execution sample: {e}")
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
