"""Promotion and demotion — the engine that did not exist.

Before this module the ONLY lifecycle write in the codebase was a single
`w.status = "WATCH"` inside the scoring function. `WATCH -> SMART_MONEY`,
`SMART_MONEY -> HIGH_CONVICTION` and every demotion path were absent, so
`counts()` reported smart_money and high_conviction buckets that nothing
could ever fill.

TWO SEPARATIONS ARE THE POINT.

1. SCORING DECIDES NOTHING. Scoring measures; this decides. They were
   coupled — a measurable wallet was promoted as a side effect of being
   scored — which meant the promotion rule could never be changed, audited
   or tested independently of the arithmetic that produced its inputs.

2. SIZE IS NOT SKILL. A whale is a wallet with money. Promotion requires
   measured trading evidence, and `whale_score` is deliberately absent from
   every rule below. Turning a whale into smart money because it is large
   is precisely how the first live discovery pass would have promoted a
   Binance hot wallet.

DEMOTION IS SYMMETRIC AND CHEAPER TO EARN THAN PROMOTION. A wallet that
stops producing evidence degrades; one whose evidence turns bad degrades
faster. The registry keeps the identity and the history either way — a
demoted wallet is still something the system has learned.

The objective is NOT more SMART_MONEY labels. It is fewer false ones.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Promotion bars ───────────────────────────────────────────────────────
# Every threshold is a MINIMUM, and a wallet must clear all of them. They
# are deliberately hard: the cost of a false SMART_MONEY is that the desk
# starts treating noise as signal, and the cost of a missed one is that a
# wallet stays on WATCH where it is still being measured.

WATCH_MIN_SMART_MONEY = 55.0
WATCH_MIN_CONFIDENCE = 25.0

SMART_MONEY_MIN_SCORE = 65.0
SMART_MONEY_MIN_CONFIDENCE = 50.0
SMART_MONEY_MIN_TRADES = 15
# Post-entry alpha is REQUIRED here, not optional: "this wallet trades well"
# and "following this wallet creates an opportunity" are different claims,
# and SMART_MONEY asserts the second.
SMART_MONEY_MIN_ALPHA = 55.0
SMART_MONEY_MIN_ALPHA_OBSERVATIONS = 5

HIGH_CONVICTION_MIN_SCORE = 75.0
HIGH_CONVICTION_MIN_CONFIDENCE = 70.0
HIGH_CONVICTION_MIN_ALPHA = 65.0
HIGH_CONVICTION_MIN_ALPHA_OBSERVATIONS = 15
# Copyability is a SEPARATE dimension and only gates the top tier: a wallet
# can be genuinely excellent and completely uncopyable, and that fact
# belongs in the label rather than being averaged away.
HIGH_CONVICTION_MIN_COPY = 45.0

# ── Demotion ─────────────────────────────────────────────────────────────
STALE_AFTER_DAYS = 30           # no new evidence at all
DEGRADE_SMART_MONEY_BELOW = 50.0
DEGRADE_ALPHA_BELOW = 45.0
ARCHIVE_AFTER_DEGRADED_DAYS = 60

ORDER = ["DISCOVERED", "CANDIDATE", "ANALYZING", "WATCH",
         "SMART_MONEY", "HIGH_CONVICTION"]


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return None


def evaluate(wallet, alpha: dict | None = None,
             copy: dict | None = None) -> dict:
    """The lifecycle verdict for ONE wallet. Pure — writes nothing.

    Returns {"status": <target>, "changed": bool, "reasons": [...]} so the
    decision can be tested, logged and reviewed without a database.
    """
    current = wallet.status or "CANDIDATE"
    alpha = alpha or {}
    copy = copy or {}
    reasons: list[str] = []

    if current in ("EXCLUDED_ENTITY", "ARCHIVED"):
        return {"status": current, "changed": False,
                "reasons": [f"{current} is terminal for lifecycle purposes"]}

    # IDENTITY OVERRIDES SCORE. Promotion asked only "how well did it do?",
    # never "what is it?" — so a router, pool, treasury or exchange whose
    # flow happens to score well was promotable into WATCH and from there
    # into the monitored population that produces wallet-alpha picks.
    # Entity classification is not something a good score can outrank.
    from lib.wallet_classify import NON_TRADER_ENTITIES

    etype = str(getattr(wallet, "entity_type", None) or "").upper()
    if etype in NON_TRADER_ENTITIES or getattr(wallet, "is_protocol", False):
        return {"status": "EXCLUDED_ENTITY", "changed": current != "EXCLUDED_ENTITY",
                "reasons": [f"classified {etype or 'PROTOCOL'} — entity flow is "
                            f"not copyable trader alpha, whatever it scores"]}

    sm = wallet.smart_money_score
    conf = wallet.confidence_score or 0.0
    trades = wallet.qualified_trades or 0
    a_score = alpha.get("alpha_score")
    a_n = (alpha.get("horizons", {}).get("1h", {}) or {}).get("n") or 0
    c_score = copy.get("copy_score")

    # ── demotion first: evidence that has gone bad or gone quiet ─────────
    stale = _age_days(wallet.last_seen_at or wallet.last_score_update)
    if current in ("WATCH", "SMART_MONEY", "HIGH_CONVICTION"):
        if stale is not None and stale > STALE_AFTER_DAYS:
            reasons.append(
                f"no new evidence for {stale:.0f} days (limit {STALE_AFTER_DAYS})")
            return {"status": "DEGRADED", "changed": True, "reasons": reasons}
        if sm is not None and sm < DEGRADE_SMART_MONEY_BELOW:
            reasons.append(
                f"smart money {sm:.1f} fell below {DEGRADE_SMART_MONEY_BELOW}")
            return {"status": "DEGRADED", "changed": True, "reasons": reasons}
        if (a_score is not None and a_n >= SMART_MONEY_MIN_ALPHA_OBSERVATIONS
                and a_score < DEGRADE_ALPHA_BELOW):
            reasons.append(
                f"post-entry alpha {a_score:.1f} fell below {DEGRADE_ALPHA_BELOW} "
                f"over {a_n} observations")
            return {"status": "DEGRADED", "changed": True, "reasons": reasons}

    if current == "DEGRADED":
        age = _age_days(wallet.updated_at)
        if age is not None and age > ARCHIVE_AFTER_DEGRADED_DAYS:
            return {"status": "ARCHIVED", "changed": True,
                    "reasons": [f"degraded for {age:.0f} days"]}
        return {"status": "DEGRADED", "changed": False,
                "reasons": ["still degraded; recovery requires fresh evidence"]}

    # ── promotion: strictly one tier at a time ───────────────────────────
    if sm is None:
        return {"status": current, "changed": False,
                "reasons": ["unmeasured — no smart money score yet"]}

    target = current

    if current in ("DISCOVERED", "CANDIDATE", "ANALYZING"):
        if sm >= WATCH_MIN_SMART_MONEY and conf >= WATCH_MIN_CONFIDENCE:
            target = "WATCH"
            reasons.append(f"smart money {sm:.1f} >= {WATCH_MIN_SMART_MONEY} "
                           f"at {conf:.0f}% confidence")
        else:
            reasons.append(
                f"below the WATCH bar (smart money {sm:.1f}/{WATCH_MIN_SMART_MONEY}, "
                f"confidence {conf:.0f}/{WATCH_MIN_CONFIDENCE})")

    elif current == "WATCH":
        gates = [
            (sm >= SMART_MONEY_MIN_SCORE,
             f"smart money {sm:.1f}/{SMART_MONEY_MIN_SCORE}"),
            (conf >= SMART_MONEY_MIN_CONFIDENCE,
             f"confidence {conf:.0f}/{SMART_MONEY_MIN_CONFIDENCE}"),
            (trades >= SMART_MONEY_MIN_TRADES,
             f"trades {trades}/{SMART_MONEY_MIN_TRADES}"),
            (a_n >= SMART_MONEY_MIN_ALPHA_OBSERVATIONS,
             f"alpha observations {a_n}/{SMART_MONEY_MIN_ALPHA_OBSERVATIONS}"),
            (a_score is not None and a_score >= SMART_MONEY_MIN_ALPHA,
             f"post-entry alpha {a_score if a_score is not None else 'unmeasured'}"
             f"/{SMART_MONEY_MIN_ALPHA}"),
        ]
        failed = [w for ok, w in gates if not ok]
        if failed:
            reasons.append("held at WATCH: " + "; ".join(failed))
        else:
            target = "SMART_MONEY"
            reasons.append("cleared every SMART_MONEY gate including measured "
                           "post-entry alpha")

    elif current == "SMART_MONEY":
        gates = [
            (sm >= HIGH_CONVICTION_MIN_SCORE,
             f"smart money {sm:.1f}/{HIGH_CONVICTION_MIN_SCORE}"),
            (conf >= HIGH_CONVICTION_MIN_CONFIDENCE,
             f"confidence {conf:.0f}/{HIGH_CONVICTION_MIN_CONFIDENCE}"),
            (a_n >= HIGH_CONVICTION_MIN_ALPHA_OBSERVATIONS,
             f"alpha observations {a_n}/{HIGH_CONVICTION_MIN_ALPHA_OBSERVATIONS}"),
            (a_score is not None and a_score >= HIGH_CONVICTION_MIN_ALPHA,
             f"post-entry alpha {a_score if a_score is not None else 'unmeasured'}"
             f"/{HIGH_CONVICTION_MIN_ALPHA}"),
            (c_score is not None and c_score >= HIGH_CONVICTION_MIN_COPY,
             f"copyability {c_score if c_score is not None else 'unmeasured'}"
             f"/{HIGH_CONVICTION_MIN_COPY}"),
        ]
        failed = [w for ok, w in gates if not ok]
        if failed:
            reasons.append("held at SMART_MONEY: " + "; ".join(failed))
        else:
            target = "HIGH_CONVICTION"
            reasons.append("cleared every HIGH_CONVICTION gate")

    return {"status": target, "changed": target != current, "reasons": reasons}


def run(limit: int = 200, db=None) -> dict:
    """Apply lifecycle transitions across the registry.

    Reads alpha and copyability from the observation evidence rather than
    recomputing anything: this module decides, it does not measure.
    """
    from app.database import WalletRegistry, get_db, now_iso
    from lib.wallet_alpha import alpha_for_wallet, copyability

    def _run(session):
        rows = (session.query(WalletRegistry)
                .filter(~WalletRegistry.status.in_(("EXCLUDED_ENTITY", "ARCHIVED")))
                .limit(max(1, min(limit, 1000))).all())
        stats = {"examined": 0, "promoted": 0, "demoted": 0,
                 "unchanged": 0, "transitions": []}
        for w in rows:
            stats["examined"] += 1
            alpha = alpha_for_wallet(session, w.address)
            copy = copyability(session, w.address)
            verdict = evaluate(w, alpha, copy)
            if not verdict["changed"]:
                stats["unchanged"] += 1
                continue
            before, after = w.status, verdict["status"]
            try:
                up = ORDER.index(after) > ORDER.index(before)
            except ValueError:
                up = False
            w.status = after
            # Alpha earned by observation belongs on the row, so the UI and
            # the promotion rule read the same number.
            if alpha.get("measurable"):
                w.alpha_score = alpha["alpha_score"]
            if copy.get("measurable"):
                w.copy_score = copy["copy_score"]
            w.updated_at = now_iso()
            stats["promoted" if up else "demoted"] += 1
            stats["transitions"].append({
                "address": w.address, "from": before, "to": after,
                "reasons": verdict["reasons"],
            })
            logger.info(f"[WalletLifecycle] {w.address[:8]}… {before} -> {after}: "
                        f"{'; '.join(verdict['reasons'])}")
        return stats

    if db is not None:
        return _run(db)
    with get_db() as _db:
        return _run(_db)
