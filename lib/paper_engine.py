"""
Paper Trading Engine v2.0
Supports: Long, Long Leveraged, Short, Short Leveraged
Tracks a virtual account with P&L, mark-to-market, and margin simulation.

v2.0 Fixes:
- open_paper_position: direction key normalization is now exhaustive (handles LLM variants)
- open_paper_position: asset_class auto-detected from symbol if not provided
- mark_to_market: improved symbol lookup covers slash/no-slash variants
- mark_to_market: SHORT stop/target logic was inverted (stop ABOVE entry, target BELOW)
  — now correctly closes shorts at stop when price >= stop_loss
- mark_to_market: added missing margin_used fallback to prevent $0 margin positions
- DEFAULT_POSITION_SIZE raised to $3,000 for better trade visibility
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError
from app.database import get_db, PaperPosition, PaperTrade, PaperPortfolio
from lib.learning_engine import record_trade_outcome as _record_outcome

logger = logging.getLogger(__name__)

PAPER_STARTING_CAPITAL = 100_000.0   # $100k virtual account
MAX_LEVERAGE           = 20.0         # Max leverage multiplier (5x/10x/20x supported)
MARGIN_CALL_THRESHOLD  = 0.15         # Liquidate if equity < 15% of margin (lost 85% of capital)
DEFAULT_POSITION_SIZE  = 3_000.0      # legacy fallback when risk sizing is impossible

# ── Margin-first sizing (the trade amount IS the committed capital) ──────
# A $10 trade at 10x controls $100 of exposure but is still a $10 trade:
# $10 leaves the account and $10 is the most that can be lost. An earlier
# risk-first version inverted this — it solved for the notional needed to
# risk 1% at the stop, which produced a $125,000 position on a $100,000
# account. Exposure is now bounded by construction: commit a fixed slice of
# equity, let conviction (2x-20x) decide how far that slice reaches, and
# let the stop govern the loss WITHIN it.
TRADE_MARGIN_PCT       = 1.0    # % of equity COMMITTED per position
# Per-trade sizing alone does not bound a portfolio: 1% each x 86 positions
# committed 99.7% of the account and left $281 of cash, after which every
# new entry failed on funds. These are the PORTFOLIO-level limits that were
# missing — deployed capital and position count, checked before opening.
# The two limits are set to bind at the SAME point: at 1% per trade, 60
# positions is exactly 60% deployed. A count cap that trips first would make
# the deployment cap decorative (and vice versa) — matching them means
# whichever runs out first is the one that genuinely matters, and a
# cash-capped smaller position lets a few more trades through honestly.
MAX_DEPLOYED_PCT       = 60.0   # total margin across open positions, % of equity
MAX_OPEN_POSITIONS     = 60
MAX_MARGIN_PCT_OF_CASH = 15.0   # one position may tie up at most this % of free cash
DEFAULT_SCORE_FLOOR    = 55.0   # used only when no criteria are configured


def _configured_floor() -> float:
    """The operator's own minimum score, from Ops -> Execution Criteria.

    The leverage curve is anchored to THIS, not a constant: raise the floor
    to 70 and a 70 becomes 1x while 100 still earns the maximum, so the
    ladder always spans the range actually being traded."""
    try:
        from lib.trading_preferences import get_user_preference
        return float(get_user_preference().get("live_min_score") or DEFAULT_SCORE_FLOOR)
    except Exception:
        return DEFAULT_SCORE_FLOOR


def _historical_edge(score: float | None, asset_class: str | None,
                     direction: str | None) -> tuple[float | None, int]:
    """Realized win rate for this score band / class / direction bucket."""
    try:
        from lib.ev_model import compute_ev_buckets, _score_band
        from app.database import get_db, SignalEvaluation
        with get_db() as db:
            rows = [
                {"composite_score": r.composite_score, "asset_class": r.asset_class,
                 "direction": r.direction, "outcome": r.outcome,
                 "entry_price": r.entry_price, "exit_price": getattr(r, "exit_price", None)}
                for r in db.query(SignalEvaluation).limit(4000).all()
            ]
        band = _score_band(score)
        for b in compute_ev_buckets(rows):
            if (b["score_band"] == band
                    and str(b["asset_class"]).lower() == str(asset_class or "").lower()
                    and str(b["direction"]).lower() == str(direction or "").lower()):
                decided = int(b.get("decided") or 0)
                if decided:
                    return (int(b.get("wins") or 0) / decided), decided
                return None, 0
    except Exception as e:
        logger.debug(f"[Paper] Historical edge lookup failed: {e}")
    return None, 0


def _consecutive_losses() -> int:
    """Losing streak on the paper book — the account's own warning signal."""
    try:
        from app.database import get_db, PaperTrade
        with get_db() as db:
            recent = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(20).all()
        streak = 0
        for t in recent:
            if float(t.realized_pnl or 0) < 0:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def score_leverage(score: float | None, *, asset_class: str | None = None,
                   direction: str | None = None, atr_pct: float | None = None,
                   explain: bool = False):
    """Leverage for a signal: 1x at the configured floor rising to 25x at
    100, then reduced by regime, realized win rate, losing streak, and
    volatility. See lib/leverage_policy.py — nothing can raise it above
    what conviction alone earned."""
    from lib.leverage_policy import decide
    regime = None
    try:
        from lib.market_regime import get_regime
        regime = get_regime()
    except Exception:
        pass
    win_rate, sample = _historical_edge(score, asset_class, direction)
    result = decide(
        score, _configured_floor(),
        regime=regime, win_rate=win_rate, sample=sample,
        consecutive_losses=_consecutive_losses(), atr_pct=atr_pct,
    )
    return result if explain else result["leverage"]


def size_position(equity: float, entry: float, stop: float, leverage: float,
                  free_cash: float, margin_override: float = 0.0) -> dict:
    """Margin-first sizing.

        margin   = equity * TRADE_MARGIN_PCT   (or an explicit override)
        notional = margin * leverage
        qty      = notional / entry

    The returned loss_at_stop says what a stop-out actually costs out of
    that committed margin — the number that matters once leverage is high.
    """
    if entry <= 0 or equity <= 0:
        return {"ok": False, "reason": "cannot size: missing entry or equity"}

    margin = margin_override if margin_override > 0 else equity * (TRADE_MARGIN_PCT / 100.0)
    cap = free_cash * (MAX_MARGIN_PCT_OF_CASH / 100.0)
    capped = False
    if cap > 0 and margin > cap:
        margin = cap
        capped = True
    if margin <= 0:
        return {"ok": False, "reason": "no free cash to commit"}

    notional = margin * max(1.0, leverage)
    qty = notional / entry
    stop_distance = abs(entry - stop) if stop > 0 else 0.0
    loss_at_stop = qty * stop_distance
    return {
        "ok": True,
        "qty": qty,
        "margin": margin,
        "notional": notional,
        "leverage": leverage,
        "loss_at_stop": loss_at_stop,
        "loss_pct_of_margin": (loss_at_stop / margin * 100) if margin else 0.0,
        "capped_by_cash": capped,
    }

# A candidate close/mark price implying more than this multiple away from
# entry, in either direction, is rejected as an implausible single-interval
# move rather than trusted. No genuine mark-to-market tick (run every few
# minutes) legitimately moves a price 50x, let alone the 74,000x seen in the
# incident this guards against: a BEAT/USD crypto position (entry $0.000039)
# got marked-to-market against NASDAQ-listed BEAT's equity price ($2.87)
# when the crypto quote briefly went missing and an upstream symbol-lookup
# fallback fell through to the unrelated equity's bare ticker (see
# jobs/paper_trading.py's _get_all_prices/_get_current_price). That single
# bad tick inflated the paper portfolio's realized P&L by ~$148M. This is
# the last line of defense — it protects portfolio integrity from *any*
# upstream price-source bug, not just this specific collision.
MAX_PLAUSIBLE_PRICE_MULTIPLE = 50.0


def _price_move_is_plausible(entry: float, price: float) -> bool:
    if entry is None or price is None or entry <= 0 or price <= 0:
        return False
    ratio = price / entry
    return (1.0 / MAX_PLAUSIBLE_PRICE_MULTIPLE) <= ratio <= MAX_PLAUSIBLE_PRICE_MULTIPLE

# Leverage by asset class — futures get tighter margin than equity
ASSET_CLASS_MARGIN = {
    "futures":  1_500.0,   # Futures use smaller margin (higher leverage)
    "forex":    1_000.0,   # Forex pip-based — smaller notional per pip
    "crypto":   2_000.0,
    "equity":   3_000.0,
}

DIRECTION_LEVERAGE = {
    "Long":               (1,   1.0),
    "Bounce":             (1,   1.0),
    "Long_Leveraged":     (1,   2.0),
    "Long_5x":            (1,   5.0),
    "Long_10x":           (1,  10.0),
    "Long_20x":           (1,  20.0),
    "Short":              (-1,  1.0),
    "Short_Leveraged":    (-1,  2.0),
    "Short_5x":           (-1,  5.0),
    "Short_10x":          (-1, 10.0),
    "Short_20x":          (-1, 20.0),
}

# Exhaustive mapping for LLM output normalization
_DIR_ALIASES = {
    "long":              "Long",
    "bounce":            "Bounce",
    "long_leveraged":    "Long_Leveraged",
    "longleveraged":     "Long_Leveraged",
    "long leveraged":    "Long_Leveraged",
    "long-leveraged":    "Long_Leveraged",
    "leveraged long":    "Long_Leveraged",
    "leveraged_long":    "Long_Leveraged",
    "long_2x":           "Long_Leveraged",
    "long_5x":           "Long_5x",
    "long5x":            "Long_5x",
    "long 5x":           "Long_5x",
    "long-5x":           "Long_5x",
    "long_10x":          "Long_10x",
    "long10x":           "Long_10x",
    "long 10x":          "Long_10x",
    "long_20x":          "Long_20x",
    "long20x":           "Long_20x",
    "long 20x":          "Long_20x",
    "short":             "Short",
    "short_leveraged":   "Short_Leveraged",
    "shortleveraged":    "Short_Leveraged",
    "short leveraged":   "Short_Leveraged",
    "short-leveraged":   "Short_Leveraged",
    "leveraged short":   "Short_Leveraged",
    "leveraged_short":   "Short_Leveraged",
    "short_2x":          "Short_Leveraged",
    "short_5x":          "Short_5x",
    "short5x":           "Short_5x",
    "short 5x":          "Short_5x",
    "short-5x":          "Short_5x",
    "short_10x":         "Short_10x",
    "short10x":          "Short_10x",
    "short 10x":         "Short_10x",
    "short_20x":         "Short_20x",
    "short20x":          "Short_20x",
    "short 20x":         "Short_20x",
}


def _normalize_direction(raw: str) -> str:
    """Normalize any LLM direction output to a canonical DIRECTION_LEVERAGE key."""
    if not raw:
        return "Long"
    cleaned = raw.strip().replace(" ", "_").replace("-", "_")
    # Try direct match first
    if cleaned in DIRECTION_LEVERAGE:
        return cleaned
    # Try alias map (case-insensitive)
    lower = cleaned.lower().replace("_", " ")
    for alias, canonical in _DIR_ALIASES.items():
        if lower == alias:
            return canonical
    # Fallback: if "short" anywhere → Short
    if "short" in cleaned.lower():
        return "Short_Leveraged" if "lever" in cleaned.lower() else "Short"
    if "lever" in cleaned.lower():
        return "Long_Leveraged"
    return "Long"


def _now(): return datetime.now(timezone.utc).isoformat()


def _get_portfolio_cash(db):
    """Fetch the paper portfolio record. init_db() guarantees it exists."""
    p = db.query(PaperPortfolio).first()
    if not p:
        from app.database import new_id
        p = PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        )
        db.add(p)
        db.flush()
        logger.warning("[Paper] Portfolio row was missing — created with $100k starting capital")
    return p


def _calc_pnl(entry: float, close_price: float, qty: float, side: int, leverage: float, margin: float):
    """
    Unified P&L calculation.
    - qty = notional / entry  (notional = margin * leverage)
    - raw_pnl = price_move * qty * side  →  already reflects full leveraged exposure
    - pnl_pct uses MARGIN (capital at risk) as the base, which gives the correct ROI
    """
    raw_pnl = (close_price - entry) * qty * side
    pnl_pct = (raw_pnl / margin) * 100 if margin else 0.0
    return raw_pnl, pnl_pct


def open_paper_position(signal: dict, current_price: float = None) -> dict:
    """
    Open a new paper position from a trading signal.
    direction can be: Long, Bounce, Long_Leveraged, Short, Short_Leveraged
    """
    sym = signal.get("asset_symbol", "").upper().strip()
    if not sym:
        return {"error": "No asset_symbol provided"}

    # Normalize direction — handle any LLM output variant
    raw_dir = signal.get("paper_direction") or signal.get("direction") or "Long"
    dir_key = _normalize_direction(raw_dir)

    side, leverage = DIRECTION_LEVERAGE[dir_key]
    entry = float(current_price or signal.get("entry_price") or 0)
    # Try futures price source if still no price
    if (not entry or entry <= 0):
        try:
            from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                fd = get_cached_futures_price(sym)
                if fd:
                    entry = float(fd.get("price") or 0)
        except Exception:
            pass
    if not entry or entry <= 0:
        return {"error": f"No valid entry price for {sym} (got: {current_price}, signal entry: {signal.get('entry_price')})"}

    # Auto-detect asset class (Equity | Crypto | Futures | Forex)
    asset_class_raw = (signal.get("asset_class") or "").lower()
    if "futures" in asset_class_raw or "commodity" in asset_class_raw:
        asset_class = "Futures"
    elif "forex" in asset_class_raw or "currency" in asset_class_raw:
        asset_class = "Forex"
    elif "/" in sym or sym.upper().endswith("USD"):
        asset_class = "Crypto"
    else:
        # Check the futures universe
        try:
            from lib.futures_data import FUTURES_UNIVERSE
            if sym in FUTURES_UNIVERSE:
                cat = FUTURES_UNIVERSE[sym]["category"]
                asset_class = "Forex" if cat == "Forex" else "Futures"
            else:
                asset_class = "Equity"
        except Exception:
            asset_class = "Equity"

    target = float(signal.get("target_price") or 0)
    stop   = float(signal.get("stop_loss") or 0)

    # Ensure stop/target are on the correct side of entry
    if side == 1:  # Long / Bounce / Long_Leveraged
        if not target or target <= entry:
            target = round(entry * 1.05, 4 if entry < 1 else 2)
        if not stop or stop >= entry:
            stop = round(entry * 0.97, 4 if entry < 1 else 2)
    else:  # Short / Short_Leveraged — stop ABOVE entry, target BELOW entry
        if not target or target >= entry:
            target = round(entry * 0.95, 4 if entry < 1 else 2)
        if not stop or stop <= entry:
            stop = round(entry * 1.03, 4 if entry < 1 else 2)

    # ── Stop discipline before sizing ────────────────────────────────────
    # Two ceilings, whichever is tighter: the horizon cap (3% scalp / 10%
    # longer) and the liquidation bound (a position at leverage L is wiped
    # out by a 1/L adverse move, so the stop must sit well inside that).
    try:
        from lib.trading_preferences import horizon_for_timeframe
        _horizon = horizon_for_timeframe(signal.get("timeframe"))
    except Exception:
        _horizon = "all"
    _horizon_cap = 0.03 if _horizon == "scalp" else 0.10
    _prelim_lev = leverage if leverage > 1.0 else score_leverage(
        signal.get("composite_score") or signal.get("confidence"),
        asset_class=asset_class, direction=dir_key,
        atr_pct=float(signal.get("atr_pct") or 0) or None)
    _liq_cap = 0.80 / max(1.0, _prelim_lev)          # 80% of margin, never 100%
    _max_stop_frac = min(_horizon_cap, _liq_cap)
    _max_move = entry * _max_stop_frac
    if side == 1:
        _floor = entry - _max_move
        if stop < _floor:
            logger.info(f"[Paper] {sym} stop {stop:g} -> {_floor:g} "
                        f"({_max_stop_frac:.1%} cap at {_prelim_lev:g}x, {_horizon})")
            stop = round(_floor, 8)
    else:
        _ceil = entry + _max_move
        if stop > _ceil:
            logger.info(f"[Paper] {sym} stop {stop:g} -> {_ceil:g} "
                        f"({_max_stop_frac:.1%} cap at {_prelim_lev:g}x, {_horizon})")
            stop = round(_ceil, 8)

    # ── Sizing: the SIGNAL decides quantity ──────────────────────────────
    # Leverage comes from conviction (2x-20x), quantity from the setup's own
    # stop distance, so every position risks the same slice of equity.
    # An explicit direction like Long_10x still wins — that is a deliberate
    # instruction, not an inference.
    ac_lower = asset_class.lower()
    override_margin = float(signal.get("margin_override") or 0)
    explicit_leverage = leverage > 1.0
    conviction = signal.get("composite_score") or signal.get("confidence")
    if not explicit_leverage:
        leverage = score_leverage(
            conviction, asset_class=asset_class, direction=dir_key,
            atr_pct=float(signal.get("atr_pct") or 0) or None,
        )

    sizing = {"ok": False}
    try:
        with get_db() as _db:
            _pf = _get_portfolio_cash(_db)
            _equity = float(_pf.cash or 0) + sum(
                float(r.margin_used or 0)
                for r in _db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            )
            sizing = size_position(_equity, entry, stop, leverage, float(_pf.cash or 0),
                                   margin_override=override_margin)
    except Exception as e:
        logger.warning(f"[Paper] Risk sizing unavailable ({e}) — falling back to flat margin")

    if sizing.get("ok"):
        qty = round(sizing["qty"], 6)
        margin = round(sizing["margin"], 2)
        notional = sizing["notional"]
        logger.info(
            f"[Paper] {sym}: ${margin:,.0f} committed @ {leverage:g}x = ${notional:,.0f} exposure | "
            f"qty={qty:g} | stop-out costs ${sizing['loss_at_stop']:,.0f} "
            f"({sizing['loss_pct_of_margin']:.0f}% of the ${margin:,.0f} committed)"
            + (" [capped by free cash]" if sizing.get("capped_by_cash") else "")
        )
    else:
        base_margin = (override_margin if override_margin > 0
                       else ASSET_CLASS_MARGIN.get(ac_lower, DEFAULT_POSITION_SIZE))
        margin   = base_margin
        notional = margin * leverage
        if entry < 2.0 and ac_lower == "forex":
            qty = round(notional / (entry * 1000), 2) if entry > 0 else 0.0
            qty = max(qty, 0.01)
        else:
            qty = round(notional / entry, 6)

    # NOTE on the duplicate-open race: the "already open?" check below and the
    # INSERT further down happen in the same SQLAlchemy session/transaction,
    # but that only protects against races *within* this process — a second
    # process/thread (e.g. a concurrent scheduler cycle vs. a Telegram
    # callback) using its own session can still pass the SELECT before this
    # transaction commits. The real guard is the partial unique index
    # `uq_paper_position_open_symbol` on paper_positions(user_id, symbol)
    # WHERE status='Open' (see app/database.py:_ensure_paper_position_unique_open_index),
    # which turns that race into an IntegrityError we catch below instead of
    # a silent duplicate position. On a pre-existing DB with duplicate open
    # rows the index creation is skipped (logged at startup) and this
    # residual race remains until those duplicates are cleaned up.
    try:
        with get_db() as db:
            existing = db.query(PaperPosition).filter(
                PaperPosition.symbol == sym,
                PaperPosition.status == "Open"
            ).first()
            if existing:
                return {"error": f"Paper position already open for {sym}"}

            portfolio = _get_portfolio_cash(db)

            # ── Portfolio-level capacity checks ──────────────────────────
            open_rows = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
            deployed = sum(float(r.margin_used or 0) for r in open_rows)
            equity_now = float(portfolio.cash or 0) + deployed
            if len(open_rows) >= MAX_OPEN_POSITIONS:
                return {"error": (f"Paper book full: {len(open_rows)}/{MAX_OPEN_POSITIONS} positions open. "
                                  f"Close something before adding risk.")}
            deploy_cap = equity_now * (MAX_DEPLOYED_PCT / 100.0)
            if deployed + margin > deploy_cap:
                return {"error": (f"Paper deployment cap reached: ${deployed:,.0f} of ${deploy_cap:,.0f} "
                                  f"({MAX_DEPLOYED_PCT:.0f}% of ${equity_now:,.0f} equity) already committed.")}

            logger.info(
                f"[Paper] Cash ${portfolio.cash:,.2f} | deployed ${deployed:,.0f}/${deploy_cap:,.0f} "
                f"({len(open_rows)}/{MAX_OPEN_POSITIONS} slots) | this trade ${margin:,.2f}"
            )
            if portfolio.cash < margin:
                logger.warning(f"[Paper] Insufficient cash — have ${portfolio.cash:.2f}, need ${margin:.2f}")
                return {"error": f"Insufficient paper cash (${portfolio.cash:.0f}) for margin ${margin:.0f}. Use /api/paper/reset to restore $100k."}

            from app.database import new_id
            pos = PaperPosition(
                id            = new_id(),
                symbol        = sym,
                asset_class   = asset_class,
                direction     = dir_key,
                side          = "long" if side == 1 else "short",
                leverage      = leverage,
                qty           = qty,
                entry_price   = entry,
                current_price = entry,
                target_price  = target,
                stop_loss     = stop,
                notional      = notional,
                margin_used   = margin,
                unrealized_pnl= 0.0,
                unrealized_pct= 0.0,
                signal_id     = signal.get("id"),
                status        = "Open",
                opened_at     = _now(),
                updated_at    = _now(),
            )
            db.add(pos)
            portfolio.cash    -= margin
            portfolio.updated_at = _now()

            pos_id   = pos.id
            pos_data = {
                "id": pos_id, "symbol": sym, "direction": dir_key,
                "side": "long" if side == 1 else "short", "leverage": leverage, "qty": qty,
                "entry_price": entry, "target": target, "stop": stop,
                "notional": notional, "margin_required": margin, "asset_class": asset_class,
            }
    except IntegrityError:
        # Lost the race: another session committed an open position for this
        # symbol between our SELECT above and this transaction's commit.
        logger.warning(f"[Paper] Duplicate-open race detected for {sym} — a position was already opened concurrently")
        return {"error": f"Paper position already open for {sym}"}

    logger.info(
        f"[Paper] ✅ Opened {dir_key} on {sym} ({asset_class}) @ ${entry:.4f} | "
        f"qty={qty:.4f} | notional=${notional:.0f} | margin=${margin:.0f} | "
        f"target=${target:.4f} | stop=${stop:.4f}"
    )
    return {"ok": True, "position": pos_data}


def close_paper_position(pos_id: str, close_price: float, reason: str = "manual") -> dict:
    """Close a paper position and record the trade."""
    result = {}
    log_symbol = ""
    log_direction = ""
    log_pnl = 0.0
    log_pct = 0.0

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}

        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at
        pos_notional  = float(pos.notional or 0)
        pos_margin    = float(pos.margin_used or DEFAULT_POSITION_SIZE)

        entry  = float(pos.entry_price)
        qty    = float(pos.qty)
        lev    = float(pos.leverage or 1.0)
        side   = 1 if pos_side == "long" else -1

        if not _price_move_is_plausible(entry, close_price):
            logger.error(
                f"[Paper] Rejected close for {pos_symbol}: entry=${entry:.6g} candidate_close=${close_price:.6g} "
                f"is a {close_price / entry if entry else 0:.1f}x move — almost certainly a bad price (symbol "
                f"collision or stale/wrong data source), not a real market move. Position left open."
            )
            return {"error": f"Rejected implausible close price for {pos_symbol}: ${close_price:.6g} vs entry ${entry:.6g}"}

        pnl, pnl_pct = _calc_pnl(entry, close_price, qty, side, lev, pos_margin)

        portfolio = _get_portfolio_cash(db)
        portfolio.cash         += pos_margin + pnl
        portfolio.realized_pnl  = (portfolio.realized_pnl or 0) + pnl
        portfolio.total_trades  = (portfolio.total_trades or 0) + 1
        if pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        from app.database import new_id
        trade = PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos_symbol,
            asset_class  = pos_asset_cls,
            direction    = pos_direction,
            side         = pos_side,
            leverage     = lev,
            qty          = qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = pos_notional,
            realized_pnl = pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        )
        db.add(trade)

        pos.status         = "Closed"
        pos.current_price  = close_price
        pos.unrealized_pnl = pnl
        pos.updated_at     = _now()

        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price
        }
        log_symbol     = pos_symbol
        log_direction  = pos_direction
        log_asset_cls  = pos_asset_cls
        log_entry      = entry
        log_qty        = qty
        log_pnl        = pnl
        log_pct        = pnl_pct
        # Pull signal metadata for learning engine
        log_timeframe  = "4H"
        log_confidence = None
        log_reasoning  = None
        if pos_signal_id:
            try:
                from app.database import TradingSignal
                sig_row = db.query(TradingSignal).filter(TradingSignal.id == pos_signal_id).first()
                if sig_row:
                    log_timeframe  = sig_row.timeframe or "4H"
                    log_confidence = float(sig_row.confidence) if sig_row.confidence else None
                    log_reasoning  = sig_row.reasoning or None
            except Exception:
                pass

    # ── Record to Learning Engine (Tiers 1-5) ───────────────────────────
    try:
        _record_outcome(
            symbol=log_symbol,
            asset_class=log_asset_cls,
            direction=log_direction,
            entry_price=log_entry,
            exit_price=close_price,
            qty=log_qty,
            exit_reason=reason,
            timeframe=log_timeframe,
            signal_confidence=log_confidence,
            signal_reasoning=log_reasoning,
            ta_profile=None,   # TA not re-fetched at paper close — populated next cycle
            market_regime=None,
            paper_mode=True,
        )
    except Exception as _le:
        logger.warning(f"[Paper][Learning] record_outcome failed: {_le}")
    logger.info(f"[Paper] Closed {log_symbol} ({log_direction}) @ ${close_price:.4f} | P&L=${log_pnl:.2f} ({log_pct:.1f}%) | {reason}")
    return result


def partial_close_paper_position(pos_id: str, close_fraction: float, close_price: float, reason: str = "scale_out") -> dict:
    """Realize P&L on a fraction of an open paper position and keep the rest
    open with reduced size — mirrors close_paper_position but for a partial
    exit (e.g. locking in profit at an intermediate target). The remaining
    qty/notional/margin are reduced proportionally; the position stays Open."""
    if not (0 < close_fraction < 1):
        return {"error": "close_fraction must be between 0 and 1 (exclusive)"}

    with get_db() as db:
        pos = db.query(PaperPosition).filter(PaperPosition.id == pos_id).first()
        if not pos or pos.status != "Open":
            return {"error": "Position not found or already closed"}
        if bool(pos.scaled_out):
            return {"error": "Position has already been scaled out"}

        pos_symbol    = pos.symbol
        pos_direction = pos.direction
        pos_side      = pos.side
        pos_asset_cls = pos.asset_class
        pos_signal_id = pos.signal_id
        pos_opened_at = pos.opened_at

        entry = float(pos.entry_price)
        qty   = float(pos.qty)
        lev   = float(pos.leverage or 1.0)
        side  = 1 if pos_side == "long" else -1
        notional = float(pos.notional or 0)
        margin   = float(pos.margin_used or DEFAULT_POSITION_SIZE)

        close_qty   = qty * close_fraction
        remain_qty  = qty - close_qty
        close_margin   = margin * close_fraction
        remain_margin  = margin - close_margin
        close_notional = notional * close_fraction
        remain_notional = notional - close_notional
        if close_qty <= 0 or remain_qty <= 0:
            return {"error": "Position too small to split"}

        if not _price_move_is_plausible(entry, close_price):
            logger.error(
                f"[Paper] Rejected partial close for {pos_symbol}: entry=${entry:.6g} candidate_close=${close_price:.6g} "
                f"is a {close_price / entry if entry else 0:.1f}x move — almost certainly a bad price, not a real "
                f"market move. Position left open, unmodified."
            )
            return {"error": f"Rejected implausible close price for {pos_symbol}: ${close_price:.6g} vs entry ${entry:.6g}"}

        pnl, pnl_pct = _calc_pnl(entry, close_price, close_qty, side, lev, close_margin)

        portfolio = _get_portfolio_cash(db)
        portfolio.cash        += close_margin + pnl
        portfolio.realized_pnl = (portfolio.realized_pnl or 0) + pnl
        portfolio.total_trades = (portfolio.total_trades or 0) + 1
        if pnl > 0:
            portfolio.winning_trades = (portfolio.winning_trades or 0) + 1
        portfolio.updated_at = _now()

        from app.database import new_id
        db.add(PaperTrade(
            id           = new_id(),
            position_id  = pos_id,
            symbol       = pos_symbol,
            asset_class  = pos_asset_cls,
            direction    = pos_direction,
            side         = pos_side,
            leverage     = lev,
            qty          = close_qty,
            entry_price  = entry,
            exit_price   = close_price,
            notional     = close_notional,
            realized_pnl = pnl,
            pnl_pct      = pnl_pct,
            close_reason = reason,
            signal_id    = pos_signal_id,
            opened_at    = pos_opened_at,
            closed_at    = _now(),
        ))

        pos.qty            = remain_qty
        pos.notional        = remain_notional
        pos.margin_used     = remain_margin
        pos.scaled_out       = True
        pos.scaled_out_qty   = close_qty
        pos.updated_at       = _now()

        result = {
            "ok": True, "symbol": pos_symbol, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "reason": reason, "close_price": close_price,
            "closed_qty": close_qty, "remaining_qty": remain_qty,
        }

    try:
        _record_outcome(
            symbol=pos_symbol, asset_class=pos_asset_cls, direction=pos_direction,
            entry_price=entry, exit_price=close_price, qty=close_qty,
            exit_reason=reason, paper_mode=True,
        )
    except Exception as _le:
        logger.warning(f"[Paper][Learning] partial-close record_outcome failed: {_le}")

    logger.info(
        f"[Paper] Scaled out {pos_symbol} ({pos_direction}): closed {close_qty:.6g} @ ${close_price:.4f} "
        f"| P&L=${pnl:.2f} ({pnl_pct:.1f}%) | {remain_qty:.6g} remaining"
    )
    return result


def mark_to_market(prices: dict) -> dict:
    """
    Update unrealized P&L for all open paper positions.
    prices = {symbol: current_price}
    Auto-triggers stop-loss / take-profit / margin-call checks.
    """
    closed  = []
    updated = []

    with get_db() as db:
        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = [
            {
                "id":           p.id,
                "symbol":       p.symbol,
                "entry_price":  float(p.entry_price or 0),
                "qty":          float(p.qty or 0),
                "side":         p.side or "long",
                "leverage":     float(p.leverage or 1.0),
                "target_price": float(p.target_price or 0),
                "stop_loss":    float(p.stop_loss or 0),
                "notional":     float(p.notional or 0),
                "margin_used":  float(p.margin_used or DEFAULT_POSITION_SIZE),
                "direction":    p.direction or "Long",
            }
            for p in positions
            if p.entry_price and p.qty
        ]

    for pos in pos_list:
        sym = pos["symbol"]
        # Try multiple price lookup variants
        price = (
            prices.get(sym) or
            prices.get(sym.replace("/USD", "")) or
            prices.get(sym.replace("/", "") + "USD") or
            prices.get(sym.replace("/", ""))
        )
        # Futures/Forex fallback — covers GC=F, EURUSD=X, CL=F, etc.
        if not price:
            try:
                from lib.futures_data import get_cached_futures_price, FUTURES_UNIVERSE
                if sym in FUTURES_UNIVERSE:
                    fd = get_cached_futures_price(sym)
                    if fd and fd.get("price"):
                        price = float(fd["price"])
            except Exception:
                pass

        # Also check with and without = suffix (yfinance oddities)
        if not price:
            for alt in [sym.upper(), sym.replace("=X",""), sym.replace("=F","")]:
                if alt in prices and prices[alt]:
                    price = float(prices[alt])
                    break

        if not price:
            logger.debug(f"[Paper] No price in MTM for {sym}")
            continue

        entry  = pos["entry_price"]
        qty    = pos["qty"]
        lev    = pos["leverage"]
        margin = pos["margin_used"]
        side   = 1 if pos["side"] == "long" else -1

        if not _price_move_is_plausible(entry, price):
            logger.error(
                f"[Paper] Rejected MTM price for {sym}: entry=${entry:.6g} candidate=${price:.6g} "
                f"is a {price / entry if entry else 0:.1f}x move — almost certainly a symbol collision or bad "
                f"tick from an upstream price source, not a real move. Skipping this cycle; position stays open "
                f"at its last known-good price."
            )
            continue

        pnl, pct = _calc_pnl(entry, price, qty, side, lev, margin)

        # Trigger checks — MUST respect side direction:
        # LONG:  stop when price falls BELOW stop_loss, profit when price rises ABOVE target
        # SHORT: stop when price rises ABOVE stop_loss, profit when price falls BELOW target
        reason = None
        stop   = pos["stop_loss"]
        target = pos["target_price"]

        if side == 1:   # LONG
            if stop  > 0 and price <= stop:    reason = "stop_loss"
            elif target > 0 and price >= target: reason = "take_profit"
        else:           # SHORT
            if stop  > 0 and price >= stop:    reason = "stop_loss"
            elif target > 0 and price <= target: reason = "take_profit"

        # Margin call: equity in position (margin + pnl) < 15% of original margin (lost 85%)
        equity_in_pos = margin + pnl
        if margin > 0 and equity_in_pos < margin * MARGIN_CALL_THRESHOLD:
            reason = "margin_call"

        if reason:
            result = close_paper_position(pos["id"], price, reason)
            closed.append({"symbol": sym, "reason": reason, "pnl": result.get("pnl")})
        else:
            with get_db() as db:
                p = db.query(PaperPosition).filter(PaperPosition.id == pos["id"]).first()
                if p:
                    p.current_price   = price
                    p.unrealized_pnl  = round(pnl, 2)
                    p.unrealized_pct  = round(pct, 2)
                    p.updated_at      = _now()
            updated.append(sym)

    return {"updated": len(updated), "closed": closed}


def get_paper_summary() -> dict:
    """Return portfolio summary and open positions. Null-safe throughout."""
    with get_db() as db:
        portfolio = _get_portfolio_cash(db)
        p_data = {
            "cash":           round(float(portfolio.cash or 0), 2),
            "total_trades":   int(portfolio.total_trades or 0),
            "winning_trades": int(portfolio.winning_trades or 0),
            "realized_pnl":   round(float(portfolio.realized_pnl or 0), 2),
            "updated_at":     portfolio.updated_at,
        }

        positions = db.query(PaperPosition).filter(PaperPosition.status == "Open").all()
        pos_list = []
        for p in positions:
            if not p.entry_price:
                continue
            try:
                pos_list.append({
                    "id":            p.id,
                    "symbol":        p.symbol or "",
                    "direction":     p.direction or "Long",
                    "side":          p.side or "long",
                    "leverage":      float(p.leverage or 1.0),
                    "qty":           float(p.qty or 0),
                    "entry_price":   float(p.entry_price or 0),
                    "current_price": float(p.current_price or p.entry_price or 0),
                    "target_price":  float(p.target_price or 0),
                    "stop_loss":     float(p.stop_loss or 0),
                    "notional":      float(p.notional or 0),
                    "margin_used":   float(p.margin_used or DEFAULT_POSITION_SIZE),
                    "unrealized_pnl":float(p.unrealized_pnl or 0),
                    "unrealized_pct":float(p.unrealized_pct or 0),
                    "opened_at":     p.opened_at.isoformat() if hasattr(p.opened_at, "isoformat") else (p.opened_at or ""),
                    "asset_class":   p.asset_class or "Equity",
                    "signal_id":     p.signal_id or "",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad position row {p.id}: {e}")

        trades = db.query(PaperTrade).order_by(PaperTrade.closed_at.desc()).limit(50).all()
        trade_list = []
        for t in trades:
            try:
                trade_list.append({
                    "id":           t.id,
                    "symbol":       t.symbol or "",
                    "direction":    t.direction or "Long",
                    "side":         t.side or "long",
                    "leverage":     float(t.leverage or 1.0),
                    "entry_price":  float(t.entry_price or 0),
                    "exit_price":   float(t.exit_price or 0),
                    "realized_pnl": round(float(t.realized_pnl or 0), 2),
                    "pnl_pct":      round(float(t.pnl_pct or 0), 2),
                    "close_reason": t.close_reason or "manual",
                    "opened_at":    t.opened_at.isoformat() if hasattr(t.opened_at, "isoformat") else (t.opened_at or ""),
                    "closed_at":    t.closed_at.isoformat() if hasattr(t.closed_at, "isoformat") else (t.closed_at or ""),
                    "asset_class":  t.asset_class or "Equity",
                })
            except Exception as e:
                logger.warning(f"[Paper] Skipping bad trade row {t.id}: {e}")

    # Join signal data for paper positions (for TA/reasoning/news display)
    signal_ids = [p["signal_id"] for p in pos_list if p.get("signal_id")]
    signal_map = {}
    if signal_ids:
        try:
            from app.database import TradingSignal
            sigs = db.query(TradingSignal).filter(TradingSignal.id.in_(signal_ids)).all()
            for s in sigs:
                signal_map[s.id] = {
                    "id":             s.id,
                    "direction":      s.direction or "Long",
                    "confidence":     float(s.confidence or 0),
                    "composite_score":float(getattr(s, "composite_score", None) or s.confidence or 0),
                    "timeframe":      s.timeframe or "",
                    "reasoning":      s.reasoning or "",
                    "key_risks":      getattr(s, "key_risks", None) or "",
                    "momentum":       getattr(s, "momentum", None) or "",
                    "signal_source":  getattr(s, "signal_source", None) or "watchlist",
                    "generated_at":   s.generated_at.isoformat() if hasattr(s.generated_at, "isoformat") else (s.generated_at or ""),
                    "entry_price":    float(s.entry_price or 0),
                    "target_price":   float(s.target_price or 0),
                    "stop_loss":      float(s.stop_loss or 0),
                    "status":         s.status or "",
                    "trigger_event":  getattr(s, "trigger_event", None) or "",
                }
        except Exception as e:
            logger.warning(f"[Paper] Could not join signals: {e}")

    # Attach signal context to each position
    for p in pos_list:
        p["signal"] = signal_map.get(p.get("signal_id"), None)

    open_pnl  = sum(p["unrealized_pnl"] for p in pos_list)
    margin_in = sum(p["margin_used"] for p in pos_list)
    # Equity = cash on hand + margin deployed + any unrealized gains/losses
    # (margin is still your capital — just locked in positions, not lost)
    equity    = p_data["cash"] + margin_in + open_pnl
    total     = p_data["total_trades"]
    wins      = p_data["winning_trades"]
    win_rate  = round(wins / total * 100, 1) if total > 0 else 0.0

    return {
        "portfolio": {
            **p_data,
            "open_pnl":          round(open_pnl, 2),
            "equity":            round(equity, 2),
            "margin_in_use":     round(margin_in, 2),
            "win_rate":          win_rate,
            "starting_capital":  PAPER_STARTING_CAPITAL,
            "total_return_pct":  round((equity - PAPER_STARTING_CAPITAL) / PAPER_STARTING_CAPITAL * 100, 2),
        },
        "positions": pos_list,
        "trades":    trade_list,
    }


def reset_paper_portfolio() -> dict:
    """Reset the paper portfolio back to $100k starting capital."""
    from app.database import new_id
    with get_db() as db:
        db.query(PaperTrade).delete()
        db.query(PaperPosition).delete()
        db.query(PaperPortfolio).delete()
        db.flush()
        db.add(PaperPortfolio(
            id=new_id(),
            cash=PAPER_STARTING_CAPITAL,
            total_trades=0,
            winning_trades=0,
            realized_pnl=0.0,
            updated_at=_now()
        ))
    logger.info("[Paper] Portfolio hard reset to $100,000")
    return {"ok": True, "cash": PAPER_STARTING_CAPITAL}
