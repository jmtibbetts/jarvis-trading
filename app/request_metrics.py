"""In-memory API request/error tracking for the Ops error-rate panel.

Deliberately not DB-backed: this is a live-ops signal (recent request health),
not an audit record, so it doesn't need to survive a restart. A bounded
deque keeps memory flat regardless of uptime.
"""
import collections
import time

_LOG_MAXLEN = 2000
request_log = collections.deque(maxlen=_LOG_MAXLEN)  # (timestamp, path, status_code)


def record(path: str, status_code: int) -> None:
    request_log.append((time.time(), path, status_code))


def error_rate_summary(window_minutes: int = 15) -> dict:
    cutoff = time.time() - window_minutes * 60
    recent = [row for row in request_log if row[0] >= cutoff]
    total = len(recent)
    errors = [row for row in recent if row[2] >= 500]
    by_path: dict[str, dict] = {}
    for _, path, status in errors:
        entry = by_path.setdefault(path, {"path": path, "count": 0})
        entry["count"] += 1
    return {
        "window_minutes": window_minutes,
        "total_requests": total,
        "error_count": len(errors),
        "error_rate_pct": round(len(errors) / total * 100, 2) if total else 0.0,
        "top_error_paths": sorted(by_path.values(), key=lambda r: r["count"], reverse=True)[:5],
        "logged_since": recent[0][0] if recent else None,
    }
