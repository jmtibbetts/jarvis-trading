"""Walk historical signals forward under the CURRENT exit rules.

The problem this solves: 8,904 recorded outcomes were quarantined because
93.6% of them were closed by exit rules that no longer exist. Calibration
therefore has no history, confidence caps at the no-evidence ceiling, and
almost nothing clears the gates — no data, so no trades, so no data.

The price history is still real, though. What was wrong was the exit logic
applied to it, not the bars. So each historical signal is replayed: take its
own entry, stop and target, walk the actual subsequent bars, and close it
the way the system would close it TODAY —

    the catastrophic backstop at 35% of margin, as a PRICE
    the horizon-scaled tier cuts
    the hold window from its own timeframe
    real venue fees, spread and funding

producing an outcome that describes the current machine rather than the old
one.

REPLAY IS NOT LIVE, and is labelled so. A replayed fill is perfect: no
slippage variance, no partial fills, no queue position, and the bar's high
and low are both assumed reachable. That makes replay systematically
optimistic, so calibration weights it below live evidence and can later
measure how far the two diverged. Mixing them indistinguishably would mean
calibrating partly on backtest optimism without knowing it.

NO LOOKAHEAD. Only bars strictly AFTER a signal's generated_at are used.
A signal is skipped rather than guessed at when its history is missing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Marks an outcome as simulated rather than observed. Calibration must be
# able to tell the difference — see the module docstring.
SOURCE_REPLAY = "replay"

# Where a path label came from. Kept separate from `outcome_source` because
# a live TRADE can still have a replay-derived path (reconstructed from
# bars) and the two provenances answer different questions.
PATH_SOURCE_REPLAY = "REPLAY_OHLC"
PATH_SOURCE_LIVE = "LIVE_OBSERVED"
SOURCE_LIVE = "live"

# Bars to walk forward before giving up on a signal. Beyond its own hold
# window a setup has been refuted by time, which is itself a real outcome.
MAX_BARS = 400

# A signal that resolves in its FIRST bar predicted nothing: the level was
# already inside that bar's range when the signal was generated. Measured,
# every 1m outcome in the first full replay resolved in one bar and the
# bucket read 98.2% — an artifact, not an edge.
MIN_BARS_TO_RESOLVE = 2


def _utc(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    except Exception:
        return None


def replay_signal(signal: dict, bars, margin: float = 1000.0) -> dict | None:
    """One signal, walked forward through its own subsequent bars.

    Returns an outcome dict, or None when there is no usable history — never
    a guessed result.
    """
    try:
        entry = float(signal.get("entry_price") or 0)
        stop = float(signal.get("stop_loss") or 0)
        target = float(signal.get("target_price") or 0)
    except (TypeError, ValueError):
        return None
    if not (entry > 0 and stop > 0 and target > 0):
        return None

    is_short = str(signal.get("direction") or "Long").lower().startswith("short")
    started = _utc(signal.get("generated_at"))
    if started is None or bars is None or len(bars) == 0:
        return None

    # Strictly after the signal existed. A bar that overlaps the moment of
    # generation may contain the move the signal was reacting to.
    try:
        future = bars[bars.index > started]
    except Exception:
        return None
    if future is None or len(future) == 0:
        return None
    future = future.head(MAX_BARS)

    # The exit levels the CURRENT system would use.
    from jobs.paper_trading import catastrophic_stop_price
    qty = margin / entry if entry else 0
    hard = catastrophic_stop_price(entry, qty, margin, is_short)
    if hard:
        stop = min(stop, hard) if is_short else max(stop, hard)

    from lib.trade_horizon import expected_hold_minutes
    _lo, hold_max = expected_hold_minutes(signal.get("timeframe"))

    # ── Path tracking ────────────────────────────────────────────────────
    # How far the trade went the RIGHT way before it resolved (MFE) and how
    # far the wrong way (MAE). The outcome alone cannot distinguish a trade
    # that went straight to target from one that spent two bars nearly
    # stopped out first — same P&L, completely different risk, and only one
    # of them is repeatable. `trade_outcomes` records entry and exit and
    # nothing in between, so this loop is the only place these can be
    # measured: it is already walking exactly the bars required.
    max_favorable = max_adverse = 0.0
    mfe_bar = mae_bar = None
    first_touch = None

    exit_price, reason, bars_held = None, None, 0
    for i, (_, bar) in enumerate(future.iterrows(), start=1):
        bars_held = i
        hi, lo = float(bar["high"]), float(bar["low"])

        if is_short:
            favorable, adverse = entry - lo, hi - entry
            stop_touched, target_touched = hi >= stop, lo <= target
        else:
            favorable, adverse = hi - entry, entry - lo
            stop_touched, target_touched = lo <= stop, hi >= target

        if favorable > max_favorable:
            max_favorable, mfe_bar = favorable, i
        if adverse > max_adverse:
            max_adverse, mae_bar = adverse, i

        if first_touch is None and (stop_touched or target_touched):
            # Both inside one bar: OHLC cannot reveal intrabar ordering, and
            # picking the profitable one is precisely how a backtest invents
            # an edge. Recorded as AMBIGUOUS so the label can be excluded
            # from first-touch training rather than silently biasing it.
            if stop_touched and target_touched:
                first_touch = "AMBIGUOUS"
            elif stop_touched:
                first_touch = "STOP"
            else:
                first_touch = "TARGET"

        if is_short:
            # Stop first: within a single bar the order of touches is
            # unknowable, and assuming the favourable one is how a backtest
            # flatters itself.
            if hi >= stop:
                exit_price, reason = stop, "stop_loss"; break
            if lo <= target:
                exit_price, reason = target, "take_profit"; break
        else:
            if lo <= stop:
                exit_price, reason = stop, "stop_loss"; break
            if hi >= target:
                exit_price, reason = target, "take_profit"; break

    if exit_price is None:
        exit_price = float(future.iloc[-1]["close"])
        reason = "hold_window_elapsed"
    elif bars_held < MIN_BARS_TO_RESOLVE:
        # Resolved instantly — the level was already within reach when the
        # signal was written, so this measures level placement, not
        # prediction. Dropped rather than counted.
        return None

    side = -1 if is_short else 1
    gross = (exit_price - entry) * qty * side

    # Real costs, from the model corrected today.
    fees = 0.0
    try:
        from lib.paper_engine import venue_round_trip_fee
        f, _ = venue_round_trip_fee(signal.get("asset_symbol") or "", qty * entry, 1.0, entry)
        fees = float(f or 0)
    except Exception:
        pass
    net = gross - fees

    # In R, so a 15m scalp and a weekly position are comparable. Without a
    # risk distance there is no R, and None is the honest answer rather than
    # a zero that would train as "never moved".
    risk_distance = abs(entry - stop)
    if risk_distance > 0:
        mfe_r = round(max_favorable / risk_distance, 4)
        mae_r = round(max_adverse / risk_distance, 4)
    else:
        mfe_r = mae_r = None

    return {
        "signal_id": signal.get("id"),
        "symbol": signal.get("asset_symbol"),
        "asset_class": signal.get("asset_class"),
        "direction": signal.get("direction"),
        "timeframe": signal.get("timeframe"),
        "entry_price": entry,
        "exit_price": exit_price,
        "qty": qty,
        "pnl_usd": round(net, 6),
        "pnl_pct": round(net / margin * 100, 4) if margin else 0.0,
        "gross_usd": round(gross, 6),
        "fees": round(fees, 6),
        "outcome": "WIN" if net > 0 else "LOSS" if net < 0 else "BREAKEVEN",
        "exit_reason": reason,
        "bars_held": bars_held,
        "hold_window_max_min": hold_max,
        "source": SOURCE_REPLAY,
        # ── Path labels (Phase 1) ────────────────────────────────────────
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "mfe_bar": mfe_bar,
        "mae_bar": mae_bar,
        "first_touch": first_touch,
        "max_favorable_price": round(entry + (max_favorable * (-1 if is_short else 1)), 8),
        "max_adverse_price": round(entry + (max_adverse * (1 if is_short else -1)), 8),
        # Provenance travels WITH the label. Replay assumes perfect fills and
        # that both a bar's high and low were reachable, so these are
        # systematically optimistic and must never be pooled with live
        # observations as though they were equivalent.
        "path_source": PATH_SOURCE_REPLAY,
    }


def load_cached_bars(symbol: str, timeframe: str):
    """Bars from the local OHLCV cache, indexed by timestamp.

    A replay reconstructs the past; it must not reach for live data to do
    it. Reading the cache directly also keeps the whole job local, which is
    what makes replaying thousands of signals practical.
    """
    try:
        import sqlite3
        import pandas as pd
        from pathlib import Path
        path = Path(__file__).parent.parent / "data" / "ohlcv_cache.db"
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path))
        try:
            df = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM ohlcv_bars "
                "WHERE symbol=? AND timeframe=? ORDER BY ts",
                conn, params=(symbol, timeframe),
            )
        finally:
            conn.close()
        if df.empty:
            return None
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.dropna(subset=["ts"]).set_index("ts")
        return df
    except Exception as e:
        logger.debug(f"[Replay] cache read failed for {symbol} {timeframe}: {e}")
        return None


def replay_batch(limit: int = 2000, symbols: list | None = None,
                 min_bars: int = 5) -> dict:
    """Replay many signals, grouped by symbol so bars load once each.

    Returns a summary plus the outcomes, for the caller to persist.
    """
    from app.database import get_db, TradingSignal

    with get_db() as db:
        q = db.query(TradingSignal).filter(
            TradingSignal.entry_price > 0,
            TradingSignal.stop_loss > 0,
            TradingSignal.target_price > 0,
            TradingSignal.generated_at.isnot(None),
            TradingSignal.timeframe.isnot(None),
        )
        if symbols:
            q = q.filter(TradingSignal.asset_symbol.in_(symbols))
        rows = q.order_by(TradingSignal.generated_at.desc()).limit(limit).all()
        sigs = [{
            "id": s.id, "asset_symbol": s.asset_symbol, "asset_class": s.asset_class,
            "direction": s.direction, "timeframe": s.timeframe,
            "entry_price": s.entry_price, "target_price": s.target_price,
            "stop_loss": s.stop_loss, "generated_at": s.generated_at,
            "composite_score": s.composite_score, "confidence": s.confidence,
        } for s in rows]

    # Near-identical signals are the SAME setup regenerated each cycle, not
    # independent evidence. Counting all of them lets one repeated idea
    # dominate a bucket — 54 of the 55 1m outcomes were one XRP setup with
    # identical levels.
    seen, deduped = set(), []
    for s in sigs:
        key = (s["asset_symbol"], s["timeframe"], s["direction"],
               round(float(s["entry_price"] or 0), 8),
               round(float(s["target_price"] or 0), 8),
               round(float(s["stop_loss"] or 0), 8))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    duplicates = len(sigs) - len(deduped)
    sigs = deduped

    by_symbol_tf: dict = {}
    for s in sigs:
        by_symbol_tf.setdefault((s["asset_symbol"], s["timeframe"]), []).append(s)

    outcomes, skipped = [], 0
    for (sym, tf), group in by_symbol_tf.items():
        # Straight from the local cache. Going through fetch_multi_timeframe
        # would hit the network once per symbol/timeframe pair, which turned
        # a 300-signal replay into a multi-minute job — and a replay of
        # HISTORY has no business asking for fresh data anyway.
        bars = load_cached_bars(sym, tf)
        if bars is None or len(bars) < min_bars:
            skipped += len(group)
            continue
        for s in group:
            out = replay_signal(s, bars)
            if out:
                outcomes.append(out)
            else:
                skipped += 1

    wins = sum(1 for o in outcomes if o["pnl_pct"] > 0)
    return {
        "replayed": len(outcomes),
        "skipped": skipped,
        "duplicates_dropped": duplicates,
        "wins": wins,
        "win_rate": round(wins / len(outcomes) * 100, 1) if outcomes else None,
        "outcomes": outcomes,
    }


def persist(outcomes: list) -> dict:
    """Write replayed outcomes, tagged so they are never mistaken for live.

    Existing replay rows for the same signal are replaced rather than
    duplicated, so re-running the replay after a rule change updates the
    evidence instead of inflating it.
    """
    from app.database import get_db, TradeOutcome, new_id
    from lib.calibration import CURRENT_EPOCH
    written = 0
    with get_db() as db:
        ids = [o["signal_id"] for o in outcomes if o.get("signal_id")]
        if ids:
            db.query(TradeOutcome).filter(
                TradeOutcome.signal_id.in_(ids),
                TradeOutcome.outcome_source == SOURCE_REPLAY,
            ).delete(synchronize_session=False)
        for o in outcomes:
            db.add(TradeOutcome(
                id=new_id(), signal_id=o.get("signal_id"), symbol=o.get("symbol"),
                asset_class=o.get("asset_class"), direction=o.get("direction"),
                timeframe=o.get("timeframe"), entry_price=o.get("entry_price"),
                exit_price=o.get("exit_price"), qty=o.get("qty"),
                pnl_usd=o.get("pnl_usd"), pnl_pct=o.get("pnl_pct"),
                outcome=o.get("outcome"), exit_reason=o.get("exit_reason"),
                paper_mode=True, engine_epoch=CURRENT_EPOCH,
                outcome_source=SOURCE_REPLAY,
                mfe_r=o.get("mfe_r"), mae_r=o.get("mae_r"),
                mfe_bar=o.get("mfe_bar"), mae_bar=o.get("mae_bar"),
                first_touch=o.get("first_touch"),
                path_source=o.get("path_source"),
            ))
            written += 1
    return {"written": written}
