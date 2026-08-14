"""The two live-eligibility gates, side by side — one experiment, one winner.

For months live capital was gated and RANKED by
`coalesce(composite_score, confidence) >= live_min_score` — and the
composite is measured INVERTED against outcomes (80+ band: 30.2% win rate
over 222 trades; <60 band: 53.3% over 2,249; MFE falling monotonically as
the score rises). The operator's decision: demote it, but do not delete it
— run both gates over the same candidate stream and let resolved outcomes
pick the winner.

  gate_legacy   what the old query would have done — recorded on every
                candidate, EXECUTES NOTHING.
  gate_v8       validity first (strict side, coherent levels), then the
                measured-expectancy verdict with its robust lower bound.
                This is the arm that actually trades.

Both verdicts persist on candidate_signals at creation, both arms are
judged by the same counterfactual resolver, and the scoreboard reports
them head-to-head. Symmetric by design: if legacy out-selects v8
out-of-sample, it earns its authority back through the same promotion
test. See HARDENING_PLAN.md, "The gate experiment".

UNKNOWN expectancy is paper/sim by default (P0.10): the desk's job is to
generate evidence there, not to spend live capital learning what it could
learn for free. ALLOW_EXPERIMENTAL_LIVE=1 is the explicit operator
override.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

TRADE = "TRADE"
TENTATIVE = "TENTATIVE"       # point estimate clears, lower bound does not
NO_TRADE = "NO_TRADE"
UNKNOWN = "UNKNOWN"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def allow_experimental_live() -> bool:
    return os.getenv("ALLOW_EXPERIMENTAL_LIVE", "").lower() in ("1", "true", "yes")


def gate_legacy(signal: dict, live_min_score: float = 55.0) -> dict:
    """The old gate, verbatim, as a recorder: coalesce(score, confidence)
    against the operator threshold. Kept byte-compatible with the query it
    replaced so the experiment measures the REAL historical policy, not a
    reconstruction of it."""
    score = _f(signal.get("composite_score"))
    if score is None:
        score = _f(signal.get("confidence")) or 0.0
    return {"take": score >= live_min_score, "score": score,
            "threshold": live_min_score}


def gate_v8(signal: dict) -> dict:
    """Validity, then measured edge. Returns decision + take + the numbers.

    take=True requires ALL of:
      - an affirmatively parsed side and coherent stop/target layout
      - expectancy verdict TRADE (net expected R above the bar)
      - the ROBUST condition: the same arithmetic at the lower confidence
        bound of the win rate still clears the bar. When the point
        estimate says TRADE and the lower bound disagrees, the edge is
        inside the noise — that is TENTATIVE, and TENTATIVE trades paper,
        not live (P0.11: do not compute uncertainty and then ignore it at
        the capital boundary).
    """
    from lib.trade_side import parse_side_strict, validate_levels

    side = parse_side_strict(signal.get("direction"))
    if side is None:
        return {"decision": NO_TRADE, "take": False,
                "reason": f"unparseable direction {signal.get('direction')!r}"}

    ok, why = validate_levels(signal.get("direction"), signal.get("entry_price"),
                              signal.get("stop_loss"), signal.get("target_price"))
    if not ok:
        return {"decision": NO_TRADE, "take": False, "reason": f"levels: {why}"}

    try:
        from lib.expectancy import evaluate
        ev = evaluate(signal)
    except Exception as e:
        # The edge engine failing is not a license to trade blind.
        logger.warning(f"[Gate] expectancy unavailable: {e}")
        return {"decision": UNKNOWN, "take": allow_experimental_live(),
                "reason": f"expectancy engine unavailable: {e}"}

    verdict = ev.get("verdict")
    net = (ev.get("net") or {}).get("net_expected_r")
    net_lower = (ev.get("net_lower") or {}).get("net_expected_r")

    if verdict == "TRADE":
        if ev.get("robust"):
            return {"decision": TRADE, "take": True, "reason": ev.get("reason"),
                    "net_r": net, "net_r_lower": net_lower}
        return {"decision": TENTATIVE, "take": False,
                "reason": (f"point net {net:+.3f}R clears but lower bound "
                           f"{net_lower if net_lower is None else format(net_lower, '+.3f')}R "
                           "does not — edge inside the noise; paper collects it"),
                "net_r": net, "net_r_lower": net_lower}
    if verdict == "NO_TRADE":
        return {"decision": NO_TRADE, "take": False, "reason": ev.get("reason"),
                "net_r": net, "net_r_lower": net_lower}

    # UNKNOWN: no bucket has enough evidence. Paper/sim generate that
    # evidence for free; live capital does not (P0.10).
    take = allow_experimental_live()
    return {"decision": UNKNOWN, "take": take,
            "reason": (ev.get("reason") or "no measured evidence")
                      + (" [ALLOW_EXPERIMENTAL_LIVE override]" if take else ""),
            "net_r": net, "net_r_lower": net_lower}


def record_both(signal: dict, live_min_score: float = 55.0) -> dict:
    """Both verdicts for candidate persistence, small and JSON-friendly."""
    legacy = gate_legacy(signal, live_min_score)
    v8 = gate_v8(signal)
    return {
        "gate_legacy_take": bool(legacy["take"]),
        "gate_v8_decision": v8["decision"],
        "gate_v8_take": bool(v8["take"]),
        "gate_v8_reason": (v8.get("reason") or "")[:300],
        "gate_v8_net_r": v8.get("net_r"),
    }
