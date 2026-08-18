"""Did JARVIS decide well at T0 — not did the simulator make money.

THE QUESTION THIS ANSWERS, AND THE THREE IT REFUSES TO.

    asked      was the decision defensible on what was known at T0?
    refused    did it turn a profit?
    refused    what would hindsight have chosen?
    refused    which horizon makes the decision look best?

THE STATISTICAL UNIT IS ONE DecisionObservation. A decision schedules several
horizons — 15m, 1h, 4h — and those are REPEATED MEASUREMENTS OF ONE DECISION,
not three decisions. Counting outcome rows would inflate N by the scheduling
policy, so every decision count here is `COUNT(DISTINCT observation_id)` and
every horizon panel carries its own denominator.

WHY THE PRIMARY HORIZON IS FIXED FROM T0. Headline metrics need one outcome
per decision, and the tempting selections are all forms of hindsight: the
horizon that completed, the one with the best return, the one that happened to
resolve. Each would let the reporting choose its own conclusion. The primary
horizon is therefore derived only from what T0 knew — expected hold, else a
fixed timeframe table — and the policy is versioned so a change to it is
visible in the report rather than silent.

MISSINGNESS IS DATA. Every headline number carries usable/total. A decision
whose evidence never resolved is not dropped to make a rate look cleaner; it
appears in the denominator, because "we could not see" and "it went badly" are
different facts and only one of them is about the decision.

READ ONLY, STRUCTURALLY. Analytics open the evidence database through stdlib
sqlite3 with `mode=ro` and `query_only=ON`, import no ORM, and write nothing
back. Reports are artifacts; the evidence is not analytics state.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

REPORT_VERSION = "decision_quality_v1"
PRIMARY_HORIZON_POLICY_VERSION = "primary_horizon_v1_t0_only"

# The campaign this report describes by default.
FORWARD_EVIDENCE_ONLY = "FORWARD_EVIDENCE_ONLY"

# Resolution semantics. A perpetual outcome terminalized before strict
# contract isolation existed is NOT the same claim as one made after it, even
# when its evidence happened to be contract-clean — see the 15m control case.
STRICT_EXACT_INSTRUMENT = "STRICT_EXACT_INSTRUMENT"
PRE_STRICT_INSTRUMENT_RESOLUTION = "PRE_STRICT_INSTRUMENT_RESOLUTION"
MISSING_EXACT_INSTRUMENT = "MISSING_EXACT_INSTRUMENT"
NOT_APPLICABLE = "NOT_APPLICABLE"

STRICT_OBSERVER = "decision_outcome_observer_v2_instrument_key"
STRICT_RANGE = "range_collector_v3_instrument_key"

# Routing identity quality, so the 95 pre-fix rows stay visible rather than
# being quietly filtered out of the denominator.
NATIVE_ROUTING_IDENTITY = "NATIVE_ROUTING_IDENTITY"
LEGACY_MISSING_ROUTING_IDENTITY = "LEGACY_MISSING_ROUTING_IDENTITY"
PARTIAL_IDENTITY = "PARTIAL_IDENTITY"

# T0 -> primary horizon. A FIXED table: unknown timeframes take one explicit
# fallback rather than whichever horizon happens to have resolved.
_TIMEFRAME_TO_HORIZON = {
    "1m": "1m", "2m": "2m", "3m": "5m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "1h", "1H": "1h", "2h": "2h", "2H": "2h",
    "4h": "4h", "4H": "4h", "8h": "8h", "1d": "1d", "1D": "1d",
}
PRIMARY_HORIZON_FALLBACK = "1h"

_HORIZON_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                    "1h": 60, "2h": 120, "4h": 240, "8h": 480, "1d": 1440}

# The CURRENT expectancy floor. Named, because "the threshold" once meant a
# modelled round-trip COST of ~0.50R and that confusion invited someone to
# lower a number that does not exist.
EXPECTANCY_MIN_NET_R = 0.05
HISTORICAL_MODELLED_ROUND_TRIP_COST_R = 0.50

THRESHOLD_GRID = (0.00, 0.025, 0.05, 0.075, 0.10, 0.15,
                  0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """Read-only by construction, not by convention."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


# ── population ───────────────────────────────────────────────────────────

def population(conn, *, epoch: str, source: str = FORWARD_EVIDENCE_ONLY,
               boundary: str | None = None) -> list[sqlite3.Row]:
    """The prospective decisions this report describes.

    Legacy, backtest and replay sources are excluded from the headline
    population by construction — mixing them would make a forward claim from
    rows that were never forward.
    """
    sql = ("SELECT * FROM decision_observations "
           "WHERE source = ? AND engine_epoch = ?")
    args: list = [source, epoch]
    if boundary:
        sql += " AND decision_at >= ?"
        args.append(boundary)
    return list(conn.execute(sql + " ORDER BY decision_at", args))


def routing_identity_quality(row) -> str:
    if row["product"] and row["venue"] and row["asset_class"]:
        return NATIVE_ROUTING_IDENTITY
    if row["product"] or row["venue"] or row["asset_class"]:
        return PARTIAL_IDENTITY
    return LEGACY_MISSING_ROUTING_IDENTITY


# ── primary horizon: T0 information ONLY ─────────────────────────────────

def primary_horizon(*, timeframe: str | None,
                    expected_hold_hours: float | None) -> str:
    """The horizon this decision is judged on, decided at T0.

    Never a function of outcomes. Feeding this anything the market did later
    would let the report pick the horizon that flatters the decision.
    """
    if expected_hold_hours:
        minutes = float(expected_hold_hours) * 60.0
        return min(_HORIZON_MINUTES,
                   key=lambda h: abs(_HORIZON_MINUTES[h] - minutes))
    return _TIMEFRAME_TO_HORIZON.get(str(timeframe or ""),
                                     PRIMARY_HORIZON_FALLBACK)


def resolution_quality(outcome) -> str:
    """Which resolver semantics produced this terminal claim."""
    if outcome is None:
        return NOT_APPLICABLE
    if str(outcome["product"] or "") != "CRYPTO_PERP":
        return NOT_APPLICABLE
    if not outcome["instrument_id"]:
        return MISSING_EXACT_INSTRUMENT
    if (outcome["observer_version"] == STRICT_OBSERVER
            and outcome["range_source"] == STRICT_RANGE):
        return STRICT_EXACT_INSTRUMENT
    return PRE_STRICT_INSTRUMENT_RESOLUTION


# ── binding constraint ───────────────────────────────────────────────────

EDGE, COST, UNCERTAINTY = "EDGE", "COST", "UNCERTAINTY"
RISK, CAPABILITY, DATA = "RISK", "CAPABILITY", "DATA"
ACCOUNT_STATE, UNCLASSIFIED, NONE = "ACCOUNT_STATE", "UNCLASSIFIED", "NONE"


def binding_constraint(row) -> str:
    """What actually refused this decision.

    THE DIAGNOSTIC RULE. A measured edge that never had refusal authority
    cannot be reported as the binding constraint. The live AMD case is the
    permanent example: net edge ~0.02R against a 0.05R floor with a negative
    lower bound, and the decision was still TRADE — because on that path the
    edge was DIAGNOSTIC. Reporting EDGE there would invent a causal claim the
    pipeline never made.
    """
    stored = row["binding_constraint"]
    if stored == EDGE and row["edge_gate_role"] != "BINDING":
        return UNCLASSIFIED
    return stored or (NONE if row["final_decision"] == "TRADE" else UNCLASSIFIED)


# ── favourability ────────────────────────────────────────────────────────
#
# FAVORABLE_AFTER_REJECTION, never FALSE_NEGATIVE. A market moving the right
# way after a refusal does not prove the trade was executable or permitted:
# cost, uncertainty, risk, capability and data quality can each make execution
# unjustified at T0 while the price still goes on to move.

def favorability(outcome) -> dict:
    if outcome is None:
        return {"market_direction": None, "side_reference": None}
    md = outcome["direction_adjusted_mid_return_pct"]
    sr = outcome["side_reference_return_pct"]
    return {"market_direction": None if md is None else md > 0,
            "side_reference": None if sr is None else sr > 0}


# ── threshold sensitivity, from STORED T0 values only ────────────────────

def threshold_sensitivity(rows) -> list[dict]:
    """Descriptive only. NOT counterfactual execution.

    `WOULD_CLEAR_POINT_EDGE_AT_T` says the stored T0 edge cleared a bar. It
    does NOT say the candidate would have traded — risk, capability, AI
    judgement, account state and data quality all sit downstream of edge and
    each could still have refused it.
    """
    out = []
    for t in THRESHOLD_GRID:
        point = [r for r in rows if r["expected_net_r"] is not None]
        robust = [r for r in rows if r["net_expected_r_lower"] is not None]
        out.append({
            "threshold_r": t,
            "point_n": len(point),
            "would_clear_point_edge_at_t": sum(
                1 for r in point if r["expected_net_r"] >= t),
            "robust_n": len(robust),
            "would_clear_robust_edge_at_t": sum(
                1 for r in robust if r["net_expected_r_lower"] >= t),
            "is_current_policy": abs(t - EXPECTANCY_MIN_NET_R) < 1e-9,
        })
    return out


# ── distributions ────────────────────────────────────────────────────────

def _dist(values: list[float]) -> dict:
    """Percentiles only where the sample can carry them."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0}
    def pct(p):
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    d = {"n": len(vals), "min": vals[0], "max": vals[-1],
         "median": vals[len(vals) // 2]}
    if len(vals) >= 4:
        d.update({"p25": pct(0.25), "p75": pct(0.75)})
    if len(vals) >= 20:
        d.update({"p90": pct(0.90), "p95": pct(0.95)})
    return d


def _counts(items) -> dict:
    out: dict = {}
    for i in items:
        k = str(i) if i is not None else "UNKNOWN"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def outcomes_for(conn, observation_ids: list[str]) -> dict:
    if not observation_ids:
        return {}
    q = ("SELECT * FROM decision_observation_outcomes WHERE observation_id IN "
         "(" + ",".join("?" * len(observation_ids)) + ")")
    by: dict = {}
    for r in conn.execute(q, observation_ids):
        by.setdefault(r["observation_id"], {})[r["horizon"]] = r
    return by


def build_report(db_path: str, *, epoch: str, boundary: str | None = None,
                 source: str = FORWARD_EVIDENCE_ONLY) -> dict:
    conn = connect(db_path)
    try:
        rows = population(conn, epoch=epoch, source=source, boundary=boundary)
        ids = [r["observation_id"] for r in rows]
        outcomes = outcomes_for(conn, ids)

        identity = _counts(routing_identity_quality(r) for r in rows)
        verdicts = _counts(r["final_decision"] for r in rows)
        constraints = _counts(binding_constraint(r) for r in rows)

        primary, res_quality, states = {}, [], []
        mfe, mae, mid_ret, side_ret, chrono = [], [], [], [], []
        fav_md = fav_sr = 0
        fav_md_n = fav_sr_n = 0
        for r in rows:
            h = primary_horizon(timeframe=r["timeframe"],
                                expected_hold_hours=r["expected_hold_hours"])
            primary[r["observation_id"]] = h
            o = outcomes.get(r["observation_id"], {}).get(h)
            states.append(o["status"] if o is not None else "NOT_SCHEDULED")
            res_quality.append(resolution_quality(o))
            if o is None or o["status"] != "COMPLETE":
                continue
            mfe.append(o["mfe_pct"]); mae.append(o["mae_pct"])
            mid_ret.append(o["direction_adjusted_mid_return_pct"])
            side_ret.append(o["side_reference_return_pct"])
            chrono.append(o["touch_order"])
            f = favorability(o)
            if f["market_direction"] is not None:
                fav_md_n += 1; fav_md += bool(f["market_direction"])
            if f["side_reference"] is not None:
                fav_sr_n += 1; fav_sr += bool(f["side_reference"])

        horizon_curve = {}
        for oid, hs in outcomes.items():
            for h, o in hs.items():
                c = horizon_curve.setdefault(
                    h, {"measurements": 0, "complete": 0, "decisions": set()})
                c["measurements"] += 1
                c["decisions"].add(oid)
                c["complete"] += (o["status"] == "COMPLETE")
        for h, c in horizon_curve.items():
            c["distinct_decisions"] = len(c.pop("decisions"))

        return {
            "meta": {
                "report_version": REPORT_VERSION,
                "generated_at": _now(),
                "db_path": db_path,
                "epoch": epoch,
                "activation_boundary": boundary,
                "source": source,
                "primary_horizon_policy_version": PRIMARY_HORIZON_POLICY_VERSION,
                "expectancy_min_net_r": EXPECTANCY_MIN_NET_R,
                "historical_modelled_round_trip_cost_r":
                    HISTORICAL_MODELLED_ROUND_TRIP_COST_R,
                "strict_observer_version": STRICT_OBSERVER,
                "strict_range_version": STRICT_RANGE,
                "policy_changes_authorized": False,
            },
            "denominators": {
                "total_decisions": len(rows),
                "decisions_with_any_outcome": len(outcomes),
                "primary_horizon_scheduled": sum(
                    1 for s in states if s != "NOT_SCHEDULED"),
                "primary_horizon_complete": sum(
                    1 for s in states if s == "COMPLETE"),
                "outcome_rows_total": sum(len(h) for h in outcomes.values()),
            },
            "routing_identity_quality": identity,
            "resolution_quality": _counts(res_quality),
            "verdicts": verdicts,
            "binding_constraints": constraints,
            "primary_horizon_states": _counts(states),
            "horizon_curve": horizon_curve,
            "favorable_after_rejection": {
                "market_direction_favorable": fav_md,
                "market_direction_n": fav_md_n,
                "side_reference_favorable": fav_sr,
                "side_reference_n": fav_sr_n,
                "label": "FAVORABLE_AFTER_REJECTION",
                "note": ("a favourable move after a refusal does not prove "
                         "the trade was executable or permitted at T0"),
            },
            "returns": {
                "direction_adjusted_mid_pct": _dist(mid_ret),
                "side_reference_pct": _dist(side_ret),
            },
            "excursions": {"mfe_pct": _dist(mfe), "mae_pct": _dist(mae)},
            "chronology": _counts(chrono),
            "threshold_sensitivity": threshold_sensitivity(rows),
        }
    finally:
        conn.close()
