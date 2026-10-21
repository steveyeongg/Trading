/**
 * TypeScript mirrors of the swing API's JSON: every response of `/api/v1/*` that the swing
 * dashboard reads, and every request body it sends.
 *
 * SOURCE OF TRUTH: the Pydantic models in
 * `apps/signal-service/src/signal_service/swing_routes.py` (`Verdict` is in
 * `swing_track_record.py`; `SwingSettings` is in `apps/swing-engine/src/swing_engine/settings.py`).
 * Each interface here keeps its Python model's name, so the two can be compared line by line.
 * The models forbid extra keys, so these shapes are exact: names, `| null` and the literal unions
 * are copied, not interpreted. **When a model there changes, update this file in the same commit.**
 *
 * Conventions on the wire:
 * - `IsoDate` is "YYYY-MM-DD" (a calendar date, never shifted through a timezone).
 * - `IsoDateTime` is an ISO-8601 timestamp with an offset.
 * - NaN and infinity always arrive as `null`.
 * - Errors are `{ detail: string }`, or FastAPI's 422 shape (see `ErrorBody`).
 */

export type IsoDate = string;
export type IsoDateTime = string;

/** Whether a setup family has passed testing (`swing_track_record.Verdict`). */
export type Verdict = 'live' | 'research' | 'not_tested';

/** `positions.ActionKind`: what to do with an open position tonight. */
export type ActionKind = 'hold' | 'raise_stop' | 'stop_hit' | 'max_hold' | 'no_data';

export type RegimeState = 'uptrend' | 'mixed' | 'downtrend';

/** The latest job run, as Today reports it (`"never"` when no run is stored). */
export type LastScanStatus = 'ok' | 'failed' | 'running' | 'never';

/** Why Today has no ideas to show (`null` when it has some). */
export type EmptyReason = 'first_run' | 'downtrend' | 'no_candidates' | 'last_scan_failed';

/** The `status` filter of `GET /v1/positions`. */
export type PositionFilter = 'open' | 'closed';

// --- errors ---------------------------------------------------------------------------------

/** One entry of FastAPI's 422 `detail` array. */
export interface ValidationDetail {
  type: string;
  loc: (string | number)[];
  msg: string;
  input?: unknown;
}

/** Every non-2xx body: a string `detail`, or the list of validation errors on a 422. */
export interface ErrorBody {
  detail: string | ValidationDetail[];
}

/** `DetailOut`: the `{detail}` body of the documented 404, 409 and 503 answers. */
export interface DetailOut {
  detail: string;
}

// --- shared ---------------------------------------------------------------------------------

export interface ProgressOut {
  done: number;
  total: number;
}

export interface PointOut {
  time: IsoDate;
  value: number;
}

/** What the committed validation says about an idea's setup family. */
export interface IdeaTrackRecord {
  verdict: Verdict;
  /** "Live", or "Research only: <first failure, verbatim>" (for `not_tested` that failure reads
   * "not yet tested"), or plain "Research only" when no failure is listed. */
  verdict_text: string;
  win_rate: number | null;
  expectancy_r: number | null;
  /** n: every triggered idea. */
  trades: number | null;
  /** n_distinct: trades entered while no earlier trade on the symbol was open. */
  trades_distinct: number | null;
  source: 'oos' | 'insample' | null;
}

export interface IdeaOut {
  /** The stored idea's row id (for "I bought this"); null when it isn't found. */
  id: string | null;
  symbol: string;
  name: string | null;
  setup: string;
  setup_label: string;
  version: string;
  buy_above: number | null;
  stop: number | null;
  /** buy_above + 2 * (buy_above - stop). */
  target_2r: number | null;
  shares: number;
  risk_dollars: number | null;
  position_value: number | null;
  /** Relative-strength percentile among the eligible stocks, 0 to 100 (99 = strongest). */
  rs_pct: number | null;
  rank: number;
  /** The scan said Live and the family's verdict is Live now. */
  live: boolean;
  watch_only: boolean;
  regime_warning: boolean;
  overlaps: string[];
  /** The symbol is an open position. */
  already_held: boolean;
  /** Narration stored with the scan; null for older payloads. */
  why: string | null;
  cancels: string | null;
  track_record: IdeaTrackRecord;
}

// --- GET /v1/today --------------------------------------------------------------------------

export interface FreshnessOut {
  /** The latest scan's session. */
  data_as_of: IsoDate | null;
  /** The session the ideas are for (the scan's target session). */
  ideas_for: IsoDate | null;
  next_scan_at: IsoDateTime | null;
  last_scan_status: LastScanStatus;
  failed_step: string | null;
  failed_reason: string | null;
}

export interface RegimeOut {
  state: RegimeState;
  factor: number | null;
  /** Percent (0 to 100, not a fraction) of eligible stocks closing above their 50-day average. */
  breadth: number | null;
  /** The plain-English label, e.g. "UPTREND, new trades OK". */
  label: string;
}

export interface ActionOut {
  position_id: string;
  symbol: string;
  kind: ActionKind;
  stop: number | null;
  text: string;
}

export interface CapacityOut {
  open_positions: number;
  max_positions: number;
  /** Sum over open positions of shares x max(0, entry - current stop). */
  open_risk_dollars: number;
  /** equity x max_total_risk_pct. */
  max_risk_dollars: number;
}

/** The latest scan was saved from incomplete bars (`PartialDataOut`). */
export interface PartialDataOut {
  /** Stocks that traded the session before but have no bar on the scan's session. */
  missing: number;
  /** Active common stocks with a bar on the session before. */
  expected: number;
}

export interface TodayOut {
  freshness: FreshnessOut;
  regime: RegimeOut | null;
  /** Live ideas (not watch-only). */
  ideas: IdeaOut[];
  /** Ideas of setups that have not passed testing (not watch-only). */
  unproven: IdeaOut[];
  watch_only: IdeaOut[];
  positions: ActionOut[];
  capacity: CapacityOut;
  empty_reason: EmptyReason | null;
  /** The first-time download's progress, only while that job is running. */
  backfill: ProgressOut | null;
  /** Set when the latest scan had prices for under 95% of the stocks; null when complete. */
  partial_data: PartialDataOut | null;
  /** Some setup family's verdict is Live now. False is exactly when "no setup has passed testing"
   * is true; it does not depend on tonight's ideas. */
  any_live_setup: boolean;
}

// --- GET /v1/stocks/{symbol} ----------------------------------------------------------------

export interface BarOut {
  time: IsoDate;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number;
  volume: number | null;
}

export interface LinesOut {
  sma50: PointOut[];
  sma200: PointOut[];
  ema20: PointOut[];
}

export interface CheckOut {
  rule: string;
  passed: boolean;
  value: string;
  threshold: string;
}

export interface LevelsOut {
  trigger: number | null;
  stop: number | null;
  target_2r: number | null;
  trail: number | null;
}

export interface StockOut {
  symbol: string;
  name: string | null;
  /** The session of the scan data the page was built from. */
  session: IsoDate;
  bars: BarOut[];
  lines: LinesOut;
  checklist: CheckOut[];
  /** The latest scan's idea for this stock (non-watch-only first, best rank). */
  idea: IdeaOut | null;
  why: string | null;
  cancels: string | null;
  levels: LevelsOut | null;
}

// --- GET /v1/track-record -------------------------------------------------------------------

export interface StatsOut {
  /** Every triggered idea. */
  n: number;
  /** Trades entered while no earlier trade on the symbol was open. */
  n_distinct: number | null;
  win_rate: number | null;
  avg_win_r: number | null;
  avg_loss_r: number | null;
  expectancy_r: number | null;
  /** "inf" arrives as null. */
  profit_factor: number | null;
  avg_hold: number | null;
  /** Only on the live and reconstructed records. */
  open: number | null;
}

export interface VersionOut {
  version: string;
  run_index: number | null;
  freeze_date: IsoDate | null;
  passed: boolean | null;
}

/** Random entries with the same exits (the baseline), in R. */
export interface BaselineOut {
  median: number | null;
  p5: number | null;
  p95: number | null;
}

export interface EquityOut {
  /** Account value in dollars; [] when the report has no series. */
  strategy: PointOut[];
  /** SPY on the same starting account; [] when the report has no series. */
  spy: PointOut[];
}

export interface PortfolioStatsOut {
  cagr: number | null;
  sharpe: number | null;
  max_dd: number | null;
  exposure: number | null;
  trades: number | null;
}

export interface PortfolioOut {
  strategy: PortfolioStatsOut | null;
  spy: PortfolioStatsOut | null;
  combined: PortfolioStatsOut | null;
  combined_note: string | null;
}

export interface FamilyOut {
  family: string;
  label: string;
  verdict: Verdict;
  verdict_text: string;
  /** Every failed condition of a `research` verdict, verbatim and in order (the first is also in
   * `verdict_text`). Empty for `live` and for `not_tested`. */
  failures: string[];
  versions: VersionOut[];
  insample: StatsOut | null;
  oos: StatsOut | null;
  oos_distinct: StatsOut | null;
  live: StatsOut;
  reconstructed: StatsOut;
  baseline: BaselineOut | null;
  equity: EquityOut;
  portfolio: PortfolioOut;
  warnings: string[];
}

export interface TrackRecordOut {
  /** In order: breakout, then pullback. */
  families: FamilyOut[];
}

// --- positions ------------------------------------------------------------------------------

export interface PositionOut {
  id: string;
  symbol: string;
  name: string | null;
  opened_on: IsoDate;
  entry_price: number;
  shares: number;
  initial_stop: number;
  current_stop: number | null;
  /** "open" or "closed" (a free-text column on the server, so not a union here). */
  status: string;
  /** Open positions only, from the cached panel; null when it has none. */
  last_close: number | null;
  /** (last_close - entry) / (entry - initial_stop); open positions only. */
  r_now: number | null;
  /** An `ActionKind` value when set (a free-text column on the server). */
  last_action: string | null;
  last_action_text: string | null;
  last_action_on: IsoDate | null;
  closed_on: IsoDate | null;
  exit_price: number | null;
  exit_reason: string | null;
  r_multiple: number | null;
  /** Sessions with a bar, the open day included (to closed_on when closed). */
  held_sessions: number | null;
  idea_id: string | null;
  notes: string | null;
}

/** `PositionIn`: `POST /v1/positions` (201). `initial_stop` defaults to the idea's stop. */
export interface PositionIn {
  symbol: string;
  opened_on: IsoDate;
  entry_price: number;
  shares: number;
  initial_stop?: number | null;
  idea_id?: string | null;
  notes?: string | null;
}

/** `PositionPatch`: `PATCH /v1/positions/{id}`. Leave a field out to keep it; null is refused for
 * `current_stop` and `shares`. */
export interface PositionPatch {
  current_stop?: number;
  shares?: number;
  notes?: string | null;
}

/** `CloseIn`: `POST /v1/positions/{id}/close`. */
export interface CloseIn {
  closed_on: IsoDate;
  exit_price: number;
  exit_reason?: string | null;
}

// --- settings -------------------------------------------------------------------------------

/** `swing_engine.settings.SwingSettings`, with the server's bounds:
 * equity > 0; risk_pct in (0, 0.05]; max_position_pct in (0, 1]; max_positions 1 to 50;
 * max_total_risk_pct in (0, 0.5]; min_price >= 1; min_dollar_volume >= 1,000,000. */
export interface SwingSettings {
  equity: number;
  risk_pct: number;
  max_position_pct: number;
  max_positions: number;
  max_total_risk_pct: number;
  min_price: number;
  min_dollar_volume: number;
  telegram_digest: boolean;
}

/** `SettingsIn`: `PUT /v1/settings`. Fields left out keep their stored values; null is a 422. */
export type SettingsIn = Partial<SwingSettings>;

export interface TelegramTestOut {
  ok: boolean;
  detail: string;
}

// --- scan and status ------------------------------------------------------------------------

/** `POST /v1/scan/run` (202). `job_id` is null only while `starting` is true: the job is still
 * recording its run, so poll `GET /v1/system/status`. */
export interface ScanRunOut {
  job_id: string | null;
  starting: boolean;
}

/** One step of a job run's log (`steps` is `dict[str, Any]` on the server). */
export interface StepInfo {
  status: string;
  error?: string;
  reason?: string;
  /** The bars step reports progress as done/total. */
  done?: number;
  total?: number;
  [key: string]: unknown;
}

export interface LastScanOut {
  session: IsoDate | null;
  kind: string;
  started_at: IsoDateTime | null;
  /** The job's finished_at; null while it runs. */
  generated_at: IsoDateTime | null;
  status: string;
  steps: Record<string, StepInfo>;
  error: string | null;
}

export interface StatusOut {
  /** The latest scan's session. */
  data_as_of: IsoDate | null;
  last_scan: LastScanOut | null;
  next_scan_at: IsoDateTime | null;
  backfill: ProgressOut | null;
  /** Whether the keys are set; their values are never returned. */
  alpaca_configured: boolean;
  telegram_configured: boolean;
  auto_execution: boolean;
  scheduler_running: boolean;
  disabled_reason: string | null;
}
