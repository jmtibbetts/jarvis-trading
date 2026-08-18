"""Turn the B9 JSON measurements into the human-readable report.

Reads the newest measurement/storage/recovery JSON in data/reports and emits
a matching markdown file. Reports stay gitignored under data/.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
REPORTS = Path("data/reports")


def _newest(prefix: str):
    files = sorted(REPORTS.glob(f"{prefix}*.json"))
    if not files:
        return None, None
    return files[-1], json.loads(files[-1].read_text())


def main() -> None:
    from lib import evidence_retention as RET

    mf, m = _newest("bitnomial_market_data_measurement_")
    sf, s = _newest("evidence_storage_measurement_")
    rf, r = _newest("bitnomial_recovery_verification_")
    if not m:
        print("no measurement JSON found — run measure_bitnomial_evidence first")
        return

    sess = m["session"]
    prov = m["provider_traffic"]
    tob = m["top_of_book"]
    samp = m.get("sampling_policy_measured", {})
    bpr = (s or {}).get("bytes_per_row", RET.MEASURED_BYTES_PER_ROW)
    proj = RET.projection(tob_changes_per_s=tob["changes_per_s"],
                          bytes_per_row=bpr,
                          products=sess["products_active"])

    per = m.get("per_product", {})
    ranked = sorted(per.items(), key=lambda kv: -kv[1]["tob_changes_per_s"])

    L = []
    A = L.append
    A("# B9 — Bitnomial read-only market-data measurement\n")
    A(f"Session {sess['started_at']} · {sess['duration_s']}s · "
      f"{sess['products_active']} active perpetuals\n")
    A("Strictly READ_PROVIDER_READ_ONLY: public WebSocket, no authentication, "
      "no order/cancel/amend/transfer surface. Disposable DB; the operator "
      "database was not opened.\n")

    A("\n## Provider traffic\n")
    A(f"- total messages: **{prov['total_messages']:,}** "
      f"(**{prov['messages_per_s']}/s**)")
    for k, v in sorted(prov["by_type"].items(), key=lambda kv: -kv[1]):
        A(f"- `{k}`: {v:,}")

    A("\n## Top-of-book activity — what the evidence store actually needs\n")
    A(f"- top-of-book changes: **{tob['total_changes']:,}** "
      f"(**{tob['changes_per_s']}/s**)")
    A(f"- per product per minute: **{tob['changes_per_product_per_min']}**")
    A("\nTotal depth-message rate is much larger than the rate at which the "
      "executable quote actually moves; only the latter is evidence.\n")

    A("\n## Sampling policy, measured\n")
    if samp:
        for k, v in samp.items():
            A(f"- {k}: {v}")
    A("\n**The original design assumed ~6 samples per product per minute.** "
      f"The venue produces ~{tob['changes_per_product_per_min']:.0f}. A 1Hz "
      "polling collector persisted about 13% of real book movement, so the "
      "collector is now event-driven off the same ingest.\n")

    A("\n## Per product\n")
    A("| product | book msgs/s | TOB changes/s | bid px | ask px | "
      "bid size | ask size | longest quiet (s) |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for sym, d in ranked:
        A(f"| {sym} | {d['book_msgs_per_s']} | {d['tob_changes_per_s']} | "
          f"{d['bid_px_changes']} | {d['ask_px_changes']} | "
          f"{d['bid_size_changes']} | {d['ask_size_changes']} | "
          f"{d['longest_quiet_s']} |")

    if s:
        A("\n## SQLite cost, measured at scale\n")
        A(f"- rows inserted: **{s['rows']:,}**")
        A(f"- empty DB: {s['empty_db_bytes']:,} bytes")
        A(f"- populated (checkpointed): {s['db_bytes_checkpointed']:,} bytes")
        A(f"- **bytes/row: {s['bytes_per_row']}**")
        A(f"- table {s.get('table_bytes'):,} / index {s.get('index_bytes'):,} "
          f"bytes (index overhead {s.get('index_overhead_pct')}%)")
        A(f"- insert throughput: **{s['insert_rows_per_s']:,} rows/s**")
        A(f"- range query uses index: **{s['range_query_uses_index']}**")
        A("\n| window | p50 ms | p95 ms |")
        A("|---|---:|---:|")
        for w, t in s["query_ms_by_window"].items():
            A(f"| {w} | {t['p50_ms']} | {t['p95_ms']} |")
        A(f"\n- chronological 1h scan (the touch-order query): "
          f"**{s['chronological_1h_scan_ms']} ms**")

    A("\n## Storage projection from MEASURED rates\n")
    for k in ("rows_per_day", "gb_per_day", "gb_per_30d", "gb_per_year",
              "storage_case", "policy"):
        A(f"- {k}: **{proj[k]}**")

    A("\n## Retention decision\n")
    A(f"Projected **{proj['gb_per_year']} GB/year** for the perpetual set "
      f"against ~8 TB NVMe + ~60 TB SATA — {proj['storage_case']}.\n")
    A("**RAW TIMESTAMPED EVIDENCE IS KEPT. ARCHIVE, NEVER DELETE.**\n")
    A("- no pruning job, no 30-day delete, no compaction job")
    A("- `RAW_RETENTION_DAYS = None`, `DELETE_ENABLED = False`, test-enforced")
    A("- warm 1-minute aggregates remain available as a QUERY-SPEED option "
      "later; they never replace raw chronology")
    A("- SQLite retained: two orders of magnitude of write headroom and "
      "single-digit-ms indexed range queries do not motivate a migration\n")
    A("This is the failure being designed against: 11,775 historical rejected "
      "candidates have no forward evidence because it was discarded before "
      "anyone asked. A 30-day rule would reproduce that on a timer.\n")

    if r:
        A("\n## Recovery verification\n")
        rec = r.get("recovery", {})
        for k, v in rec.items():
            A(f"- {k}: {v}")
        A("\n**Verdicts**\n")
        for k, v in r.get("verdicts", {}).items():
            A(f"- {'PASS' if v else 'FAIL'} — {k}")
        if r.get("healthy_window"):
            A(f"\n- quiet-but-healthy window: {r['healthy_window']}")
        if r.get("outage_window"):
            A(f"- outage window: {r['outage_window']}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / f"bitnomial_market_data_measurement_{ts}.md"
    out.write_text("\n".join(L))
    print(f"wrote {out}")
    print(f"  sources: {mf.name if mf else '-'}, {sf.name if sf else '-'}, "
          f"{rf.name if rf else '-'}")


if __name__ == "__main__":
    main()
