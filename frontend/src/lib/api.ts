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
  };
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

  tradingPreference: () => get<TradingPreference>(`/preferences/trading`),
  setTradeMode: (trade_mode: "scalp" | "longer" | "all") =>
    fetch(`/api/preferences/trading`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trade_mode }) }).then(
      (r) => r.json() as Promise<TradingPreference>,
    ),

  approveAllSignals: () => post<{ ok: boolean; approved?: number }>(`/signals/approve-all`),
  rejectAllSignals: () => post<{ ok: boolean; rejected?: number }>(`/signals/reject-all`),
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
