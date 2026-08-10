"""
lib/backtester.py — deterministic, no-LLM historical backtesting engine.

Reuses the exact same signal-construction/scoring/evaluation pipeline the live
bot uses, just fed historical bar slices instead of live ones:

  lib.ta_engine.analyze_symbol                    -> per-timeframe TA
  jobs.generate_signals.build_ta_fallback_signals  -> deterministic candidate signal (no LLM)
  lib.signal_scorer.score_signal                   -> composite_score gate
  lib.signal_evaluation.evaluate_signal_path        -> forward-only target/stop resolution
  lib.performance_analytics.compute_max_drawdown/compute_sharpe_ratio -> equity-curve stats

No LLM calls anywhere in this module. The only network calls happen through
lib.ohlcv_cache.backfill_symbol()'s existing yfinance path, which run_backtest()
uses to warm the cache before walking it — the walk-forward logic itself
(evaluate_checkpoint / walk_symbol) never performs I/O, which is what makes it
unit-testable with synthetic in-memory DataFrames (see tests/test_backtester.py).

── No-lookahead-bias guarantees ──────────────────────────────────────────────
1. walk_symbol() slices each timeframe's DataFrame to `index <= checkpoint`
   BEFORE calling analyze_symbol() (see the `sliced = frame[frame.index <= checkpoint]`
   line below) — analyze_symbol() never sees a bar timestamped after the
   checkpoint it is being asked to score.
2. evaluate_signal_path() (lib/signal_evaluation.py) independently filters to
   `index > generated_at` internally, so even though it is handed the FULL
   historical frame for resolution, it cannot resolve using bars at or before
   the signal's generation time.
3. _rebase_bar_age() recomputes bar_age_seconds relative to the *checkpoint*
   time rather than wall-clock time (see docstring below) — this does not
   affect lookahead safety, it only prevents an unrelated wall-clock artifact
   from crippling every historical composite_score (see design note there).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from lib.ohlcv_cache import backfill_symbol, get_cached_range, TF_CONFIG
from lib.ta_engine import analyze_symbol
from lib.signal_scorer import score_signal
from lib.signal_evaluation import evaluate_signal_path, summarize_evaluations
from lib.performance_analytics import compute_max_drawdown, compute_sharpe_ratio

logger = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES = ["4H", "1D"]
MIN_BARS_FOR_TA = 20          # analyze_symbol needs ~20+ rows per tf for a non-error result
RESOLVED_OUTCOMES = {"TARGET_HIT", "STOP_HIT"}


# ── small parsing helpers ──────────────────────────────────────────────────

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _finer_and_coarser(timeframes: list[str]) -> tuple[str, str]:
    """Pick the finer (smaller hist_days) and coarser (larger hist_days)
    timeframe from a list, using TF_CONFIG as the ordering — this is what
    determines the checkpoint cadence (coarser) and the resolution/TA-detail
    frame (finer)."""
    ranked = sorted(timeframes, key=lambda tf: TF_CONFIG.get(tf, TF_CONFIG["1D"])["hist_days"])
    return ranked[0], ranked[-1]


def _rebase_bar_age(ta_profile: dict, checkpoint: datetime) -> dict:
    """Recompute bar_age_seconds relative to the backtest checkpoint instead
    of wall-clock time.

    lib.ta_engine.compute_timeframe() stamps bar_age_seconds using
    `pd.Timestamp.now(tz="UTC")` (real wall-clock "now") because it was only
    ever written for live use. In a backtest, the "last bar" is historical
    (weeks/months before the real now), so every single checkpoint would read
    as maximally stale (bar_age_seconds in the millions), which drives
    lib.signal_scorer.score_signal()'s freshness score to 0 and applies its
    -15 stale_penalty on literally every candidate — a wall-clock artifact
    unrelated to how "fresh" the bar actually was relative to when the
    hypothetical trade was opened. We deliberately do NOT touch
    lib.ta_engine.py (that would change live behavior); instead we patch the
    already-computed bar_age_seconds here, in the backtester only, to be
    relative to the checkpoint — i.e. "how old was the last bar when this
    signal was generated," which is what freshness scoring is actually meant
    to measure.
    """
    for tf, data in (ta_profile or {}).items():
        if not data or data.get("error"):
            continue
        bar_time = _parse_iso(data.get("bar_time"))
        if bar_time is None:
            continue
        age = (checkpoint - bar_time).total_seconds()
        data["bar_age_seconds"] = max(0, round(age))
    return ta_profile


# ── pure, network-free checkpoint decision logic (unit-testable) ───────────

def evaluate_checkpoint(
    symbol: str,
    bars_by_tf: dict[str, pd.DataFrame],
    checkpoint: datetime,
    trade_mode: str = "longer",
    min_composite_score: float = 55.0,
    regime: Optional[dict] = None,
    news_confidence: float = 50.0,
) -> Optional[dict]:
    """Given bar slices that ALREADY stop at `checkpoint` (caller's
    responsibility — see walk_symbol below), decide whether the deterministic
    TA-fallback pipeline would open a hypothetical trade here. Returns a
    scored signal dict or None. Pure function: no I/O, no network, no LLM."""
    from jobs.generate_signals import build_ta_fallback_signals  # lazy: avoids import cycles

    checkpoint = _as_utc(checkpoint)
    valid_frames = {tf: df for tf, df in (bars_by_tf or {}).items() if df is not None and len(df) >= MIN_BARS_FOR_TA}
    if len(valid_frames) < 2:
        return None

    ta_profile = analyze_symbol(valid_frames)
    ta_profile = _rebase_bar_age(ta_profile, checkpoint)

    valid_count = sum(1 for data in ta_profile.values() if data and not data.get("error"))
    if valid_count < 2:
        return None

    last_close = None
    for df in valid_frames.values():
        try:
            last_close = float(df["close"].iloc[-1])
            break
        except Exception:
            continue
    asset_map = {symbol: {"price": last_close or 0.0}}

    candidates = build_ta_fallback_signals(
        [symbol], {symbol: ta_profile}, asset_map, trade_mode=trade_mode,
        reason="Backtest: deterministic signal (no LLM used in backtesting)",
    )
    if not candidates:
        return None
    signal = candidates[0]
    signal["generated_at"] = checkpoint.isoformat()

    scored = score_signal(
        signal, ta_profile, regime or {"risk": "medium"},
        news_confidence=news_confidence,
    )
    if float(scored.get("composite_score") or 0) < min_composite_score:
        return None
    return scored


def walk_symbol(
    symbol: str,
    frames: dict[str, pd.DataFrame],
    checkpoints: list,
    checkpoint_tf: str,
    finer_tf: str,
    trade_mode: str = "longer",
    min_composite_score: float = 55.0,
    regime: Optional[dict] = None,
    news_confidence: float = 50.0,
) -> list[dict]:
    """Walk `checkpoints` (an ascending list of Timestamps, normally the
    coarser timeframe's bar index restricted to the requested date range) for
    one symbol, opening at most one hypothetical trade at a time.

    `frames` must be a dict of {timeframe: full historical DataFrame} already
    loaded by the caller — this function performs NO cache/network I/O, only
    slicing already-in-memory DataFrames, which is what makes it directly
    unit-testable with synthetic data (see tests/test_backtester.py).

    No-lookahead guard: at every checkpoint, each timeframe's frame is sliced
    to `index <= checkpoint` before being handed to evaluate_checkpoint() /
    analyze_symbol() — bars after the checkpoint are never visible to the
    TA/signal-construction step.
    """
    timeframes = list(frames.keys())
    trades: list[dict] = []
    open_until: Optional[datetime] = None

    for cp in checkpoints:
        cp_dt = _as_utc(pd.Timestamp(cp).to_pydatetime())
        if open_until is not None and cp_dt <= open_until:
            continue

        bars_by_tf: dict[str, pd.DataFrame] = {}
        insufficient = False
        for tf in timeframes:
            frame = frames.get(tf)
            if frame is None or frame.empty:
                insufficient = True
                break
            sliced = frame[frame.index <= pd.Timestamp(cp_dt)]
            if len(sliced) < MIN_BARS_FOR_TA:
                insufficient = True
                break
            bars_by_tf[tf] = sliced
        if insufficient:
            continue

        candidate = evaluate_checkpoint(
            symbol, bars_by_tf, cp_dt,
            trade_mode=trade_mode, min_composite_score=min_composite_score,
            regime=regime, news_confidence=news_confidence,
        )
        if candidate is None:
            continue

        finer_full = frames.get(finer_tf)
        outcome = evaluate_signal_path(candidate, finer_full)
        if outcome is None:
            continue

        resolved_at_str = outcome.get("target_hit_at") or outcome.get("stop_hit_at") or outcome.get("last_bar_at")
        resolved_at = _parse_iso(resolved_at_str) or cp_dt

        trade = {
            "symbol": symbol,
            "generated_at": candidate.get("generated_at"),
            "direction": candidate.get("direction"),
            "timeframe": candidate.get("timeframe"),
            "entry_price": candidate.get("entry_price"),
            "target_price": candidate.get("target_price"),
            "stop_loss": candidate.get("stop_loss"),
            "composite_score": candidate.get("composite_score"),
            "confidence": candidate.get("confidence"),
            "resolution_date": resolved_at_str or resolved_at.isoformat(),
            **outcome,
        }
        trades.append(trade)
        open_until = resolved_at

    return trades


# ── pure equity-curve math (unit-testable independent of data walking) ─────

def compute_equity_curve(
    trades: list[dict],
    starting_equity: float = 10000.0,
    risk_pct_per_trade: float = 1.0,
) -> tuple[list[tuple[str, float]], float]:
    """R-multiple compounding equity curve from resolved trades.

    Only TARGET_HIT / STOP_HIT trades affect equity (AMBIGUOUS/INVALID_DATA/
    OPEN/EXPIRED are skipped). For each, in chronological order by
    resolution_date:
        stop_distance_pct = abs(entry - stop) / entry * 100
        realized_pct = mfe_pct if TARGET_HIT else mae_pct
        R = realized_pct / stop_distance_pct
        equity *= (1 + risk_pct_per_trade/100 * R)
    One point per resolved trade, keyed by resolution date (day granularity);
    if multiple trades resolve on the same day, the last one wins (matches
    the "last snapshot of the day" convention used by
    lib.performance_analytics.daily_equity_curve).
    Returns (curve, final_equity).
    """
    equity = float(starting_equity)
    decided = [t for t in trades if t.get("outcome") in RESOLVED_OUTCOMES]
    decided.sort(key=lambda t: t.get("resolution_date") or "")

    curve_by_date: dict[str, float] = {}
    for t in decided:
        entry = float(t.get("entry_price") or 0)
        stop = float(t.get("stop_loss") or 0)
        if entry <= 0:
            continue
        stop_distance_pct = abs(entry - stop) / entry * 100
        if stop_distance_pct < 1e-9:
            continue
        realized_pct = t.get("mfe_pct") if t.get("outcome") == "TARGET_HIT" else t.get("mae_pct")
        realized_pct = float(realized_pct or 0)
        r_multiple = realized_pct / stop_distance_pct
        equity *= (1 + (risk_pct_per_trade / 100) * r_multiple)
        date_key = str(t.get("resolution_date") or "")[:10]
        if date_key:
            curve_by_date[date_key] = equity

    curve = sorted(curve_by_date.items())
    return curve, equity


# ── orchestration: backfill, load cache, walk every symbol ─────────────────

def run_backtest(
    symbols: list[str],
    start_date: str,
    end_date: str,
    timeframes: Optional[list[str]] = None,
    trade_mode: str = "longer",
    starting_equity: float = 10000.0,
    risk_pct_per_trade: float = 1.0,
    min_composite_score: float = 55.0,
) -> dict:
    """Run a historical, no-LLM backtest of the deterministic TA-fallback
    signal pipeline over `symbols` between start_date/end_date (YYYY-MM-DD or
    ISO strings). See module docstring for the lookahead-bias guarantees."""
    timeframes = list(timeframes) if timeframes else list(DEFAULT_TIMEFRAMES)
    if len(timeframes) < 2:
        raise ValueError("run_backtest requires at least 2 timeframes (build_ta_fallback_signals needs 2+ agreeing timeframes)")

    finer_tf, checkpoint_tf = _finer_and_coarser(timeframes)
    if "1D" in timeframes:
        checkpoint_tf = "1D"  # spec: walk one day at a time when daily bars are available

    requested_start = _parse_iso(start_date) or _as_utc(datetime.fromisoformat(start_date))
    requested_end = _parse_iso(end_date) or _as_utc(datetime.fromisoformat(end_date))
    requested_start, requested_end = _as_utc(requested_start), _as_utc(requested_end)

    now = datetime.now(timezone.utc)
    end_dt = min(requested_end, now)

    max_days = min(TF_CONFIG.get(tf, TF_CONFIG["1D"])["hist_days"] for tf in timeframes)
    earliest_allowed = end_dt - timedelta(days=max_days)

    date_range_clamped = False
    start_dt = requested_start
    if start_dt < earliest_allowed:
        start_dt = earliest_allowed
        date_range_clamped = True
    if end_dt != requested_end:
        date_range_clamped = True

    symbols_skipped: list[dict] = []
    trades_by_symbol: dict[str, list[dict]] = {}
    all_trades: list[dict] = []

    for symbol in symbols:
        try:
            for tf in timeframes:
                backfill_symbol(symbol, tf, days=max_days)
        except Exception as e:
            logger.warning(f"[Backtester] Backfill failed for {symbol}: {e}")
            symbols_skipped.append({"symbol": symbol, "reason": f"backfill_failed: {e}"})
            continue

        frames: dict[str, pd.DataFrame] = {}
        load_failed = False
        for tf in timeframes:
            cfg = TF_CONFIG.get(tf, TF_CONFIG["1D"])
            load_start = now - timedelta(days=cfg["hist_days"])
            try:
                df = get_cached_range(symbol, tf, load_start, end_dt)
            except Exception as e:
                logger.warning(f"[Backtester] get_cached_range failed for {symbol}/{tf}: {e}")
                df = None
            if df is None or df.empty:
                load_failed = True
                break
            frames[tf] = df
        if load_failed:
            symbols_skipped.append({"symbol": symbol, "reason": "no_cached_data"})
            continue

        cp_frame = frames.get(checkpoint_tf)
        checkpoints = list(cp_frame[(cp_frame.index >= pd.Timestamp(start_dt)) & (cp_frame.index <= pd.Timestamp(end_dt))].index)
        if not checkpoints:
            symbols_skipped.append({"symbol": symbol, "reason": "no_checkpoints_in_range"})
            continue

        try:
            symbol_trades = walk_symbol(
                symbol, frames, checkpoints, checkpoint_tf, finer_tf,
                trade_mode=trade_mode, min_composite_score=min_composite_score,
            )
        except Exception as e:
            logger.warning(f"[Backtester] Walk failed for {symbol}: {e}")
            symbols_skipped.append({"symbol": symbol, "reason": f"walk_failed: {e}"})
            continue

        trades_by_symbol[symbol] = symbol_trades
        all_trades.extend(symbol_trades)

    all_trades.sort(key=lambda t: t.get("resolution_date") or t.get("generated_at") or "")
    equity_curve, final_equity = compute_equity_curve(all_trades, starting_equity, risk_pct_per_trade)

    decided = [t for t in all_trades if t.get("outcome") in RESOLVED_OUTCOMES]
    wins = sum(1 for t in decided if t.get("outcome") == "TARGET_HIT")
    total_return_pct = (final_equity - starting_equity) / starting_equity * 100 if starting_equity else 0.0

    return {
        "symbols": symbols,
        "symbols_skipped": symbols_skipped,
        "timeframes": timeframes,
        "checkpoint_timeframe": checkpoint_tf,
        "finer_timeframe": finer_tf,
        "trade_mode": trade_mode,
        "min_composite_score": min_composite_score,
        "requested_start_date": requested_start.date().isoformat(),
        "requested_end_date": requested_end.date().isoformat(),
        "actual_start_date": start_dt.date().isoformat(),
        "actual_end_date": end_dt.date().isoformat(),
        "date_range_clamped": date_range_clamped,
        "trades_by_symbol": trades_by_symbol,
        "all_trades": all_trades,
        "total_signals": len(all_trades),
        "decided": len(decided),
        "wins": wins,
        "losses": len(decided) - wins,
        "win_rate_pct": round(wins / len(decided) * 100, 2) if decided else 0.0,
        "starting_equity": starting_equity,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 3),
        "equity_curve": equity_curve,
        "max_drawdown": compute_max_drawdown(equity_curve),
        "sharpe_ratio": compute_sharpe_ratio(equity_curve),
        "summary": summarize_evaluations(all_trades),
    }
