import type { ReactNode } from 'react';
import { sessionDate } from '@/lib/format';
import type { FreshnessOut, ProgressOut, TodayOut } from '@/lib/swing-types';

// Today's empty-state copy, exactly as the product specifies it.
export const DOWNTREND_TEXT =
  'Market in downtrend: no new buy ideas (stand aside). Strongest names below are watch-only.';
export const NO_SETUP_PASSED_TEXT = 'No setup has passed testing yet';
export const NO_VALIDATED_IDEAS_TEXT = 'No validated-setup ideas tonight.';
export const WAITING_FOR_FIRST_SCAN_TEXT = 'Waiting for the first scan to start.';

export const firstRunText = (percent: number) =>
  `First-time setup: downloading 10 years of daily data (${percent}%)`;
export const noCandidatesText = (session: string) =>
  `No stock met a setup's rules after ${session}'s close.`;

// The failure's reason as the server worded it, without a trailing full stop (the sentence adds one).
const bare = (reason: string) => reason.replace(/[\s.]+$/, '');

// The step a failed run stopped at and why, with a stated fallback for each.
function failure(freshness: FreshnessOut): { step: string; reason: string } {
  return {
    step: freshness.failed_step ?? 'unknown',
    reason: bare(freshness.failed_reason ?? '') || 'no reason was recorded',
  };
}

/** The warn line of a scan saved from incomplete bars (Today's `partial_data`). */
export const partialDataText = (missing: number, expected: number, session: string) =>
  `Prices were missing for ${missing} of ${expected} stocks on ${session}; tonight's ideas may be incomplete.`;

export function scanFailedText(freshness: FreshnessOut): string {
  const { step, reason } = failure(freshness);
  return `Last scan failed at step ${step}: ${reason}. Showing the previous scan.`;
}

/** The download's progress as a whole percent, 0 when it has not reported any. */
export function backfillPercent(backfill: ProgressOut | null): number {
  if (backfill === null || !(backfill.total > 0)) return 0;
  return Math.max(0, Math.min(100, Math.floor((backfill.done * 100) / backfill.total)));
}

type Tone = 'warn' | 'plain';

function Notice({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <div
      role="status"
      className={`rounded-lg border px-4 py-3 text-sm ${
        tone === 'warn'
          ? 'border-warn/50 bg-warn/10 text-warn'
          : 'border-line bg-bg-surface text-ink-muted'
      }`}
    >
      {children}
    </div>
  );
}

/** The warn banner of a scan that was saved although prices were missing for many stocks. */
export function PartialDataBanner({ today }: { today: TodayOut }) {
  const partial = today.partial_data;
  if (partial === null) return null;
  return (
    <Notice tone="warn">
      {partialDataText(partial.missing, partial.expected, sessionDate(today.freshness.data_as_of))}
    </Notice>
  );
}

/** The banner shown above everything when the latest scan failed; the previous ideas follow it. */
export function ScanFailedBanner({ freshness }: { freshness: FreshnessOut }) {
  return <Notice tone="warn">{scanFailedText(freshness)}</Notice>;
}

function FirstRun({ today }: { today: TodayOut }) {
  const { freshness, backfill } = today;
  // No scan exists, so the server reports first_run even when the first job has already failed.
  // Say so, instead of promising a download that is not happening.
  if (freshness.last_scan_status === 'failed') {
    const { step, reason } = failure(freshness);
    return (
      <Notice tone="warn">
        <p>{`Last scan failed at step ${step}: ${reason}.`}</p>
        <p className="mt-1 text-xs text-ink-muted">Use Run scan now to try again.</p>
      </Notice>
    );
  }
  const percent = backfillPercent(backfill);
  return (
    <Notice tone="plain">
      <p className="text-ink">{firstRunText(percent)}</p>
      {backfill === null ? (
        <p className="mt-1">{WAITING_FOR_FIRST_SCAN_TEXT}</p>
      ) : (
        <div
          role="progressbar"
          aria-label="First-time download progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          className="mt-2 h-1.5 w-full overflow-hidden rounded bg-bg-subtle"
        >
          <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
        </div>
      )}
    </Notice>
  );
}

/**
 * Why Today has no ideas to show, for the reasons that replace the ideas area: the first-time
 * download, a market in downtrend and a scan that found nothing. (`last_scan_failed` is the
 * `ScanFailedBanner`, which sits above the ideas that the previous scan still supplies.)
 */
export function EmptyState({ today }: { today: TodayOut }) {
  switch (today.empty_reason) {
    case 'first_run':
      return <FirstRun today={today} />;
    case 'downtrend':
      return <Notice tone="warn">{DOWNTREND_TEXT}</Notice>;
    case 'no_candidates':
      return <Notice tone="plain">{noCandidatesText(sessionDate(today.freshness.data_as_of))}</Notice>;
    default:
      return null;
  }
}
