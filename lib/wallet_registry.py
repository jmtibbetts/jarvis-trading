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


def b58encode(raw: bytes) -> str:
    """Canonical base58. Needed to round-trip an address and reject a
    non-canonical encoding of the same bytes."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    # Leading zero BYTES map to leading '1' characters, and when the value
    # is entirely zero those '1's ARE the whole encoding. An `or "1"`
    # fallback here made the all-zero System Program encode to 33
    # characters and get rejected as malformed.
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def is_valid_address(addr: str) -> bool:
    """LAYER 1 — STRUCTURAL validation only.

    Valid base58, decodes to exactly 32 bytes, and round-trips canonically.
    The round-trip rejects a non-canonical encoding of the same bytes (a
    stray leading '1', say) which the length check alone lets through.

    What this deliberately does NOT do is check the key lies on the ed25519
    curve. Program Derived Addresses are off-curve BY DESIGN and are
    completely valid Solana addresses; rejecting them would throw away
    every PDA, program-owned account and protocol address. `off_curve` is
    an entity-classification signal, never a validity verdict.

    Known and documented limit: a truncated or altered address often still
    decodes to 32 bytes and round-trips cleanly, because dropping a base58
    character just divides the value by ~58. No structural test can infer
    "this was probably meant to be a different address". That question is
    answered by LAYER 2 — on-chain evidence — not by the encoding.
    """
    if not addr or not (32 <= len(addr) <= 44):
        return False
    try:
        raw = b58decode(addr)
    except ValueError:
        return False
    return len(raw) == 32 and b58encode(raw) == addr


def structural_check(addr: str) -> dict:
    """`is_valid_address` with its reasoning exposed, for the UI.

    The UI must never say "INVALID ADDRESS" for an address that is merely
    off-curve or merely inactive, so it needs to know WHICH test failed.
    """
    if not addr:
        return {"valid": False, "reason": "empty"}
    if not (32 <= len(addr) <= 44):
        return {"valid": False,
                "reason": f"length {len(addr)} outside the 32–44 range a "
                          f"32-byte base58 value can occupy"}
    try:
        raw = b58decode(addr)
    except ValueError as e:
        return {"valid": False, "reason": f"not valid base58: {e}"}
    if len(raw) != 32:
        return {"valid": False,
                "reason": f"decodes to {len(raw)} bytes, not 32"}
    if b58encode(raw) != addr:
        return {"valid": False,
                "reason": "non-canonical base58 encoding of these bytes"}
    return {
        "valid": True,
        "reason": "structurally valid 32-byte base58 address",
        # Informational ONLY. Off-curve is not invalid — see above.
        "note": ("Structural validity cannot prove this is the address that "
                 "was intended, nor that it is a trader. On-chain evidence "
                 "and entity classification decide that."),
    }


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
        # FLUSH, so the row is visible to the next query in this same
        # session. Without it a second upsert of the same address inside one
        # transaction sees nothing and inserts a duplicate, tripping the
        # UNIQUE constraint. That is not hypothetical: bootstrap seeds
        # infrastructure and then operator seeds, discovery upserts the same
        # wallet as both a token holder and a pool trader in a single pass,
        # and `load_seed_wallets` de-duplicates only within its own list.
        db.flush()
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


# ── Lifecycle ────────────────────────────────────────────────────────────
# The states were already in use as bare string literals across four
# modules. Naming them here is what lets `get_monitorable_wallets` and the
# discovery cap talk about the SAME sets instead of each re-deciding.
DISCOVERED = "DISCOVERED"
CANDIDATE = "CANDIDATE"
ANALYZING = "ANALYZING"
WATCH = "WATCH"
SMART_MONEY = "SMART_MONEY"
HIGH_CONVICTION = "HIGH_CONVICTION"
DEGRADED = "DEGRADED"
ARCHIVED = "ARCHIVED"
EXCLUDED_ENTITY = "EXCLUDED_ENTITY"

# ── MONITORING PURPOSE. Orthogonal to STATUS, and deliberately so. ──────
#
# STATUS answers "how proven is this wallet?" (CANDIDATE -> WATCH ->
# SMART_MONEY). PURPOSE answers "what are we watching it FOR?". They are
# different questions and collapsing them produced a real contradiction: a
# wallet distributing tokens across 415 counterparties in under two hours
# sat at WATCH with a smart-money score of 80, because the score measured
# activity quality and nothing asked whether the activity was copyable.
ALPHA = "ALPHA"                          # directional, possibly copyable
FLOW_CONTEXT = "FLOW_CONTEXT"            # real activity, not copyable alpha
EVIDENCE_COLLECTION = "EVIDENCE_COLLECTION"   # not yet characterised

MONITORING_PURPOSES = frozenset({ALPHA, FLOW_CONTEXT, EVIDENCE_COLLECTION})

#: Only ALPHA wallets may source a wallet-alpha shadow pick. FLOW_CONTEXT
#: wallets are still observed — their flow informs market context — and
#: EVIDENCE_COLLECTION wallets are still observed because that is the only
#: way they ever stop being unknown. NOTHING here is an identity claim.
ALPHA_PURPOSES = frozenset({ALPHA})

#: A wallet removed from the alpha population must not become unobservable,
#: or it can never gather the evidence that would let it return. Every
#: purpose keeps a bounded observation budget; they differ in PRIORITY, not
#: in whether they are looked at.
OBSERVABLE_PURPOSES = MONITORING_PURPOSES


# Wallets whose activity is worth spending API budget on every pass. These
# are the PROVEN ones plus anything the operator pinned by hand.
MONITORABLE_STATUSES = frozenset({WATCH, SMART_MONEY, HIGH_CONVICTION})

# States that consume expensive candidate-analysis capacity, and therefore
# the only ones the discovery cap may count. A registry row for an archived
# wallet or a known exchange costs nothing per pass; counting it toward the
# cap is what let registry growth silently switch discovery off.
ACTIVE_ANALYSIS_STATUSES = frozenset({DISCOVERED, CANDIDATE, ANALYZING})

# Never monitored and never analysed, whatever else is true of the row —
# including `pinned`, so a mistakenly pinned exchange cannot buy its way
# back into the scoring pipeline.
NEVER_MONITOR_STATUSES = frozenset({EXCLUDED_ENTITY, ARCHIVED})


def get_monitorable_wallets(db=None, *, limit: int | None = None) -> list[str]:
    """THE wallet population, for every runtime consumer without exception.

    This function exists because there were two universes. `wallet_registry`
    was documented as the source of truth while `wallet_activity` and
    `/wallet/intel` both read `HELIUS_WATCH_WALLETS` directly and skipped
    when it was blank — which it is on this deployment. Autonomous discovery
    was therefore filling a table that no runtime analytics loop read, and
    the collector reported "skipped" every fifteen minutes while doing it.

    The population is:
      - any wallet in a PROVEN state (WATCH / SMART_MONEY / HIGH_CONVICTION)
      - plus anything `pinned`, which is an explicit operator instruction
        and is how imported seeds keep being watched — a seed lands as a
        pinned CANDIDATE, so the addresses that used to be polled straight
        out of the env var still are, through the registry
      - minus anything EXCLUDED_ENTITY or ARCHIVED, which no flag overrides

    Ordered so the most-proven wallets survive truncation: a caller bounded
    at 12 addresses should spend them on HIGH_CONVICTION before CANDIDATE.
    """
    from sqlalchemy import or_

    from app.database import WalletRegistry, get_db

    def _run(session):
        rows = (session.query(WalletRegistry)
                .filter(~WalletRegistry.status.in_(tuple(NEVER_MONITOR_STATUSES)))
                .filter(or_(WalletRegistry.status.in_(tuple(MONITORABLE_STATUSES)),
                            WalletRegistry.pinned.is_(True),
                            # FLOW_CONTEXT IS A PURPOSE, NOT AN EXILE. A
                            # wallet demoted out of the alpha population for
                            # its behaviour must keep receiving the cheap
                            # transfer observations that would let it be
                            # re-evaluated — otherwise the demotion is
                            # permanent by construction and the wallet can
                            # never recover even if its behaviour changes.
                            # It is excluded from the expensive deep-history
                            # queue, not from being looked at.
                            WalletRegistry.monitoring_purpose == FLOW_CONTEXT))
                .all())
        rank = {HIGH_CONVICTION: 0, SMART_MONEY: 1, WATCH: 2}
        rows.sort(key=lambda r: (rank.get(r.status, 3),
                                 not bool(r.pinned),
                                 r.first_discovered_at or ""))
        out = [r.address for r in rows]
        return out[:limit] if limit else out

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def monitorable_breakdown(db=None) -> dict:
    """Why the monitored population is the size it is.

    An empty list is a legitimate state and must never look like a failure,
    so the caller needs to be able to say WHICH emptiness this is: nothing
    discovered yet, nothing promoted yet, or everything excluded.
    """
    from sqlalchemy import func, or_

    from app.database import WalletRegistry, get_db

    def _run(session):
        rows = (session.query(WalletRegistry.status,
                              func.count(WalletRegistry.address))
                .filter(~WalletRegistry.status.in_(tuple(NEVER_MONITOR_STATUSES)))
                .filter(or_(WalletRegistry.status.in_(tuple(MONITORABLE_STATUSES)),
                            WalletRegistry.pinned.is_(True)))
                .group_by(WalletRegistry.status).all())
        by = {s: n for s, n in rows}
        total = sum(by.values())
        return {
            "monitored": total,
            "by_status": by,
            "reason": (
                "registry is empty — no seeds imported and discovery has found nothing"
                if total == 0 and _registry_total(session) == 0 else
                "no wallet has been promoted or pinned yet; discovery is still building evidence"
                if total == 0 else
                f"{total} wallet(s) proven or pinned"),
        }

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def _registry_total(session) -> int:
    from app.database import WalletRegistry
    return session.query(WalletRegistry).count()


def active_analysis_count(db=None) -> int:
    """How many wallets are actually consuming candidate-analysis capacity.

    `HELIUS_DISCOVERY_MAX_CANDIDATES` was compared against an unfiltered
    `count()` of the whole registry, so every archived wallet, every known
    exchange and every promoted SMART_MONEY row counted toward a cap meant
    to bound EXPENSIVE work. The registry is designed to accumulate learned
    identities permanently; under that comparison, doing its job eventually
    disabled discovery.
    """
    from app.database import WalletRegistry, get_db

    def _run(session):
        return (session.query(WalletRegistry)
                .filter(WalletRegistry.status.in_(tuple(ACTIVE_ANALYSIS_STATUSES)))
                .count())

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def bootstrap(db=None) -> dict:
    """The one startup call: exclusions first, then operator seeds.

    Order matters. `seed_known_infrastructure` must run BEFORE `import_seeds`
    so that an address appearing in both lands as EXCLUDED_ENTITY rather than
    as a pinned candidate — `upsert_wallet` will re-exclude it either way,
    but only this order avoids a window where it is monitorable.

    Idempotent by construction: `upsert_wallet` never downgrades a promoted
    wallet and never overwrites an earned score, so running this on every
    boot is free.
    """
    infra = seed_known_infrastructure(db)
    seeds = import_seeds(db)
    out = {**infra, **seeds}
    logger.info(f"[WalletRegistry] bootstrap: {out}")
    return out


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
