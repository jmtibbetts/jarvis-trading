// Thin typed wrappers over the existing FastAPI /api/* surface (app/routes.py).
// Deliberately not a codegen'd client — the backend is Python/Pydantic, not
// an OpenAPI-first service, so these types are hand-mirrored from the actual
// response-building functions (_sig_dict, _threat_dict, _position_dict, ...).

export type Signal = {
  id: string;
  asset_symbol: string;
  asset_name: string | null;
  asset_class: string | null;
  direction: string;
  confidence: number | null;
  composite_score: number | null;
  timeframe: string | null;
  entry_price: number | null;
  target_price: number | null;
  stop_loss: number | null;
  status: string;
  generated_at: string;
  signal_source: string;
  paper_mode: boolean;
  paper_direction: string | null;
  rr_ratio: number | null;
  notes?: string | null;
};

export type Threat = {
  id: string;
  title: string;
  description?: string | null;
  event_type?: string | null;
  severity: string;
  country: string | null;
  region: string | null;
  latitude: number | null;
  longitude: number | null;
  source?: string | null;
  source_url?: string | null;
  status?: string | null;
  published_at: string | null;
  created_date?: string | null;
  source_kind?: string | null;
  reliability_score?: number | null;
  confirmation_status?: string | null;
  corroboration_count?: number;
  claim_confidence?: number | null;
  cluster_id?: string | null;
};

export type Position = {
  symbol: string;
  side: string;
  qty: number;
  market_value: number;
  unrealized_pl: number;
  unrealized_plpc: number;
  avg_entry_price: number;
  current_price: number;
  asset_class: string;
};

export type PositionsResponse = {
  positions: Position[];
  account: {
    equity: number;
    cash: number;
    buying_power: number;
    market_value: number;
    unrealized_pl: number;
    unrealized_plpc: number;
    day_trade_count: number;
  };
};

export type EquityPoint = {
  time: string;
  equity: number;
  cash: number;
  market_value: number;
  unrealized_pl: number;
  position_count: number;
};

export type Regime = { label: string; risk: string; spy_trend?: string; recommendation?: string; [key: string]: unknown };

export type MarketAsset = {
  symbol: string;
  name: string;
  asset_class: string;
  price: number;
  change_percent: number;
  volume: number;
};

export type NewsArticle = {
  id: string;
  title: string;
  summary: string;
  source: string;
  url?: string | null;
  category: string | null;
  sentiment: string | null;
  affected_assets?: string[];
  region?: string | null;
  published_at: string | null;
  created_date?: string | null;
  canonical_url?: string | null;
  source_kind?: string | null;
  provider?: string | null;
  ingested_at?: string | null;
  reliability_score?: number | null;
  confirmation_status?: string | null;
  corroboration_count?: number;
  corroborated_sources?: string[];
  claim_confidence?: number | null;
  is_stale?: boolean;
  entities?: Record<string, unknown>;
};

export type IntelligenceSource = {
  source: string;
  source_kind: string | null;
  provider: string | null;
  url: string | null;
  reliability_score: number | null;
  status: "healthy" | "degraded" | "failing";
  success_count: number;
  failure_count: number;
  consecutive_failures: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  last_latency_ms: number | null;
  last_article_count: number;
  updated_at: string | null;
};

export type IngestionRun = {
  id: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  source_count: number;
  failed_sources: number;
  fetched_count: number;
  fresh_count: number;
  selected_count: number;
  saved_news: number;
  saved_threats: number;
  error: string | null;
};

export type IntelligenceStatus = {
  status: "healthy" | "degraded" | "not_run";
  source_count: number;
  healthy_sources: number;
  failing_sources: number;
  recent_news: number;
  corroborated_recent: number;
  social_unconfirmed_recent: number;
  latest_run: IngestionRun | null;
  checked_at: string;
};

export type JobStatus = { status: "idle" | "running" | "ok" | "error"; last: string | null; error: string | null };
export type JobStatusMap = Record<string, JobStatus>;

export type PerformanceAnalytics = {
  period_days: number;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  trades_analyzed: number;
  by_signal_source: { signal_source: string; total: number; wins: number; losses: number; win_rate_pct: number; avg_pnl_pct: number }[];
};

export type ScannerStatus = { scanner: Record<string, JobStatus> };

export type Candle = { time: string; open: number; high: number; low: number; close: number; volume: number };

export type TfAnalysis = {
  bias?: string;
  rsi?: number;
  atr?: { pct?: number; value?: number };
  emas?: Record<string, number | null>;
  macd?: { macd?: number; signal?: number; histogram?: number; trend?: string; crossover?: string };
  bollinger_bands?: { upper?: number; mid?: number; lower?: number; position?: string };
  volume?: { surge?: boolean; dry?: boolean; surge_ratio?: number };
  price?: { last?: number };
  adx?: { value?: number; strong?: boolean };
  error?: string;
};

export type SignalAnalysis = {
  signal: Signal & Record<string, unknown>;
  analysis_generated_at: string;
  timeframes: string[];
  ta: Record<string, TfAnalysis>;
  candles: Record<string, Candle[]>;
  sources: Record<string, string | null>;
  confluence: {
    expected_bias: string;
    score: number;
    label: string;
    bullish_timeframes: string[];
    bearish_timeframes: string[];
    neutral_timeframes: string[];
    risk_flags: string[];
  };
  news: { id: string; title: string; sentiment: string | null; relevance?: string }[];
  threats: { id: string; title: string; severity: string; relevance?: string }[];
};

export type TradingPreference = {
  user_id: string;
  trade_mode: "scalp" | "longer" | "all";
  min_confidence: number;
  telegram_enabled: boolean;
  auto_sim_enabled: boolean;
  paper_auto_trade_enabled: boolean;
  live_min_score: number;
  live_min_rr: number;
  live_min_confidence: number;
};

export type PaperPosition = {
  id: string;
  symbol: string;
  direction: string;
  side: string;
  leverage: number;
  qty: number;
  entry_price: number;
  current_price: number;
  target_price: number;
  stop_loss: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  margin_used: number;
  asset_class: string;
  opened_at: string;
};

export type PaperTrade = {
  id: string;
  symbol: string;
  qty: number;
  notional: number;
  gross_pnl: number;
  fees: number;
  fee_basis: string | null;
  direction: string;
  realized_pnl: number;
  pnl_pct: number;
  close_reason: string;
  closed_at: string;
};

export type PaperSummary = {
  portfolio: {
    cash: number;
    equity: number;
    open_pnl: number;
    margin_in_use: number;
    win_rate: number;
    total_trades: number;
    starting_capital: number;
    total_return_pct: number;
  };
  positions: PaperPosition[];
  trades: PaperTrade[];
};

export type SlippageSummary = {
  count: number;
  avg_slippage_pct: number | null;
  median_slippage_pct: number | null;
  worst_slippage_pct: number | null;
  trades: { symbol: string; asset_class: string; entry_price: number; actual_fill_price: number; slippage_pct: number; fill_recorded_at: string }[];
};

export type PositionWithSignal = Position & {
  signal: {
    asset_symbol: string;
    direction: string;
    entry_price: number;
    target_price: number | null;
    stop_loss: number | null;
    confidence: number | null;
    composite_score: number | null;
    timeframe: string | null;
    rr: number | null;
    progress_pct: number | null;
    reasoning: string | null;
    key_risks: string | null;
    signal_source: string;
    _manual?: boolean;
  };
};

export type RMultipleSummary = {
  trades: { id: string; symbol: string; direction: string; entry_price: number; stop_loss: number | null; exit_price: number; qty: number; realized_pnl: number; pnl_pct: number; close_reason: string; closed_at: string; r_multiple: number }[];
  count: number;
  skipped: number;
  avg_r: number | null;
  expectancy_r: number | null;
  win_rate_pct: number | null;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  best_r: number | null;
  worst_r: number | null;
};

export type EarningsWatchlist = { at_risk_symbols: string[]; checked_at: string };

export type ThreatExposure = {
  exposure: Record<string, { id: string; title: string; severity: string; country: string | null; region: string | null }[]>;
  symbols_checked: number;
  symbols_exposed: number;
};

export type ErrorRateSummary = {
  window_minutes: number;
  total_requests: number;
  error_count: number;
  error_rate_pct: number;
  top_error_paths: { path: string; count: number }[];
  logged_since: number | null;
};

export type InsiderTransaction = {
  id: string;
  accession_number: string;
  issuer_cik: string | null;
  issuer_name: string | null;
  ticker: string | null;
  owner_cik: string | null;
  owner_name: string | null;
  owner_title: string | null;
  is_director: boolean;
  is_officer: boolean;
  is_ten_pct_owner: boolean;
  security_title: string | null;
  table: "non_derivative" | "derivative";
  transaction_date: string | null;
  transaction_code: string | null;
  transaction_label: string | null;
  acquired_disposed: "A" | "D" | null;
  shares: number | null;
  price_per_share: number | null;
  total_value: number | null;
  shares_owned_after: number | null;
  filing_url: string | null;
  filed_at: string | null;
};

export type InsiderCluster = {
  ticker: string;
  buy_count: number;
  sell_count: number;
  distinct_buyers: number;
  distinct_sellers: number;
  officer_buyers: string[];
  buy_value: number;
  sell_value: number;
  net_value: number;
  flags: string[];
};

export type InsiderClustersResponse = { window_days: number; transactions_analyzed: number; clusters: InsiderCluster[] };

export type YieldCurvePoint = {
  date: string;
  "2yr"?: number | null;
  "10yr"?: number | null;
  spread_2s10s: number | null;
  spread_3m10y: number | null;
  "2s10s_inverted": boolean;
  "3m10y_inverted": boolean;
};

export type YieldCurveSnapshot = {
  latest: YieldCurvePoint & Record<string, number | string | boolean | null>;
  trend: YieldCurvePoint[];
  fetched_at: string;
};

export type MacroReading = { date: string; value: number; compared_to: string | null; unit: string; series_id: string } | null;

export type OptionsSummary = {
  underlying: string;
  current_price: number;
  contracts_analyzed: number;
  call_count: number;
  put_count: number;
  traded_call_count: number;
  traded_put_count: number;
  put_call_ratio: number | null;
  avg_call_iv: number | null;
  avg_put_iv: number | null;
  iv_skew: number | null;
  total_call_delta: number;
  total_put_delta: number;
  total_gamma: number;
  expirations_covered: string[];
  nearest_expiration: string | null;
  expected_move: {
    strike: number; expiration: string; straddle_price: number;
    expected_move_pct: number | null; expected_move_low: number; expected_move_high: number;
  } | null;
  top_iv_contracts: { symbol: string; type: "call" | "put"; strike: number; expiration: string; iv: number }[];
  fetched_at: string;
};

export type OrderBookSnapshot = {
  exchange: "binance" | "coinbase";
  symbol: string;
  bids: [number, number][];
  asks: [number, number][];
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  spread_bps: number | null;
  bid_depth: number;
  ask_depth: number;
  imbalance: number | null;
  ts: number;
};

export type OrderBookResponse = { symbol: string; binance: OrderBookSnapshot | null; coinbase: OrderBookSnapshot | null };

export type CryptoLiquidationRow = {
  side: string | null; pos_side: "long" | "short" | null; price: number; size: number;
  notional_usd: number; liquidated_at: string;
};

export type CryptoDerivativesSnapshot = {
  symbol: string;
  price: number | null;
  funding_rate: number | null;
  open_interest_usd: number | null;
  long_short_ratio: number | null;
  oi_price_action: "long_buildup" | "short_buildup" | "short_covering" | "long_unwinding" | null;
  fetched_at: string;
  liquidations: CryptoLiquidationRow[];
  liquidations_summary: {
    count: number; long_liquidated_usd: number; short_liquidated_usd: number;
    total_liquidated_usd: number; long_liquidation_share: number | null;
  };
};

export type DarkPoolSymbol = {
  symbol: string;
  issuer_name: string | null;
  shares: number | null;
  trade_count: number | null;
  notional: number | null;
  wow_pct: number | null;
  week_start: string;
  published_at: string | null;
  reporting_delay_days: number | null;
};

export type DarkPoolTopActivity = { tier: string; week_start: string; symbols: DarkPoolSymbol[]; fetched_at: string };

export type DarkPoolVenue = { mpid: string | null; name: string | null; shares: number | null; trade_count: number | null; notional: number | null };
export type DarkPoolVenues = { symbol: string; week_start: string; venues: DarkPoolVenue[] };

export type MacroSnapshot = {
  configured: boolean;
  fetched_at: string | null;
  readings: {
    cpi: MacroReading;
    core_cpi: MacroReading;
    pce: MacroReading;
    core_pce: MacroReading;
    unemployment_rate: MacroReading;
    fed_funds_rate: MacroReading;
    nonfarm_payrolls: MacroReading;
    real_gdp: MacroReading;
    jobless_claims: MacroReading;
  } | null;
};

export type ReversalProposal = {
  direction: string;
  entry_price: number;
  stop_loss: number;
  target_price: number;
  rr_ratio: number;
  risk_per_unit: number;
  basis: string;
  ai_reasoning?: string | null;
  ai_confidence?: number | null;
  warning: string;
};

export type VerifyResult = {
  reversal_proposal?: ReversalProposal | null;
  llm_assessment?: {
    assessment: string;
    confidence?: number | null;
    reasoning?: string | null;
    key_change?: string | null;
    context_used?: Record<string, boolean>;
  };
  signal_id: string;
  verdict: "CONFIRMED" | "STALE_ENTRY" | "INVALIDATED" | "DATA_UNAVAILABLE";
  checks: { check: string; ok: boolean; detail: string }[];
  current_price: number | null;
  price_source?: string | null;
  price_asof?: string | null;
  drift_pct?: number | null;
  suggested_update?: { entry_price: number; stop_loss: number; target_price: number; basis: string } | null;
  update_applied?: boolean;
  verified_at?: string;
  note?: string;
  detail?: string;
};

export type CatalystEvent = {
  date: string; type: string; title: string;
  days_away: number | null; approximation: string | null;
  tickers?: string[]; ticker?: string | null; granularity?: string;
};

export type CatalystCalendar = { events: CatalystEvent[]; note: string };

export type WatchlistRow = {
  symbol: string; name: string | null; asset_class: string | null;
  price: number | null; change_percent: number | null; volume: number | null;
  signal: { composite_score: number | null; direction: string; id: string } | null;
  insider_flags: string[];
  insider_net_value: number | null;
  congress_90d: { purchases: number; sales: number } | null;
  institutional_holders: number | null;
  in_dark_pool_top: boolean;
};

export type EnrichedWatchlist = { rows: WatchlistRow[]; note: string };

export type AnalystAnswer = {
  question: string; answer: string; context_used: string[]; note: string;
};

export type IpoPipelineRow = {
  cik: string; company_name: string; stage: string; latest_form: string;
  latest_filed_at: string | null; first_seen_at: string | null;
  ticker: string | null; exchange: string | null;
  offer_price: number | null; shares_offered: number | null; total_offering_usd: number | null;
  is_likely_spac: boolean; cover_mentions_ipo: boolean | null;
  filing_url: string | null;
};

export type IpoPipelineResponse = {
  pipeline: IpoPipelineRow[];
  stage_counts: { filed: number; amended: number; priced: number };
  note: string;
};

export type PsychologyComponent = {
  score: number;
  detail: string;
  [k: string]: unknown;
} | null;

export type PsychologyIndex = {
  score: number | null;
  label: string | null;
  components: {
    vix: PsychologyComponent; breadth: PsychologyComponent; funding: PsychologyComponent;
    long_short: PsychologyComponent; liquidations: PsychologyComponent;
  };
  components_available: number;
  components_possible: number;
  weight_coverage?: number;
  note: string;
  rate_of_change: { delta: number; hours: number; per_day: number; direction: string } | null;
  markets?: Record<string, {
    market: string; score: number | null; label: string | null;
    components_available: number; components_possible?: number;
  }>;
  computed_at: string;
};

export type CongressDisclaimer = {
  data_type: string; amounts_are_ranges: string; reporting_delay: string;
  interpretation: string; coverage: string;
};

export type CongressTrade = {
  id: string;
  doc_id: string; member_name: string; state_district: string | null; chamber: string;
  owner: string | null; asset_name: string | null; ticker: string | null; asset_type: string | null;
  transaction_code: string; transaction_label: string;
  transaction_date: string; notification_date: string; filing_date: string | null;
  filing_delay_days: number | null;
  amount_low: number | null; amount_high: number | null; amount_text: string | null;
  pdf_url: string | null;
};

export type CongressTradesResponse = {
  trades: CongressTrade[]; count: number; filings_processed: number;
  disclaimer: CongressDisclaimer;
};

export type CongressTickerActivity = {
  ticker: string; purchases: number; sales: number; other: number;
  member_count: number; disclosure_count: number; net_direction: string;
  range_low_total: number; range_high_total: number; latest_transaction_date: string | null;
};

export type CongressActivityResponse = {
  tickers: CongressTickerActivity[]; window_days: number; note: string;
  disclaimer: CongressDisclaimer;
};

export type InstitutionalTickerRow = {
  ticker: string; issuer_name: string | null;
  holder_count: number; total_value_usd: number; total_shares: number;
  insufficient_history: boolean;
  prior_holder_count: number | null; holder_delta: number | null;
  prior_shares: number | null; share_delta: number | null; share_change_pct: number | null;
  status: string;
};

export type InstitutionalDisclaimer = {
  data_type: string; caveat: string; periods_ingested: string[]; coverage_note: string;
};

export type InstitutionalAccumulation = {
  current_period: string; prior_period: string | null;
  insufficient_history: boolean;
  tickers: InstitutionalTickerRow[];
  disclaimer: InstitutionalDisclaimer;
};

export type SqueezeScore = {
  squeeze_score: number | null;
  days_to_cover_component: number | null;
  short_change_component: number | null;
  short_interest_pct_of_float: null;
  float_note: string;
  interpretation: string;
};

export type ShortInterestRow = {
  symbol: string;
  issue_name: string | null;
  settlement_date: string;
  current_short_shares: number | null;
  previous_short_shares: number | null;
  change_shares: number | null;
  change_percent: number | null;
  avg_daily_volume: number | null;
  days_to_cover: number | null;
  market_class: string | null;
  squeeze: SqueezeScore;
  reporting_lag_days?: number | null;
};

export type SqueezeTopResponse = {
  settlement_date: string;
  reporting_lag_days: number | null;
  candidates: ShortInterestRow[];
  universe_size: number;
  qualified_count: number;
  excluded: {
    sentinel_days_to_cover: number; implausible_days_to_cover: number;
    below_min_days_to_cover: number; no_short_position: number; funds_and_spacs: number;
  };
  exchanges_included: string[];
  funds_excluded: boolean;
  fund_filter_note: string;
  fetched_at: string;
};

export type RankedOpportunity = {
  signal_id: string; symbol: string; asset_class: string; direction: string; timeframe: string;
  base_composite_score: number; opportunity_score: number;
  opportunity_breakdown: {
    base_composite_score: number; smart_money_adjustment: number; smart_money_note: string;
    historical_adjustment: number; historical_note: string;
  };
  smart_money: {
    alignment_score: number | null; net_directional_score: number | null; agreement: string; sources_available: number;
    components: { insider: unknown; options: unknown; dark_pool_activity: unknown };
  } | null;
  anomaly: { flags: { flag: string; detail: string }[]; anomaly_score: number | null; sources_evaluated: number } | null;
  crypto_context: { funding_rate: number | null; open_interest_usd: number | null; long_short_ratio: number | null } | null;
  historical: { total_trades: number; win_rate: number } | null;
};

export type LlmHealth = { ok: boolean; platform?: string; model?: string; url?: string; error?: string; status_code?: number };
export type CacheStats = {
  total_bars: number;
  symbols_cached: number;
  by_timeframe: Record<string, { symbols: number; bars: number; latest_bar_ts: string; last_updated: string }>;
  latest_bar_ts: string;
  db_size_mb: number;
};

export type PlatformConfig = {
  id: string;
  key: string;
  label: string;
  platform: string;
  config_type: string;
  api_url: string;
  has_api_key: boolean;
  has_api_secret: boolean;
  extra_field_1: string;
  extra_field_2: string;
  is_active: boolean;
  is_default: boolean;
  notes: string;
};

export type ConfigCreate = {
  label: string;
  platform: string;
  config_type?: string;
  api_key?: string;
  api_secret?: string;
  api_url?: string;
  extra_field_1?: string;
  extra_field_2?: string;
  is_active?: boolean;
  is_default?: boolean;
  notes?: string;
};

export type LearningFullSummary = {
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_pnl: number;
  avg_hold_min: number;
  best_trade: number;
  worst_trade: number;
  total_pnl_usd: number;
};

export type TradeOutcome = {
  id: string;
  symbol: string;
  asset_class: string;
  direction: string;
  timeframe: string;
  entry_price: number;
  exit_price: number;
  pnl_usd: number;
  pnl_pct: number;
  outcome: string;
  exit_reason: string;
  hold_duration_m: number | null;
  paper_mode: number;
  entered_at: string;
  exited_at: string;
};

export type SignalAccuracy = {
  id: string;
  symbol: string;
  asset_class: string;
  timeframe: string;
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_pnl_pct: number;
  avg_hold_min: number;
  best_pnl_pct: number;
  worst_pnl_pct: number;
};

export type PatternMemory = {
  id: string;
  pattern_desc: string;
  asset_class: string;
  timeframe: string;
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_pnl_pct: number;
};

export type RegimeStat = {
  id: string;
  regime: string;
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_pnl_pct: number;
  avg_confidence: number;
};

export type Lesson = {
  id: string;
  symbol: string;
  outcome: string;
  lesson: string;
  lesson_category: string;
  applied_count: number;
  created_at: string;
};

export type Decision = {
  id: string;
  source: string;
  symbol: string | null;
  action: string;
  reasoning: string;
  price: number | null;
  pnl_pct: number | null;
  score: number | null;
  created_at: string;
};

export type LearningSummary = {
  total: number;
  wins: number;
  losses: number;
  avg_pnl: number;
  [key: string]: unknown;
};

export type BacktestRun = {
  id: string;
  symbols: string[];
  timeframes: string[];
  trade_mode: string;
  start_date: string;
  end_date: string;
  status: "running" | "completed" | "failed";
  error: string | null;
  created_at: string;
  finished_at: string | null;
  result?: {
    total_signals: number;
    decided: number;
    wins: number;
    losses: number;
    win_rate_pct: number;
    starting_equity: number;
    final_equity: number;
    total_return_pct: number;
    equity_curve: [string, number][];
    max_drawdown: { max_drawdown_pct: number };
    sharpe_ratio: number | null;
    date_range_clamped: boolean;
    symbols_skipped: { symbol: string; reason: string }[];
  };
};

export type AutoSimSummary = {
  paper_only: boolean;
  summary: {
    starting_cash: number;
    equity: number;
    realized_pnl: number;
    unrealized_pnl: number;
    total_pnl: number;
    total_trades: number;
    wins: number;
    losses: number;
    win_rate: number;
    open_positions: number;
    // Costs are reported, not merely netted out. Auto Sim used to charge
    // nothing at all, so its P&L could not be compared with the paper book.
    total_fees: number;
    fees_realized: number;
    fees_reserved_open: number;
    pnl_before_costs: number;
    cost_drag_pct: number | null;
  };
  // The endpoint has always returned these; the type omitted them, so the
  // 60 open Auto Sim positions had no way to reach the UI and the tab showed
  // a summary with nothing under it.
  positions: {
    id: string;
    signal_id: string | null;
    symbol: string;
    asset_class: string | null;
    direction: string | null;
    leverage: number;
    qty: number;
    margin_used: number | null;
    notional: number | null;
    market_value: number | null;
    entry_price: number;
    current_price: number | null;
    target_price: number | null;
    stop_loss: number | null;
    unrealized_pnl: number | null;
    fees: number | null;
    fee_basis: string | null;
    entry_slippage_pct: number | null;
    opened_at: string;
  }[];
  trades: {
    id: string;
    symbol: string;
    direction: string | null;
    leverage: number;
    qty: number;
    notional: number | null;
    entry_price: number;
    exit_price: number;
    gross_pnl: number | null;
    fees: number | null;
    fee_basis: string | null;
    realized_pnl: number;
    pnl_pct: number;
    close_reason: string | null;
    opened_at: string;
    closed_at: string;
  }[];
};
export type AnalyzeResult = {
  symbol: string;
  ta: Record<string, any>;
  signal: (Record<string, any> & { error?: string }) | null;
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? detail?.error ?? `POST ${path} -> ${res.status}`);
  }
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`DELETE ${path} -> ${res.status}`);
  return res.json();
}

export const api = {
  signals: (status?: string, limit = 150) =>
    get<Signal[]>(`/signals${status ? `?status=${status}&limit=${limit}` : `?limit=${limit}`}`),
  threats: (limit = 60, filters?: { confirmation?: string; minReliability?: number }) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (filters?.confirmation) p.set("confirmation", filters.confirmation);
    if (filters?.minReliability != null) p.set("min_reliability", String(filters.minReliability));
    return get<Threat[]>(`/threats?${p.toString()}`);
  },
  positions: () => get<PositionsResponse>(`/positions`),
  equity: (hours = 24) => get<EquityPoint[]>(`/portfolio/equity?hours=${hours}`),
  regime: () => get<Regime>(`/regime`),
  jobStatus: () => get<JobStatusMap>(`/jobs/status`),
  performanceAnalytics: (days = 30) => get<PerformanceAnalytics>(`/performance/analytics?days=${days}`),
  news: (limit = 20, filters?: { confirmation?: string; minReliability?: number; stale?: boolean; category?: string }) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (filters?.confirmation) p.set("confirmation", filters.confirmation);
    if (filters?.minReliability != null) p.set("min_reliability", String(filters.minReliability));
    if (filters?.stale != null) p.set("stale", String(filters.stale));
    if (filters?.category) p.set("category", filters.category);
    return get<NewsArticle[]>(`/news?${p.toString()}`);
  },
  intelligenceSources: () => get<IntelligenceSource[]>(`/intelligence/sources`),
  intelligenceStatus: () => get<IntelligenceStatus>(`/intelligence/status`),
  marketFull: () => get<{ equities: MarketAsset[]; crypto: MarketAsset[]; count: number }>(`/market/full`),

  approveSignal: (id: string) => post<{ ok: boolean }>(`/signals/${id}/approve`),
  rejectSignal: (id: string) => post<{ ok: boolean }>(`/signals/${id}/reject`),
  executeSignal: (id: string) => post<Record<string, unknown>>(`/signals/${id}/execute`, {}),
  paperExecuteSignal: (id: string, direction = "Long") =>
    post<Record<string, unknown>>(`/signals/${id}/paper-execute?direction=${direction}`),
  deleteSignal: (id: string) => del<{ ok: boolean }>(`/signals/${id}`),
  clearExpiredSignals: () => del<{ ok: boolean; cleared?: number }>(`/signals/clear/expired`),

  runScanner: (mode: "pre_market" | "intraday" | "crypto" | "futures" | "all") =>
    post<{ status: string; mode: string }>(`/scanner/run`, { mode }),
  scannerStatus: () => get<ScannerStatus>(`/scanner/status`),

  analyze: (symbol: string, timeframes: string[], generate_signal: boolean) =>
    post<AnalyzeResult>(`/analyze`, { symbol, timeframes, generate_signal }),

  signalAnalysis: (id: string) => get<SignalAnalysis>(`/signals/${id}/analysis`),
  saveSignal: (body: Record<string, unknown>) => post<Signal>(`/signals/save`, body),
  saveSignalNotes: (id: string, notes: string) => post<{ ok: boolean }>(`/signals/${id}/notes`, { notes }),

  rMultiples: (limit = 200) => get<RMultipleSummary>(`/performance/r-multiples?limit=${limit}`),
  earningsWatchlist: () => get<EarningsWatchlist>(`/earnings/watchlist`),
  threatExposure: () => get<ThreatExposure>(`/positions/threat-exposure`),
  errorRate: (windowMinutes = 15) => get<ErrorRateSummary>(`/ops/error-rate?window_minutes=${windowMinutes}`),

  insiderActivity: (ticker?: string, days = 30, limit = 200) => {
    const p = new URLSearchParams({ days: String(days), limit: String(limit) });
    if (ticker) p.set("ticker", ticker);
    return get<InsiderTransaction[]>(`/insider/activity?${p.toString()}`);
  },
  insiderClusters: (days = 14) => get<InsiderClustersResponse>(`/insider/clusters?days=${days}`),
  yieldCurve: () => get<YieldCurveSnapshot>(`/macro/yield-curve`),
  macroFred: () => get<MacroSnapshot>(`/macro/fred`),
  darkPoolTop: (tier = "T1", limit = 25) => get<DarkPoolTopActivity>(`/darkpool/top?tier=${tier}&limit=${limit}`),
  darkPoolVenues: (symbol: string, weekStart: string) => get<DarkPoolVenues>(`/darkpool/${symbol}/venues?week_start=${weekStart}`),
  orderbook: (symbol: string) => get<OrderBookResponse>(`/orderbook/${symbol}`),
  cryptoDerivatives: (symbol: string, liquidationHours = 24) =>
    get<CryptoDerivativesSnapshot>(`/crypto/${symbol}/derivatives?liquidation_hours=${liquidationHours}`),
  opportunitiesRanked: (limit = 30) => get<RankedOpportunity[]>(`/opportunities/ranked?limit=${limit}`),
  psychology: () => get<PsychologyIndex>("/psychology"),
  ipoPipeline: (limit = 40) => get<IpoPipelineResponse>(`/ipo/pipeline?limit=${limit}`),
  catalystCalendar: () => get<CatalystCalendar>("/calendar/catalysts"),
  enrichedWatchlist: (limit = 40, assetClass?: string) =>
    get<EnrichedWatchlist>(`/watchlist/enriched?limit=${limit}${assetClass ? `&asset_class=${assetClass}` : ""}`),
  askAnalyst: (question: string) =>
    fetch("/api/analyst/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }).then((r) => {
      if (!r.ok) throw new Error(`analyst ${r.status}`);
      return r.json() as Promise<AnalystAnswer>;
    }),
  congressTrades: (limit = 50, opts: { ticker?: string; days?: number } = {}) => {
    const p = new URLSearchParams({ limit: String(limit), days: String(opts.days ?? 180) });
    if (opts.ticker) p.set("ticker", opts.ticker);
    return get<CongressTradesResponse>(`/congress/trades?${p}`);
  },
  congressByOfficial: (days = 365, limit = 50) =>
    get<{ officials: {
      member_name: string; state_district: string | null; chamber: string;
      purchases: number; sales: number; other: number; trade_count: number;
      range_low_total: number; range_high_total: number; trades: CongressTrade[];
    }[]; note: string }>(`/congress/by-official?days=${days}&limit=${limit}`),
  feeComparison: (notional = 10000, contracts = 1) =>
    get<{
      notional: number; contracts: number; region: string; cheapest: string | null;
      rows: { venue: string; round_trip_usd: number; pct_of_notional: number; note: string }[];
      note: string;
    }>(`/venue/fee-comparison?notional=${notional}&contracts=${contracts}`),
  krakenVenue: () =>
    get<{
      venue: string; paper_venue: string;
      account: { connected: boolean; granted_scopes?: string[]; missing_scopes?: string[]; reason?: string };
      fees: { taker_pct?: number; maker_pct?: number; source?: string; volume_30d?: number | null; note?: string; error?: string };
      stream: {
        connected: boolean; error: string | null; since: string | null;
        symbols: {
          symbol: string; spread_pct: number | null; spread_age: string;
          flow_imbalance: number | null; prints: number;
          buy_count: number; sell_count: number; largest_print: number | null;
        }[];
      };
    }>(`/venue/kraken`),
  focusList: () =>
    get<{
      focus: {
        symbol: string; note: string | null; added: string | null;
        price: number | null; change_percent: number | null;
        profile: {
          summary: string | null; narrative: string | null; updated_at: string | null;
          stats: Record<string, number | string | null>;
        } | null;
      }[];
      min_score: number;
      note: string;
    }>(`/watchlist/focus`),
  setFocus: (symbol: string, focus: boolean, note?: string) =>
    fetch(`/api/watchlist/focus`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, focus, note }),
    }).then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body?.detail ?? `focus ${r.status}`);
      return body as { ok: boolean; symbol: string; focus: boolean };
    }),
  watchlistAdd: (symbol: string) =>
    fetch(`/api/watchlist/add`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    }).then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body?.detail ?? `add ${r.status}`);
      return body as { ok: boolean; symbol: string; already_tracked?: boolean };
    }),
  providerStatus: () =>
    get<{ providers: { name: string; ok: boolean; detail: string }[]; checked_at: string }>(`/status/providers`),
  fxRates: () =>
    get<{ pairs: { symbol: string; pair: string; rate: number | null; rate_source: string | null; change_pct: number | null; history: { date: string; rate: number }[] }[]; as_of: string; note: string }>(`/fx/rates`),
  cryptoMarkets: () =>
    get<{ coins: { id: string; symbol: string; price: number; market_cap: number; volume_24h: number; chg_1h: number | null; chg_24h: number | null; chg_7d: number | null; ath: number; ath_chg_pct: number }[]; as_of: string; note: string }>(`/crypto/markets`),
  webNews: () =>
    get<{ items: { title: string; snippet: string | null }[]; as_of: string | null; note: string }>(`/news/web`),
  updateExecutionCriteria: (c: { live_min_score?: number; live_min_rr?: number; live_min_confidence?: number }) =>
    fetch(`/api/preferences/execution`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c),
    }).then((r) => {
      if (!r.ok) throw new Error(`criteria ${r.status}`);
      return r.json() as Promise<TradingPreference>;
    }),
  flattenTrading: (scope: "live" | "paper" | "all") =>
    fetch(`/api/trading/flatten`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope, confirm: "FLATTEN" }),
    }).then(async (r) => {
      const body = await r.json();
      if (!r.ok) throw new Error(body?.detail ?? `flatten ${r.status}`);
      return body as { ok: boolean; scope: string; live?: { orders_cancelled: number; positions_closed: number; errors: string[] }; paper?: { positions_closed: number; errors: string[] }; autosim?: { closed: number; skipped_no_price?: string[]; error?: string }; signals_rejected: number };
    }),
  autosimReset: () => post<{ ok: boolean }>(`/autosim/reset`),
  congressActivity: (limit = 20, days = 180) =>
    get<CongressActivityResponse>(`/congress/activity/top?limit=${limit}&days=${days}`),
  institutionalAccumulation: (limit = 25) =>
    get<InstitutionalAccumulation>(`/institutional/accumulation/top?limit=${limit}`),
  signalSizing: (id: string) =>
    get<{
      ok: boolean; reason?: string; leverage: number; leverage_source?: string;
      margin?: number; notional?: number; qty?: number;
      loss_at_stop?: number; loss_pct_of_margin?: number;
      gain_at_target?: number | null; gain_pct_of_margin?: number | null;
      capped_by_cash?: boolean; note?: string;
    }>(`/signals/${id}/sizing`),
  reverseSignal: (id: string, supersedeOriginal = true) =>
    fetch(`/api/signals/${id}/reverse`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ supersede_original: supersedeOriginal }),
    }).then(async (r) => {
      const body = await r.json();
      if (!r.ok) {
        // A 409 carries a structured detail naming the live successor for a
        // signal that was superseded while the page was open. Stringifying
        // it would render "[object Object]" and throw away the one field
        // the caller needs to recover, so the object rides on the Error.
        const detail = body?.detail;
        const err = new Error(
          typeof detail === "string" ? detail : detail?.message ?? `reverse ${r.status}`,
        ) as Error & { detail?: unknown; status?: number };
        err.detail = detail;
        err.status = r.status;
        throw err;
      }
      return body as { ok: boolean; new_signal_id: string; proposal: ReversalProposal; original_superseded: boolean };
    }),
  verifySignal: (id: string, applyUpdate = false, deep = false) =>
    fetch(`/api/signals/${id}/verify?apply_update=${applyUpdate}&deep=${deep}`, { method: "POST" }).then((r) => {
      if (!r.ok) throw new Error(`verify ${r.status}`);
      return r.json() as Promise<VerifyResult>;
    }),
  shortInterest: (symbol: string) => get<ShortInterestRow>(`/shortinterest/${symbol}`),
  squeezeTop: (limit = 20, minDaysToCover = 3) =>
    get<SqueezeTopResponse>(`/shortinterest/squeeze/top?limit=${limit}&min_days_to_cover=${minDaysToCover}`),
  optionsSummary: (symbol: string, dteMax = 45) => get<OptionsSummary>(`/options/${symbol}/summary?dte_max=${dteMax}`),

  tradingPreference: () => get<TradingPreference>(`/preferences/trading`),
  setTradeMode: (trade_mode: "scalp" | "longer" | "all") =>
    fetch(`/api/preferences/trading`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trade_mode }) }).then(
      (r) => r.json() as Promise<TradingPreference>,
    ),

  approveAllSignals: () => post<{ ok: boolean; approved?: number }>(`/signals/approve-all`),
  rejectAllSignals: () => post<{ ok: boolean; rejected?: number }>(`/signals/reject-all`),
  updateSetting: (id: string, body: Record<string, unknown>) =>
    fetch(`/api/settings/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(async (r) => {
      if (!r.ok) {
        // Surface the server's reason — a bare "update 422" hid a schema
        // mismatch that made saving impossible.
        let why = `${r.status}`;
        try {
          const d = await r.json();
          const detail = d?.detail;
          why = Array.isArray(detail)
            ? detail.map((x: { loc?: string[]; msg?: string }) => `${x.loc?.slice(-1)[0] ?? ""}: ${x.msg ?? ""}`).join("; ")
            : (detail ?? why);
        } catch { /* non-JSON body — keep the status */ }
        throw new Error(`update failed — ${why}`);
      }
      return r.json();
    }),
  cancelAllOrders: () => del<{ ok: boolean }>(`/alpaca/orders`),
  alpacaOrders: () => get<{ id: string; symbol: string; qty: number; side: string; status: string; type: string }[]>(`/alpaca/orders`),
  cancelOrder: (id: string) => del<{ ok: boolean }>(`/alpaca/orders/${id}`),

  closeLivePosition: (symbol: string) => post<{ ok: boolean }>(`/positions/${symbol}/close`),
  positionsWithSignals: () => get<{ positions: PositionWithSignal[]; account: PositionsResponse["account"] }>(`/positions/with-signals`),
  slippageSummary: (limit = 200) => get<SlippageSummary>(`/execution/slippage?limit=${limit}`),
  paperOpen: (body: { symbol: string; asset_class?: string; paper_direction?: string; entry_price?: number; target_price?: number; stop_loss?: number }) =>
    post<Record<string, unknown>>(`/paper/open`, body),

  paperSummary: () => get<PaperSummary>(`/paper/summary`),
  paperClose: (id: string) => post<Record<string, unknown>>(`/paper/close/${id}`),
  paperReset: () => post<{ ok: boolean }>(`/paper/reset`),
  paperRunMtm: () => post<Record<string, unknown>>(`/paper/run-mtm`),

  autoSimSummary: () => get<AutoSimSummary>(`/auto-paper/summary`),
  autoSimRun: () => post<Record<string, unknown>>(`/auto-paper/run`),

  decisions: (limit = 100) => get<Decision[]>(`/decisions?limit=${limit}`),
  clearDecisions: () => del<{ ok: boolean }>(`/decisions/clear`),
  learningSummary: (paper: "live" | "paper" | "all" = "live") => get<LearningFullSummary>(`/learning/summary?paper=${paper}`),
  learningOutcomes: (mode: "live" | "paper" | "all" = "live", limit = 200) =>
    get<TradeOutcome[]>(`/learning/outcomes?paper=${mode === "paper" ? "true" : mode === "all" ? "all" : "false"}&limit=${limit}`),
  learningAccuracy: () => get<SignalAccuracy[]>(`/learning/accuracy`),
  learningPatterns: () => get<PatternMemory[]>(`/learning/patterns`),
  learningRegimes: () => get<RegimeStat[]>(`/learning/regimes`),
  learningLessons: (limit = 50) => get<Lesson[]>(`/learning/lessons?limit=${limit}`),
  learningBackfillPaper: () => post<{ ok: boolean; imported?: number }>(`/learning/backfill-paper`),

  backtestRun: (body: { symbols: string[]; start_date: string; end_date: string; timeframes?: string[]; trade_mode?: string }) =>
    post<{ run_id: string; status: string }>(`/backtest/run`, body),
  backtestGet: (runId: string) => get<BacktestRun>(`/backtest/${runId}`),
  backtestList: () => get<{ runs: Omit<BacktestRun, "timeframes" | "error" | "result">[] }>(`/backtest`),

  jobReset: (name: string) => post<{ ok: boolean }>(`/jobs/${name}/reset`),
  jobTrigger: (name: string) => post<{ ok: boolean; already_running?: boolean; detail?: string }>(`/jobs/${name}/trigger`),

  llmHealth: () => get<LlmHealth>(`/llm/health`),
  cacheStats: () => get<CacheStats>(`/cache/stats`),
  cacheBackfill: () => post<{ ok: boolean; message: string }>(`/cache/backfill`),

  telegramDetectChat: (body: { config_id?: string; bot_token?: string; chat_id?: string }) =>
    post<{ ok: boolean; chat_id: string; chat_name: string }>(`/settings/telegram/detect-chat`, body),
  telegramTest: (body: { config_id?: string; bot_token?: string; chat_id?: string }) =>
    post<{ ok: boolean; bot_name: string; bot_username: string; chat_id: string }>(`/settings/telegram/test`, body),

  settingsList: () => get<PlatformConfig[]>(`/settings`),
  settingsCreate: (body: ConfigCreate) => post<PlatformConfig>(`/settings`, body),
  settingsUpdate: (id: string, body: Partial<ConfigCreate>) =>
    fetch(`/api/settings/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(
      (r) => r.json() as Promise<PlatformConfig>,
    ),
  settingsDelete: (id: string) => del<{ ok: boolean }>(`/settings/${id}`),
  settingsSetDefault: (id: string) => post<{ ok: boolean }>(`/settings/${id}/set-default`),
};
