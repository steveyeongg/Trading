// Pure helpers for the Track record page: what its verdict badge says, which columns of the
// results table have data, and the equity series prepared for the chart. Nothing is invented: a
// missing or non-finite value is left out, and an unknown verdict is shown as the server wrote it.

import type { LineData, Time } from 'lightweight-charts';
import {
  NOT_RUN_TEXT,
  NO_LIVE_IDEAS_TEXT,
  NO_RECONSTRUCTED_IDEAS_TEXT,
  VERDICT_LIVE_LABEL,
  VERDICT_LIVE_NOTE,
  VERDICT_NOT_TESTED_LABEL,
  VERDICT_UNPROVEN_NOTE,
  equityDescriptionText,
} from './copy';
import { ascending, isDay } from './daily-chart-data';
import { money } from './format';
import type { FamilyOut, PointOut, StatsOut } from './swing-types';

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);

/** True for a number that arrived; false for null, undefined and NaN. */
export const isFiniteNumber = isNum;

// --- the verdict badge -----------------------------------------------------------------------------

export type BadgeTone = 'bull' | 'warn' | 'muted';

export interface VerdictBadge {
  label: string;
  tone: BadgeTone;
}

/**
 * The badge for a family's verdict. It branches on `verdict`, never on the words of
 * `verdictText`: Live is green, Research only (the server's own text, which names the first
 * failed condition) is amber, Not yet tested is muted, and a value this build does not know is
 * shown as the server wrote it, muted.
 */
export function verdictBadge(verdict: string, verdictText: string): VerdictBadge {
  const text = verdictText.trim();
  switch (verdict) {
    case 'live':
      return { label: VERDICT_LIVE_LABEL, tone: 'bull' };
    case 'research':
      return { label: text === '' ? 'Research only' : verdictText, tone: 'warn' };
    case 'not_tested':
      return { label: VERDICT_NOT_TESTED_LABEL, tone: 'muted' };
    default:
      return { label: text === '' ? verdict : verdictText, tone: 'muted' };
  }
}

/** What the verdict means for the family's ideas on Today; null for a verdict this build doesn't know. */
export function verdictNote(verdict: string): string | null {
  switch (verdict) {
    case 'live':
      return VERDICT_LIVE_NOTE;
    case 'research':
    case 'not_tested':
      return VERDICT_UNPROVEN_NOTE;
    default:
      return null;
  }
}

// --- the results table -------------------------------------------------------------------------------

export type ColumnKey = 'insample' | 'oos' | 'live' | 'reconstructed';

export interface RecordColumn {
  key: ColumnKey;
  title: string;
  stats: StatsOut | null;
  /** Why the column has no numbers (shown instead of them); null when it has some. */
  emptyText: string | null;
}

/** The four columns, in order: In-sample, Out-of-sample, Live, Reconstructed. */
export function recordColumns(family: FamilyOut): RecordColumn[] {
  const insample = family.insample ?? null;
  const oos = family.oos ?? null;
  const live = family.live ?? null;
  const rebuilt = family.reconstructed ?? null;
  return [
    {
      key: 'insample',
      title: 'In-sample',
      stats: insample,
      emptyText: insample === null ? NOT_RUN_TEXT : null,
    },
    {
      key: 'oos',
      title: 'Out-of-sample',
      stats: oos,
      emptyText: oos === null ? NOT_RUN_TEXT : null,
    },
    {
      key: 'live',
      title: 'Live',
      stats: live,
      emptyText: live === null || live.n === 0 ? NO_LIVE_IDEAS_TEXT : null,
    },
    {
      key: 'reconstructed',
      title: 'Reconstructed',
      stats: rebuilt,
      emptyText: rebuilt === null || rebuilt.n === 0 ? NO_RECONSTRUCTED_IDEAS_TEXT : null,
    },
  ];
}

// --- the equity chart --------------------------------------------------------------------------------

/** A report's weekly series as chart points: real days only, ascending, one point per day. */
export function equityLine(points: PointOut[]): LineData<Time>[] {
  return ascending(
    points.flatMap((p) => (isDay(p.time) && isNum(p.value) ? [{ time: p.time, value: p.value }] : [])),
  );
}

/** The first and last day over both series, or null when both are empty. */
export function equitySpan(...series: LineData<Time>[][]): { from: string; to: string } | null {
  const days = series.flatMap((s) => s.map((p) => String(p.time))).sort();
  return days.length === 0 ? null : { from: days[0], to: days[days.length - 1] };
}

/** The chart's text alternative: where each line starts and ends. */
export function equityDescription(
  strategy: LineData<Time>[],
  spy: LineData<Time>[],
): string {
  const parts: string[] = [];
  const range = (name: string, line: LineData<Time>[]) => {
    if (line.length === 0) return;
    parts.push(`${name} ${money(line[0].value)} to ${money(line[line.length - 1].value)}.`);
  };
  range('Strategy', strategy);
  range('SPY', spy);
  return equityDescriptionText(parts);
}
