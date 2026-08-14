"""Score-variant promotion framework — §4.3 as code, not as a promise.

"A challenger that wins in-sample does not replace production." This module
is the machinery that enforces it. Every shadow variant (B, C, MS) is
judged against the current champion on RESOLVED CANDIDATES ONLY — the
forward stream of counterfactual outcomes that started accumulating when
the gate experiment flipped — and only on candidates created AFTER the
variant's definition was frozen. A variant can never be graded on the data
that calibrated it.

The eight §4.3 requirements, each as an explicit criterion in the output:

  chronological walk-forward   contiguous time folds; challenger must win
                               a majority of valid folds, not the average
  multiple regimes             approximated by fold consistency + per-
                               asset-class breakdown (regime labels are
                               not stored on candidates; reported honestly
                               as the approximation it is)
  enough calendar span         >= MIN_CALENDAR_DAYS between first and last
                               resolved candidate in the OOS window
  after-cost net R             replay resolutions include venue round-trip
                               fees; improvement must clear MIN_NET_R_IMPROVEMENT
  drawdown / tail-loss         p5 of the R distribution and max cumulative-R
                               drawdown, within tolerance of the champion's
  selection frequency          always reported; degenerate selectors
                               (<1% of the universe) cannot promote
  no leakage                   structural: scores are read from the
                               shadow_variants JSON stored AT BIRTH, never
                               recomputed; defined_at cutoffs exclude
                               in-sample history
  champion artifact            score_champions is append-only; promotion
                               writes a new row with the full evidence
                               frozen in; the current champion is the
                               latest row and rows are never edited

promote() records the artifact — it does NOT rewire live scoring. Nothing
in execution reads the champion table yet, deliberately: the gate
experiment (started 2026-08-14) must conclude before the composite's
composition changes, and Phase 8 owns that wiring. This module makes the
decision auditable; it does not make it automatic.

All resolutions here are counterfactual replays (perfect fills, both bar
extremes reachable) — systematically optimistic, but IDENTICALLY so for
both arms, which is what makes the comparison meaningful while the
absolute numbers are not promises.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Promotion thresholds. Deliberately conservative: the cost of keeping a
# good challenger in shadow another month is small; the cost of promoting
# a lucky one is a live book selecting on noise.
MIN_SELECTED = 25            # challenger must actually select this many
MIN_CALENDAR_DAYS = 14       # OOS window span
N_FOLDS = 3                  # chronological walk-forward folds
MIN_FOLD_SAMPLE = 8          # selected-per-arm for a fold to count
MIN_FOLDS_WON = 2            # majority of valid folds
MIN_NET_R_IMPROVEMENT = 0.05  # after-cost mean net R, challenger - champion
TAIL_P5_TOLERANCE_R = 0.25   # challenger p5 may be at most this much worse
DD_TOLERANCE = (1.25, 0.5)   # challenger max drawdown <= champ*1.25 + 0.5R
MIN_SELECTION_FREQ = 0.01    # selecting <1% of the universe is untradeable

FOUNDING_CHAMPION = "A"      # the live composite — the control everything
                             # must beat, exactly as it runs today


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _net_r(pnl_pct, entry, stop) -> float | None:
    """After-cost R from a resolved candidate.

    replay_signal sizes qty = margin/entry, so pnl_pct is a net (fee-
    inclusive) price-return percent; dividing by the stop distance as a
    percent of entry recovers R exactly.
    """
    p, e, s = _f(pnl_pct), _f(entry), _f(stop)
    if p is None or not e or s is None:
        return None
    risk_pct = abs(e - s) / abs(e) * 100.0
    if risk_pct <= 0:
        return None
    return p / risk_pct


def _p5(rs: list[float]) -> float | None:
    if not rs:
        return None
    xs = sorted(rs)
    k = max(0, int(0.05 * (len(xs) - 1)))
    return xs[k]


def _max_drawdown(rs_chronological: list[float]) -> float:
    """Max peak-to-trough of cumulative R, in R units (>= 0)."""
    peak = cum = 0.0
    worst = 0.0
    for r in rs_chronological:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def _arm_stats(selected: list[tuple[str, float]]) -> dict:
    """Stats for one arm's selected set: [(created_at, net_r), ...]."""
    n = len(selected)
    if n == 0:
        return {"n": 0}
    ordered = [r for _, r in sorted(selected, key=lambda t: t[0])]
    wins = sum(1 for r in ordered if r > 0)
    return {
        "n": n,
        "win_rate": round(100.0 * wins / n, 1),
        "mean_net_r": round(sum(ordered) / n, 4),
        "p5_net_r": round(_p5(ordered), 4),
        "max_drawdown_r": round(_max_drawdown(ordered), 4),
    }


def _variant_score(row: dict, variant: str) -> float | None:
    """The variant's score for this candidate AS STORED AT BIRTH.

    A is the composite column; everything else comes out of the
    shadow_variants JSON written when the candidate was recorded. Nothing
    is ever recomputed from the breakdown here — recomputing with today's
    code against yesterday's rows is exactly the leakage §4.3 forbids.
    """
    if variant == "A":
        return _f(row.get("composite_score"))
    sv = row.get("_shadow")
    if not isinstance(sv, dict):
        return None
    return _f(sv.get(variant))


def _load_universe():
    """All resolved candidates with the fields promotion needs."""
    from sqlalchemy import text

    from app.database import engine

    rows = []
    with engine.connect() as c:
        for cid, created, comp, sv, entry, stop, pnl, ac in c.execute(text("""
            SELECT id, created_at, composite_score, shadow_variants,
                   entry_price, stop_loss, pnl_pct, asset_class
            FROM candidate_signals
            WHERE resolved = 1 AND pnl_pct IS NOT NULL
              AND entry_price IS NOT NULL AND stop_loss IS NOT NULL
            ORDER BY created_at ASC
        """)):
            try:
                shadow = json.loads(sv) if sv else {}
            except Exception:
                shadow = {}
            r = _net_r(pnl, entry, stop)
            if r is None:
                continue
            rows.append({
                "id": cid, "created_at": created,
                "composite_score": comp, "_shadow": shadow,
                "net_r": r, "asset_class": ac or "?",
            })
    return rows


def _evaluate_challenger(universe: list[dict], challenger: str,
                         champion: str, gate: float) -> dict:
    """One challenger vs the champion, per §4.3. Pure function of the
    universe — separable so tests can feed it a synthetic history."""
    from datetime import datetime

    from lib.score_variants import VARIANT_DEFINED

    defined_at = VARIANT_DEFINED.get(challenger, "9999-01-01")

    # Leakage cutoff: only candidates born after the definition froze, and
    # only rows where the variant's stored score exists (older rows were
    # recorded under a schema that didn't include it).
    oos = [r for r in universe
           if str(r["created_at"]) > defined_at
           and _variant_score(r, challenger) is not None]

    out = {
        "challenger": challenger, "champion": champion, "gate": gate,
        "defined_at": defined_at, "oos_universe": len(oos),
        "criteria": {}, "verdict": None,
    }
    crit = out["criteria"]

    if not oos:
        out["verdict"] = "INSUFFICIENT_DATA"
        crit["min_sample"] = {"pass": False, "selected": 0,
                              "required": MIN_SELECTED}
        return out

    ch_sel = [(r["created_at"], r["net_r"]) for r in oos
              if (_variant_score(r, challenger) or -1) >= gate]
    cp_sel = [(r["created_at"], r["net_r"]) for r in oos
              if (_variant_score(r, champion) or -1) >= gate]
    ch, cp = _arm_stats(ch_sel), _arm_stats(cp_sel)
    out["challenger_stats"], out["champion_stats"] = ch, cp

    # ── selection frequency: always reported, degenerate selectors barred
    freq_ch = len(ch_sel) / len(oos)
    crit["selection_frequency"] = {
        "challenger": round(freq_ch, 4),
        "champion": round(len(cp_sel) / len(oos), 4),
        "pass": freq_ch >= MIN_SELECTION_FREQ,
    }

    # ── sample size
    crit["min_sample"] = {"pass": len(ch_sel) >= MIN_SELECTED,
                          "selected": len(ch_sel), "required": MIN_SELECTED}

    # ── calendar span of the OOS window
    def _day(s):
        try:
            return datetime.fromisoformat(str(s)[:19])
        except Exception:
            return None
    first, last = _day(oos[0]["created_at"]), _day(oos[-1]["created_at"])
    span = (last - first).days if first and last else 0
    crit["calendar_span"] = {"pass": span >= MIN_CALENDAR_DAYS,
                             "days": span, "required": MIN_CALENDAR_DAYS}

    insufficient = (not crit["min_sample"]["pass"]
                    or not crit["calendar_span"]["pass"])

    # ── chronological walk-forward: contiguous thirds of the OOS stream
    folds = []
    fold_size = max(1, len(oos) // N_FOLDS)
    for i in range(N_FOLDS):
        chunk = oos[i * fold_size: (i + 1) * fold_size if i < N_FOLDS - 1 else len(oos)]
        f_ch = [r["net_r"] for r in chunk
                if (_variant_score(r, challenger) or -1) >= gate]
        f_cp = [r["net_r"] for r in chunk
                if (_variant_score(r, champion) or -1) >= gate]
        valid = len(f_ch) >= MIN_FOLD_SAMPLE and len(f_cp) >= MIN_FOLD_SAMPLE
        won = (valid and
               sum(f_ch) / len(f_ch) > sum(f_cp) / len(f_cp))
        folds.append({"fold": i + 1, "n": len(chunk),
                      "challenger_n": len(f_ch), "champion_n": len(f_cp),
                      "valid": valid, "challenger_won": bool(won)})
    valid_folds = [f for f in folds if f["valid"]]
    folds_won = sum(1 for f in valid_folds if f["challenger_won"])
    crit["walk_forward"] = {
        "pass": len(valid_folds) >= MIN_FOLDS_WON and folds_won >= MIN_FOLDS_WON,
        "folds": folds, "valid_folds": len(valid_folds),
        "folds_won": folds_won, "required_wins": MIN_FOLDS_WON,
    }

    # ── multiple regimes, approximated: per-asset-class agreement where
    # sample permits. Candidates do not store a regime label; folds cover
    # time variation, classes cover market variation. Named honestly.
    by_class = {}
    for r in oos:
        by_class.setdefault(r["asset_class"], []).append(r)
    class_rows = []
    for cls, rows_ in sorted(by_class.items()):
        c_ch = [r["net_r"] for r in rows_
                if (_variant_score(r, challenger) or -1) >= gate]
        c_cp = [r["net_r"] for r in rows_
                if (_variant_score(r, champion) or -1) >= gate]
        if len(c_ch) >= MIN_FOLD_SAMPLE and len(c_cp) >= MIN_FOLD_SAMPLE:
            class_rows.append({
                "asset_class": cls,
                "challenger_mean_r": round(sum(c_ch) / len(c_ch), 4),
                "champion_mean_r": round(sum(c_cp) / len(c_cp), 4),
                "challenger_won": sum(c_ch) / len(c_ch) > sum(c_cp) / len(c_cp),
            })
    crit["regimes"] = {
        "approximation": "asset-class breakdown (regime labels not stored)",
        "classes": class_rows,
        # Not worse in any class with enough sample; vacuously true only
        # when NO class has sample, which min_sample already guards.
        "pass": all(c["challenger_won"] for c in class_rows) if class_rows
                else not insufficient and len(valid_folds) >= MIN_FOLDS_WON,
    }

    # ── after-cost net R improvement
    if ch.get("n") and cp.get("n"):
        delta = ch["mean_net_r"] - cp["mean_net_r"]
        crit["net_r_improvement"] = {
            "pass": delta >= MIN_NET_R_IMPROVEMENT,
            "delta": round(delta, 4), "required": MIN_NET_R_IMPROVEMENT,
        }
        # ── tail / drawdown tolerance
        p5_ok = ch["p5_net_r"] >= cp["p5_net_r"] - TAIL_P5_TOLERANCE_R
        dd_cap = cp["max_drawdown_r"] * DD_TOLERANCE[0] + DD_TOLERANCE[1]
        dd_ok = ch["max_drawdown_r"] <= dd_cap
        crit["tail_not_worse"] = {
            "pass": bool(p5_ok and dd_ok),
            "challenger_p5": ch["p5_net_r"], "champion_p5": cp["p5_net_r"],
            "p5_tolerance": TAIL_P5_TOLERANCE_R,
            "challenger_dd": ch["max_drawdown_r"],
            "champion_dd": cp["max_drawdown_r"],
            "dd_cap": round(dd_cap, 4),
        }
    else:
        crit["net_r_improvement"] = {"pass": False, "delta": None,
                                     "required": MIN_NET_R_IMPROVEMENT}
        crit["tail_not_worse"] = {"pass": False}

    # ── leakage: structural, so reported as a property rather than measured
    crit["no_leakage"] = {
        "pass": True,
        "enforced_by": "stored-at-birth scores only; created_at > defined_at",
    }

    if insufficient:
        out["verdict"] = "INSUFFICIENT_DATA"
    elif all(c.get("pass") for c in crit.values()):
        out["verdict"] = "PROMOTE_ELIGIBLE"
    else:
        out["verdict"] = "NOT_ELIGIBLE"
    out["failed"] = [k for k, c in crit.items() if not c.get("pass")]
    return out


def evaluate_promotion(gate: float = 55.0) -> dict:
    """Every challenger vs the current champion, on live candidate data."""
    from lib.score_variants import VARIANT_DEFINED, VARIANT_SCHEMA_VERSION

    champ = current_champion()
    universe = _load_universe()
    out = {
        "schema": VARIANT_SCHEMA_VERSION,
        "champion": champ,
        "gate": gate,
        "resolved_universe": len(universe),
        "challengers": {},
        "note": ("promote() records an artifact only; live scoring is "
                 "wired in Phase 8 after the gate experiment concludes"),
    }
    for v in VARIANT_DEFINED:
        if v == champ["variant"]:
            continue
        out["challengers"][v] = _evaluate_challenger(
            universe, v, champ["variant"], gate)
    return out


# ── Champion artifact (append-only) ──────────────────────────────────────────

def current_champion() -> dict:
    """Latest row of the ledger; seeds the founding champion on first use."""
    from app.database import ScoreChampion, get_db

    with get_db() as db:
        row = (db.query(ScoreChampion)
                 .order_by(ScoreChampion.id.desc()).first())
        if row is None:
            from lib.score_variants import VARIANT_SCHEMA_VERSION
            row = ScoreChampion(
                variant=FOUNDING_CHAMPION,
                schema_version=VARIANT_SCHEMA_VERSION,
                evidence=json.dumps({"founding": True}),
                note=("founding champion: the live composite as measured "
                      "2026-08-13 (inverted) — the control every challenger "
                      "must beat out of sample"),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        return {"id": row.id, "variant": row.variant,
                "schema_version": row.schema_version,
                "promoted_at": row.promoted_at, "note": row.note}


def champion_history() -> list[dict]:
    from app.database import ScoreChampion, get_db

    current_champion()          # ensure the founding row exists
    with get_db() as db:
        return [{"id": r.id, "variant": r.variant,
                 "schema_version": r.schema_version,
                 "promoted_at": r.promoted_at, "note": r.note}
                for r in db.query(ScoreChampion)
                           .order_by(ScoreChampion.id.asc()).all()]


def promote(variant: str, note: str = "") -> dict:
    """Append a new champion row — ONLY if the evaluation run right now
    says PROMOTE_ELIGIBLE. The evidence is re-computed at call time and
    frozen into the artifact; a stale evaluation cannot be replayed to
    smuggle a challenger past fresh data.
    """
    from app.database import ScoreChampion, get_db
    from lib.score_variants import VARIANT_SCHEMA_VERSION

    evaluation = evaluate_promotion()
    ev = evaluation["challengers"].get(variant)
    if ev is None:
        raise ValueError(f"unknown or already-champion variant: {variant}")
    if ev["verdict"] != "PROMOTE_ELIGIBLE":
        raise ValueError(
            f"{variant} is {ev['verdict']}, not PROMOTE_ELIGIBLE "
            f"(failed: {ev.get('failed')}) — §4.3 does not negotiate")

    with get_db() as db:
        row = ScoreChampion(
            variant=variant,
            schema_version=VARIANT_SCHEMA_VERSION,
            evidence=json.dumps(ev, sort_keys=True),
            note=note or f"promoted over {ev['champion']} per §4.3",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(f"[Promotion] {variant} promoted (row {row.id})")
        return {"id": row.id, "variant": row.variant,
                "promoted_at": row.promoted_at}
