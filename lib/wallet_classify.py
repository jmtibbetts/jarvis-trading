"""Layers 2–4: on-chain evidence, entity classification, trader eligibility.

Structural validation (LAYER 1, `wallet_registry.is_valid_address`) answers
one narrow question: is this 32 base58-encoded bytes that round-trip? It
cannot answer any of the questions that actually matter, and pretending
otherwise is how a validator starts rejecting real addresses.

The three distinctions this module exists to keep apart:

    VALID SOLANA ADDRESS   != HUMAN / TRADER WALLET
    OFF-CURVE              != INVALID
    NO ON-CHAIN ACTIVITY   != MALFORMED

Each is a separate finding with its own evidence, so the UI can say "valid
address, no trading activity found" instead of the flatly wrong "invalid
address" — and so autonomous discovery does not fill the registry with
PDAs, token accounts, routers and pools while still recognising them as
legitimate Solana addresses.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Entity classes. Only TRADER_CANDIDATE and UNKNOWN are ever eligible for
# trader scoring; everything else is a real address doing a real job that
# is simply not "trading with its own money".
TRADER_CANDIDATE = "TRADER_CANDIDATE"
NON_TRADER_ENTITIES = {
    "PDA", "PROGRAM", "TOKEN_ACCOUNT", "DEX_ROUTER", "LIQUIDITY_POOL",
    "VAULT", "CEX", "BRIDGE", "TREASURY", "MARKET_MAKER", "CUSTODY",
    "BURN",
}

# The SPL Token program owns every token account; seeing it as an account's
# owner is what identifies that account as a token account rather than a
# wallet.
_SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_SPL_TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# A user wallet is a System Program account. This is the single check
# that separates a person from protocol plumbing.
_SYSTEM_PROGRAM = "11111111111111111111111111111111"


def on_chain_evidence(address: str) -> dict:
    """LAYER 2 — what the chain actually knows about this address.

    Never raises. A Helius failure is reported as unknown evidence rather
    than as an invalid address: "we could not look" and "there is nothing
    there" are different answers and only one of them is about the address.
    """
    out = {
        "checked": False, "exists": None, "executable": None,
        "owner_program": None, "lamports": None, "signature_count": None,
        "error": None,
    }
    try:
        from lib.helius_client import rpc
        info = (rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}])
                or {}).get("value")
        out["checked"] = True
        if info is None:
            # A valid address that has never been funded has no account.
            # That is ordinary on Solana, not a malformed input.
            out["exists"] = False
            return out
        out["exists"] = True
        out["executable"] = bool(info.get("executable"))
        out["owner_program"] = info.get("owner")
        out["lamports"] = info.get("lamports")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        logger.debug(f"[WalletClassify] account lookup failed {address[:8]}…: {e}")
        return out

    try:
        from lib.helius_client import rpc
        sigs = rpc("getSignaturesForAddress", [address, {"limit": 25}]) or []
        out["signature_count"] = len(sigs)
        out["activity_checked"] = True
    except Exception as e:
        # signature_count stays None, and downstream MUST read that as
        # UNKNOWN rather than as zero or as permission to proceed. The
        # account lookup succeeding does not make the wallet measurable.
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["activity_checked"] = False
    return out


# How long a Helius identity label stays trustworthy. An exchange does not
# stop being an exchange, so this is long by design — the cost being
# avoided is asking whether the same address is Binance on every pass.
IDENTITY_TTL_DAYS = 30

# Helius identity type -> this module's entity taxonomy. Mapped rather than
# adopted verbatim so one vocabulary governs eligibility.
_IDENTITY_TO_ENTITY = {
    "exchange": "CEX", "cex": "CEX",
    "program": "PROGRAM", "protocol": "PROGRAM",
    "pool": "LIQUIDITY_POOL", "amm": "LIQUIDITY_POOL",
    "bridge": "BRIDGE",
    "treasury": "TREASURY",
    "market_maker": "MARKET_MAKER", "mm": "MARKET_MAKER",
    "custody": "CUSTODY", "custodian": "CUSTODY",
    "validator": "PROGRAM",
    "burn": "BURN",
}


def identity_is_fresh(checked_at: str | None, *, ttl_days: int = IDENTITY_TTL_DAYS) -> bool:
    if not checked_at:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t).days
        return age < ttl_days
    except (TypeError, ValueError):
        return False


def entity_from_identity(identity: dict | None) -> str | None:
    """Helius identity -> an entity type this module gates on, or None.

    None means "no label was available", which is the COMMON case and is
    NOT evidence of a human trader. Structural classification continues
    afterwards; this only ever adds knowledge, never concludes innocence.
    """
    if not identity:
        return None
    t = str(identity.get("type") or "").strip().lower()
    if not t or t == "unknown":
        return None
    return _IDENTITY_TO_ENTITY.get(t)


def resolve_identities(session, addresses: list[str], *,
                       ttl_days: int = IDENTITY_TTL_DAYS,
                       max_lookups: int = 100) -> dict:
    """Batch-resolve identity for addresses whose cache is cold or stale.

    `batch_identity` existed in the client and had NO callers — the entity
    layer was relying on a ten-entry hardcoded infrastructure map plus
    on-chain account shape, and paying nothing for Helius's own labels
    because nothing asked for them.

    Cached to the registry, because the wasteful pattern is not the lookup;
    it is the same lookup repeated every pass forever.
    """
    from app.database import WalletRegistry, now_iso

    rows = {w.address: w for w in session.query(WalletRegistry)
            .filter(WalletRegistry.address.in_(list(dict.fromkeys(addresses)))).all()}
    stale = [a for a, w in rows.items()
             if not identity_is_fresh(w.identity_checked_at, ttl_days=ttl_days)]
    stats = {"requested": len(addresses), "cached": len(rows) - len(stale),
             "looked_up": 0, "labelled": 0, "unlabelled": 0, "errors": 0}
    if not stale:
        return stats

    stale = stale[:max_lookups]
    try:
        from lib.helius_client import batch_identity
        found = batch_identity(stale)
    except Exception as e:
        # A failed lookup leaves every cached label INTACT. Identity we
        # already knew is not invalidated by a network problem.
        logger.debug(f"[WalletClassify] batch identity failed: {e}")
        stats["errors"] = len(stale)
        return stats

    stats["looked_up"] = len(stale)
    for addr in stale:
        w = rows.get(addr)
        if w is None:
            continue
        ident = found.get(addr) or {}
        entity = entity_from_identity(ident)
        w.identity_checked_at = now_iso()
        w.identity_source = "helius"
        if entity is None:
            # Explicitly recorded so the negative result is cached too —
            # otherwise every unlabelled address is re-queried forever,
            # and unlabelled is the common case.
            w.identity_type = None
            w.identity_name = None
            stats["unlabelled"] += 1
            continue
        w.identity_type = entity
        w.identity_name = ident.get("name")
        w.identity_category = ident.get("category") or ident.get("type")
        tags = ident.get("tags")
        w.identity_tags = ",".join(tags) if isinstance(tags, list) else (tags or None)
        stats["labelled"] += 1
        # A labelled infrastructure entity is excluded from trader scoring
        # immediately — that is the whole point of asking.
        if entity in NON_TRADER_ENTITIES:
            w.entity_type = entity
            w.entity_name = w.identity_name
            if w.status not in ("EXCLUDED_ENTITY", "ARCHIVED"):
                w.status = "EXCLUDED_ENTITY"
    return stats


def resolve_token_account_owner(address: str) -> str | None:
    """A token account is not a trader; its OWNER might be.

    Autonomous discovery reaches token accounts constantly —
    `getTokenLargestAccounts` returns them, not wallets — so without this
    the registry fills with accounts that can never have a trading history
    of their own.
    """
    try:
        from lib.helius_client import rpc
        info = (rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}])
                or {}).get("value")
        if not info:
            return None
        if info.get("owner") not in (_SPL_TOKEN_PROGRAM, _SPL_TOKEN_2022):
            return None
        return (info.get("data", {}).get("parsed", {})
                    .get("info", {}).get("owner"))
    except Exception as e:
        logger.debug(f"[WalletClassify] owner resolve failed {address[:8]}…: {e}")
        return None


def classify(address: str, evidence: dict | None = None) -> dict:
    """LAYER 3 — what this address IS.

    Order matters. The known-infrastructure list is consulted first because
    it is certain; on-chain shape is inferred after, and only then does
    anything default to a trader candidate.
    """
    from lib.wallet_registry import KNOWN_INFRASTRUCTURE, is_valid_address

    result = {"address": address, "entity_type": "UNKNOWN",
              "entity_name": None, "is_trader": None, "is_protocol": False,
              "off_curve": None, "owner_wallet": None, "reason": ""}

    if not is_valid_address(address):
        result.update(entity_type="INVALID", is_trader=False,
                      reason="failed structural validation")
        return result

    known = KNOWN_INFRASTRUCTURE.get(address)
    if known:
        kind, name = known
        result.update(entity_type={"exchange": "CEX", "program": "PROGRAM",
                                   "pool": "LIQUIDITY_POOL",
                                   "burn": "BURN"}.get(kind, "PROGRAM"),
                      entity_name=name, is_trader=False, is_protocol=True,
                      reason=f"known {kind}: {name}")
        return result

    ev = evidence if evidence is not None else on_chain_evidence(address)

    if ev.get("error") and not ev.get("checked"):
        result.update(reason=f"could not classify — chain lookup failed: "
                             f"{ev['error']}")
        return result

    if ev.get("exists") is False:
        # Valid, unfunded. Not a trader today; also not malformed, and the
        # distinction is the entire point of this module.
        result.update(entity_type="UNKNOWN", is_trader=False,
                      reason="valid address with no account on chain — "
                             "never funded, or funded and closed")
        return result

    if ev.get("executable"):
        result.update(entity_type="PROGRAM", is_trader=False,
                      is_protocol=True, off_curve=True,
                      reason="executable account — a deployed program")
        return result

    owner_program = ev.get("owner_program")
    if owner_program in (_SPL_TOKEN_PROGRAM, _SPL_TOKEN_2022):
        owner = resolve_token_account_owner(address)
        result.update(entity_type="TOKEN_ACCOUNT", is_trader=False,
                      is_protocol=True, owner_wallet=owner,
                      reason="owned by the SPL Token program — a token "
                             "account. Its OWNER is the trading candidate.")
        return result

    # A user wallet is owned by the SYSTEM program. Anything owned by some
    # OTHER program is that program's account — a pool vault, a bonding
    # curve, an escrow — and cannot have a trading history of its own.
    #
    # This check was missing and the first live discovery pass proved why:
    # among five sampled "trader candidates" were an account owned by
    # Pump.fun's AMM and another owned by Meteora DLMM. Both are liquidity
    # vaults. They are not executable and not SPL token accounts, so every
    # earlier test passed them, and they would have been scored as traders
    # with enormous volume and constant activity — ranking protocol
    # plumbing above every human on the chain.
    if owner_program and owner_program != _SYSTEM_PROGRAM:
        result.update(entity_type="PDA", is_trader=False, is_protocol=True,
                      off_curve=True,
                      reason=f"owned by program {owner_program[:12]}… — a "
                             f"program-derived account (pool vault, bonding "
                             f"curve or escrow), not a wallet")
        return result

    result.update(entity_type=TRADER_CANDIDATE, is_trader=True,
                  reason="system-owned account with no infrastructure "
                         "marker — eligible for behavioural analysis")
    return result


def trader_eligibility(address: str, evidence: dict | None = None,
                       min_signatures: int = 5) -> dict:
    """LAYER 4 — should this address enter wallet-intelligence scoring?

    Deliberately conservative and deliberately separate from
    classification. An address can be a perfectly valid trader wallet and
    still be ineligible simply because there is not enough history to say
    anything honest about it — which is a statement about our evidence, not
    about the wallet.
    """
    ev = evidence if evidence is not None else on_chain_evidence(address)
    cls = classify(address, ev)

    if cls["entity_type"] == "INVALID":
        return {**cls, "eligible": False,
                "eligibility_reason": "not a structurally valid address"}
    if cls["entity_type"] in NON_TRADER_ENTITIES:
        return {**cls, "eligible": False,
                "eligibility_reason": (
                    f"{cls['entity_type']} is infrastructure — scoring it as "
                    f"a trader would rank a protocol above every human")}
    if ev.get("exists") is False:
        return {**cls, "eligible": False,
                "eligibility_reason": "no account on chain to analyse"}

    # FAIL CLOSED ON UNKNOWN ACTIVITY.
    #
    # `signature_count` is None in two completely different situations: the
    # activity call was never made, or it FAILED. Both used to fall past
    # this check straight into `eligible: True` — so a Helius timeout on
    # getSignaturesForAddress promoted an unmeasured address to "active
    # enough to analyse", and the expensive history work that follows was
    # spent on evidence nobody had.
    #
    # ELIGIBLE / INELIGIBLE / UNKNOWN are three answers. UNKNOWN means
    # retry later; it must never mean yes.
    sigs = ev.get("signature_count")
    if sigs is None:
        return {**cls, "eligible": False, "eligibility": "UNKNOWN",
                "activity_checked": bool(ev.get("checked")),
                "activity_error": ev.get("error"),
                "retry": True,
                "eligibility_reason": (
                    "activity could not be measured this pass"
                    + (f" ({ev['error']})" if ev.get("error") else "")
                    + " — UNKNOWN, not eligible; will retry")}

    if sigs < min_signatures:
        return {**cls, "eligible": False, "eligibility": "INELIGIBLE",
                "activity_checked": True, "activity_count": sigs,
                "retry": False,
                "eligibility_reason": (
                    f"only {sigs} recent signature(s); below the {min_signatures} "
                    f"needed before any performance claim is honest")}

    return {**cls, "eligible": True, "eligibility": "ELIGIBLE",
            "activity_checked": True, "activity_count": sigs,
            "eligibility_reason": "structurally valid, not infrastructure, "
                                  "and active enough to analyse"}
