import { API_DOWN_TEXT } from './copy';
import { isUnreachable } from './swing-api';

/**
 * The words a page shows for a failed request. An unreachable API gets the standard copy (never
 * the raw code); anything else shows the server's own `detail`, which `swingApi` puts in the
 * error's message. A bare `http_<status>` code (a body with no readable detail) is spelled out.
 */
export function errorText(err: unknown): string {
  if (isUnreachable(err)) return API_DOWN_TEXT;
  const message = err instanceof Error ? err.message.trim() : '';
  const bare = /^http_(\d{3})$/.exec(message);
  if (bare !== null) return `The server answered with an error (HTTP ${bare[1]}).`;
  return message === '' ? 'Something went wrong.' : message;
}
