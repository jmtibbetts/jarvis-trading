"""Autonomous wallet discovery — find wallet #1 without being told one.

The requirement that shapes this module: JARVIS must start from TOKEN
ACTIVITY, not from a known wallet. A system that can only expand a list it
was handed is a watchlist with extra steps.

The chain, verified against the live API before any of it was written:

    interesting Solana token (mint)
        -> getTokenLargestAccounts       20 token ACCOUNTS, 1 RPC call
        -> getMultipleAccounts jsonParsed  their OWNERS, 1 RPC call
        -> classify                        exchange? program? token account?
        -> candidate

Two RPC calls per token, which is what makes scanning affordable.

The single most important thing here is what it REFUSES. The largest
holder of the first token queried on live data was a Binance hot wallet.
A discoverer that skips classification does not find smart money — it
finds exchanges, pools and routers, ranks them top because they hold the
most and trade constantly, and buries every real trader beneath them.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

GT_BASE = "https://api.geckoterminal.com/api/v2"

# Holders to pull per token. getTokenLargestAccounts caps at 20 and the
# tail is mostly dust; the interesting wallets are near the top but below
# the exchanges, which is exactly the band classification clears out.
HOLDERS_PER_TOKEN = 20


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def interesting_solana_mints(limit: int = 10, errors: list | None = None) -> list[dict]:
    """Mints worth investigating, from pools that have actually caught on.

    Reuses GeckoTerminal, already the desk's DEX source. `dex_discovery`
    queries the same endpoints but keeps only `pool_address` and drops
    `relationships.base_token` — which is the mint, and the only field this
    pipeline needs. Read here directly rather than widening that module's
    contract for a different consumer.
    """
    import httpx

    out: list[dict] = []
    seen: set[str] = set()
    for path in ("trending_pools", "new_pools"):
        try:
            r = httpx.get(f"{GT_BASE}/networks/solana/{path}", timeout=30.0)
            if r.status_code != 200:
                if errors is not None:
                    errors.append(f"{path}: HTTP {r.status_code}")
                continue
            for pool in (r.json().get("data") or []):
                rel = pool.get("relationships") or {}
                token_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
                # GeckoTerminal ids are "<network>_<address>".
                mint = token_id.split("_", 1)[1] if "_" in token_id else ""
                if not mint or mint in seen:
                    continue
                a = pool.get("attributes") or {}
                seen.add(mint)
                out.append({
                    "mint": mint,
                    "name": a.get("name"),
                    "pool": a.get("address"),
                    "source_list": path,
                    "volume_24h_usd": float((a.get("volume_usd") or {}).get("h24") or 0),
                    "liquidity_usd": float(a.get("reserve_in_usd") or 0),
                })
        except Exception as e:
            if errors is not None:
                errors.append(f"{path}: {type(e).__name__}")
            logger.debug(f"[WalletDiscovery] {path}: {e}")
    out.sort(key=lambda t: t["volume_24h_usd"], reverse=True)
    return out[:limit]


def owners_of_token(mint: str, limit: int = HOLDERS_PER_TOKEN,
                    errors: list | None = None) -> list[dict]:
    """Owner WALLETS holding a mint, resolved from its token accounts.

    getTokenLargestAccounts returns token accounts, which cannot have a
    trading history of their own — the owner is the thing that trades.
    Skipping this step fills the registry with addresses that can never be
    scored.
    """
    from lib.helius_client import rpc

    try:
        accounts = (rpc("getTokenLargestAccounts", [mint]) or {}).get("value") or []
    except Exception as e:
        if errors is not None:
            errors.append(f"{mint[:8]}… largest accounts: {type(e).__name__}")
        return []
    addrs = [a.get("address") for a in accounts[:limit] if a.get("address")]
    if not addrs:
        return []

    try:
        got = (rpc("getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}])
               or {}).get("value") or []
    except Exception as e:
        if errors is not None:
            errors.append(f"{mint[:8]}… owner resolve: {type(e).__name__}")
        return []

    owners: list[dict] = []
    for token_account, info in zip(addrs, got):
        if not info:
            continue
        try:
            parsed = info["data"]["parsed"]["info"]
            owner = parsed.get("owner")
            amount = float((parsed.get("tokenAmount") or {}).get("uiAmount") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if owner:
            owners.append({"owner": owner, "token_account": token_account,
                           "balance": amount})
    return owners


def discover_from_tokens(max_tokens: int = 5, db=None) -> dict:
    """One discovery pass: interesting tokens -> owners -> candidates.

    Every address is classified BEFORE it is written. Infrastructure is
    recorded as EXCLUDED_ENTITY rather than dropped, so the next pass that
    meets the same exchange recognises it instead of paying to classify it
    again — the exclusion list teaches itself.
    """
    from app.database import get_db
    from lib.wallet_classify import classify
    from lib.wallet_registry import discovery_enabled, upsert_wallet

    stats = {"tokens_scanned": 0, "owners_seen": 0, "candidates_created": 0,
             "excluded": 0, "already_known": 0, "errors": []}

    if not discovery_enabled():
        stats["errors"].append("discovery disabled "
                               "(HELIUS_WALLET_DISCOVERY_ENABLED / no key)")
        return stats

    tokens = interesting_solana_mints(limit=max_tokens, errors=stats["errors"])
    max_candidates = _cfg_int("HELIUS_DISCOVERY_MAX_CANDIDATES", 5000)

    def _run(session):
        from app.database import WalletRegistry
        if session.query(WalletRegistry).count() >= max_candidates:
            stats["errors"].append(
                f"registry at HELIUS_DISCOVERY_MAX_CANDIDATES ({max_candidates})")
            return
        for tok in tokens:
            stats["tokens_scanned"] += 1
            for row in owners_of_token(tok["mint"], errors=stats["errors"]):
                owner = row["owner"]
                stats["owners_seen"] += 1
                existing = session.query(WalletRegistry).filter(
                    WalletRegistry.address == owner).first()
                if existing is not None:
                    stats["already_known"] += 1
                    continue

                verdict = classify(owner)
                reason = (f"holder of {tok.get('name') or tok['mint'][:8]} "
                          f"({tok['source_list']}, ${tok['volume_24h_usd']:,.0f} 24h vol); "
                          f"{verdict['reason']}")
                if not verdict.get("is_trader"):
                    # Written, not skipped. A recorded exclusion is cheaper
                    # than re-classifying the same exchange every pass.
                    w = upsert_wallet(session, owner, source="token_holders",
                                      discovery_reason=reason)
                    w.status = "EXCLUDED_ENTITY"
                    w.entity_type = verdict["entity_type"]
                    w.entity_name = verdict.get("entity_name")
                    w.is_trader, w.is_protocol = False, True
                    stats["excluded"] += 1
                    continue

                upsert_wallet(session, owner, source="token_holders",
                              discovery_reason=reason, status="CANDIDATE")
                stats["candidates_created"] += 1

    if db is not None:
        _run(db)
    else:
        with get_db() as _db:
            _run(_db)
    return stats
