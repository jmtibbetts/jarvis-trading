"""Regime on four independent axes, measured against the right benchmark.

Two problems with the single label.

**It was one word for four different questions.** "Risk-On Bull" bundles
direction, volatility, liquidity and positioning into one string, so a
market that is rising *and* getting more violent reads the same as one
rising calmly. Those call for different position sizes and different
strategies, and the one label cannot express the difference. Worse, it was
consumed as a scalar `regime_score` in the composite, so all four
dimensions collapsed into one number before anything could reason about
them.

**It was SPY for everything.** `get_regime()` reads SPY's EMAs, RSI and
ADX, and that verdict was applied to BTC, to SOL, to gold futures. SPY
trending up says close to nothing about whether a crypto breakout is
likely to follow through; on a weekend it says nothing at all, because the
equity market is shut while crypto keeps trading. A regime input that is
stale or irrelevant is worse than none, because it is scored with the same
confidence as a real one.

Each axis is measured separately, from inputs belonging to the asset's own
market, and reports its own confidence. Axes with no data abstain rather
than defaulting to neutral — "we could not measure liquidity" and
"liquidity is normal" are different statements and only one of them should
affect a trade.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

AXES = ("trend", "volatility", "liquidity", "flow")

# What each asset class is actually driven by. The point of this table is
# that no row contains SPY unless SPY is genuinely the relevant benchmark.
BENCHMARKS = {
    "crypto": {"primary": "BTC/USD", "secondary": "ETH/USD"},
    "equity": {"primary": "SPY", "secondary": "QQQ"},
    "futures": {"primary": "SPY", "secondary": "GC=F"},
    "forex": {"primary": "UUP", "secondary": None},
}
DEFAULT_CLASS = "equity"

_CACHE: dict = {}
_CACHE_TTL = 300.0


def _f(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def asset_class_of(symbol: str, declared: str | None = None) -> str:
    if declared and str(declared).lower() in BENCHMARKS:
        return str(declared).lower()
    s = str(symbol or "")
    if "/" in s:
        return "crypto"
    if s.endswith("=F"):
        return "futures"
    if s.endswith("=X"):
        return "forex"
    return DEFAULT_CLASS


class Axis(dict):
    """One measured dimension. `state` is the reading, `confidence` is how
    much the reading is worth, and `abstained` says plainly that nothing was
    measured — which is not the same as a neutral reading."""


def _axis(name, state, score, confidence, detail, inputs=None) -> Axis:
    return Axis({
        "axis": name, "state": state, "score": round(float(score), 3),
        "confidence": round(float(confidence), 3), "detail": detail,
        "inputs": inputs or [], "abstained": False,
    })


def _abstain(name, why) -> Axis:
    return Axis({"axis": name, "state": "unknown", "score": 0.0,
                 "confidence": 0.0, "detail": why, "inputs": [],
                 "abstained": True})


# ── The axes ─────────────────────────────────────────────────────────────────

def _trend_axis(bench: dict) -> Axis:
    """Direction and conviction of the asset's OWN benchmark."""
    primary = bench.get("primary") or {}
    if not primary:
        return _abstain("trend", "no benchmark data")
    last = _f(primary.get("close"))
    e21, e50 = _f(primary.get("ema21")), _f(primary.get("ema50"))
    adx = _f(primary.get("adx"))
    if last is None or e21 is None or e50 is None:
        return _abstain("trend", "benchmark missing moving averages")

    if last > e21 > e50:
        state, score = "uptrend", 1.0
    elif last < e21 < e50:
        state, score = "downtrend", -1.0
    elif last > e50:
        state, score = "choppy_up", 0.35
    elif last < e50:
        state, score = "choppy_down", -0.35
    else:
        state, score = "flat", 0.0

    # ADX decides how much the direction is worth. A trend read off ADX 12
    # is a trend in name only, and the old single label had no way to say so.
    conf = 0.5
    if adx is not None:
        conf = 0.35 if adx < 20 else 0.7 if adx < 30 else 0.95
    secondary = bench.get("secondary") or {}
    inputs = [primary.get("symbol")]
    if secondary:
        s_last, s_e50 = _f(secondary.get("close")), _f(secondary.get("ema50"))
        if s_last is not None and s_e50 is not None:
            agrees = (s_last > s_e50) == (score > 0)
            # Two benchmarks disagreeing is a real fact about the market,
            # not noise to be averaged out.
            conf = conf * (1.0 if agrees else 0.6)
            inputs.append(secondary.get("symbol"))
    return _axis("trend", state, score, conf,
                 f"{primary.get('symbol')} {state}"
                 + (f", ADX {adx:.0f}" if adx is not None else ""), inputs)


def _volatility_axis(bench: dict) -> Axis:
    """How violent conditions are, as a percentile of the benchmark's own
    recent ATR — self-referential on purpose, since 2% daily range is calm
    for an alt and extreme for a utility."""
    primary = bench.get("primary") or {}
    # atr_profile reports the percentile on a 0-100 scale (SPY read 62.4,
    # BTC 0.4). Treating it as a 0-1 fraction made every symbol above the
    # 1st percentile read "expanding", and printed "the 6240% percentile".
    pct = _f(primary.get("atr_percentile"))
    atr_pct = _f(primary.get("atr_pct"))
    if pct is None:
        return _abstain("volatility", "no ATR history for the benchmark")
    if pct >= 85:
        state, score = "expanding", 1.0
    elif pct >= 60:
        state, score = "elevated", 0.5
    elif pct <= 15:
        state, score = "compressed", -1.0
    elif pct <= 40:
        state, score = "quiet", -0.5
    else:
        state, score = "normal", 0.0
    return _axis("volatility", state, score, 0.8,
                 f"ATR at the {pct:.0f}th percentile of its own recent range"
                 + (f" ({atr_pct:.2f}% of price)" if atr_pct is not None else ""),
                 [primary.get("symbol")])


def _liquidity_axis(bench: dict) -> Axis:
    """Whether there is enough participation for levels to mean anything."""
    primary = bench.get("primary") or {}
    ratio = _f(primary.get("volume_ratio"))
    if ratio is None:
        return _abstain("liquidity", "no volume baseline")
    if ratio >= 1.5:
        state, score = "heavy", 1.0
    elif ratio >= 0.9:
        state, score = "normal", 0.0
    elif ratio >= 0.6:
        state, score = "thin", -0.5
    else:
        state, score = "very_thin", -1.0
    return _axis("liquidity", state, score, 0.7,
                 f"volume {ratio:.2f}x its 20-bar average", [primary.get("symbol")])


def _flow_axis(asset_class: str, derivatives: dict | None) -> Axis:
    """Positioning. Only crypto has a free, honest read on this — funding
    and open interest — so everything else abstains rather than inventing
    one from price, which would just restate the trend axis."""
    if asset_class != "crypto":
        return _abstain("flow", "no free positioning data for this asset class")
    if not derivatives:
        return _abstain("flow", "no derivatives snapshot")
    funding = _f(derivatives.get("funding_rate"))
    oi = _f(derivatives.get("oi_change_pct"))
    if funding is None and oi is None:
        return _abstain("flow", "derivatives snapshot had neither funding nor OI")

    score, bits = 0.0, []
    if funding is not None:
        # Crowded longs pay to stay long. Extreme funding is a warning about
        # the crowd, not a confirmation of its direction.
        if funding > 0.0005:
            score -= 1.0
            bits.append(f"funding {funding * 100:.3f}% — longs crowded")
        elif funding < -0.0005:
            score += 1.0
            bits.append(f"funding {funding * 100:.3f}% — shorts crowded")
        else:
            bits.append("funding neutral")
    if oi is not None and abs(oi) >= 5:
        bits.append(f"open interest {oi:+.1f}%")
    score = max(-1.0, min(1.0, score))
    state = "crowded_long" if score < -0.3 else "crowded_short" if score > 0.3 else "balanced"
    return _axis("flow", state, score, 0.65, "; ".join(bits), ["derivatives"])


# ── Assembly ─────────────────────────────────────────────────────────────────

def measure(asset_class: str, bench: dict, derivatives: dict | None = None) -> dict:
    """The four axes for one asset class. `bench` is the shape produced by
    benchmark_snapshot() — kept as a parameter so this is pure and testable
    without any network."""
    cls = str(asset_class or DEFAULT_CLASS).lower()
    axes = {
        "trend": _trend_axis(bench or {}),
        "volatility": _volatility_axis(bench or {}),
        "liquidity": _liquidity_axis(bench or {}),
        "flow": _flow_axis(cls, derivatives),
    }
    measured = [a for a in axes.values() if not a["abstained"]]
    return {
        "asset_class": cls,
        "benchmark": (bench or {}).get("primary", {}).get("symbol"),
        "axes": axes,
        "measured_axes": len(measured),
        "abstained_axes": [a["axis"] for a in axes.values() if a["abstained"]],
        "summary": "; ".join(f"{a['axis']} {a['state']}" for a in measured)
                   or "nothing measurable",
    }


# Which regimes each strategy belongs in. Mean reversion in a strong trend
# is the classic way to lose money with a technically valid setup, and
# nothing in the engine could express that — the strategy scored the same
# in every regime.
STRATEGY_FIT = {
    "breakout":            {"trend": ("uptrend", "downtrend", "choppy_up", "choppy_down"),
                            "volatility": ("expanding", "elevated", "normal")},
    "trend_continuation":  {"trend": ("uptrend", "downtrend"),
                            "volatility": ("normal", "elevated", "quiet")},
    "mean_reversion":      {"trend": ("flat", "choppy_up", "choppy_down"),
                            "volatility": ("normal", "elevated", "expanding")},
    "range_fade":          {"trend": ("flat", "choppy_up", "choppy_down"),
                            "volatility": ("quiet", "normal", "compressed")},
    "momentum":            {"trend": ("uptrend", "downtrend", "choppy_up", "choppy_down"),
                            "volatility": ("expanding", "elevated", "normal")},

    # Phase 6. A retest needs a trend to retest INTO; a failed break and a
    # sweep are reversal trades and belong where direction is contested;
    # squeeze expansion is the one strategy that WANTS compression, which
    # is exactly the regime every other breakout strategy avoids.
    "breakout_retest":     {"trend": ("uptrend", "downtrend", "choppy_up", "choppy_down"),
                            "volatility": ("expanding", "elevated", "normal")},
    "failed_breakout":     {"trend": ("flat", "choppy_up", "choppy_down"),
                            "volatility": ("expanding", "elevated", "normal")},
    "liquidity_sweep_reversal": {"trend": ("flat", "choppy_up", "choppy_down",
                                           "uptrend", "downtrend"),
                                 "volatility": ("expanding", "elevated", "normal")},
    "squeeze_expansion":   {"volatility": ("compressed", "quiet", "normal")},
    "momentum_ignition":   {"volatility": ("expanding", "elevated", "normal"),
                            "liquidity": ("heavy", "normal")},
    "vwap_reclaim":        {"trend": ("uptrend", "downtrend", "choppy_up", "choppy_down",
                                      "flat"),
                            "liquidity": ("heavy", "normal")},
    "relative_strength_breakout": {"trend": ("uptrend", "downtrend", "choppy_up",
                                             "choppy_down"),
                                   "liquidity": ("heavy", "normal")},
    "funding_squeeze":     {"flow": ("crowded_long", "crowded_short")},
    "divergence_reversal": {"trend": ("uptrend", "downtrend", "choppy_up", "choppy_down")},
}

# How much a strategy used outside its regime is marked down. Not a veto:
# the setup may still be valid, and the measured record should decide.
OUT_OF_REGIME_PENALTY = 8.0


def strategy_fit(strategy: str | None, regime: dict | None) -> dict:
    """Whether this strategy belongs in this regime, and what it costs if not.

    Abstained axes cannot object. A regime we failed to measure must not
    penalise a setup — that would turn missing data into evidence.
    """
    fit = STRATEGY_FIT.get(str(strategy or "").lower())
    if not fit or not regime:
        return {"fits": True, "penalty": 0.0, "reason": "no regime rule for this strategy",
                "conflicts": []}
    axes = regime.get("axes") or {}
    conflicts = []
    for axis_name, allowed in fit.items():
        axis = axes.get(axis_name) or {}
        if axis.get("abstained") or not axis.get("state"):
            continue
        if axis["state"] not in allowed:
            conflicts.append(f"{strategy} in a {axis['state']} {axis_name}")
    if not conflicts:
        return {"fits": True, "penalty": 0.0,
                "reason": f"{strategy} suits this regime", "conflicts": []}
    return {
        "fits": False,
        "penalty": round(OUT_OF_REGIME_PENALTY * len(conflicts), 2),
        "reason": "; ".join(conflicts),
        "conflicts": conflicts,
    }


# ── Timeframe hierarchy ──────────────────────────────────────────────────────

# The three roles a timeframe plays. Requiring every timeframe to agree is
# how a system produces no trades; ignoring the higher one is how it takes
# every countertrend setup. The hierarchy names which is which.
HIERARCHY = {
    "1m":  ("15m", "5m"), "3m": ("30m", "15m"), "5m": ("1H", "15m"),
    "15m": ("4H", "1H"),  "30m": ("4H", "1H"),  "1H": ("1D", "4H"),
    "2H":  ("1D", "4H"),  "4H": ("1D", "1H"),   "1D": ("1W", "4H"),
    "2D":  ("1W", "1D"),  "1W": ("1W", "1D"),
}


def timeframe_roles(setup_timeframe: str | None) -> dict:
    """Which timeframe sets context, which defines the setup, which times
    entry. Returns the setup timeframe for all three when it is unknown,
    rather than guessing a hierarchy that does not apply."""
    tf = str(setup_timeframe or "").strip()
    higher, execution = HIERARCHY.get(tf, (tf or None, tf or None))
    return {
        "higher_timeframe_context": higher,
        "setup_timeframe": tf or None,
        "execution_timeframe": execution,
    }


def hierarchy_alignment(ta_data: dict, setup_timeframe: str | None,
                        direction: str | None) -> dict:
    """Whether the higher timeframe permits this trade.

    Deliberately NOT a requirement that every timeframe agrees. The higher
    timeframe is context: trading against it is allowed and is sometimes
    the whole point, but it should be known and priced, not invisible.
    """
    roles = timeframe_roles(setup_timeframe)
    higher = roles["higher_timeframe_context"]
    d = (ta_data or {}).get(higher) or {}
    bias = str(d.get("bias") or "").lower()
    wants_bear = str(direction or "Long").lower().startswith("short")
    if not bias or bias == "neutral":
        return {**roles, "higher_bias": bias or None, "aligned": None,
                "detail": f"no usable bias on {higher}"}
    aligned = (bias == "bearish") if wants_bear else (bias == "bullish")
    return {
        **roles,
        "higher_bias": bias,
        "aligned": aligned,
        "detail": (f"{higher} is {bias}, trade is "
                   f"{'short' if wants_bear else 'long'} — "
                   f"{'with' if aligned else 'against'} higher-timeframe context"),
    }


# ── Live snapshot ────────────────────────────────────────────────────────────

def benchmark_snapshot(asset_class: str) -> dict:
    """Fetch and reduce the benchmarks for one asset class.

    Cached briefly: every signal in a scan shares an asset class, and
    re-fetching SPY per signal would dominate the scan's runtime.
    """
    cls = str(asset_class or DEFAULT_CLASS).lower()
    now = time.time()
    hit = _CACHE.get(cls)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    out = {}
    try:
        from lib.ohlcv import fetch_multi_timeframe
        from lib.ta_engine import compute_timeframe
        conf = BENCHMARKS.get(cls) or BENCHMARKS[DEFAULT_CLASS]
        for role in ("primary", "secondary"):
            sym = conf.get(role)
            if not sym:
                continue
            try:
                bars = fetch_multi_timeframe(sym, ["1D"])
                df = bars.get("1D")
                if df is None or len(df) < 60:
                    continue
                d = compute_timeframe(df, "1D")
                if d.get("error"):
                    continue
                atr_pct = (d.get("atr") or {}).get("pct")
                prof = d.get("atr_profile") or {}
                # "emas", plural — the singular key does not exist, and
                # reading it abstained the trend axis on every symbol.
                emas = d.get("emas") or {}
                out[role] = {
                    "symbol": sym,
                    "close": (d.get("price") or {}).get("last") or float(df["close"].iloc[-1]),
                    "ema21": emas.get("ema21"),
                    "ema50": emas.get("ema50"),
                    "adx": (d.get("adx") or {}).get("value"),
                    "atr_pct": atr_pct,
                    "atr_percentile": prof.get("percentile"),
                    "volume_ratio": (d.get("volume") or {}).get("surge_ratio"),
                }
            except Exception as e:
                logger.debug(f"[Regime] benchmark {sym} failed: {e}")
    except Exception as e:
        logger.debug(f"[Regime] snapshot failed for {cls}: {e}")
    _CACHE[cls] = (now, out)
    return out


def for_symbol(symbol: str, asset_class: str | None = None,
               derivatives: dict | None = None) -> dict:
    """The four axes for one symbol, measured against its own market."""
    cls = asset_class_of(symbol, asset_class)
    return measure(cls, benchmark_snapshot(cls), derivatives)
