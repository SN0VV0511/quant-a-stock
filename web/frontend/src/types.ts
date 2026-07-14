export type TradeAction = "buy" | "sell" | string;

export interface LoginResponse {
  success: boolean;
  error?: string;
}

export interface StatusResponse {
  live_runner: boolean;
  web_server: boolean;
  strategy_mode: "weekly_close_target" | string;
  scan_running: boolean;
  scan_status: "never_run" | "completed" | "failed" | string;
  latest_scan_at: string;
  latest_scan_trade_date: string;
  next_scan_at: string;
  scan_schedule: string;
  daemon_heartbeat_at: string;
  last_log_time: string;
  now: string;
}

export interface Position {
  code: string;
  name: string;
  shares: number;
  avg_cost: number;
  current_price: number;
  value: number;
  profit: number;
  profit_pct: number;
}

export interface PortfolioResponse {
  total_value: number;
  cash: number;
  positions_value: number;
  position_ratio: number;
  position_count: number;
  pnl: number;
  pnl_pct: number;
  positions: Position[];
  updated_at: string;
}

export interface Trade {
  date?: string;
  time?: string;
  code?: string;
  name?: string;
  action?: TradeAction;
  direction?: TradeAction;
  shares?: number;
  price?: number;
  actual_price?: number;
  status?: string;
  reject_reason?: string;
  reason?: string;
  amount?: number;
  strategy?: string;
}

export interface ProfitRankItem {
  code: string;
  name: string;
  net_profit: number;
  roi: number;
  buy_amount: number;
  sell_amount: number;
  shares_traded: number;
}

export interface ProfitRankingResponse {
  ranking: ProfitRankItem[];
}

export interface TradesResponse {
  trades: Trade[];
  dates: string[];
}

export interface Candidate {
  rank: number;
  name: string;
  code: string;
  score: number;
  price: number;
  current_price: number;
  pb: number;
  market_cap: number;
  reversal: number;
  gain_5d: number;
  selected: boolean;
}

export interface CandidatesResponse {
  candidates: Candidate[];
  updated_at: string;
  trade_date: string;
  status: "never_run" | "completed" | "failed" | string;
  mode: "scheduled" | "daily_observation" | "manual_preview" | string;
  scan_running: boolean;
  input_count: number;
  eligible_count: number;
  universe: {
    mainboard_count?: number;
    realtime_quote_count?: number;
    rough_candidate_count?: number;
    history_loaded_count?: number;
  };
  prefilter_counts: Record<string, number>;
  prefilter_labels: Record<string, string>;
  filter_counts: Record<string, number>;
  filter_labels: Record<string, string>;
  selected_codes: string[];
  next_scheduled_scan_at: string;
  error: string;
  schedule: string;
}

export interface ScanTriggerResponse {
  status: "started" | "running" | "error" | string;
  message: string;
}

export interface RpsSignal {
  rank?: number;
  name?: string;
  code: string;
  rps?: number;
  avg_volume?: number;
  price?: number;
  momentum?: number;
}

export interface RpsOrder {
  action?: TradeAction;
  code?: string;
  name?: string;
  status?: string;
  reason?: string;
  message?: string;
  shares?: number;
  price?: number;
}

export interface RpsResponse {
  available?: boolean;
  status?: string;
  completed?: boolean;
  message?: string;
  date?: string;
  etf_loaded?: number;
  etf_pool_size?: number;
  industry_loaded?: number;
  industry_pool_size?: number;
  etf_signals?: RpsSignal[];
  industry_signals?: RpsSignal[];
  orders?: RpsOrder[];
  errors?: string[];
}

export interface EquityPoint {
  t: string;
  value: number;
  drawdown?: number;
}

export interface EquityResponse {
  points: EquityPoint[];
  initial: number;
}

export interface ObservationResponse {
  health?: {
    ok?: boolean;
    failures?: string[];
  };
  review?: {
    days?: number;
    total_return?: number;
    max_drawdown?: number;
    win_rate?: number;
    trade_count?: number;
  };
  acceptance?: {
    snapshot_days?: number;
    required_snapshot_days?: number;
    ready_for_qmt_dry_run?: boolean;
    failures?: string[];
  };
}

export interface BacktestSeries {
  name: string;
  metrics?: Record<string, number | null>;
  equity: Array<{ date: string; value: number }>;
}

export interface BacktestResponse {
  available?: boolean;
  generating?: boolean;
  stale?: boolean;
  status?: string;
  error?: string;
  window?: string;
  generated_at?: string;
  series?: BacktestSeries[];
}

export interface LogsResponse {
  logs: string[];
  file: string;
  total?: number;
}

export interface DashboardSnapshot {
  status: StatusResponse | null;
  portfolio: PortfolioResponse | null;
  trades: TradesResponse | null;
  candidates: CandidatesResponse | null;
  rps: RpsResponse | null;
  equity: EquityResponse | null;
  logs: LogsResponse | null;
  observation: ObservationResponse | null;
  backtest: BacktestResponse | null;
}
