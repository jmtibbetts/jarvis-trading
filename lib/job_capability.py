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
