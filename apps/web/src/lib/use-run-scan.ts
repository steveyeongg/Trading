import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { errorText } from './error-text';
import { swingApi } from './swing-api';
import type { StatusOut } from './swing-types';

// The "Run scan now" behaviour that Today (RegimeBanner) and Settings (SystemStatusPanel) share.

const SCAN_STARTED_TEXT = 'Scan started.';
const SCAN_STARTING_TEXT = 'Scan is starting…';
const SCAN_RUNNING_TEXT = 'Scan running…';

// How long, after a scan was asked for, the page checks for it every few seconds.
const POLL_MS = 3_000;
const GIVE_UP_MS = 45_000;
// The server stamps a run's start with its own clock; allow for the browser's being a little off.
const CLOCK_SKEW_MS = 5_000;

/**
 * Whether the scan asked for at `clickedAt` (a `Date.now()` value) has finished: the latest job
 * run started after the click and is no longer running. Today's own fields can't say so when a
 * quick scan of the same session ends between two looks, but the run's start time can.
 */
export function scanSettled(status: StatusOut, clickedAt: number): boolean {
  const last = status.last_scan;
  if (last === null || last.status === 'running' || last.started_at === null) return false;
  const started = Date.parse(last.started_at);
  return Number.isFinite(started) && started >= clickedAt - CLOCK_SKEW_MS;
}

/** What the button's page says next to it: the scan is starting or started, or why it cannot. */
export interface RunScanNotice {
  kind: 'starting' | 'started' | 'error';
  text: string;
}

export interface RunScan {
  /** The note to show beside the button (a `role="status"` line), or null. */
  message: RunScanNotice | null;
  /** The button is off: a scan is running, or one was just asked for. */
  busy: boolean;
  /** The button's click handler. */
  start: () => void;
}

/**
 * The state behind a "Run scan now" button. `running` is the page's own view of whether a scan is
 * running now (Today reads it from `/v1/today`, Settings from `/v1/system/status`).
 *
 * The button is off while a scan runs or the request is in flight; it never claims the scan's
 * results, only that a scan was asked for. After a 202 the job may not be recorded yet, and a
 * quick one can finish between two looks at the page, so for a while the hook looks again every
 * few seconds, at Today and at the latest job run. Once that run started after the click and has
 * ended, the scan is over: the note and the lock go, and Today and the status panel refetch.
 */
export function useRunScan(running: boolean): RunScan {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<RunScanNotice | null>(null);
  const kind = notice?.kind;

  const scan = useMutation({
    mutationFn: () => swingApi.runScan(),
    onSuccess: (out) =>
      setNotice(
        out.starting
          ? { kind: 'starting', text: SCAN_STARTING_TEXT }
          : { kind: 'started', text: SCAN_STARTED_TEXT },
      ),
    // 409 (a scan is already running) and 503 (it cannot start) carry the server's reason.
    onError: (err) => setNotice({ kind: 'error', text: errorText(err) }),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['today'] }),
        queryClient.invalidateQueries({ queryKey: ['status'] }),
      ]),
  });

  // "Starting" has done its job once the scan shows as running; when the run ends, the notes go.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (running && kind === 'starting') setNotice({ kind: 'started', text: SCAN_STARTED_TEXT });
    if (wasRunning.current && !running) setNotice(null);
    wasRunning.current = running;
  }, [running, kind]);

  const clickedAt = useRef(0);
  useEffect(() => {
    if (kind !== 'starting' && kind !== 'started') return;
    let alive = true;
    const look = () => {
      void queryClient.invalidateQueries({ queryKey: ['today'] });
      // The latest job run is read through the cache (not around it), so the status panel on
      // Settings shows what this look saw.
      queryClient
        .fetchQuery({
          queryKey: ['status'],
          queryFn: () => swingApi.status(),
          staleTime: 0,
          retry: false,
        })
        .then((status) => {
          if (alive && scanSettled(status, clickedAt.current)) {
            setNotice(null);
            // The scan is over. Today's and the status panel's last refetch may predate its end,
            // so refetch both now instead of showing the old scan until the next timed refetch.
            void queryClient.invalidateQueries({ queryKey: ['today'] });
            void queryClient.invalidateQueries({ queryKey: ['status'] });
          }
        })
        .catch(() => undefined); // the next look tries again
    };
    look();
    const poll = window.setInterval(look, POLL_MS);
    // Give up GIVE_UP_MS after the click, however often this effect runs again (starting -> started).
    const left = Math.max(0, GIVE_UP_MS - (Date.now() - clickedAt.current));
    const giveUp = window.setTimeout(() => setNotice(null), left);
    return () => {
      alive = false;
      window.clearInterval(poll);
      window.clearTimeout(giveUp);
    };
  }, [kind, queryClient]);

  return {
    message: notice ?? (running ? { kind: 'started', text: SCAN_RUNNING_TEXT } : null),
    busy: running || scan.isPending || kind === 'starting' || kind === 'started',
    start: () => {
      clickedAt.current = Date.now();
      setNotice(null);
      scan.mutate();
    },
  };
}
