"""What a setup is actually worth, after what it actually costs.

A win rate is not an edge. 45% wins that average +2R against losses that
average -1R is a good business; 60% wins that average +0.4R against -1R
losses is a slow bankruptcy, and the second one looks better on every
dashboard this codebase had. Nothing computed expectancy from the measured
distribution of outcomes — the composite score ranked setups by how much
evidence supported them, which is a different question from how much money
they make.

And gross expectancy is not the number that matters either. Costs are
already modelled properly in lib/transaction_costs.py — spread, fees,
slippage, funding, borrow — and the measured fact from earlier work is that
they routinely consume the whole edge on short-horizon trades. A setup with
+0.15R gross and 0.20R of costs is a losing trade with a beautiful chart.

So: measure P(win), average win in R and average loss in R from closed
trades, bucket them by the things that plausibly change the answer, subtract
the real costs, and return NO_TRADE when nothing survives.

Two disciplines carried from the calibration work:

**Hierarchical fallback with honest labelling.** The most specific bucket
with enough evidence wins, and the answer says which bucket it came from.
A number computed from 12 trades in an exactly-matching bucket is worse
than one from 4,000 trades in a broader one.

**Replayed outcomes are weighted below live ones.** A replayed fill is
frictionless and systematically optimistic.
"""
from __future__ import annotations

import logging
import math
import threading
import time

logger = logging.getLogger(__name__)

# Below this, a bucket has not earned an opinion and we fall back to a
# broader one rather than reporting a confident number from noise.
MIN_SAMPLE = 25

# Replayed outcomes assumed perfect fills and that both a bar's high and low
# were reachable. Real money does not get that.
REPLAY_WEIGHT = 0.5

# Buckets are tried most-specific first. Every level after the first is a
# broader claim, and the result records which one answered.
# Ordered by how much they constrain, NOT by branch. An earlier draft ran
# the strategy branch to exhaustion first, so the chain fell from a 2-key
# strategy bucket to a 3-key asset-class one — a step that is broader in
# one dimension and narrower in another, which is not a fallback. Within
# each width, strategy is preferred, because which strategy was used is
# the more informative fact.
HIERARCHY = (
    ("strategy", "asset_class", "direction", "timeframe"),
    ("strategy", "asset_class", "timeframe"),
    ("asset_class", "direction", "timeframe"),
    ("strategy", "timeframe"),
    ("asset_class", "timeframe"),
    ("timeframe",),
    (),
)

_CACHE: dict = {"built_at": 0.0, "table": None}
_CACHE_TTL = 300.0
_LOCK = threading.Lock()


def _f(v, default=None):
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def wilson_interval(wins: float, total: float, z: float = 1.96) -> tuple[float, float]:
    """Confidence interval for a proportion.

    Reported because a 60% win rate over 10 trades and over 4,000 are not
    the same claim, and a point estimate makes them look identical. The
    LOWER bound is what a decision should lean on.
    """
    if total <= 0:
        return 0.0, 1.0
    p = wins / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _r_of(entry, stop, exit_price, direction) -> float | None:
    """Result in R: multiples of the risk originally taken.

    R is the only unit in which a 15m scalp and a weekly position can be
    averaged together. Percent cannot: 1% is a full stop on one and noise
    on the other.
    """
    entry, stop, exit_price = _f(entry), _f(stop), _f(exit_price)
    if entry is None or stop is None or exit_price is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    # MISSING IS NOT LONG. `startswith("short")` turned a missing or
    # unparseable direction into False, which is indistinguishable from a
    # known long — so an outcome whose side nobody recorded was booked with
    # the sign of a long, and a winning short entered the learning ledger as
    # a loss. The strict parser returns None instead, and an R that cannot
    # be computed is not computed.
    from lib.trade_side import SHORT, parse_side_strict
    side = parse_side_strict(direction)
    if side is None:
        return None
    move = (entry - exit_price) if side == SHORT else (exit_price - entry)
    return move / risk


def _placed_stops(db) -> dict:
    """signal_id -> the stop AS PLACED at position open (immutable,
    Phase 0's initial_stop_loss). The learning ledger must compute R
    against the risk actually taken: the signal's stop and the placed
    stop diverge whenever the horizon cap clamped it or a spread-adjusted
    fill moved the entry — and every R computed from the wrong one is a
    lie about risk (P0.12 consumer side)."""
    try:
        from app.database import PaperPosition
        rows = db.query(PaperPosition.signal_id, PaperPosition.initial_stop_loss).filter(
            PaperPosition.signal_id.isnot(None),
            PaperPosition.initial_stop_loss.isnot(None)).all()
        return {sid: float(stop) for sid, stop in rows if stop}
    except Exception:
        return {}


def build_table(force: bool = False) -> dict:
    """Measured R-distribution per bucket, from closed trades."""
    with _LOCK:
        if not force and _CACHE["table"] is not None and \
                (time.time() - _CACHE["built_at"]) < _CACHE_TTL:
            return _CACHE["table"]

    table: dict = {}
    try:
        from app.database import get_db, TradeOutcome, TradingSignal
        from lib.calibration import CURRENT_EPOCH
        with get_db() as db:
            rows = db.query(
                TradeOutcome.timeframe, TradeOutcome.direction, TradeOutcome.asset_class,
                TradeOutcome.entry_price, TradeOutcome.exit_price,
                TradeOutcome.outcome_source, TradingSignal.stop_loss, TradingSignal.strategy,
                TradeOutcome.signal_id,
            ).outerjoin(
                TradingSignal, TradingSignal.id == TradeOutcome.signal_id
            ).filter(TradeOutcome.engine_epoch == CURRENT_EPOCH).all()
            placed = _placed_stops(db)
    except Exception as e:
        logger.warning(f"[Expectancy] could not read outcomes: {e}")
        return table

    for tf, direction, cls, entry, exit_price, src, stop, strategy, sig_id in rows:
        # Prefer the stop as PLACED over the signal's proposal.
        stop_used = placed.get(sig_id) or stop
        r = _r_of(entry, stop_used, exit_price, direction)
        if r is None:
            continue
        # An outcome worse than -1R usually means the stop was gapped or
        # never honoured. Clipped rather than dropped: the loss was real,
        # but letting a single -40R fill dominate an average would make the
        # bucket describe one bad fill instead of the strategy.
        r = max(-3.0, min(10.0, r))
        w = REPLAY_WEIGHT if src == "replay" else 1.0
        key_values = {
            "strategy": (strategy or "unclassified"),
            "asset_class": (cls or "unknown").lower(),
            "direction": (direction or "unknown").lower(),
            "timeframe": tf or "unknown",
        }
        for level in HIERARCHY:
            key = (level, tuple(key_values[k] for k in level))
            cell = table.setdefault(key, {"n": 0.0, "wins": 0.0,
                                          "win_r": 0.0, "loss_r": 0.0,
                                          "win_n": 0.0, "loss_n": 0.0, "raw": 0,
                                          "raw_live": 0, "raw_replay": 0})
            cell["n"] += w
            cell["raw"] += 1
            cell["raw_replay" if src == "replay" else "raw_live"] += 1
            if r > 0:
                cell["wins"] += w
                cell["win_r"] += r * w
                cell["win_n"] += w
            else:
                cell["loss_r"] += abs(r) * w
                cell["loss_n"] += w

    with _LOCK:
        _CACHE["table"] = table
        _CACHE["built_at"] = time.time()
    logger.info(f"[Expectancy] built {len(table)} buckets from {len(rows)} outcomes")
    return table


def _summarise(cell: dict, level: tuple, values: tuple) -> dict:
    n = cell["n"]
    p_win = cell["wins"] / n if n else 0.0
    avg_win_r = cell["win_r"] / cell["win_n"] if cell["win_n"] else 0.0
    avg_loss_r = cell["loss_r"] / cell["loss_n"] if cell["loss_n"] else 1.0
    gross = p_win * avg_win_r - (1 - p_win) * avg_loss_r
    lo, hi = wilson_interval(cell["wins"], n)
    # The pessimistic read: expectancy computed at the LOWER bound of the
    # win-rate interval. This is what a decision should lean on, because a
    # point estimate from a thin sample flatters itself.
    gross_lower = lo * avg_win_r - (1 - lo) * avg_loss_r
    return {
        "bucket": "/".join(level) if level else "overall",
        "bucket_values": dict(zip(level, values)) if level else {},
        "sample": round(n, 1),
        "raw_sample": cell["raw"],
        # The evidence MIX, always visible (doc §1.2): a bucket built from
        # 500 replayed bars and 3 live fills must never present itself as
        # 503 equivalent observations.
        "sample_live": cell.get("raw_live", 0),
        "sample_replay": cell.get("raw_replay", 0),
        "p_win": round(p_win, 4),
        "p_win_ci": [round(lo, 4), round(hi, 4)],
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "gross_expected_r": round(gross, 4),
        "gross_expected_r_lower": round(gross_lower, 4),
    }


def lookup(strategy=None, asset_class=None, direction=None, timeframe=None) -> dict | None:
    """Most specific bucket with enough evidence, labelled with which one."""
    table = build_table()
    if not table:
        return None
    values = {
        "strategy": (strategy or "unclassified"),
        "asset_class": (asset_class or "unknown").lower(),
        "direction": (direction or "unknown").lower(),
        "timeframe": timeframe or "unknown",
    }
    for level in HIERARCHY:
        key = (level, tuple(values[k] for k in level))
        cell = table.get(key)
        if cell and cell["n"] >= MIN_SAMPLE:
            out = _summarise(cell, level, key[1])
            out["exact_match"] = (level == HIERARCHY[0])
            return out
    return None


# A setup must clear this much NET expectancy to be worth taking. Zero is
# not the bar: an edge indistinguishable from zero is not an edge, and
# every trade carries execution risk the model does not capture.
MIN_NET_R = 0.05

NO_TRADE = "NO_TRADE"


def evaluate(signal: dict, *, hold_hours: float | None = None,
             costs: dict | None = None) -> dict:
    """Net expectancy for one proposed trade, and whether to take it.

    Returns a verdict of TRADE or NO_TRADE with the arithmetic attached.
    A high-quality NO_TRADE is as valuable as finding a trade, so this is
    deliberately willing to say it — but never on the basis of missing
    data, only on measured negative expectancy.
    """
    symbol = signal.get("asset_symbol") or ""
    entry = _f(signal.get("entry_price"))
    stop = _f(signal.get("stop_loss"))
    direction = signal.get("direction")
    # The same rule on the pricing side. Costs are asymmetric between long
    # and short (borrow, funding, the side of the spread crossed), so
    # guessing "not short means long" prices an unknown position as a long
    # and can only be right by luck. UNKNOWN STAYS UNKNOWN.
    from lib.trade_side import SHORT, parse_side_strict
    _side = parse_side_strict(direction)
    if _side is None:
        return {"verdict": "UNKNOWN", "expectancy": None, "costs": None, "net": None,
                "reason": f"direction {direction!r} is unreadable — an unknown "
                          f"side cannot be priced"}
    is_short = _side == SHORT

    stats = lookup(signal.get("strategy"), signal.get("asset_class"),
                   direction, signal.get("timeframe"))
    if not stats:
        return {"verdict": "UNKNOWN", "reason": "no bucket has enough closed trades yet",
                "expectancy": None, "costs": None, "net": None}

    if entry is None or stop is None or entry <= 0 or abs(entry - stop) <= 0:
        return {"verdict": "UNKNOWN", "expectancy": stats, "costs": None, "net": None,
                "reason": "no risk distance — expectancy in R is not computable"}

    if costs is None:
        try:
            from lib.transaction_costs import estimate_costs
            if hold_hours is None:
                from lib.trade_horizon import expected_hold_minutes
                lo, hi = expected_hold_minutes(signal.get("timeframe"))
                hold_hours = (lo + hi) / 2.0 / 60.0
            costs = estimate_costs(
                symbol, entry, stop, hold_hours=hold_hours, is_short=is_short,
                quoted_spread_pct=_f(signal.get("spread_pct")),
                leveraged=bool(signal.get("leverage")),
            )
        except Exception as e:
            logger.debug(f"[Expectancy] cost estimate failed for {symbol}: {e}")
            costs = None

    from lib.transaction_costs import net_expected_r
    net = net_expected_r(stats["gross_expected_r"], costs or {})
    net_lower = net_expected_r(stats["gross_expected_r_lower"], costs or {})

    if net.get("net_expected_r") is None:
        verdict, reason = "UNKNOWN", net.get("reason") or "costs not computable"
    elif net["net_expected_r"] < MIN_NET_R:
        verdict = NO_TRADE
        reason = (f"net {net['net_expected_r']:+.3f}R after {net['expected_cost_r']:.3f}R "
                  f"of costs — below the {MIN_NET_R}R bar "
                  f"({stats['bucket']}, {stats['raw_sample']} trades)")
    else:
        verdict = "TRADE"
        reason = (f"net {net['net_expected_r']:+.3f}R "
                  f"({stats['p_win']:.0%} win, {stats['avg_win_r']:.2f}R up / "
                  f"{stats['avg_loss_r']:.2f}R down, {stats['bucket']}, "
                  f"{stats['raw_sample']} trades)")

    return {
        "verdict": verdict,
        "reason": reason,
        "expectancy": stats,
        "costs": costs,
        "net": net,
        # The same arithmetic at the lower bound of the win-rate interval.
        # When the point estimate says TRADE and this says otherwise, the
        # edge is inside the noise.
        "net_lower": net_lower,
        "robust": bool(net_lower.get("net_expected_r") is not None
                       and net_lower["net_expected_r"] >= MIN_NET_R),
    }


def summary() -> dict:
    """Every bucket with enough evidence, for the UI."""
    table = build_table()
    rows = []
    for (level, values), cell in table.items():
        if cell["n"] < MIN_SAMPLE:
            continue
        rows.append(_summarise(cell, level, values))
    rows.sort(key=lambda r: (-r["gross_expected_r"], -r["sample"]))
    return {"min_sample": MIN_SAMPLE, "min_net_r": MIN_NET_R,
            "buckets": rows[:60], "total_buckets": len(rows)}
