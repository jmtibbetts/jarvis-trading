"""Emit the decision-quality report as JSON and Markdown.

Read-only: the evidence database is opened `mode=ro` with `query_only=ON` and
nothing is written back to it. The report is an artifact, not analytics state
stored beside the evidence.

Usage:
    .venv/bin/python scripts/report_decision_quality.py
    .venv/bin/python scripts/report_decision_quality.py --db PATH --epoch E
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import decision_quality as DQ  # noqa: E402

DEFAULT_DB = REPO / "data" / "forward_evidence.db"
DEFAULT_EPOCH = "FORWARD_EVIDENCE_20260818T075321Z"
DEFAULT_BOUNDARY = "2026-08-18T07:53:21.357402+00:00"


def _pct(num: int, den: int) -> str:
    return f"{num}/{den}" + (f" ({num / den * 100:.1f}%)" if den else "")


def _table(title: str, counts: dict, total: int) -> list[str]:
    out = [f"### {title}", "", "| value | n | of total |", "|---|---:|---:|"]
    for k, v in counts.items():
        out.append(f"| {k} | {v} | {_pct(v, total)} |")
    return out + [""]


def to_markdown(r: dict) -> str:
    m, d = r["meta"], r["denominators"]
    total = d["total_decisions"]
    L = [
        "# Forward decision quality",
        "",
        f"- report `{m['report_version']}` generated {m['generated_at']}",
        f"- epoch `{m['epoch']}` from `{m['activation_boundary']}`",
        f"- source `{m['source']}`",
        f"- primary horizon policy `{m['primary_horizon_policy_version']}`"
        " — chosen from T0 information only, never from outcomes",
        f"- current expectancy floor **{m['expectancy_min_net_r']}R**"
        f" (the historical ~{m['historical_modelled_round_trip_cost_r']}R was a"
        " modelled round-trip COST, never a threshold)",
        f"- **policy changes authorized: {m['policy_changes_authorized']}**",
        "",
        "## Denominators",
        "",
        "One `DecisionObservation` is one decision. Horizons are repeated",
        "measurements of the same decision and never inflate N.",
        "",
        "| measure | n |",
        "|---|---:|",
        f"| total decisions | {total} |",
        f"| decisions with any outcome row | {d['decisions_with_any_outcome']} |",
        f"| primary horizon scheduled | {d['primary_horizon_scheduled']} |",
        f"| primary horizon COMPLETE | {d['primary_horizon_complete']} |",
        f"| outcome rows (all horizons) | {d['outcome_rows_total']} |",
        "",
    ]
    L += _table("Verdicts", r["verdicts"], total)
    L += _table("Routing identity quality", r["routing_identity_quality"], total)
    L += _table("Binding constraints", r["binding_constraints"], total)
    L += _table("Resolution semantics", r["resolution_quality"], total)
    L += _table("Primary horizon states", r["primary_horizon_states"], total)

    L += ["### Horizon curve", "",
          "| horizon | distinct decisions | measurements | COMPLETE |",
          "|---|---:|---:|---:|"]
    for h, c in sorted(r["horizon_curve"].items()):
        L.append(f"| {h} | {c['distinct_decisions']} | {c['measurements']} "
                 f"| {c['complete']} |")
    L.append("")

    f = r["favorable_after_rejection"]
    L += ["### Favorable after rejection", "",
          "Reported as `FAVORABLE_AFTER_REJECTION`. A favourable move after a",
          "refusal does not prove the trade was executable or permitted at T0.",
          "",
          f"- market-direction favorable: {_pct(f['market_direction_favorable'], f['market_direction_n'])}",
          f"- side-reference favorable: {_pct(f['side_reference_favorable'], f['side_reference_n'])}",
          ""]

    L += ["### Excursions", "",
          "`NULL` where interval evidence is absent — never zero. A negative",
          "MFE is valid and means the best excursion stayed below T0.",
          "", "| metric | n | min | median | max |", "|---|---:|---:|---:|---:|"]
    for name, dist in (("MFE %", r["excursions"]["mfe_pct"]),
                       ("MAE %", r["excursions"]["mae_pct"]),
                       ("dir-adj mid return %",
                        r["returns"]["direction_adjusted_mid_pct"]),
                       ("side-reference return %",
                        r["returns"]["side_reference_pct"])):
        if dist.get("n"):
            L.append(f"| {name} | {dist['n']} | {dist['min']:.4f} "
                     f"| {dist['median']:.4f} | {dist['max']:.4f} |")
        else:
            L.append(f"| {name} | 0 | — | — | — |")
    L.append("")

    L += _table("Touch chronology", r["chronology"],
                sum(r["chronology"].values()) or 1)

    L += ["### Threshold sensitivity", "",
          "Descriptive only. `WOULD_CLEAR_*_EDGE_AT_T` says the stored T0 edge",
          "cleared a bar — **not** that the candidate would have traded, since",
          "risk, capability, AI judgement, account state and data quality all",
          "sit downstream of edge.",
          "", "| threshold R | clears point edge | clears robust edge | |",
          "|---:|---:|---:|---|"]
    for t in r["threshold_sensitivity"]:
        mark = " **current policy**" if t["is_current_policy"] else ""
        L.append(f"| {t['threshold_r']:.3f} "
                 f"| {_pct(t['would_clear_point_edge_at_t'], t['point_n'])} "
                 f"| {_pct(t['would_clear_robust_edge_at_t'], t['robust_n'])} "
                 f"|{mark} |")
    L += ["",
          "## Interpretation",
          "",
          f"The analytics are verified against {total} prospective decisions.",
          "The prospective sample is still small and no policy conclusion is",
          "warranted from it. Numbers above are descriptive, and every rate",
          "carries its own denominator so thin coverage is visible rather than",
          "hidden.",
          ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--epoch", default=DEFAULT_EPOCH)
    ap.add_argument("--boundary", default=DEFAULT_BOUNDARY)
    args = ap.parse_args()

    r = DQ.build_report(args.db, epoch=args.epoch, boundary=args.boundary)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPO / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    j, md = out / f"decision_quality_{ts}.json", out / f"decision_quality_{ts}.md"
    j.write_text(json.dumps(r, indent=2, default=str))
    md.write_text(to_markdown(r))
    print(to_markdown(r))
    print(f"\nwrote {j}\nwrote {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
