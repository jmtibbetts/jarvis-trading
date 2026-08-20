"""What each scheduled job is ALLOWED to do — collect, analyse, or trade.

WHY THIS EXISTS. Turning the scheduler off was the only way to stop JARVIS
trading, and it also stopped every paid data feed. Helius,
TwelveData, Massive, Tavily, FRED, EIA, CoinGecko — all driven by scheduler
jobs, all idle for days while their subscriptions were being paid for. The
evidence base stopped growing precisely during the period we most wanted to
be watching the market: the run-up to activating a fresh canonical book.

Switching the trading scheduler off says JARVIS may not ACT. It does not say
JARVIS may not LOOK. Those are different permissions and they now have
different names.

    COLLECTION   read the world and persist evidence. No economic state is
                 touched. Safe under EVIDENCE_ONLY, and the whole point of
                 paying for a data subscription.

    ANALYSIS     derive from evidence already collected — features, labels,
                 scores, postmortems, candidate generation. Writes
                 conclusions, never money. Also safe under EVIDENCE_ONLY.

    ECONOMIC     may open, mutate or settle a position, or touch a broker.
                 Forbidden unless the runtime explicitly permits economic
                 mutation.

FAIL CLOSED. A job that nobody classified is treated as ECONOMIC and does
not run in a non-economic mode. This is the same rule the cutover table
classification uses, for the same reason: the cost of wrongly withholding a
data job is a gap in a dataset, and the cost of wrongly running an economic
one is a trade nobody authorised. Those are not comparable, so the default
goes to the cheap mistake.

THIS IS NOT THE RUNTIME GUARD. `lib/runtime_mode` still refuses economic
mutation at the mutation boundary itself, and that guard is what actually
protects the book. This is scheduling policy in front of it — defence in
depth, not a replacement. A job classified COLLECTION that tries to settle a
position will still be refused where it counts.
"""
from __future__ import annotations

COLLECTION = "COLLECTION"
ANALYSIS = "ANALYSIS"
ECONOMIC = "ECONOMIC"

CAPABILITIES = (COLLECTION, ANALYSIS, ECONOMIC)


# ── Read the world; persist evidence ─────────────────────────────────────
_COLLECTION = {
    "market": "prices/OHLCV refresh (TwelveData, Massive, venues)",
    "threats": "threat + news ingestion (Tavily and news providers)",
    "insider": "SEC Form 4 insider transactions",
    "inst13f": "13F institutional holdings",
    "congress": "congressional trade disclosures",
    "senate": "senate disclosures",
    "ipo": "IPO filings pipeline",
    "crypto_derivatives": "funding, open interest, liquidations",
    "futures_curve": "futures term structure",
    "kraken_sync": "Kraken venue reference/instrument sync",
    "official_data": "macro/official releases (FRED, EIA)",
    "onchain": "on-chain protocol/token data",
    "token_surge": "token activity surges",
    "wallet_discovery": "Helius wallet discovery",
    "wallet_activity": "Helius wallet activity observation",
    "feature_snapshots": "freeze the feature vector behind a decision",
    "social_sentiment": "LunarCrush social/sentiment observations",
}

# ── Derive from what was collected; write conclusions, never money ───────
_ANALYSIS = {
    "signals": "LLM signal generation",
    "event_signals": "event-driven signal trigger",
    "candidates": "candidate funnel",
    "scanner_premarket": "pre-market opportunity scan",
    "scanner_intraday": "intraday opportunity scan",
    "scanner_crypto": "crypto opportunity scan",
    "scanner_futures": "futures opportunity scan",
    "signal_evaluation": "forward-only signal evaluation — reads cached bars",
    "feature_labels": "resolve labels for matured feature snapshots",
    "postmortem": "post-hoc analysis of closed trades",
    "wallet_scoring": "score discovered wallets",
    "wallet_lifecycle": "wallet lifecycle transitions",
    "wallet_alpha": "wallet alpha resolution",
    "brief_push": "compose and deliver the operator brief",
    "telegram": "operator chat delivery",
}

# ── May move money, or touch a broker ────────────────────────────────────
_ECONOMIC = {
    "paper_trading": "opens, manages and exits canonical paper positions",
    "execute": "submits orders to the external broker",
    "positions": "mutates external broker positions",
    "guardian": "external broker account guardian",
    "auto_simulator": "AutoSim's own economy",
    "dex_autotrade": "on-chain paper economy mutation",
}

_MAP: dict[str, str] = {}
_MAP.update({k: COLLECTION for k in _COLLECTION})
_MAP.update({k: ANALYSIS for k in _ANALYSIS})
_MAP.update({k: ECONOMIC for k in _ECONOMIC})

REASONS: dict[str, str] = {**_COLLECTION, **_ANALYSIS, **_ECONOMIC}


# ── SIDE-EFFECT CLASS ────────────────────────────────────────────────────
#
# WHY A SECOND AXIS. COLLECTION/ANALYSIS/ECONOMIC says which STAGE a job
# belongs to. It does not say what the job can DO to the world, and those
# are different questions that an audit kept conflating:
#
#   `telegram` is ANALYSIS, but it sends messages to a human.
#   `guardian` and `positions` were read as virtual, but both mutate a REAL
#     broker account -- guardian closes positions and cancels orders,
#     positions writes stop-loss, take-profit and trailing-stop exits.
#   `execute` opens real exposure, which is categorically different from
#     the two above even though all three touch the same broker.
#
# Classified from the CALL GRAPH, not from names. The distinction that
# carries the most weight is RISK_INCREASING vs RISK_REDUCING: closing
# exposure under VIRTUAL_ONLY must stay possible, because refusing to close
# a real position would trap real capital behind a training-mode flag --
# the guard becoming the cause of the loss it exists to prevent.
READ_ONLY_INTERNAL = "READ_ONLY_INTERNAL"
EXTERNAL_READ_ONLY = "EXTERNAL_READ_ONLY"
EXTERNAL_NOTIFICATION = "EXTERNAL_NOTIFICATION"
VIRTUAL_STATE_MUTATING = "VIRTUAL_STATE_MUTATING"
REAL_ACCOUNT_RISK_REDUCING = "REAL_ACCOUNT_RISK_REDUCING"
REAL_ACCOUNT_RISK_INCREASING = "REAL_ACCOUNT_RISK_INCREASING"
UNKNOWN = "UNKNOWN"

SIDE_EFFECT_CLASSES = (
    READ_ONLY_INTERNAL, EXTERNAL_READ_ONLY, EXTERNAL_NOTIFICATION,
    VIRTUAL_STATE_MUTATING, REAL_ACCOUNT_RISK_REDUCING,
    REAL_ACCOUNT_RISK_INCREASING, UNKNOWN,
)

# Jobs whose class is NOT the default for their capability group. Anything
# absent inherits from _CLASS_BY_CAPABILITY below.
_SIDE_EFFECT_OVERRIDES = {
    # Outbound to a human. Not a mutation, but not read-only either: an
    # operator woken at 3am by a test run has been affected by it.
    "telegram": EXTERNAL_NOTIFICATION,
    "brief_push": EXTERNAL_NOTIFICATION,

    # REAL Alpaca account. Verified by AST over the call graph.
    #   execute   -> submit_bracket_order  (OPENS exposure)
    #   guardian  -> close_position, cancel_open_orders_for_symbol
    #   positions -> stop / target / trailing-stop exits, close, partial close
    "execute": REAL_ACCOUNT_RISK_INCREASING,
    "guardian": REAL_ACCOUNT_RISK_REDUCING,
    "positions": REAL_ACCOUNT_RISK_REDUCING,
}

_CLASS_BY_CAPABILITY = {
    COLLECTION: EXTERNAL_READ_ONLY,
    ANALYSIS: READ_ONLY_INTERNAL,
    ECONOMIC: VIRTUAL_STATE_MUTATING,
}


def side_effect_class(job_id: str) -> str:
    """What this job can do to the world.

    UNKNOWN for anything unclassified, and UNKNOWN is never treated as
    harmless -- see `policy_for`, where it is blocked in every mode.
    """
    if job_id in _SIDE_EFFECT_OVERRIDES:
        return _SIDE_EFFECT_OVERRIDES[job_id]
    cap = REASONS.get(job_id)
    if cap is None and job_id not in REASONS:
        # capability_of raises for unclassified ids; mirror that as UNKNOWN
        # rather than guessing a stage for it.
        return UNKNOWN
    return _CLASS_BY_CAPABILITY.get(capability_of(job_id), UNKNOWN)


# Which side-effect classes each runtime mode permits.
#
# EXTERNAL_NOTIFICATION is deliberately absent from both: it is allowed only
# when its own delivery configuration is explicitly enabled, so that a mode
# switch alone can never start messaging people.
_ALLOWED_BY_RUNTIME_MODE = {
    "FULL_VIRTUAL": frozenset({
        READ_ONLY_INTERNAL, EXTERNAL_READ_ONLY, VIRTUAL_STATE_MUTATING}),
    # EVIDENCE_ONLY gathers and derives. It must NOT move the virtual book
    # either -- that is the whole distinction from FULL_VIRTUAL, and if the
    # two permitted identical job sets the mode would be decorative.
    "EVIDENCE_ONLY": frozenset({READ_ONLY_INTERNAL, EXTERNAL_READ_ONLY}),
}


def policy_for(job_id: str, *, runtime_mode: str, platform_mode: str,
               notifications_enabled: bool = False) -> dict:
    """May this job run, under this posture? Returns the decision AND why.

    Fail-closed in every direction: an unrecognised mode permits nothing, an
    unclassified job is blocked, and real risk-increasing work is refused
    unless the platform explicitly permits real money.
    """
    cls = side_effect_class(job_id)
    allowed = _ALLOWED_BY_RUNTIME_MODE.get(runtime_mode)

    if allowed is None:
        return {"job": job_id, "side_effect_class": cls, "allowed": False,
                "blocked_reason": f"unrecognised runtime mode {runtime_mode!r}"}
    if cls == UNKNOWN:
        return {"job": job_id, "side_effect_class": cls, "allowed": False,
                "blocked_reason": "unclassified job — UNKNOWN defaults to "
                                  "BLOCKED rather than being assumed safe"}
    if cls == REAL_ACCOUNT_RISK_INCREASING:
        live = platform_mode in ("LIVE_LIMITED", "LIVE_ENABLED")
        return {"job": job_id, "side_effect_class": cls, "allowed": live,
                "blocked_reason": None if live else
                f"platform mode {platform_mode} forbids increasing real "
                f"exposure"}
    if cls == REAL_ACCOUNT_RISK_REDUCING:
        # Permitted under VIRTUAL_ONLY on purpose. See the class comment:
        # refusing to close real exposure would trap real capital behind a
        # training flag. Still requires a broker to exist at all.
        return {"job": job_id, "side_effect_class": cls, "allowed": True,
                "blocked_reason": None,
                "note": "risk-reducing actions stay permitted so real "
                        "exposure can always be closed"}
    if cls == EXTERNAL_NOTIFICATION:
        return {"job": job_id, "side_effect_class": cls,
                "allowed": bool(notifications_enabled),
                "blocked_reason": None if notifications_enabled else
                "notification delivery is not explicitly enabled"}
    return {"job": job_id, "side_effect_class": cls, "allowed": cls in allowed,
            "blocked_reason": None if cls in allowed else
            f"{cls} is not permitted in {runtime_mode}"}


def policy_matrix(*, runtime_mode: str, platform_mode: str,
                  notifications_enabled: bool = False) -> list[dict]:
    """Every known job's decision, for Ops to render."""
    return [policy_for(j, runtime_mode=runtime_mode,
                       platform_mode=platform_mode,
                       notifications_enabled=notifications_enabled)
            for j in sorted(REASONS)]


def capability_of(job_id: str) -> str:
    """What this job is allowed to do. Unknown means ECONOMIC."""
    return _MAP.get(job_id, ECONOMIC)


def is_classified(job_id: str) -> bool:
    return job_id in _MAP


def allowed_capabilities(*, economic: bool) -> frozenset[str]:
    """Which capability groups may run.

    `economic=False` is the EVIDENCE_ONLY shape: look and think, do not act.
    """
    if economic:
        return frozenset(CAPABILITIES)
    return frozenset({COLLECTION, ANALYSIS})


def describe() -> dict:
    """For the API: what would run, and what would not."""
    out: dict[str, list[str]] = {c: [] for c in CAPABILITIES}
    for job_id, cap in sorted(_MAP.items()):
        out[cap].append(job_id)
    return out


def describe_flat() -> dict[str, str]:
    """job id -> capability, for callers that want the raw map."""
    return dict(_MAP)
