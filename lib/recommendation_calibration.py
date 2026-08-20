"""How accurate are JARVIS'S PREDICTIONS, measured against real venue evidence.

WHY A NEW CONSUMER RATHER THAN AN EXISTING ONE. Every learning aggregate this
system already has answers a variant of "did the trade make money", and none
of them can separate a sound recommendation from good execution:

    calibration          win rate by score/timeframe, from JARVIS'S OWN fills
    expectancy           R buckets for SIZING, from JARVIS's own fills
    signal_accuracy      win rate by symbol, from the same rows
    edge_cost_matrix     venue BASELINE cost realism
    strategy_lifecycle   promotes and retires JARVIS's strategies
    decision_quality     needs a DecisionObservation with scheduled forward
                         horizons; a manual trade has neither

Feeding a manually executed trade into any of them attributes a PERSON'S
entry timing, venue choice and exit discipline to the executor being
measured. That is the contamination `lib/learning_population` exists to
prevent, and it is not fixed by relabelling the row.

WHAT A MANUAL TRADE HONESTLY PROVES. It separates prediction from execution
BY CONSTRUCTION: JARVIS made the claim, and someone else produced the fills.
So the learnable quantity is not a win rate — it is PREDICTION ERROR, every
term of which is measured rather than inferred:

    recommended entry   vs   the price actually paid
    expected fee        vs   the fee the venue actually charged
    expected funding    vs   the funding actually settled
    expected R          vs   the R actually realized
    recommended venue   vs   the venue actually used
    recommended side    vs   the side actually taken

None of that needs the market path, which is exactly why it is safe. A
question like "would the recommendation have hit its target?" DOES need the
path, this system does not have it for a manual trade, and answering it from
the operator's two fills would be fabrication. So it is not answered.

THE OPPOSED CASE IS RECORDED, NEVER INVERTED. If JARVIS said long and the
operator went short and made money, it is tempting to score that as evidence
against the long. It is not sound: the operator's holding window is not
JARVIS's horizon, and the two claims are about different intervals. Opposed
trades get their own class and their own counters, and are NEVER pooled into
the followed statistics in either direction.

AGGREGATES ARE RECOMPUTED, NEVER INCREMENTED. Every number here is derived
from the sample rows at read time. That is what makes a correction safe: a
re-projection updates one row and every derived figure follows, with no
counter to unwind. It is the same discipline `_refresh_signal_accuracy_conn`
uses, for the same reason.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RECOMMENDATION_CALIBRATION_VERSION = "recommendation_calibration_v1"

# ── Deviation classes ────────────────────────────────────────────────────
FOLLOWED_AS_RECOMMENDED = "FOLLOWED_AS_RECOMMENDED"
FOLLOWED_DIFFERENT_VENUE = "FOLLOWED_DIFFERENT_VENUE"
OPPOSED_DIRECTION = "OPPOSED_DIRECTION"
DIRECTION_UNSTATED = "DIRECTION_UNSTATED"

DEVIATION_CLASSES = (FOLLOWED_AS_RECOMMENDED, FOLLOWED_DIFFERENT_VENUE,
                     OPPOSED_DIRECTION, DIRECTION_UNSTATED)

#: Classes whose realized result may inform DIRECTIONAL recommendation
#: quality. Opposed and unstated are deliberately absent.
FOLLOWED_CLASSES = frozenset({FOLLOWED_AS_RECOMMENDED,
                              FOLLOWED_DIFFERENT_VENUE})

# ── Cost evidence scope — the promotion normalization ────────────────────
#: The account paid the venue's ordinary economics, so a cost error here is
#: evidence about THE MODEL.
VENUE_BASELINE = "VENUE_BASELINE"
#: A waiver or promotion applied. The cost error is evidence about THIS
#: ACCOUNT and says NOTHING about the venue's published schedule, so it is
#: excluded from every venue-scoped cost statistic. A promotional zero may
#: teach "this account paid zero"; it may never teach "this venue is free".
ACCOUNT_PROMOTIONAL = "ACCOUNT_PROMOTIONAL"

COST_SCOPES = (VENUE_BASELINE, ACCOUNT_PROMOTIONAL)

# ── Refusals ─────────────────────────────────────────────────────────────
REFUSED_NO_THESIS = "REFUSED_NO_THESIS"
REFUSED_NO_RECOMMENDATION = "REFUSED_NO_RECOMMENDATION"
REFUSED_THESIS_ALREADY_CONTRIBUTED = "REFUSED_THESIS_ALREADY_CONTRIBUTED"


class RecommendationCalibrationError(ValueError):
    """A sample that cannot be recorded honestly."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v):
    """Finite float, or None. NaN and infinity are not measurements."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ratio(actual, expected):
    """actual / expected, or None.

    A ratio against a ZERO expectation is undefined, not infinite: "we
    predicted nothing and were charged $6" is a real finding, and it is
    reported as the DEVIATION in dollars rather than as an infinite ratio
    that would poison every average it entered.
    """
    a, e = _f(actual), _f(expected)
    if a is None or e is None or e == 0:
        return None
    return a / e


def classify(trade) -> str:
    """How far the operator's execution departed from the recommendation."""
    rec = trade.recommendation
    if rec is None or not rec.direction:
        return DIRECTION_UNSTATED
    from lib.trade_side import parse_side_strict

    recommended = parse_side_strict(rec.direction)
    if recommended is None:
        return DIRECTION_UNSTATED
    if recommended != trade.direction:
        return OPPOSED_DIRECTION
    same_venue = (str(rec.venue or "").upper() == str(trade.venue or "").upper()
                  if rec.venue else True)
    return FOLLOWED_AS_RECOMMENDED if same_venue else FOLLOWED_DIFFERENT_VENUE


def cost_evidence_scope(trade) -> str:
    """Whether this trade's COSTS may speak for the venue or only the account.

    A declared-absent cost category is exactly a promotion or waiver: the
    operator stated the venue charged nothing. That is true of the account
    and false of the schedule.
    """
    return (ACCOUNT_PROMOTIONAL if trade.declared_absent_costs
            else VENUE_BASELINE)


def _entry_deviation_bps(trade, recommended_entry) -> float | None:
    """Signed so POSITIVE ALWAYS MEANS WORSE FOR THE TRADE.

    A long that paid MORE than planned and a short that sold LOWER than
    planned are the same kind of error, and an unsigned or naively signed
    figure would cancel them against each other in any average.
    """
    from lib.trade_side import SHORT

    rec, actual = _f(recommended_entry), _f(trade.entry_vwap)
    if rec is None or actual is None or rec <= 0:
        return None
    raw_bps = (actual - rec) / rec * 10_000.0
    return -raw_bps if trade.direction == SHORT else raw_bps


def build_sample(trade, outcome) -> dict:
    """The measured comparison for one thesis. PURE — no DB, no market.

    REFUSES AN UNLINKED TRADE. There is no prediction to score, and
    inventing one would score the system against a claim it never made.
    """
    rec = trade.recommendation
    if rec is None:
        raise RecommendationCalibrationError(
            f"{REFUSED_NO_RECOMMENDATION}: trade {trade.trade_id} carries no "
            f"recommendation, so there is no prediction to measure against")
    if not rec.thesis_id:
        raise RecommendationCalibrationError(
            f"{REFUSED_NO_THESIS}: the recommendation names no thesis, and "
            f"calibration counts distinct theses")

    from lib import learning_population as LP
    from lib.account_economics import is_owned_capital

    costs = trade.costs_usd()
    actual_fee = _f(costs.get("commission_usd"))
    actual_funding = _f(costs.get("funding_usd"))
    actual_cost = _f(trade.explicit_costs_usd)
    realized_r = _f(trade.net_r)

    expected_fee = _f(rec.expected_fee_usd)
    expected_funding = _f(rec.expected_funding_usd)
    expected_cost = _f(rec.expected_cost_usd)
    expected_r = _f(rec.expected_r)

    def _dev(actual, expected):
        return (None if actual is None or expected is None
                else actual - expected)

    klass = classify(trade)
    return {
        "thesis_id": rec.thesis_id,
        "manual_trade_id": trade.trade_id,
        "signal_id": rec.signal_id,
        "population": LP.MANUAL_OPERATOR,

        "venue_recommended": rec.venue,
        "venue_actual": trade.venue,
        "venue_followed": (None if not rec.venue else
                           str(rec.venue).upper() == str(trade.venue).upper()),
        "product_recommended": rec.product,
        "product_actual": trade.product,
        "direction_recommended": rec.direction,
        "direction_actual": trade.direction,
        "direction_followed": (klass in FOLLOWED_CLASSES
                               if klass != DIRECTION_UNSTATED else None),
        "deviation_class": klass,

        "entry_recommended": _f(rec.entry),
        "entry_actual": _f(trade.entry_vwap),
        "entry_deviation_bps": _entry_deviation_bps(trade, rec.entry),

        "expected_fee_usd": expected_fee,
        "actual_fee_usd": actual_fee,
        "fee_deviation_usd": _dev(actual_fee, expected_fee),
        "fee_ratio": _ratio(actual_fee, expected_fee),

        "expected_funding_usd": expected_funding,
        "actual_funding_usd": actual_funding,
        "funding_deviation_usd": _dev(actual_funding, expected_funding),

        "expected_cost_usd": expected_cost,
        "actual_cost_usd": actual_cost,
        "cost_deviation_usd": _dev(actual_cost, expected_cost),

        "expected_r": expected_r,
        "realized_r": realized_r,
        "r_deviation": _dev(realized_r, expected_r),

        "account_label": trade.account_label,
        "cost_evidence_scope": cost_evidence_scope(trade),
        "promotional_capital": not is_owned_capital(
            trade.collateral_capital_kind),

        "outcome": outcome.outcome,
        "net_pnl_usd": _f(outcome.net_pnl_usd),
        "confidence": _f(rec.confidence),

        "engine_epoch": trade.engine_epoch,
        "model_version": RECOMMENDATION_CALIBRATION_VERSION,
    }


# ── Persistence: exactly one row per thesis ──────────────────────────────
_TRACKED = ("venue_recommended", "venue_actual", "venue_followed",
            "product_recommended", "product_actual",
            "direction_recommended", "direction_actual",
            "direction_followed", "deviation_class",
            "entry_recommended", "entry_actual", "entry_deviation_bps",
            "expected_fee_usd", "actual_fee_usd", "fee_deviation_usd",
            "fee_ratio", "expected_funding_usd", "actual_funding_usd",
            "funding_deviation_usd", "expected_cost_usd", "actual_cost_usd",
            "cost_deviation_usd", "expected_r", "realized_r", "r_deviation",
            "account_label", "cost_evidence_scope", "promotional_capital",
            "outcome", "net_pnl_usd", "confidence")


def record(conn, sample: dict) -> dict:
    """Write or SUPERSEDE this thesis's single contribution.

    Takes the CALLER'S connection so the calibration sample and the learning
    row commit together — a learning row whose calibration contribution
    silently failed would be a quiet divergence between two records of the
    same event.

    A SECOND MANUAL TRADE ON THE SAME THESIS IS REFUSED BY NAME. One thesis
    is one market observation however many times the operator acted on it,
    and counting the second would inflate exactly the sample this exists to
    measure. Refused visibly rather than dropped silently.
    """
    import json

    from sqlalchemy import text

    from app.database import new_id

    existing = conn.execute(text(
        "SELECT id, manual_trade_id, revision FROM "
        "recommendation_calibration_samples WHERE thesis_id = :t"),
        {"t": sample["thesis_id"]}).fetchone()

    now = _now()
    if existing is not None:
        if existing[1] != sample["manual_trade_id"]:
            return {"ok": False, "result": REFUSED_THESIS_ALREADY_CONTRIBUTED,
                    "detail": (f"thesis {sample['thesis_id']!r} already "
                               f"contributed through manual trade "
                               f"{existing[1]!r}. One thesis is ONE market "
                               f"observation, however many times the "
                               f"operator acted on it"),
                    "existing_manual_trade_id": existing[1]}

        # SUPERSESSION. The prior learned values are preserved on the row
        # so a corrected contribution can be told from an original one.
        prior = conn.execute(text(
            f"SELECT {', '.join(_TRACKED)} FROM "
            f"recommendation_calibration_samples WHERE id = :i"),
            {"i": existing[0]}).fetchone()
        previous = dict(zip(_TRACKED, prior)) if prior else {}
        sets = ", ".join(f"{k} = :{k}" for k in _TRACKED)
        conn.execute(text(
            f"UPDATE recommendation_calibration_samples SET {sets}, "
            f"revision = :rev, previous_values_json = :prev, "
            f"superseded_at = :now, updated_at = :now WHERE id = :i"),
            {**{k: sample[k] for k in _TRACKED},
             "rev": int(existing[2] or 0) + 1,
             "prev": json.dumps(previous, default=str),
             "now": now, "i": existing[0]})
        return {"ok": True, "result": "SUPERSEDED", "id": existing[0],
                "revision": int(existing[2] or 0) + 1,
                "previous_values": previous}

    row_id = new_id()
    cols = ["id", "thesis_id", "manual_trade_id", "signal_id", "population",
            *_TRACKED, "engine_epoch", "model_version", "revision",
            "created_at", "updated_at"]
    values = {**sample, "id": row_id, "revision": 0,
              "created_at": now, "updated_at": now}
    conn.execute(text(
        f"INSERT INTO recommendation_calibration_samples "
        f"({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
        {c: values.get(c) for c in cols})
    return {"ok": True, "result": "RECORDED", "id": row_id, "revision": 0}


def remove_for_trade(conn, manual_trade_id: str) -> int:
    """Withdraw a contribution whose trade stopped being eligible."""
    from sqlalchemy import text

    res = conn.execute(text(
        "DELETE FROM recommendation_calibration_samples "
        "WHERE manual_trade_id = :m"), {"m": manual_trade_id})
    return int(res.rowcount or 0)


# ── The read side. RECOMPUTED, never incremented ─────────────────────────
def _dist(values: list) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        # NOT ZERO. An absent measurement has no median.
        return {"n": 0, "mean": None, "median": None,
                "p10": None, "p90": None}
    ordered = sorted(vals)
    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 6),
        "median": round(statistics.median(ordered), 6),
        "p10": round(ordered[max(0, int(len(ordered) * 0.10) - 1)], 6),
        "p90": round(ordered[min(len(ordered) - 1,
                                 int(len(ordered) * 0.90))], 6),
    }


def lookup(*, venue: str | None = None, product: str | None = None,
           account_label: str | None = None,
           engine_epoch: str | None = None) -> dict:
    """Measured prediction accuracy for the requested scope.

    THE COST FIGURES EXCLUDE PROMOTIONAL TRADES unless an account is named.
    A venue-scoped question is a question about the venue's economics, and a
    waived fee answers a question about the account instead. Asking for an
    account explicitly admits both, and says which is which.
    """
    from sqlalchemy import text

    from app.database import engine
    from lib.engine_epoch import ENGINE_EPOCH

    epoch = engine_epoch or ENGINE_EPOCH
    where = ["engine_epoch = :epoch"]
    params = {"epoch": epoch}
    if venue:
        where.append("UPPER(venue_actual) = :venue")
        params["venue"] = venue.upper()
    if product:
        where.append("product_actual = :product")
        params["product"] = product
    if account_label:
        where.append("account_label = :account")
        params["account"] = account_label

    cols = ("thesis_id, deviation_class, direction_followed, outcome, "
            "entry_deviation_bps, fee_deviation_usd, fee_ratio, "
            "funding_deviation_usd, cost_deviation_usd, r_deviation, "
            "expected_r, realized_r, cost_evidence_scope, "
            "promotional_capital, net_pnl_usd")
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT {cols} FROM recommendation_calibration_samples "
            f"WHERE {' AND '.join(where)}"), params).fetchall()

    followed = [r for r in rows if r[1] in FOLLOWED_CLASSES]
    opposed = [r for r in rows if r[1] == OPPOSED_DIRECTION]
    # Cost accuracy speaks for the VENUE only where the venue's ordinary
    # economics applied — unless an account was named.
    cost_rows = (rows if account_label
                 else [r for r in rows if r[12] == VENUE_BASELINE])
    promotional = [r for r in rows if r[12] == ACCOUNT_PROMOTIONAL]

    followed_wins = sum(1 for r in followed if r[3] == "WIN")
    return {
        "scope": {"venue": venue, "product": product,
                  "account_label": account_label, "engine_epoch": epoch},
        "population": "manual_operator",
        # DISTINCT THESES. The sample unit, not the row count.
        "theses": len({r[0] for r in rows}),
        "direction": {
            "followed": len(followed),
            "followed_wins": followed_wins,
            # None, not 0.0 — an empty population has no win rate.
            "followed_win_rate": (round(followed_wins / len(followed), 4)
                                  if followed else None),
            # NEVER pooled into the above, and never inverted into evidence
            # against the recommendation: the operator's window is not
            # JARVIS's horizon, so the two claims are about different
            # intervals and only the disagreement itself is a fact.
            "opposed": len(opposed),
            "opposed_note": ("recorded, never scored. An opposed trade's "
                             "result is not evidence for or against the "
                             "recommendation it disagreed with"),
        },
        "entry_deviation_bps": _dist([r[4] for r in followed]),
        "cost_accuracy": {
            "scope": ("ACCOUNT" if account_label else VENUE_BASELINE),
            "samples": len(cost_rows),
            "fee_deviation_usd": _dist([r[5] for r in cost_rows]),
            "fee_ratio": _dist([r[6] for r in cost_rows]),
            "funding_deviation_usd": _dist([r[7] for r in cost_rows]),
            "total_cost_deviation_usd": _dist([r[8] for r in cost_rows]),
            "promotional_samples_excluded": (
                0 if account_label else len(promotional)),
            "note": ("a promotional or waived fee is evidence about THIS "
                     "ACCOUNT, never about the venue's published schedule, "
                     "so it is excluded from a venue-scoped cost figure"),
        },
        "r_deviation": _dist([r[9] for r in followed]),
        "model_version": RECOMMENDATION_CALIBRATION_VERSION,
    }


def summary(*, engine_epoch: str | None = None) -> dict:
    """Everything recorded, grouped by the venue/product actually used."""
    from sqlalchemy import text

    from app.database import engine
    from lib.engine_epoch import ENGINE_EPOCH

    epoch = engine_epoch or ENGINE_EPOCH
    with engine.connect() as conn:
        pairs = conn.execute(text(
            "SELECT DISTINCT venue_actual, product_actual FROM "
            "recommendation_calibration_samples WHERE engine_epoch = :e"),
            {"e": epoch}).fetchall()
    return {
        "engine_epoch": epoch,
        "overall": lookup(engine_epoch=epoch),
        "by_venue_product": [
            lookup(venue=v, product=p, engine_epoch=epoch) for v, p in pairs],
        "model_version": RECOMMENDATION_CALIBRATION_VERSION,
        "note": ("built from operator-executed trades linked to a JARVIS "
                 "thesis. It measures PREDICTION ERROR, not whether JARVIS "
                 "executes well — JARVIS did not execute these"),
    }
