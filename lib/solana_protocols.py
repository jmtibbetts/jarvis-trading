"""Solana protocol registry — every entry verified against the chain.

Each program ID here was checked with getAccountInfo before it was written
down: programs must come back `executable: true`, mints must be owned by
the SPL Token program. Two entries were wrong on the first pass and the
check is the only reason that is known:

  Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb is NOT a program. It exists,
  it is not executable, and it is owned by the SPL Stake Pool program — it
  is Jito's stake POOL ACCOUNT. Jito is therefore parsed through the
  generic SPL Stake Pool program, not through an ID of its own.

  pAMMBay6oceH9fJKBRHGwLPKnCACo9AJ7YXFPz3mQzY does not exist. It was
  reconstructed from a 20-character truncated log line with the remaining
  23 characters invented, and it had already reached a test. The real
  Pump.fun AMM is pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA, read back
  off accounts discovery had actually found.

Both share a prefix with the wrong value, which is exactly how a plausible
fabricated address survives review. Nothing goes in this file unverified.

WHY THIS EXISTS AT ALL: wallet_classify treats protocol interaction as a
reason to EXCLUDE an address — right for "do not rank Jupiter as the best
trader on Solana", and backwards for capital-flow intelligence, which
needs those same interactions parsed. This registry is the other half:
which protocol, what category, and therefore what kind of capital event a
transaction touching it might be.
"""
from __future__ import annotations

# Protocol categories drive which adapter interprets a transaction.
NATIVE_STAKE = "native_stake"
LIQUID_STAKING = "liquid_staking"
LENDING = "lending"
AMM = "amm"
AGGREGATOR = "aggregator"
BRIDGE = "bridge"
INFRASTRUCTURE = "infrastructure"

# address -> (category, name, verified_on_chain)
PROGRAMS: dict[str, tuple[str, str, bool]] = {
    "Stake11111111111111111111111111111111111111":
        (NATIVE_STAKE, "Solana Stake Program", True),
    # Jito, Marinade's stake-pool path, and most LSTs route through this.
    "SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy":
        (LIQUID_STAKING, "SPL Stake Pool", True),
    "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD":
        (LIQUID_STAKING, "Marinade Finance", True),
    "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD":
        (LENDING, "Kamino Lend", True),
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8":
        (AMM, "Raydium AMM v4", True),
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":
        (AMM, "Orca Whirlpool", True),
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo":
        (AMM, "Meteora DLMM", True),
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA":
        (AMM, "Pump.fun AMM", True),
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4":
        (AGGREGATOR, "Jupiter Aggregator v6", True),
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth":
        (BRIDGE, "Wormhole Core", True),
    "11111111111111111111111111111111":
        (INFRASTRUCTURE, "System Program", True),
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
        (INFRASTRUCTURE, "SPL Token Program", True),
    "ComputeBudget111111111111111111111111111111":
        (INFRASTRUCTURE, "Compute Budget Program", True),
}

# Specific stake-pool STATE accounts, which identify the LST behind a
# generic SPL Stake Pool instruction.
STAKE_POOLS: dict[str, str] = {
    "Jito4APyf642JPZPx3hGc6WWJ8zPKtRbRs4P815Awbb": "Jito",
}

# Liquid staking tokens. The reason this matters is a live misread:
# a wallet's SOL balance falling because it converted SOL into JitoSOL is
# NOT a sale, and without this mapping it is indistinguishable from one.
LST_MINTS: dict[str, tuple[str, str]] = {
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": ("JitoSOL", "Jito"),
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": ("mSOL", "Marinade"),
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": ("bSOL", "BlazeStake"),
}

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

STABLES = {USDC_MINT, USDT_MINT}
# Everything that still represents SOL exposure after a transformation.
SOL_EQUIVALENTS = {WSOL_MINT} | set(LST_MINTS)


def protocol_of(program_id: str | None) -> dict | None:
    """Which protocol a program belongs to, or None if unknown.

    None means UNKNOWN, never "not a protocol". An unrecognised program is
    a gap in this registry, and the adapters must treat it as unparseable
    rather than assuming it was a plain transfer.
    """
    if not program_id:
        return None
    hit = PROGRAMS.get(program_id)
    if not hit:
        return None
    category, name, verified = hit
    return {"program_id": program_id, "category": category, "name": name,
            "verified_on_chain": verified}


def is_lst(mint: str | None) -> bool:
    return bool(mint) and mint in LST_MINTS


def lst_info(mint: str | None) -> dict | None:
    if not mint or mint not in LST_MINTS:
        return None
    symbol, provider = LST_MINTS[mint]
    return {"mint": mint, "symbol": symbol, "provider": provider,
            "underlying": "SOL"}


def retains_sol_exposure(mint: str | None) -> bool:
    """True when holding this mint still means being long SOL.

    The distinction the whole LST section exists for: SOL -> JitoSOL lowers
    the raw SOL balance and changes nothing about the wallet's exposure.
    Reading that as a sell inverts the signal.
    """
    return bool(mint) and mint in SOL_EQUIVALENTS


def is_stable(mint: str | None) -> bool:
    return bool(mint) and mint in STABLES


def categorize_programs(program_ids) -> dict:
    """Summarise the programs a transaction touched.

    `unknown_programs` is returned rather than swallowed: a transaction
    whose programs are unrecognised must be classifiable as UNPARSEABLE,
    not silently reduced to whatever the known subset suggests.
    """
    known, unknown, cats = [], [], set()
    for pid in program_ids or []:
        p = protocol_of(pid)
        if p:
            known.append(p)
            cats.add(p["category"])
        else:
            unknown.append(pid)
    return {"protocols": known, "unknown_programs": unknown,
            "categories": sorted(cats),
            "fully_understood": not unknown}
