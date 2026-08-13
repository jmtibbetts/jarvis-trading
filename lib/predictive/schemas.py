"""The feature contract, versioned, and the difference between missing and zero.

Two failure modes this exists to make impossible.

**Silent schema drift.** A model trained on 30 features in a fixed order,
handed 30 features in a different order, does not fail — it returns
confident nonsense. Every model records the schema hash it was trained
against, and inference refuses to run against a different one. A refusal is
recoverable; a plausible wrong number is not.

**Zero standing in for absent.** If funding rate is unavailable and the
vector carries 0.0, the model reads "funding is exactly neutral" — a
specific and usually false claim. Every feature therefore travels with a
mask saying whether it was actually observed, and a model asked to predict
from too little real data abstains instead.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Feature:
    """One input. `unit` and `source` exist so a vector can be audited a
    year from now without re-deriving where each number came from."""
    name: str
    unit: str
    source: str
    # Values outside this are clipped, not passed through. A 330R MFE is
    # real (it happened, on a near-zero risk distance) but it would dominate
    # any gradient it touched.
    lo: float = -10.0
    hi: float = 10.0


def _f(name, unit, source, lo=-10.0, hi=10.0):
    return Feature(name, unit, source, lo, hi)


# ── v1 ───────────────────────────────────────────────────────────────────
# Deliberately small. Every entry is something JARVIS already computes
# deterministically — nothing here recomputes an indicator, which would
# create a second TA engine that can silently disagree with the first.
#
# ATR-normalised wherever a distance is involved, so one model can span
# SHIB and crude without a per-symbol scaler.
FEATURES_V1: tuple[Feature, ...] = (
    # trend / momentum, from lib/ta_engine.py
    _f("rsi", "0-100 scaled to 0-1", "ta.rsi", 0.0, 1.0),
    _f("adx", "0-100 scaled to 0-1", "ta.adx.value", 0.0, 1.0),
    _f("macd_hist_atr", "ATR", "ta.macd.histogram / atr"),
    _f("ema9_ema21_atr", "ATR", "ta.emas"),
    _f("ema21_ema50_atr", "ATR", "ta.emas"),
    _f("vwap_dist_atr", "ATR", "ta.atr_distances.to_vwap"),
    # volatility, from lib/atr_normalization.py
    _f("atr_pct", "percent of price", "ta.atr.pct", 0.0, 25.0),
    _f("atr_percentile", "0-100 scaled to 0-1", "ta.atr_profile.percentile", 0.0, 1.0),
    # structure, from lib/structure.py
    _f("support_dist_atr", "ATR", "ta.atr_distances.to_support"),
    _f("resistance_dist_atr", "ATR", "ta.atr_distances.to_resistance"),
    _f("position_in_range", "0-1", "ta.support_resistance.position_in_range", 0.0, 1.0),
    _f("level_strength", "0-1", "structure.levels[0].strength", 0.0, 1.0),
    _f("break_held", "flag", "structure.breaks", 0.0, 1.0),
    _f("break_failed", "flag", "structure.breaks", 0.0, 1.0),
    _f("break_swept", "flag", "structure.breaks", 0.0, 1.0),
    _f("break_age_bars", "bars / 20", "structure.breaks.bars_ago", 0.0, 5.0),
    _f("break_volume_ratio", "x average", "structure.breaks.break_volume_ratio", 0.0, 10.0),
    # participation
    _f("volume_ratio", "x 20-bar average", "ta.volume.surge_ratio", 0.0, 10.0),
    _f("obv_rising", "flag", "ta.obv_trend", 0.0, 1.0),
    _f("mfi", "0-100 scaled to 0-1", "ta.mfi", 0.0, 1.0),
    # evidence, from lib/evidence.py — the contradiction engine's own output
    _f("supporting_weight", "weight", "evidence.supporting_weight", 0.0, 6.0),
    _f("contradicting_weight", "weight", "evidence.contradicting_weight", 0.0, 6.0),
    _f("contradiction_count", "count", "evidence.contradiction_count", 0.0, 8.0),
    _f("confluence", "0-100 scaled to 0-1", "evidence.confluence", 0.0, 1.0),
    # regime, from lib/regime_axes.py — scores, not opaque labels
    _f("regime_trend_score", "-1..1", "regime.axes.trend.score", -1.0, 1.0),
    _f("regime_vol_score", "-1..1", "regime.axes.volatility.score", -1.0, 1.0),
    _f("regime_liquidity_score", "-1..1", "regime.axes.liquidity.score", -1.0, 1.0),
    _f("regime_flow_score", "-1..1", "regime.axes.flow.score", -1.0, 1.0),
    # the trade being proposed
    _f("is_short", "flag", "signal.direction", 0.0, 1.0),
    _f("rr_ratio", "ratio", "signal.rr_ratio", 0.0, 10.0),
    _f("stop_dist_atr", "ATR", "signal.stop_loss vs entry", 0.0, 10.0),
    _f("strategy_match", "0-1", "signal.strategy_score", 0.0, 1.0),
    # horizon, one-hot-ish so a 1m and a 1W setup are distinguishable
    _f("tf_minutes_log", "ln(minutes)/10", "signal.timeframe", 0.0, 1.5),
)

SCHEMAS = {"v1": FEATURES_V1}
CURRENT_SCHEMA = "v1"


def feature_names(version: str = CURRENT_SCHEMA) -> tuple[str, ...]:
    return tuple(f.name for f in SCHEMAS[version])


def schema_hash(version: str = CURRENT_SCHEMA) -> str:
    """Identity of the contract: names, order, units and bounds.

    Order is included deliberately. Reordering features while keeping the
    same names produces a vector a model will happily consume and
    misinterpret, and that is exactly the failure this hash exists to catch.
    """
    spec = "|".join(f"{f.name}:{f.unit}:{f.lo}:{f.hi}" for f in SCHEMAS[version])
    return hashlib.sha256(spec.encode("utf-8")).hexdigest()[:16]


def dimension(version: str = CURRENT_SCHEMA) -> int:
    return len(SCHEMAS[version])


@dataclass
class FeatureVector:
    """Values plus the mask that says which were real.

    `values` is already clipped and scaled. `mask` is 1.0 where the feature
    was genuinely observed and 0.0 where it was absent — a model must be
    able to tell "funding is neutral" from "we have no funding feed".
    """
    values: list[float]
    mask: list[float]
    schema_version: str
    schema_hash: str
    max_source_age_s: float = 0.0
    symbol: str | None = None
    timeframe: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def missing_fraction(self) -> float:
        if not self.mask:
            return 1.0
        return 1.0 - (sum(self.mask) / len(self.mask))

    def matches(self, expected_hash: str | None) -> bool:
        return expected_hash is None or expected_hash == self.schema_hash
