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

# Activity floor. An acceleration computed on a trickle is noise:
# 300% more of $200 is still $600, and the wallets in it are not the
# ones worth the RPC calls.
MIN_H1_VOLUME_USD = 20_000.0


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def surge_metrics(attrs: dict) -> dict:
    """Is this token SUDDENLY busy, or just permanently large?

    Ranking by absolute 24h volume finds big tokens, which is a different
    question and mostly the wrong one: a pair doing $2M every day outranks
    one that went from $5k to $500k in the last hour, and the second is
    where wallets that were early are still visible.

    Acceleration is measured against the token's OWN recent pace, so a
    small pair waking up scores like a large one waking up:

        vol_accel_1h = h1 volume / (h24 volume / 24)
        vol_accel_5m = m5 volume / (h1 volume / 12)

    Both are 1.0 at steady state. Two guards keep them honest:

    A pool younger than a day has no meaningful h24 baseline — dividing by
    a window the pool did not exist for manufactures enormous ratios out of
    nothing, so young pools are scored on the 5m/1h pair only.

    A token already past its move is penalised rather than ranked highly.
    h24 +110% with h6 -45% is a completed pump; buying pressure there is
    exit liquidity, and the wallets worth finding are long gone.
    """
    vol = attrs.get("volume_usd") or {}
    txn = attrs.get("transactions") or {}
    chg = attrs.get("price_change_percentage") or {}

    v_h24, v_h6, v_h1, v_m5 = (_f(vol.get("h24")), _f(vol.get("h6")),
                               _f(vol.get("h1")), _f(vol.get("m5")))
    t_h24, t_h1 = txn.get("h24") or {}, txn.get("h1") or {}
    tx_h24 = _f(t_h24.get("buys")) + _f(t_h24.get("sells"))
    tx_h1 = _f(t_h1.get("buys")) + _f(t_h1.get("sells"))

    # Pool age from the h6/h24 relationship: if essentially all of the
    # day's volume happened in the last 6 hours, the pool is young and the
    # h24 baseline is not a baseline.
    young = v_h24 <= 0 or (v_h6 / v_h24) > 0.9

    # A ratio needs a window the pool actually traded through. When m5 IS
    # essentially all of h1, the pool is minutes old and m5/(h1/12) pins to
    # its ceiling of 12.0 by construction — every brand-new pool scores
    # identically and the ranking becomes "newest first", which is a
    # different question wearing this one's clothes. Measured live: the
    # entire top 8 sat at exactly 12.0.
    minutes_old = v_h1 <= 0 or (v_m5 / v_h1) > 0.8

    vol_accel_1h = (v_h1 / (v_h24 / 24.0)) if v_h24 > 0 and not young else None
    # A young pool still has a usable 6h baseline even when 24h is fiction.
    if vol_accel_1h is None and v_h6 > 0 and not minutes_old:
        vol_accel_1h = v_h1 / (v_h6 / 6.0)
    vol_accel_5m = (v_m5 / (v_h1 / 12.0)) if v_h1 > 0 and not minutes_old else None
    txn_accel_1h = (tx_h1 / (tx_h24 / 24.0)) if tx_h24 > 0 and not young else None

    buys_h1, sells_h1 = _f(t_h1.get("buys")), _f(t_h1.get("sells"))
    buy_pressure = buys_h1 / (buys_h1 + sells_h1) if (buys_h1 + sells_h1) else None

    c_h24, c_h6, c_h1 = _f(chg.get("h24")), _f(chg.get("h6")), _f(chg.get("h1"))
    # Pumped and now falling: the move already happened and current buying
    # is exit liquidity. On a young pool h24 and h6 are the same number, so
    # the h24>50 condition never fires — hence the second clause, which
    # caught GIF/SOL sitting at -77% and still scoring 12.9.
    post_peak = (c_h24 > 50 and (c_h6 < -15 or c_h1 < -15)) or c_h6 < -25

    # Composite. Deliberately uses the acceleration available rather than
    # substituting absolute volume when it is missing, so a token cannot
    # score for merely being large.
    parts = [x for x in (vol_accel_1h, vol_accel_5m, txn_accel_1h) if x is not None]
    score = (sum(parts) / len(parts)) if parts else 0.0
    if buy_pressure is not None:
        score *= (0.5 + buy_pressure)      # 0.5x all-sells .. 1.5x all-buys
    if post_peak:
        score *= 0.25
    # An acceleration on nothing is nothing. Below a real activity floor the
    # ratio is noise — 300% more of $200 is still $600.
    if v_h1 < MIN_H1_VOLUME_USD:
        score *= 0.1
    if minutes_old:
        # Not discarded: a pool minutes old may be exactly the thing worth
        # watching. But it has no measured baseline, so it must not
        # outrank a token with an observed one.
        score *= 0.3

    return {
        "vol_accel_1h": round(vol_accel_1h, 3) if vol_accel_1h is not None else None,
        "vol_accel_5m": round(vol_accel_5m, 3) if vol_accel_5m is not None else None,
        "txn_accel_1h": round(txn_accel_1h, 3) if txn_accel_1h is not None else None,
        "buy_pressure_1h": round(buy_pressure, 3) if buy_pressure is not None else None,
        "price_change_h1": c_h1, "price_change_h6": c_h6, "price_change_h24": c_h24,
        "young_pool": young,
        "minutes_old": minutes_old,
        "post_peak": post_peak,
        "surge_score": round(score, 3),
    }


def interesting_solana_mints(limit: int = 10, errors: list | None = None) -> list[dict]:
    """Mints worth investigating, from pools that have actually caught on.

    Reuses GeckoTerminal, already the desk's DEX source. `dex_discovery`
    queries the same endpoints but keeps only `pool_address` and drops
    `relationships.base_token` — which is the mint, and the only field this
    pipeline needs. Read here directly rather than widening that module's
    contract for a different consumer.
    """
    from lib.geckoterminal import solana_pools

    out: list[dict] = []
    seen: set[str] = set()
    for path in ("trending_pools", "new_pools"):
        # Shared client: retries 429 rather than reporting an empty market.
        for pool in solana_pools(path, errors=errors):
            rel = pool.get("relationships") or {}
            token_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
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
                "volume_24h_usd": _f((a.get("volume_usd") or {}).get("h24")),
                "liquidity_usd": _f(a.get("reserve_in_usd")),
                **surge_metrics(a),
            })
    # Ranked by ACCELERATION, not size. See surge_metrics: absolute volume
    # answers "which token is big", and the question here is "which token
    # just woke up", where wallets that were early are still visible.
    out.sort(key=lambda t: t["surge_score"], reverse=True)
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


def traders_of_pool(pool_address: str, signatures: int = 30,
                    inspect: int = 12, errors: list | None = None) -> list[dict]:
    """Wallets that TRADED through a pool — a different population entirely.

    `owners_of_token` finds who HOLDS the biggest bags right now. That is a
    snapshot, and it systematically misses the wallet worth finding most: a
    trader who buys early, sells into strength and is flat again by the time
    anyone looks. Such a wallet never appears in the top 20 holders and is
    invisible to holdings-based discovery, however profitable it is.

    Reading the pool's recent signatures finds them, because a trade leaves
    a signature whether or not the position survives. Costs more — one call
    for the signature list plus one per transaction inspected — so it is
    bounded and reserved for tokens that already look interesting.

    Signers, not accountKeys generally: the fee payer and signers are the
    parties that ACTED. Routers and pool accounts appear in the key list of
    every swap without having chosen anything.
    """
    from lib.helius_client import rpc

    out: list[dict] = []
    try:
        sigs = rpc("getSignaturesForAddress",
                   [pool_address, {"limit": max(1, min(signatures, 100))}]) or []
    except Exception as e:
        if errors is not None:
            errors.append(f"{pool_address[:8]}… signatures: {type(e).__name__}")
        return out

    seen: set[str] = set()
    for entry in sigs[:max(1, min(inspect, len(sigs)))]:
        sig = entry.get("signature")
        if not sig:
            continue
        try:
            tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 0}])
        except Exception as e:
            if errors is not None:
                errors.append(f"{sig[:8]}…: {type(e).__name__}")
            continue
        if not tx:
            continue
        msg = (tx.get("transaction") or {}).get("message") or {}
        for k in (msg.get("accountKeys") or []):
            addr = k.get("pubkey") if isinstance(k, dict) else k
            is_signer = k.get("signer") if isinstance(k, dict) else False
            if not (is_signer and addr) or addr in seen:
                continue
            seen.add(addr)
            out.append({"owner": addr, "signature": sig,
                        "block_time": tx.get("blockTime"),
                        "slot": tx.get("slot")})
    return out


def discover_from_tokens(max_tokens: int = 5, db=None,
                         include_traders: bool = True) -> dict:
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
             "excluded": 0, "already_known": 0, "errors": [],
             # Tracked separately because the two sources find genuinely
             # different populations: holders are whoever is sitting on a
             # bag right now, traders are whoever ACTED.
             "from_holders": 0, "from_traders": 0}

    if not discovery_enabled():
        stats["errors"].append("discovery disabled "
                               "(HELIUS_WALLET_DISCOVERY_ENABLED / no key)")
        return stats

    tokens = interesting_solana_mints(limit=max_tokens, errors=stats["errors"])
    max_candidates = _cfg_int("HELIUS_DISCOVERY_MAX_CANDIDATES", 5000)

    def _run(session):
        # Count only the wallets consuming expensive analysis capacity, not
        # the whole registry. The unfiltered count() this replaces meant
        # every archived wallet, every known exchange and every promoted
        # SMART_MONEY row pushed against a cap that exists to bound WORK —
        # so the registry doing its job (accumulating learned identities
        # permanently) would eventually switch discovery off entirely.
        from lib.wallet_registry import active_analysis_count
        active = active_analysis_count(session)
        if active >= max_candidates:
            stats["errors"].append(
                f"{active} wallets awaiting analysis, at "
                f"HELIUS_DISCOVERY_MAX_CANDIDATES ({max_candidates}) — "
                f"the cap bounds the analysis queue, not registry size")
            return
        for tok in tokens:
            stats["tokens_scanned"] += 1
            candidates = [(r, "token_holders")
                          for r in owners_of_token(tok["mint"], errors=stats["errors"])]
            if include_traders and tok.get("pool"):
                # The population holdings-based discovery cannot see: a
                # wallet that bought early, sold into strength and is flat
                # again never enters the top 20 holders, however good it is.
                candidates += [(r, "pool_traders")
                               for r in traders_of_pool(tok["pool"],
                                                        errors=stats["errors"])]
            for row, source in candidates:
                owner = row["owner"]
                stats["owners_seen"] += 1
                existing = session.query(WalletRegistry).filter(
                    WalletRegistry.address == owner).first()
                if existing is not None:
                    stats["already_known"] += 1
                    continue

                verdict = classify(owner)
                verb = "traded" if source == "pool_traders" else "holder of"
                reason = (f"{verb} {tok.get('name') or tok['mint'][:8]} "
                          f"({tok['source_list']}, ${tok['volume_24h_usd']:,.0f} 24h vol); "
                          f"{verdict['reason']}")
                if not verdict.get("is_trader"):
                    # Written, not skipped. A recorded exclusion is cheaper
                    # than re-classifying the same exchange every pass.
                    w = upsert_wallet(session, owner, source=source,
                                      discovery_reason=reason)
                    w.status = "EXCLUDED_ENTITY"
                    w.entity_type = verdict["entity_type"]
                    w.entity_name = verdict.get("entity_name")
                    w.is_trader, w.is_protocol = False, True
                    stats["excluded"] += 1
                    continue

                w = upsert_wallet(session, owner, source=source,
                                  discovery_reason=reason, status="CANDIDATE")
                # The classifier decided this; persist it. It was only ever
                # written on the EXCLUDED path, so every trader candidate
                # sat at NULL and "is_trader == True" matched nothing.
                w.is_trader = True
                w.is_protocol = False
                w.entity_type = verdict["entity_type"]
                stats["candidates_created"] += 1
                stats["from_traders" if source == "pool_traders" else "from_holders"] += 1

    if db is not None:
        _run(db)
    else:
        with get_db() as _db:
            _run(_db)
    return stats
