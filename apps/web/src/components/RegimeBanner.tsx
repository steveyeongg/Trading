'use client';

import { dateTimeET, sessionDate } from '@/lib/format';
import type { FreshnessOut, RegimeOut, TodayOut } from '@/lib/swing-types';
import { useRunScan } from '@/lib/use-run-scan';

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

/**
 * The top of Today: the market regime and breadth, how fresh the data is, and the "Run scan now"
 * button. The button's behaviour is `useRunScan`'s, shared with the status panel on Settings.
 */
export function RegimeBanner({ today }: { today: TodayOut }) {
  const { freshness, regime } = today;
  const { message, busy, start } = useRunScan(freshness.last_scan_status === 'running');

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
            onClick={start}
            className="rounded border border-accent-muted px-3 py-1.5 text-sm text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
          >
            Run scan now
          </button>
        </div>
      </div>
    </section>
  );
}
