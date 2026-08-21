"""What a wallet DOES, measured — and whether that is copyable alpha.

TWO QUESTIONS THAT ARE NOT THE SAME, and conflating them is how a system
either copies a market maker or refuses a good trader:

    IDENTITY      what IS this address?  (exchange, router, pool, program)
    BEHAVIOUR     what does it DO?       (directional, making, consolidating)

`lib/wallet_classify` answers the first from structural and authoritative
evidence. This module answers the second from observed activity, and it is
CAREFUL never to dress a behavioural pattern up as an identity fact. A
wallet that quotes both sides all day looks like a market maker; that is a
finding about its behaviour, not a claim about who owns it, and it is
written as MARKET_MAKING_PATTERN rather than MARKET_MAKER for exactly that
reason.

AUTOMATION IS NOT A DISQUALIFICATION. A profitable automated directional
trader is precisely the thing worth following — it is fast, consistent and
copyable. What is NOT copyable is market making (its edge is the spread it
quotes, which a follower cannot capture), liquidity operation, custody
consolidation and routing. Those are different economic activities, and
they are separated here on evidence rather than on velocity.

MISSING EVIDENCE IS NOT A VERDICT. Every metric carries its sample count,
window, source and quality. A wallet observed for two hours produces
INSUFFICIENT_BEHAVIOURAL_EVIDENCE, never "clean". And a provider capability
that could not be consulted is reported as unavailable rather than silently
counted as absence of a problem.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BEHAVIOUR_VERSION = "wallet_behaviour_v1"

# ── Behavioural findings. Patterns, never identities. ────────────────────
DIRECTIONAL_TRADER = "DIRECTIONAL_TRADER"
AUTOMATED_DIRECTIONAL_TRADER = "AUTOMATED_DIRECTIONAL_TRADER"
MARKET_MAKING_PATTERN = "MARKET_MAKING_PATTERN"
LIQUIDITY_OPERATOR_PATTERN = "LIQUIDITY_OPERATOR_PATTERN"
CUSTODY_OR_CONSOLIDATION_PATTERN = "CUSTODY_OR_CONSOLIDATION_PATTERN"
ROUTER_PATTERN = "ROUTER_PATTERN"
INSUFFICIENT_BEHAVIOURAL_EVIDENCE = "INSUFFICIENT_BEHAVIOURAL_EVIDENCE"
UNKNOWN = "UNKNOWN"

BEHAVIOURS = (DIRECTIONAL_TRADER, AUTOMATED_DIRECTIONAL_TRADER,
              MARKET_MAKING_PATTERN, LIQUIDITY_OPERATOR_PATTERN,
              CUSTODY_OR_CONSOLIDATION_PATTERN, ROUTER_PATTERN,
              INSUFFICIENT_BEHAVIOURAL_EVIDENCE, UNKNOWN)

#: Behaviours whose economics a follower cannot reproduce. A market maker's
#: edge is the spread it quotes; a router's is flow it is paid to carry.
NON_COPYABLE_BEHAVIOURS = frozenset({
    MARKET_MAKING_PATTERN, LIQUIDITY_OPERATOR_PATTERN,
    CUSTODY_OR_CONSOLIDATION_PATTERN, ROUTER_PATTERN,
})

# ── Copyability verdicts ─────────────────────────────────────────────────
COPYABLE_EVIDENCE_SUPPORTED = "COPYABLE_EVIDENCE_SUPPORTED"
PROVISIONAL_IDENTITY_UNRESOLVED = "PROVISIONAL_IDENTITY_UNRESOLVED"
COPY_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_BEHAVIOURAL_EVIDENCE"
COPY_MARKET_MAKING = "MARKET_MAKING_PATTERN"
COPY_LIQUIDITY_OPERATION = "LIQUIDITY_OPERATION_PATTERN"
COPY_CUSTODY = "CUSTODY_CONSOLIDATION_PATTERN"
COPY_COORDINATED = "COORDINATED_CLUSTER_RISK"
COPY_CAPABILITY_UNAVAILABLE = "PROVIDER_CAPABILITY_UNAVAILABLE"
COPY_CONFIRMED_ENTITY = "CONFIRMED_ENTITY_WALLET"

COPYABILITY_STATES = (
    COPYABLE_EVIDENCE_SUPPORTED, PROVISIONAL_IDENTITY_UNRESOLVED,
    COPY_INSUFFICIENT_EVIDENCE, COPY_MARKET_MAKING,
    COPY_LIQUIDITY_OPERATION, COPY_CUSTODY, COPY_COORDINATED,
    COPY_CAPABILITY_UNAVAILABLE, COPY_CONFIRMED_ENTITY,
)

# ── Evidence quality ─────────────────────────────────────────────────────
MEASURED = "MEASURED"
ESTIMATED = "ESTIMATED"
UNAVAILABLE = "UNAVAILABLE"

# ── Thresholds. Configurable, testable, and each one explained. ──────────
# NONE of these was tuned to produce a verdict about the wallets currently
# on this deployment; they are stated in terms of the economics they mean.
_ENV = "JARVIS_WALLET_BEHAVIOUR_"


def _cfg(name: str, default: float) -> float:
    raw = os.getenv(f"{_ENV}{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


#: Below this many signatures nothing is claimed. Two hours of activity
#: cannot distinguish a market maker from a trader having a busy morning.
def min_signatures() -> int:
    return int(_cfg("MIN_SIGNATURES", 30))


#: And below this many hours of coverage, likewise — a burst says nothing
#: about a habit.
def min_window_hours() -> float:
    return _cfg("MIN_WINDOW_HOURS", 6.0)


#: ...UNLESS the sample is large. The window floor exists because a handful
#: of transactions in an hour could be anyone having a busy morning. That
#: argument disappears once the sample is big: a wallet that produces this
#: many signatures inside a short window has demonstrated machine pacing by
#: doing it, and refusing to characterise it would be discarding the
#: strongest evidence available for the reason that there is a lot of it.
def high_confidence_signatures() -> int:
    return int(_cfg("HIGH_CONFIDENCE_SIGNATURES", 100))


#: Signatures per hour above which activity is machine-paced. This does NOT
#: disqualify anything on its own; it only separates AUTOMATED_DIRECTIONAL
#: from DIRECTIONAL.
def automation_sigs_per_hour() -> float:
    return _cfg("AUTOMATION_SIGS_PER_HOUR", 5.0)


#: Two-sided share: of the mints this wallet touched, the fraction where it
#: both bought AND sold. A directional trader rotates positions; a market
#: maker quotes both sides of the same book continuously.
def market_making_two_sided_share() -> float:
    return _cfg("MM_TWO_SIDED_SHARE", 0.6)


#: ...but only when it is also fast. A slow wallet that round-trips its
#: positions is a trader taking profit, not a maker.
def market_making_min_sigs_per_hour() -> float:
    return _cfg("MM_MIN_SIGS_PER_HOUR", 5.0)


#: Distinct counterparties per signature. A router passes value between many
#: parties per transaction; a trader deals with a pool.
def router_counterparties_per_signature() -> float:
    return _cfg("ROUTER_CP_PER_SIG", 3.0)


#: Share of legs that are plain transfers rather than swaps. Custody and
#: consolidation move value without trading it.
def custody_transfer_share() -> float:
    return _cfg("CUSTODY_TRANSFER_SHARE", 0.9)


#: ...alongside a fan-out wide enough that it is serving many parties.
def custody_min_counterparties() -> int:
    return int(_cfg("CUSTODY_MIN_COUNTERPARTIES", 100))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _metric(value, *, samples, window_hours, source, quality=MEASURED,
            note=None) -> dict:
    """Every number carries how much evidence produced it."""
    return {"value": value, "samples": samples,
            "window_hours": (round(window_hours, 2)
                             if window_hours is not None else None),
            "source": source, "quality": quality if value is not None
            else UNAVAILABLE, "note": note}


def safe_label(address) -> str:
    a = str(address or "")
    return f"{a[:4]}…{a[-4:]}" if len(a) > 10 else a


# ── Evidence collection ──────────────────────────────────────────────────
def _legs_for(address: str) -> list[dict]:
    """This wallet's stored transfer legs. READ-ONLY, from the event store."""
    import os as _os
    import sqlite3

    from lib.event_store import _db_path

    path = _db_path()
    if not _os.path.exists(path):
        return []
    out: list[dict] = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for (payload,) in con.execute(
                "SELECT payload FROM events WHERE dedup_key LIKE 'helius:%'"):
            try:
                d = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if d.get("watched_wallet") != address:
                continue
            dk = str(d.get("dedup_key") or "").split(":")
            if len(dk) < 5:
                continue
            metric = str(d.get("metric") or "")
            out.append({
                "signature": dk[1],
                "mint": d.get("mint") or dk[2],
                "counterparty": d.get("counterparty"),
                "amount": d.get("value"),
                "direction": ("in" if metric.endswith("_in")
                              else "out" if metric.endswith("_out") else None),
                "block_time": d.get("exchange_ts"),
                "symbol": d.get("symbol"),
            })
    except sqlite3.Error as e:                               # noqa: BLE001
        logger.debug("[WalletBehaviour] leg read failed: %s", e)
    finally:
        con.close()
    return out


def _trades_for(address: str) -> list[dict]:
    """Durable balance-delta swaps, if deep history has reached this wallet."""
    from sqlalchemy import text

    from app.database import engine

    try:
        with engine.connect() as c:
            return [{"mint": r[0], "direction": r[1], "quantity": r[2],
                     "value_usd": r[3], "opened_at": r[4]}
                    for r in c.execute(text(
                        "SELECT mint, direction, quantity, value_usd, "
                        "opened_at FROM wallet_trades WHERE address=:a"),
                        {"a": address}).fetchall()]
    except Exception as e:                                   # noqa: BLE001
        logger.debug("[WalletBehaviour] trade read failed: %s", e)
        return []


def profile(address: str) -> dict:
    """Everything measurable about how this wallet behaves.

    Built ONLY from evidence already collected — the event store, the
    durable swap ledger and the registry. It makes no provider call, so it
    costs nothing and cannot be blocked by a plan entitlement.
    """
    legs = _legs_for(address)
    trades = _trades_for(address)

    out = {
        "wallet": safe_label(address),
        "version": BEHAVIOUR_VERSION,
        "as_of": _now().isoformat(),
        "metrics": {},
        "evidence": {"legs": len(legs), "trades": len(trades),
                     "signatures": 0, "window_hours": None,
                     "completeness": "NONE"},
    }
    M = out["metrics"]
    if not legs:
        M["signatures"] = _metric(None, samples=0, window_hours=None,
                                  source="event_store", quality=UNAVAILABLE,
                                  note="no stored transfer legs")
        return out

    sigs = {}
    times: list[float] = []
    cps_out, cps_in, cps_all, mints = set(), set(), set(), set()
    n_in = n_out = 0
    amounts: dict[str, int] = {}
    per_mint_dirs: dict[str, set] = {}

    for L in legs:
        s = L["signature"]
        sigs.setdefault(s, []).append(L)
        t = L.get("block_time")
        if isinstance(t, (int, float)):
            times.append(float(t))
        cp = L.get("counterparty")
        if cp:
            cps_all.add(cp)
            (cps_out if L["direction"] == "out" else cps_in).add(cp)
        if L.get("mint"):
            mints.add(L["mint"])
            per_mint_dirs.setdefault(L["mint"], set()).add(L["direction"])
        if L["direction"] == "in":
            n_in += 1
        elif L["direction"] == "out":
            n_out += 1
        a = L.get("amount")
        if isinstance(a, (int, float)) and a:
            amounts[f"{a:.12g}"] = amounts.get(f"{a:.12g}", 0) + 1

    n_sigs = len(sigs)
    window_h = ((max(times) - min(times)) / 3600.0) if len(times) > 1 else 0.0
    out["evidence"]["signatures"] = n_sigs
    out["evidence"]["window_hours"] = round(window_h, 2)

    src = "event_store"
    M["signatures"] = _metric(n_sigs, samples=n_sigs, window_hours=window_h,
                              source=src)
    M["legs"] = _metric(len(legs), samples=len(legs), window_hours=window_h,
                        source=src)
    M["legs_per_signature"] = _metric(
        round(len(legs) / n_sigs, 2), samples=n_sigs, window_hours=window_h,
        source=src)

    per_hour = (n_sigs / window_h) if window_h > 0 else None
    M["signatures_per_hour"] = _metric(
        round(per_hour, 3) if per_hour is not None else None,
        samples=n_sigs, window_hours=window_h, source=src,
        note="observation window, not the wallet's whole life")
    M["signatures_per_day"] = _metric(
        round(per_hour * 24, 2) if per_hour is not None else None,
        samples=n_sigs, window_hours=window_h, source=src)

    # Burstiness: coefficient of variation of inter-transaction gaps. A
    # scheduled bot is regular (low); a human is bursty (high).
    gaps = []
    ts = sorted(set(times))
    for i in range(1, len(ts)):
        gaps.append(ts[i] - ts[i - 1])
    if len(gaps) >= 3:
        mean = statistics.fmean(gaps)
        sd = statistics.pstdev(gaps)
        M["burstiness_cv"] = _metric(
            round(sd / mean, 3) if mean else None, samples=len(gaps),
            window_hours=window_h, source=src,
            note="stdev/mean of inter-transaction gaps; low is machine-regular")
        M["median_gap_seconds"] = _metric(
            round(statistics.median(gaps), 1), samples=len(gaps),
            window_hours=window_h, source=src)
    else:
        M["burstiness_cv"] = _metric(None, samples=len(gaps),
                                     window_hours=window_h, source=src,
                                     quality=UNAVAILABLE,
                                     note="fewer than 3 intervals")
        M["median_gap_seconds"] = _metric(None, samples=len(gaps),
                                          window_hours=window_h, source=src,
                                          quality=UNAVAILABLE)

    M["counterparty_diversity"] = _metric(len(cps_all), samples=n_sigs,
                                          window_hours=window_h, source=src)
    M["recipient_fan_out"] = _metric(len(cps_out), samples=n_out,
                                     window_hours=window_h, source=src)
    M["sender_fan_in"] = _metric(len(cps_in), samples=n_in,
                                 window_hours=window_h, source=src)
    M["counterparties_per_signature"] = _metric(
        round(len(cps_all) / n_sigs, 2), samples=n_sigs,
        window_hours=window_h, source=src)
    M["token_diversity"] = _metric(len(mints), samples=len(legs),
                                   window_hours=window_h, source=src)
    M["in_leg_share"] = _metric(
        round(n_in / len(legs), 3), samples=len(legs),
        window_hours=window_h, source=src)

    # Concentration: does one counterparty dominate? A trader hitting one
    # pool concentrates; a custodian serving customers does not.
    cp_counts: dict[str, int] = {}
    for L in legs:
        if L.get("counterparty"):
            cp_counts[L["counterparty"]] = cp_counts.get(L["counterparty"], 0) + 1
    top_share = (max(cp_counts.values()) / len(legs)) if cp_counts else None
    M["top_counterparty_share"] = _metric(
        round(top_share, 3) if top_share is not None else None,
        samples=len(legs), window_hours=window_h, source=src,
        note="one dominant counterparty usually means one pool")

    # Repeated identical amounts: slicing, or a fixed platform fee.
    repeats = sum(c for c in amounts.values() if c > 1)
    M["repeated_amount_share"] = _metric(
        round(repeats / len(legs), 3) if legs else None,
        samples=len(legs), window_hours=window_h, source=src,
        note="identical amounts recur in sliced execution and fixed fees")
    M["distinct_amounts"] = _metric(len(amounts), samples=len(legs),
                                    window_hours=window_h, source=src)

    # Two-sided share: mints where BOTH directions appear.
    two_sided = sum(1 for d in per_mint_dirs.values() if len(d) > 1)
    M["two_sided_mint_share"] = _metric(
        round(two_sided / len(per_mint_dirs), 3) if per_mint_dirs else None,
        samples=len(per_mint_dirs), window_hours=window_h, source=src,
        note="quoting both sides of the same book is the maker's signature")

    # Durable swap evidence, where deep history has reached.
    if trades:
        buys = [t for t in trades if str(t["direction"]).upper() == "BUY"]
        sells = [t for t in trades if str(t["direction"]).upper() == "SELL"]
        M["ledger_swaps"] = _metric(len(trades), samples=len(trades),
                                    window_hours=None, source="wallet_trades")
        M["buy_sell_ratio"] = _metric(
            round(len(buys) / len(sells), 3) if sells else None,
            samples=len(trades), window_hours=None, source="wallet_trades")
        M["swap_share_of_legs"] = _metric(
            round(len(trades) / n_sigs, 3) if n_sigs else None,
            samples=n_sigs, window_hours=window_h, source="wallet_trades",
            note="proven swaps as a share of observed signatures")
    else:
        for k in ("ledger_swaps", "buy_sell_ratio", "swap_share_of_legs"):
            M[k] = _metric(None, samples=0, window_hours=None,
                           source="wallet_trades", quality=UNAVAILABLE,
                           note="deep history has not reached this wallet")

    enough = (n_sigs >= min_signatures()
              and (window_h >= min_window_hours()
                   or n_sigs >= high_confidence_signatures()))
    out["evidence"]["completeness"] = "SUFFICIENT" if enough else "PARTIAL"
    out["evidence"]["sufficiency_basis"] = (
        "window" if window_h >= min_window_hours()
        else "sample_size" if n_sigs >= high_confidence_signatures()
        else "insufficient")
    return out


# ── The behavioural verdict ──────────────────────────────────────────────
def classify(prof: dict) -> dict:
    """One behavioural finding, with the evidence that produced it.

    A FINDING, NOT AN IDENTITY. Nothing here may write CEX, CUSTODY,
    MARKET_MAKER or EXCLUDED_ENTITY — those require structural evidence,
    known infrastructure or an authoritative label. This says only what the
    activity looks like.
    """
    M = prof.get("metrics") or {}
    ev = prof.get("evidence") or {}
    reasons: list[str] = []

    def val(name):
        return (M.get(name) or {}).get("value")

    n_sigs = ev.get("signatures") or 0
    window = ev.get("window_hours") or 0.0
    if ev.get("completeness") != "SUFFICIENT":
        return {"behaviour": INSUFFICIENT_BEHAVIOURAL_EVIDENCE,
                "confidence": "NONE",
                "reasons": [f"{n_sigs} signatures over {window}h (need "
                            f"{min_signatures()} over {min_window_hours()}h, "
                            f"or {high_confidence_signatures()} signatures "
                            f"in any window)"],
                "version": BEHAVIOUR_VERSION}

    per_hour = val("signatures_per_hour") or 0.0
    cps_per_sig = val("counterparties_per_signature") or 0.0
    two_sided = val("two_sided_mint_share")
    top_share = val("top_counterparty_share")
    swap_share = val("swap_share_of_legs")
    automated = per_hour >= automation_sigs_per_hour()

    # ROUTER: many distinct parties per transaction, value passing through.
    if cps_per_sig >= router_counterparties_per_signature():
        reasons.append(f"{cps_per_sig} counterparties per signature "
                       f"(>= {router_counterparties_per_signature()})")
        return {"behaviour": ROUTER_PATTERN, "confidence": "MODERATE",
                "reasons": reasons, "version": BEHAVIOUR_VERSION}

    # CUSTODY / CONSOLIDATION: wide fan-out, little actual trading.
    fan = (val("recipient_fan_out") or 0) + (val("sender_fan_in") or 0)
    if (fan >= custody_min_counterparties()
            and (swap_share is None or swap_share < 0.05)):
        reasons.append(f"fan-in/out across {fan} counterparties with "
                       f"almost no proven swaps")
        return {"behaviour": CUSTODY_OR_CONSOLIDATION_PATTERN,
                "confidence": "MODERATE", "reasons": reasons,
                "version": BEHAVIOUR_VERSION}

    # MARKET MAKING: both sides of the same books, continuously, fast.
    if (two_sided is not None
            and two_sided >= market_making_two_sided_share()
            and per_hour >= market_making_min_sigs_per_hour()):
        reasons.append(f"{two_sided:.0%} of mints traded in BOTH directions "
                       f"at {per_hour:.1f} signatures/hour — the edge is the "
                       f"spread, which a follower cannot capture")
        return {"behaviour": MARKET_MAKING_PATTERN, "confidence": "MODERATE",
                "reasons": reasons, "version": BEHAVIOUR_VERSION}

    # LIQUIDITY OPERATION: one venue, two-sided, but not fast.
    if (two_sided is not None and two_sided >= market_making_two_sided_share()
            and top_share is not None and top_share >= 0.8):
        reasons.append(f"two-sided flow concentrated {top_share:.0%} on a "
                       f"single counterparty")
        return {"behaviour": LIQUIDITY_OPERATOR_PATTERN,
                "confidence": "LOW", "reasons": reasons,
                "version": BEHAVIOUR_VERSION}

    # DIRECTIONAL. Automation is a description here, not a demerit.
    reasons.append(f"{per_hour:.1f} signatures/hour over {window:.0f}h, "
                   f"{two_sided:.0%} two-sided" if two_sided is not None
                   else f"{per_hour:.1f} signatures/hour over {window:.0f}h")
    if automated:
        reasons.append("machine-paced, but directional — automation is not "
                       "by itself a reason to refuse copying")
        return {"behaviour": AUTOMATED_DIRECTIONAL_TRADER,
                "confidence": "MODERATE", "reasons": reasons,
                "version": BEHAVIOUR_VERSION}
    return {"behaviour": DIRECTIONAL_TRADER, "confidence": "MODERATE",
            "reasons": reasons, "version": BEHAVIOUR_VERSION}


# ── The copyability gate ─────────────────────────────────────────────────
def copyability(address: str, *, prof=None, behaviour=None,
                registry_row=None) -> dict:
    """Is this observed behaviour copyable directional alpha?

    SEPARATE FROM IDENTITY ON PURPOSE. The entity gate asks "is this known
    infrastructure?" and overrides everything. This asks a different
    question — "even if it is a real independent wallet, can its economics
    be reproduced by a follower?" — and a wallet can fail it while being
    perfectly legitimate.

    UNRESOLVED IS NOT CLEAN. When the identity capability could not be
    consulted, the answer is PROVISIONAL, never supported.
    """
    from lib import provider_health as PH
    from lib import wallet_shadow_intel as SI

    prof = prof if prof is not None else profile(address)
    behaviour = behaviour if behaviour is not None else classify(prof)
    b = behaviour.get("behaviour")

    out = {"wallet": safe_label(address), "behaviour": b,
           "behaviour_reasons": behaviour.get("reasons"),
           "state": None, "reason": None, "copyable": False,
           "identity_capability": None, "identity_resolved": False,
           "evidence": prof.get("evidence"), "version": BEHAVIOUR_VERSION}

    row = registry_row
    if row is None:
        try:
            from sqlalchemy import text

            from app.database import engine
            with engine.connect() as c:
                r = c.execute(text(
                    "SELECT status, entity_type, is_protocol, identity_source,"
                    " identity_type FROM wallet_registry WHERE address=:a"),
                    {"a": address}).fetchone()
            row = ({"status": r[0], "entity_type": r[1], "is_protocol": r[2],
                    "identity_source": r[3], "identity_type": r[4]}
                   if r else {})
        except Exception as e:                               # noqa: BLE001
            logger.debug("[WalletBehaviour] registry read failed: %s", e)
            row = {}

    # 1. IDENTITY STILL OVERRIDES EVERYTHING.
    if SI._is_non_trader_entity(row.get("entity_type"), row.get("status")) \
            or row.get("is_protocol"):
        out.update(state=COPY_CONFIRMED_ENTITY, copyable=False,
                   reason="confirmed entity identity overrides all behaviour "
                          "and every score")
        return out

    # 2. A behavioural pattern whose economics a follower cannot reproduce.
    if b in NON_COPYABLE_BEHAVIOURS:
        state = {MARKET_MAKING_PATTERN: COPY_MARKET_MAKING,
                 LIQUIDITY_OPERATOR_PATTERN: COPY_LIQUIDITY_OPERATION,
                 CUSTODY_OR_CONSOLIDATION_PATTERN: COPY_CUSTODY,
                 ROUTER_PATTERN: COPY_CUSTODY}[b]
        out.update(state=state, copyable=False,
                   reason=f"{b}: " + "; ".join(behaviour.get("reasons") or []))
        return out

    # 3. Not enough observation to say anything either way.
    if b in (INSUFFICIENT_BEHAVIOURAL_EVIDENCE, UNKNOWN):
        out.update(state=COPY_INSUFFICIENT_EVIDENCE, copyable=False,
                   reason="; ".join(behaviour.get("reasons") or []))
        return out

    # 4. Behaviour looks directional. Now: do we actually know what it IS?
    gate = PH.should_probe("helius", "wallet_batch_identity")
    out["identity_capability"] = gate.get("status")
    resolved = bool(row.get("identity_source"))
    out["identity_resolved"] = resolved

    if not resolved:
        if gate.get("status") in (PH.PLAN_FORBIDDEN, PH.AUTH_FAILED,
                                  PH.PAYMENT_REQUIRED, PH.NOT_CONFIGURED):
            out.update(state=COPY_CAPABILITY_UNAVAILABLE, copyable=False,
                       reason=(f"behaviour is {b}, but identity could not be "
                               f"established: helius wallet_batch_identity "
                               f"is {gate['status']}. An unqueried wallet is "
                               f"not an independent one"))
            return out
        out.update(state=PROVISIONAL_IDENTITY_UNRESOLVED, copyable=False,
                   reason=(f"behaviour is {b} and no authoritative identity "
                           f"has been resolved yet"))
        return out

    out.update(state=COPYABLE_EVIDENCE_SUPPORTED, copyable=True,
               reason=(f"{b} with resolved identity and "
                       f"{prof['evidence']['signatures']} signatures over "
                       f"{prof['evidence']['window_hours']}h"))
    return out


def assess(address: str) -> dict:
    """Profile, behaviour and copyability for one wallet, in one call."""
    p = profile(address)
    b = classify(p)
    c = copyability(address, prof=p, behaviour=b)
    return {"wallet": safe_label(address), "profile": p,
            "behaviour": b, "copyability": c}
