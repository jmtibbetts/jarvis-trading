"""
Fusion & scoring layer — combines the intelligence modules built earlier this
session (insider clusters, dark-pool activity, options chain, crypto
derivatives) with the existing TA/regime-based signal score
(lib/signal_scorer.py's composite_score) into a single ranked opportunity
view. This is pure aggregation over data already collected — no new
external calls, no LLM.

Design discipline carried over from every module this session:
  - FINRA dark-pool volume has NO buy/sell direction in the data itself
    (it's an aggregate crossing-network report) — it contributes an
    unsigned "activity" score, never a bullish/bearish tilt. Scoring it as
    directional would be exactly the fabrication the mega-prompt's own
    instructions explicitly forbid ("never automatically classify every
    off-exchange transaction as bullish or bearish").
  - Insider Form 4 buys/sells ARE directional (a literal buy or sell
    decision), so that component IS signed.
  - Options put/call ratio is a conventional, widely-used sentiment proxy
    (not this module's invention) — signed, but modest in magnitude since
    it's a convention, not a certainty (e.g. puts can be protective hedges,
    not bearish bets).
  - Crypto funding/OI/liquidations are deliberately NOT folded into
    "smart money alignment" here — that framing belongs to insider/
    institutional/politician-style equity data. Crypto's data is shown
    descriptively (positioning, liquidation skew) rather than scored,
    to avoid overclaiming what funding rate actually predicts.
  - Every composite score ships with its component breakdown — nothing is
    hidden behind one number, per the mega-prompt's explicit
    "show both composite and components" signal-fusion requirement.
"""
from __future__ import annotations


def compute_insider_component(cluster: dict | None) -> dict | None:
    """Signed -100..100 from a ticker's insider_analytics.cluster_summary
    net_value (buy_value - sell_value). $2M+ net reaches full magnitude —
    calibrated to typical open-market Form 4 transaction sizes, not a
    universal constant."""
    if not cluster:
        return None
    net = cluster.get("net_value") or 0
    if net == 0:
        return {"score": 0.0, "net_value": 0.0, "flags": cluster.get("flags", [])}
    magnitude = min(100.0, abs(net) / 2_000_000 * 100)
    if "OFFICER_BUYING" in (cluster.get("flags") or []):
        magnitude = min(100.0, magnitude + 10)
    score = magnitude if net > 0 else -magnitude
    return {"score": round(score, 1), "net_value": round(net, 2), "flags": cluster.get("flags", [])}


def compute_options_component(options_summary: dict | None) -> dict | None:
    """Signed -100..100 from put/call ratio — the standard options-sentiment
    convention (ratio 1.0 = neutral). Deliberately modest scaling (x80, not
    x100+) since this is a convention, not a certainty."""
    if not options_summary:
        return None
    ratio = options_summary.get("put_call_ratio")
    if ratio is None:
        return {"score": None, "detail": "no traded contracts to compute a ratio"}
    score = max(-100.0, min(100.0, (1 - ratio) * 80))
    return {"score": round(score, 1), "put_call_ratio": ratio, "iv_skew": options_summary.get("iv_skew")}


def compute_dark_pool_component(row: dict | None) -> dict | None:
    """UNSIGNED 0..100 activity/attention score from week-over-week share
    volume change. No direction — see module docstring for why."""
    if not row:
        return None
    wow = row.get("wow_pct")
    if wow is None:
        return {"activity_score": 30.0, "wow_pct": None, "detail": "present in top activity, no prior-week baseline"}
    activity_score = min(100.0, 30 + abs(wow) * 0.5)
    return {"activity_score": round(activity_score, 1), "wow_pct": wow}


def compute_smart_money_alignment(
    insider: dict | None = None, dark_pool: dict | None = None, options: dict | None = None,
) -> dict:
    """Combines the DIRECTIONAL components (insider, options) into a single
    0..100 alignment_score (50 = neutral/no data, >50 = bullish tilt, <50 =
    bearish tilt). dark_pool is reported separately as non-directional
    attention — never merged into the directional average."""
    directional = {}
    if insider is not None and insider.get("score") is not None:
        directional["insider"] = insider["score"]
    if options is not None and options.get("score") is not None:
        directional["options"] = options["score"]

    if not directional:
        alignment_score = None
        net_score = None
        agreement = "no_data"
    else:
        net_score = sum(directional.values()) / len(directional)
        alignment_score = round(50 + net_score / 2, 1)
        if len(directional) == 1:
            agreement = "single_source"
        else:
            signs = {1 if v > 0 else -1 if v < 0 else 0 for v in directional.values()}
            agreement = "aligned" if len(signs - {0}) <= 1 else "mixed"

    return {
        "alignment_score": alignment_score,
        "net_directional_score": round(net_score, 1) if net_score is not None else None,
        "agreement": agreement,
        "sources_available": len(directional) + (1 if dark_pool else 0),
        "components": {
            "insider": insider,
            "options": options,
            "dark_pool_activity": dark_pool,
        },
    }


def compute_opportunity_score(signal_composite_score: float, direction: str, smart_money: dict | None = None,
                               historical: dict | None = None) -> dict:
    """JARVIS_OPPORTUNITY_SCORE: the existing TA/regime/RR composite_score
    (already computed per-signal by lib/signal_scorer.py) adjusted by
    whether smart-money alignment CONFIRMS or CONFLICTS with the signal's
    own direction, plus a modest nudge from that symbol's historical win
    rate (lib/learning_engine's signal_accuracy table). Every adjustment is
    returned alongside the composite so nothing is hidden behind one
    number.

    historical, when given, is a signal_accuracy row: its "win_rate" is a
    FRACTION (0.0-1.0), not a percentage — see the note at its use below."""
    base = max(0.0, min(100.0, float(signal_composite_score or 0)))
    is_long = str(direction or "").lower().startswith("long")

    smart_money_adj = 0.0
    smart_money_note = "no smart-money data available"
    alignment = (smart_money or {}).get("alignment_score")
    if alignment is not None:
        tilt = alignment - 50  # -50..+50, positive = bullish tilt
        confirms = (is_long and tilt > 0) or (not is_long and tilt < 0)
        conflicts = (is_long and tilt < 0) or (not is_long and tilt > 0)
        magnitude = abs(tilt) * 0.3  # cap ~15 either way
        if confirms:
            smart_money_adj = magnitude
            smart_money_note = "smart-money signals confirm signal direction"
        elif conflicts:
            smart_money_adj = -magnitude
            smart_money_note = "smart-money signals conflict with signal direction"
        else:
            smart_money_note = "smart-money signals neutral"

    # NOTE: historical["win_rate"] is a FRACTION (0.0-1.0), matching how
    # lib/learning_engine.py's signal_accuracy table actually stores it
    # (wins / total) — not a 0-100 percentage.
    # Historical win rate is NO LONGER added here. It already moved this
    # signal once, via calibrated_confidence inside signal_scorer — and
    # since calibrated confidence is a component of the composite score,
    # and the composite score is `base` below, adding it again compounded
    # one piece of evidence into two movements of the same number.
    # The record is still SHOWN, because an operator should see it; it just
    # no longer votes twice. See lib/historical_edge.py.
    from lib.historical_edge import describe_for_ui
    historical_adj = 0.0
    historical_note = describe_for_ui(historical) + " (already counted in the base score)"

    opportunity_score = round(max(0.0, min(100.0, base + smart_money_adj + historical_adj)), 1)
    return {
        "opportunity_score": opportunity_score,
        "breakdown": {
            "base_composite_score": round(base, 1),
            "smart_money_adjustment": round(smart_money_adj, 1),
            "smart_money_note": smart_money_note,
            "historical_adjustment": round(historical_adj, 1),
            "historical_note": historical_note,
        },
    }


def compute_anomaly_flags(dark_pool: dict | None = None, liquidation_summary: dict | None = None,
                           options_summary: dict | None = None) -> dict:
    """Unusual-activity flags across the modules that have a meaningful
    historical-baseline comparison available. Returns the flag list AND a
    0..100 score (flags triggered / flags evaluable) — never a single
    number with no explanation attached."""
    flags = []
    evaluable = 0

    if dark_pool is not None:
        evaluable += 1
        wow = dark_pool.get("wow_pct")
        if wow is not None and abs(wow) >= 50:
            flags.append({"flag": "DARK_POOL_WOW_SPIKE", "detail": f"{wow:+.1f}% week-over-week off-exchange volume"})

    if liquidation_summary is not None:
        evaluable += 1
        total = liquidation_summary.get("total_liquidated_usd") or 0
        if total >= 5_000_000:
            flags.append({"flag": "LARGE_LIQUIDATION_CLUSTER", "detail": f"${total:,.0f} liquidated in window"})

    if options_summary is not None:
        evaluable += 1
        ratio = options_summary.get("put_call_ratio")
        if ratio is not None and (ratio >= 2.5 or ratio <= 0.35):
            flags.append({"flag": "EXTREME_PUT_CALL_RATIO", "detail": f"put/call ratio {ratio}"})

    return {
        "flags": flags,
        "anomaly_score": round(len(flags) / evaluable * 100, 1) if evaluable else None,
        "sources_evaluated": evaluable,
    }
