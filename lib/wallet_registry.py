"""The wallet universe — persistent, self-expanding, and honest about what
it does not yet know.

Replaces `HELIUS_WATCH_WALLETS` as the source of truth. That env var made
an environment variable the database: blank meant the entire subsystem
reported `configured: false`, so a fully working Helius connection and a
24KB scoring engine sat idle behind an empty string.

The env var survives as SEEDS. A seed is a starting point for
investigation, never a verdict — imported as CANDIDATE with every score
null, because "the operator thinks this wallet is interesting" and "this
wallet has demonstrated alpha" are different claims and only the second
one is earned.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Infrastructure that must never be scored as a trader (§13).
#
# This is a correctness requirement, not a nicety: the largest holder of
# the first token queried on live data was a Binance hot wallet. Without
# this list the system's opening discovery is an exchange moving customer
# funds, promoted to HIGH_CONVICTION for having enormous "positions" and
# a perfect "win rate" — every number technically computed, every
# conclusion nonsense.
#
# Seeded with the well-known ones; `entity_type` is also inferred at
# discovery time from Helius identity data, so this list is a floor rather
# than the whole defence.
KNOWN_INFRASTRUCTURE: dict[str, tuple[str, str]] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": ("exchange", "Binance 2"),
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": ("exchange", "Binance hot wallet"),
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": ("program", "Jupiter Aggregator v6"),
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": ("program", "Jupiter Aggregator v4"),
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("pool", "Raydium AMM v4"),
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": ("pool", "Orca Whirlpools"),
    "11111111111111111111111111111111": ("program", "System Program"),
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": ("program", "SPL Token Program"),
    "ComputeBudget111111111111111111111111111111": ("program", "Compute Budget Program"),
    "1nc1nerator11111111111111111111111111111111": ("burn", "Incinerator (burn address)"),
}


def b58decode(s: str) -> bytes:
    """Minimal base58 decode. Inline rather than a dependency — this is the
    only thing in the codebase that needs it, and validating an address is
    not worth a new package in the lockfile."""
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError(f"illegal base58 character {ch!r}")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def is_valid_address(addr: str) -> bool:
    """A Solana public key is 32 bytes, base58 encoded. Rejecting malformed
    input here keeps junk out of the registry, where it would otherwise
    burn Helius calls returning 404 forever."""
    if not addr or not (32 <= len(addr) <= 44):
        return False
    try:
        return len(b58decode(addr)) == 32
    except ValueError:
        return False


def load_seed_wallets() -> list[str]:
    """Parse HELIUS_WATCH_WALLETS into validated, de-duplicated addresses.

    Tolerant of newlines, spaces and trailing commas — an operator pasting
    a list should not have to think about the separator. Invalid entries
    are dropped with a warning rather than raising: one typo must not stop
    the other four wallets from loading.
    """
    raw = os.getenv("HELIUS_WATCH_WALLETS", "") or ""
    parts = [w.strip() for w in raw.replace("\n", ",").replace(" ", ",").split(",")]
    seeds, bad = [], []
    for w in parts:
        if not w:
            continue
        (seeds if is_valid_address(w) else bad).append(w)
    if bad:
        logger.warning(f"[WalletRegistry] ignoring {len(bad)} malformed "
                       f"seed address(es): {', '.join(b[:12] + '…' for b in bad)}")
    return list(dict.fromkeys(seeds))


def intelligence_enabled() -> bool:
    """Wallet intelligence is configured when Helius works and the feature
    is on — NOT when someone has hand-typed a wallet list. That conflation
    is the whole reason the subsystem reported itself disabled."""
    from lib.helius_client import configured
    flag = os.getenv("HELIUS_WALLET_INTELLIGENCE_ENABLED", "true").strip().lower()
    return configured() and flag not in ("0", "false", "no", "off")


def discovery_enabled() -> bool:
    flag = os.getenv("HELIUS_WALLET_DISCOVERY_ENABLED", "true").strip().lower()
    return intelligence_enabled() and flag not in ("0", "false", "no", "off")


# Names the operator supplied alongside the seed addresses. A label says
# who a wallet is, never how good it is — these arrive as CANDIDATE with
# every score null, exactly like an anonymous discovery would.
SEED_LABELS: dict[str, str] = {
    "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN": "West",
    "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o": "Cented",
    "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt": "theo",
    "5T229oePmJGE5Cefys8jE9Jq8C7qfGNNWy3RVA7SmwEP": "Tuults",
    "EqiFgyNw6kgrmYstWyrP8VjKhka7XEmKTZzHSmwpr1Zb": "ban",
}


def upsert_wallet(db, address: str, *, source: str = "manual_seed",
                  source_wallet: str | None = None,
                  discovery_reason: str | None = None,
                  status: str = "DISCOVERED",
                  label: str | None = None,
                  pinned: bool = False):
    """Insert a wallet, or update the parts of an existing row that are
    still unknown.

    Never downgrades. Re-running seed import must not knock a wallet that
    has since been promoted to SMART_MONEY back to CANDIDATE, and must not
    wipe scores that were expensively earned. The only fields a repeat
    sighting may set are ones that are still empty — plus `pinned`, which
    is an explicit operator instruction and always wins.
    """
    from app.database import WalletRegistry, now_iso

    if not is_valid_address(address):
        raise ValueError(f"not a valid Solana address: {address!r}")

    row = db.query(WalletRegistry).filter(
        WalletRegistry.address == address).first()

    infra = KNOWN_INFRASTRUCTURE.get(address)
    if row is None:
        row = WalletRegistry(address=address, source=source,
                             source_wallet=source_wallet,
                             discovery_reason=discovery_reason,
                             first_discovered_at=now_iso())
        # Infrastructure is classified on the way IN. Letting an exchange
        # into the scoring pipeline and catching it later means it has
        # already contaminated whatever ran in between.
        if infra:
            row.status = "EXCLUDED_ENTITY"
            row.entity_type, row.entity_name = infra
            row.is_protocol, row.is_trader = True, False
            row.discovery_reason = (discovery_reason or "") + \
                f" | excluded on sight: known {infra[0]} ({infra[1]})"
        else:
            row.status = status
        row.pinned = pinned
        row.label = label or SEED_LABELS.get(address)
        db.add(row)
        return row

    if pinned:
        row.pinned = True
    if not row.label:
        row.label = label or SEED_LABELS.get(address)
    if not row.source_wallet and source_wallet:
        row.source_wallet = source_wallet
    if not row.discovery_reason and discovery_reason:
        row.discovery_reason = discovery_reason
    if infra and row.status != "EXCLUDED_ENTITY":
        row.status = "EXCLUDED_ENTITY"
        row.entity_type, row.entity_name = infra
        row.is_protocol, row.is_trader = True, False
    row.last_seen_at = now_iso()
    return row


def import_seeds(db=None) -> dict:
    """Load the configured seeds into the registry as unproven candidates.

    Deliberately CANDIDATE and not WATCH: these came from a human's
    interest, which is a reason to investigate and not evidence of skill.
    Every score stays null until the wallet earns one. Pinned, so the
    demotion engine cannot quietly archive an address the operator chose.
    """
    from app.database import get_db

    def _run(session):
        seeds = load_seed_wallets()
        made, seen = 0, 0
        for addr in seeds:
            from app.database import WalletRegistry
            existed = session.query(WalletRegistry).filter(
                WalletRegistry.address == addr).first() is not None
            upsert_wallet(session, addr, source="manual_seed",
                          discovery_reason="operator-supplied seed; unproven",
                          status="CANDIDATE", label=SEED_LABELS.get(addr),
                          pinned=True)
            seen += 1
            made += 0 if existed else 1
        return {"seeds_configured": len(seeds), "imported": made,
                "already_present": seen - made}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def seed_known_infrastructure(db=None) -> dict:
    """Pre-load the exclusion list so discovery never has to learn it the
    expensive way — by promoting an exchange."""
    from app.database import get_db

    def _run(session):
        n = 0
        for addr in KNOWN_INFRASTRUCTURE:
            upsert_wallet(session, addr, source="known_infrastructure",
                          discovery_reason="seeded exclusion list")
            n += 1
        return {"infrastructure_excluded": n}

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def counts(db=None) -> dict:
    """Population by lifecycle state, for /api/wallet/intel."""
    from sqlalchemy import func

    from app.database import WalletRegistry, get_db

    def _run(session):
        rows = session.query(WalletRegistry.status,
                             func.count(WalletRegistry.address)) \
                      .group_by(WalletRegistry.status).all()
        by = {s: n for s, n in rows}
        return {
            "seed": session.query(WalletRegistry).filter(
                WalletRegistry.source == "manual_seed").count(),
            "discovered": by.get("DISCOVERED", 0),
            "candidates": by.get("CANDIDATE", 0),
            "analyzing": by.get("ANALYZING", 0),
            "watching": by.get("WATCH", 0),
            "smart_money": by.get("SMART_MONEY", 0),
            "high_conviction": by.get("HIGH_CONVICTION", 0),
            "degraded": by.get("DEGRADED", 0),
            "archived": by.get("ARCHIVED", 0),
            "excluded_entities": by.get("EXCLUDED_ENTITY", 0),
            "total": sum(by.values()),
        }

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
