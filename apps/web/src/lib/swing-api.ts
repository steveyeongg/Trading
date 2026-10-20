import { ApiError, BASE } from './api';
import type {
  CloseIn,
  PositionFilter,
  PositionIn,
  PositionOut,
  PositionPatch,
  ScanRunOut,
  SettingsIn,
  StatusOut,
  StockOut,
  SwingSettings,
  TelegramTestOut,
  TodayOut,
  TrackRecordOut,
} from './swing-types';

// Client for the swing engine's routes (apps/signal-service/.../swing_routes.py). Same `/api/v1`
// base as the legacy client; the swing tables are single-user, so no dev-auth headers are sent.
//
// Errors are always an `ApiError`:
// - Network failure: a fetch rejection, or the Next rewrite proxy's own 500/502/504 with a
//   non-JSON body (what it answers when the API is down), is `ApiError('unreachable', 0)`.
//   Use `isUnreachable(err)` to show `ApiDown`.
// - Anything else: the message is the server's `detail` string, or the first message of a 422's
//   `detail` array (so a page can show it as is); `status` is the HTTP status. A body without a
//   readable detail falls back to the code `http_<status>`.

/** The `ApiError.code` of a request that never reached the API. */
export const UNREACHABLE = 'unreachable';

// What the Next dev proxy answers (a plain-text body) when the API behind it is down.
const PROXY_FAILURES = new Set([500, 502, 504]);

/** True when `err` is the "can't reach the API" error (render `ApiDown` for it). */
export function isUnreachable(err: unknown): boolean {
  return err instanceof ApiError && err.code === UNREACHABLE && err.status === 0;
}

// Pydantic prefixes the messages of validators that raise ValueError.
const VALUE_ERROR_PREFIX = /^value error,\s*/i;

function detailMessage(body: unknown): string | null {
  if (typeof body !== 'object' || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail.trim() ? detail : null;
  if (Array.isArray(detail)) {
    for (const item of detail) {
      const msg = (item as { msg?: unknown } | null)?.msg;
      if (typeof msg === 'string' && msg.trim()) return msg.replace(VALUE_ERROR_PREFIX, '');
    }
  }
  return null;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  let text: string;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      cache: 'no-store',
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    text = await response.text();
  } catch {
    throw new ApiError(UNREACHABLE, 0);
  }

  let parsed: unknown;
  let isJson = true;
  try {
    parsed = JSON.parse(text);
  } catch {
    isJson = false;
  }

  if (!response.ok) {
    if (!isJson && PROXY_FAILURES.has(response.status)) throw new ApiError(UNREACHABLE, 0);
    throw new ApiError(detailMessage(parsed) ?? `http_${response.status}`, response.status);
  }
  // A 2xx that isn't JSON came from something other than the API.
  if (!isJson) throw new ApiError(UNREACHABLE, 0);
  return parsed as T;
}

const get = <T>(path: string) => request<T>('GET', path);
const send = <T>(method: 'POST' | 'PUT' | 'PATCH', path: string, body?: unknown) =>
  request<T>(method, path, body);

const segment = (value: string) => encodeURIComponent(value.trim());

export const swingApi = {
  // GET /v1/today: the latest scan's ideas, position actions, capacity and empty-state reason.
  today: () => get<TodayOut>('/today'),

  // GET /v1/stocks/{symbol}: bars, lines, checklist, idea and levels. 404 for an unknown symbol.
  stock: (symbol: string) =>
    get<StockOut>(`/stocks/${encodeURIComponent(symbol.trim().toUpperCase())}`),

  // GET /v1/track-record: one record per setup family.
  trackRecord: () => get<TrackRecordOut>('/track-record'),

  // GET /v1/positions, newest first; `status` limits it to open or closed ones.
  positions: (status?: PositionFilter) =>
    get<PositionOut[]>(status ? `/positions?status=${status}` : '/positions'),

  // POST /v1/positions (201): 409 when the symbol already has an open position.
  createPosition: (body: PositionIn) => send<PositionOut>('POST', '/positions', body),

  // PATCH /v1/positions/{id}: change the stop, shares or notes.
  updatePosition: (id: string, body: PositionPatch) =>
    send<PositionOut>('PATCH', `/positions/${segment(id)}`, body),

  // POST /v1/positions/{id}/close: 409 when it is already closed.
  closePosition: (id: string, body: CloseIn) =>
    send<PositionOut>('POST', `/positions/${segment(id)}/close`, body),

  // GET and PUT /v1/settings. Fields left out of a save keep their stored values.
  settings: () => get<SwingSettings>('/settings'),
  saveSettings: (body: SettingsIn) => send<SwingSettings>('PUT', '/settings', body),

  // POST /v1/settings/telegram-test: sends one fixed test message; `{ok, detail}`.
  telegramTest: () => send<TelegramTestOut>('POST', '/settings/telegram-test'),

  // POST /v1/scan/run (202): `{job_id, starting}`. `job_id` is null while `starting` is true; poll
  // `status()` then. 409 ("a scan is already running") and 503 (can't start) carry the server's
  // reason as the ApiError message.
  runScan: () => send<ScanRunOut>('POST', '/scan/run'),

  // GET /v1/system/status: last job and its steps, next scan, backfill, configuration.
  status: () => get<StatusOut>('/system/status'),
};
