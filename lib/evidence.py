"""Evidence grouped by what it independently measures, and what argues back.

Two defects in how confluence was counted before this module.

**Correlated indicators voted repeatedly.** RSI, Stochastic, CCI, Williams %R
and MFI are five different arithmetic treatments of "where is price inside
its recent range". When price is extended they all say so, and the old
scorer counted that as five confirmations. It is one observation reported
five times, and weighting it five-fold is how a signal reaches 85%
confidence on a single fact. Here, momentum is ONE category with ONE
verdict; the five readings decide that verdict between them and then count
once.

**Disagreement was averaged away.** The old path computed a single
`ta_confluence` number, so a setup where price structure was bullish and
volume flow was bearish scored the same as one where both were mildly
positive — the conflict vanished into the mean. A mean is the one summary
guaranteed to hide a contradiction. This module never averages the two
sides together: `supporting` and `contradicting` are returned as separate
lists and stay separate all the way to the UI.

Categories are chosen to be as independent of each other as the available
data allows, because the whole point of counting confluence is that
agreement between INDEPENDENT measurements is informative and agreement
between restatements of one measurement is not.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BULL, BEAR, NEUTRAL = "bullish", "bearish", "neutral"

# What each category independently measures. Weights reflect how much a
# category tells you that the others do not — structure and trend are the
# load-bearing ones; volatility mostly qualifies the others rather than
# pointing a direction of its own.
CATEGORY_WEIGHTS = {
    "structure": 1.0,
    "trend": 1.0,
    "momentum": 0.8,
    "volume": 0.7,
    "relative_strength": 0.7,
    "derivatives": 0.7,
    "volatility": 0.4,
}
CATEGORIES = tuple(CATEGORY_WEIGHTS)

# Categories that can only ever object, never endorse. Volatility says
# whether the levels are readable at all — it has no opinion on direction.
# Left to the ordinary path it would be recorded as SUPPORTING a short
# whenever conditions were too wild to read, which reads as "the chaos
# agrees with me". An objection stays an objection whichever way the trade
# points.
NON_DIRECTIONAL = {"volatility"}


def _f(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _verdict(votes: list[tuple[str, str]]) -> tuple[str, float, list[str]]:
    """Resolve one category's readings into a single verdict.

    `votes` is [(direction, human reading), ...]. Strength is the margin of
    the winning side over the total, so a category whose own indicators
    disagree reports low strength rather than a confident direction — an
    internally split category is not evidence.
    """
    if not votes:
        return NEUTRAL, 0.0, []
    bull = [r for d, r in votes if d == BULL]
    bear = [r for d, r in votes if d == BEAR]
    if len(bull) == len(bear):
        return NEUTRAL, 0.0, [r for _, r in votes]
    winner, readings = (BULL, bull) if len(bull) > len(bear) else (BEAR, bear)
    strength = abs(len(bull) - len(bear)) / len(votes)
    return winner, round(strength, 3), readings


# ── Category readers ─────────────────────────────────────────────────────────

def _structure(d: dict) -> tuple[str, float, list[str]]:
    """Where price sits in its own range, and whether it has broken out."""
    votes = []
    don = d.get("donchian") or {}
    if don.get("breakout_up"):
        votes.append((BULL, "broke the Donchian channel high"))
    if don.get("breakout_down"):
        votes.append((BEAR, "broke the Donchian channel low"))

    sr = d.get("support_resistance") or {}
    pos = _f(sr.get("position_in_range"))
    if pos is not None:
        if pos >= 0.8:
            votes.append((BEAR, f"at the top of its range ({pos:.0%}) — resistance overhead"))
        elif pos <= 0.2:
            votes.append((BULL, f"at the bottom of its range ({pos:.0%}) — support beneath"))

    kel = (d.get("keltner") or {}).get("position")
    if kel == "above":
        votes.append((BULL, "above the Keltner channel"))
    elif kel == "below":
        votes.append((BEAR, "below the Keltner channel"))

    ms = d.get("market_structure") or {}
    if isinstance(ms, dict):
        b = str(ms.get("bias") or ms.get("structure") or "").lower()
        if "bull" in b:
            votes.append((BULL, "market structure making higher highs"))
        elif "bear" in b:
            votes.append((BEAR, "market structure making lower lows"))
    return _verdict(votes)


def _trend(d: dict) -> tuple[str, float, list[str]]:
    """Direction and whether it has any conviction behind it."""
    votes = []
    st = d.get("supertrend") or {}
    if st.get("direction") == "up":
        votes.append((BULL, "Supertrend up"))
    elif st.get("direction") == "down":
        votes.append((BEAR, "Supertrend down"))

    ema = d.get("ema") or {}
    fast, slow = _f(ema.get("ema9")), _f(ema.get("ema21"))
    if fast and slow:
        if fast > slow:
            votes.append((BULL, "EMA9 above EMA21"))
        elif fast < slow:
            votes.append((BEAR, "EMA9 below EMA21"))

    vwap = d.get("vwap") or {}
    if vwap.get("position") == "above":
        votes.append((BULL, "trading above VWAP"))
    elif vwap.get("position") == "below":
        votes.append((BEAR, "trading below VWAP"))

    direction, strength, readings = _verdict(votes)
    # ADX has no direction of its own — it says how much the direction is
    # worth. A trend read off ADX 12 is a trend in name only.
    adx = _f((d.get("adx") or {}).get("value"))
    if adx is not None and direction != NEUTRAL:
        if adx >= 25:
            readings = readings + [f"ADX {adx:.0f} — trend has conviction"]
        else:
            strength = round(strength * 0.5, 3)
            readings = readings + [f"ADX {adx:.0f} — weak, direction is unreliable"]
    return direction, strength, readings


def _momentum(d: dict) -> tuple[str, float, list[str]]:
    """The five-way restatement, collapsed to one vote.

    RSI, Stochastic, CCI, Williams %R and MFI are different arithmetic over
    the same question. They belong in one category precisely so that their
    agreement — which is close to automatic — cannot be scored as five
    independent confirmations.
    """
    votes = []
    rsi = _f(d.get("rsi"))
    if rsi is not None:
        if rsi >= 70:
            votes.append((BEAR, f"RSI {rsi:.0f} — overbought"))
        elif rsi <= 30:
            votes.append((BULL, f"RSI {rsi:.0f} — oversold"))

    stoch = (d.get("stochastic") or {}).get("signal")
    if stoch == "overbought":
        votes.append((BEAR, "Stochastic overbought"))
    elif stoch == "oversold":
        votes.append((BULL, "Stochastic oversold"))

    cci = _f(d.get("cci"))
    if cci is not None:
        if cci >= 100:
            votes.append((BEAR, f"CCI {cci:.0f} — extended"))
        elif cci <= -100:
            votes.append((BULL, f"CCI {cci:.0f} — extended"))

    wr = _f(d.get("williams_r"))
    if wr is not None:
        if wr >= -20:
            votes.append((BEAR, f"Williams %R {wr:.0f} — overbought"))
        elif wr <= -80:
            votes.append((BULL, f"Williams %R {wr:.0f} — oversold"))

    # MACD crossover is a genuinely different question (rate of change of
    # the trend) but sits closest to momentum, so it votes here rather than
    # earning a category of its own.
    cross = (d.get("macd") or {}).get("crossover")
    if cross == "bullish":
        votes.append((BULL, "MACD crossed up"))
    elif cross == "bearish":
        votes.append((BEAR, "MACD crossed down"))
    return _verdict(votes)


def _volume(d: dict) -> tuple[str, float, list[str]]:
    """Whether participation confirms the move — the classic place a price
    thesis and a flow thesis part company."""
    votes = []
    obv = str(d.get("obv_trend") or "").lower()
    if obv == "rising":
        votes.append((BULL, "OBV rising — accumulation"))
    elif obv == "falling":
        votes.append((BEAR, "OBV falling — distribution"))

    mfi = _f(d.get("mfi"))
    if mfi is not None:
        if mfi >= 80:
            votes.append((BEAR, f"MFI {mfi:.0f} — money flow overextended"))
        elif mfi <= 20:
            votes.append((BULL, f"MFI {mfi:.0f} — money flow washed out"))

    vol = d.get("volume") or {}
    if vol.get("dry"):
        votes.append((BEAR, "volume dry — no participation"))
    return _verdict(votes)


def _volatility(d: dict) -> tuple[str, float, list[str]]:
    """Not directional. Reports whether the setup is being read in
    conditions where levels mean anything at all."""
    atr_pct = _f((d.get("atr") or {}).get("pct"))
    if atr_pct is None:
        return NEUTRAL, 0.0, []
    if atr_pct > 10:
        return BEAR, 0.5, [f"ATR {atr_pct:.1f}% of price — levels are noise at this volatility"]
    if atr_pct < 0.15:
        return BEAR, 0.3, [f"ATR {atr_pct:.2f}% — too quiet for the move to be reached"]
    return NEUTRAL, 0.0, [f"ATR {atr_pct:.1f}% — workable"]


def _relative_strength(rs: dict | None) -> tuple[str, float, list[str]]:
    """Performance against its benchmark — independent of anything the
    symbol's own chart says."""
    if not rs:
        return NEUTRAL, 0.0, []
    votes = []
    state = str(rs.get("state") or "").lower()
    if "outperform" in state or "leader" in state:
        votes.append((BULL, f"outperforming its benchmark ({state})"))
    elif "underperform" in state or "laggard" in state:
        votes.append((BEAR, f"underperforming its benchmark ({state})"))
    slope = _f(rs.get("rs_slope"))
    if slope is not None and abs(slope) > 1e-9:
        votes.append((BULL if slope > 0 else BEAR,
                      f"relative strength {'improving' if slope > 0 else 'deteriorating'}"))
    if rs.get("rs_breakout"):
        votes.append((BULL, "relative-strength breakout"))
    return _verdict(votes)


def _derivatives(dv: dict | None) -> tuple[str, float, list[str]]:
    """Funding and open interest — positioning, not price. Crypto only."""
    if not dv:
        return NEUTRAL, 0.0, []
    votes = []
    funding = _f(dv.get("funding_rate"))
    if funding is not None:
        # Crowded longs pay to stay long. Extreme funding is a contrarian
        # reading, not a confirmation of the direction being paid for.
        if funding > 0.0005:
            votes.append((BEAR, f"funding {funding * 100:.3f}% — longs crowded and paying"))
        elif funding < -0.0005:
            votes.append((BULL, f"funding {funding * 100:.3f}% — shorts crowded and paying"))
    oi = _f(dv.get("oi_change_pct"))
    if oi is not None and abs(oi) >= 5:
        votes.append((BULL if oi > 0 else BEAR,
                      f"open interest {oi:+.1f}% — positions {'building' if oi > 0 else 'unwinding'}"))
    return _verdict(votes)


# ── The engine ───────────────────────────────────────────────────────────────

def gather(ta_timeframe: dict, direction: str,
           relative_strength: dict | None = None,
           derivatives: dict | None = None) -> dict:
    """Every category's verdict, split into what supports the trade and what
    argues against it.

    The two sides are never combined into one number here. A caller that
    wants a scalar gets `confluence`, but `supporting` and `contradicting`
    travel with it so the conflict cannot be lost on the way to a decision.
    """
    d = ta_timeframe or {}
    wanted = BEAR if str(direction or "Long").lower().startswith("short") else BULL
    against = BULL if wanted == BEAR else BEAR

    readers = {
        "structure": lambda: _structure(d),
        "trend": lambda: _trend(d),
        "momentum": lambda: _momentum(d),
        "volume": lambda: _volume(d),
        "volatility": lambda: _volatility(d),
        "relative_strength": lambda: _relative_strength(relative_strength),
        "derivatives": lambda: _derivatives(derivatives),
    }

    supporting, contradicting, neutral = [], [], []
    sup_w = con_w = 0.0
    for name in CATEGORIES:
        try:
            verdict, strength, readings = readers[name]()
        except Exception as e:
            logger.debug(f"[Evidence] {name} failed: {e}")
            continue
        item = {"category": name, "verdict": verdict, "strength": strength,
                "weight": CATEGORY_WEIGHTS[name], "readings": readings}
        if name in NON_DIRECTIONAL:
            # Objects or stays out of it; never counts as agreement.
            if strength > 0:
                item["verdict"] = "caveat"
                contradicting.append(item)
                con_w += CATEGORY_WEIGHTS[name] * strength
            else:
                neutral.append(item)
            continue
        if verdict == wanted and strength > 0:
            supporting.append(item)
            sup_w += CATEGORY_WEIGHTS[name] * strength
        elif verdict == against and strength > 0:
            contradicting.append(item)
            con_w += CATEGORY_WEIGHTS[name] * strength
        else:
            neutral.append(item)

    total = sup_w + con_w
    confluence = round(sup_w / total * 100, 1) if total > 0 else 50.0

    return {
        "direction": direction,
        "supporting": sorted(supporting, key=lambda i: -i["weight"] * i["strength"]),
        "contradicting": sorted(contradicting, key=lambda i: -i["weight"] * i["strength"]),
        "neutral": neutral,
        "supporting_weight": round(sup_w, 3),
        "contradicting_weight": round(con_w, 3),
        # Independent categories that disagree. This is the number that
        # matters: one dissenting category is an imperfect setup, three is
        # a different trade than the one being proposed.
        "contradiction_count": len(contradicting),
        "confluence": confluence,
        "categories_read": len(supporting) + len(contradicting) + len(neutral),
        "summary": _summary(supporting, contradicting),
    }


def _summary(supporting: list, contradicting: list) -> str:
    if not supporting and not contradicting:
        return "no directional evidence in any category"
    s = ", ".join(i["category"] for i in supporting) or "nothing"
    if not contradicting:
        return f"{s} support it; nothing contradicts"
    c = ", ".join(i["category"] for i in contradicting)
    return f"{s} support it, but {c} contradict it"


# How much a contradiction costs. Scaled by the weight of the categories
# that disagree, not just how many — flow disagreeing with price is a
# heavier objection than volatility being unhelpful.
CONTRADICTION_PENALTY_PER_WEIGHT = 9.0
MAX_CONTRADICTION_PENALTY = 30.0


def compact(ev: dict | None) -> dict | None:
    """The version that gets persisted on every signal.

    score_breakdown is stored as JSON on each row, and the scanner alone
    writes on the order of 1,300 signals a day — the full block runs ~2KB,
    which is ~100MB a month of mostly-nothing. The neutral categories are
    dropped (their names are kept, so "we looked and found nothing there"
    is still recoverable) and only the categories that actually took a side
    carry their readings.
    """
    if not ev:
        return None
    keep = ("category", "verdict", "strength", "weight", "readings")
    return {
        "timeframe": ev.get("timeframe"),
        "direction": ev.get("direction"),
        "supporting": [{k: c[k] for k in keep} for c in ev.get("supporting", [])],
        "contradicting": [{k: c[k] for k in keep} for c in ev.get("contradicting", [])],
        "neutral_categories": [c["category"] for c in ev.get("neutral", [])],
        "supporting_weight": ev.get("supporting_weight"),
        "contradicting_weight": ev.get("contradicting_weight"),
        "contradiction_count": ev.get("contradiction_count"),
        "confluence": ev.get("confluence"),
        "summary": ev.get("summary"),
    }


def contradiction_penalty(ev: dict) -> float:
    """Points to subtract from the composite for evidence pointing the other way.

    Returned positive; the caller subtracts. Capped so that a setup with
    every category against it is heavily penalised but not driven to a
    number that stops distinguishing between bad and worse.
    """
    if not ev:
        return 0.0
    w = float(ev.get("contradicting_weight") or 0)
    return round(min(MAX_CONTRADICTION_PENALTY, w * CONTRADICTION_PENALTY_PER_WEIGHT), 2)
