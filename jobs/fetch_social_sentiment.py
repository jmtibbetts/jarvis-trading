"""Collect LunarCrush social/sentiment evidence. Read-only, no economics.

CLASSIFIED COLLECTION. It reads a provider and persists observations. It
never opens, mutates or settles a position, and it is not hidden inside an
LLM analysis job — a provider call buried in an analysis step is a call
nobody can find, budget or switch off.

CADENCE COMES FROM THE QUOTA, NOT FROM HABIT. The key reports 100
requests/day and 4/minute. One `coins/list` request describes every tracked
asset, so the whole universe costs ONE request; polling per symbol would
exhaust a day in under two minutes. At hourly cadence this spends ~24 of
100 daily requests and leaves the rest for diagnostics and topics.

RIGHT NOW IT COLLECTS NOTHING, and says so precisely. The credential is
valid and the subscription is inactive (HTTP 402), which no retry can fix.
The job records PAYMENT_REQUIRED and returns without writing rows — it does
not write zeros, and it does not pretend to have data.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# One request per run, hourly. See the module docstring for the arithmetic.
COIN_LIMIT = 200


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist(observations: list[dict], run_id: str) -> int:
    from app.database import LunarCrushObservation, get_db, new_id

    if not observations:
        return 0
    now = _utc()
    with get_db() as db:
        for obs in observations:
            db.add(LunarCrushObservation(
                id=new_id(),
                provider=obs["provider"],
                api_version=obs["api_version"],
                endpoint=obs["endpoint"],
                provider_asset_id=obs.get("provider_asset_id"),
                symbol=obs["symbol"],
                name=obs.get("name"),
                provider_at=obs.get("provider_at"),
                received_at=obs["received_at"],
                persisted_at=now,
                metrics_json=json.dumps(obs["metrics"], sort_keys=True),
                ingestion_run_id=run_id,
            ))
        db.commit()
    return len(observations)


def run() -> dict:
    """One collection cycle. Returns what happened, truthfully."""
    from lib import lunarcrush_client as LC
    from lib import provider_health as PH

    run_id = str(uuid.uuid4())
    if not LC.is_configured():
        PH.record(LC.PROVIDER, LC.CAP_COINS, status=PH.NOT_CONFIGURED,
                  detail="LUNARCRUSH_API_KEY not set")
        logger.info("[Social] LunarCrush not configured — nothing to collect")
        return {"ok": True, "status": PH.NOT_CONFIGURED, "persisted": 0,
                "run_id": run_id}

    fetch = LC.fetch_coins(limit=COIN_LIMIT)
    if not fetch.ok:
        # NO FAKE CONTINUITY. Nothing is written when nothing was received.
        level = logger.warning
        if fetch.status == PH.PAYMENT_REQUIRED:
            # Not a fault to alarm about on every cycle — a standing fact
            # only the account owner can change.
            level = logger.info
        level("[Social] LunarCrush %s (HTTP %s) — no rows written",
              fetch.status, fetch.http_status)
        return {"ok": False, "status": fetch.status, "persisted": 0,
                "http_status": fetch.http_status,
                "daily_remaining": LC.daily_remaining(fetch),
                "run_id": run_id,
                "detail": ("subscription inactive; credential is valid"
                           if fetch.status == PH.PAYMENT_REQUIRED else None)}

    observations = LC.normalize(fetch.data, endpoint="/public/coins/list/v2")
    persisted = _persist(observations, run_id)
    logger.info("[Social] LunarCrush: %d observations persisted (%d rows "
                "returned, %s daily requests left)",
                persisted, fetch.rows, LC.daily_remaining(fetch))
    return {"ok": True, "status": PH.HEALTHY, "persisted": persisted,
            "returned": fetch.rows, "run_id": run_id,
            "daily_remaining": LC.daily_remaining(fetch)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
