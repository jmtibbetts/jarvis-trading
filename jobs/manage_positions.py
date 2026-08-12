from app.routes import log_decision
"""
Job: Manage Positions v6.8
- Every position evaluated against fresh TA + recent news/threats every cycle
- LLM reviews each position and returns: HOLD | TIGHTEN_STOP | EXIT
- Hard deterministic rules still fire first (no LLM latency on urgent closes)
- Separate crypto/equity tier thresholds
- Crypto: -4% stop, trail at +2%/+5%, take profit at +10%
- Equity: -5% stop, trail at +5%/+10%, take profit at +15%
"""
import logging, uuid, json, math, os, time
from datetime import datetime, timezone, timedelta
from app.database import get_db, TradingSignal, Position, PortfolioSnapshot, ThreatEvent, NewsItem
from lib.alpaca_client import get_positions, get_account, close_position, cancel_open_orders_for_symbol, get_trading_client, normalize_symbol
from lib.learning_engine import record_trade_outcome
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


def _env_float(name: str, default: float, min_value: float = None, max_value: float = None) -> float:
    try:
        value = float(os.getenv(name, default))
    except Exception:
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


MAX_LOSS_PER_TRADE_USD = _env_float("MAX_LOSS_PER_TRADE_USD", 15.0, 10.0, 20.0)
PROFIT_LOCK_USD = _env_float("PROFIT_LOCK_USD", 10.0, 0.0, 50.0)
MIN_DYNAMIC_TRAIL_PCT = _env_float("MIN_DYNAMIC_TRAIL_PCT", 0.75, 0.1, 5.0)
TARGET_REWARD_MULTIPLIER = _env_float("TARGET_REWARD_MULTIPLIER", 2.8, 1.2, 6.0)
EXIT_ORDER_REPRICE_PCT = _env_float("EXIT_ORDER_REPRICE_PCT", 0.25, 0.05, 2.0)
ALLOW_STANDALONE_TAKE_PROFIT = os.getenv("ALLOW_STANDALONE_TAKE_PROFIT", "false").lower() in ("1", "true", "yes")
SCALE_OUT_ENABLED = os.getenv("SCALE_OUT_ENABLED", "true").lower() in ("1", "true", "yes")
SCALE_OUT_FRACTION = _env_float("SCALE_OUT_FRACTION", 0.5, 0.1, 0.9)
SCALE_OUT_TP1_PCT_OF_TARGET = _env_float("SCALE_OUT_TP1_PCT_OF_TARGET", 0.5, 0.2, 0.9)


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


def _position_is_crypto(pos) -> bool:
    """Use broker asset class so equity tickers BTC/ETH are not treated as coins."""
    asset_class = str(getattr(pos, "asset_class", "") or "").lower()
    if "crypto" in asset_class:
        return True
    if "equity" in asset_class:
        return False
    return _is_crypto(str(getattr(pos, "symbol", "") or ""))


def _alpaca_sym(sym: str) -> str:
    """Normalize symbol to Alpaca format for order submission (no slash)."""
    s = sym.upper().strip()
    if "/" in s:
        return s.replace("/", "")
    return s

def _sym_variants(sym: str, is_crypto: bool = False) -> list:
    """Return DB forms without conflating BTC/ETH equity tickers with coins."""
    s = sym.upper().strip()
    if not is_crypto:
        return [s]
    base = s.split("/", 1)[0] if "/" in s else (s[:-3] if s.endswith("USD") else s)
    return [f"{base}/USD", f"{base}USD"]


def _primary_ta(ta_data: dict) -> dict:
    for tf in ("4H", "1H", "1D"):
        data = ta_data.get(tf) if isinstance(ta_data, dict) else None
        if data and not data.get("error"):
            return data
    return {}


def _trend_strength(primary: dict, is_long: bool) -> float:
    """Return 0..1 trend support used to stretch or tighten targets."""
    if not primary:
        return 0.4
    score = 0.0
    price = (primary.get("price") or {}).get("last") or 0
    emas = primary.get("emas") or {}
    macd = primary.get("macd") or {}
    adx = primary.get("adx") or {}
    vwap = primary.get("vwap") or {}
    rsi = primary.get("rsi") or 50

    if is_long:
        score += 0.2 if price and emas.get("ema21") and price > emas["ema21"] else 0
        score += 0.2 if price and emas.get("ema50") and price > emas["ema50"] else 0
        score += 0.2 if macd.get("trend") == "bullish" else 0
        score += 0.15 if vwap.get("position") == "above" else 0
        score += 0.15 if 45 <= rsi <= 72 else 0
    else:
        score += 0.2 if price and emas.get("ema21") and price < emas["ema21"] else 0
        score += 0.2 if price and emas.get("ema50") and price < emas["ema50"] else 0
        score += 0.2 if macd.get("trend") == "bearish" else 0
        score += 0.15 if vwap.get("position") == "below" else 0
        score += 0.15 if 28 <= rsi <= 55 else 0
    score += 0.1 if adx.get("strong") else 0
    return max(0.0, min(1.0, score))


def _exit_levels_are_sane(reference_price: float, side: str, stop_price: float,
                          target_price: float, is_crypto: bool = False) -> bool:
    if reference_price <= 0 or stop_price <= 0 or target_price <= 0:
        return False
    is_short = "short" in str(side or "").lower()
    directional = (
        target_price < reference_price < stop_price if is_short
        else stop_price < reference_price < target_price
    )
    max_distance = 0.40 if is_crypto else 0.25
    return bool(
        directional
        and abs(stop_price - reference_price) / reference_price <= max_distance
        and abs(target_price - reference_price) / reference_price <= max_distance
    )


def _dynamic_exit_plan(pos, current_price: float, ta_data: dict, original_signal: dict,
                       is_crypto: bool = False) -> dict:
    """
    Build a stop/target plan that caps dollar risk and trails winners.
    Stops only tighten relative to the signal's current stop; they do not loosen.
    """
    sym = str(pos.symbol)
    qty = abs(float(pos.qty or 0))
    avg = float(pos.avg_entry_price or 0)
    side_raw = str(pos.side or "long").lower()
    is_long = "short" not in side_raw
    pl = float(pos.unrealized_pl or 0)

    if qty <= 0 or avg <= 0 or current_price <= 0:
        return {"ok": False, "reason": "missing position values"}

    if pl <= -MAX_LOSS_PER_TRADE_USD:
        return {
            "ok": True,
            "action": "EXIT",
            "reason": f"P&L ${pl:.2f} breached max loss ${MAX_LOSS_PER_TRADE_USD:.2f}",
        }

    primary = _primary_ta(ta_data)
    atr_data = primary.get("atr") or {}
    atr_value = float(atr_data.get("value") or 0)
    atr_pct = float(atr_data.get("pct") or 0)
    if atr_value > current_price * 0.20:
        atr_value = 0.0
    if atr_value <= 0:
        atr_value = current_price * max(MIN_DYNAMIC_TRAIL_PCT, min(3.0, atr_pct or 1.0)) / 100.0

    risk_per_unit = MAX_LOSS_PER_TRADE_USD / qty
    profit_lock_per_unit = PROFIT_LOCK_USD / qty if PROFIT_LOCK_USD > 0 else 0
    trail_distance = max(atr_value * 1.15, current_price * MIN_DYNAMIC_TRAIL_PCT / 100.0)
    strength = _trend_strength(primary, is_long)
    reward_mult = TARGET_REWARD_MULTIPLIER + (strength * 1.4)

    old_stop = float(original_signal.get("stop_loss") or 0)
    old_target = float(original_signal.get("target_price") or 0)

    if is_long:
        max_loss_stop = avg - risk_per_unit
        trailing_stop = current_price - trail_distance
        new_stop = max(max_loss_stop, trailing_stop, old_stop or 0)
        if pl >= PROFIT_LOCK_USD and profit_lock_per_unit > 0:
            new_stop = max(new_stop, avg + profit_lock_per_unit)
        new_stop = min(new_stop, current_price * 0.995)

        target_distance = max(atr_value * (2.0 + strength * 2.0), (current_price - new_stop) * reward_mult)
        new_target = max(old_target or 0, current_price + target_distance)
    else:
        max_loss_stop = avg + risk_per_unit
        trailing_stop = current_price + trail_distance
        stop_candidates = [max_loss_stop, trailing_stop]
        if old_stop > 0:
            stop_candidates.append(old_stop)
        new_stop = min(stop_candidates)
        if pl >= PROFIT_LOCK_USD and profit_lock_per_unit > 0:
            new_stop = min(new_stop, avg - profit_lock_per_unit)
        new_stop = max(new_stop, current_price * 1.005)

        target_distance = max(atr_value * (2.0 + strength * 2.0), (new_stop - current_price) * reward_mult)
        new_target = min(old_target if old_target > 0 else current_price - target_distance, current_price - target_distance)

    precision = 6 if current_price < 1 else 2
    stop_price = round(max(new_stop, 0.000001), precision)
    target_price = round(max(new_target, 0.000001), precision)
    side = "long" if is_long else "short"
    if not _exit_levels_are_sane(current_price, side, stop_price, target_price, is_crypto):
        return {"ok": False, "reason": "exit levels failed price sanity checks"}

    return {
        "ok": True,
        "action": "ADJUST",
        "stop_price": stop_price,
        "target_price": target_price,
        "strength": round(strength, 2),
        "atr_pct": round(atr_pct, 3),
        "risk_dollars": MAX_LOSS_PER_TRADE_USD,
        "side": side,
    }


def _record_slippage(signal_id: str, intended_entry: float, actual_fill_price: float, now_iso: str) -> None:
    """Persist the gap between a signal's intended entry price and the broker's
    actual fill price, the first time a live position for it is observed. Only
    meaningful for live (non-paper) fills — paper fills always fill exactly at
    the requested price, so there's no execution quality to measure there."""
    if not signal_id or not intended_entry or not actual_fill_price:
        return
    try:
        with get_db() as db:
            sig = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
            if sig and sig.actual_fill_price is None:
                sig.actual_fill_price = actual_fill_price
                sig.slippage_pct = round((actual_fill_price - intended_entry) / intended_entry * 100, 4)
                sig.fill_recorded_at = now_iso
    except Exception as e:
        logger.debug(f"[Positions] Slippage record failed for signal {signal_id}: {e}")


def _maybe_scale_out(sym: str, alpaca_sym: str, qty: float, avg: float, current_price: float,
                       is_c: bool, original_signal: dict, current_regime: str, now_iso: str) -> bool:
    """Lock in partial profit at an intermediate target (TP1) instead of an
    all-or-nothing exit: close SCALE_OUT_FRACTION of the position, move the
    remaining runner's stop to breakeven, and let it ride toward the original
    target. Only fires once per position (tracked via TradingSignal.scaled_out)
    and only for long positions — live execution is long-only today."""
    if not SCALE_OUT_ENABLED or qty <= 0:
        return False
    sig_id = original_signal.get("id")
    entry = float(original_signal.get("entry_price") or 0)
    target = float(original_signal.get("target_price") or 0)
    if not sig_id or not entry or not target or target <= entry:
        return False
    if original_signal.get("scaled_out"):
        return False

    tp1_price = entry + (target - entry) * SCALE_OUT_TP1_PCT_OF_TARGET
    if current_price < tp1_price:
        return False

    close_qty = _safe_qty(qty * SCALE_OUT_FRACTION, is_c)
    remaining_qty = _safe_qty(qty - close_qty, is_c)
    # Both halves must be tradeable — don't scale a position too small to split.
    if close_qty <= 0 or remaining_qty <= 0:
        return False

    try:
        from lib.alpaca_client import partial_close_position
        partial_close_position(alpaca_sym, close_qty)
    except Exception as e:
        logger.warning(f"[Positions] Scale-out partial close failed {sym}: {e}")
        return False

    try:
        record_trade_outcome(
            symbol=sym, asset_class="crypto" if is_c else "equity",
            direction="long", entry_price=avg, exit_price=current_price,
            qty=close_qty, exit_reason="SCALE_OUT_TP1",
            signal_id=sig_id, timeframe=original_signal.get("timeframe", "1D"),
            signal_confidence=original_signal.get("confidence"),
            signal_reasoning=original_signal.get("reasoning"),
            market_regime=current_regime,
        )
    except Exception as e:
        logger.warning(f"[Learning] Scale-out record failed {sym}: {e}")

    with get_db() as db:
        sig = db.query(TradingSignal).filter(TradingSignal.id == sig_id).first()
        if sig:
            sig.scaled_out = True
            sig.scaled_out_qty = close_qty
            sig.updated_date = now_iso

    # Move the remaining runner's stop to breakeven and keep the original target —
    # _sync_exit_orders replaces just the stop/target legs in place (no destructive
    # cancel-everything), so a resting take-profit for the reduced qty survives.
    breakeven_stop = entry
    _sync_exit_orders(alpaca_sym, remaining_qty, "long", breakeven_stop, target, is_c, current_price=current_price)

    logger.info(
        f"[Positions] ✂ Scaled out {sym}: closed {close_qty:.6g} @ ${current_price:.4f} "
        f"(TP1 ${tp1_price:.4f}), {remaining_qty:.6g} remaining with stop @ breakeven ${breakeven_stop:.4f}"
    )
    log_decision(
        "positions", "SCALE_OUT",
        f"Locked in {SCALE_OUT_FRACTION*100:.0f}% at TP1 (${tp1_price:.4f}), stop moved to breakeven",
        symbol=sym, pnl_pct=(current_price - entry) / entry * 100, price=current_price, thinking=False,
    )
    return True


def _price_changed_enough(old_price: float, new_price: float) -> bool:
    if not old_price or old_price <= 0:
        return True
    return abs(new_price - old_price) / old_price * 100 >= EXIT_ORDER_REPRICE_PCT


def _cancel_open_orders(client, sym: str):
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        sym_clean = sym.upper().replace("/", "")  # Alpaca needs no-slash symbol
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym_clean]))
        for o in open_orders:
            try:
                client.cancel_order_by_id(o.id)
                logger.debug(f"[Positions] Cancelled order {o.id} for {sym}")
            except Exception as ce:
                logger.debug(f"[Positions] Could not cancel {o.id}: {ce}")
    except Exception as e:
        logger.debug(f"[Positions] Cancel check failed for {sym}: {e}")


def _open_exit_orders(client, sym: str, exit_side: str) -> tuple[object | None, object | None]:
    """Return (stop_order, target_order) for the symbol/side when discoverable."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        sym_clean = sym.upper().replace("/", "")
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[sym_clean], nested=True,
        ))
    except Exception as e:
        logger.debug(f"[Positions] Open order lookup failed for {sym}: {e}")
        return None, None

    stop_order = None
    target_order = None
    pending = list(orders)
    expanded_orders = []
    while pending:
        order = pending.pop(0)
        expanded_orders.append(order)
        pending.extend(list(getattr(order, "legs", None) or []))

    for order in expanded_orders:
        side = str(getattr(order, "side", "")).lower()
        if exit_side.lower() not in side:
            continue
        order_type = str(getattr(order, "order_type", getattr(order, "type", ""))).lower()
        stop_price = getattr(order, "stop_price", None)
        limit_price = getattr(order, "limit_price", None)
        if stop_price or "stop" in order_type or "trail" in order_type:
            stop_order = stop_order or order
        elif limit_price:
            target_order = target_order or order
    return stop_order, target_order


def _pending_market_exit(client, sym: str, exit_side: str):
    """Return an accepted market exit so closed-market cycles do not resubmit it."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[sym.upper().replace("/", "")],
            nested=True,
        ))
    except Exception as e:
        logger.debug(f"[Positions] Pending exit lookup failed for {sym}: {e}")
        return None

    pending = list(orders)
    while pending:
        order = pending.pop(0)
        pending.extend(list(getattr(order, "legs", None) or []))
        side = str(getattr(order, "side", "")).lower().split(".")[-1]
        order_type = str(
            getattr(order, "order_type", getattr(order, "type", ""))
        ).lower().split(".")[-1]
        if side == exit_side.lower() and order_type == "market":
            return order
    return None


def _order_is_filled(order) -> bool:
    if order is None:
        return True
    status = str(getattr(order, "status", "")).lower().split(".")[-1]
    return status == "filled"


def _wait_for_close_fill(order, timeout_seconds: float = 3.0) -> tuple[bool, object]:
    """Briefly confirm normal market-hours fills; return pending after the timeout."""
    if _order_is_filled(order):
        return True, order
    order_id = getattr(order, "id", None)
    if not order_id:
        return False, order
    client = get_trading_client()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest = order
    while time.monotonic() < deadline:
        time.sleep(0.25)
        try:
            latest = client.get_order_by_id(order_id)
        except Exception as e:
            logger.debug(f"[Positions] Close fill check failed for {order_id}: {e}")
            break
        if _order_is_filled(latest):
            return True, latest
    return False, latest


def _safe_qty(qty: float, is_crypto: bool) -> float:
    qty = abs(float(qty or 0))
    if is_crypto:
        return math.floor(qty * 0.999 * 100_000_000) / 100_000_000
    return max(0, int(math.floor(qty + 1e-9)))


def _submit_stop(client, sym: str, qty: float, exit_side, stop_price: float,
                 is_crypto: bool):
    """Submit a protective stop in the ONLY form the venue accepts.

    Alpaca rejects plain StopOrderRequest for crypto with 40010001 "invalid
    order type for crypto order" — crypto supports market/limit/stop_limit
    only. Sending the wrong type left positions unprotected while the log
    merely said "Stop sync failed". The limit leg sits 1% through the stop
    so a normal stop-out still fills."""
    from alpaca.trading.requests import StopLimitOrderRequest
    if is_crypto:
        return client.submit_order(StopLimitOrderRequest(
            symbol=sym, qty=qty, side=exit_side, time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 8),
            limit_price=round(stop_price * 0.99, 8),
        ))
    return client.submit_order(StopOrderRequest(
        symbol=sym, qty=qty, side=exit_side,
        time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 8),
    ))


def _replace_or_submit_stop(client, sym: str, qty: float, exit_side, stop_price: float, existing=None, is_crypto=False, current_price: float = None) -> bool:
    # NOTE: "tighter" here is assumed to mean a higher stop_price, which only holds for
    # long/protective-sell exits. Live execution is long-only today (execute_signals.py
    # always submits side='buy'); revisit this ratchet direction before enabling short exits.
    try:
        from alpaca.trading.requests import ReplaceOrderRequest, StopOrderRequest
        from alpaca.trading.enums import TimeInForce
        qty_safe = _safe_qty(qty, is_crypto)
        if qty_safe <= 0:
            logger.warning(f"[Positions] Stop skipped {sym}: qty too small")
            return False
        if existing:
            existing_type = str(
                getattr(existing, "order_type", getattr(existing, "type", "")) or ""
            ).lower()
            if "trail" in existing_type:
                # A trailing stop can't be repriced directly (Alpaca tracks it by
                # trail_percent, not an absolute stop_price). Only replace it when we
                # can prove the desired stop is strictly tighter than what the trailing
                # order is currently protecting — otherwise keep it rather than risk
                # loosening protection with an unverified swap.
                trail_pct = getattr(existing, "trail_percent", None)
                existing_effective_stop = None
                if trail_pct is not None and current_price:
                    try:
                        existing_effective_stop = float(current_price) * (1.0 - float(trail_pct) / 100.0)
                    except Exception:
                        existing_effective_stop = None
                if existing_effective_stop is not None and stop_price > existing_effective_stop * (1 + EXIT_ORDER_REPRICE_PCT / 100.0):
                    try:
                        client.cancel_order_by_id(existing.id)
                    except Exception as ce:
                        logger.warning(f"[Positions] Could not cancel trailing stop {sym} before tightening: {ce}")
                        return True
                    _submit_stop(client, sym, qty_safe, exit_side, stop_price, is_crypto)
                    logger.info(f"[Positions] Converted trailing stop -> tighter fixed stop {sym} -> ${stop_price:.6g}")
                    return True
                logger.info(f"[Positions] Retained existing trailing stop for {sym}")
                return True
            current = float(getattr(existing, "stop_price", 0) or 0)
            if not _price_changed_enough(current, stop_price):
                return True
            if current and stop_price < current:
                logger.info(f"[Positions] Ignored stop update for {sym}: ${stop_price:.6g} would loosen existing ${current:.6g}")
                return True
            client.replace_order_by_id(existing.id, ReplaceOrderRequest(qty=qty_safe, stop_price=round(stop_price, 8)))
            logger.info(f"[Positions] Replaced stop {sym} -> ${stop_price:.6g}")
        else:
            _submit_stop(client, sym, qty_safe, exit_side, stop_price, is_crypto)
            logger.info(f"[Positions] Submitted stop {sym} -> ${stop_price:.6g}")
        return True
    except Exception as e:
        if existing and "insufficient qty available" in str(e).lower():
            logger.info(f"[Positions] Retained existing protective order for {sym}: quantity already held")
            return True
        logger.warning(f"[Positions] Stop sync failed {sym}: {e}")
        return False


def _replace_or_submit_target(client, sym: str, qty: float, exit_side, target_price: float, existing=None, is_crypto=False, force: bool = False) -> bool:
    try:
        from alpaca.trading.requests import ReplaceOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import TimeInForce
        qty_safe = _safe_qty(qty, is_crypto)
        if qty_safe <= 0:
            logger.warning(f"[Positions] Target skipped {sym}: qty too small")
            return False
        if existing:
            current = float(getattr(existing, "limit_price", 0) or 0)
            if not _price_changed_enough(current, target_price):
                return True
            client.replace_order_by_id(existing.id, ReplaceOrderRequest(qty=qty_safe, limit_price=round(target_price, 8)))
            logger.info(f"[Positions] Replaced target {sym} -> ${target_price:.6g}")
        else:
            if not (force or ALLOW_STANDALONE_TAKE_PROFIT):
                logger.debug(f"[Positions] Target creation skipped {sym}: no existing target order")
                return True
            client.submit_order(LimitOrderRequest(
                symbol=sym, qty=qty_safe, side=exit_side,
                time_in_force=TimeInForce.GTC, limit_price=round(target_price, 8)
            ))
            logger.info(f"[Positions] Submitted target {sym} -> ${target_price:.6g}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Target sync failed {sym}: {e}")
        return False


def _sync_exit_orders(sym: str, qty: float, side: str, stop_price: float, target_price: float, is_crypto: bool, current_price: float = None) -> bool:
    """Synchronize live protective stop and take-profit orders where Alpaca allows it."""
    try:
        from alpaca.trading.enums import OrderSide
        client = get_trading_client()
        exit_side = OrderSide.SELL if side != "short" else OrderSide.BUY
        exit_side_text = "sell" if side != "short" else "buy"
        stop_order, target_order = _open_exit_orders(client, sym, exit_side_text)
        stop_ok = _replace_or_submit_stop(client, sym, qty, exit_side, stop_price, existing=stop_order, is_crypto=is_crypto, current_price=current_price)
        target_ok = _replace_or_submit_target(client, sym, qty, exit_side, target_price, existing=target_order, is_crypto=is_crypto)
        return bool(stop_ok)
    except Exception as e:
        logger.warning(f"[Positions] Exit order sync failed {sym}: {e}")
        return False


def _update_signal_exit_levels(sym: str, stop_price: float, target_price: float,
                               now_iso: str, is_crypto: bool = False) -> bool:
    updated = False
    with get_db() as db:
        sig = db.query(TradingSignal).filter(
            TradingSignal.asset_symbol.in_(_sym_variants(sym, is_crypto)),
            TradingSignal.status.in_(["Executed", "Active"]),
            TradingSignal.paper_mode.is_not(True),
            TradingSignal.direction.in_(["Long", "Bounce"]),
        ).order_by(TradingSignal.generated_at.desc()).first()
        if sig:
            if _price_changed_enough(float(sig.stop_loss or 0), stop_price):
                sig.stop_loss = stop_price
                updated = True
            if _price_changed_enough(float(sig.target_price or 0), target_price):
                sig.target_price = target_price
                updated = True
            if updated:
                sig.updated_date = now_iso
    return updated


def _set_trailing_stop_equity(client, sym: str, qty: float, trail_pct: float) -> bool:
    try:
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        qty_safe = _safe_qty(qty, is_crypto=False)
        if qty_safe <= 0:
            logger.warning(f"[Positions] Equity trailing stop skipped {sym}: fractional qty below 1 share")
            return False
        # Alpaca requires trail_percent >= 0.1 — clamp any LLM-supplied value to a sane
        # range so a hallucinated large value can't install a near-worthless stop.
        trail_pct_safe = min(15.0, max(0.1, float(trail_pct)))
        req = TrailingStopOrderRequest(
            symbol=sym, qty=qty_safe,
            side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            trail_percent=trail_pct_safe,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] ✓ Equity trailing stop — {sym} trail={trail_pct}% | {order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Equity trailing stop failed {sym}: {e}")
        return False


def _set_crypto_limit_stop(client, sym: str, qty: float, current_price: float, trail_pct: float) -> bool:
    """Protective stop for a crypto position.

    MUST be a STOP-LIMIT: Alpaca crypto supports only market/limit/stop_limit
    order types — the plain StopOrderRequest this function originally
    submitted was rejected with 40010001 "invalid order type for crypto
    order" on EVERY attempt, which silently left every crypto position
    running without a protective stop (found via the production error log).
    The limit leg sits 1% below the stop so a normal stop-out fills, at the
    cost of the standard stop-limit caveat: a violent gap through the limit
    can leave the order unfilled. That is the exchange's constraint, not a
    choice — crypto-on-Alpaca has no plain stop order to fall back to."""
    try:
        from alpaca.trading.requests import StopLimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        trail_pct = min(15.0, max(0.1, float(trail_pct)))
        stop_price = round(current_price * (1.0 - trail_pct / 100.0), 6)
        limit_price = round(stop_price * 0.99, 6)  # 1% through the stop
        # Truncate qty using floor — never round up, Alpaca rejects if qty > available balance
        # (their API returns e.g. 127.679999999 due to float precision, so 127.68 gets rejected)
        qty_safe = math.floor(qty * 1_000_000) / 1_000_000
        if qty_safe <= 0:
            logger.warning(f"[Positions] Crypto stop-limit skipped {sym} — dust position (qty={qty})")
            return False
        req = StopLimitOrderRequest(
            symbol=sym, qty=qty_safe,
            side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
            stop_price=stop_price, limit_price=limit_price,
        )
        order = client.submit_order(req)
        logger.info(f"[Positions] ✓ Crypto stop-limit — {sym} stop=${stop_price:.4f} limit=${limit_price:.4f} ({trail_pct}% below ${current_price:.4f}) | {order.id}")
        return True
    except Exception as e:
        logger.warning(f"[Positions] Crypto stop-limit failed {sym}: {e}")
        return False


def _set_protective_order(sym: str, qty: float, trail_pct: float,
                          current_price: float, is_crypto: bool = False) -> bool:
    try:
        client = get_trading_client()
        _cancel_open_orders(client, sym)
        if is_crypto:
            return _set_crypto_limit_stop(client, sym, qty, current_price, trail_pct)
        else:
            return _set_trailing_stop_equity(client, sym, qty, trail_pct)
    except Exception as e:
        logger.warning(f"[Positions] Protective order failed {sym}: {e}")
        return False


def _fetch_ta(sym: str, is_crypto: bool = False) -> dict:
    """Fetch multi-timeframe TA for a position symbol."""
    try:
        # Normalize to cache format (slash for crypto)
        cache_sym = sym
        if is_crypto and "/" not in sym:
            base = sym[:-3] if sym.upper().endswith("USD") else sym
            cache_sym = f"{base}/USD"

        timeframes = ["1H", "4H", "1D"]
        bars_by_tf = {}
        for tf in timeframes:
            try:
                df = fetch_with_cache(
                    cache_sym, tf,
                    asset_class="crypto" if is_crypto else "equity",
                )
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
        f"[{t.severity}] {t.title}: {(t.description or '')[:200]}"
        for t in threats
    ) or "No active threats"

    news_ctx = "\n".join(
        f"[{n.sentiment or 'neutral'}] {n.title}: {(n.summary or '')[:200]}"
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

    prompt = f"""Answer directly. Return only the JSON object specified below.

You are an active position manager for a trading AI. Evaluate this open position and decide what to do RIGHT NOW.

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
        raw = call_lm_studio(prompt, system="You are a precise trading risk manager. Answer directly without reasoning. Return ONLY the JSON object, no markdown, no explanation, no thinking.", max_tokens=1024, thinking=False)
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
    risk_adjusted = 0

    # Fetch current regime for learning engine
    current_regime = "Unknown"
    try:
        from lib.market_regime import get_regime as _get_regime
        current_regime = _get_regime().get("label", "Unknown")
    except Exception as _re:
        logger.warning(f"[Positions] Regime fetch failed: {_re}")
    total_mv = sum(float(p.market_value or 0) for p in positions)
    total_pl = sum(float(p.unrealized_pl or 0) for p in positions)

    logger.info(f"[Positions] {len(positions)} positions | MV=${total_mv:,.0f} | P&L=${total_pl:+,.0f} | equity=${equity:,.0f}")

    # Load shared context once (threats + news)
    with get_db() as db:
        threat_ctx, news_ctx = _get_context(db)

        # Load executed signals for original thesis lookup
        exec_sigs = db.query(TradingSignal).filter(
            TradingSignal.status.in_(["Executed", "Active"]),
            TradingSignal.paper_mode.is_not(True),
            TradingSignal.direction.in_(["Long", "Bounce"]),
        ).order_by(TradingSignal.generated_at.asc()).all()
        sig_map = {s.asset_symbol.upper().replace("/", ""): {
            "id":           s.id,
            "entry_price":  s.entry_price,
            "target_price": s.target_price,
            "stop_loss":    s.stop_loss,
            "confidence":   s.confidence,
            "timeframe":    s.timeframe or "1D",
            "reasoning":    s.reasoning or "",
            "signal_source": s.signal_source or "watchlist",
            "actual_fill_price": s.actual_fill_price,
            "scaled_out":   bool(s.scaled_out),
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
                asset_class="Crypto" if _position_is_crypto(pos) else "Equity",
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
        is_c          = _position_is_crypto(pos)
        alpaca_sym    = _alpaca_sym(sym)

        # ── DUST GUARD: skip positions with negligible market value ────────────
        # These are residual fractional positions from previous closes.
        # They can't be meaningfully managed and waste LLM tokens + API calls.
        DUST_THRESHOLD_USD = 1.0
        if abs(mv) < DUST_THRESHOLD_USD:
            logger.warning(f"[Positions] Skipping {sym} — dust position (MV=${mv:.6f}, qty={qty:.8g})")
            continue

        logger.info(f"[Positions] {sym:12} {plpc:+6.1f}% | MV=${mv:>10,.0f} | P&L=${pl:>+8,.0f} | qty={qty}")

        exit_side = "sell" if qty > 0 else "buy"
        pending_exit = _pending_market_exit(get_trading_client(), alpaca_sym, exit_side)
        if pending_exit is not None:
            logger.info(
                f"[Positions] Close already pending for {sym} "
                f"(order={getattr(pending_exit, 'id', 'unknown')})"
            )
            continue

        # ── SAFETY NET: adopt any position that has no protective order ────
        # Entry-time stop placement can fail (a market order still unfilled
        # when the bracket legs are submitted leaves the position naked).
        # Rather than trust that path, every sweep verifies protection and
        # attaches a stop when none exists — a naked position is the single
        # most expensive failure mode in this system.
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            client_o = get_trading_client()
            # Alpaca stores crypto POSITIONS as "ETHUSD" and crypto ORDERS as
            # "ETH/USD", so a symbol-filtered query matched nothing: the sweep
            # concluded "NO protective order", tried to add one, and failed
            # with insufficient balance — because the real stop was already
            # holding the coins. Match unfiltered on the normalized symbol.
            want_sym = str(alpaca_sym).upper().replace("/", "")
            open_o = [
                o for o in (client_o.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) or [])
                if str(o.symbol).upper().replace("/", "") == want_sym
            ]
            protective = [
                o for o in open_o
                if str(getattr(o, "side", "")).lower().endswith("sell" if qty > 0 else "buy")
            ]
            if not protective and current_price > 0:
                stop_pct = 3.0 if is_c else 2.0
                if is_c:
                    placed = _set_crypto_limit_stop(client_o, alpaca_sym, abs(qty), current_price, stop_pct)
                else:
                    # Equities use Alpaca's native trailing stop (crypto has none).
                    placed = _set_trailing_stop_equity(client_o, alpaca_sym, abs(qty), stop_pct)
                logger.warning(
                    f"[Positions] {sym} had NO protective order — "
                    f"{'attached' if placed else 'FAILED to attach'} a {stop_pct}% stop"
                )
        except Exception as e:
            logger.debug(f"[Positions] Protection check skipped for {sym}: {e}")

        tier = _tier(plpc, is_crypto=is_c)
        ta_data = _fetch_ta(sym, is_crypto=is_c)
        original_signal = sig_map.get(sym.upper().replace("/", ""), {})
        if original_signal.get("id") and original_signal.get("actual_fill_price") is None:
            _record_slippage(original_signal["id"], float(original_signal.get("entry_price") or 0), avg, now_iso)
        risk_plan = _dynamic_exit_plan(
            pos, current_price, ta_data, original_signal, is_crypto=is_c,
        )
        if risk_plan.get("ok") and risk_plan.get("action") == "EXIT":
            try:
                n_cancelled = cancel_open_orders_for_symbol(alpaca_sym)
                if n_cancelled:
                    logger.info(f"[Positions] Cancelled {n_cancelled} open order(s) for {sym} before risk close")
                close_order = close_position(alpaca_sym)
                filled, close_order = _wait_for_close_fill(close_order)
                if not filled:
                    logger.warning(
                        f"[Positions] RISK EXIT submitted for {sym}; awaiting broker fill: "
                        f"{risk_plan.get('reason')}"
                    )
                    continue
                logger.warning(f"[Positions] RISK EXIT filled for {sym}: {risk_plan.get('reason')}")
                log_decision("positions", "EXIT", risk_plan.get("reason", "Max dollar loss breached"), symbol=sym, pnl_pct=plpc, price=current_price, thinking=False)
                with get_db() as db:
                    sig = db.query(TradingSignal).filter(
                        TradingSignal.asset_symbol.in_(_sym_variants(sym, is_c)),
                        TradingSignal.status == "Executed"
                    ).first()
                    if sig:
                        sig.status = "Closed"
                        sig.updated_date = now_iso
                closed += 1
            except Exception as e:
                logger.error(f"[Positions] Risk close {sym} failed: {e}")
            continue

        if (risk_plan.get("ok") and risk_plan.get("action") == "ADJUST"
                and not (tier and tier["action"] == "close")):
            stop_price = float(risk_plan["stop_price"])
            target_price = float(risk_plan["target_price"])
            _update_signal_exit_levels(
                sym, stop_price, target_price, now_iso, is_crypto=is_c,
            )
            ok = _sync_exit_orders(
                alpaca_sym, qty, risk_plan.get("side", "long"),
                stop_price, target_price, is_c, current_price=current_price
            )
            if ok:
                risk_adjusted += 1
                logger.info(
                    f"[Positions] Risk guard {sym}: stop=${stop_price:.6g} target=${target_price:.6g} "
                    f"max_loss=${risk_plan['risk_dollars']:.2f} strength={risk_plan['strength']}"
                )

        # ── STEP 1: Hard deterministic rules — no LLM, instant ────────────────
        if tier is not None:
            label  = tier["label"]
            action = tier["action"]
            trail_pct = (3.0 if action == "trail_tight" else 5.0) if is_c else \
                        (5.0 if action == "trail_tight" else 8.0)

            if action == "close":
                try:
                    # Cancel any open bracket legs (stop/target) before closing
                    n_cancelled = cancel_open_orders_for_symbol(alpaca_sym)
                    if n_cancelled:
                        logger.info(f"[Positions] Cancelled {n_cancelled} open order(s) for {sym} before close")
                    close_order = close_position(alpaca_sym)
                    filled, close_order = _wait_for_close_fill(close_order)
                    if not filled:
                        logger.info(
                            f"[Positions] [RULE] Close submitted for {sym}; awaiting broker fill | {label}"
                        )
                        continue
                    try:
                        _sig_data = sig_map.get(sym.upper().replace("/", ""), {})
                        record_trade_outcome(
                            symbol=sym, asset_class=pos.asset_class or "equity",
                            direction=pos.side or "long",
                            entry_price=float(pos.avg_entry_price or 0),
                            exit_price=current_price,
                            qty=float(pos.qty or 0),
                            exit_reason="HARD_STOP" if plpc < 0 else "TAKE_PROFIT",
                            signal_id=_sig_data.get("id"),
                            timeframe=_sig_data.get("timeframe", "1D"),
                            signal_confidence=_sig_data.get("confidence"),
                            signal_reasoning=_sig_data.get("reasoning"),
                            ta_profile=_fetch_ta(sym, is_crypto=is_c),
                            market_regime=current_regime,
                        )
                    except Exception as _le: logger.warning(f"[Learning] record failed: {_le}")
                    logger.info(f"[Positions] ✓ [RULE] Closed {sym} @ {plpc:+.1f}% | {label}")
                    log_decision("positions", "EXIT", f"Hard rule: {label}", symbol=sym, pnl_pct=plpc, price=current_price, thinking=False)
                    with get_db() as db:
                        sig = db.query(TradingSignal).filter(
                            TradingSignal.asset_symbol.in_(_sym_variants(sym, is_c)),
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
                log_decision("positions", "TIGHTEN_STOP", f"Hard rule trail: {label} @ {trail_pct}%", symbol=sym, pnl_pct=plpc, price=current_price, thinking=False)
                ok = _set_protective_order(
                    alpaca_sym, qty, trail_pct, current_price, is_crypto=is_c,
                )
                if ok:
                    trailing += 1
                # Still fall through to LLM for deeper analysis even after setting stop
                # (LLM might escalate to EXIT if thesis is broken)

        if _maybe_scale_out(sym, alpaca_sym, qty, avg, current_price, is_c, original_signal, current_regime, now_iso):
            continue

        # ── STEP 2: LLM evaluation — TA + news + threats ──────────────────────
        logger.info(f"[Positions] 🤖 LLM evaluating {sym}...")
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
        log_decision("positions", action, reason, symbol=sym, pnl_pct=plpc, price=current_price, thinking=True)

        if action == "EXIT":
            # Don't double-close if hard rule already fired
            if tier and tier["action"] == "close":
                logger.info(f"[Positions] {sym} already closed by hard rule — skipping LLM EXIT")
                continue
            try:
                # Cancel any open bracket legs (stop/target) before closing
                n_cancelled = cancel_open_orders_for_symbol(alpaca_sym)
                if n_cancelled:
                    logger.info(f"[Positions] Cancelled {n_cancelled} open order(s) for {sym} before LLM close")
                    # A cancelled protective order still RESERVES the asset for a
                    # moment at the broker; closing immediately failed with
                    # "insufficient balance for ARB (available: 0.000000175)".
                    # Give the cancel a beat to settle before selling.
                    import time as _t
                    _t.sleep(1.5)
                close_order = close_position(alpaca_sym)
                filled, close_order = _wait_for_close_fill(close_order)
                if not filled:
                    logger.info(
                        f"[Positions] [LLM] Close submitted for {sym}; awaiting broker fill | {reason}"
                    )
                    continue
                try:
                    _sig_data2 = sig_map.get(sym.upper().replace("/", ""), {})
                    record_trade_outcome(
                        symbol=sym, asset_class=pos.asset_class or "equity",
                        direction=pos.side or "long",
                        entry_price=float(pos.avg_entry_price or 0),
                        exit_price=current_price,
                        qty=float(pos.qty or 0),
                        exit_reason="LLM_EXIT",
                        signal_id=_sig_data2.get("id"),
                        timeframe=_sig_data2.get("timeframe", "1D"),
                        signal_confidence=_sig_data2.get("confidence"),
                        signal_reasoning=_sig_data2.get("reasoning"),
                        ta_profile=ta_data,
                        market_regime=current_regime,
                    )
                except Exception as _le: logger.warning(f"[Learning] record failed: {_le}")
                logger.info(f"[Positions] ✓ [LLM] Closed {sym} @ {plpc:+.1f}% | {reason}")
                with get_db() as db:
                    sig = db.query(TradingSignal).filter(
                        TradingSignal.asset_symbol.in_(_sym_variants(sym, is_c)),
                        TradingSignal.status == "Executed"
                    ).first()
                    if sig:
                        sig.status = "Closed"
                        sig.updated_date = now_iso
                closed += 1
            except Exception as e:
                logger.error(f"[Positions] LLM EXIT close {sym} failed: {e}")

        elif action == "TIGHTEN_STOP" and new_stop_pct:
            try:
                from alpaca.trading.enums import OrderSide
                # Clamp to the same sane trail range used elsewhere (portfolio_guardian's
                # TIGHTEN_ALL) so a hallucinated LLM value can't install a near-worthless stop.
                new_stop_pct_clamped = min(15.0, max(0.5, float(new_stop_pct)))
                stop_price = current_price * (1.0 - new_stop_pct_clamped / 100.0)

                client = get_trading_client()
                exit_side_text = "sell" if qty > 0 else "buy"
                existing_stop, existing_target = _open_exit_orders(client, alpaca_sym, exit_side_text)

                existing_stop_price = None
                if existing_stop is not None:
                    fixed_stop = float(getattr(existing_stop, "stop_price", 0) or 0)
                    trail_pct = getattr(existing_stop, "trail_percent", None)
                    if fixed_stop:
                        existing_stop_price = fixed_stop
                    elif trail_pct is not None:
                        existing_stop_price = current_price * (1.0 - float(trail_pct) / 100.0)

                # Ratchet-only: never let the LLM install a looser stop than what's live.
                if existing_stop_price and stop_price <= existing_stop_price:
                    logger.info(
                        f"[Positions] LLM TIGHTEN_STOP {sym} ignored — requested ${stop_price:.4f} "
                        f"would not tighten existing protective stop ${existing_stop_price:.4f}"
                    )
                else:
                    target_price_to_restore = 0.0
                    if existing_target is not None:
                        target_price_to_restore = float(getattr(existing_target, "limit_price", 0) or 0)
                    if not target_price_to_restore:
                        target_price_to_restore = float(original_signal.get("target_price") or 0)

                    _cancel_open_orders(client, alpaca_sym)
                    if is_c:
                        stop_ok = _set_crypto_limit_stop(client, alpaca_sym, qty, current_price, new_stop_pct_clamped)
                    else:
                        stop_ok = _set_trailing_stop_equity(client, alpaca_sym, qty, new_stop_pct_clamped)

                    if stop_ok and target_price_to_restore > 0:
                        exit_side_enum = OrderSide.SELL if qty > 0 else OrderSide.BUY
                        _replace_or_submit_target(
                            client, alpaca_sym, qty, exit_side_enum, target_price_to_restore,
                            existing=None, is_crypto=is_c, force=True,
                        )
                    logger.info(
                        f"[Positions] ⟳ [LLM] Tighten stop {sym} → ${stop_price:.4f} "
                        f"({new_stop_pct_clamped}% below current) | {reason}"
                    )
                    trailing += 1
            except Exception as e:
                logger.error(f"[Positions] LLM TIGHTEN_STOP {sym} failed: {e}")

        else:
            logger.info(f"[Positions] ✓ {sym} holding — {reason}")

    logger.info(f"[Positions] Done — {closed} closed, {trailing} protective orders, {risk_adjusted} risk-adjusted | equity=${equity:,.2f}")
    return {"closed": closed, "trailing": trailing, "risk_adjusted": risk_adjusted, "total": len(positions), "equity": equity}



