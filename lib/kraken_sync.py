"""Pull the operator's real Kraken account state into Jarvis. Read-only.

Alpaca covers 73 crypto pairs and the operator's actual trading happens on
Kraken — hundreds of pairs, FX majors, tokenized gold, margin. Until this
existed, Jarvis was blind to the account it was nominally helping run: the
Positions view showed Alpaca's sliver, and the execution model starved
while real fills — with real prices and real fees — accumulated unseen in
Kraken's trades history.

Everything here uses the READ scopes the operator granted (Query Funds,
Open Orders, Open Positions, Trades History, Ledger/Volume). There is no
order placement in this module and none planned without an explicit,
separately-keyed decision.

Idempotent by trade id: Kraken trade ids are stable, so re-syncing the
same window inserts nothing twice and resuming after downtime needs no
bookkeeping beyond "sync since the newest stored fill".
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Overlap re-fetched at each sync so a fill that landed during the previous
# sync's pagination cannot fall between pages. Duplicates are free (id upsert).
RESYNC_OVERLAP_SEC = 3600

# One page = 50 trades; Kraken's private rate limiter tolerates a few pages
# per call comfortably at a 30-minute cadence. First-ever sync walks the
# whole history over several runs rather than hammering it in one.
MAX_PAGES_PER_RUN = 6


def _newest_stored_ts() -> float:
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as c:
            v = c.execute(text(
                "SELECT MAX(executed_at_unix) FROM kraken_trades")).fetchone()[0]
            return float(v or 0.0)
    except Exception:
        return 0.0


def sync_trades() -> dict:
    """Fetch fills newer than what's stored (minus overlap), persist them."""
    from app.database import KrakenTrade, get_db
    from lib.kraken_account import is_configured, trades_history

    if not is_configured():
        return {"ok": False, "reason": "kraken credentials not configured"}

    since = max(0.0, _newest_stored_ts() - RESYNC_OVERLAP_SEC)
    inserted = scanned = 0
    for page in range(MAX_PAGES_PER_RUN):
        out = trades_history(start=since or None, offset=page * 50)
        if not out.get("ok"):
            return {"ok": False, "reason": out.get("reason") or "trades fetch failed",
                    "inserted": inserted}
        trades = out.get("trades") or []
        if not trades:
            break
        scanned += len(trades)
        with get_db() as db:
            for t in trades:
                # A fill without a timestamp cannot be ordered against
                # anything — and a 0.0 renders as 1970, poisoning the span
                # and any time-based join. Skip rather than store a lie.
                if not t.get("executed_at"):
                    continue
                exists = db.query(KrakenTrade).filter(
                    KrakenTrade.trade_id == t["trade_id"]).first()
                if exists:
                    continue
                db.add(KrakenTrade(
                    trade_id=t["trade_id"],
                    order_id=t.get("order_id"),
                    pair=t.get("pair"),
                    side=t.get("side"),
                    order_type=t.get("order_type"),
                    price=t.get("price"),
                    cost=t.get("cost"),
                    fee=t.get("fee"),
                    volume=t.get("volume"),
                    margin=t.get("margin"),
                    executed_at_unix=t.get("executed_at"),
                    executed_at=datetime.fromtimestamp(
                        t["executed_at"], tz=timezone.utc).isoformat(),
                ))
                inserted += 1
        if len(trades) < 50:
            break
        time.sleep(1.2)     # stay polite to the private rate limiter

    if inserted:
        logger.info(f"[KrakenSync] {inserted} new fill(s) stored ({scanned} scanned)")
    return {"ok": True, "inserted": inserted, "scanned": scanned}


def account_snapshot() -> dict:
    """Balances + open positions + open orders + measured fee tier, one call.

    The fee tier matters beyond display: lib/venues assumes the most
    expensive tier when volume is unknown, so the measured 30-day volume
    turns an assumed cost into a fact the EV gate can trust.
    """
    from lib.kraken_account import (balances, check_connection, fee_tier,
                                    open_orders, open_positions)
    conn = check_connection()
    if not conn.get("connected") or not conn.get("authenticated"):
        return {"ok": False, "reason": "kraken not connected", "detail": conn}
    out = {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()}
    for key, fn in (("balances", balances), ("positions", open_positions),
                    ("orders", open_orders), ("fee_tier", fee_tier)):
        try:
            r = fn()
            out[key] = r if r.get("ok") else {"error": r.get("reason")}
        except Exception as e:
            out[key] = {"error": str(e)[:120]}
    return out


def fills_summary() -> dict:
    """What the stored history says about the operator's real trading —
    the numbers the execution model will train against."""
    from app.database import engine
    from sqlalchemy import text
    try:
        with engine.connect() as c:
            total, fees, vol = c.execute(text(
                "SELECT COUNT(*), COALESCE(SUM(fee),0), COALESCE(SUM(cost),0) "
                "FROM kraken_trades")).fetchone()
            by_pair = c.execute(text(
                "SELECT pair, COUNT(*), ROUND(SUM(cost),2) FROM kraken_trades "
                "GROUP BY pair ORDER BY COUNT(*) DESC LIMIT 10")).fetchall()
            span = c.execute(text(
                "SELECT MIN(executed_at), MAX(executed_at) FROM kraken_trades")).fetchone()
        return {"ok": True, "fills": int(total or 0),
                "total_fees_usd": round(float(fees or 0), 2),
                "total_notional_usd": round(float(vol or 0), 2),
                "span": {"first": span[0], "last": span[1]},
                "by_pair": [{"pair": p, "fills": n, "notional": v}
                            for p, n, v in by_pair]}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}
