"""What happens to every table when the economy is retired.

THE RULE THIS ENCODES. A canonical epoch cutover archives the old economic
database whole and starts a fresh one. It does not migrate, convert or tidy
the old book. So every table in the source has to be assigned an explicit
fate, and **a table nobody classified is a failure, not a default.** Guessing
is how contaminated history gets carried across.

THE CLASSES

    ARCHIVE_ONLY_ECONOMIC    the money. Positions, trades, portfolios,
                             settlement ledgers, realized outcomes, and the
                             learning votes derived directly from them.
                             Preserved in the archive, never copied.

    ARCHIVE_ONLY_HISTORY     what happened. Signals, decisions, news,
                             research, market snapshots, delivery logs.
                             History, not configuration — the new book earns
                             its own evidence.

    RESET_DERIVED_LEARNING   conclusions the OLD simulator reached. Reset by
                             default; see the note below.

    COPY_OPERATOR_CONFIG     what the operator deliberately chose.

    COPY_REFERENCE           static facts about the world, not about us.

    RESET_RUNTIME_STATE      caches and ephemera. Regenerated on demand.

    GENERATED_FRESH          created new in the candidate.

    UNKNOWN_REFUSE           unclassified. Fails the dry run.

WHY DERIVED LEARNING RESETS. The legacy simulator had measured defects: the
caller's mark used as the fill, unit-blind contract sizing (26 CONTRACTS
priced as 26 BTC — a 100x error that also drove risk decisions), scale-outs
voting twice, and no holding cost. Its aggregates are conclusions reached by
a machine we just retired. Copying them forward would have the new simulator
begin by believing them. They stay in the archive, where they are honest
history of what that machine thought.

WHY HISTORICAL SIGNALS ARE NOT CONFIGURATION. A table is not configuration
merely because the application reads it. Signals, decisions, postmortems and
snapshots are all read constantly and are all history.

MIXED TABLES. `market_assets` holds identity (symbol, name, asset class,
region, the operator's focus flags) beside transient market state (price,
volume, market cap, last_updated). Copying the row wholesale would carry a
stale price into a fresh book as though it were reference data, so only the
identity columns cross and the market columns are left NULL to be
repopulated by the live feed.
"""
from __future__ import annotations

ARCHIVE_ONLY_ECONOMIC = "ARCHIVE_ONLY_ECONOMIC"
ARCHIVE_ONLY_HISTORY = "ARCHIVE_ONLY_HISTORY"
RESET_DERIVED_LEARNING = "RESET_DERIVED_LEARNING"
COPY_OPERATOR_CONFIG = "COPY_OPERATOR_CONFIG"
COPY_REFERENCE = "COPY_REFERENCE"
RESET_RUNTIME_STATE = "RESET_RUNTIME_STATE"
GENERATED_FRESH = "GENERATED_FRESH"
UNKNOWN_REFUSE = "UNKNOWN_REFUSE"

CLASSES = (ARCHIVE_ONLY_ECONOMIC, ARCHIVE_ONLY_HISTORY,
           RESET_DERIVED_LEARNING, COPY_OPERATOR_CONFIG, COPY_REFERENCE,
           RESET_RUNTIME_STATE, GENERATED_FRESH, UNKNOWN_REFUSE)

COPY_CLASSES = (COPY_OPERATOR_CONFIG, COPY_REFERENCE)


# ── The paper economy and everything voted from it ───────────────────────
_ECONOMIC = {
    "paper_positions": "the legacy paper book",
    "paper_trades": "legacy closed trades",
    "paper_portfolio": "legacy wallet; the candidate gets a fresh one",
    "trade_outcomes": "learning votes from the retired simulator",
    "paper_position_settlements": "B1 entry ledger (absent pre-migration)",
    "paper_settlement_legs": "B1/B2 settlement legs (absent pre-migration)",
    "paper_realized_outcomes": "B2A realized outcomes (absent pre-migration)",
    "virtual_execution_commitments": "committed virtual fills — economic",
    "auto_sim_portfolios": "a separate ORM economy, still economic history",
    "auto_sim_positions": "a separate ORM economy, still economic history",
    "auto_sim_trades": "a separate ORM economy, still economic history",
    "dex_portfolio": "on-chain paper economy",
    "dex_positions": "on-chain paper economy",
    "dex_trades": "on-chain paper economy",
    "execution_samples": "measured fills — evidence about the old engine",
    "portfolio_snapshots": "equity curve of the retired book",
    # MANUAL OPERATOR EVIDENCE. Real money at a real venue, which the
    # retired simulator neither produced nor corrupted — so the case for
    # carrying these forward is genuinely arguable, unlike everything else
    # in this block. They are classified ARCHIVE_ONLY_ECONOMIC anyway, in
    # the fail-safe direction: the archive preserves every row and stays
    # readable, whereas copying money-bearing rows into a fresh book is
    # the one mistake a cutover cannot undo. Their thesis links point at
    # signals that do NOT cross, so copied rows would arrive dangling.
    # Promoting them to a COPY class is an OPERATOR DECISION, recorded as
    # an open question in docs/JARVIS_HANDOFF.md rather than made here.
    "manual_trades": "operator-executed trades — external real money",
    "manual_trade_legs": "operator-executed fills",
    "manual_trade_cost_events": "operator-evidenced funding/fees/gas",
    "manual_trade_corrections": "amendment history of the above",
}

# ── Conclusions the old machine reached ──────────────────────────────────
_DERIVED = {
    "signal_accuracy": "win rates aggregated from legacy outcomes",
    "pattern_memory": "patterns learned from legacy outcomes",
    "regime_performance": "per-regime performance from legacy outcomes",
    "llm_lessons": "lessons written from legacy trade results",
    "score_champions": "a promoted variant justified by legacy evidence",
    "signal_postmortems": "post-hoc analysis of legacy trades",
    "feature_labels": "labels resolved from legacy outcomes",
    "focus_profiles": "derived per-symbol narrative, regenerable",
}

# ── What happened ────────────────────────────────────────────────────────
_HISTORY = {
    "ai_decisions": "decision log",
    "alerts": "alert log",
    "candidate_signals": "historical candidates",
    "trading_signals": "historical signals",
    "signal_evaluations": "historical evaluations",
    "feature_snapshots": "feature vectors bound to legacy signals",
    "news_items": "historical news",
    "llm_calls": "model call log",
    "threat_events": "historical threat events",
    "psychology_snapshots": "historical sentiment",
    "crypto_derivatives_snapshots": "historical derivatives state",
    "crypto_liquidations": "historical liquidations",
    "token_activity_snapshots": "historical on-chain activity",
    "congress_trades": "external filings history",
    "insider_transactions": "external filings history",
    "institutional_holdings": "external filings history",
    "ipo_filings": "external filings history",
    "processed_13f_filings": "ingestion bookkeeping",
    "processed_congress_filings": "ingestion bookkeeping",
    "kraken_trades": "historical venue prints",
    "backtest_runs": "historical backtests",
    "wallet_observations": "wallet intel history",
    "wallet_registry": "wallet intel, built from observation",
    "wallet_relationships": "wallet intel history",
    "wallet_strategy_chains": "wallet intel history",
    "wallet_trades": "wallet intel history",
    "wallet_capital_events": "wallet intel history",
    "wallet_liquidation_risk": "wallet intel history",
    "telegram_deliveries": "delivery log",
    "telegram_callbacks": "delivery log",
    "intelligence_ingestion_runs": "ingestion history",
    # Present in the CURRENT schema but not in the pre-cutover legacy
    # source, so the first cutover never had to classify them. Classified
    # now, because the next run happens against a book that HAS them and an
    # unclassified table refuses.
    "instrument_quote_samples": "observed venue quotes — market history",
    "decision_observations": "forward-evidence decision record",
    "decision_observation_outcomes": "forward-evidence decision outcomes",
}

# ── Caches and ephemera ──────────────────────────────────────────────────
_RUNTIME = {
    "api_cache": "provider response cache",
    "snapshot_cache": "cached snapshots",
    "positions_cache": "cached position view",
    "token_surge_state": "transient surge tracking",
    "intelligence_source_health": "live source health",
    "telegram_link_tokens": "ephemeral link tokens",
}

# ── What the operator chose ──────────────────────────────────────────────
_CONFIG = {
    "app_users": "the operator's account",
    "user_preferences": "trade mode, thresholds, asset classes, directions",
    "platform_configs": "provider/platform credentials and selection",
    "system_state": "live-trading enable flag and pause reason",
    "user_telegram_links": "the operator's own notification link",
}

# ── Static facts about the world ─────────────────────────────────────────
_REFERENCE = {
    "cusip_ticker_map": "CUSIP to ticker, a fact about securities",
    "market_assets": "symbol identity and the operator's focus flags",
}

CLASSIFICATION: dict[str, tuple[str, str]] = {}
for _t, _why in _ECONOMIC.items():
    CLASSIFICATION[_t] = (ARCHIVE_ONLY_ECONOMIC, _why)
for _t, _why in _DERIVED.items():
    CLASSIFICATION[_t] = (RESET_DERIVED_LEARNING, _why)
for _t, _why in _HISTORY.items():
    CLASSIFICATION[_t] = (ARCHIVE_ONLY_HISTORY, _why)
for _t, _why in _RUNTIME.items():
    CLASSIFICATION[_t] = (RESET_RUNTIME_STATE, _why)
for _t, _why in _CONFIG.items():
    CLASSIFICATION[_t] = (COPY_OPERATOR_CONFIG, _why)
for _t, _why in _REFERENCE.items():
    CLASSIFICATION[_t] = (COPY_REFERENCE, _why)


# ── The explicit column maps (P6.1) ──────────────────────────────────────
# No `SELECT *`. Every copied column is named, so a schema change upstream
# surfaces as a refusal rather than as silently carried data.
COPY_PLAN: list[dict] = [
    {
        "source_table": "app_users",
        "target_table": "app_users",
        "columns": ["id", "email", "display_name", "is_active",
                    "created_at", "updated_at"],
        "predicate": None,
        "reason": "the operator's own account identity",
    },
    {
        "source_table": "user_preferences",
        "target_table": "user_preferences",
        "columns": ["user_id", "trade_mode", "min_confidence",
                    "asset_classes", "directions", "telegram_enabled",
                    "auto_sim_enabled", "updated_at",
                    "paper_auto_trade_enabled", "live_min_score",
                    "live_min_rr", "live_min_confidence"],
        "predicate": None,
        "reason": "deliberate operator trading preferences and thresholds",
    },
    {
        "source_table": "platform_configs",
        "target_table": "platform_configs",
        "columns": ["id", "key", "label", "platform", "config_type",
                    "api_key", "api_secret", "api_url", "extra_field_1",
                    "extra_field_2", "extra_field_3", "is_active",
                    "is_default", "notes", "created_date", "updated_date",
                    "user_id"],
        "predicate": None,
        "reason": "provider selection and credentials the operator entered",
    },
    {
        "source_table": "system_state",
        "target_table": "system_state",
        "columns": ["id", "live_trading_enabled", "paused_reason",
                    "paused_at", "updated_at"],
        "predicate": None,
        "reason": "the live-trading enable flag must not silently flip",
    },
    {
        "source_table": "user_telegram_links",
        "target_table": "user_telegram_links",
        "columns": None,          # resolved from the live schema; 0 rows
        "predicate": None,
        "reason": "the operator's notification link",
    },
    {
        "source_table": "cusip_ticker_map",
        "target_table": "cusip_ticker_map",
        "columns": ["cusip", "resolved_ticker", "resolved_at"],
        "predicate": None,
        "reason": "a static fact about securities, expensive to rebuild",
    },
    {
        # MIXED TABLE — identity crosses, market state does not. A stale
        # price carried into a fresh book would look like reference data.
        "source_table": "market_assets",
        "target_table": "market_assets",
        "columns": ["id", "symbol", "name", "asset_class", "region",
                    "is_focus", "focus_note", "focus_added"],
        "excluded_columns": ["price", "change_percent", "volume",
                             "market_cap", "last_updated", "created_date",
                             "updated_date"],
        "predicate": None,
        "reason": ("symbol identity and the operator's focus list; the "
                   "transient market columns are deliberately left NULL"),
    },
]

COPY_TABLES = frozenset(e["source_table"] for e in COPY_PLAN)


def classify(table: str) -> tuple[str, str]:
    """Never guesses. An unknown table is a refusal."""
    return CLASSIFICATION.get(
        table, (UNKNOWN_REFUSE, "not classified — the dry run must refuse"))
