"""Native SOL staking — the one capital layer that parses without an IDL.

Solana's RPC parses stake accounts itself, so this adapter reads structured
fields rather than guessing byte offsets into a borsh blob. That is why
staking is built first: Kamino's obligation layout resisted inspection (the
owner is not in the first 96 bytes and nothing in that range resolves to a
real account), and guessing there would invent lending positions that do
not exist.

Field offsets used for memcmp were VERIFIED against live accounts, not
assumed from a layout doc:

    offset 12 -> authorized.staker       (2 accounts for the test key)
    offset 44 -> authorized.withdrawer   (19 accounts for the same key)

Different counts for the same key is the check that matters — a wrong
guess landing on the same field twice would have returned identical
numbers and looked correct.

THE SIGNAL THIS PRODUCES, and the mistake it avoids:

A wallet deactivating a large stake is freeing capital. That is
CAPITAL_LIQUIDITY_INCREASING — not "about to sell". Deactivated SOL gets
redeployed, restaked, lent or spent, and calling it bearish because the
stake shrank is the same class of error as reading SOL -> JitoSOL as a
sale. The direction is a separate question with its own evidence.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
STAKE_ACCOUNT_SIZE = 200
OFFSET_STAKER = 12
OFFSET_WITHDRAWER = 44

LAMPORTS_PER_SOL = 1_000_000_000
# u64::MAX in deactivationEpoch means "not deactivating".
NOT_DEACTIVATING = 18_446_744_073_709_551_615

# Below this a stake change is not a capital-flow event worth a signal.
MIN_MATERIAL_SOL = 100.0


def _epoch(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def stake_accounts_for(wallet: str, current_epoch: int | None = None) -> dict:
    """Every stake account this wallet can WITHDRAW from.

    Keyed on the withdrawer authority rather than the staker: the staker
    can be delegated to a third party (a staking service), while the
    withdrawer is the one who actually controls the capital. Using the
    staker would attribute a custodian's stake to the wrong wallet.
    """
    from lib.helius_client import rpc

    out = {"wallet": wallet, "accounts": [], "error": None,
           "total_sol": 0.0, "active_sol": 0.0, "deactivating_sol": 0.0,
           "inactive_sol": 0.0}
    try:
        rows = rpc("getProgramAccounts", [STAKE_PROGRAM, {
            "encoding": "jsonParsed",
            "filters": [{"dataSize": STAKE_ACCOUNT_SIZE},
                        {"memcmp": {"offset": OFFSET_WITHDRAWER, "bytes": wallet}}],
        }]) or []
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        logger.debug(f"[CapitalStake] {wallet[:8]}…: {e}")
        return out

    if current_epoch is None:
        try:
            current_epoch = int((rpc("getEpochInfo", []) or {}).get("epoch") or 0)
        except Exception:
            current_epoch = 0

    for row in rows:
        try:
            parsed = row["account"]["data"]["parsed"]
            info = parsed.get("info") or {}
            meta = info.get("meta") or {}
            deleg = ((info.get("stake") or {}).get("delegation") or {})
        except (KeyError, TypeError):
            continue

        lamports = float(row["account"].get("lamports") or 0)
        sol = lamports / LAMPORTS_PER_SOL
        deact = _epoch(deleg.get("deactivationEpoch"))
        act = _epoch(deleg.get("activationEpoch"))

        if not deleg:
            state = "undelegated"
        elif deact is not None and deact != NOT_DEACTIVATING:
            state = "deactivating" if deact >= (current_epoch or 0) else "inactive"
        elif act is not None and act > (current_epoch or 0):
            state = "activating"
        else:
            state = "active"

        acct = {
            "stake_account": row.get("pubkey"),
            "sol": round(sol, 6),
            "state": state,
            "validator": deleg.get("voter"),
            "activation_epoch": act,
            "deactivation_epoch": None if deact == NOT_DEACTIVATING else deact,
            "staker": (meta.get("authorized") or {}).get("staker"),
            "withdrawer": (meta.get("authorized") or {}).get("withdrawer"),
            "locked_until": (meta.get("lockup") or {}).get("unixTimestamp") or 0,
        }
        out["accounts"].append(acct)
        out["total_sol"] += sol
        if state == "active":
            out["active_sol"] += sol
        elif state == "deactivating":
            out["deactivating_sol"] += sol
        elif state == "inactive":
            out["inactive_sol"] += sol

    for k in ("total_sol", "active_sol", "deactivating_sol", "inactive_sol"):
        out[k] = round(out[k], 6)
    out["account_count"] = len(out["accounts"])
    out["validators"] = sorted({a["validator"] for a in out["accounts"]
                                if a.get("validator")})
    return out


def capital_liquidity_signal(stake: dict, sol_price_usd: float = 0.0) -> dict:
    """What the stake picture says about capital becoming available.

    Deliberately NOT a directional call. Freed stake is capital looking for
    a destination; whether that destination is bullish is decided by what
    the wallet does next, and asserting it here would turn an unstake into
    a sell signal on no evidence.
    """
    freed = float(stake.get("deactivating_sol") or 0) + float(stake.get("inactive_sol") or 0)
    total = float(stake.get("total_sol") or 0)
    usd = freed * float(sol_price_usd or 0)

    if stake.get("error"):
        return {"state": "unknown", "reason": f"stake lookup failed: {stake['error']}",
                "freed_sol": 0.0, "freed_usd": 0.0}
    if total <= 0:
        return {"state": "none", "reason": "no stake accounts",
                "freed_sol": 0.0, "freed_usd": 0.0}
    if freed < MIN_MATERIAL_SOL:
        return {"state": "stable",
                "reason": f"{freed:,.1f} SOL unlocking — below the "
                          f"{MIN_MATERIAL_SOL:,.0f} SOL materiality floor",
                "freed_sol": round(freed, 4), "freed_usd": round(usd, 2)}

    share = freed / total
    return {
        "state": "CAPITAL_LIQUIDITY_INCREASING",
        "freed_sol": round(freed, 4),
        "freed_usd": round(usd, 2),
        "share_of_stake": round(share, 4),
        "still_staked_sol": round(total - freed, 4),
        "reason": (f"{freed:,.1f} SOL ({share*100:.0f}% of this wallet's stake) "
                   f"is deactivating or already inactive"),
        # The standing caveat, carried with the signal so nothing downstream
        # has to remember it.
        "note": ("Capital becoming available is NOT a sale and NOT bearish. "
                 "Deactivated SOL is commonly restaked, lent, posted as "
                 "collateral or deployed. Direction is decided by what the "
                 "wallet does next."),
    }
