"""Candidate persistence and counterfactual resolution.

The learning loop's blind spot: filters were judged only by the trades they
let through. Rejected setups vanished, so "is the filter discarding
winners?" had no data — and the 2026-08-13 decomposition showed the
composite score is inverted, which makes it likely the answer is yes.

record_candidate() writes every considered setup, accepted or not, with the
judgment as it stood. resolve_pending() later walks each candidate forward
through cached bars — the same replay machinery that labels real outcomes —
and fills in what would have happened. selection_bias_summary() then
answers the forbidden question directly: rejected vs accepted, measured.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

# Generators re-emit the same setup every cycle while it stays valid; the
# replay effort already found 12,845 of 39,235 signals were duplicate
# regenerations. One candidate row per distinct setup is the record of a
# decision; fifty copies of it are noise that would also multiply into the
# resolution job.
def dedup_hash(symbol, timeframe, direction, entry, stop, target) -> str:
    key = f"{symbol}|{timeframe}|{direction}|{entry}|{stop}|{target}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def _is_stale_link(db, signal_id: str) -> bool:
    """True when the signal a candidate points at is no longer live.

    Only a stale link is re-pointed. If the linked signal is still Active or
    PendingApproval, two live signals share one setup and moving the verdict
    would leave the other reading UNMEASURED — trading one orphan for
    another. Missing signals count as stale: nothing is worse off.
    """
    try:
        from sqlalchemy import func

        from app.database import TradingSignal
        row = db.query(TradingSignal).filter(TradingSignal.id == signal_id).first()
        if row is None:
            return True
        return str(row.status or "").lower() not in ("active", "pendingapproval")
    except Exception as e:
        logger.debug(f"[Candidates] stale-link check failed (non-fatal): {e}")
        # Fail toward NOT moving an existing link — a wrong re-point would
        # rewrite history, a missed one only leaves the badge as it was.
        return False


def relink_orphan_signals(db=None, limit: int = 2000) -> dict:
    """Attach live signals to the candidate row that already judged them.

    The re-pointing fix above stops NEW orphans; it cannot heal the ones
    already on the board, because a setup only re-enters `record_candidate`
    if the scanner sees those exact levels again and prices move.

    Matching is by EXACT dedup hash — symbol, timeframe, direction, entry,
    stop and target all identical. Nothing looser: a verdict computed for a
    different entry is a measurement of a different trade, and attaching it
    to make a badge go away would be inventing evidence. A signal whose
    numbers match no candidate stays UNMEASURED, which is then true.
    """
    from app.database import CandidateSignal, TradingSignal, get_db

    def _run(session):
        out = {"scanned": 0, "relinked": 0, "no_candidate": 0, "held": 0}
        live = session.query(TradingSignal).filter(
            TradingSignal.status.in_(["Active", "PendingApproval"])
        ).limit(limit).all()
        linked_ids = {
            c.signal_id for c in session.query(CandidateSignal).filter(
                CandidateSignal.signal_id.isnot(None)).all()
        }
        for sig in live:
            if sig.id in linked_ids:
                continue
            out["scanned"] += 1
            h = dedup_hash(sig.asset_symbol, sig.timeframe, sig.direction,
                           sig.entry_price, sig.stop_loss, sig.target_price)
            cand = session.query(CandidateSignal).filter(
                CandidateSignal.dedup_hash == h).first()
            if cand is None:
                out["no_candidate"] += 1
                continue
            if cand.signal_id and not _is_stale_link(session, cand.signal_id):
                # Its verdict belongs to another LIVE signal; moving it would
                # just relocate the orphan.
                out["held"] += 1
                continue
            cand.signal_id = sig.id
            out["relinked"] += 1
        return out

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)


def record_candidate(db, scored: dict, verdict: str,
                     rejection_reason: str | None = None,
                     signal_id: str | None = None,
                     is_paper: bool = False,
                     source: str = "generator"):
    """Persist one considered setup. Never raises — candidate bookkeeping
    must not be able to break signal generation.

    A repeat sighting of the same setup does NOT create a second row, but
    it does complete the first one: if the original row was written before
    a signal existed (the usual case — a setup is judged, then persists a
    cycle later) the signal_id is attached now. Returning early instead,
    as this did until 2026-08-16, orphaned the link and left the signal
    card reading UNMEASURED forever.

    What is never rewritten: the judgment itself. Score, breakdown,
    verdict, rejection reason and BOTH gate verdicts stay exactly as first
    recorded — hindsight editing its own paper trail is how a learning
    system lies to itself, and the gate experiment depends on those five
    fields being immutable.
    """
    try:
        from app.database import CandidateSignal
        from lib.calibration import CURRENT_EPOCH
        from lib.score_variants import compute_variants

        # Coerced HERE, not trusted to the ORM: a junk value raises at
        # flush time, which is outside this try/except's reach in the
        # caller's session and would take the whole signal batch with it.
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        entry = _num(scored.get("entry_price"))
        stop = _num(scored.get("stop_loss"))
        target = _num(scored.get("target_price"))
        score = _num(scored.get("composite_score"))

        h = dedup_hash(scored.get("asset_symbol"), scored.get("timeframe"),
                       scored.get("direction"), entry, stop, target)
        existing = db.query(CandidateSignal).filter(
            CandidateSignal.dedup_hash == h).first()
        if existing is not None:
            # Complete the row rather than dropping the news. Only fields
            # that are still EMPTY get filled; nothing already judged is
            # touched.
            #
            # The link also RE-POINTS when the signal it holds is no longer
            # live. The scanner supersedes an unchanged setup's old signal
            # and writes a new row with a new id every cycle; this branch
            # used to attach only when `signal_id` was empty, so the
            # candidate stayed bolted to the superseded signal and the new
            # ACTIVE one joined to nothing — which is what put 43 of 138
            # live signals on UNMEASURED while 85 unlinked candidate rows
            # sat there for the same symbols. The dedup hash is
            # symbol+timeframe+direction+entry+stop+target, so the setup
            # being re-pointed to is the same setup the gate already judged;
            # the verdict travels with it legitimately.
            #
            # The judgment itself still never moves: score, breakdown,
            # verdict, rejection_reason and both gate fields are untouched
            # here, exactly as the docstring promises.
            if signal_id and signal_id != existing.signal_id:
                if not existing.signal_id or _is_stale_link(db, existing.signal_id):
                    existing.signal_id = signal_id
                    if existing.verdict != "persisted":
                        # It reached the book after all. The original
                        # rejection_reason stays on the row as the record of
                        # what the first look concluded.
                        existing.verdict = "persisted"
            if not existing.source:
                existing.source = source
            return existing

        breakdown = scored.get("score_breakdown") or {}

        # The gate experiment: BOTH arms' verdicts at birth, immutable.
        # Never raises past this point — a gate error records as UNKNOWN
        # rather than costing the candidate row.
        try:
            from lib.gate import record_both
            from lib.trading_preferences import get_user_preference
            _thresh = float(get_user_preference().get("live_min_score", 55.0))
            gates = record_both(scored, _thresh)
        except Exception as ge:
            logger.debug(f"[Candidates] gate verdicts failed (non-fatal): {ge}")
            gates = {"gate_legacy_take": None, "gate_v8_decision": "UNKNOWN",
                     "gate_v8_take": None, "gate_v8_reason": f"gate error: {ge}",
                     "gate_v8_net_r": None}

        # Macro context at the moment of judgment (4C shadow features):
        # stored at birth like the variants, so the eventual ablation
        # joins on what the desk actually knew, never on hindsight.
        try:
            from lib.candidate_context import context_for_candidate
            ctx = context_for_candidate(scored.get("asset_symbol"))
            market_context = json.dumps(ctx, sort_keys=True) if ctx else None
        except Exception as ce:
            logger.debug(f"[Candidates] context failed (non-fatal): {ce}")
            market_context = None

        row = CandidateSignal(
            market_context=market_context,
            gate_legacy_take=gates["gate_legacy_take"],
            gate_v8_decision=gates["gate_v8_decision"],
            gate_v8_take=gates["gate_v8_take"],
            gate_v8_reason=gates["gate_v8_reason"],
            gate_v8_net_r=gates["gate_v8_net_r"],
            engine_epoch=CURRENT_EPOCH,
            dedup_hash=h,
            symbol=scored.get("asset_symbol"),
            asset_class=scored.get("asset_class"),
            timeframe=scored.get("timeframe"),
            direction=scored.get("direction"),
            strategy=scored.get("strategy"),
            entry_price=entry,
            stop_loss=stop,
            target_price=target,
            composite_score=score,
            score_breakdown=json.dumps(breakdown, sort_keys=True),
            shadow_variants=json.dumps(compute_variants(score, breakdown)),
            verdict=verdict,
            rejection_reason=rejection_reason,
            signal_id=signal_id,
            source=source,
            paper_mode=bool(is_paper),
        )
        db.add(row)
        return row
    except Exception as e:
        logger.debug(f"[Candidates] record failed (non-fatal): {e}")
        return None


def resolve_pending(limit: int = 500) -> dict:
    """Counterfactually resolve unresolved candidates old enough to judge.

    Reuses replay_signal: same walk, same stop-first-within-a-bar
    conservatism, same AMBIGUOUS rule, same fee model. A candidate resolved
    under different assumptions than real outcomes could not be compared
    with them, which is the entire point of resolving it.
    """
    from datetime import datetime, timedelta, timezone

    from app.database import CandidateSignal, get_db
    from lib.signal_replay import MIN_BARS_TO_RESOLVE, load_cached_bars, replay_signal

    checked = resolved = no_bars = too_young = expired = 0
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=30)).isoformat()
    # A candidate with no cached bars after this long will never get them
    # (unlisted crypto the bar pipeline doesn't fetch). It leaves the
    # queue as resolved-with-NULL-outcome so it can't poison stats, and —
    # the bug this exists to prevent — can't sit at the head of the
    # oldest-first queue eating the limit forever. 440 of them starved
    # every resolvable candidate behind them for a day (measured
    # 2026-08-14: a manual wide pass resolved 128 the scheduled job's
    # capped passes never reached).
    expire_before = (now - timedelta(days=14)).isoformat()

    with get_db() as db:
        # The limit budgets REPLAY ATTEMPTS, not rows scanned: barless
        # rows are one dict lookup each and must not count against it.
        # The scan window is floored so a barless flood larger than a
        # small limit's multiple can't reintroduce the starvation the
        # multiplier exists to prevent.
        rows = (db.query(CandidateSignal)
                  .filter(CandidateSignal.resolved == False)  # noqa: E712
                  .filter(CandidateSignal.created_at < cutoff)
                  .order_by(CandidateSignal.created_at.asc())
                  .limit(max(limit * 6, 2000)).all())
        bars_cache = {}
        attempted = 0
        for cand in rows:
            if attempted >= limit:
                break
            checked += 1
            key = (cand.symbol, cand.timeframe)
            if key not in bars_cache:
                bars_cache[key] = load_cached_bars(cand.symbol, cand.timeframe)
            bars = bars_cache[key]
            if bars is None or len(bars) == 0:
                no_bars += 1
                if cand.created_at < expire_before:
                    cand.resolved = True
                    cand.resolved_at = now.isoformat()
                    cand.exit_reason = "expired_no_bars"
                    expired += 1
                continue
            attempted += 1
            result = replay_signal({
                "id": cand.id,
                "asset_symbol": cand.symbol,
                "asset_class": cand.asset_class,
                "direction": cand.direction,
                "timeframe": cand.timeframe,
                "entry_price": cand.entry_price,
                "stop_loss": cand.stop_loss,
                "target_price": cand.target_price,
                "generated_at": cand.created_at,
            }, bars)
            if result is None:
                # Not enough forward bars yet, or degenerate levels. Left
                # unresolved; a later run gets more history to work with.
                too_young += 1
                continue
            cand.resolved = True
            cand.resolved_at = datetime.now(timezone.utc).isoformat()
            cand.outcome = result.get("outcome")
            cand.pnl_pct = result.get("pnl_pct")
            cand.mfe_r = result.get("mfe_r")
            cand.mae_r = result.get("mae_r")
            cand.first_touch = result.get("first_touch")
            cand.exit_reason = result.get("exit_reason")
            resolved += 1
        db.commit()

    out = {"checked": checked, "resolved": resolved,
           "no_bars": no_bars, "too_young": too_young, "expired": expired}
    logger.info(f"[Candidates] resolution pass: {out}")
    return out


def selection_bias_summary() -> dict:
    """Rejected vs accepted, on resolved counterfactuals.

    THE question this table exists to answer: are the filters discarding
    positive-expectancy setups? If rejected candidates outperform accepted
    ones, the gate is running backwards — no amount of new data fixes a
    filter pointed the wrong way.
    """
    from sqlalchemy import text

    from app.database import engine

    out = {"by_verdict": [], "by_rejection_reason": [], "by_gate_decision": []}
    with engine.connect() as c:
        # pnl_pct IS NOT NULL everywhere below: expired_no_bars rows are
        # resolved (so they leave the queue) but carry no outcome, and a
        # NULL falling into an ELSE 0 would count them as losses.
        for verdict, n, wr, pnl, mfe in c.execute(text("""
            SELECT verdict, COUNT(*),
                   ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 100.0 ELSE 0 END), 1),
                   ROUND(AVG(pnl_pct), 3), ROUND(AVG(mfe_r), 3)
            FROM candidate_signals WHERE resolved = 1 AND pnl_pct IS NOT NULL
            GROUP BY verdict
        """)):
            out["by_verdict"].append({
                "verdict": verdict, "n": n, "win_rate": wr,
                "avg_pnl_pct": pnl, "avg_mfe_r": mfe})
        # Aligned to the ACTUAL live gate (Phase 2): persisted-vs-rejected
        # measures the old persistence filter; what decides capital now is
        # gate_v8, so its decisions get the same counterfactual treatment.
        # If NO_TRADE resolves better than TRADE out of sample, the gate is
        # wrong and this row is where that shows first.
        #
        # STRATIFIED by timeframe (audit 2026-08-15): the decisions compose
        # differently — TRADE was 87% 1D futures while NO_TRADE was mostly
        # intraday, so a pooled TRADE-vs-NO_TRADE row would compare daily
        # corn against 15-minute crypto and call it a gate verdict.
        # effective_n counts distinct (symbol, day): five same-day ZC=F
        # candidates are one market opinion, not five samples.
        for decision, tf, n, eff, wr, pnl, mfe in c.execute(text("""
            SELECT gate_v8_decision, timeframe, COUNT(*),
                   COUNT(DISTINCT symbol || '|' || date(created_at)),
                   ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 100.0 ELSE 0 END), 1),
                   ROUND(AVG(pnl_pct), 3), ROUND(AVG(mfe_r), 3)
            FROM candidate_signals
            WHERE resolved = 1 AND gate_v8_decision IS NOT NULL
            GROUP BY gate_v8_decision, timeframe
            ORDER BY gate_v8_decision, COUNT(*) DESC
        """)):
            out["by_gate_decision"].append({
                "decision": decision, "timeframe": tf, "n": n,
                "effective_n": eff, "win_rate": wr,
                "avg_pnl_pct": pnl, "avg_mfe_r": mfe})
        for reason, n, wr, pnl in c.execute(text("""
            SELECT rejection_reason, COUNT(*),
                   ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 100.0 ELSE 0 END), 1),
                   ROUND(AVG(pnl_pct), 3)
            FROM candidate_signals
            WHERE resolved = 1 AND verdict = 'rejected'
            GROUP BY rejection_reason
        """)):
            out["by_rejection_reason"].append({
                "reason": reason, "n": n, "win_rate": wr,
                "avg_pnl_pct": pnl})
    return out
