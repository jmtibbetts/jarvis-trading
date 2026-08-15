"""USD value for Solana token amounts — by precedence, or not at all.

`/v1/wallet/{addr}/transfers` carries amount and mint but no USD, so
absolute size (§27's whale floor) needs a value the transfer does not
supply. Three sources exist and they are not equally trustworthy, so the
resolution order is explicit and every price carries the source that
produced it.

  PEG        a USDT or USDC amount IS its dollar value. Exact, free, and
             needs no lookup — and it is not a rounding convenience:
             measured on a live wallet, 29 of 154 transfers were
             stablecoin-denominated, so this alone prices a fifth of the
             flow with no network call at all.
  HELIUS     /v1/wallet/{addr}/balances returns pricePerToken per mint.
             Partial: 6 of 100 mints priced on a live sample, so it
             answers for SOL and majors and stays silent on the tail.
  MARKET     the desk's existing MarketAsset table, joined by SYMBOL.
             Weakest link, because a mint-to-symbol match is not an
             identity: ticker collisions are routine on Solana and a
             wrong join silently values a scam token at a real token's
             price. Only used when Helius has already supplied the
             symbol, and marked as the lowest-confidence source.
  NONE       abstain. An unpriced mint returns None and the caller says
             "no USD value available" — the alternative is a fabricated
             number feeding a whale threshold, which is worse than a gap.

THE PEG IS AN ASSUMPTION, NOT A FACT. A depegged stablecoin values
wrongly here, and by exactly the depeg. That is acceptable for sizing a
whale threshold and would not be acceptable for marking a position, so
this module is not a valuation engine and should not become one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Verified against Helius getAsset 2026-08-17 rather than from memory:
# symbol, name and 6 decimals all confirmed on-chain.
STABLECOIN_MINTS: dict[str, str] = {
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
}

# Confidence by source, so a caller can require a floor before acting.
SOURCE_CONFIDENCE = {"peg": 0.99, "helius": 0.9, "market": 0.5}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def price_map(mints, helius_prices: dict | None = None,
              market_prices: dict | None = None,
              symbols: dict | None = None) -> dict[str, dict]:
    """Resolve mint -> {price, source, confidence}. Pure.

    helius_prices: mint -> pricePerToken (from a balances response)
    market_prices: SYMBOL -> price (the desk's existing providers)
    symbols:       mint -> symbol, needed only for the market fallback
    """
    helius_prices = helius_prices or {}
    market_prices = {str(k).upper(): v for k, v in (market_prices or {}).items()}
    symbols = symbols or {}
    out: dict[str, dict] = {}

    for mint in dict.fromkeys(m for m in (mints or []) if m):
        if mint in STABLECOIN_MINTS:
            out[mint] = {"price": 1.0, "source": "peg",
                         "confidence": SOURCE_CONFIDENCE["peg"],
                         "symbol": STABLECOIN_MINTS[mint]}
            continue
        p = _num(helius_prices.get(mint))
        if p > 0:
            out[mint] = {"price": p, "source": "helius",
                         "confidence": SOURCE_CONFIDENCE["helius"],
                         "symbol": symbols.get(mint)}
            continue
        sym = str(symbols.get(mint) or "").upper()
        # No symbol means no market join. Guessing one from a mint prefix
        # would be inventing an identity.
        if sym and _num(market_prices.get(sym)) > 0:
            out[mint] = {"price": _num(market_prices[sym]), "source": "market",
                         "confidence": SOURCE_CONFIDENCE["market"],
                         "symbol": sym}
            continue
        out[mint] = {"price": None, "source": None, "confidence": 0.0,
                     "symbol": symbols.get(mint)}
    return out


def usd_value(amount, mint: str, prices: dict) -> float | None:
    """Dollar value of a token amount, or None when it cannot be known."""
    entry = (prices or {}).get(mint) or {}
    price = entry.get("price")
    if price is None:
        return None
    return abs(_num(amount)) * _num(price)


def value_transfers(transfers: list[dict], prices: dict) -> list[dict]:
    """Attach usd_value to transfers, leaving it None where unknown.

    Mutating nothing: an unpriced transfer keeps every field it had and
    gains an explicit None, so downstream `if usd_value is None` is a
    real branch rather than an accident of a missing key.
    """
    out = []
    for t in transfers or []:
        v = usd_value(t.get("amount"), t.get("mint"), prices)
        entry = (prices or {}).get(t.get("mint")) or {}
        out.append({**t, "usd_value": v,
                    "usd_source": entry.get("source"),
                    "usd_confidence": entry.get("confidence", 0.0)})
    return out


def coverage(valued: list[dict]) -> dict:
    """How much of a set could actually be priced, by source.

    Reported rather than assumed: a whale scan over transfers that are
    80% unpriced is a scan with an 80% blind spot, and that fact belongs
    on screen next to its results.
    """
    total = len(valued or [])
    by_source: dict[str, int] = {}
    priced = 0
    for t in valued or []:
        if t.get("usd_value") is not None:
            priced += 1
            s = t.get("usd_source") or "unknown"
            by_source[s] = by_source.get(s, 0) + 1
    return {"total": total, "priced": priced, "unpriced": total - priced,
            "priced_pct": round(100.0 * priced / total, 1) if total else None,
            "by_source": by_source}


def resolve_prices(mints, address_for_helius: str | None = None) -> dict:
    """Build a price map from live sources. Never raises.

    Helius prices arrive as a side effect of a balances call, so this
    only spends one when an address is supplied — pricing a counterparty's
    mints does not justify fetching that counterparty's whole portfolio.
    """
    mints = [m for m in dict.fromkeys(mints or []) if m]
    helius_prices: dict[str, float] = {}
    symbols: dict[str, str] = {}

    if address_for_helius:
        try:
            from lib.helius_client import balances
            for row in (balances(address_for_helius) or {}).get("balances") or []:
                mint = row.get("mint")
                if not mint:
                    continue
                if row.get("pricePerToken"):
                    helius_prices[mint] = _num(row["pricePerToken"])
                if row.get("symbol"):
                    symbols[mint] = row["symbol"]
        except Exception as e:
            logger.warning(f"[TokenPricing] helius prices unavailable: {e}")

    market_prices: dict[str, float] = {}
    try:
        from app.database import MarketAsset, get_db
        wanted = {s.upper() for s in symbols.values() if s}
        if wanted:
            with get_db() as db:
                for a in db.query(MarketAsset).all():
                    if a.symbol and a.price and a.symbol.upper() in wanted:
                        market_prices[a.symbol.upper()] = float(a.price)
    except Exception as e:
        logger.debug(f"[TokenPricing] market prices unavailable: {e}")

    return price_map(mints, helius_prices, market_prices, symbols)
