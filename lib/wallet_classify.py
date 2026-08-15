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
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out


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

    sigs = ev.get("signature_count")
    if sigs is not None and sigs < min_signatures:
        return {**cls, "eligible": False,
                "eligibility_reason": (
                    f"only {sigs} recent signature(s); below the {min_signatures} "
                    f"needed before any performance claim is honest")}

    return {**cls, "eligible": True,
            "eligibility_reason": "structurally valid, not infrastructure, "
                                  "and active enough to analyse"}
