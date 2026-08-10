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
  severity: string;
  country: string | null;
  region: string | null;
  latitude: number | null;
  longitude: number | null;
  published_at: string | null;
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
  category: string | null;
  sentiment: string | null;
  published_at: string | null;
};

export type JobStatus = { status: "idle" | "running" | "ok" | "error"; last: string | null; error: string | null };
export type JobStatusMap = Record<string, JobStatus>;

export type PerformanceAnalytics = {
  period_days: number;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  by_source?: Record<string, { count: number; win_rate_pct: number; avg_pnl_pct: number }>;
};

export type ScannerStatus = { scanner: Record<string, JobStatus> };

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
  threats: (limit = 60) => get<Threat[]>(`/threats?limit=${limit}`),
  positions: () => get<PositionsResponse>(`/positions`),
  equity: (hours = 24) => get<EquityPoint[]>(`/portfolio/equity?hours=${hours}`),
  regime: () => get<Regime>(`/regime`),
  jobStatus: () => get<JobStatusMap>(`/jobs/status`),
  performanceAnalytics: (days = 30) => get<PerformanceAnalytics>(`/performance/analytics?days=${days}`),
  news: (limit = 20) => get<NewsArticle[]>(`/news?limit=${limit}`),
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

  closeLivePosition: (symbol: string) => post<{ ok: boolean }>(`/positions/${symbol}/close`),

  paperSummary: () => get<PaperSummary>(`/paper/summary`),
  paperClose: (id: string) => post<Record<string, unknown>>(`/paper/close/${id}`),
  paperReset: () => post<{ ok: boolean }>(`/paper/reset`),
  paperRunMtm: () => post<Record<string, unknown>>(`/paper/run-mtm`),

  autoSimSummary: () => get<AutoSimSummary>(`/auto-paper/summary`),
  autoSimRun: () => post<Record<string, unknown>>(`/auto-paper/run`),
};
