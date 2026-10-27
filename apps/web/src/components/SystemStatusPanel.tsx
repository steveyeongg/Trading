'use client';

import { firstRunText, backfillPercent } from '@/components/EmptyState';
import { LoadFailure } from '@/components/LoadFailure';
import { SectionHeading } from '@/components/SectionHeading';
import {
  RUN_SCAN_LABEL,
  STATUS_AUTO_EXECUTION_OFF_TEXT,
  STATUS_AUTO_EXECUTION_ON_TEXT,
  STATUS_CONFIGURED_TEXT,
  STATUS_HEADING,
  STATUS_LOADING_TEXT,
  STATUS_NEXT_SCAN_AFTER_RUN_TEXT,
  STATUS_NOT_SET_TEXT,
  STATUS_NO_DATA_TEXT,
  STATUS_NO_SCAN_TEXT,
  STATUS_STEPS,
  STATUS_STEPS_LABEL,
  STATUS_STEP_NOT_RUN_TEXT,
  backfillLabelText,
  schedulerOffText,
  statusDataAsOfText,
  statusLastScanText,
  statusNextScanText,
  statusScanTimesText,
} from '@/lib/copy';
import { count, dateTimeET, sessionDate } from '@/lib/format';
import { RUN_SCAN_BUTTON_CLASS } from '@/lib/form-styles';
import type { LastScanOut, ProgressOut, StatusOut, StepInfo } from '@/lib/swing-types';
import { useRunScan } from '@/lib/use-run-scan';
import { useSystemStatus } from '@/lib/use-system-status';

const BLOCK = 'space-y-1';
const MUTED = 'text-ink-muted';

// --- one step of the last scan --------------------------------------------------------------------

interface StepView {
  /** The mark in front of the step: a glyph, hidden from screen readers (`word` stands in). */
  mark: string;
  /** What a screen reader hears for the mark: "ok", "failed", ... */
  word: string;
  tone: string;
  /** Shown after the step's name; null for a step that simply worked. */
  outcome: string | null;
  /** The step's own error text, verbatim, on its own line. */
  error: string | null;
}

/**
 * How one step of the log reads. `info` is undefined when the run never reached the step; a value
 * that is not a step record at all (null, a string, a list, a record with no status) reads the same
 * way, so a malformed log can never throw here. A status this build does not know is shown as the
 * text the job recorded.
 */
function stepView(info: unknown): StepView {
  const notRun: StepView = {
    mark: '·',
    word: 'not run',
    tone: MUTED,
    outcome: STATUS_STEP_NOT_RUN_TEXT,
    error: null,
  };
  if (typeof info !== 'object' || info === null || Array.isArray(info)) return notRun;
  const step = info as StepInfo;
  if (typeof step.status !== 'string') return notRun;
  switch (step.status) {
    case 'ok':
      return { mark: '✓', word: 'ok', tone: 'text-bull', outcome: null, error: null };
    case 'failed': {
      const error = typeof step.error === 'string' && step.error.trim() !== '' ? step.error : null;
      return {
        mark: '✗',
        word: 'failed',
        tone: 'text-bear',
        outcome: error === null ? 'failed' : null,
        error,
      };
    }
    case 'skipped': {
      const reason =
        typeof step.reason === 'string' && step.reason.trim() !== '' ? step.reason : null;
      return {
        mark: '–',
        word: 'skipped',
        tone: MUTED,
        outcome: reason === null ? 'skipped' : `skipped: ${reason}`,
        error: null,
      };
    }
    case 'running':
      return { mark: '…', word: 'running', tone: 'text-accent', outcome: 'running…', error: null };
    default:
      return {
        mark: '?',
        word: step.status,
        tone: MUTED,
        outcome: step.status,
        error: null,
      };
  }
}

function StepRow({ name, label, info }: { name: string; label: string | null; info: unknown }) {
  const view = stepView(info);
  return (
    <li>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span aria-hidden="true" className={`w-4 text-center ${view.tone}`}>
          {view.mark}
        </span>
        {view.outcome === null && <span className="sr-only">{`${view.word}:`}</span>}
        <span className="font-mono">{name}</span>
        {label !== null && <span className={MUTED}>{label}</span>}
        {view.outcome !== null && <span className={view.tone}>{view.outcome}</span>}
      </div>
      {view.error !== null && (
        <p className="ml-6 mt-0.5 break-words text-bear">{view.error}</p>
      )}
    </li>
  );
}

const KNOWN_STEPS: ReadonlySet<string> = new Set(STATUS_STEPS.map((step) => step.name));
const hasKey = (record: object, key: string) => Object.prototype.hasOwnProperty.call(record, key);

function LastScan({ last }: { last: LastScanOut | null }) {
  if (last === null) return <p className={MUTED}>{STATUS_NO_SCAN_TEXT}</p>;
  const times = statusScanTimesText(
    last.started_at === null ? null : dateTimeET(last.started_at),
    last.generated_at === null ? null : dateTimeET(last.generated_at),
  );
  const steps: Record<string, unknown> =
    typeof last.steps === 'object' && last.steps !== null && !Array.isArray(last.steps)
      ? last.steps
      : {};
  // A run that failed before the six steps (the calendar, the settings) records that step under
  // its own name; it is listed after the six so the real failing step is visible.
  const extra = Object.keys(steps).filter((name) => !KNOWN_STEPS.has(name));
  return (
    <div className={BLOCK}>
      <p className={last.status === 'failed' ? 'text-bear' : undefined}>
        {statusLastScanText(last.kind, last.status)}
      </p>
      {times !== '' && <p className={`text-xs ${MUTED}`}>{times}</p>}
      <ol aria-label={STATUS_STEPS_LABEL} className="mt-2 space-y-1">
        {STATUS_STEPS.map(({ name, label }) => (
          <StepRow
            key={name}
            name={name}
            label={label}
            info={hasKey(steps, name) ? steps[name] : undefined}
          />
        ))}
        {extra.map((name) => (
          <StepRow key={`extra-${name}`} name={name} label={null} info={steps[name]} />
        ))}
      </ol>
      {last.error !== null && last.error.trim() !== '' && (
        <p className="mt-2 break-words text-bear">{last.error}</p>
      )}
    </div>
  );
}

// --- the first-time download ----------------------------------------------------------------------

function Backfill({ backfill, firstRun }: { backfill: ProgressOut; firstRun: boolean }) {
  const percent = backfillPercent(backfill);
  const max = Math.max(1, backfill.total);
  const label = backfillLabelText(count(backfill.done), count(backfill.total), percent);
  return (
    <div className={BLOCK}>
      {firstRun && <p>{firstRunText(percent)}</p>}
      <label className="block">
        <span className="block text-ink-muted">{label}</span>
        <progress
          value={Math.min(Math.max(0, backfill.done), max)}
          max={max}
          className="mt-1 block h-2 w-full appearance-none overflow-hidden rounded [&::-moz-progress-bar]:bg-accent [&::-webkit-progress-bar]:bg-bg-subtle [&::-webkit-progress-value]:bg-accent"
        />
      </label>
    </div>
  );
}

// --- the page of facts ----------------------------------------------------------------------------

function ConfigDot({ name, configured }: { name: string; configured: boolean }) {
  return (
    <li className="flex items-center gap-2">
      <span
        aria-hidden="true"
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${configured ? 'bg-bull' : 'bg-ink-subtle'}`}
      />
      <span>{`${name}: ${configured ? STATUS_CONFIGURED_TEXT : STATUS_NOT_SET_TEXT}`}</span>
    </li>
  );
}

function NextScan({ status }: { status: StatusOut }) {
  if (!status.scheduler_running) {
    return <p className="text-warn">{schedulerOffText(status.disabled_reason)}</p>;
  }
  if (status.next_scan_at !== null) {
    return <p>{statusNextScanText(dateTimeET(status.next_scan_at))}</p>;
  }
  // The server clears the next scan time while the scheduled job itself runs.
  return <p className={MUTED}>{STATUS_NEXT_SCAN_AFTER_RUN_TEXT}</p>;
}

function RunScanControl({ running }: { running: boolean }) {
  const { message, busy, start } = useRunScan(running);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <button
        type="button"
        disabled={busy}
        onClick={start}
        className={RUN_SCAN_BUTTON_CLASS}
      >
        {RUN_SCAN_LABEL}
      </button>
      <span
        role="status"
        className={`text-xs ${message?.kind === 'error' ? 'text-warn' : 'text-ink-muted'}`}
      >
        {message?.text}
      </span>
    </div>
  );
}

function StatusFacts({ status }: { status: StatusOut }) {
  const running = status.last_scan?.status === 'running';
  return (
    <div className="space-y-4 rounded-lg border border-line bg-bg-surface p-4 text-sm">
      <p>
        {status.data_as_of === null
          ? STATUS_NO_DATA_TEXT
          : statusDataAsOfText(sessionDate(status.data_as_of))}
      </p>
      {status.backfill !== null && (
        <Backfill backfill={status.backfill} firstRun={status.data_as_of === null} />
      )}
      <LastScan last={status.last_scan} />
      <NextScan status={status} />
      <ul className="space-y-1">
        <ConfigDot name="Alpaca" configured={status.alpaca_configured} />
        <ConfigDot name="Telegram" configured={status.telegram_configured} />
      </ul>
      {/* The server reports whether the opt-in is on; if it ever is, say so rather than hide it. */}
      <p className={status.auto_execution ? 'font-medium text-bear' : undefined}>
        {status.auto_execution ? STATUS_AUTO_EXECUTION_ON_TEXT : STATUS_AUTO_EXECUTION_OFF_TEXT}
      </p>
      <RunScanControl running={running} />
    </div>
  );
}

/**
 * What the system is doing: when the data is from, the last scan and each of its steps, when the
 * next one runs (or why none will), the first-time download, which providers are set up, that
 * ATLAS places no orders, and the "Run scan now" button. Polled every 15 s, every 5 s while a scan
 * runs.
 */
export function SystemStatusPanel() {
  const status = useSystemStatus();
  return (
    <section aria-labelledby="status-heading" className="space-y-3">
      <SectionHeading id="status-heading">{STATUS_HEADING}</SectionHeading>
      {status.error !== null && (
        <LoadFailure
          error={status.error}
          retrying={status.isFetching}
          onRetry={() => void status.refetch()}
        />
      )}
      {status.data !== undefined ? (
        <StatusFacts status={status.data} />
      ) : (
        status.isPending && <p className="text-sm text-ink-muted">{STATUS_LOADING_TEXT}</p>
      )}
    </section>
  );
}
