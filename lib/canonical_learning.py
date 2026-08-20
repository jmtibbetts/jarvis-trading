"""B2C — the idempotent canonical learning projection.

THE GOLDEN RULE, LEARNING EDITION: the bot must never learn a result
different from the one it actually settled. PaperRealizedOutcome is
financial truth; this module PROJECTS that truth into the learning tables —
it recomputes nothing, resolves nothing, prices nothing, asks no LLM, and
reads no current market. Every number in the learning row is a persisted
number copied under validation:

    entry/exit prices     the settled fills, not marks
    qty + unit basis      2 CONTRACTS at 0.01 stays 2 CONTRACTS at 0.01 —
                          never an unlabelled "2"
    pnl_pct + basis       net_return_pct WITH return_pct_basis (MARGIN);
                          never net_r * 100, never an inferred denominator
    engine_epoch          the epoch that PRODUCED the economics, not the
                          one running at projection time
    entered/exited/hold   the persisted trade timestamps; projection time
                          is `projected_at`, a different fact

ENTRY-TIME METADATA FIELD MAP (audited, B2C §18):

    timeframe        DecisionObservation.timeframe (decision-frozen), else
                     trading_signals.timeframe
    confidence       trading_signals.confidence          (written at entry)
    score            trading_signals.composite_score     (written at entry)
    reasoning        trading_signals.reasoning           (written at entry)
    ta_profile       NOT PERSISTED anywhere for canonical entries today —
                     pattern memory is therefore SKIPPED, never recomputed
                     from exit-time TA
    ta_summary       NOT PERSISTED — NULL
    market_regime    NOT PERSISTED — regime performance SKIPPED, never
                     asked of today's regime engine

Missing entry evidence stays missing. The learner may skip a derived
update; it may not backfill entry context with future information.

IDEMPOTENCY. One PaperRealizedOutcome maps to at most one trade_outcomes
row, enforced by the partial UNIQUE index on canonical_outcome_id — the
database, not a SELECT, is the race backstop. The whole deterministic
projection (learning row, aggregates, APPLIED marker) commits in ONE
transaction, so there is no trade_outcomes row without APPLIED and no
APPLIED without its row. Tier 5 (LLM reasoning audit) is deliberately NOT
part of APPLIED: it is external, non-deterministic work and stays a
separate, future, separately-idempotent projector.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Learning states on PaperRealizedOutcome ──────────────────────────────
PENDING = "PENDING"
APPLIED = "APPLIED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED_PERMANENT = "FAILED_PERMANENT"
# TERMINAL, and deliberately not a failure: this outcome is real financial
# history that the strategy must never be taught from. An administrative
# book reset closed the position; the market did not.
SKIPPED_POLICY = "SKIPPED_POLICY"

# Exit reasons whose outcomes are financial truth but NOT strategy evidence.
NON_LEARNING_EXIT_REASONS = frozenset({"ADMINISTRATIVE_RESET"})

# ── Result vocabulary ────────────────────────────────────────────────────
LEARNING_ALREADY_APPLIED = "LEARNING_ALREADY_APPLIED"
LEARNING_SKIPPED_POLICY = "LEARNING_SKIPPED_POLICY"
LEARNING_OUTCOME_INVALID = "LEARNING_OUTCOME_INVALID"
LEARNING_STATE_CORRUPT = "LEARNING_STATE_CORRUPT"
LEARNING_PROJECTION_FAILED = "LEARNING_PROJECTION_FAILED"
LEARNING_RECOVERY_AMBIGUOUS = "LEARNING_RECOVERY_AMBIGUOUS"
LEARNING_FAILED_PERMANENT = "LEARNING_FAILED_PERMANENT"
LEARNING_OUTCOME_NOT_FOUND = "LEARNING_OUTCOME_NOT_FOUND"

_REP_TOL = 1e-6


class LearningValidationError(ValueError):
    """Canonical truth that is internally corrupt. Do not teach from it."""


def _close_enough(a, b, tol: float = _REP_TOL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)),
                                                 abs(float(b)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CanonicalLearningProjection:
    """One validated learning projection — pure, no DB writes, no market."""
    canonical_outcome_id: str
    position_id: str
    signal_id: str | None

    symbol: str
    asset_class: str | None
    product: str
    instrument_id: str
    direction: str                       # long | short

    quantity: float
    quantity_unit: str
    multiplier: float

    entry_price: float
    exit_price: float

    gross_pnl_usd: float
    net_pnl_usd: float
    explicit_fees_usd: float

    net_return_pct: float | None
    return_pct_basis: str | None

    outcome: str
    exit_reason: str | None

    opened_at: str | None
    closed_at: str
    hold_minutes: float | None

    timeframe: str | None
    confidence: float | None
    score: float | None
    reasoning: str | None
    ta_profile: dict | None
    ta_summary: str | None
    market_regime: str | None

    engine_epoch: str | None
    outcome_version: str
    execution_model: str | None
    cost_model: str | None
    settlement_version: str | None


def _entry_metadata(conn, signal_id: str | None,
                    position_id: str) -> dict:
    """PERSISTED entry-time metadata only — see the field map above.
    Anything not persisted at entry stays None; nothing here consults a
    current TA engine, regime detector, or LLM."""
    from sqlalchemy import text

    meta = {"timeframe": None, "confidence": None, "score": None,
            "reasoning": None, "ta_profile": None, "ta_summary": None,
            "market_regime": None}
    row = conn.execute(text(
        "SELECT timeframe FROM decision_observations "
        "WHERE position_id=:p ORDER BY decision_at LIMIT 1"),
        {"p": position_id}).fetchone()
    if row and row[0]:
        meta["timeframe"] = row[0]
    if signal_id:
        sig = conn.execute(text(
            "SELECT timeframe, confidence, composite_score, reasoning "
            "FROM trading_signals WHERE id=:s"), {"s": signal_id}).fetchone()
        if sig:
            meta["timeframe"] = meta["timeframe"] or sig[0]
            meta["confidence"] = sig[1]
            meta["score"] = sig[2]
            meta["reasoning"] = sig[3]
    return meta


def build_projection(outcome, header, meta: dict) -> CanonicalLearningProjection:
    """Assemble the projection from PERSISTED rows. Copies; never derives."""
    return CanonicalLearningProjection(
        canonical_outcome_id=outcome.id,
        position_id=outcome.position_id,
        signal_id=outcome.signal_id,
        symbol=outcome.symbol,
        asset_class=header.asset_class,     # frozen at entry, never inferred
        product=outcome.product,
        instrument_id=outcome.instrument_id,
        direction=outcome.side,
        quantity=float(outcome.quantity),
        quantity_unit=outcome.quantity_unit,
        multiplier=float(outcome.multiplier),
        entry_price=float(outcome.actual_entry_fill),
        exit_price=float(outcome.actual_exit_fill),
        gross_pnl_usd=float(outcome.gross_pnl_usd),
        net_pnl_usd=float(outcome.net_pnl_usd),
        explicit_fees_usd=(float(outcome.commission_usd or 0.0)
                           + float(outcome.regulatory_fees_usd or 0.0)
                           + float(outcome.funding_usd or 0.0)
                           + float(outcome.borrow_cost_usd or 0.0)),
        net_return_pct=(float(outcome.net_return_pct)
                        if outcome.net_return_pct is not None else None),
        return_pct_basis=outcome.return_pct_basis,
        outcome=outcome.outcome,
        exit_reason=outcome.exit_reason,
        opened_at=outcome.opened_at,
        closed_at=outcome.closed_at,
        hold_minutes=(float(outcome.hold_minutes)
                      if outcome.hold_minutes is not None else None),
        timeframe=meta.get("timeframe"),
        confidence=meta.get("confidence"),
        score=meta.get("score"),
        reasoning=meta.get("reasoning"),
        ta_profile=meta.get("ta_profile"),
        ta_summary=meta.get("ta_summary"),
        market_regime=meta.get("market_regime"),
        engine_epoch=outcome.engine_epoch,
        outcome_version=outcome.outcome_version,
        execution_model=outcome.execution_model,
        cost_model=outcome.cost_model_version,
        settlement_version=outcome.settlement_version,
    )


def validate_projection(p: CanonicalLearningProjection, outcome) -> None:
    """PURE. Refuses corrupt canonical truth; never repairs it."""
    for name in ("canonical_outcome_id", "position_id", "symbol", "product",
                 "instrument_id", "quantity_unit", "closed_at",
                 "outcome_version"):
        if not str(getattr(p, name) or "").strip():
            raise LearningValidationError(f"{name} is empty")
    if p.direction not in ("long", "short"):
        raise LearningValidationError(
            f"direction {p.direction!r} is not canonical — the side is "
            f"already known, and nothing here parses legacy vocabularies")
    if not (math.isfinite(p.quantity) and p.quantity > 0):
        raise LearningValidationError(f"quantity {p.quantity!r} invalid")
    if not (math.isfinite(p.multiplier) and p.multiplier > 0):
        raise LearningValidationError(f"multiplier {p.multiplier!r} invalid")
    for name in ("entry_price", "exit_price"):
        v = getattr(p, name)
        if not (math.isfinite(v) and v > 0):
            raise LearningValidationError(f"{name} {v!r} invalid")
    for name in ("gross_pnl_usd", "net_pnl_usd", "explicit_fees_usd"):
        if not math.isfinite(getattr(p, name)):
            raise LearningValidationError(f"{name} is not finite")
    if p.outcome not in ("WIN", "LOSS", "BREAKEVEN"):
        raise LearningValidationError(f"outcome {p.outcome!r} invalid")
    # The return basis is load-bearing: a percentage without its
    # denominator is not a percentage, and the canonical book states MARGIN.
    if p.return_pct_basis != "MARGIN":
        raise LearningValidationError(
            f"return_pct_basis {p.return_pct_basis!r} is not the canonical "
            f"MARGIN — refusing to guess a denominator")
    if p.net_return_pct is None or not math.isfinite(p.net_return_pct):
        raise LearningValidationError(
            f"net_return_pct {p.net_return_pct!r} is not a finite stated "
            f"percentage")
    # Identity, not recomputation: the projection must carry the persisted
    # values verbatim.
    if not _close_enough(p.net_pnl_usd, float(outcome.net_pnl_usd), 1e-12):
        raise LearningValidationError("net_pnl_usd drifted during projection")
    if not _close_enough(p.net_return_pct, float(outcome.net_return_pct),
                         1e-12):
        raise LearningValidationError("net_return_pct drifted during "
                                      "projection")
    if p.opened_at and p.closed_at:
        try:
            t0 = datetime.fromisoformat(str(p.opened_at))
            t1 = datetime.fromisoformat(str(p.closed_at))
            if (t1 - t0).total_seconds() < 0:
                raise LearningValidationError(
                    "closed_at precedes opened_at")
        except ValueError as e:
            raise LearningValidationError(f"unparseable trade timestamps: "
                                          f"{e}")


def _eligibility_error(outcome) -> str | None:
    """Canonical eligibility (§4) — versions and models, before anything."""
    from lib.paper_settlement import (COST_MODEL_CANONICAL,
                                      EXECUTION_MODEL_CANONICAL,
                                      SETTLEMENT_VERSION)
    from lib.realized_outcome import SETTLEMENT_OUTCOME_VERSION

    if outcome.outcome_version != SETTLEMENT_OUTCOME_VERSION:
        return (f"outcome_version {outcome.outcome_version!r} is not "
                f"{SETTLEMENT_OUTCOME_VERSION!r}")
    if outcome.settlement_version != SETTLEMENT_VERSION:
        return (f"settlement_version {outcome.settlement_version!r} is not "
                f"{SETTLEMENT_VERSION!r}")
    if outcome.execution_model != EXECUTION_MODEL_CANONICAL:
        return f"execution_model {outcome.execution_model!r} not canonical"
    if outcome.cost_model_version != COST_MODEL_CANONICAL:
        return f"cost_model {outcome.cost_model_version!r} not canonical"
    return None


def _header_disagreement(outcome, header) -> str | None:
    """§5 — the outcome and its settlement header must describe one trade."""
    if header is None:
        return "no settlement header exists for this outcome's position"
    if header.status != "CLOSED":
        return f"header status {header.status!r} is not CLOSED"
    if header.realized_outcome_id != outcome.id:
        return (f"header names outcome {header.realized_outcome_id!r}, "
                f"not {outcome.id!r}")
    for name in ("product", "venue", "instrument_id", "quantity_unit",
                 "engine_epoch", "execution_model"):
        if getattr(header, name) != getattr(outcome, name, None) and \
                name != "execution_model":
            return (f"{name}: header {getattr(header, name)!r} vs outcome "
                    f"{getattr(outcome, name, None)!r}")
    if header.cost_model != outcome.cost_model_version:
        return (f"cost model: header {header.cost_model!r} vs outcome "
                f"{outcome.cost_model_version!r}")
    if not _close_enough(header.multiplier, outcome.multiplier, 1e-12):
        return "multiplier disagrees between header and outcome"
    if not _close_enough(header.original_quantity, outcome.quantity):
        return "quantity disagrees between header and outcome"
    if not _close_enough(header.actual_entry_fill, outcome.actual_entry_fill,
                         1e-9):
        return "entry fill disagrees between header and outcome"
    return None


def _paper_trade_disagreement(conn, outcome) -> str | None:
    """§6 — the compatibility projection must not have diverged."""
    from sqlalchemy import text
    if not outcome.paper_trade_id:
        return None
    row = conn.execute(text(
        "SELECT position_id, realized_pnl, gross_pnl, qty, entry_price, "
        "exit_price FROM paper_trades WHERE id=:i"),
        {"i": outcome.paper_trade_id}).fetchone()
    if row is None:
        return f"paper_trade {outcome.paper_trade_id!r} does not exist"
    checks = (("position_id", row[0], outcome.position_id, False),
              ("realized_pnl", row[1], outcome.net_pnl_usd, True),
              ("gross_pnl", row[2], outcome.gross_pnl_usd, True),
              ("qty", row[3], outcome.quantity, True),
              ("entry_price", row[4], outcome.actual_entry_fill, True),
              ("exit_price", row[5], outcome.actual_exit_fill, True))
    for name, a, b, numeric in checks:
        same = _close_enough(a, b) if numeric else a == b
        if not same:
            return (f"PaperTrade.{name} {a!r} disagrees with the canonical "
                    f"outcome's {b!r} — two persisted projections diverged")
    return None


def _learning_row_disagreement(conn, outcome) -> str | None:
    """Verify an existing canonical trade_outcomes row against the outcome."""
    from sqlalchemy import text
    row = conn.execute(text(
        "SELECT id, canonical_outcome_id, position_id, pnl_usd, pnl_pct, "
        "qty, entry_price, exit_price, outcome, return_pct_basis "
        "FROM trade_outcomes WHERE canonical_outcome_id=:c"),
        {"c": outcome.id}).fetchone()
    if row is None:
        return "no canonical learning row exists"
    if row[1] != outcome.id:
        return "canonical_outcome_id mismatch"
    if row[2] != outcome.position_id:
        return "position mismatch on the learning row"
    for name, got, want in (("pnl_usd", row[3], outcome.net_pnl_usd),
                            ("pnl_pct", row[4], outcome.net_return_pct),
                            ("qty", row[5], outcome.quantity),
                            ("entry_price", row[6],
                             outcome.actual_entry_fill),
                            ("exit_price", row[7],
                             outcome.actual_exit_fill)):
        if not _close_enough(got, want, 1e-4):
            return (f"learning row {name} {got!r} disagrees with canonical "
                    f"{want!r}")
    if row[8] != outcome.outcome:
        return "outcome label disagrees on the learning row"
    if row[9] != outcome.return_pct_basis:
        return "return basis disagrees on the learning row"
    return None


def insert_learning_row(conn, p: "CanonicalLearningProjection", *,
                        outcome_source: str, projected_at: str) -> None:
    """THE one place a canonical learning row is written. Shared, on purpose.

    Two projectors reach this: the virtual settlement ledger (B2C) and the
    manual operator desk. They differ in what they are ALLOWED to project
    and in which aggregates may follow — they must not differ in the SHAPE
    of the row, or the two populations stop being comparable, which is the
    entire reason for keeping both.

    `outcome_source` is a REQUIRED KEYWORD with no default. It used to be
    the literal `'live'` inside the SQL, which is precisely how a second
    caller ends up silently claiming to be the first.
    `lib.learning_population` owns that vocabulary and decides which
    consumer may read which value.

    Writes the ROW and nothing else — no aggregates, no state markers. The
    caller owns the transaction and decides what else belongs inside it.
    """
    from sqlalchemy import text

    from lib import learning_population as LP

    if outcome_source not in LP.POPULATIONS:
        raise LearningValidationError(
            f"outcome_source {outcome_source!r} is not a learning "
            f"population ({', '.join(LP.POPULATIONS)}); an unlabelled row "
            f"would be pooled by whichever consumer failed to exclude it")

    conn.execute(text("""
        INSERT INTO trade_outcomes
        (id, canonical_outcome_id, position_id, signal_id, symbol,
         asset_class, direction, timeframe, entry_price, exit_price,
         qty, quantity_unit, multiplier, product, instrument_id,
         pnl_usd, pnl_pct, return_pct_basis, gross_pnl_usd,
         explicit_fees_usd, outcome, exit_reason, hold_duration_m,
         signal_confidence, signal_score, signal_reasoning,
         ta_summary, market_regime, paper_mode, entered_at,
         exited_at, projected_at, engine_epoch, outcome_version,
         execution_model, cost_model_version, settlement_version,
         outcome_source)
        VALUES
        (:id, :cid, :pos, :sig, :sym, :ac, :dir, :tf, :ep, :xp,
         :qty, :qu, :mult, :prod, :inst, :pnl, :pct, :basis,
         :gross, :fees, :outcome, :reason, :hold, :conf, :score,
         :reasoning, :tas, :regime, 1, :opened, :closed,
         :projected, :epoch, :over, :emodel, :cmodel, :sver,
         :src)
    """), {
        # One final canonical truth, one learning truth: same id.
        "id": p.canonical_outcome_id,
        "cid": p.canonical_outcome_id,
        "pos": p.position_id, "sig": p.signal_id,
        "sym": p.symbol, "ac": p.asset_class,
        "dir": p.direction, "tf": p.timeframe,
        "ep": p.entry_price, "xp": p.exit_price,
        "qty": p.quantity, "qu": p.quantity_unit,
        "mult": p.multiplier, "prod": p.product,
        "inst": p.instrument_id,
        "pnl": p.net_pnl_usd, "pct": p.net_return_pct,
        "basis": p.return_pct_basis,
        "gross": p.gross_pnl_usd, "fees": p.explicit_fees_usd,
        "outcome": p.outcome, "reason": p.exit_reason,
        "hold": p.hold_minutes,
        "conf": p.confidence, "score": p.score,
        "reasoning": p.reasoning, "tas": p.ta_summary,
        "regime": p.market_regime,
        "opened": p.opened_at, "closed": p.closed_at,
        "projected": projected_at,
        "epoch": p.engine_epoch,        # PERSISTED, never current
        "over": p.outcome_version,
        "emodel": p.execution_model, "cmodel": p.cost_model,
        "sver": p.settlement_version,
        "src": outcome_source,
    })


def _set_learning_state(outcome_id: str, state: str, error: str | None):
    """The tiny separate transaction AFTER a rollback (§33). Touches only
    the learning metadata — never financial fields."""
    from sqlalchemy import text
    from app.database import engine
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE paper_realized_outcomes SET learning_state=:s, "
            "learning_error=:e WHERE id=:i"),
            {"s": state, "e": (error or None) if error is None
             else error[:2000], "i": outcome_id})


def apply_realized_outcome(outcome_id: str) -> dict:
    """PENDING → APPLIED, exactly once. The only public single-apply API."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.database import (PaperPositionSettlement, PaperRealizedOutcome,
                              engine, get_db)
    from lib import learning_population as LP

    with get_db() as db:
        outcome = db.query(PaperRealizedOutcome).filter(
            PaperRealizedOutcome.id == outcome_id).first()
        header = None
        if outcome is not None:
            header = db.query(PaperPositionSettlement).filter(
                PaperPositionSettlement.position_id == outcome.position_id
            ).first()
        db.expunge_all()
    if outcome is None:
        return {"ok": False, "error": LEARNING_OUTCOME_NOT_FOUND,
                "detail": f"no PaperRealizedOutcome {outcome_id!r}"}

    state = outcome.learning_state

    # ── POLICY SKIP, checked before anything else. An administrative reset
    # is real financial history and NOT strategy evidence: no learning row,
    # no aggregates, no Tier 5, ever. Terminal and idempotent — a later
    # sweep finds it already terminal and moves on.
    if outcome.exit_reason in NON_LEARNING_EXIT_REASONS:
        if state != SKIPPED_POLICY:
            _set_learning_state(outcome_id, SKIPPED_POLICY, None)
        return {"ok": True, "skipped": True,
                "result": LEARNING_SKIPPED_POLICY,
                "detail": (f"{outcome.exit_reason} is an administrative "
                           f"operation, not a decision the strategy made "
                           f"about this trade")}
    if state == SKIPPED_POLICY:
        return {"ok": True, "skipped": True,
                "result": LEARNING_SKIPPED_POLICY,
                "detail": "already terminal by policy"}

    # ── APPLIED: idempotent only when the projection is intact (§27/§28) ─
    if state == APPLIED:
        with engine.connect() as conn:
            if not outcome.trade_outcome_id:
                return {"ok": False, "error": LEARNING_STATE_CORRUPT,
                        "detail": "APPLIED with no trade_outcome_id — an "
                                  "APPLIED marker with no projection is an "
                                  "integrity defect, not idempotency"}
            problem = _learning_row_disagreement(conn, outcome)
            row = conn.execute(text(
                "SELECT id FROM trade_outcomes WHERE canonical_outcome_id=:c"),
                {"c": outcome.id}).fetchone()
            if problem or row is None or row[0] != outcome.trade_outcome_id:
                return {"ok": False, "error": LEARNING_STATE_CORRUPT,
                        "detail": problem or "trade_outcome_id does not "
                                             "match the canonical row"}
        return {"ok": True, "idempotent": True,
                "result": LEARNING_ALREADY_APPLIED,
                "trade_outcome_id": outcome.trade_outcome_id}

    if state == FAILED_PERMANENT:
        return {"ok": False, "error": LEARNING_FAILED_PERMANENT,
                "detail": (f"outcome {outcome_id} is FAILED_PERMANENT "
                           f"({outcome.learning_error!r}); operator repair "
                           f"is required — it is not retried automatically")}

    # ── Eligibility and cross-checks. Corrupt truth is not taught (§4-6) ─
    def _permanent(detail: str) -> dict:
        _set_learning_state(outcome_id, FAILED_PERMANENT, detail)
        return {"ok": False, "error": LEARNING_OUTCOME_INVALID,
                "detail": detail}

    problem = _eligibility_error(outcome)
    if problem:
        return _permanent(problem)
    problem = _header_disagreement(outcome, header)
    if problem:
        return _permanent(problem)

    # ── PENDING with an existing canonical row (§29): recovery. In the
    # one-transaction design a committed row implies its aggregates and
    # APPLIED marker committed with it, so PENDING beside a row means the
    # state was tampered with or the row was inserted by hand. Heal ONLY a
    # projection that agrees in full; anything else is ambiguity, refused.
    with engine.connect() as conn:
        existing = conn.execute(text(
            "SELECT id FROM trade_outcomes WHERE canonical_outcome_id=:c"),
            {"c": outcome.id}).fetchone()
        if existing is not None:
            problem = _learning_row_disagreement(conn, outcome)
            if problem:
                return {"ok": False, "error": LEARNING_RECOVERY_AMBIGUOUS,
                        "detail": (f"a canonical learning row exists while "
                                   f"the outcome says PENDING, and it "
                                   f"disagrees: {problem} — refusing to "
                                   f"risk a double aggregate update")}
            with engine.begin() as heal:
                heal.execute(text(
                    "UPDATE paper_realized_outcomes SET "
                    "trade_outcome_id=:t, learning_state=:s, "
                    "learning_applied_at=:a, learning_error=NULL "
                    "WHERE id=:i"),
                    {"t": existing[0], "s": APPLIED, "a": _now_iso(),
                     "i": outcome_id})
            return {"ok": True, "idempotent": True, "healed": True,
                    "result": LEARNING_ALREADY_APPLIED,
                    "trade_outcome_id": existing[0]}

    # ── Build and validate the projection (pure) ─────────────────────────
    try:
        with engine.connect() as conn:
            meta = _entry_metadata(conn, outcome.signal_id,
                                   outcome.position_id)
        projection = build_projection(outcome, header, meta)
        validate_projection(projection, outcome)
    except LearningValidationError as e:
        return _permanent(str(e))

    # ── ONE deterministic learning transaction (§22) ─────────────────────
    applied_at = _now_iso()
    try:
        with engine.begin() as conn:
            p = projection
            # THE SHARED WRITER. `live` is stated here rather than baked
            # into the SQL, so the manual projector cannot inherit this
            # population by reusing the same statement.
            insert_learning_row(conn, p, outcome_source=LP.LIVE,
                                projected_at=applied_at)

            # Tier 3/4 — ONLY from persisted entry evidence (§25/§26). No
            # persisted entry TA profile or regime exists for canonical
            # entries today, so these legitimately skip. If entry evidence
            # is ever persisted, these run in THIS transaction, exactly
            # once by construction.
            if p.ta_profile and p.net_return_pct is not None:
                from lib.learning_engine import (_fingerprint_ta,
                                                 _update_pattern_memory)
                fp, desc = _fingerprint_ta(p.ta_profile, p.direction)
                if fp:
                    _update_pattern_memory(fp, desc, p.asset_class,
                                           p.timeframe, p.outcome,
                                           p.net_return_pct, conn)
            if p.market_regime:
                from lib.learning_engine import _update_regime_performance
                _update_regime_performance(p.market_regime, p.outcome,
                                           p.net_return_pct,
                                           p.confidence or 0, conn)

            # Tier 2 — derived from persisted rows, same transaction (§24).
            from lib.learning_engine import _refresh_signal_accuracy_conn
            _refresh_signal_accuracy_conn(p.symbol, p.asset_class,
                                          p.timeframe, conn)

            # The APPLIED marker commits WITH the projection or not at all.
            conn.execute(text(
                "UPDATE paper_realized_outcomes SET trade_outcome_id=:t, "
                "learning_state=:s, learning_applied_at=:a, "
                "learning_error=NULL WHERE id=:i"),
                {"t": p.canonical_outcome_id, "s": APPLIED,
                 "a": applied_at, "i": outcome_id})
    except IntegrityError as e:
        # The unique-canonical race: another worker committed first. Verify
        # rather than assume — arbitrary integrity errors are not
        # idempotency.
        if "canonical" in str(e).lower() or "trade_outcomes" in str(e):
            with engine.connect() as conn:
                problem = _learning_row_disagreement(conn, outcome)
            if problem is None:
                return {"ok": True, "idempotent": True,
                        "result": LEARNING_ALREADY_APPLIED,
                        "raced": True,
                        "trade_outcome_id": outcome.id}
        _set_learning_state(outcome_id, FAILED_RETRYABLE, str(e))
        return {"ok": False, "error": LEARNING_PROJECTION_FAILED,
                "detail": str(e)}
    except Exception as e:
        # Transient failure: everything above rolled back; record the
        # failure state in its own tiny transaction (§33).
        logger.error("[CanonicalLearning] projection failed for %s: %s",
                     outcome_id, e, exc_info=True)
        _set_learning_state(outcome_id, FAILED_RETRYABLE, str(e))
        return {"ok": False, "error": LEARNING_PROJECTION_FAILED,
                "detail": str(e)}

    logger.info("[CanonicalLearning] applied %s: %s %s %+0.2f%% (%s)",
                outcome_id, projection.symbol, projection.outcome,
                projection.net_return_pct, projection.return_pct_basis)
    return {"ok": True, "result": "LEARNING_APPLIED",
            "trade_outcome_id": projection.canonical_outcome_id,
            "outcome": projection.outcome}


def apply_pending_realized_outcomes(limit: int = 50,
                                    include_retryable: bool = False) -> dict:
    """Bounded catch-up over PENDING (optionally FAILED_RETRYABLE) final
    outcomes, oldest first. Each apply is independently transactional and
    idempotent — there is no durable claim, so a dead process leaves rows
    exactly PENDING or exactly APPLIED, never zombie-claimed. NOT scheduled;
    a callable recovery tool."""
    from sqlalchemy import bindparam, text
    from app.database import engine

    states = [PENDING] + ([FAILED_RETRYABLE] if include_retryable else [])
    stmt = text(
        "SELECT id FROM paper_realized_outcomes "
        "WHERE learning_state IN :states "
        "ORDER BY closed_at ASC LIMIT :lim"
    ).bindparams(bindparam("states", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(stmt, {"states": states,
                                   "lim": int(limit)}).fetchall()

    summary = {"scanned": len(rows), "applied": 0, "already_applied": 0,
               "retryable_failed": 0, "permanent_failed": 0,
               "skipped_policy": 0, "other": 0}
    for (oid,) in rows:
        res = apply_realized_outcome(oid)
        if res.get("ok") and res.get("skipped"):
            summary["skipped_policy"] += 1
        elif res.get("ok") and res.get("idempotent"):
            summary["already_applied"] += 1
        elif res.get("ok"):
            summary["applied"] += 1
        elif res.get("error") == LEARNING_PROJECTION_FAILED:
            summary["retryable_failed"] += 1
        elif res.get("error") in (LEARNING_OUTCOME_INVALID,
                                  LEARNING_FAILED_PERMANENT):
            summary["permanent_failed"] += 1
        else:
            summary["other"] += 1
    return summary
