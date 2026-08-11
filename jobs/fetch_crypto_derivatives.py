"""Fetch and persist crypto perpetual-futures state (funding/OI/long-short
ratio/liquidations) — free OKX public REST, no vendor key. See
lib/crypto_derivatives.py for source details and why OKX is the sole exchange."""
import logging
from datetime import datetime, timedelta, timezone

from app.database import CryptoDerivativesSnapshot, CryptoLiquidation, MarketAsset, get_db
from lib.crypto_derivatives import (
    DEFAULT_DERIVATIVES_WATCHLIST, classify_oi_price_action,
    fetch_derivatives_snapshot, fetch_recent_liquidations, summarize_liquidations,
)

logger = logging.getLogger(__name__)

# Single-event liquidation big enough to be worth a UI push, not routine noise.
NOTABLE_LIQUIDATION_USD = 250_000


def _price_for(symbol: str) -> float | None:
    with get_db() as db:
        row = db.query(MarketAsset).filter(MarketAsset.symbol == f"{symbol}/USD").first()
        return row.price if row else None


def run():
    saved_snapshots = 0
    saved_liquidations = 0
    notable_liquidations = []

    for symbol in DEFAULT_DERIVATIVES_WATCHLIST:
        snap = fetch_derivatives_snapshot(symbol)
        if snap:
            with get_db() as db:
                prev = (
                    db.query(CryptoDerivativesSnapshot)
                    .filter(CryptoDerivativesSnapshot.symbol == symbol)
                    .order_by(CryptoDerivativesSnapshot.fetched_at.desc())
                    .first()
                )
                price = _price_for(symbol)
                db.add(CryptoDerivativesSnapshot(
                    symbol=symbol, inst_id=snap["inst_id"], price=price,
                    funding_rate=snap["funding_rate"],
                    open_interest_usd=snap["open_interest_usd"],
                    long_short_ratio=snap["long_short_ratio"],
                ))
                saved_snapshots += 1
                if prev and prev.open_interest_usd and prev.price and price:
                    oi_change_pct = (snap["open_interest_usd"] - prev.open_interest_usd) / prev.open_interest_usd * 100
                    price_change_pct = (price - prev.price) / prev.price * 100
                    action = classify_oi_price_action(oi_change_pct, price_change_pct)
                    if action:
                        logger.debug(f"[CryptoDerivatives] {symbol} OI/price action: {action}")
        else:
            logger.debug(f"[CryptoDerivatives] No snapshot for {symbol} (no OKX perpetual or fetch failed)")

        liquidations = fetch_recent_liquidations(symbol, limit=100)
        if liquidations:
            with get_db() as db:
                existing_ts = {
                    row[0] for row in db.query(CryptoLiquidation.liquidated_at)
                    .filter(CryptoLiquidation.symbol == symbol)
                    .filter(CryptoLiquidation.liquidated_at.in_([l["liquidated_at"] for l in liquidations]))
                    .distinct().all()
                }
                for l in liquidations:
                    if l["liquidated_at"] in existing_ts:
                        continue
                    db.add(CryptoLiquidation(
                        symbol=l["symbol"], inst_id=l["inst_id"], side=l["side"],
                        pos_side=l["pos_side"], price=l["price"], size=l["size"],
                        notional_usd=l["notional_usd"], liquidated_at=l["liquidated_at"],
                    ))
                    saved_liquidations += 1
                    if l["notional_usd"] >= NOTABLE_LIQUIDATION_USD:
                        notable_liquidations.append(l)

    logger.info(
        f"[CryptoDerivatives] {saved_snapshots} snapshot(s), {saved_liquidations} new liquidation(s) saved"
    )

    if notable_liquidations:
        top = max(notable_liquidations, key=lambda l: l["notional_usd"])
        try:
            from app.ws import manager as ws_manager
            ws_manager.broadcast_from_thread("liquidation", {
                "symbol": top["symbol"], "pos_side": top["pos_side"],
                "notional_usd": top["notional_usd"], "price": top["price"],
                "liquidated_at": top["liquidated_at"], "count": len(notable_liquidations),
            })
        except Exception:
            pass
        try:
            from lib.alert_engine import raise_alert
            severity = "HIGH_PRIORITY" if top["notional_usd"] >= 1_000_000 else "WATCH"
            raise_alert(
                source="crypto_derivatives", severity=severity,
                title=f"Large {top['pos_side']} liquidation: {top['symbol']}",
                detail=f"${top['notional_usd']:,.0f} at {top['price']:,.2f}"
                       + (f" ({len(notable_liquidations)} notable liquidations this run)" if len(notable_liquidations) > 1 else ""),
                dedup_key=f"liquidation_{top['symbol']}_{top['liquidated_at']}", cooldown_minutes=1440,
                extra={"symbol": top["symbol"], "pos_side": top["pos_side"], "notional_usd": top["notional_usd"]},
            )
        except Exception:
            pass
