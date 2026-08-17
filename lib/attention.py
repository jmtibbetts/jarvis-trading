"""What requires the operator's attention right now — one queue, one shape.

The Command Center had thirteen research panels and no answer to the only
question an operations screen exists to answer. Finding out whether anything
needed intervention meant reading all thirteen and knowing which numbers
were bad, which is not an operations room. It is a wall.

    Daily Brief      the briefing room — what happened, what matters today
    Command Center   the operations room — what needs me RIGHT NOW
    Everything else  analyst desks — where you investigate WHY

This module is the operations room's input. Every subsystem that can produce
a reason to intervene emits the same `AttentionItem`, so the frontend never
has to understand nine backends individually, and adding a tenth producer
does not require touching the page.

THIS IS A SUMMARY LAYER AND NEVER AN AUTHORITY. Every item carries a
`deep_link` to the page that owns the truth. When they disagree, the
authority is right — this exists to get you there, not to replace it.

## The property that makes a queue trustworthy

An empty attention queue means "nothing needs you". That is a strong claim,
and it is exactly the claim a silently-failing producer would fabricate: if
the position-risk scan throws and is swallowed, the queue renders empty and
reads as all-clear while a position sits one tick off its stop.

So producers are fault-isolated INDIVIDUALLY and their failures are
REPORTED. `degraded` lists every producer that could not run, and the queue
is `complete` only when all of them did. A UI showing an empty queue without
showing `degraded` alongside it has reimplemented the bug this guards
against.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Priority. Ordered, because the queue is ranked, not filtered. ────────
CRITICAL = "CRITICAL"   # capital or training data is at risk NOW
HIGH = "HIGH"           # needs a decision this session
MEDIUM = "MEDIUM"       # worth knowing before the next session
LOW = "LOW"             # context

_RANK = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

# ── Categories ──────────────────────────────────────────────────────────
POSITION_RISK = "POSITION_RISK"
EXECUTION = "EXECUTION"
OPPORTUNITY = "OPPORTUNITY"
CATALYST = "CATALYST"
DATA = "DATA"
SYSTEM = "SYSTEM"
LEARNING = "LEARNING"
WALLET = "WALLET"
LIQUIDITY = "LIQUIDITY"
APPROVAL = "APPROVAL"

CATEGORIES = (POSITION_RISK, EXECUTION, OPPORTUNITY, CATALYST, DATA,
              SYSTEM, LEARNING, WALLET, LIQUIDITY, APPROVAL)

# A position inside this much of its stop buffer is raised. 25% means three
# quarters of the distance from entry to stop has already been travelled.
STOP_BUFFER_PCT = 25.0


@dataclass
class AttentionItem:
    id: str
    priority: str
    category: str
    title: str
    reason: str
    source: str
    detected_at: str
    symbol: str | None = None
    product: str | None = None
    current_state: str | None = None
    suggested_action: str | None = None
    # Where the AUTHORITY for this item lives. The queue summarises; it does
    # not adjudicate.
    deep_link: str | None = None
    age_minutes: float | None = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {**asdict(self), "rank": _RANK.get(self.priority, 9)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_minutes(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 60.0, 1)
    except Exception:
        return None


# ══ Producers ═══════════════════════════════════════════════════════════
# Each returns a list and may raise. collect() isolates and REPORTS each
# failure rather than letting one dead producer empty the queue.


def _positions_near_stop(now: str) -> list[AttentionItem]:
    """Open positions that have spent most of their stop buffer.

    Covers every book. The at-risk banner used to omit the shadow book
    entirely, so a shadow position one tick off its stop raised nothing.
    """
    from app.database import AutoSimPosition, PaperPosition, get_db

    out: list[AttentionItem] = []
    with get_db() as db:
        for model, book, link in ((PaperPosition, "Virtual CEX", "#positions"),
                                  (AutoSimPosition, "Shadow", "#positions")):
            for p in db.query(model).filter(model.status == "Open").all():
                # initial_stop_loss where it exists, never the trailed stop:
                # the manager moves stops, so by mid-trade the live stop
                # often sits at breakeven and the buffer would measure the
                # trail rather than the risk actually taken. AutoSimPosition
                # has no such column, so the shadow book necessarily falls
                # back to the live stop — getattr, not an assumption.
                stop = getattr(p, "initial_stop_loss", None) or p.stop_loss
                entry, cur = p.entry_price, p.current_price
                if stop is None or entry is None or cur is None or entry == stop:
                    continue
                span = abs(float(entry) - float(stop))
                left = abs(float(cur) - float(stop))
                pct = (left / span * 100.0) if span else 100.0
                if pct > STOP_BUFFER_PCT:
                    continue
                out.append(AttentionItem(
                    id=f"stop:{book}:{p.id}",
                    priority=CRITICAL if pct <= 10 else HIGH,
                    category=POSITION_RISK,
                    symbol=p.symbol,
                    title=f"{p.symbol} is {round(pct)}% from its stop",
                    reason=(f"{book} book. Entry {entry}, stop as placed "
                            f"{stop}, now {cur} — {round(pct)}% of the "
                            f"original risk buffer remains."),
                    source=book,
                    detected_at=now,
                    current_state=f"{book} · open",
                    suggested_action="Close it, widen deliberately, or let the stop work — but decide.",
                    deep_link=link,
                    age_minutes=_age_minutes(getattr(p, "opened_at", None)),
                    meta={"buffer_pct": round(pct, 1), "book": book},
                ))
    return out


def _concentration(now: str) -> list[AttentionItem]:
    """Books sitting above the exposure cap, or unable to answer at all.

    An insolvent book is raised at CRITICAL: it refuses every open while
    its percentages read as None, which looked identical to healthy.
    """
    from lib.concentration import POSITION_BOOKS, book_status

    def _equity(book: str) -> float:
        # Each book's equity computed the way that book's OWN summary
        # computes it, so this and the concentration panel cannot disagree
        # about the denominator.
        if book == "paper":
            from lib.paper_engine import get_paper_summary
            return float((get_paper_summary() or {}).get("portfolio", {}).get("equity") or 0)
        from lib.auto_simulator import get_auto_sim_summary
        return float((get_auto_sim_summary() or {}).get("summary", {}).get("equity") or 0)

    out: list[AttentionItem] = []
    for book in POSITION_BOOKS:
        st = book_status(book, _equity(book))
        if st.get("error"):
            continue
        if st.get("solvent") is False:
            out.append(AttentionItem(
                id=f"conc:insolvent:{book}",
                priority=CRITICAL, category=POSITION_RISK,
                title=f"{book} book has no capital to size against",
                reason=st.get("state_detail") or "equity is not positive",
                source="concentration guard", detected_at=now,
                current_state="insolvent — every open is refused",
                suggested_action="Reset the book, or accept that it is closed for business.",
                deep_link="#positions", meta={"book": book}))
            continue
        for e in st.get("symbols", []):
            if e.get("over_limit"):
                out.append(AttentionItem(
                    id=f"conc:{book}:{e['symbol']}",
                    priority=HIGH, category=POSITION_RISK, symbol=e["symbol"],
                    title=f"{e['symbol']} is {e['pct_of_equity']}% of the {book} book",
                    reason=("Above the per-symbol cap. The cap governs "
                            "OPENING, so an existing breach is reported and "
                            "never auto-liquidated."),
                    source="concentration guard", detected_at=now,
                    current_state=f"${e['notional']:,.0f} notional",
                    suggested_action="Trim it, or accept the concentration knowingly.",
                    deep_link="#positions",
                    meta={"book": book, "pct": e["pct_of_equity"]}))
        if st.get("gross_over_limit"):
            out.append(AttentionItem(
                id=f"conc:gross:{book}",
                priority=HIGH, category=POSITION_RISK,
                title=f"{book} gross exposure is {st['gross_pct_of_equity']}% of equity",
                reason="Above the gross cap across all symbols in this book.",
                source="concentration guard", detected_at=now,
                current_state=f"${st['gross_notional']:,.0f} gross",
                suggested_action="Reduce somewhere before the next open is refused.",
                deep_link="#positions", meta={"book": book}))
    return out


def _pending_approvals(now: str) -> list[AttentionItem]:
    """Signals waiting on a human. These auto-execute if left alone, which
    is the whole reason they belong at the top of an attention queue rather
    than in a list somebody might scroll past."""
    from app.database import TradingSignal, get_db

    out: list[AttentionItem] = []
    with get_db() as db:
        rows = (db.query(TradingSignal)
                .filter(TradingSignal.status == "PendingApproval")
                .order_by(TradingSignal.generated_at.desc()).limit(40).all())
        for s in rows:
            out.append(AttentionItem(
                id=f"approve:{s.id}",
                priority=HIGH, category=APPROVAL,
                symbol=s.asset_symbol,
                title=f"{s.asset_symbol} {s.direction} awaits approval",
                reason=("Pending signals execute automatically at 9:30 ET if "
                        "left untouched — silence is a decision here."),
                source="signal generator", detected_at=now,
                current_state=f"{s.timeframe or '—'} · entry {s.entry_price} · stop {s.stop_loss}",
                suggested_action="Approve, reject, or let it go through knowingly.",
                deep_link="#signals",
                age_minutes=_age_minutes(s.generated_at),
                meta={"signal_id": s.id}))
    return out


def _gate_picks(now: str) -> list[AttentionItem]:
    """Active signals the gate judged TRADE — the arm that actually trades.

    Measured, not scored: the composite score is inverted against outcomes,
    so a high score is not a reason to look and a TRADE verdict is.
    """
    from app.database import CandidateSignal, TradingSignal, get_db

    out: list[AttentionItem] = []
    with get_db() as db:
        rows = (db.query(TradingSignal, CandidateSignal)
                .join(CandidateSignal, CandidateSignal.signal_id == TradingSignal.id)
                .filter(TradingSignal.status == "Active")
                .filter(CandidateSignal.gate_v8_decision == "TRADE")
                .order_by(CandidateSignal.gate_v8_net_r.desc()).limit(12).all())
        for s, c in rows:
            net = c.gate_v8_net_r
            out.append(AttentionItem(
                id=f"gate:{s.id}",
                priority=MEDIUM, category=OPPORTUNITY,
                symbol=s.asset_symbol,
                title=(f"{s.asset_symbol} {s.direction} clears the gate"
                       + (f" at {net:+.2f}R net" if net is not None else "")),
                reason=(c.gate_v8_reason or "measured edge clears the bar")[:220],
                source="gate v8", detected_at=now,
                current_state=f"{s.timeframe or '—'} · active",
                suggested_action="Open it on the Virtual CEX, or say why not.",
                deep_link="#signals",
                age_minutes=_age_minutes(s.generated_at),
                meta={"signal_id": s.id, "net_r": net}))
    return out


def _dex_liquidity(now: str) -> list[AttentionItem]:
    """On-chain positions whose exit cannot be ROUTED.

    The scenario a DEX trader actually fears — liquidity vanishing under an
    open position — and the one the simulator used to resolve by booking a
    free exit at the mid.
    """
    from app.database import DexPosition, get_db

    out: list[AttentionItem] = []
    with get_db() as db:
        rows = (db.query(DexPosition)
                .filter(DexPosition.status == "Open")
                .filter(DexPosition.exit_state.in_(
                    ["EXIT_PENDING_NO_LIQUIDITY", "PARTIALLY_EXITED"])).all())
        for p in rows:
            out.append(AttentionItem(
                id=f"dexliq:{p.id}",
                priority=CRITICAL, category=LIQUIDITY,
                symbol=p.symbol or p.mint,
                product="DEX_SPOT",
                title=f"{p.symbol or p.mint[:10]} cannot be exited",
                reason=(p.exit_blocked_reason
                        or "the exit could not be routed through any pool"),
                source="DEX book", detected_at=now,
                current_state=p.exit_state,
                suggested_action="Check the pool; this position cannot be closed at will.",
                deep_link="#virtualdex",
                age_minutes=_age_minutes(p.opened_at),
                meta={"exit_state": p.exit_state}))
    return out


def _integrity(now: str) -> list[AttentionItem]:
    """Training-data invariants that are breaking right now.

    Each defect this programme found was CONFIDENT and WRONG. A violation
    here means the corpus being written this hour is wrong, which is worse
    than a losing trade because it is not self-correcting.
    """
    from lib.integrity_panel import run_all

    out: list[AttentionItem] = []
    panel = run_all()
    for c in panel.get("checks", []):
        if c.get("status") != "VIOLATION":
            continue
        crit = c.get("severity") == "CRITICAL"
        out.append(AttentionItem(
            id=f"integrity:{c.get('key')}",
            priority=CRITICAL if crit else HIGH, category=DATA,
            title=c.get("title") or str(c.get("key")),
            reason=(c.get("detail") or c.get("why_it_matters") or "")[:260],
            source="integrity panel", detected_at=now,
            current_state=f"{c.get('count')} of {c.get('scanned')} rows",
            suggested_action="Training data is being corrupted while this stands.",
            deep_link="#ops", meta={"key": c.get("key")}))
    return out


def _system(now: str) -> list[AttentionItem]:
    """Kill switch, and jobs that failed or are wedged.

    A job stuck at "running" blocks every subsequent scheduled and manual
    trigger for that job, silently, forever.
    """
    from app.scheduler import job_status
    from lib.kill_switch import get_kill_switch_state

    out: list[AttentionItem] = []
    ks = get_kill_switch_state()
    if not ks.get("live_trading_enabled"):
        out.append(AttentionItem(
            id="system:killswitch",
            priority=MEDIUM, category=SYSTEM,
            title="Live order submission is paused",
            reason=(ks.get("paused_reason")
                    or "the kill switch is engaged — new live orders are refused"),
            source="kill switch", detected_at=now,
            current_state="paused",
            suggested_action="Existing stops still enforce; only NEW live orders are blocked.",
            deep_link="#ops",
            age_minutes=_age_minutes(ks.get("paused_at"))))

    for name, st in (job_status or {}).items():
        if st.get("error"):
            out.append(AttentionItem(
                id=f"job:{name}",
                priority=HIGH, category=SYSTEM,
                title=f"Job '{name}' failed",
                reason=str(st.get("error"))[:240],
                source="scheduler", detected_at=now,
                current_state=str(st.get("status")),
                suggested_action="Ops → Jobs to retry, or reset a wedged status.",
                deep_link="#ops", meta={"job": name}))
    return out


def _catalysts(now: str) -> list[AttentionItem]:
    """Earnings landing on symbols the desk is actually holding.

    A catalyst on a symbol nobody holds is research; a catalyst under an
    open position is an operations item, and only the second belongs here.
    """
    from app.database import PaperPosition, get_db
    from lib.api_cache import serve_with_refresh
    from lib.earnings_calendar import get_earnings_this_week

    out: list[AttentionItem] = []
    # THROUGH THE CACHE, never the raw call. A cold `get_earnings_this_week`
    # is five sequential Yahoo requests at a 10s timeout each, and an
    # operations screen that can block for the better part of a minute is
    # not an operations screen. Same cache key the earnings panel uses, so
    # the two cannot show different weeks.
    cached, _stale = serve_with_refresh(
        "earnings:this_week", 6 * 3600,
        lambda: {"symbols": sorted(get_earnings_this_week())})
    reporting = {str(s).upper() for s in ((cached or {}).get("symbols") or [])}
    if not reporting:
        return out
    with get_db() as db:
        held = {str(p.symbol).upper().replace("/USD", "")
                for p in db.query(PaperPosition).filter(
                    PaperPosition.status == "Open").all()}
    for sym in sorted(reporting & held):
        out.append(AttentionItem(
            id=f"catalyst:{sym}",
            priority=MEDIUM, category=CATALYST, symbol=sym,
            title=f"{sym} reports earnings this week",
            reason=("An open position is carrying an earnings event. The gap "
                    "risk is not in the stop distance."),
            source="earnings calendar", detected_at=now,
            current_state="held through the print",
            suggested_action="Hold through it deliberately, or reduce before it.",
            deep_link="#positions"))
    return out


PRODUCERS = (
    ("positions_near_stop", _positions_near_stop),
    ("concentration", _concentration),
    ("pending_approvals", _pending_approvals),
    ("gate_picks", _gate_picks),
    ("dex_liquidity", _dex_liquidity),
    ("integrity", _integrity),
    ("system", _system),
    ("catalysts", _catalysts),
)


def collect(*, limit: int = 60) -> dict:
    """Every reason to intervene, ranked, with the failures named.

    An empty queue is a claim that nothing needs the operator. That claim is
    only honest if every producer actually ran, so a producer that raised is
    listed in `degraded` and `complete` goes false. An interface that renders
    the queue without rendering `degraded` has undone the guard.
    """
    now = _now()
    items: list[AttentionItem] = []
    degraded: list[dict] = []

    for name, fn in PRODUCERS:
        try:
            items.extend(fn(now))
        except Exception as e:
            logger.warning(f"[Attention] producer {name} failed: {e}")
            degraded.append({
                "producer": name,
                "error": f"{type(e).__name__}: {str(e)[:160]}",
                "means": (f"nothing from {name} can appear in the queue — "
                          f"an empty queue does not rule this category out"),
            })

    items.sort(key=lambda i: (_RANK.get(i.priority, 9),
                              -(i.age_minutes or 0)))

    by_priority: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for i in items:
        by_priority[i.priority] = by_priority.get(i.priority, 0) + 1
        by_category[i.category] = by_category.get(i.category, 0) + 1

    return {
        "generated_at": now,
        "items": [i.as_dict() for i in items[:limit]],
        "total": len(items),
        "truncated": max(0, len(items) - limit),
        "by_priority": by_priority,
        "by_category": by_category,
        "degraded": degraded,
        # Only true when every producer ran. "Nothing needs you" and "we
        # could not check" must never render the same.
        "complete": not degraded,
        "producers_run": len(PRODUCERS) - len(degraded),
        "producers_total": len(PRODUCERS),
        "note": (
            "A summary layer, never an authority. Every item deep-links to "
            "the page that owns the truth; when they disagree, that page is "
            "right."
        ),
    }
