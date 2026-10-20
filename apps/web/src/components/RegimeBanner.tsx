'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { errorText } from '@/lib/error-text';
import { dateTimeET, sessionDate } from '@/lib/format';
import { swingApi } from '@/lib/swing-api';
import type { FreshnessOut, RegimeOut, StatusOut, TodayOut } from '@/lib/swing-types';

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

const REGIME_TONE: Record<string, string> = {
  uptrend: 'text-bull',
  mixed: 'text-warn',
  downtrend: 'text-bear',
};

/** "Data as of Fri 16 Oct close · ideas are for Mon 19 Oct · next scan Mon 4:45pm ET". */
export function freshnessLine(freshness: FreshnessOut): string {
  const parts = [
    freshness.data_as_of === null
      ? 'No scan has finished yet'
      : `Data as of ${sessionDate(freshness.data_as_of)} close`,
  ];
  if (freshness.ideas_for !== null) parts.push(`ideas are for ${sessionDate(freshness.ideas_for)}`);
  if (freshness.next_scan_at !== null) {
    parts.push(`next scan ${dateTimeET(freshness.next_scan_at)}`);
  } else if (freshness.last_scan_status === 'running') {
    // The server clears the next scan time while a scan runs; it is not that scans are off.
    parts.push('scan running now');
  } else {
    parts.push('next scan: automatic scans are off');
  }
  return parts.join(' · ');
}

// Breadth arrives as a percent (61.3 means 61.3%), not a fraction.
const breadthText = (breadth: number) => `Breadth ${Math.round(breadth)}% above 50-day`;

function Market({ regime }: { regime: RegimeOut | null }) {
  if (regime === null) {
    return (
      <span className="font-mono text-sm font-semibold text-ink-muted">MARKET: no scan yet</span>
    );
  }
  const tone = REGIME_TONE[regime.state] ?? 'text-ink';
  const breadth = regime.breadth;
  return (
    <>
      <span className="font-mono text-sm font-semibold">
        <span className="text-ink-muted">MARKET:</span> <span className={tone}>{regime.label}</span>
      </span>
      {breadth !== null && Number.isFinite(breadth) && (
        <span className="text-sm text-ink-muted">{breadthText(breadth)}</span>
      )}
    </>
  );
}

type Notice = { kind: 'starting' | 'started' | 'error'; text: string };

/**
 * The top of Today: the market regime and breadth, how fresh the data is, and the "Run scan now"
 * button. The button is off while a scan runs or the request is in flight; it never claims the
 * scan's results, only that a scan was asked for.
 */
export function RegimeBanner({ today }: { today: TodayOut }) {
  const { freshness, regime } = today;
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<Notice | null>(null);
  const running = freshness.last_scan_status === 'running';
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
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['today'] }),
  });

  // "Starting" has done its job once the scan shows as running; when the run ends, the notes go.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (running && kind === 'starting') setNotice({ kind: 'started', text: SCAN_STARTED_TEXT });
    if (wasRunning.current && !running) setNotice(null);
    wasRunning.current = running;
  }, [running, kind]);

  // After a 202 the job may not be recorded yet, and a quick one can finish between two looks at
  // Today. So for a while, look again every few seconds, at Today and at the latest job run: once
  // that run started after the click and has ended, the scan is over and the note and the lock go.
  const clickedAt = useRef(0);
  useEffect(() => {
    if (kind !== 'starting' && kind !== 'started') return;
    let alive = true;
    const look = () => {
      void queryClient.invalidateQueries({ queryKey: ['today'] });
      swingApi
        .status()
        .then((status) => {
          if (alive && scanSettled(status, clickedAt.current)) setNotice(null);
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

  const busy = running || scan.isPending || kind === 'starting' || kind === 'started';
  const message: Notice | null =
    notice ?? (running ? { kind: 'started', text: SCAN_RUNNING_TEXT } : null);

  return (
    <section
      aria-label="Market and data freshness"
      className="rounded-lg border border-line bg-bg-surface px-4 py-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1 basis-80">
          <p className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
            <Market regime={regime} />
          </p>
          <p className="mt-1 text-xs text-ink-muted">{freshnessLine(freshness)}</p>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 sm:justify-end">
          {message !== null && (
            <span
              role="status"
              className={`text-xs ${message.kind === 'error' ? 'text-warn' : 'text-ink-muted'}`}
            >
              {message.text}
            </span>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              clickedAt.current = Date.now();
              setNotice(null);
              scan.mutate();
            }}
            className="rounded border border-accent-muted px-3 py-1.5 text-sm text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
          >
            Run scan now
          </button>
        </div>
      </div>
    </section>
  );
}
