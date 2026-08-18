"""What happened AFTER a decision — and nothing about what we did next.

THE QUESTION THIS ANSWERS. 11,952 historical cost-gate rejections were
recorded, and 11,775 of them had no usable forward evidence at all. Not
because the analysis was hard, but because the evidence was thrown away the
moment the answer was NO_TRADE. This module stops throwing it away: every
material decision gets its forward market evidence collected at horizons
fixed from T0, whether or not anything was traded.

THE THING IT MUST NEVER BECOME. A forward outcome is NOT an
ExecutionResult, a PaperTrade, a RealizedOutcome or a simulated P&L. No
row written here opens a position, moves cash, releases margin, increments
total_trades or casts a learning vote. A refused decision has no fill, so
this evidence is barred from fill and slippage calibration — the predicate
in `decision_observation.is_execution_calibration_eligible` already draws
that line and this module stays on the far side of it.

FOUR RULES THAT SHAPE EVERYTHING HERE.

1. DUE FROM T0. `due_at = decision_at + horizon`, never observer wake-up +
   horizon. Otherwise every outage silently lengthens the interval being
   measured and the horizon label becomes a lie.

2. A CHECKPOINT IS NOT A RANGE. One quote at due_at can produce a return.
   It cannot produce MFE or MAE, which are claims about the whole
   interval. Absent interval evidence those stay NULL with
   INSUFFICIENT_RANGE_DATA — because zero would claim the market never
   moved in our favour, which is a strong and usually false claim.

3. PRODUCT-CORRECT OR NOTHING. A perpetual is scored from the perpetual
   book. Never from spot: they diverge by basis and funding, and the perp
   is the instrument whose price would have determined the P&L. This is
   inherited rather than re-implemented — all market access runs through
   `execution_snapshot`/`range_collector`, which already refuse the
   substitution and already fail closed on SHIB's unverified price scale.

4. NEVER CHOOSE THE FLATTERING READING. If both levels were touched and
   the evidence cannot order them, the answer is AMBIGUOUS_INTRABAR — not
   "target first".
"""
from __future__ import annotations

import json
import logging

from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

OUTCOME_OBSERVER_VERSION = "decision_outcome_observer_v1"

# ── Lifecycle. Monotonic: PENDING is the only non-terminal state. ────────
#
# Nothing here improves with a later look. The evidence for a closed
# interval is whatever was collected while it was open, so a resolved row
# is final and a second finalisation is a no-op rather than an update.
PENDING           = "PENDING"
COMPLETE          = "COMPLETE"
PARTIAL_EVIDENCE  = "PARTIAL_EVIDENCE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
EXPIRED           = "EXPIRED"
AMBIGUOUS_INTRABAR = "AMBIGUOUS_INTRABAR"

TERMINAL = frozenset({COMPLETE, PARTIAL_EVIDENCE, INSUFFICIENT_DATA,
                      EXPIRED, AMBIGUOUS_INTRABAR})

# ── Touch ordering ───────────────────────────────────────────────────────
TARGET_FIRST = "TARGET_FIRST"
STOP_FIRST   = "STOP_FIRST"
NEITHER      = "NEITHER"
# Same bucket, both levels: the evidence records that both happened and
# refuses to say which came first.
TOUCH_AMBIGUOUS = "AMBIGUOUS_INTRABAR"

# ── Horizons ─────────────────────────────────────────────────────────────
HORIZON_MINUTES = {
    "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480, "1d": 1440,
}

# WHICH HORIZONS EACH TIMEFRAME EARNS. Not every horizon for every
# strategy: a 1-day setup learns nothing from a 1-minute checkpoint, and
# scheduling one would multiply storage and observer work by the number of
# horizons nobody will read. Deterministic by construction — the policy is
# a table, never a model's guess.
HORIZONS_BY_TIMEFRAME = {
    "1m":  ("1m", "5m", "15m", "30m"),
    "3m":  ("5m", "15m", "30m", "1h"),
    "5m":  ("5m", "15m", "30m", "1h"),
    "15m": ("15m", "30m", "1h", "2h"),
    "30m": ("30m", "1h", "2h", "4h"),
    "1h":  ("1h", "2h", "4h", "8h"),
    "2h":  ("2h", "4h", "8h", "1d"),
    "4h":  ("4h", "8h", "1d"),
    "1d":  ("8h", "1d"),
}
# An unknown timeframe gets a deliberately middling spread rather than
# everything — an unlabelled decision is not a reason to pay for ten
# horizons.
DEFAULT_HORIZONS = ("15m", "1h", "4h")

# Past this much beyond due_at with still no usable evidence, the row stops
# waiting. The interval is long closed; more observer cycles cannot help.
EXPIRY_GRACE_MIN = 1440

# Decisions worth observing. A TRADE is observed too: the counterfactual
# "what did the market do over this horizon" is the same question, and
# having it for executed decisions is what makes the refused ones
# comparable to anything.
OBSERVABLE_DECISIONS = frozenset({"NO_TRADE", "ABSTAIN", "TRADE"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        p = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return p if p.tzinfo else p.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def horizons_for(*, timeframe: str | None,
                 expected_hold_hours: float | None = None) -> tuple[str, ...]:
    """The horizons this decision earns, from stated authority only.

    `expected_hold_hours` is used when the decision actually carried one;
    it adds the nearest standard horizon so the intended holding period is
    always measured. When it is NULL — which is the common case today —
    the timeframe table decides. There is no inference step and no model
    in this path.
    """
    tf = str(timeframe or "").strip().lower()
    base = list(HORIZONS_BY_TIMEFRAME.get(tf, DEFAULT_HORIZONS))
    hold = _f(expected_hold_hours)
    if hold and hold > 0:
        want = hold * 60.0
        nearest = min(HORIZON_MINUTES, key=lambda h: abs(HORIZON_MINUTES[h] - want))
        if nearest not in base:
            base.append(nearest)
    return tuple(sorted(set(base), key=lambda h: HORIZON_MINUTES[h]))


def _reference_price(obs) -> tuple[float | None, str]:
    """The T0 anchor every return is measured from.

    Preference is the T0 MIDPOINT, because the checkpoint is a midpoint
    too and comparing a midpoint against a decision price would fold the
    half-spread into the return. `decision_price` is the documented
    fallback when the T0 book was not two-sided.
    """
    bid, ask = _f(obs.bid), _f(obs.ask)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0, "T0_MIDPOINT"
    dp = _f(obs.decision_price)
    if dp and dp > 0:
        return dp, "T0_DECISION_PRICE"
    return None, "NONE"


# ── Scheduling ───────────────────────────────────────────────────────────

def schedule_for_observation(obs, *, db=None) -> dict:
    """Create the PENDING horizon rows for one decision observation.

    Idempotent by unique index on (observation_id, horizon): a rerun adds
    nothing and raises nothing.
    """
    from app.database import DecisionOutcome, get_db

    if obs is None or not obs.observation_id:
        return {"scheduled": 0, "skipped": "no observation_id"}
    if str(obs.final_decision or "") not in OBSERVABLE_DECISIONS:
        return {"scheduled": 0, "skipped": f"decision {obs.final_decision!r}"}

    t0 = _parse(obs.decision_at)
    if t0 is None:
        return {"scheduled": 0, "skipped": "no decision_at"}

    horizons = horizons_for(timeframe=obs.timeframe,
                            expected_hold_hours=obs.expected_hold_hours)
    ref, ref_basis = _reference_price(obs)
    from lib.trade_side import parse_side_strict
    side = parse_side_strict(obs.side)

    own = db is None
    ctx = get_db() if own else None
    session = ctx.__enter__() if own else db
    try:
        existing = {h for (h,) in session.query(DecisionOutcome.horizon).filter(
            DecisionOutcome.observation_id == obs.observation_id).all()}
        n = 0
        for h in horizons:
            if h in existing:
                continue
            session.add(DecisionOutcome(
                observation_id=obs.observation_id,
                horizon=h, horizon_min=HORIZON_MINUTES[h],
                due_at=(t0 + timedelta(minutes=HORIZON_MINUTES[h])).isoformat(),
                decision_at=t0.isoformat(),
                symbol=obs.symbol, asset_class=obs.asset_class,
                product=obs.product, venue=obs.venue,
                instrument_id=obs.instrument_id,
                market_data_source=obs.market_data_source,
                side=side, reference_price=ref,
                intended_stop=_f(obs.intended_stop),
                intended_target=_f(obs.intended_target),
                status=PENDING,
                observer_version=OUTCOME_OBSERVER_VERSION,
                provenance=json.dumps({"reference_basis": ref_basis,
                                       "side_parsed_from": obs.side}),
            ))
            n += 1
        # FLUSH EXPLICITLY. The session is built with autoflush=False, so
        # without this the rows stay invisible to any caller sharing the
        # transaction — including the very next query in it — and a
        # scheduler that appears to have done nothing is indistinguishable
        # from one that failed.
        if n:
            session.flush()
        return {"scheduled": n, "horizons": list(horizons)}
    finally:
        if own:
            ctx.__exit__(None, None, None)


def schedule_pending_observations(limit: int = 500) -> dict:
    """Backfill horizons for observations that have none yet."""
    from app.database import DecisionObservation, DecisionOutcome, get_db

    with get_db() as db:
        scheduled_ids = {o for (o,) in db.query(
            DecisionOutcome.observation_id).distinct().all()}
        rows = (db.query(DecisionObservation)
                  .filter(DecisionObservation.final_decision.in_(
                      sorted(OBSERVABLE_DECISIONS)))
                  .order_by(DecisionObservation.decision_at.desc())
                  .limit(limit * 4).all())
        out = {"observations": 0, "scheduled": 0}
        for obs in rows:
            if obs.observation_id in scheduled_ids:
                continue
            r = schedule_for_observation(obs, db=db)
            out["observations"] += 1
            out["scheduled"] += r.get("scheduled", 0)
            if out["observations"] >= limit:
                break
        return out


# ── Resolution ───────────────────────────────────────────────────────────

def _returns(row, checkpoint_mid, side_ref) -> dict:
    """Percent returns. Percent, ALWAYS — never raw token delta.

    A move of $0.001 on a $0.043241 coin is +2.3126%, and at 20x on a
    $1,000 margin that is roughly +$462 of gross exposure movement. Judging
    it by the size of the dollar delta is how cheap assets get written off
    as noise.
    """
    ref = _f(row.reference_price)
    out = {"midpoint_return_pct": None,
           "direction_adjusted_mid_return_pct": None,
           "side_reference_return_pct": None}
    if not ref or ref <= 0:
        return out
    if checkpoint_mid is not None:
        mid_ret = (checkpoint_mid - ref) / ref * 100.0
        out["midpoint_return_pct"] = round(mid_ret, 6)
        # DIRECTION-ADJUSTED IS SIDE-RELATIVE, so it exists only where the
        # side was actually readable. A missing side must not silently
        # become a long.
        if row.side == "long":
            out["direction_adjusted_mid_return_pct"] = round(mid_ret, 6)
        elif row.side == "short":
            out["direction_adjusted_mid_return_pct"] = round(-mid_ret, 6)
    if side_ref is not None:
        r = (side_ref - ref) / ref * 100.0
        if row.side == "short":
            r = -r
        out["side_reference_return_pct"] = round(r, 6)
    return out


def _excursions(row, ev) -> dict:
    """MFE/MAE from INTERVAL evidence, measured on the closing side.

    LONG is favoured by price going up and closes by SELLING into the bid,
    so its excursions are measured on the bid; SHORT is favoured by price
    going down and closes by BUYING the ask, so its are measured on the
    ask. Using the midpoint for both would credit each side with half a
    spread it could never have captured.
    """
    out = {"mfe_pct": None, "mae_pct": None, "mfe_r": None, "mae_r": None}
    ref = _f(row.reference_price)
    if not ref or ref <= 0 or row.side not in ("long", "short"):
        return out
    if ev is None or not ev.usable:
        return out

    if row.side == "long":
        best, worst = ev.high_bid, ev.low_bid
        if best is None or worst is None:
            return out
        mfe = (best - ref) / ref * 100.0
        mae = (worst - ref) / ref * 100.0
    else:
        best, worst = ev.low_ask, ev.high_ask
        if best is None or worst is None:
            return out
        mfe = (ref - best) / ref * 100.0
        mae = (ref - worst) / ref * 100.0

    out["mfe_pct"], out["mae_pct"] = round(mfe, 6), round(mae, 6)

    # R only where the ORIGINAL stop makes the denominator meaningful. A
    # stop on the wrong side of the reference is not a risk distance.
    stop = _f(row.intended_stop)
    if stop and stop > 0:
        risk = abs(ref - stop)
        if risk > 0:
            valid = ((row.side == "long" and stop < ref)
                     or (row.side == "short" and stop > ref))
            if valid:
                out["mfe_r"] = round(mfe / 100.0 * ref / risk, 6)
                out["mae_r"] = round(mae / 100.0 * ref / risk, 6)
    return out


def _touches(row, ev) -> dict:
    """Which levels were reached, and in what order the evidence can prove.

    Side-aware and conservative: a LONG target is reached when the BID
    reaches it (that is the price it could have sold into) and a LONG stop
    when the BID falls to it. A SHORT is measured on the ask, which is what
    it would have to buy back at.

    Ordering comes from the sample chronology, so a target reached at
    10:03 and a stop reached at 10:07 are ordered exactly. The one
    genuinely unresolvable case is both levels crossed by the SAME sample:
    the book moved through both between two observations and nothing in
    the evidence says which came first. That is recorded as
    AMBIGUOUS_INTRABAR. Picking the profitable reading instead is precisely
    the failure this system exists to prevent.
    """
    out = {"stop_touched": None, "stop_first_seen_at": None,
           "target_touched": None, "target_first_seen_at": None,
           "touch_order": None}
    if ev is None or not ev.usable or row.side not in ("long", "short"):
        return out
    stop, target = _f(row.intended_stop), _f(row.intended_target)
    if stop is None and target is None:
        return out

    stop_at = target_at = None
    stop_i = target_i = None
    blind_before_first = 0.0
    for i, s in enumerate(ev.samples):
        px = s.bid if row.side == "long" else s.ask
        if row.side == "long":
            hit_target = target is not None and px >= target
            hit_stop = stop is not None and px <= stop
        else:
            hit_target = target is not None and px <= target
            hit_stop = stop is not None and px >= stop

        first_here = ((hit_target and target_at is None)
                      or (hit_stop and stop_at is None))
        if first_here and target_at is None and stop_at is None and i > 0:
            # How long we had our eyes shut immediately before the FIRST
            # crossing. Anything that happened in there is unobserved.
            blind_before_first = (s.at - ev.samples[i - 1].at).total_seconds()

        if hit_target and target_at is None:
            target_at, target_i = s.at.isoformat(), i
        if hit_stop and stop_at is None:
            stop_at, stop_i = s.at.isoformat(), i
        if target_at is not None and stop_at is not None:
            break

    if target is not None:
        out["target_touched"] = target_at is not None
        out["target_first_seen_at"] = target_at
    if stop is not None:
        out["stop_touched"] = stop_at is not None
        out["stop_first_seen_at"] = stop_at

    if target_at is not None and stop_at is not None:
        # THE ONLY GENUINELY UNRESOLVABLE CASE. Each sample carries ONE
        # price, so no single sample can sit on both sides of a decision —
        # which means whichever level is crossed at the earlier sample was
        # genuinely seen first, PROVIDED we were watching continuously.
        # Ambiguity therefore lives entirely in the blind interval before
        # the first crossing: if the feed was dark for longer than a
        # heartbeat right before it, the other level may well have been
        # reached and recovered inside that hole, and the evidence cannot
        # say. Reporting the profitable ordering there is exactly the
        # failure this system exists to prevent.
        from lib.range_collector import HEARTBEAT_S
        if blind_before_first > HEARTBEAT_S:
            out["touch_order"] = TOUCH_AMBIGUOUS
        elif target_i < stop_i:
            out["touch_order"] = TARGET_FIRST
        else:
            out["touch_order"] = STOP_FIRST
    elif target_at is not None:
        out["touch_order"] = TARGET_FIRST
    elif stop_at is not None:
        out["touch_order"] = STOP_FIRST
    elif stop is not None or target is not None:
        out["touch_order"] = NEITHER
    return out


def resolve_outcome(row) -> dict:
    """Resolve ONE due horizon from shared evidence. Pure of side effects
    beyond the fields it returns — the caller owns the transaction."""
    from lib import range_collector as RC

    fields: dict = {"observed_at": _now().isoformat(),
                    "range_source": RC.RANGE_COLLECTOR_VERSION}
    t0, due = _parse(row.decision_at), _parse(row.due_at)
    if t0 is None or due is None:
        return {**fields, "status": INSUFFICIENT_DATA,
                "status_reason": "missing decision_at or due_at"}

    # A decision with no usable T0 anchor cannot produce ANY return, and
    # inventing one from today's price would be hindsight.
    if not _f(row.reference_price):
        return {**fields, "status": INSUFFICIENT_DATA,
                "status_reason": "no T0 reference price",
                "range_quality": RC.INSUFFICIENT_RANGE_DATA}

    key = {"symbol": row.symbol, "product": row.product, "venue": row.venue}
    if not all(key.values()):
        return {**fields, "status": INSUFFICIENT_DATA,
                "status_reason": "incomplete product identity",
                "range_quality": RC.INSUFFICIENT_RANGE_DATA}

    cp = RC.checkpoint_at(**key, at=due)
    ev = RC.range_over(**key, start=t0, end=due)

    fields.update({
        "range_quality": ev.quality,
        "sample_count": ev.sample_count,
        "max_sample_gap_s": ev.max_sample_gap_s,
        "first_sample_at": ev.first_sample_at,
        "last_sample_at": ev.last_sample_at,
        "market_data_source": cp.source if cp.ok else row.market_data_source,
    })

    if cp.ok:
        side_ref = cp.bid if row.side == "long" else (
            cp.ask if row.side == "short" else None)
        fields.update({
            "bid": cp.bid, "ask": cp.ask, "midpoint": cp.mid,
            "side_executable_reference": side_ref,
            "quote_status": "AVAILABLE", "quote_source": cp.source,
            "quote_age_ms": (cp.age_s * 1000.0) if cp.age_s is not None else None,
            "source_timestamp": cp.at,
            "data_quality": "CHECKPOINT_OBSERVED",
        })
        fields.update(_returns(row, cp.mid, side_ref))
    else:
        fields.update({"quote_status": "UNAVAILABLE",
                       "data_quality": "NO_CHECKPOINT",
                       "quote_source": cp.reason})

    fields.update(_excursions(row, ev))
    fields.update(_touches(row, ev))

    # ── status ───────────────────────────────────────────────────────────
    if not cp.ok and not ev.usable:
        late = (_now() - due).total_seconds() / 60.0
        if late > EXPIRY_GRACE_MIN:
            return {**fields, "status": EXPIRED,
                    "status_reason": (f"no product-correct evidence "
                                      f"{late / 60.0:.1f}h past the horizon")}
        return {**fields, "status": INSUFFICIENT_DATA,
                "status_reason": cp.reason or "no forward evidence"}

    if fields.get("touch_order") == TOUCH_AMBIGUOUS:
        return {**fields, "status": AMBIGUOUS_INTRABAR,
                "status_reason": ("stop and target were both reached inside "
                                  "one bucket; the order is not provable")}

    # A row is COMPLETE only with BOTH a checkpoint and interval evidence
    # good enough to have carried MFE/MAE. Anything less is real evidence
    # wearing an honest label, not a failure.
    if cp.ok and ev.quality in (RC.HIGH_FREQUENCY_SAMPLED, RC.COARSE_SAMPLED):
        return {**fields, "status": COMPLETE, "status_reason": None}
    return {**fields, "status": PARTIAL_EVIDENCE,
            "status_reason": f"checkpoint={'yes' if cp.ok else 'no'}, "
                             f"range={ev.quality}"}


def resolve_due(limit: int = 500) -> dict:
    """The observer pass: every PENDING horizon whose due time has passed.

    THE INDEXED DUE QUERY. `(status, due_at)` is covered by
    ix_decision_outcome_due, so this asks for the work that is ready
    instead of scanning every observation ever recorded.

    IDEMPOTENT. A terminal row is never revisited — the filter excludes it
    and `finalize` refuses it — so a retried cycle re-resolves nothing and
    one market event cannot vote twice.
    """
    from app.database import DecisionOutcome, get_db

    now_iso = _now().isoformat()
    out = {"checked": 0, "resolved": 0, "by_status": {}}
    with get_db() as db:
        due = (db.query(DecisionOutcome)
                 .filter(DecisionOutcome.status == PENDING,
                         DecisionOutcome.due_at <= now_iso)
                 .order_by(DecisionOutcome.due_at.asc())
                 .limit(limit).all())
        for row in due:
            out["checked"] += 1
            try:
                fields = resolve_outcome(row)
            except Exception as exc:
                logger.warning("[DecisionOutcome] %s/%s failed: %s",
                               row.observation_id, row.horizon, exc)
                continue
            if finalize(row, fields):
                out["resolved"] += 1
                s = fields["status"]
                out["by_status"][s] = out["by_status"].get(s, 0) + 1
    if out["checked"]:
        logger.info("[DecisionOutcome] pass: %s", out)
    return out


def finalize(row, fields: dict) -> bool:
    """Write a terminal resolution ONCE. Returns False if already terminal.

    Monotonicity is enforced here rather than trusted: a second
    finalisation of the same horizon mutates nothing at all, so a retry,
    a double-scheduled job and a manual rerun are all safe.
    """
    if row.status in TERMINAL:
        return False
    status = fields.get("status")
    if status not in TERMINAL:
        raise ValueError(f"refusing non-terminal finalisation: {status!r}")
    for k, v in fields.items():
        setattr(row, k, v)
    return True


def run_observer(limit: int = 500, *, collect: bool = True) -> dict:
    """Job body: sample the shared streams, then resolve what is due.

    Runnable independently of the autonomous trading scheduler, which
    stays OFF. Sampling first means a horizon coming due in this cycle is
    resolved against evidence that includes the current instant.
    """
    from lib import range_collector as RC

    out = {"collected": RC.collect_once() if collect else None}
    out["resolved"] = resolve_due(limit=limit)
    return out
