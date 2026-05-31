import type {
  AlertDelivery,
  AlertRule,
  BacktestRequest,
  BacktestResponse,
  BarsResponse,
  ExecuteRequest,
  ExecuteResult,
  JournalResponse,
  Me,
  NewAlertRule,
  Order,
  PortfolioSummary,
  RegimeSnapshot,
  Signal,
  SignalDebug,
} from './types';

// All API traffic goes through Next.js rewrites (`/api/*` → backend).
// That keeps the browser CORS-free in dev and lets us swap origins via env.
const BASE = '/api/v1';

const TIER_KEY = 'atlas_dev_tier';

export function getDevTier(): string {
  if (typeof window === 'undefined') return 'free';
  return window.localStorage.getItem(TIER_KEY) || 'free';
}

export function setDevTier(tier: string): void {
  if (typeof window !== 'undefined') window.localStorage.setItem(TIER_KEY, tier);
}

// In dev-auth mode the backend reads tier/identity from these headers. In prod
// (JWT mode) you'd attach an Authorization bearer instead — same call sites.
function authHeaders(): Record<string, string> {
  return { 'X-Dev-Tier': getDevTier(), 'X-Dev-User': 'dashboard' };
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { cache: 'no-store', headers: authHeaders() });
  if (!r.ok) {
    if (r.status === 404) {
      // Caller handles "no bars yet" as a normal state.
      throw new ApiError('not_found', r.status);
    }
    throw new ApiError(`http_${r.status}`, r.status);
  }
  return r.json() as Promise<T>;
}

async function send<T>(method: 'POST' | 'PUT', path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'content-type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!r.ok) throw new ApiError(`http_${r.status}`, r.status);
  return r.json() as Promise<T>;
}

const post = <T>(path: string, body: unknown) => send<T>('POST', path, body);
const put = <T>(path: string, body: unknown) => send<T>('PUT', path, body);

async function del<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { method: 'DELETE', cache: 'no-store', headers: authHeaders() });
  if (!r.ok) throw new ApiError(`http_${r.status}`, r.status);
  return r.json() as Promise<T>;
}

export class ApiError extends Error {
  constructor(public code: string, public status: number) {
    super(code);
  }
}

export const api = {
  health: () => get<{ status: string }>('/healthz').catch(() => ({ status: 'down' })),

  me: () => get<Me>('/me'),

  regime: () => get<RegimeSnapshot>('/regime'),

  // /v1/signals/{symbol} returns Signal | null (200 either way).
  signal: (symbol: string) =>
    get<Signal | null>(`/signals/${encodeURIComponent(symbol.toUpperCase())}`),

  // /v1/signals/{symbol}/debug returns full readout incl. sub-scores even when
  // the gates kill the signal. The dashboard prefers this for the watchlist.
  debug: (symbol: string) =>
    get<SignalDebug>(`/signals/${encodeURIComponent(symbol.toUpperCase())}/debug`),

  // /v1/symbols/{symbol}/bars — OHLCV for charting. Always 200; empty bars
  // when nothing's ingested yet.
  bars: (symbol: string, resolution = '1m', limit = 300) =>
    get<BarsResponse>(
      `/symbols/${encodeURIComponent(symbol.toUpperCase())}/bars?resolution=${resolution}&limit=${limit}`
    ),

  // POST /v1/backtests — synchronous synthetic backtest.
  backtest: (req: BacktestRequest) => post<BacktestResponse>('/backtests', req),

  // GET /v1/portfolios/{id} — holdings + risk strip. `default` resolves the
  // seeded portfolio.
  portfolio: (id = 'default') => get<PortfolioSummary>(`/portfolios/${id}`),

  // GET /v1/journal — closed-trade log + attribution.
  journal: (limit = 200) => get<JournalResponse>(`/journal?limit=${limit}`),

  // Execution (paper/broker) — tier-gated server-side.
  execute: (req: ExecuteRequest) => post<ExecuteResult>('/execute', req),
  getOrders: (limit = 50) => get<{ orders: Order[] }>(`/orders?limit=${limit}`),

  // Watchlist read/update.
  getWatchlist: () => get<{ symbols: string[]; fallback?: boolean }>('/watchlist'),
  setWatchlist: (symbols: string[]) => put<{ symbols: string[] }>('/watchlist', { symbols }),

  // Alert rules CRUD + delivery history.
  getAlerts: () => get<{ rules: AlertRule[] }>('/alerts'),
  createAlert: (rule: NewAlertRule) => post<{ id: string }>('/alerts', rule),
  deleteAlert: (id: string) => del<{ deleted: string }>(`/alerts/${id}`),
  getDeliveries: (limit = 50) => get<{ deliveries: AlertDelivery[] }>(`/alerts/deliveries?limit=${limit}`),
};
