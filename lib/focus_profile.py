"""A persistent behavioural profile for each focus symbol.

The point of a focus list is that the desk gets to KNOW a handful of names
rather than glancing at hundreds. That knowledge has two halves, kept
strictly separate:

  MEASURED   realized volatility, typical daily range, how often it swings
             hard, volume behaviour, where its levels sit. Computed from
             bars — never guessed, never from the model.

  NARRATIVE  the LLM's written character sketch of how the symbol behaves,
             grounded in the measured block above and refreshed on a slow
             cadence. Useful precisely for names with no track record —
             a newly listed coin has no win-rate history, but it does have
             observable behaviour from its first day.

The measured half stands alone and is always shown; the narrative is
optional and clearly labelled as interpretation. A profile is not a
prediction: it describes what the symbol has DONE.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PROFILE_REFRESH_HOURS = 6
BIG_MOVE_PCT = 10.0          # a "hard swing" for the swing-frequency stat


def measure_behaviour(symbol: str) -> dict | None:
    """Deterministic behavioural statistics from real bars."""
    try:
        from lib.ohlcv import fetch_multi_timeframe
        bars = fetch_multi_timeframe(symbol, ["1H", "1D"])
    except Exception as e:
        logger.debug(f"[Focus] Bar fetch failed for {symbol}: {e}")
        return None

    daily = bars.get("1D")
    hourly = bars.get("1H")
    if daily is None or getattr(daily, "empty", True) or len(daily) < 5:
        return None

    closes = [float(c) for c in daily["close"].tolist() if c and c > 0]
    highs = [float(h) for h in daily["high"].tolist() if h and h > 0]
    lows = [float(l) for l in daily["low"].tolist() if l and l > 0]
    if len(closes) < 5:
        return None

    daily_returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(1, len(closes)) if closes[i - 1]
    ]
    ranges = [
        (highs[i] - lows[i]) / lows[i] * 100
        for i in range(len(highs)) if i < len(lows) and lows[i]
    ]
    big_moves = [r for r in daily_returns if abs(r) >= BIG_MOVE_PCT]

    # Intraday swing: how far it travels within a session relative to close.
    intraday_swing = None
    if hourly is not None and not getattr(hourly, "empty", True) and len(hourly) >= 24:
        h = [float(x) for x in hourly["high"].tolist()[-24:] if x]
        l = [float(x) for x in hourly["low"].tolist()[-24:] if x]
        if h and l and min(l) > 0:
            intraday_swing = (max(h) - min(l)) / min(l) * 100

    vols = [float(v) for v in daily.get("volume", []).tolist() if v] if "volume" in daily else []
    volume_trend = None
    if len(vols) >= 6:
        recent = statistics.fmean(vols[-3:])
        earlier = statistics.fmean(vols[-6:-3])
        if earlier:
            volume_trend = (recent - earlier) / earlier * 100

    return {
        "symbol": symbol,
        "sessions_observed": len(closes),
        "daily_move_avg_pct": round(statistics.fmean(abs(r) for r in daily_returns), 2) if daily_returns else None,
        "daily_move_max_pct": round(max(daily_returns, key=abs), 2) if daily_returns else None,
        "daily_volatility_pct": round(statistics.pstdev(daily_returns), 2) if len(daily_returns) > 1 else None,
        "daily_range_avg_pct": round(statistics.fmean(ranges), 2) if ranges else None,
        "big_move_days": len(big_moves),
        "big_move_frequency_pct": round(len(big_moves) / len(daily_returns) * 100, 1) if daily_returns else None,
        "last_24h_swing_pct": round(intraday_swing, 2) if intraday_swing is not None else None,
        "volume_trend_pct": round(volume_trend, 1) if volume_trend is not None else None,
        "price_now": closes[-1],
        "range_low": round(min(lows), 8) if lows else None,
        "range_high": round(max(highs), 8) if highs else None,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


def behaviour_summary(stats: dict) -> str:
    """One honest line of plain English from the measured numbers."""
    if not stats:
        return "no measurable history yet"
    bits = []
    if stats.get("daily_move_avg_pct") is not None:
        bits.append(f"moves {stats['daily_move_avg_pct']:.1f}%/day on average")
    if stats.get("daily_volatility_pct") is not None:
        bits.append(f"volatility {stats['daily_volatility_pct']:.1f}%")
    if stats.get("big_move_frequency_pct"):
        bits.append(f"{stats['big_move_frequency_pct']:.0f}% of days swing >={BIG_MOVE_PCT:.0f}%")
    if stats.get("last_24h_swing_pct") is not None:
        bits.append(f"{stats['last_24h_swing_pct']:.1f}% range last 24h")
    if stats.get("volume_trend_pct") is not None:
        direction = "rising" if stats["volume_trend_pct"] > 0 else "falling"
        bits.append(f"volume {direction} {abs(stats['volume_trend_pct']):.0f}%")
    return f"{stats['sessions_observed']} sessions observed: " + ", ".join(bits) if bits else "insufficient data"


def build_narrative(symbol: str, stats: dict, ta_block: str | None) -> str | None:
    """LLM character sketch, grounded in the measured stats and TA.

    Explicitly asks for BEHAVIOUR, not a forecast — the profile exists to
    describe how the symbol trades, which stays useful across setups.
    """
    if not stats:
        return None
    try:
        from lib.lmstudio import call_lm_studio
        prompt = (
            f"Write a short behavioural profile of how {symbol} TRADES. This is not a "
            f"forecast and must not contain a price prediction or a trade recommendation.\n\n"
            f"MEASURED BEHAVIOUR (from real bars, trust these):\n{json.dumps(stats, indent=1)}\n\n"
            + (f"CURRENT TECHNICALS:\n{ta_block[:2000]}\n\n" if ta_block else "")
            + "In 3-4 sentences describe: how violently it moves, whether swings tend to "
              "follow through or revert, what a sensible stop distance looks like given "
              "that volatility, and what a trader should be careful of with this symbol. "
              "Be concrete and use the numbers above."
        )
        text = call_lm_studio(
            prompt,
            system="You are a trading desk analyst describing instrument behaviour, not making predictions.",
            max_tokens=320, timeout=90,
        )
        return (text or "").strip() or None
    except Exception as e:
        logger.debug(f"[Focus] Narrative generation failed for {symbol}: {e}")
        return None


def profile_is_stale(updated_at: str | None) -> bool:
    if not updated_at:
        return True
    try:
        then = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - then > timedelta(hours=PROFILE_REFRESH_HOURS)
    except ValueError:
        return True


def get_or_build(symbol: str, force: bool = False) -> dict | None:
    """Cached profile for a focus symbol, rebuilt when stale."""
    from app.database import get_db, FocusProfile, now_iso

    with get_db() as db:
        row = db.query(FocusProfile).filter(FocusProfile.symbol == symbol).first()
        if row and not force and not profile_is_stale(row.updated_at):
            return {
                "symbol": row.symbol,
                "stats": json.loads(row.stats_json or "{}"),
                "summary": row.summary,
                "narrative": row.narrative,
                "updated_at": row.updated_at,
                "fresh": True,
            }

    stats = measure_behaviour(symbol)
    if not stats:
        return None
    summary = behaviour_summary(stats)

    ta_block = None
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import analyze_symbol, build_ta_prompt_block
        ta_block = build_ta_prompt_block(symbol, analyze_symbol(fetch_multi_timeframe(symbol, ["1H", "4H", "1D"])))
    except Exception:
        pass
    narrative = build_narrative(symbol, stats, ta_block)

    from app.database import get_db as _db, FocusProfile as _FP, now_iso as _now
    with _db() as db:
        row = db.query(_FP).filter(_FP.symbol == symbol).first()
        if not row:
            row = _FP(symbol=symbol)
            db.add(row)
        row.stats_json = json.dumps(stats)
        row.summary = summary
        row.narrative = narrative
        row.updated_at = _now()
    return {"symbol": symbol, "stats": stats, "summary": summary,
            "narrative": narrative, "updated_at": now_iso(), "fresh": False}


def profile_prompt_block(symbol: str) -> str | None:
    """The profile as prompt context for signal generation."""
    p = get_or_build(symbol)
    if not p:
        return None
    lines = [f"FOCUS PROFILE — {symbol}", f"  measured: {p['summary']}"]
    if p.get("narrative"):
        lines.append(f"  behaviour (interpretation): {p['narrative']}")
    return "\n".join(lines)
