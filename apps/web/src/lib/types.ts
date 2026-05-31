// Mirrors atlas_shared/schemas.py. Kept hand-curated so the dashboard can
// evolve independently of generated bindings; small surface, low churn.

export type AssetClass = 'equity' | 'etf' | 'crypto' | 'fx' | 'future' | 'option';
export type Horizon = 'intraday' | 'swing' | 'position' | 'long-term';
export type Direction = 'long' | 'short' | 'flat';
export type Conviction = 'low' | 'medium' | 'high' | 'very-high';

export interface SubScores {
  tech: number;
  quant: number;
  fund: number;
  macro: number;
  sent: number;
  opt: number;
  liq: number;
  chain: number;
}

export interface Signal {
  id: string;
  symbol: string;
  asset_class: AssetClass;
  generated_at: string;
  horizon: Horizon;
  direction: Direction;
  composite_score: number;
  confidence_pct: number;
  conviction: Conviction;
  regime: string;
  sub_scores: SubScores;
  entry_price: number | null;
  stop_price: number | null;
  take_profit_levels: number[];
  position_size_pct: number | null;
  expected_rr: number | null;
  rationale_md: string | null;
  model_versions: Record<string, string>;
}

export interface RegimeSnapshot {
  ts?: string;
  regime: string;
  regime_confidence: number;
  regime_probabilities: Record<string, number>;
  term_spread?: number;
  vix?: number;
  vix_z?: number;
  dxy_z?: number;
  curve_slope?: number;
  cpi_yoy?: number;
}

export interface SignalDebug {
  signal: Signal | null;
  veto: { reason: string; detail: string } | null;
  macro_snapshot: RegimeSnapshot | null;
  sentiment_snapshot: { news_sent_score: number; news_sent_confidence: number; news_count: number } | null;
}

export interface OhlcBar {
  time: number; // epoch seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BarsResponse {
  symbol: string;
  resolution: string;
  bars: OhlcBar[];
}

export interface BacktestRequest {
  symbol?: string;
  strategy?: 'trend-follower' | 'atlas';
  n_bars?: number;
  seed?: number;
  initial_capital?: number;
  cost_multiplier?: number;
  cost_sweep?: boolean;
}

export interface BacktestResponse {
  symbol: string;
  strategy: string;
  metrics: Record<string, number>;
  equity_curve: Array<[number, number]>; // [epoch_seconds, equity]
  n_trades: number;
  cost_sweep: Record<string, Record<string, number>> | null;
}

export interface Holding {
  symbol: string;
  asset_class: string;
  sector: string | null;
  quantity: number;
  avg_cost: number;
  last_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  weight_pct: number;
}

export interface PortfolioSummary {
  equity: number;
  cash: number;
  invested: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  n_positions: number;
  var_95: number;
  var_95_pct: number;
  sector_exposure: Record<string, number>;
  holdings: Holding[];
}

export interface JournalEntry {
  symbol: string;
  side: string;
  strategy: string;
  opened_at: string;
  closed_at: string | null;
  entry_price: number;
  exit_price: number | null;
  quantity: number;
  realized_pnl: number | null;
  realized_return: number | null;
  r_multiple: number | null;
  max_run_up: number | null;
  max_drawdown: number | null;
  bars_held: number | null;
  exit_reason: string | null;
  fees_paid: number | null;
  source: string;
}

export interface JournalAttribution {
  n: number;
  hit_rate: number | null;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  expectancy_r: number | null;
  total_pnl: number;
  exit_reasons: Record<string, number>;
  by_symbol: Record<string, { n: number; pnl: number }>;
}

export interface JournalResponse {
  entries: JournalEntry[];
  attribution: JournalAttribution;
}

export interface AlertRule {
  id: string;
  name: string;
  metric: string;
  op: string;
  threshold: number;
  symbol: string | null;
  direction: string | null;
  channels: string[];
  cooldown_s: number;
  enabled: boolean;
}

export interface NewAlertRule {
  name: string;
  metric: string;
  op: string;
  threshold: number;
  symbol?: string | null;
  direction?: string | null;
  channels: string[];
  cooldown_s?: number;
}

export interface AlertDelivery {
  rule_id: string | null;
  symbol: string;
  channel: string;
  ok: boolean;
  detail: string | null;
  fired_at: string;
}

export interface Entitlements {
  tier: string;
  watchlist_size: number | null;
  max_alerts: number | null;
  ai_explanations_per_day: number | null;
  asset_classes: string[];
  backtest_concurrent: number;
  backtest_years: number | null;
  broker_autotrade: boolean;
  api_access: boolean;
  api_rate_per_min: number;
  channels: string[];
}

export interface Me {
  user_id: string;
  email: string | null;
  tier: string;
  entitlements: Entitlements;
}

export interface ExecuteRequest {
  symbol: string;
  intent: 'open' | 'close';
  quantity: number;
  limit_price?: number | null;
  sector?: string | null;
  signal_ref?: string | null;
  stop?: number | null;
  target?: number | null;
  targets?: number[] | null;
  allocations?: number[] | null;
  atr?: number | null;
  direction?: string;
  time_stop_at?: string | null;
}

export interface ExecuteResult {
  ok: boolean;
  order_id: string | null;
  status: string;
  fill_price: number | null;
  realized_pnl: number | null;
  detail: string;
}

export interface Order {
  id: string;
  symbol: string;
  side: string;
  intent: string;
  quantity: number;
  limit_price: number | null;
  fill_price: number | null;
  status: string;
  broker: string;
  realized_pnl: number | null;
  detail: string | null;
  signal_ref: string | null;
  created_at: string;
}
