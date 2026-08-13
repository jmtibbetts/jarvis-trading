"""Build the canonical feature vector from what JARVIS already computed.

This module CONSUMES; it never computes an indicator. Recomputing RSI here
would create a second TA engine that can silently disagree with the first,
and then two parts of the system would be trading different charts.

The mask is the important part. Every value carries a flag saying whether
it was genuinely observed, because a model handed 0.0 for an absent funding
rate reads "funding is exactly neutral" — a specific claim, usually false,
and indistinguishable from the real thing once it is in the array.
"""
from __future__ import annotations

import logging
import math

from lib.predictive.schemas import (CURRENT_SCHEMA, FeatureVector, SCHEMAS,
                                    schema_hash)

logger = logging.getLogger(__name__)


def _num(v):
    """A real number, or None. Bools are rejected: True would become 1.0 and
    silently occupy a slot meant for a measurement."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _flag(v):
    """An explicit boolean becomes 1.0/0.0; absence stays absent."""
    if v is None:
        return None
    return 1.0 if bool(v) else 0.0


def _dig(d, *path):
    cur = d or {}
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60,
              "2H": 120, "4H": 240, "1D": 1440, "2D": 2880, "1W": 10080}


def _break_flags(structure: dict) -> dict:
    """The most recent break, as three mutually-exclusive flags plus its
    context. held / failed / swept are different trades (lib/structure.py),
    so they get separate features rather than one encoded integer that a
    model would wrongly read as ordered."""
    out = {"break_held": 0.0, "break_failed": 0.0, "break_swept": 0.0,
           "break_age_bars": None, "break_volume_ratio": None}
    if not isinstance(structure, dict):
        structure = {}
    breaks = structure.get("breaks")
    breaks = breaks if isinstance(breaks, list) else []
    b = next((x for x in breaks if isinstance(x, dict)), None)
    if not b:
        # No break is a real observation, not missing data: we looked.
        out["break_age_bars"] = 0.0
        out["break_volume_ratio"] = 1.0
        return out
    outcome = b.get("outcome")
    out["break_held"] = 1.0 if outcome == "held" else 0.0
    out["break_failed"] = 1.0 if outcome == "failed" else 0.0
    out["break_swept"] = 1.0 if outcome == "sweep" else 0.0
    age = _num(b.get("bars_ago"))
    out["break_age_bars"] = (age / 20.0) if age is not None else None
    out["break_volume_ratio"] = _num(b.get("break_volume_ratio"))
    return out


def build(*, ta: dict, signal: dict, evidence: dict | None = None,
          regime: dict | None = None, max_source_age_s: float = 0.0,
          version: str = CURRENT_SCHEMA) -> FeatureVector:
    """Assemble one vector. Anything unavailable is masked, never defaulted."""
    ta = ta or {}
    signal = signal or {}
    evidence = evidence or {}
    axes = _dig(regime or {}, "axes") or {}
    # Each nested block defended independently: a malformed TA payload must
    # produce a masked vector, not an exception inside a scan loop.
    structure = ta.get("structure")
    structure = structure if isinstance(structure, dict) else {}
    atrs = ta.get("atr_distances")
    atrs = atrs if isinstance(atrs, dict) else {}
    atr = _num(_dig(ta, "atr", "value"))
    bf = _break_flags(structure)

    entry = _num(signal.get("entry_price"))
    stop = _num(signal.get("stop_loss"))
    stop_dist_atr = None
    if entry is not None and stop is not None and atr:
        stop_dist_atr = abs(entry - stop) / atr

    macd_hist = _num(_dig(ta, "macd", "histogram"))
    emas = ta.get("emas")
    emas = emas if isinstance(emas, dict) else {}
    e9, e21, e50 = (_num(emas.get(k)) for k in ("ema9", "ema21", "ema50"))

    tf = str(signal.get("timeframe") or "")
    tf_min = TF_MINUTES.get(tf)

    raw = {
        "rsi": (lambda v: v / 100.0 if v is not None else None)(_num(ta.get("rsi"))),
        "adx": (lambda v: v / 100.0 if v is not None else None)(_num(_dig(ta, "adx", "value"))),
        "macd_hist_atr": (macd_hist / atr) if (macd_hist is not None and atr) else None,
        "ema9_ema21_atr": ((e9 - e21) / atr) if (None not in (e9, e21) and atr) else None,
        "ema21_ema50_atr": ((e21 - e50) / atr) if (None not in (e21, e50) and atr) else None,
        "vwap_dist_atr": _num(atrs.get("to_vwap")),
        "atr_pct": _num(_dig(ta, "atr", "pct")),
        "atr_percentile": (lambda v: v / 100.0 if v is not None else None)(
            _num(_dig(ta, "atr_profile", "percentile"))),
        "support_dist_atr": _num(atrs.get("to_support")),
        "resistance_dist_atr": _num(atrs.get("to_resistance")),
        "position_in_range": _num(_dig(ta, "support_resistance", "position_in_range")),
        "level_strength": _num(_dig(
            (structure.get("levels") or [{}])[0]
            if isinstance(structure.get("levels"), list) and structure.get("levels")
            else {}, "strength")),
        "break_held": bf["break_held"],
        "break_failed": bf["break_failed"],
        "break_swept": bf["break_swept"],
        "break_age_bars": bf["break_age_bars"],
        "break_volume_ratio": bf["break_volume_ratio"],
        "volume_ratio": _num(_dig(ta, "volume", "surge_ratio")),
        "obv_rising": _flag(str(ta.get("obv_trend") or "").lower() == "rising")
                      if ta.get("obv_trend") else None,
        "mfi": (lambda v: v / 100.0 if v is not None else None)(_num(ta.get("mfi"))),
        "supporting_weight": _num(evidence.get("supporting_weight")),
        "contradicting_weight": _num(evidence.get("contradicting_weight")),
        "contradiction_count": _num(evidence.get("contradiction_count")),
        "confluence": (lambda v: v / 100.0 if v is not None else None)(
            _num(evidence.get("confluence"))),
        "regime_trend_score": _num(_dig(axes, "trend", "score")),
        "regime_vol_score": _num(_dig(axes, "volatility", "score")),
        "regime_liquidity_score": _num(_dig(axes, "liquidity", "score")),
        "regime_flow_score": _num(_dig(axes, "flow", "score")),
        "is_short": _flag(str(signal.get("direction") or "").lower().startswith("short")),
        "rr_ratio": _num(signal.get("rr_ratio")),
        "stop_dist_atr": stop_dist_atr,
        "strategy_match": _num(signal.get("strategy_score")),
        "tf_minutes_log": (math.log(tf_min) / 10.0) if tf_min else None,
    }

    # An abstained regime axis is MISSING, not zero. lib/regime_axes.py is
    # careful to distinguish "could not measure" from "measured neutral";
    # collapsing that here would throw the distinction away at the last step.
    for axis in ("trend", "volatility", "liquidity", "flow"):
        if _dig(axes, axis, "abstained"):
            raw[f"regime_{ {'volatility':'vol'}.get(axis, axis) }_score"] = None

    values, mask = [], []
    for f in SCHEMAS[version]:
        v = raw.get(f.name)
        if v is None:
            values.append(0.0)      # padding, and the mask says so
            mask.append(0.0)
        else:
            values.append(float(min(f.hi, max(f.lo, v))))
            mask.append(1.0)

    return FeatureVector(
        values=values, mask=mask, schema_version=version,
        schema_hash=schema_hash(version), max_source_age_s=max_source_age_s,
        symbol=signal.get("asset_symbol"), timeframe=signal.get("timeframe"),
        meta={"bar_time": ta.get("bar_time")},
    )
