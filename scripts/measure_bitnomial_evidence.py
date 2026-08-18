"""B9 — deliberate REAL_PROVIDER_READ_ONLY exercise of the Bitnomial feed.

Measures what the sampling policy was previously only ASSUMING: provider
message rates, top-of-book change rates, and what the change-triggered +
heartbeat sampler actually persists.

STRICTLY READ-ONLY. Public WebSocket only, no authentication, no order,
cancel, amend or transfer surface anywhere in the path. It drives the
EXISTING `market_data_runtime` / `bitnomial_market_data` service rather than
opening a second client, so what is measured is what production runs.

NEVER TOUCHES THE OPERATOR DB. Every path is redirected to a disposable file
before `app.database` is imported, and the paths in force are printed at
startup so there is no ambiguous default.

Usage:  .venv/bin/python scripts/measure_bitnomial_evidence.py [seconds]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Run from anywhere: the repo root must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── DB redirection MUST happen before app.database is imported ───────────
_TMP = Path(tempfile.mkdtemp(prefix="jarvis-b9-"))
os.environ["JARVIS_DB_PATH"] = str(_TMP / "b9_evidence.db")
os.environ["JARVIS_EVENTS_DB_PATH"] = str(_TMP / "b9_events.db")
# The runtime is enabled deliberately for this one exercise.
os.environ.pop("JARVIS_DISABLE_MARKET_DATA", None)
# The trading scheduler is NOT started by this script and must stay off.
os.environ["JARVIS_DISABLE_SCHEDULER"] = "1"

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 900
POLL_HZ = 1.0            # what a 1Hz polling collector would capture


def _now():
    return datetime.now(timezone.utc)


def main() -> dict:
    from app.database import DB_PATH, InstrumentQuoteSample, get_db, init_db
    from lib import bitnomial_market_data as MD
    from lib import bitnomial_products as BP
    from lib import market_data_runtime as MDR
    from lib import range_collector as RC

    print("=" * 70)
    print("B9 LIVE BITNOMIAL READ-ONLY MEASUREMENT")
    print("=" * 70)
    print(f"  evidence DB : {DB_PATH}")
    print(f"  events DB   : {os.environ['JARVIS_EVENTS_DB_PATH']}")
    print(f"  operator DB : NOT IN USE (data/jarvis.db untouched)")
    print(f"  scheduler   : {os.environ['JARVIS_DISABLE_SCHEDULER']} (disabled)")
    print(f"  duration    : {DURATION}s")
    init_db()

    # ── discover the CURRENT active set; do not assume the old 17 ────────
    symbols = list(BP.active_symbols())
    print(f"  products    : {len(symbols)} active now")
    if not symbols:
        print("  !! no active products discovered — aborting")
        return {"error": "no active products"}

    # ── instrument the EXISTING ingest ───────────────────────────────────
    msg_by_type: dict = defaultdict(int)
    book_msgs: dict = defaultdict(int)
    tob_changes: dict = defaultdict(int)
    bid_px_changes: dict = defaultdict(int)
    ask_px_changes: dict = defaultdict(int)
    bid_sz_changes: dict = defaultdict(int)
    ask_sz_changes: dict = defaultdict(int)
    last_tob: dict = {}
    quiet_gaps: dict = defaultdict(float)
    last_change_at: dict = {}
    lock = threading.Lock()
    total_msgs = [0]

    real_apply = MD.apply_message

    def counting_apply(msg: dict) -> None:
        real_apply(msg)
        try:
            sym = msg.get("symbol")
            mtype = str(msg.get("type") or msg.get("channel") or "?")
            with lock:
                total_msgs[0] += 1
                msg_by_type[mtype] += 1
                if sym:
                    if mtype in ("book", "level", "snapshot"):
                        book_msgs[sym] += 1
                    top = MD.latest_top(sym)
                    if top:
                        cur = (top.get("bid_raw"), top.get("ask_raw"),
                               top.get("bid_size"), top.get("ask_size"))
                        prev = last_tob.get(sym)
                        if prev is not None and cur != prev:
                            tob_changes[sym] += 1
                            if cur[0] != prev[0]:
                                bid_px_changes[sym] += 1
                            if cur[1] != prev[1]:
                                ask_px_changes[sym] += 1
                            if cur[2] != prev[2]:
                                bid_sz_changes[sym] += 1
                            if cur[3] != prev[3]:
                                ask_sz_changes[sym] += 1
                            now = time.monotonic()
                            if sym in last_change_at:
                                quiet_gaps[sym] = max(quiet_gaps[sym],
                                                      now - last_change_at[sym])
                            last_change_at[sym] = now
                        last_tob[sym] = cur
        except Exception:
            pass

    MD.apply_message = counting_apply

    started = MDR.start(bitnomial_symbols=symbols)
    print(f"  runtime     : {started}")
    t_start = _now()
    mono_start = time.monotonic()

    # The runtime now feeds evidence EVENT-DRIVEN from this same ingest,
    # so there is no poller here: what lands is exactly what production
    # persists.
    spec = BP._load()["by_base"]
    desk = sorted({f"{r['contract_size_unit']}/USD"
                   for b, r in spec.items() if b in BP._VERIFIED_PRICE_SCALE})
    print(f"  desk syms   : {len(desk)}")
    persisted = {"recorded": 0, "polls": 0}

    # ── run, reporting progress ──────────────────────────────────────────
    for elapsed in range(0, DURATION, 30):
        time.sleep(min(30, DURATION - elapsed))
        with lock:
            m = total_msgs[0]
        h = MD.stream_health()
        print(f"  [{elapsed + 30:>4}s] msgs={m:<8} connected={h['connected']} "
              f"books={h['books']} reconnects={h['reconnect_count']} "
              f"buffered={RC.buffered_count()}")

    dur = time.monotonic() - mono_start
    health = MD.stream_health()
    MDR.stop()
    MD.apply_message = real_apply

    # ── what actually landed ─────────────────────────────────────────────
    with get_db() as db:
        rows = db.query(InstrumentQuoteSample).count()
        changes = db.query(InstrumentQuoteSample).filter(
            InstrumentQuoteSample.sample_reason == "CHANGE").count()
        beats = db.query(InstrumentQuoteSample).filter(
            InstrumentQuoteSample.sample_reason == "HEARTBEAT").count()

    with lock:
        agg_msgs = total_msgs[0]
        per_product = {}
        for s in symbols:
            per_product[s] = {
                "book_msgs": book_msgs.get(s, 0),
                "book_msgs_per_s": round(book_msgs.get(s, 0) / dur, 4),
                "tob_changes": tob_changes.get(s, 0),
                "tob_changes_per_s": round(tob_changes.get(s, 0) / dur, 4),
                "bid_px_changes": bid_px_changes.get(s, 0),
                "ask_px_changes": ask_px_changes.get(s, 0),
                "bid_size_changes": bid_sz_changes.get(s, 0),
                "ask_size_changes": ask_sz_changes.get(s, 0),
                "longest_quiet_s": round(quiet_gaps.get(s, 0.0), 2),
            }
        types = dict(msg_by_type)

    tob_total = sum(v["tob_changes"] for v in per_product.values())
    out = {
        "session": {
            "started_at": t_start.isoformat(),
            "duration_s": round(dur, 1),
            "products_active": len(symbols),
            "products": symbols,
            "db_path": str(DB_PATH),
        },
        "provider_traffic": {
            "total_messages": agg_msgs,
            "messages_per_s": round(agg_msgs / dur, 3),
            "by_type": types,
        },
        "top_of_book": {
            "total_changes": tob_total,
            "changes_per_s": round(tob_total / dur, 3),
            "changes_per_product_per_min": round(
                tob_total / dur * 60 / max(len(symbols), 1), 3),
        },
        "sampling_policy_measured": {
            "poll_hz": POLL_HZ,
            "polls": persisted["polls"],
            "rows_persisted": rows,
            "change_samples": changes,
            "heartbeat_samples": beats,
            "change_pct": round(changes / rows * 100, 2) if rows else None,
            "heartbeat_pct": round(beats / rows * 100, 2) if rows else None,
            "samples_per_s": round(rows / dur, 3),
            "samples_per_product_per_min": round(
                rows / dur * 60 / max(len(symbols), 1), 3),
            "duplicates_avoided": max(0, persisted["polls"] * len(symbols) - rows),
        },
        "health_at_end": health,
        "per_product": per_product,
    }
    return out


if __name__ == "__main__":
    result = main()
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    outdir = Path("data/reports")
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / f"bitnomial_market_data_measurement_{ts}.json"
    p.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {p}")
    print(json.dumps(result.get("provider_traffic", {}), indent=2))
    print(json.dumps(result.get("top_of_book", {}), indent=2))
    print(json.dumps(result.get("sampling_policy_measured", {}), indent=2))
