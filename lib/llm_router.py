"""One place that decides how hard the model thinks, and records whether it helped.

Before this module there was a `thinking` parameter on call_lm_studio that
defaulted to True. Fourteen call sites; three passed it. The other eleven —
news tagging, ticker triage, the analyst answer, focus profiling — inherited
chain-of-thought by accident. Nothing chose; a default chose, and it chose
the expensive mode for work that is pure extraction.

Three modes, always stated at the call site:

    FAST   enable_thinking=False. Extraction, classification, formatting.
    DEEP   enable_thinking=True.  Judgement under conflicting evidence.
    AUTO   decided here, from the numbers, with the reason recorded.

AUTO is deterministic. It reads a context dict of facts the caller already
has — leverage, contradiction count, regime transition, catalyst — and
applies fixed thresholds. It never asks a model whether to use a model, and
it never consults anything it cannot show you afterwards: every AUTO
decision carries the trigger that fired.

The second half of this module is the point of the first. Every call is
recorded with its task, mode, thinking flag, token counts and latency, and
— when the caller supplies a signal_id — the outcome of the trade that came
out of it. That makes "does thinking mode actually improve trading
outcomes, per task type" a query rather than an opinion. Right now nobody
knows, including whoever set the default to True.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FAST = "FAST"
DEEP = "DEEP"
AUTO = "AUTO"
MODES = (FAST, DEEP, AUTO)


# ── What the model is never allowed to be responsible for ────────────────────
#
# Every one of these is arithmetic with one right answer, already computed by
# a deterministic module: lib/venues.py (fees), lib/paper_engine.py (P&L,
# margin, liquidation), lib/risk_manager.py (sizing, portfolio limits),
# lib/ta_engine.py (indicators), lib/signal_scorer.py (expected value).
#
# A language model asked for one of these does not decline — it produces a
# plausible number. That number then flows into a position size or a fee
# estimate and is indistinguishable from a computed one. So the router
# strips these keys out of parsed output rather than trusting review to
# catch them: the failure is silent by construction, and silent numeric
# corruption is exactly what this codebase has already been bitten by.
FORBIDDEN_OUTPUT_KEYS = {
    # fees and costs
    "fee", "fees", "fee_usd", "fee_pct", "commission", "funding_cost",
    "slippage_usd", "total_cost", "round_trip_fee",
    # P&L
    "pnl", "pnl_usd", "pnl_pct", "profit", "loss", "realized_pnl",
    "unrealized_pnl", "net_pnl", "gross_pnl",
    # leverage and liquidation
    "leverage", "liquidation_price", "liq_price", "margin", "margin_used",
    "maintenance_margin", "margin_ratio",
    # sizing and portfolio limits
    "position_size", "position_size_usd", "qty", "quantity", "shares",
    "contracts", "notional", "max_position", "portfolio_pct", "kelly",
    "risk_amount", "risk_usd",
    # expected value arithmetic
    "expected_value", "ev", "ev_usd", "edge", "expectancy",
    # indicator values
    "rsi", "macd", "atr", "adx", "ema", "sma", "vwap", "bollinger",
    "stoch", "obv", "cci", "mfi",
}


@dataclass(frozen=True)
class Task:
    """A named kind of LLM work, with the mode it defaults to.

    `numeric_authority` is an explicit opt-out from the forbidden-key strip,
    for the rare task that legitimately owns a number. It is False
    everywhere by default and should stay that way.
    """
    name: str
    default_mode: str
    why: str
    numeric_authority: frozenset = field(default_factory=frozenset)


def _t(name, mode, why, owns=()):
    return Task(name, mode, why, frozenset(owns))


# ── The taxonomy ─────────────────────────────────────────────────────────────
#
# DEEP where the work is judgement: weighing evidence that points both ways,
# or committing real risk. FAST where the work is transcription: the answer
# is already in the input and the model is reformatting it.
TASKS: dict[str, Task] = {t.name: t for t in [
    # ---- reasoning required ----
    _t("contradiction_review",   DEEP, "evidence points both ways; the whole job is weighing it"),
    _t("trade_review",           DEEP, "committing or holding real risk"),
    # AUTO, not DEEP: the stated policy is that HIGH-RISK positions reason,
    # which means routine ones must not. Declaring this DEEP made AUTO
    # resolve to DEEP unconditionally and the risk context it is handed —
    # P&L, leverage, horizon — decided nothing at all.
    _t("position_management",    AUTO, "deep only when the position is genuinely at risk"),
    _t("signal_generation",      AUTO, "depends on leverage, conflict and horizon of the setup"),
    _t("signal_verification",    AUTO, "cheap when the setup is clean, deep when it is contested"),
    _t("regime_transition",      DEEP, "the expensive error is calling a turn that has not happened"),
    _t("catalyst_analysis",      DEEP, "a major catalyst reprices everything downstream"),
    _t("macro_synthesis",        DEEP, "geopolitical and macro evidence is inherently conflicting"),
    _t("cross_asset_analysis",   DEEP, "relationships between assets, not facts about one"),
    _t("strategy_classification", AUTO, "deep only when the setup matches no strategy cleanly"),
    _t("postmortem",             DEEP, "the point is finding the cause that is not obvious"),
    _t("hypothesis_generation",  DEEP, "generative by definition"),
    _t("risk_guardian",          DEEP, "portfolio-level exposure decisions"),

    # ---- transcription: the answer is already in the input ----
    _t("extraction",             FAST, "pulling stated facts out of text"),
    _t("classification",         FAST, "assigning a label from a fixed set"),
    _t("json_formatting",        FAST, "reshaping data that is already decided"),
    _t("ticker_detection",       FAST, "symbol lookup"),
    _t("triage",                 FAST, "routing a request to the right handler"),
    _t("summarization",          FAST, "compressing text that is already written"),
    _t("notification",           FAST, "wording an alert whose content is fixed"),
    _t("sentiment",              FAST, "one axis, coarse buckets"),
    _t("market_state",           FAST, "describing numbers that are already computed"),
    _t("news_tagging",           FAST, "labelling articles"),
    _t("focus_profile",          FAST, "descriptive text about a watched symbol"),
    _t("analyst_chat",           AUTO, "depends entirely on what was asked"),
]}

DEFAULT_TASK = _t("unspecified", AUTO, "no task declared")


# ── AUTO triggers ────────────────────────────────────────────────────────────
#
# Fixed thresholds over facts the caller already has. Each returns a reason
# string when it fires, so the log records WHY thinking was switched on and
# the policy can be argued with rather than guessed at.

# 10x is where a routine adverse move becomes a margin event rather than a
# drawdown, which is the point at which the reasoning is worth its latency.
HIGH_LEVERAGE = 10.0
# Two independent pieces of evidence pointing opposite ways is the smallest
# genuine contradiction; one is just a weak signal.
CONTRADICTION_FLOOR = 2
# A position already down this much is deciding whether to realise a real
# loss, not whether to open a speculative one.
HIGH_RISK_LOSS_PCT = -8.0
# A setup committing this much of the book is a portfolio decision.
LARGE_PORTFOLIO_PCT = 15.0
# Below this, the strategy classifier could not match a named strategy, so
# there is nothing deterministic to lean on.
AMBIGUOUS_MATCH = 0.5

# Horizon: a weekly position is a commitment that will not be revisited for
# a month. Worth thinking about; a 1m scalp is not, and the latency alone
# would invalidate it.
#
# 4H was in this set and had to come out. 4H is 60% of every signal this
# system produces (23,735 of ~39,350), so it fired on almost everything and
# AUTO collapsed into DEEP — every routine position check paid for
# chain-of-thought, which is the exact defect this module was built to end,
# reintroduced from the other direction. A trigger that fires on the
# majority case is not a trigger. Only genuinely multi-week commitments
# qualify.
DELIBERATE_TIMEFRAMES = {"1D", "2D", "1W"}


def _num(ctx: dict, key: str):
    v = ctx.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def auto_triggers(context: dict | None) -> list[str]:
    """Every reason this call warrants reasoning. Empty list means FAST."""
    ctx = context or {}
    fired: list[str] = []

    lev = _num(ctx, "leverage")
    if lev is not None and lev >= HIGH_LEVERAGE:
        fired.append(f"leverage {lev:g}x >= {HIGH_LEVERAGE:g}x")

    n = _num(ctx, "contradiction_count")
    if n is not None and n >= CONTRADICTION_FLOOR:
        fired.append(f"{n:g} contradicting signals")
    if ctx.get("contradictory_evidence"):
        fired.append("contradictory evidence flagged")

    pnl = _num(ctx, "pnl_pct")
    if pnl is not None and pnl <= HIGH_RISK_LOSS_PCT:
        fired.append(f"position down {abs(pnl):.1f}%")

    pct = _num(ctx, "portfolio_pct")
    if pct is not None and pct >= LARGE_PORTFOLIO_PCT:
        fired.append(f"{pct:.1f}% of the book")

    if ctx.get("regime_transition"):
        fired.append("regime transition")
    if ctx.get("major_catalyst"):
        fired.append("major catalyst")
    if ctx.get("cross_asset"):
        fired.append("cross-asset")

    m = _num(ctx, "strategy_match")
    if m is not None and m < AMBIGUOUS_MATCH:
        fired.append(f"strategy match {m:.2f} — unclassified")
    if str(ctx.get("strategy") or "").upper() == "UNCLASSIFIED":
        fired.append("strategy unclassified")

    tf = str(ctx.get("timeframe") or "").strip()
    if tf in DELIBERATE_TIMEFRAMES:
        fired.append(f"{tf} horizon")

    return fired


@dataclass(frozen=True)
class Routing:
    task: str
    mode: str            # what the caller asked for
    thinking: bool       # what was decided
    reason: str

    @property
    def resolved_mode(self) -> str:
        return DEEP if self.thinking else FAST


def route(task: str, mode: str = AUTO, context: dict | None = None) -> Routing:
    """Decide thinking on/off, deterministically, with the reason attached."""
    t = TASKS.get(task, DEFAULT_TASK)
    requested = str(mode or AUTO).upper()
    if requested not in MODES:
        logger.warning(f"[LLM] unknown mode {mode!r} for task {task!r} — using AUTO")
        requested = AUTO

    if requested == FAST:
        return Routing(task, FAST, False, "caller asked for FAST")
    if requested == DEEP:
        return Routing(task, DEEP, True, "caller asked for DEEP")

    # AUTO — fall back to the task's declared default, then let triggers
    # escalate. A task whose default is FAST can still be escalated by a
    # trigger; that is the whole point of AUTO.
    fired = auto_triggers(context)
    if fired:
        return Routing(task, AUTO, True, "; ".join(fired))
    if t.default_mode == DEEP:
        return Routing(task, AUTO, True, f"{t.name} is reasoning work: {t.why}")
    if t.default_mode == AUTO:
        return Routing(task, AUTO, False, f"no trigger fired for {t.name}")
    return Routing(task, AUTO, False, f"{t.name} is transcription: {t.why}")


# ── Keeping arithmetic away from the model ───────────────────────────────────

def strip_forbidden(task: str, parsed):
    """Remove keys the model must not be the source of.

    Returns (cleaned, removed_keys). Recurses through dicts and lists so a
    fee buried in a nested object is caught too. Non-dict input is returned
    untouched.
    """
    t = TASKS.get(task, DEFAULT_TASK)
    removed: list[str] = []

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            key = str(k).strip().lower()
            if key in FORBIDDEN_OUTPUT_KEYS and key not in t.numeric_authority:
                removed.append(key)
                continue
            out[k] = walk(v)
        return out

    cleaned = walk(parsed)
    if removed:
        logger.warning(
            f"[LLM] {task}: dropped model-supplied {sorted(set(removed))} — "
            "these are computed deterministically, not asked for"
        )
    return cleaned, removed


# ── The call ─────────────────────────────────────────────────────────────────

def call(prompt: str, *, task: str, mode: str = AUTO, context: dict | None = None,
         system: str = None, system_deep: str = None,
         max_tokens: int = None, temperature: float = 0.15,
         signal_id: str = None, symbol: str = None,
         queue_timeout: float = None, request_timeout: float = None) -> str:
    """Route, call, record. The only entry point the rest of Jarvis should use.

    `signal_id` is what later joins this call to the trade it produced, and
    is the difference between measuring whether thinking helps and assuming
    it does.

    `system_deep` is the system prompt to use when routing resolves to
    thinking — for the several call sites whose prompt says "answer directly
    without reasoning", which has to stop being said once the router decides
    reasoning is warranted. It exists so those callers do not route twice:
    routing to pick a prompt and then passing the RESULT back in as the mode
    overwrites the recorded reason with "caller asked for DEEP" and loses
    the trigger that actually fired, which is the one thing worth keeping.
    """
    from lib.lmstudio import call_lm_studio

    r = route(task, mode, context)
    if r.thinking and system_deep is not None:
        system = system_deep
    stats: dict = {}
    started = time.monotonic()
    text, error = "", None
    try:
        text = call_lm_studio(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature,
            thinking=r.thinking, queue_timeout=queue_timeout,
            request_timeout=request_timeout, stats=stats,
        )
        return text
    except Exception as e:
        error = str(e)[:400]
        raise
    finally:
        _record(r, stats, (time.monotonic() - started) * 1000.0,
                len(text or ""), error, signal_id, symbol)


def _record(r: Routing, stats: dict, latency_ms: float, chars: int,
            error: str | None, signal_id: str | None, symbol: str | None):
    """Persist one row. Never raises — telemetry must not break a trade."""
    try:
        from app.database import get_db, LlmCall
        with get_db() as db:
            db.add(LlmCall(
                task=r.task, mode_requested=r.mode, thinking=bool(r.thinking),
                reason=r.reason[:500], model=stats.get("model"),
                prompt_tokens=stats.get("prompt_tokens"),
                completion_tokens=stats.get("completion_tokens"),
                latency_ms=round(latency_ms, 1), response_chars=chars,
                ok=error is None, error=error,
                signal_id=signal_id, symbol=symbol,
            ))
            db.commit()
    except Exception as e:
        logger.debug(f"[LLM] telemetry write failed: {e}")


# ── Did it help? ─────────────────────────────────────────────────────────────

def usage_stats(days: int = 30) -> list[dict]:
    """Cost and latency per task, split by thinking on/off."""
    from sqlalchemy import text
    from app.database import engine
    sql = text("""
        SELECT task,
               thinking,
               COUNT(*)                    AS calls,
               SUM(CASE WHEN ok THEN 0 ELSE 1 END) AS failures,
               AVG(latency_ms)             AS avg_latency_ms,
               SUM(COALESCE(prompt_tokens, 0))     AS prompt_tokens,
               SUM(COALESCE(completion_tokens, 0)) AS completion_tokens
        FROM llm_calls
        WHERE created_at >= datetime('now', :window)
        GROUP BY task, thinking
        ORDER BY calls DESC
    """)
    with engine.connect() as c:
        rows = c.execute(sql, {"window": f"-{int(days)} days"}).mappings().all()
    return [
        {"task": r["task"], "thinking": bool(r["thinking"]), "calls": r["calls"],
         "failures": r["failures"],
         "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
         "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
         "avg_completion_tokens": round((r["completion_tokens"] or 0) / max(1, r["calls"]), 1)}
        for r in rows
    ]


def thinking_effectiveness(days: int = 90, min_sample: int = 20) -> list[dict]:
    """Win rate and average P&L of the trades each mode produced, per task.

    This is the whole reason the telemetry exists. `thinking=True` was the
    default for eleven of fourteen call sites on the assumption that
    reasoning helps; this measures it. A task appears here only once BOTH
    arms clear `min_sample`, because the comparison is the point and one
    arm alone cannot make it.

    Replayed outcomes are excluded: a replayed fill is frictionless and
    systematically optimistic, and it was produced by no LLM call at all.
    """
    from sqlalchemy import text
    from app.database import engine
    sql = text("""
        SELECT c.task                                        AS task,
               c.thinking                                    AS thinking,
               COUNT(*)                                      AS trades,
               AVG(CASE WHEN o.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
               AVG(o.pnl_pct)                                AS avg_pnl_pct,
               AVG(c.latency_ms)                             AS avg_latency_ms,
               AVG(COALESCE(c.completion_tokens, 0))         AS avg_tokens
        FROM llm_calls c
        JOIN trade_outcomes o ON o.signal_id = c.signal_id
        WHERE c.signal_id IS NOT NULL
          AND COALESCE(o.outcome_source, 'live') = 'live'
          AND c.created_at >= datetime('now', :window)
        GROUP BY c.task, c.thinking
    """)
    with engine.connect() as c:
        rows = c.execute(sql, {"window": f"-{int(days)} days"}).mappings().all()

    by_task: dict[str, dict] = {}
    for r in rows:
        arm = {"trades": r["trades"],
               "win_rate": round((r["win_rate"] or 0) * 100, 1),
               "avg_pnl_pct": round(r["avg_pnl_pct"] or 0, 2),
               "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
               "avg_tokens": round(r["avg_tokens"] or 0, 1)}
        by_task.setdefault(r["task"], {})["deep" if r["thinking"] else "fast"] = arm

    out = []
    for task, arms in sorted(by_task.items()):
        deep, fast = arms.get("deep"), arms.get("fast")
        if not deep or not fast:
            continue
        if deep["trades"] < min_sample or fast["trades"] < min_sample:
            continue
        out.append({
            "task": task, "deep": deep, "fast": fast,
            "win_rate_delta": round(deep["win_rate"] - fast["win_rate"], 1),
            "pnl_delta": round(deep["avg_pnl_pct"] - fast["avg_pnl_pct"], 2),
            "extra_ms": round(deep["avg_latency_ms"] - fast["avg_latency_ms"], 1),
            "extra_tokens": round(deep["avg_tokens"] - fast["avg_tokens"], 1),
        })
    return out


def coverage(days: int = 30) -> dict:
    """How much of the LLM traffic is actually going through the router.

    Calls made directly to call_lm_studio leave no row here, so a task list
    that looks thin is evidence of unmigrated call sites, not of a quiet
    system.
    """
    from sqlalchemy import text
    from app.database import engine
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN thinking THEN 1 ELSE 0 END) AS deep,
                   SUM(CASE WHEN task = 'unspecified' THEN 1 ELSE 0 END) AS unspecified
            FROM llm_calls WHERE created_at >= datetime('now', :w)
        """), {"w": f"-{int(days)} days"}).mappings().first()
    n = (rows or {}).get("n") or 0
    deep = (rows or {}).get("deep") or 0
    return {"days": days, "routed_calls": n, "thinking": deep, "non_thinking": n - deep,
            "thinking_pct": round(deep / n * 100, 1) if n else 0.0,
            "unspecified_task": (rows or {}).get("unspecified") or 0,
            "known_tasks": len(TASKS)}
