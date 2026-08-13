"""Is this asset actually leading, or just floating on the tide?

This is NOT RSI. RSI asks whether a chart is stretched against its own
recent history. Relative strength asks whether it is outperforming the
thing that actually drives it.

The distinction matters because most crypto moves are BTC moves and most
equity moves are index moves. A SOL breakout during a BTC rally may be no
information at all — SOL went up because everything went up. The same
breakout while BTC is flat is a much stronger claim.

Without this, the system buys beta and calls it a setup.

BENCHMARKS are chosen per asset class, not globally. Comparing crude oil to
SPY is not relative strength, it is noise with a ratio sign.

MISSING IS NOT ZERO. No benchmark data means no measurement — never a
relative strength of 0, which would read as "exactly in line".
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Benchmark per asset class. The primary is what the asset actually trades
# against; the secondary adds context where it is genuinely different.
BENCHMARKS = {
    "crypto": ["BTC/USD", "ETH/USD"],
    "equity": ["SPY", "QQQ"],
    "etf": ["SPY"],
    "futures": [],      # a complex-relative benchmark, not an index — see below
    "forex": [],        # currency strength is a basket, handled separately
}

# Lookbacks in BARS of whatever timeframe is passed in. Short says "right
# now", long says "over the move".
LOOKBACKS = (1, 5, 20)

# The longest lookback plus one, which is the actual requirement — an
# arbitrary margin above it silently excluded whole asset classes. Equity
# 1H history is thin (NVDA had 24 bars, SPY 26), so a floor of 25 reported
# "no benchmark data" for every stock while the data was in fact present.
MIN_BARS = max(LOOKBACKS) + 1


def _asset_class(symbol: str, declared: str | None = None) -> str:
    if declared:
        d = declared.lower()
        for key in BENCHMARKS:
            if key in d:
                return key
    s = (symbol or "").upper()
    if "/" in s or s.endswith("USD") and len(s) > 5:
        return "crypto"
    if "=" in s:
        return "futures"
    return "equity"


def benchmarks_for(symbol: str, asset_class: str | None = None) -> list:
    """Which benchmarks this symbol should be judged against.

    A symbol is never compared to itself: BTC has no meaningful relative
    strength against BTC, and returning 1.0 would read as "perfectly in
    line" rather than "not applicable".
    """
    cls = _asset_class(symbol, asset_class)
    return [b for b in BENCHMARKS.get(cls, []) if b.upper() != (symbol or "").upper()]


def _closes(bars) -> list | None:
    try:
        s = bars["close"].dropna()
        return [float(x) for x in s] if len(s) >= MIN_BARS else None
    except Exception:
        return None


def compute_relative_strength(symbol_bars, benchmark_bars,
                              benchmark_name: str) -> dict | None:
    """Ratio-based relative strength over several lookbacks.

    The ratio series (asset / benchmark) rising means the asset is
    outperforming, whatever both did in absolute terms — which is the
    entire point. Both can be falling and the ratio still improving.
    """
    a = _closes(symbol_bars)
    b = _closes(benchmark_bars)
    if not a or not b:
        # Which side was short matters for diagnosis: a missing benchmark
        # is an infrastructure problem, a short asset history is not.
        return None

    n = min(len(a), len(b))
    if n < MIN_BARS:
        return None
    a, b = a[-n:], b[-n:]
    ratio = [x / y for x, y in zip(a, b) if y]
    if len(ratio) < MIN_BARS:
        return None

    out: dict = {"benchmark": benchmark_name, "bars": len(ratio)}
    cur = ratio[-1]
    for k in LOOKBACKS:
        if len(ratio) > k and ratio[-1 - k]:
            out[f"rs_{k}"] = round((cur / ratio[-1 - k] - 1) * 100, 3)
        else:
            out[f"rs_{k}"] = None

    # Slope of the ratio over the long lookback, per bar, in percent. This
    # is the "is it improving" question, separate from "is it high".
    window = ratio[-min(len(ratio), 20):]
    if len(window) >= 5 and window[0]:
        out["rs_slope"] = round((window[-1] / window[0] - 1) / len(window) * 100, 4)
    else:
        out["rs_slope"] = None

    # Is the ratio itself breaking out? An asset making new highs AGAINST
    # its benchmark is a much stronger claim than one making new highs
    # alongside it.
    hist = ratio[-min(len(ratio), 60):]
    if len(hist) >= MIN_BARS:
        out["rs_breakout"] = cur >= max(hist[:-1])
        out["rs_breakdown"] = cur <= min(hist[:-1])
    else:
        out["rs_breakout"] = out["rs_breakdown"] = None

    short, long_ = out.get("rs_1"), out.get("rs_20")
    if short is not None and long_ is not None:
        out["leading"] = short > 0 and long_ > 0
        out["lagging"] = short < 0 and long_ < 0
        out["state"] = ("LEADING" if out["leading"]
                        else "LAGGING" if out["lagging"] else "MIXED")
    else:
        out["state"] = None
    return out


def relative_strength_for(symbol: str, timeframe: str = "1H",
                          asset_class: str | None = None,
                          symbol_bars=None) -> dict:
    """Relative strength against every benchmark that applies.

    Fetches benchmark bars on the same timeframe so the comparison is
    like-for-like — comparing a 1H asset series to a 1D benchmark would
    produce a ratio that means nothing.
    """
    marks = benchmarks_for(symbol, asset_class)
    if not marks:
        return {"available": False,
                "reason": f"no benchmark defined for {symbol}",
                "vs": {}}

    from lib.ohlcv import fetch_multi_timeframe
    if symbol_bars is None:
        try:
            symbol_bars = (fetch_multi_timeframe(symbol, [timeframe]) or {}).get(timeframe)
        except Exception as e:
            logger.debug(f"[RS] no bars for {symbol}: {e}")
            return {"available": False, "reason": "no bars for symbol", "vs": {}}
    if symbol_bars is None:
        return {"available": False, "reason": "no bars for symbol", "vs": {}}

    vs = {}
    for mark in marks:
        try:
            mb = (fetch_multi_timeframe(mark, [timeframe]) or {}).get(timeframe)
            rs = compute_relative_strength(symbol_bars, mb, mark) if mb is not None else None
            if rs:
                vs[mark] = rs
        except Exception as e:
            logger.debug(f"[RS] {symbol} vs {mark} failed: {e}")

    if not vs:
        # Say what was actually wrong. "No benchmark data" sent me looking
        # for a broken feed when the real answer was 24 bars against a
        # 25-bar floor.
        try:
            have = len(_closes(symbol_bars) or [])
        except Exception:
            have = 0
        reason = (f"{symbol} has {have} usable bars on {timeframe}, "
                  f"needs {MIN_BARS}" if have < MIN_BARS
                  else f"no usable {timeframe} history for {', '.join(marks)}")
        return {"available": False, "reason": reason, "vs": {}}

    primary = vs.get(marks[0]) or next(iter(vs.values()))
    return {
        "available": True,
        "primary_benchmark": primary.get("benchmark"),
        "state": primary.get("state"),
        "rs_1": primary.get("rs_1"),
        "rs_5": primary.get("rs_5"),
        "rs_20": primary.get("rs_20"),
        "rs_slope": primary.get("rs_slope"),
        "rs_breakout": primary.get("rs_breakout"),
        "vs": vs,
    }
