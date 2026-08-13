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
import logging, json, re, os
from datetime import datetime, timezone, timedelta
from app.database import (
    DEFAULT_USER_ID, get_db, TradingSignal, MarketAsset, PaperPosition,
    NewsItem, ThreatEvent, UserPreference,
)

logger = logging.getLogger(__name__)

PAPER_MIN_CONFIDENCE = 55   # fallback when no criteria are configured


# ── Bootstrap ───────────────────────────────────────────────────────────
# Calibration needs OBSERVED outcomes, and observed outcomes need trades.
# With the pre-fix history quarantined, honest confidence sat near the
# no-evidence ceiling and almost nothing cleared the gate — no data, so no
# trades, so no data.
#
# The paper book exists to break exactly that circle: it risks nothing real,
# and every position it opens becomes evidence the live gate can later rely
# on. So while the current epoch is still thin, PAPER trades at a lower bar
# than the broker does. The live gate is untouched.
BOOTSTRAP_MIN_OUTCOMES = 300      # observed, current-epoch, non-replay
BOOTSTRAP_MIN_SCORE = 45.0


def _observed_outcome_count() -> int:
    """Live outcomes in the current epoch. Replayed ones are excluded — a
    simulation cannot certify that the system is ready to stop bootstrapping."""
    try:
        from app.database import get_db, TradeOutcome
        from lib.calibration import CURRENT_EPOCH
        with get_db() as db:
            return db.query(TradeOutcome).filter(
                TradeOutcome.engine_epoch == CURRENT_EPOCH,
                TradeOutcome.outcome_source != "replay",
            ).count()
    except Exception:
        return 0


def _criteria() -> dict:
    """Operator-configured execution criteria (Ops -> Execution Criteria).

    The paper book used a hardcoded 55 while the live book read these
    settings, so raising the bar in the UI silently did nothing to the
    book doing most of the trading. Both now read the same knobs."""
    try:
        from lib.trading_preferences import get_user_preference
        pref = get_user_preference()
        configured = float(pref.get("live_min_score") or PAPER_MIN_CONFIDENCE)
        observed = _observed_outcome_count()
        if observed < BOOTSTRAP_MIN_OUTCOMES:
            score = min(configured, BOOTSTRAP_MIN_SCORE)
            if score < configured:
                logger.info(
                    f"[Paper] Bootstrap: {observed}/{BOOTSTRAP_MIN_OUTCOMES} observed "
                    f"outcomes this epoch — paper gate {score:.0f} (live stays "
                    f"{configured:.0f}) so the book can earn the evidence calibration needs"
                )
        else:
            score = configured
        return {
            "min_score": score,
            "min_rr": float(pref.get("live_min_rr") or 0),
            "min_confidence": float(pref.get("live_min_confidence") or 0),
            "bootstrapping": observed < BOOTSTRAP_MIN_OUTCOMES,
            "observed_outcomes": observed,
        }
    except Exception:
        return {"min_score": PAPER_MIN_CONFIDENCE, "min_rr": 0.0, "min_confidence": 0.0}
SCALE_OUT_ENABLED = os.getenv("SCALE_OUT_ENABLED", "true").lower() in ("1", "true", "yes")
SCALE_OUT_FRACTION = 0.5
SCALE_OUT_TP1_PCT_OF_TARGET = 0.5


def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


# ── Risk: a backstop, not a working stop ────────────────────────────────────
# MAX_LOSS_PER_TRADE_USD was a FIXED $15 applied to positions carrying
# $7,000-$12,000 of notional, i.e. a trigger at a 0.12-0.21% price move —
# well inside ordinary crypto noise (ATR 2-5%). Measured over 218 closed
# trades it was not a risk limit but a churn engine:
#
#   exit path              n   win%      net P&L     avg
#   dollar cap ($15)     103     4%    -5,687.58  -55.22
#   signal's own stop     67    27%    -3,602.87  -53.77
#   AI EXIT (deep verify) 28    21%      -117.62   -4.20
#   target / scale-out    17   100%    +4,097.30    +241
#
# Two lessons drove this rewrite. The dollar cap and the ATR stop bled at
# the SAME rate, so widening one and keeping the other changes nothing —
# the mechanical stops were the problem, not their calibration. And every
# trade that reached its target won; only 17 of 218 ever got there because
# the stops killed the rest first.
#
# So the dollar figure becomes a CATASTROPHIC BACKSTOP scaled to the margin
# actually committed, the deep-verify exit becomes the primary decision, and
# the signal's own levels do the day-to-day work.
CATASTROPHIC_LOSS_PCT_OF_MARGIN = _env_float("CATASTROPHIC_LOSS_PCT_OF_MARGIN", 35.0, 10.0, 90.0)

# Retained for the sizing helpers that still reason in dollars, but no longer
# used as an exit trigger.
MAX_LOSS_PER_TRADE_USD = _env_float("MAX_LOSS_PER_TRADE_USD", 15.0, 10.0, 20.0)


PROFIT_LOCK_USD = _env_float("PROFIT_LOCK_USD", 10.0, 0.0, 50.0)
MIN_DYNAMIC_TRAIL_PCT = _env_float("MIN_DYNAMIC_TRAIL_PCT", 0.75, 0.1, 5.0)
TARGET_REWARD_MULTIPLIER = _env_float("TARGET_REWARD_MULTIPLIER", 2.8, 1.2, 6.0)
EXIT_ORDER_REPRICE_PCT = _env_float("EXIT_ORDER_REPRICE_PCT", 0.25, 0.05, 2.0)
FUTURES_MIN_CONFIDENCE = 45  # lower bar for futures — macro-driven, even 47% conviction is tradeable


# ── Trade horizon ───────────────────────────────────────────────────────────
# A 3% adverse move kills a 5-minute scalp and is ordinary noise on a weekly
# position. Managing both with one set of numbers means one of them is always
# being managed wrong, and this book holds both at once.
#
# The table lives in lib/trade_horizon.py — it already existed in three
# copies (signals API, Telegram formatter, signal card) as a bare string map
# that the management loop could not use. Same numbers, now readable here.
from lib.trade_horizon import (expected_hold_minutes, hold_estimate,
                               hold_status, room_multiplier as horizon_room_multiplier,
                               format_duration as _fmt_duration)

# How much wider than its expected hold a position may run before the fact
# that it has NOT resolved is itself evidence the setup failed.
STALE_HOLD_MULTIPLE = _env_float("STALE_HOLD_MULTIPLE", 3.0, 1.5, 10.0)


def hold_window_label(timeframe: str | None) -> str:
    return hold_estimate(timeframe)


def catastrophic_loss_usd(margin: float) -> float:
    """The most a position may lose before it is closed regardless of what
    any model thinks. Scaled to the capital committed, because a fixed
    dollar figure cannot serve a $500 position and a $12,000 one at once."""
    return abs(float(margin or 0)) * (CATASTROPHIC_LOSS_PCT_OF_MARGIN / 100.0)


def catastrophic_stop_price(entry: float, qty: float, margin: float,
                            is_short: bool) -> float | None:
    """The PRICE at which that loss is reached.

    This is the half that was missing. The cap was only ever a comparison
    made when the 15-minute job happened to look, so a "$15 limit" exited at
    -$55 on average and -$379 at worst — 25x its own stated bound. Expressed
    as a price and written onto the position at open, it is enforced by the
    same stop machinery as every other level, at the level rather than
    whenever the poll next runs."""
    q = abs(float(qty or 0))
    if q <= 0 or entry <= 0:
        return None
    move = catastrophic_loss_usd(margin) / q
    return entry + move if is_short else entry - move

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


def _tier(plpc: float, is_crypto: bool, timeframe: str | None = None):
    """The deterministic tier for this P&L, widened for longer horizons.

    The loss thresholds (-4% crypto, -5% equity) were written for one
    horizon and applied to all of them, so a 1D setup was cut for exactly
    the pullback a 1D setup is supposed to absorb. Only the LOSS side is
    scaled — a 10% gain is worth taking on any timeframe, but a 4% drawdown
    means something entirely different on a 5m chart than on a weekly.
    """
    tiers = TIERS_CRYPTO if is_crypto else TIERS_EQUITY
    room = horizon_room_multiplier(timeframe) if timeframe else 1.0
    for t in tiers:
        mg, xg = t["min_gain"], t["max_gain"]
        if xg is not None and xg < 0:
            xg = xg * room          # the loss cut, given room for its horizon
        if mg is not None and xg is not None:
            if mg <= plpc < xg: return t
        elif mg is not None and xg is None:
            if plpc >= mg: return t
        elif mg is None and xg is not None:
            if plpc <= xg: return dict(t, scaled_threshold=xg)
    return None


def _get_all_prices() -> dict:
    # Layer 1: Alpaca / MarketAsset DB (equities + crypto)
    with get_db() as db:
        assets = db.query(MarketAsset).all()
        prices = {}
        # Pass 1: canonical symbols only (each MarketAsset row's own exact
        # symbol). These are authoritative and must never be overwritten by
        # a *different* asset's convenience alias below.
        for a in assets:
            if a.price and float(a.price) > 0:
                prices[a.symbol] = float(a.price)
        # Pass 2: convenience aliases (slash-stripped crypto pair, bare "SYM"
        # -> "SYM/USD") so mark_to_market's lookup can find a price even when
        # a position's stored symbol format doesn't exactly match a DB row.
        # Only fill in a key that's still empty — an alias must never
        # overwrite another asset's canonical entry. Without this guard, a
        # crypto pair's bare-symbol alias (e.g. "BEAT/USD" -> "BEAT") can
        # silently collide with an unrelated equity ticker sharing the same
        # bare symbol (NASDAQ: BEAT), handing a completely different
        # instrument's price to a mark-to-market lookup. Confirmed root
        # cause of a $148M phantom paper gain on 2026-08-10: a BEAT/USD
        # crypto position (entry $0.000039) was marked-to-market and closed
        # against BEAT the equity's price ($2.87) once the crypto quote
        # temporarily went missing and the lookup fell through to this
        # alias. See lib/paper_engine.py's _price_move_is_plausible() for
        # the defense-in-depth guard against this class of bug recurring.
        for a in assets:
            if not (a.price and float(a.price) > 0):
                continue
            price = float(a.price)
            if "/" in a.symbol:
                alias = a.symbol.replace("/", "")
                if alias not in prices:
                    prices[alias] = price
            elif a.symbol.endswith("USD") and len(a.symbol) > 3:
                alias = a.symbol[:-3] + "/USD"
                if alias not in prices:
                    prices[alias] = price

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
    """Return all open paper positions as plain dicts.

    The signal's TIMEFRAME rides along, because it is what says whether a
    position is a 25-minute scalp or a three-week hold — and the same adverse
    move means opposite things in those two cases. It lives on the signal,
    not the position, so it has to be joined here or the management loop
    manages every trade as though it had the same horizon."""
    with get_db() as db:
        rows = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        sig_ids = [p.signal_id for p in rows if p.signal_id]
        timeframes: dict[str, str] = {}
        if sig_ids:
            for sid, tf in db.query(TradingSignal.id, TradingSignal.timeframe).filter(
                TradingSignal.id.in_(sig_ids)
            ).all():
                if tf:
                    timeframes[sid] = tf
        out = []
        for p in rows:
            tf = timeframes.get(p.signal_id)
            out.append({
                "id":           str(p.id),
                "symbol":       p.symbol,
                "direction":    p.direction,
                "entry_price":  float(p.entry_price or 0),
                "qty":          float(p.qty or 0),
                "margin":       float(p.margin_used or 0),
                "margin_used":  float(p.margin_used or 0),
                "leverage":     float(p.leverage or 1),
                "stop_loss":    float(p.stop_loss or 0),
                "target_price": float(p.target_price or 0),
                "opened_at":    str(p.opened_at or ""),
                "scaled_out":   bool(p.scaled_out),
                "timeframe":    tf,
                "signal_id":    p.signal_id,
            })
        return out


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


def _price_changed_enough(old_price: float, new_price: float) -> bool:
    if not old_price or old_price <= 0:
        return True
    return abs(new_price - old_price) / old_price * 100 >= EXIT_ORDER_REPRICE_PCT


def _primary_ta(ta_data: dict) -> dict:
    for tf in ("4H", "1H", "1D"):
        data = ta_data.get(tf) if isinstance(ta_data, dict) else None
        if data and not data.get("error"):
            return data
    return {}


def _paper_exit_plan(pos: dict, current_price: float, pl_dollar: float, ta_data: dict) -> dict:
    qty = abs(float(pos.get("qty") or 0))
    leverage = max(1.0, float(pos.get("leverage") or 1.0))
    entry = float(pos.get("entry_price") or 0)
    if qty <= 0 or entry <= 0 or current_price <= 0:
        return {"ok": False}

    # The catastrophic backstop. It should almost never be what closes a
    # trade — the position carries this same level as a price-enforced stop
    # from the moment it opens, so reaching it here means the price gapped
    # past the level between polls.
    margin = float(pos.get("margin_used") or 0) or (qty * entry / max(1.0, leverage))
    hard_loss = catastrophic_loss_usd(margin)
    if pl_dollar <= -hard_loss:
        return {"ok": True, "action": "EXIT",
                "reason": (f"catastrophic backstop — lost ${abs(pl_dollar):,.2f} of "
                           f"${margin:,.2f} margin ({CATASTROPHIC_LOSS_PCT_OF_MARGIN:.0f}% cap)")}

    primary = _primary_ta(ta_data)
    atr = float(((primary.get("atr") or {}).get("value")) or current_price * MIN_DYNAMIC_TRAIL_PCT / 100.0)
    # Trail width follows the trade's own horizon. A trail calibrated for a
    # 5-minute chart stops a daily position out on its first ordinary
    # pullback — the position never gets the room its thesis needs.
    room = horizon_room_multiplier(pos.get("timeframe"))
    trail = max(atr * 1.15, current_price * MIN_DYNAMIC_TRAIL_PCT / 100.0) * room
    # The widest the stop may sit from entry, in price. Previously this was
    # $15 spread over the leveraged quantity, which pinned every stop to a
    # ~0.15% move and guaranteed the noise-triggered exits above.
    risk_per_unit = hard_loss / qty
    lock_per_unit = PROFIT_LOCK_USD / (qty * leverage) if PROFIT_LOCK_USD else 0
    direction = str(pos.get("direction") or "Long").lower()
    is_short = "short" in direction
    old_stop = float(pos.get("stop_loss") or 0)
    old_target = float(pos.get("target_price") or 0)

    if not is_short:
        stop = max(entry - risk_per_unit, current_price - trail, old_stop or 0)
        if pl_dollar >= PROFIT_LOCK_USD and lock_per_unit > 0:
            stop = max(stop, entry + lock_per_unit)
        stop = min(stop, current_price * 0.995)
        target = max(old_target or 0, current_price + max(atr * 3.0, (current_price - stop) * TARGET_REWARD_MULTIPLIER))
    else:
        candidates = [entry + risk_per_unit, current_price + trail]
        if old_stop > 0:
            candidates.append(old_stop)
        stop = min(candidates)
        if pl_dollar >= PROFIT_LOCK_USD and lock_per_unit > 0:
            stop = min(stop, entry - lock_per_unit)
        stop = max(stop, current_price * 1.005)
        target = min(old_target if old_target > 0 else current_price, current_price - max(atr * 3.0, (stop - current_price) * TARGET_REWARD_MULTIPLIER))

    precision = 6 if current_price < 1 else 2
    return {
        "ok": True,
        "action": "ADJUST",
        "stop_loss": round(max(stop, 0.000001), precision),
        "target_price": round(max(target, 0.000001), precision),
    }


def _maybe_scale_out_paper(pos: dict, current_price: float) -> dict | None:
    """Lock in partial profit at an intermediate target (TP1): close
    SCALE_OUT_FRACTION of the position, move the remaining runner's stop to
    breakeven, and leave the original target in place. Only fires once per
    position (tracked via PaperPosition.scaled_out). Direction-aware —
    unlike live, paper positions can be short."""
    if not SCALE_OUT_ENABLED or pos.get("scaled_out"):
        return None
    entry = float(pos.get("entry_price") or 0)
    target = float(pos.get("target_price") or 0)
    qty = float(pos.get("qty") or 0)
    if entry <= 0 or target <= 0 or qty <= 0:
        return None

    is_short = "short" in str(pos.get("direction") or "").lower()
    if is_short:
        if target >= entry:
            return None
        tp1_price = entry - (entry - target) * SCALE_OUT_TP1_PCT_OF_TARGET
        reached = current_price <= tp1_price
    else:
        if target <= entry:
            return None
        tp1_price = entry + (target - entry) * SCALE_OUT_TP1_PCT_OF_TARGET
        reached = current_price >= tp1_price

    if not reached:
        return None

    from lib.paper_engine import partial_close_paper_position
    result = partial_close_paper_position(pos["id"], SCALE_OUT_FRACTION, current_price, reason="scale_out_tp1")
    if not result.get("ok"):
        return None

    breakeven_stop = entry
    with get_db() as db:
        p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
        if p:
            p.stop_loss = breakeven_stop

    logger.info(
        f"[PaperTrading] ✂ Scaled out {pos['symbol']}: closed {result['closed_qty']:.6g} @ ${current_price:.4f} "
        f"(TP1 ${tp1_price:.4f}), {result['remaining_qty']:.6g} remaining with stop @ breakeven ${breakeven_stop:.4f}"
    )
    return result


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

    def _manage_one(pos: dict) -> dict:
        """One position's full evaluation — deterministic tiers, risk plan,
        then LLM. Independent of every other position (own DB sessions), so
        positions run through a small pool: the serial version made ~40
        back-to-back LLM calls at ~6s each (visible as 'one prompt at a
        time' in LM Studio) while the model has 4 idle slots."""
        r = {"evaluated": 0, "closed": 0, "held": 0, "adjusted": 0}
        sym = pos["symbol"]
        current_price = _get_current_price(sym, prices)
        if not current_price or current_price <= 0:
            logger.debug(f"[PaperTrading] No price for {sym} — skipping management")
            return r

        entry = pos["entry_price"]
        if entry <= 0:
            return r

        is_c = _is_crypto(sym)
        direction = pos["direction"].lower()
        side = -1 if direction == "short" else 1
        plpc = ((current_price - entry) / entry) * 100 * side
        # qty ALREADY carries the leveraged exposure (qty = margin*leverage/entry),
        # so multiplying by leverage again squared it — a 10x position reported
        # 100x the real P&L. Harmless when everything was 1-2x; badly wrong now
        # that conviction sizing runs to 20x.
        pl_dollar = (current_price - entry) * pos["qty"] * side
        r["evaluated"] += 1
        ta_data = _fetch_ta(sym)

        plan = _paper_exit_plan(pos, current_price, pl_dollar, ta_data)
        if plan.get("ok") and plan.get("action") == "EXIT":
            logger.warning(f"[PaperTrading] Risk EXIT {sym}: {plan.get('reason')} | P&L=${pl_dollar:.2f}")
            log_decision("paper", "EXIT", plan.get("reason", "Max paper loss breached"), symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=plan.get("reason", "risk_guard"))
            r["closed"] += 1
            return r
        if plan.get("ok") and plan.get("action") == "ADJUST":
            new_stop = float(plan["stop_loss"])
            new_target = float(plan["target_price"])
            if _price_changed_enough(pos["stop_loss"], new_stop) or _price_changed_enough(pos["target_price"], new_target):
                with get_db() as db:
                    p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                    if p:
                        p.stop_loss = new_stop
                        p.target_price = new_target
                pos["stop_loss"] = new_stop
                pos["target_price"] = new_target
                r["adjusted"] += 1
                logger.info(f"[PaperTrading] Risk guard {sym}: stop=${new_stop:.6g} target=${new_target:.6g}")

        # ── Deterministic hard rules ───────────────────────────────────────
        # Split by direction, because the two halves earned opposite verdicts
        # over 218 closed trades:
        #
        #   target / take-profit closes   17 trades, 100% win, +$4,097
        #   mechanical loss cuts          170 trades,  ~5% win, -$9,290
        #
        # So a profit close still fires immediately — every trade that
        # reached its target won, and there is nothing to deliberate about.
        # A LOSS cut now waits for the deep verify below, which is the only
        # exit path in the book that is not a disaster (-$4.20 average
        # against -$55 for the mechanical stops).
        #
        # Caveat kept deliberately in view: that comparison is confounded.
        # The LLM only ever judged positions the mechanical stops had not
        # already killed, so it was working a different population. The
        # change is a bet worth measuring, not a proven win — which is why
        # the catastrophic backstop above still fires first and
        # unconditionally.
        tier = _tier(plpc, is_c, pos.get("timeframe"))
        tier_is_loss_cut = bool(tier and tier["action"] == "close" and plpc < 0)
        if tier and tier["action"] == "close" and not tier_is_loss_cut:
            logger.info(f"[PaperTrading] 🔒 Hard rule: {sym} {plpc:+.2f}% → {tier['label']}")
            log_decision('paper', 'EXIT', tier['label'], symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=tier["label"])
            r["closed"] += 1
            return r

        if _maybe_scale_out_paper(pos, current_price):
            r["adjusted"] += 1
            return r

        # ── LLM + TA evaluation ────────────────────────────────────────────
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
        # The clock the setup implies. Without it every position is judged as
        # though it had the same horizon, so a 1D trade gets cut for the
        # wobble a 1D trade is supposed to absorb, and a 5m scalp is nursed
        # for hours after its thesis expired.
        hs = hold_status(pos.get("timeframe"), pos.get("opened_at"), STALE_HOLD_MULTIPLE)
        horizon_ctx = (
            f"  Chart timeframe: {pos.get('timeframe') or 'unknown'}\n"
            f"  Expected hold:   {hs['label']}\n"
            f"  Open for:        {_fmt_duration(hs['age_min']) if hs['age_min'] is not None else 'unknown'}"
            f"  ({hs['state']})\n"
        )
        _margin = float(pos.get("margin_used") or 0) or (abs(float(pos.get("qty") or 0)) * entry / max(1.0, float(pos.get("leverage") or 1)))
        _hard = catastrophic_loss_usd(_margin)
        _room = _hard - abs(min(0.0, pl_dollar))
        # What is riding on this call, stated plainly. The model is now the
        # deciding vote on whether a losing position is cut, so it needs to
        # know that — and how much capital stands behind the trade, since a
        # price move means nothing without the margin it is measured against.
        risk_ctx = (
            f"  Margin at risk:  ${_margin:,.2f}\n"
            f"  Backstop:        position force-closes at ${_hard:,.2f} of loss "
            f"({CATASTROPHIC_LOSS_PCT_OF_MARGIN:.0f}% of margin); "
            f"${max(0.0, _room):,.2f} of room left\n"
        )
        authority_ctx = (
            "YOUR CALL DECIDES THIS ONE. The deterministic rule above wants to cut "
            "this loss, and it has been deferred to you. HOLD keeps the position "
            "open; EXIT closes it now. Mechanical loss-cutting has performed badly "
            "on this book (about a 5% win rate), so a setup that is still "
            "structurally intact is worth holding — but do not hold a broken one "
            "just because the stop was noisy."
            if tier_is_loss_cut else
            "No deterministic rule is pending; judge the setup on its merits."
        )

        prompt = f"""You are managing an open PAPER trade position. Evaluate and decide what to do RIGHT NOW.

POSITION: {sym} ({'Crypto 24/7' if is_c else 'Equity'})
  Direction:      {pos['direction']}
  P&L:            {plpc:+.2f}%  (${pl_dollar:+.2f})
  Entry:          ${entry:.4f}
  Current Price:  ${current_price:.4f}
  Stop Loss:      ${pos['stop_loss']:.4f}
  Take Profit:    ${pos['target_price']:.4f}
  Leverage:       {pos['leverage']}x
{horizon_ctx}{risk_ctx}  Deterministic tier: {tier_label}

HORIZON MATTERS. Judge the move against the timeframe this setup was taken
on, not against the clock. A 3% adverse move refutes a 5-minute scalp and is
ordinary noise on a 1D or 1W position — do not cut a longer-horizon trade for
the wobble its own timeframe is supposed to absorb. Equally, a scalp still
open well past its expected hold has been refuted by TIME even if price has
not hit the stop: the move it was entered for did not happen.

{authority_ctx}

TECHNICAL ANALYSIS:
{ta_block}

SYMBOL NEWS:
{sym_news_ctx}

MACRO THREATS:
{threat_ctx}

RECENT MARKET NEWS:
{news_ctx}

TASK: Decide what to do with this position RIGHT NOW.
Options:
- HOLD: Setup still valid, stay in the trade
- TIGHTEN_STOP: Position at risk but not yet at stop — move stop closer. Provide new_stop_pct (% below current price, e.g. 2.0 = stop at current_price * 0.98)
- EXIT: Close this position now (bad setup, news headwind, deteriorating TA, etc.)

Respond ONLY with valid JSON (no markdown):
{{"action": "HOLD"|"TIGHTEN_STOP"|"EXIT", "new_stop_pct": null_or_float, "reasoning": "1-2 sentences"}}"""

        try:
            _wait_out_llm_cooldown()
            raw = call_lm_studio(
                prompt,
                system="You are a precise trading risk manager. Respond only with the JSON object, no markdown.",
                max_tokens=150
            )
            cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
            result = json.loads(cleaned)
            action = str(result.get("action", "HOLD")).upper()
            reasoning = result.get("reasoning", "")
            new_stop_pct = result.get("new_stop_pct")
            llm_ok = True
        except Exception as e:
            logger.warning(f"[PaperTrading] LLM eval failed for {sym}: {e} — falling back to the deterministic tier")
            action = "HOLD"
            reasoning = "LLM unavailable"
            new_stop_pct = None
            llm_ok = False

        # The loss cut deferred above is the FALLBACK, not a rule that was
        # deleted. If the deep verify could not be reached, the deterministic
        # tier decides exactly as it used to — a losing position must never
        # be held open merely because the model was unreachable.
        if tier_is_loss_cut and not llm_ok:
            logger.info(f"[PaperTrading] 🔒 Tier loss cut (LLM unavailable): {sym} {plpc:+.2f}% → {tier['label']}")
            log_decision('paper', 'EXIT', tier['label'], symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=tier["label"])
            r["closed"] += 1
            return r

        if action == "EXIT":
            logger.info(f"[PaperTrading] 🤖 LLM EXIT {sym} {plpc:+.2f}% | {reasoning}")
            log_decision("paper", "EXIT", reasoning, symbol=sym, pnl_pct=plpc, price=current_price)
            close_paper_position(pos["id"], current_price, reason=f"AI EXIT: {reasoning[:80]}")
            r["closed"] += 1
        elif action == "TIGHTEN_STOP" and new_stop_pct:
            try:
                # Direction-aware. A short's stop sits ABOVE the price, so
                # tightening moves it DOWN — the old long-only arithmetic
                # (current * (1 - pct), keep only if higher) could never
                # satisfy that test on a short, so every TIGHTEN_STOP on a
                # short was silently discarded. Most of this book is short.
                pct = abs(float(new_stop_pct)) / 100.0
                old_stop = float(pos["stop_loss"] or 0)
                if side == -1:
                    new_stop = round(current_price * (1.0 + pct), 6)
                    tighter = old_stop <= 0 or new_stop < old_stop
                    where = "above"
                else:
                    new_stop = round(current_price * (1.0 - pct), 6)
                    tighter = new_stop > old_stop
                    where = "below"
                if tighter:
                    with get_db() as db:
                        p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                        if p:
                            p.stop_loss = new_stop
                    pos["stop_loss"] = new_stop
                    logger.info(f"[PaperTrading] 🤖 TIGHTEN_STOP {sym} stop → ${new_stop:.6g} "
                                f"({new_stop_pct}% {where} ${current_price:.6g}) | {reasoning}")
                else:
                    logger.debug(f"[PaperTrading] TIGHTEN_STOP for {sym} ignored — ${new_stop:.6g} "
                                 f"is not tighter than ${old_stop:.6g}")
            except Exception as e:
                logger.warning(f"[PaperTrading] TIGHTEN_STOP update failed for {sym}: {e}")
            r["held"] += 1
        else:
            logger.info(f"[PaperTrading] 🤖 HOLD {sym} {plpc:+.2f}% | {reasoning}")
            log_decision("paper", "HOLD", reasoning, symbol=sym, pnl_pct=plpc, price=current_price)
            r["held"] += 1
        return r

    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(4, int(os.getenv("LLM_MAX_PARALLEL", "4"))))
    totals = {"evaluated": 0, "closed": 0, "held": 0, "adjusted": 0}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="papermgmt") as pool:
        for res in pool.map(_manage_one, positions):
            for k in totals:
                totals[k] += res.get(k, 0)
    return totals


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


def _wait_out_llm_cooldown(max_wait: float = 20.0) -> None:
    """Sleep through an open circuit rather than failing instantly.

    Entry evaluation runs ~30 symbols in a burst; when the breaker was open
    every one of them failed inside the same 0.2 seconds ("LLM unavailable
    — using original confidence" x30), which looked like the LLM was barely
    being used. Waiting a few seconds lets the batch actually get answers."""
    try:
        from lib.lmstudio import get_llm_cooldown
        import time as _t
        waited = 0.0
        while waited < max_wait:
            cd = get_llm_cooldown()
            if cd <= 0:
                return
            nap = min(cd + 0.25, max_wait - waited)
            if nap <= 0:
                return
            _t.sleep(nap)
            waited += nap
    except Exception:
        return


def _evaluate_entry_with_ai(sig: dict, current_price: float, threat_ctx: str, news_ctx: str) -> dict:
    from lib.lmstudio import call_lm_studio
    from lib.futures_data import FUTURES_UNIVERSE
    sym = sig["asset_symbol"]
    is_futures = sym in FUTURES_UNIVERSE
    crit = _criteria()
    # Futures keep their lower macro-driven bar unless the operator has set
    # a HIGHER one — their setting always wins upward, never downward.
    min_conf = max(FUTURES_MIN_CONFIDENCE, crit["min_score"]) if is_futures else crit["min_score"]
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

    prompt = f"""You are evaluating a paper trade entry for {sym} ({sig['asset_class']}).

SIGNAL:
  Direction: {sig['direction']}
  Original confidence: {sig['confidence']:.0f}%
  Entry: ${sig['entry_price']:.4f} | Target: ${sig['target_price']:.4f} | Stop: ${sig['stop_loss']:.4f}
  Current price: ${current_price:.4f}
  Original reasoning: {sig['reasoning'][:300]}

TECHNICAL ANALYSIS:
{ta_block}

SYMBOL NEWS:
{sym_news_ctx}

MACRO THREATS:
{threat_ctx}

RECENT MARKET NEWS:
{news_ctx}

Is this setup still valid at current price ${current_price:.4f}? Does TA confirm {sig['direction']}?

Respond ONLY with valid JSON (no markdown):
{{"approved": true/false, "score": 0-100, "reasoning": "1-2 sentences"}}

approved=true means enter the paper trade. Score below {min_conf} should set approved=false."""

    try:
        _wait_out_llm_cooldown()
        raw = call_lm_studio(
            prompt,
            system="You are a precise trading analyst. Respond only with the JSON object, no markdown.",
            max_tokens=150
        )
        cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
        result = json.loads(cleaned)
        approved = bool(result.get("approved", False))
        score = float(result.get("score", 50))
        reasoning = result.get("reasoning", "")
        crit = _criteria()
        min_conf = max(FUTURES_MIN_CONFIDENCE, crit["min_score"]) if is_futures else crit["min_score"]
        if score < min_conf:
            approved = False
        # R:R and raw-confidence gates from the same panel.
        rr = float(sig.get("rr_ratio") or 0)
        if crit["min_rr"] > 0 and rr > 0 and rr < crit["min_rr"]:
            approved = False
            reasoning = f"R:R {rr:.2f} below the configured minimum {crit['min_rr']:.2f}"
        if crit["min_confidence"] > 0 and float(sig.get("confidence") or 0) < crit["min_confidence"]:
            approved = False
            reasoning = f"confidence {float(sig.get('confidence') or 0):.0f} below the configured minimum {crit['min_confidence']:.0f}"
        return {"approved": approved, "score": score, "reasoning": reasoning}
    except Exception as e:
        logger.warning(f"[PaperTrading] Entry LLM eval failed for {sym}: {e} — using original confidence")
        crit = _criteria()
        min_conf = max(FUTURES_MIN_CONFIDENCE, crit["min_score"]) if is_futures else crit["min_score"]
        approved = sig["confidence"] >= min_conf
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
    # Hard stop-loss/take-profit checks above always run. This preference only
    # controls discretionary AI management and automatic entries.
    with get_db() as db:
        pref = db.query(UserPreference).filter(
            UserPreference.user_id == DEFAULT_USER_ID
        ).first()
        auto_trade_enabled = bool(
            pref.paper_auto_trade_enabled if pref is not None else True
        )

    mgmt = (
        _manage_open_positions(prices)
        if auto_trade_enabled
        else {"evaluated": 0, "closed": 0, "held": 0, "adjusted": 0}
    )
    logger.info(
        f"[PaperTrading] Position mgmt: evaluated={mgmt['evaluated']} | "
        f"closed={mgmt['closed']} | held={mgmt['held']} | adjusted={mgmt.get('adjusted', 0)}"
    )

    # ── Step 3: Evaluate and open new positions ───────────────────────────────
    with get_db() as db:
        sig_list = _get_pending_signals(db) if auto_trade_enabled else []

    if not auto_trade_enabled:
        logger.info(
            "[PaperTrading] Automatic entries and TA management are disabled; "
            "hard stop-loss/take-profit checks remain active"
        )

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
        f"mgmt_closed={mgmt['closed']} | mgmt_adjusted={mgmt.get('adjusted', 0)} | open={len(summary['positions'])} | "
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


