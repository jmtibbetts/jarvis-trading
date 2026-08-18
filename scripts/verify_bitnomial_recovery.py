"""B9 — prove the recovery path against the real venue, once.

Recovery is the part of a feed nobody exercises until it matters. This drops
ONE connection deliberately and measures what the desk does next:

    connected -> forced local disconnect -> books unusable -> backoff
    -> reconnect -> resubscribe -> fresh snapshot -> AVAILABLE again

It also proves the distinction the sampler depends on:

    QUIET BUT HEALTHY   heartbeat rows keep arriving, no GAP_PRESENT
    DISCONNECTED        no rows at all, blind interval, quality degrades

Only this side of the socket is closed; the venue is asked for nothing
beyond one ordinary re-subscribe.

READ-ONLY. Disposable DB. Usage:
    .venv/bin/python scripts/verify_bitnomial_recovery.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-recovery-"))
os.environ["JARVIS_DB_PATH"] = str(_TMP / "recovery.db")
os.environ["JARVIS_EVENTS_DB_PATH"] = str(_TMP / "recovery_events.db")
os.environ.pop("JARVIS_DISABLE_MARKET_DATA", None)
os.environ["JARVIS_DISABLE_SCHEDULER"] = "1"

SETTLE_S = 45      # let the book establish before breaking it
RECOVER_S = 90     # allow backoff + reconnect + fresh snapshot


def _now():
    return datetime.now(timezone.utc)


def main() -> dict:
    from app.database import DB_PATH, InstrumentQuoteSample, get_db, init_db
    from lib import bitnomial_market_data as MD
    from lib import bitnomial_products as BP
    from lib import market_data_runtime as MDR
    from lib import range_collector as RC
    from lib.execution_snapshot import (DEFAULT_PERP_MAX_AGE_S,
                                        execution_market_snapshot)

    print(f"  disposable DB : {DB_PATH}")
    print(f"  operator DB   : NOT IN USE")
    init_db()

    probe = "BTC/USD"
    out: dict = {"probe": probe, "timeline": []}

    def note(label, **kw):
        e = {"at": _now().isoformat(), "event": label, **kw}
        out["timeline"].append(e)
        print(f"  [{label}] {kw}")

    def snap():
        return execution_market_snapshot(probe, "kraken_derivatives_us",
                                         product="CRYPTO_PERP",
                                         max_age_s=DEFAULT_PERP_MAX_AGE_S)

    MDR.start(bitnomial_symbols=list(BP.active_symbols()))
    note("started")

    # ── 1. establish a healthy, executable book ──────────────────────────
    t_ok = None
    for _ in range(SETTLE_S):
        time.sleep(1)
        s = snap()
        if s.fillable:
            t_ok = _now()
            note("available_before", status=s.status, bid=s.bid, ask=s.ask)
            break
    if t_ok is None:
        MDR.stop()
        return {**out, "error": "book never became executable"}

    # quiet-vs-healthy needs a stretch of observation to judge
    time.sleep(20)
    RC.flush_samples()
    pre_start = t_ok
    pre_end = _now()
    pre = RC.range_over(symbol=probe, product="CRYPTO_PERP",
                        venue="kraken_derivatives_us",
                        start=pre_start, end=pre_end)
    out["healthy_window"] = {
        "quality": pre.quality, "samples": pre.sample_count,
        "max_gap_s": pre.max_sample_gap_s,
        "is_gap_present": pre.quality == RC.GAP_PRESENT,
    }
    note("healthy_window", **out["healthy_window"])

    # ── 2. break it ──────────────────────────────────────────────────────
    health_before = MD.stream_health()
    disconnect_at = _now()
    dispatched = MD.force_disconnect()
    note("forced_disconnect", dispatched=dispatched,
         reconnects_before=health_before["reconnect_count"])

    # ── 3. the stale book must NOT remain executable ─────────────────────
    unusable_at = None
    for _ in range(20):
        time.sleep(0.5)
        s = snap()
        if not s.fillable:
            unusable_at = _now()
            note("book_unusable", status=s.status, reason=(s.reason or "")[:90])
            break
    out["stale_book_refused"] = unusable_at is not None

    # ── 4. recovery ──────────────────────────────────────────────────────
    recovered_at = None
    for _ in range(RECOVER_S):
        time.sleep(1)
        s = snap()
        if s.fillable:
            recovered_at = _now()
            note("available_after", status=s.status, bid=s.bid, ask=s.ask)
            break

    h = MD.stream_health()
    RC.flush_samples()

    blind_s = ((recovered_at - disconnect_at).total_seconds()
               if recovered_at else None)
    out["recovery"] = {
        "disconnect_at": disconnect_at.isoformat(),
        "unusable_at": unusable_at.isoformat() if unusable_at else None,
        "reconnect_at": recovered_at.isoformat() if recovered_at else None,
        "blind_interval_s": round(blind_s, 2) if blind_s else None,
        "reconnect_count_before": health_before["reconnect_count"],
        "reconnect_count_after": h["reconnect_count"],
        "resubscribed": h["subscribed"],
        "products_subscribed": h["products_subscribed"],
        "recovered": recovered_at is not None,
    }

    # ── 5. does the gap show up in the evidence? ─────────────────────────
    if recovered_at:
        gapw = RC.range_over(symbol=probe, product="CRYPTO_PERP",
                             venue="kraken_derivatives_us",
                             start=disconnect_at - timedelta(seconds=5),
                             end=recovered_at + timedelta(seconds=5))
        out["outage_window"] = {
            "quality": gapw.quality, "samples": gapw.sample_count,
            "max_gap_s": gapw.max_sample_gap_s,
            "degraded": gapw.quality in (RC.GAP_PRESENT, RC.PARTIAL,
                                         RC.INSUFFICIENT_RANGE_DATA),
        }
        note("outage_window", **out["outage_window"])

    with get_db() as db:
        out["rows_total"] = db.query(InstrumentQuoteSample).count()

    MDR.stop()
    note("stopped")

    out["verdicts"] = {
        "stale_book_not_executable": bool(out.get("stale_book_refused")),
        "reconnected_and_resubscribed": bool(
            out["recovery"]["recovered"] and out["recovery"]["resubscribed"]),
        "quiet_healthy_not_flagged_as_gap": not out["healthy_window"][
            "is_gap_present"],
        "outage_degraded_quality": bool(
            out.get("outage_window", {}).get("degraded")),
    }
    return out


if __name__ == "__main__":
    r = main()
    print("\nVERDICTS:")
    for k, v in r.get("verdicts", {}).items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    out = Path("data/reports")
    out.mkdir(parents=True, exist_ok=True)
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    p = out / f"bitnomial_recovery_verification_{ts}.json"
    p.write_text(json.dumps(r, indent=2))
    print(f"\nwrote {p}")
