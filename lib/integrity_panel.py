"""Make the machine tell us when its own invariants are breaking.

Every defect this programme found had the same signature: the system was
CONFIDENT and WRONG, and nothing in the interface distinguished that from
confident and right. A leveraged short marked to market as a long, a
futures P&L missing its multiplier, a liquidity failure booked as a perfect
exit, a wallet promoted on statistics nothing ever wrote — none of them
threw. Several looked like improvements.

The common remedy was an invariant. This panel runs them continuously, so
the next one is visible before it has poisoned a season of training rather
than after.

WHAT BELONGS HERE. Only checks that can FAIL LOUDLY on real rows. A metric
that merely describes the data ("we have 400 signals") is a dashboard; a
check that says "31 of these signals have no readable direction" is an
invariant. The difference is whether a number being wrong makes it turn
red.

EVERY CHECK COUNTS ROWS IT ACTUALLY QUERIED. Where a column does not exist
on this deployment the check reports UNAVAILABLE rather than zero — zero
violations and no ability to look are completely different statements, and
collapsing them is how a panel becomes reassuring instead of useful.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PANEL_VERSION = "integrity_v1"

OK = "OK"
VIOLATION = "VIOLATION"
UNAVAILABLE = "UNAVAILABLE"
ERROR = "ERROR"

# Severity. CRITICAL means training data is being corrupted right now.
CRITICAL = "CRITICAL"
WARNING = "WARNING"
INFO = "INFO"


@dataclass
class Check:
    key: str
    title: str
    status: str = OK
    severity: str = WARNING
    count: int = 0
    scanned: int = 0
    detail: str | None = None
    why_it_matters: str | None = None
    examples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _table_has(model, *cols) -> bool:
    have = {c.name for c in model.__table__.columns}
    return all(c in have for c in cols)


def _check(key, title, severity, why, fn) -> Check:
    """Run one check, and let it report UNAVAILABLE or ERROR honestly."""
    c = Check(key=key, title=title, severity=severity, why_it_matters=why)
    try:
        fn(c)
    except Exception as e:
        c.status = ERROR
        c.detail = f"{type(e).__name__}: {str(e)[:160]}"
        logger.debug(f"[Integrity] {key} failed: {e}")
    return c


def unknown_directions(db) -> Check:
    def run(c):
        from app.database import TradingSignal
        from lib.trade_side import parse_side_strict
        rows = db.query(TradingSignal.id, TradingSignal.asset_symbol,
                        TradingSignal.direction).limit(5000).all()
        c.scanned = len(rows)
        bad = [r for r in rows if parse_side_strict(r[2]) is None]
        c.count = len(bad)
        c.status = VIOLATION if bad else OK
        c.examples = [{"id": r[0], "symbol": r[1], "direction": r[2]}
                      for r in bad[:5]]
        c.detail = (f"{len(bad)} of {len(rows)} signals state no readable side"
                    if bad else f"all {len(rows)} signals state a side")
    return _check(
        "unknown_directions", "Signals with no readable direction", CRITICAL,
        ("An unreadable direction used to default to LONG. A signal that "
         "cannot state a side must never buy anything."), run)


def signals_missing_asset_class(db) -> Check:
    def run(c):
        from app.database import TradingSignal
        rows = db.query(TradingSignal.id, TradingSignal.asset_symbol).filter(
            (TradingSignal.asset_class.is_(None))
            | (TradingSignal.asset_class == "")).limit(2000).all()
        total = db.query(TradingSignal.id).limit(5000).count()
        c.scanned, c.count = total, len(rows)
        c.status = VIOLATION if rows else OK
        c.examples = [{"id": r[0], "symbol": r[1]} for r in rows[:5]]
        c.detail = (f"{len(rows)} signals carry no asset class — the "
                    f"population where every microscopic-stop defect lived"
                    if rows else "every signal is classified")
    return _check(
        "missing_asset_class", "Signals with no asset class", WARNING,
        ("Unclassified signals bypassed the stop-cost floor. 5,054 of 7,106 "
         "cost rejections came from this population."), run)


def unattributed_outcomes(db) -> Check:
    def run(c):
        from sqlalchemy import text
        rows = db.execute(text(
            "SELECT COUNT(*) FROM trade_outcomes WHERE signal_id IS NULL "
            "OR signal_id = ''")).scalar() or 0
        total = db.execute(text(
            "SELECT COUNT(*) FROM trade_outcomes")).scalar() or 0
        c.scanned, c.count = total, rows
        c.status = VIOLATION if rows else OK
        c.detail = (f"{rows} of {total} outcomes cannot be attributed to a "
                    f"signal — LEGACY_UNATTRIBUTED, and they must stay that "
                    f"way rather than be fuzzy-matched"
                    if rows else f"all {total} outcomes carry a signal")
    return _check(
        "unattributed_outcomes", "Outcomes with no originating signal",
        WARNING,
        ("An anonymous win/loss can only teach an aggregate. These may not "
         "be matched to strategies by resemblance."), run)


def futures_without_spec(db) -> Check:
    def run(c):
        from app.database import TradingSignal
        from lib.instruments import resolve
        rows = db.query(TradingSignal.asset_symbol).filter(
            TradingSignal.asset_symbol.like("%=F")).all()
        # DISTINCT SYMBOLS, not rows. Counting rows made one unspecified
        # contract with 140 signals read as "88 futures symbols", which is
        # exactly the confident-and-wrong reporting this panel exists to
        # catch — in the panel itself.
        symbols = {r[0] for r in rows if r[0]}
        c.scanned = len(symbols)
        bad = set()
        for s in symbols:
            try:
                if not resolve(s).executable:
                    bad.add(s)
            except Exception:
                bad.add(s)
        c.count = len(bad)
        c.status = VIOLATION if bad else OK
        affected = sum(1 for r in rows if r[0] in bad)
        c.examples = [{"symbol": s,
                       "signals": sum(1 for r in rows if r[0] == s)}
                      for s in sorted(bad)[:5]]
        c.detail = (
            f"{len(bad)} of {len(symbols)} distinct futures contracts have no "
            f"verified spec, affecting {affected} signals — research may "
            f"continue, execution must refuse until the spec is verified"
            if bad else f"all {len(symbols)} futures contracts are specified")
    return _check(
        "futures_without_spec", "Futures with no contract specification",
        CRITICAL,
        ("A futures position sized on an equity multiplier is wrong by 50x "
         "on ES and 100x on gold, and nothing downstream can tell."), run)


def dex_positions_unpriceable(db) -> Check:
    def run(c):
        from app.database import DexPosition
        if not _table_has(DexPosition, "exit_state"):
            c.status, c.detail = UNAVAILABLE, "exit_state column not present"
            return
        rows = db.query(DexPosition.id, DexPosition.symbol,
                        DexPosition.exit_blocked_reason).filter(
            DexPosition.exit_state == "EXIT_PENDING_NO_LIQUIDITY").all()
        c.scanned = db.query(DexPosition.id).filter(
            DexPosition.status == "Open").count()
        c.count = len(rows)
        c.status = VIOLATION if rows else OK
        c.examples = [{"id": r[0], "symbol": r[1], "reason": r[2]}
                      for r in rows[:5]]
        c.detail = (f"{len(rows)} DEX positions cannot currently be exited — "
                    f"capital that is not recoverable, not a closed trade"
                    if rows else "every open DEX position has a priceable exit")
    return _check(
        "dex_unpriceable_exit", "DEX positions with no priceable exit",
        CRITICAL,
        ("An unpriceable exit used to book at the gross mid with zero costs, "
         "making liquidity failure the best outcome in the simulator."), run)


def wallet_analysis_failures(db) -> Check:
    def run(c):
        from app.database import WalletRegistry
        if not _table_has(WalletRegistry, "analysis_status"):
            c.status, c.detail = UNAVAILABLE, "analysis_status not present"
            return
        rows = db.query(WalletRegistry.address,
                        WalletRegistry.analysis_error).filter(
            WalletRegistry.analysis_status == "FAILED").limit(500).all()
        c.scanned = db.query(WalletRegistry.address).count()
        c.count = len(rows)
        c.status = VIOLATION if rows else OK
        c.examples = [{"address": r[0][:12], "error": (r[1] or "")[:60]}
                      for r in rows[:5]]
        c.detail = (f"{len(rows)} wallets could not be measured on their last "
                    f"pass — their previous counts are preserved and stale"
                    if rows else "no wallet analysis failures outstanding")
    return _check(
        "wallet_analysis_failed", "Wallets whose last analysis failed",
        WARNING,
        ("FAILED is not ZERO. A provider failure preserves the last known "
         "counts rather than overwriting them with nothing."), run)


def alpha_observations_ineligible(db) -> Check:
    def run(c):
        from app.database import WalletObservation
        if not _table_has(WalletObservation, "evidence_class"):
            c.status, c.detail = UNAVAILABLE, "evidence_class not present"
            return
        total = db.query(WalletObservation.id).count()
        no_class = db.query(WalletObservation.id).filter(
            WalletObservation.evidence_class.is_(None)).count()
        no_price = db.query(WalletObservation.id).filter(
            WalletObservation.alpha_eligible == 1,
            WalletObservation.entry_price_usd.is_(None)).count()
        c.scanned = total
        c.count = no_price
        c.status = VIOLATION if no_price else OK
        c.detail = (
            f"{no_price} observations claim alpha eligibility with no entry "
            f"price; {no_class} legacy rows carry no evidence class"
            if no_price or no_class else
            f"all {total} observations are correctly classified")
    return _check(
        "alpha_eligibility", "Alpha observations without an entry price",
        CRITICAL,
        ("Only a VERIFIED_BUY_ENTRY may anchor post-entry alpha. A signer is "
         "not a buyer and a holder is not an entry."), run)


def stale_surge_sampler(db) -> Check:
    def run(c):
        from app.scheduler import job_status
        st = job_status.get("token_surge") or {}
        last = st.get("last")
        c.scanned = 1
        if not last:
            c.status, c.count = VIOLATION, 1
            c.detail = ("the surge sampler has never run — every baseline "
                        "downstream is starving")
            return
        c.status = OK
        c.detail = f"surge sampler last ran {last}"
    return _check(
        "surge_sampler_lag", "Token surge sampler health", WARNING,
        ("It is the ONLY writer of surge snapshots. A stalled sampler "
         "starves every baseline that depends on it."), run)


ALL_CHECKS = (
    unknown_directions, signals_missing_asset_class, unattributed_outcomes,
    futures_without_spec, dex_positions_unpriceable, wallet_analysis_failures,
    alpha_observations_ineligible, stale_surge_sampler,
)


def run_all(db=None) -> dict:
    """Every invariant, with a verdict that can actually be red."""
    from app.database import get_db

    def _run(session):
        checks = [fn(session) for fn in ALL_CHECKS]
        violations = [c for c in checks if c.status == VIOLATION]
        critical = [c for c in violations if c.severity == CRITICAL]
        unavailable = [c for c in checks if c.status == UNAVAILABLE]
        errored = [c for c in checks if c.status == ERROR]
        return {
            "version": PANEL_VERSION,
            "checks": [c.as_dict() for c in checks],
            "total": len(checks),
            "violations": len(violations),
            "critical": len(critical),
            "unavailable": len(unavailable),
            "errors": len(errored),
            # CLEAN only when everything was actually LOOKED AT. A check
            # that could not run is not a check that passed.
            "healthy": not violations and not errored and not unavailable,
            "verdict": (
                "CRITICAL — training data is being corrupted now" if critical
                else "VIOLATIONS — invariants are breaking" if violations
                else "INCOMPLETE — some checks could not run" if (unavailable or errored)
                else "CLEAN"),
        }

    if db is not None:
        return _run(db)
    with get_db() as session:
        return _run(session)
