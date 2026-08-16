"""Post-entry market alpha — what the TOKEN did after the wallet entered.

This is the metric the old `alpha_score` claimed to be and was not. The
comment above it read "the token's move after entry, which is what a
follower would actually capture", sitting directly on top of arithmetic
over the wallet's own CLOSED ROUND TRIPS. Those answer different questions:

    REALIZED PERFORMANCE   was the wallet good?
                           win rate, profit factor, median realized return,
                           drawdown, expectancy

    POST-ENTRY ALPHA       did following this wallet's entry create a future
                           opportunity? measured at 5m / 15m / 1h / 4h / 24h
                           after each entry, from market data, without
                           reference to when or whether the wallet exited

    COPYABILITY            could JARVIS actually capture it? what survives
                           detection latency, spread, slippage, fees and
                           the decay of the entry price

They are deliberately not aliases. A wallet can legitimately score:

    SMART MONEY 93   ALPHA 41   COPY 18
    SMART MONEY 68   ALPHA 94   COPY 86

and both combinations are informative. The first is a skilled trader whose
edge lives in exits a follower never sees; the second is a mediocre trader
who is reliably early into moves other people can still catch.

Horizons resolve LATE and INDEPENDENTLY. `return_1h` is fillable an hour
after the entry and `return_24h` a day after, so an observation is normally
partially resolved. A pending horizon is NULL and never zero — the
difference between "the token did not move" and "we have not looked yet" is
the whole point of measuring this at all.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The horizons, in seconds. Ordered, because a later horizon cannot resolve
# before an earlier one has elapsed.
HORIZONS = (("5m", 300), ("15m", 900), ("1h", 3600),
            ("4h", 14400), ("24h", 86400))

# Below this many resolved observations an aggregate is an anecdote. Same
# discipline as MIN_TRADES_FOR_SCORE in wallet_scoring: a 100% figure from
# two sightings must never outrank a measured one from forty.
MIN_OBSERVATIONS_FOR_ALPHA = 5
FULL_CONFIDENCE_OBSERVATIONS = 25


def _parse_ts(v) -> datetime | None:
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        t = datetime.fromisoformat(str(v))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def record_observation(session, *, wallet_address: str, mint: str,
                       signature: str, entry_timestamp, entry_price_usd=None,
                       entry_amount=None, entry_notional_usd=None,
                       pool=None, token_symbol=None, discovery_source=None,
                       surge_event_id=None, surge_started_at=None,
                       price_source=None, price_quality=None):
    """Append one sighting. Idempotent on (wallet, signature, mint).

    Returns (row, created). An existing sighting is NOT new evidence — a
    re-scan of the same signature is the same event — but a NEW signature
    from a known wallet very much is, and that is what discovery used to
    discard by `continue`-ing past any wallet already in the registry.
    """
    from app.database import WalletObservation, now_iso

    existing = (session.query(WalletObservation)
                .filter(WalletObservation.wallet_address == wallet_address,
                        WalletObservation.signature == signature,
                        WalletObservation.mint == mint).first())
    if existing is not None:
        return existing, False

    entry_dt = _parse_ts(entry_timestamp)
    surge_dt = _parse_ts(surge_started_at)
    seconds_before = None
    if entry_dt and surge_dt:
        # NEGATIVE means the wallet was EARLY — it entered before the surge
        # crossed its threshold. That population is the point of W7.
        seconds_before = (entry_dt - surge_dt).total_seconds()

    row = WalletObservation(
        wallet_address=wallet_address, mint=mint, pool=pool,
        token_symbol=token_symbol, discovery_source=discovery_source,
        surge_event_id=surge_event_id,
        surge_started_at=surge_dt.isoformat() if surge_dt else None,
        seconds_before_surge=seconds_before,
        signature=signature,
        entry_timestamp=entry_dt.isoformat() if entry_dt else None,
        entry_amount=entry_amount, entry_notional_usd=entry_notional_usd,
        entry_price_usd=entry_price_usd,
        price_source=price_source, price_quality=price_quality,
        horizons_resolved="", fully_resolved=0, observed_at=now_iso(),
    )
    session.add(row)
    session.flush()
    return row, True


def due_horizons(row, *, now: datetime | None = None) -> list[str]:
    """Which horizons have ELAPSED for this observation and are still unset.

    Elapsed is not the same as resolved: this says what can now be looked
    up, and the lookup may still fail for want of price data.
    """
    entry = _parse_ts(row.entry_timestamp)
    if entry is None:
        return []
    now = now or datetime.now(timezone.utc)
    done = set((row.horizons_resolved or "").split(",")) - {""}
    out = []
    for name, secs in HORIZONS:
        if name in done:
            continue
        if now >= entry + timedelta(seconds=secs):
            out.append(name)
    return out


def resolve_observation(session, row, price_lookup, *,
                        now: datetime | None = None) -> dict:
    """Fill every horizon that has elapsed, using `price_lookup(mint, when)`.

    `price_lookup` returns a float or None. None means the price is not
    available, and the horizon stays NULL and unresolved so a later pass can
    try again — it must never be recorded as a zero return.
    """
    entry = _parse_ts(row.entry_timestamp)
    entry_px = row.entry_price_usd
    if entry is None or not entry_px:
        return {"resolved": [], "skipped": "no entry price or timestamp"}

    done = set((row.horizons_resolved or "").split(",")) - {""}
    resolved = []
    for name in due_horizons(row, now=now):
        secs = dict(HORIZONS)[name]
        px = price_lookup(row.mint, entry + timedelta(seconds=secs))
        if px is None:
            continue
        setattr(row, f"price_{name}", float(px))
        setattr(row, f"return_{name}", (float(px) - entry_px) / entry_px * 100.0)
        done.add(name)
        resolved.append(name)

    if resolved:
        row.horizons_resolved = ",".join(
            n for n, _ in HORIZONS if n in done)
        row.fully_resolved = int(len(done) == len(HORIZONS))
    return {"resolved": resolved, "all_done": bool(row.fully_resolved)}


def alpha_for_wallet(session, wallet_address: str) -> dict:
    """Aggregate post-entry alpha across this wallet's observations.

    MEDIAN per horizon, so one moonshot cannot carry the profile, and each
    horizon is aggregated over its OWN resolved observations — a wallet with
    forty resolved 5m returns and three resolved 24h returns reports both
    honestly rather than pretending to a 24h number it has not earned.
    """
    from app.database import WalletObservation

    rows = (session.query(WalletObservation)
            .filter(WalletObservation.wallet_address == wallet_address).all())

    out = {
        "wallet": wallet_address,
        "observations": len(rows),
        "horizons": {},
        "alpha_score": None,
        "confidence": 0.0,
        "measurable": False,
        "reason": "",
    }
    if not rows:
        out["reason"] = "no observations recorded for this wallet"
        return out

    early = [r.seconds_before_surge for r in rows
             if r.seconds_before_surge is not None]
    if early:
        out["median_seconds_before_surge"] = statistics.median(early)
        out["entered_before_surge"] = sum(1 for s in early if s < 0)

    for name, _ in HORIZONS:
        vals = [getattr(r, f"return_{name}") for r in rows
                if getattr(r, f"return_{name}") is not None]
        out["horizons"][name] = {
            "n": len(vals),
            "median_return_pct": round(statistics.median(vals), 4) if vals else None,
            "positive_rate": (round(sum(1 for v in vals if v > 0) / len(vals), 4)
                              if vals else None),
        }

    # The headline score reads the 1h horizon, which is the one a follower
    # could realistically act within. It is NOT an average across horizons —
    # that would blur a wallet that is early into fast moves with one that
    # is early into slow ones.
    h1 = out["horizons"]["1h"]
    n1 = h1["n"]
    if n1 < MIN_OBSERVATIONS_FOR_ALPHA:
        out["reason"] = (f"{n1} resolved 1h observation(s) — below the "
                         f"{MIN_OBSERVATIONS_FOR_ALPHA} needed before a median "
                         f"means anything")
        return out

    confidence = min(1.0, n1 / FULL_CONFIDENCE_OBSERVATIONS)
    med = h1["median_return_pct"] or 0.0
    raw = min(100.0, max(0.0, 50.0 + med * 2.0))
    # Pulled toward neutral by sample size, same as smart_money_score.
    out["alpha_score"] = round(50.0 + (raw - 50.0) * confidence, 2)
    out["confidence"] = round(confidence * 100.0, 2)
    out["measurable"] = True
    out["reason"] = (f"median 1h post-entry move {med:+.2f}% over {n1} "
                     f"resolved observations, confidence {confidence:.0%}")
    return out


def copyability(session, wallet_address: str, *,
                detection_latency_s: float = 900.0,
                round_trip_cost_pct: float = 0.60) -> dict:
    """What a follower could realistically have captured, not what existed.

    Post-entry alpha measures the opportunity. This measures the part of it
    that survives:

      DETECTION LATENCY  JARVIS sees an entry when it next polls, not when
                         it happens. The 15-minute collector means the 5m
                         horizon is already gone by the time anything knows,
                         so the capturable horizon starts at the first one
                         beyond the latency.
      ENTRY DECAY        the price has already moved by then; what remains
                         is the later horizon measured from the same entry.
      COSTS              spread, slippage and fees on the way in and out.

    Deliberately conservative and explicit: every input is a stated
    assumption, and none of them is hidden inside a score.
    """
    a = alpha_for_wallet(session, wallet_address)
    out = {"wallet": wallet_address, "copy_score": None, "measurable": False,
           "assumptions": {"detection_latency_s": detection_latency_s,
                           "round_trip_cost_pct": round_trip_cost_pct},
           "reason": ""}
    if not a["measurable"]:
        out["reason"] = a["reason"]
        return out

    # The first horizon a follower could actually act at.
    capturable = next((n for n, s in HORIZONS if s >= detection_latency_s), None)
    if capturable is None:
        out["reason"] = "detection latency exceeds every measured horizon"
        return out

    h = a["horizons"][capturable]
    if not h["n"] or h["median_return_pct"] is None:
        out["reason"] = f"no resolved {capturable} observations to judge capture"
        return out

    net = h["median_return_pct"] - round_trip_cost_pct
    out.update(
        capturable_horizon=capturable,
        gross_move_pct=h["median_return_pct"],
        net_after_costs_pct=round(net, 4),
        observations=h["n"],
        copy_score=round(min(100.0, max(0.0, 50.0 + net * 2.0)), 2),
        measurable=True,
        reason=(f"a follower detecting at ~{detection_latency_s / 60:.0f}m could "
                f"target the {capturable} horizon: {h['median_return_pct']:+.2f}% "
                f"gross, {net:+.2f}% after {round_trip_cost_pct}% costs"),
    )
    return out
