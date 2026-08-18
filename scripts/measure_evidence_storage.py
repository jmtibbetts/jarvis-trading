"""B9 — real SQLite cost of the evidence store, at realistic scale.

Bytes-per-row estimated from a couple of hundred rows is dominated by fixed
page and schema overhead, so this populates a DISPOSABLE database with
hundreds of thousands of rows and measures the file.

Also times the queries Phase B and C actually run, and checks with EXPLAIN
QUERY PLAN that they use an index rather than scanning.

Usage:  .venv/bin/python scripts/measure_evidence_storage.py [rows]
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-storage-"))
os.environ["JARVIS_DB_PATH"] = str(_TMP / "storage.db")
os.environ["JARVIS_EVENTS_DB_PATH"] = str(_TMP / "storage_events.db")
os.environ["JARVIS_DISABLE_MARKET_DATA"] = "1"

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000
PRODUCTS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
            "LINK/USD", "ADA/USD", "AVAX/USD", "LTC/USD", "BCH/USD",
            "DOT/USD", "XLM/USD", "TRX/USD", "HBAR/USD", "AAVE/USD",
            "XTZ/USD"]


def _size(p: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def main() -> dict:
    from sqlalchemy import text

    from app.database import (DB_PATH, InstrumentQuoteSample, engine, get_db,
                              init_db)

    db_file = Path(DB_PATH)
    print(f"  disposable DB : {db_file}")
    init_db()
    empty_bytes = _size(db_file)
    print(f"  empty         : {empty_bytes:,} bytes")

    # ── populate ─────────────────────────────────────────────────────────
    t0 = datetime.now(timezone.utc) - timedelta(days=1)
    rnd = random.Random(11)
    batch, written = [], 0
    start = time.monotonic()
    for i in range(ROWS):
        sym = PRODUCTS[i % len(PRODUCTS)]
        px = 100.0 + rnd.gauss(0, 5)
        at = t0 + timedelta(milliseconds=i * 300)
        batch.append({
            "product": "CRYPTO_PERP", "venue": "kraken_derivatives_us",
            "symbol": sym, "instrument_id": None,
            "market_data_source": "bitnomial_public_book",
            "observed_at": at.isoformat(), "source_at": at.isoformat(),
            "bid": round(px, 4), "ask": round(px + 0.5, 4),
            "mid": round(px + 0.25, 4),
            "sample_reason": "CHANGE" if i % 20 else "HEARTBEAT",
        })
        if len(batch) >= 10_000:
            with get_db() as db:
                db.bulk_insert_mappings(InstrumentQuoteSample.__mapper__, batch)
            written += len(batch)
            batch = []
    if batch:
        with get_db() as db:
            db.bulk_insert_mappings(InstrumentQuoteSample.__mapper__, batch)
        written += len(batch)
    insert_s = time.monotonic() - start

    wal_bytes = _size(db_file)
    with engine.connect() as c:
        c.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    checkpointed = _size(db_file)

    # index vs table split, where dbstat is available
    idx_bytes = tbl_bytes = None
    try:
        with engine.connect() as c:
            rows = c.execute(text(
                "SELECT name, SUM(pgsize) FROM dbstat "
                "WHERE name LIKE '%quote_sample%' GROUP BY name")).fetchall()
        for name, sz in rows:
            if name.startswith("ix_") or name.startswith("sqlite_autoindex"):
                idx_bytes = (idx_bytes or 0) + int(sz)
            else:
                tbl_bytes = (tbl_bytes or 0) + int(sz)
    except Exception as e:
        print(f"  (dbstat unavailable: {e})")

    payload = checkpointed - empty_bytes
    print(f"  populated     : {checkpointed:,} bytes for {written:,} rows")
    print(f"  bytes/row     : {payload / written:.1f}")

    # ── query performance ────────────────────────────────────────────────
    windows = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    qperf = {}
    with engine.connect() as c:
        plan = " ".join(str(r) for r in c.execute(text(
            "EXPLAIN QUERY PLAN SELECT bid, ask, observed_at FROM "
            "instrument_quote_samples WHERE product=:p AND venue=:v AND symbol=:s "
            "AND observed_at BETWEEN :a AND :b ORDER BY observed_at"),
            {"p": "CRYPTO_PERP", "v": "kraken_derivatives_us",
             "s": "BTC/USD", "a": "a", "b": "b"}
        ).fetchall())
        uses_index = "ix_quote_sample_window" in plan

        for label, minutes in windows.items():
            times = []
            for k in range(20):
                s = t0 + timedelta(minutes=k * 7)
                e = s + timedelta(minutes=minutes)
                t = time.monotonic()
                c.execute(text(
                    "SELECT MIN(bid), MAX(bid), MIN(ask), MAX(ask), COUNT(*) "
                    "FROM instrument_quote_samples WHERE product=:p AND "
                    "venue=:v AND symbol=:s AND observed_at BETWEEN :a AND :b"),
                    {"p": "CRYPTO_PERP", "v": "kraken_derivatives_us",
                     "s": "BTC/USD", "a": s.isoformat(), "b": e.isoformat()}
                ).fetchone()
                times.append((time.monotonic() - t) * 1000)
            times.sort()
            qperf[label] = {
                "p50_ms": round(statistics.median(times), 3),
                "p95_ms": round(times[int(len(times) * 0.95) - 1], 3),
            }

        # chronological scan, the touch-order query
        t = time.monotonic()
        c.execute(text(
            "SELECT observed_at, bid, ask FROM instrument_quote_samples "
            "WHERE product=:p AND venue=:v AND symbol=:s AND observed_at "
            "BETWEEN :a AND :b ORDER BY observed_at ASC"),
            {"p": "CRYPTO_PERP", "v": "kraken_derivatives_us", "s": "BTC/USD",
             "a": t0.isoformat(),
             "b": (t0 + timedelta(hours=1)).isoformat()}).fetchall()
        chrono_ms = round((time.monotonic() - t) * 1000, 3)

    return {
        "rows": written,
        "empty_db_bytes": empty_bytes,
        "db_bytes_with_wal": wal_bytes,
        "db_bytes_checkpointed": checkpointed,
        "payload_bytes": payload,
        "bytes_per_row": round(payload / written, 2),
        "table_bytes": tbl_bytes,
        "index_bytes": idx_bytes,
        "index_overhead_pct": (round(idx_bytes / tbl_bytes * 100, 1)
                               if idx_bytes and tbl_bytes else None),
        "insert_seconds": round(insert_s, 2),
        "insert_rows_per_s": int(written / insert_s) if insert_s else None,
        "range_query_uses_index": uses_index,
        "range_query_plan_ok": uses_index,
        "query_ms_by_window": qperf,
        "chronological_1h_scan_ms": chrono_ms,
    }


if __name__ == "__main__":
    r = main()
    print(json.dumps(r, indent=2))
    out = Path("data/reports")
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"evidence_storage_measurement_{ts}.json").write_text(
        json.dumps(r, indent=2))
    print(f"wrote data/reports/evidence_storage_measurement_{ts}.json")
