export type TradeAction = "buy" | "sell" | string;

export interface LoginResponse {
  success: boolean;
  error?: string;
}

export interface StatusResponse {
  live_runner: boolean;
  web_server: boolean;
  watch_thread: boolean;
  scan_thread: boolean;
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
}

export interface TradesResponse {
  trades: Trade[];
}

export interface Candidate {
  rank: number;
  name: string;
  code: string;
  momentum: number;
  score: number;
  current_price: number;
}

export interface CandidatesResponse {
  candidates: Candidate[];
  updated_at: string;
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
