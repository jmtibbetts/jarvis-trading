"""Solana wallet activity — JARVIS polls Helius; nothing listens.

Replaces the webhook design. An earlier plan put an internet-facing
mailbox VM in front of Helius webhooks because webhooks PUSH and this
machine — which holds every venue credential — accepts no inbound
connection from anywhere. Polling reaches the same data while preserving
that property trivially, with no exposed host to isolate, patch, or
firewall. The receiver was dropped rather than hardened: the most secure
version of an internet-facing service is not running one.

Latency is the trade, and it does not bind here. Wallet flow is the SLOW
shadow-only context layer, like on-chain fundamentals — it describes who
is moving what, never when to enter.

SOURCE: GET /v1/wallet/{address}/transfers, chosen over
/v0/addresses/{address}/transactions after probing both live. v1 arrives
pre-normalized with `direction` and `counterparty`; v0 requires inferring
direction by comparing fromUserAccount/toUserAccount against the watched
address, and a direction inferred backwards is invisible once stored.

AMOUNTS COME FROM `amount`, NEVER FROM amountRaw/decimals. Measured
2026-08-17 against a live USDT transfer: amount=49.7, amountRaw="50",
decimals=0 — USDT has six decimals, and those three fields reconcile
under no exponent at all. Every SOL row in the same response reconciles
exactly, so the defect is per-token, not systemic, which is worse: it is
the kind that passes a spot check. The v0 endpoint independently reports
49.7 for the same signature. `int(amountRaw) / 10**decimals` would have
stored 50.

Idempotent by dedup_key rather than by cursor bookkeeping. Re-polling an
overlapping window is therefore free, which is why this keeps no cursor
state that could drift, corrupt, or silently skip a window.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

PARSER_VERSION = "helius_v1_transfers_v1"

DEFAULT_BASE = "https://api.helius.xyz"


def _config() -> tuple[str, str, list[str], int]:
    return (
        (os.getenv("HELIUS_BASE") or DEFAULT_BASE).rstrip("/"),
        os.getenv("HELIUS_API_KEY", "").strip(),
        [w.strip() for w in os.getenv("HELIUS_WATCH_WALLETS", "").split(",")
         if w.strip()],
        max(1, min(int(os.getenv("HELIUS_PAGE_LIMIT", "100") or 100), 1000)),
    )


def parse_transfers(payload: dict, address: str) -> list[dict]:
    """One /v1/wallet/{addr}/transfers response -> observations.

    Returns [] for anything unrecognized. An empty list is honest; a
    fabricated observation is not.
    """
    out = []
    rows = (payload or {}).get("data")
    if not isinstance(rows, list):
        return out
    for t in rows:
        if not isinstance(t, dict):
            continue
        sig = t.get("signature")
        if not sig:
            continue
        amount = t.get("amount")
        if amount is None:
            # No usable magnitude. Deriving one from amountRaw/decimals is
            # exactly the reconstruction this module refuses to do.
            continue
        direction = str(t.get("direction") or "").lower()
        if direction not in ("in", "out"):
            continue
        try:
            value = abs(float(amount))
        except (TypeError, ValueError):
            continue
        mint = t.get("mint")
        out.append({
            "signature": sig,
            "timestamp": t.get("timestamp"),
            "direction": direction,
            "counterparty": t.get("counterparty"),
            "mint": mint,
            # `symbol` is frequently null for SPL tokens; the mint is the
            # only always-present identity, so it is the fallback.
            "symbol": t.get("symbol") or mint,
            "amount": value,
            "wallet": address,
        })
    return out


def _fetch(address: str) -> tuple[dict | None, str | None]:
    import httpx

    base, key, _, limit = _config()
    try:
        r = httpx.get(f"{base}/v1/wallet/{address}/transfers",
                      params={"api-key": key, "limit": limit}, timeout=30.0)
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        # The key rides in the query string, so an httpx error message can
        # carry the full URL. Never let it reach a log.
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        if key:
            msg = msg.replace(key, "<redacted>")
        return None, msg


def collect_once() -> dict:
    """One polling pass across every watched wallet.

    Never raises — a context feed must not be able to take the desk down.
    """
    base, key, wallets, limit = _config()
    if not key:
        return {"skipped": "HELIUS_API_KEY not configured"}
    if not wallets:
        # An empty watchlist is a configuration state, not a failure, and
        # this module does not invent addresses to follow.
        return {"skipped": "HELIUS_WATCH_WALLETS is empty"}

    observations, errors, truncated = [], [], []
    for address in wallets:
        payload, err = _fetch(address)
        if err:
            errors.append(f"{address[:8]}…: {err}")
            continue
        obs = parse_transfers(payload, address)
        if payload and not obs and (payload.get("data") or []):
            # Rows arrived and none survived parsing — the signal that
            # this parser needs correcting, and it must never look like a
            # quiet chain.
            errors.append(f"{address[:8]}…: {len(payload['data'])} rows, 0 parsed")
        observations.extend(obs)
        # A wallet busier than one page between polls loses the overflow.
        # Say so rather than let a gap read as calm.
        if ((payload or {}).get("pagination") or {}).get("hasMore"):
            truncated.append(address)

    stored = _store(observations)
    out = {
        "wallets": len(wallets), "observations": len(observations),
        "stored": stored, "duplicates": len(observations) - stored,
        "errors": errors, "page_limit": limit,
        "truncated_wallets": truncated, "parser": PARSER_VERSION,
    }
    if observations or errors:
        logger.info(f"[WalletActivity] {out}")
    return out


def _store(observations: list[dict]) -> int:
    """Land observations as canonical events.

    Shadow-only: wallet flow informs nothing until an ablation earns it a
    place, exactly like every other feed added this month.
    """
    if not observations:
        return 0
    from lib.event_store import get_store
    from lib.market_events import OnChainEvent, event_to_dict, make_meta

    rows = []
    for o in observations:
        try:
            ts = float(o["timestamp"]) if o.get("timestamp") else None
        except (TypeError, ValueError):
            ts = None
        try:
            rows.append(event_to_dict(OnChainEvent(
                meta=make_meta("helius", PARSER_VERSION, ts),
                symbol=str(o.get("symbol") or "UNKNOWN"),
                metric=f"wallet_transfer_{o['direction']}",
                value=float(o["amount"]),
                chain="solana",
                # One signature can move several mints between several
                # counterparties; all four are needed for identity.
                dedup_key=(f"helius:{o['signature']}:{o.get('mint')}"
                           f":{o.get('counterparty')}:{o['direction']}"),
            )))
        except Exception as e:
            logger.debug(f"[WalletActivity] row skipped: {e}")
    return get_store().append(rows) if rows else 0


def status() -> dict:
    base, key, wallets, limit = _config()
    return {
        "configured": bool(key and wallets),
        "has_key": bool(key),
        "wallets_watched": len(wallets),
        "base": base,
        "page_limit": limit,
        "parser": PARSER_VERSION,
        "note": ("JARVIS polls Helius directly; no inbound connection and no "
                 "internet-facing host. Wallet flow is shadow-only context."),
    }
