// The stock page chart's data, prepared from the API's `bars`, `lines` and `levels`. These are pure
// functions (no chart library at run time), so the rules for gaps and missing values sit in one
// place. Nothing is invented: a bar without a full open, high, low and close is left out, a missing
// day on an indicator line stays a break, and a level that is null is not drawn.

import type { CandlestickData, HistogramData, LineData, Time } from 'lightweight-charts';
import { CHART_COLORS } from './chart-colors';
import { isNum } from './format';
import type { BarOut, LevelsOut, LinesOut, PointOut } from './swing-types';

// "YYYY-MM-DD" is a business day to the chart. The string is passed on untouched: it never goes
// through a JS Date, whose timezone could move it a day.
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
export const isDay = (x: unknown): x is string => typeof x === 'string' && ISO_DAY.test(x);

/** Ascending by day, one entry per day (the last one wins). */
export function ascending<T extends { time: string }>(rows: T[]): T[] {
  const byDay = new Map<string, T>();
  for (const row of rows) byDay.set(row.time, row);
  return Array.from(byDay.values()).sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

/** A bar that can be drawn: a real day and a finite open, high, low and close. */
function drawable(bar: BarOut): bar is BarOut & { open: number; high: number; low: number } {
  return (
    isDay(bar.time) && isNum(bar.open) && isNum(bar.high) && isNum(bar.low) && isNum(bar.close)
  );
}

export function candleData(bars: BarOut[]): CandlestickData<Time>[] {
  return ascending(
    bars.filter(drawable).map((b) => ({
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    })),
  );
}

/** Volume for the bars that are drawn; a bar with no volume has no column. */
export function volumeData(bars: BarOut[]): HistogramData<Time>[] {
  return ascending(
    bars.filter(drawable).flatMap((b) =>
      isNum(b.volume)
        ? [
            {
              time: b.time,
              value: b.volume,
              color: `${b.close >= b.open ? CHART_COLORS.bull : CHART_COLORS.bear}44`,
            },
          ]
        : [],
    ),
  );
}

/**
 * One indicator line as separate runs of points, so that it breaks where the data does. The API
 * leaves out the days an indicator has no value (a gap in the stock's history, or too few sessions
 * to compute it). The chart joins neighbouring points of one series, even across a whitespace
 * entry, which would draw a straight line over the gap; so each stretch of consecutive days that
 * have a value is its own run (the caller gives each run its own series). A day counts as a gap
 * when the chart has a bar for it and the line has no point. Nothing is interpolated.
 */
export function lineRuns(points: PointOut[], bars: BarOut[]): LineData<Time>[][] {
  const values = new Map<string, number>();
  for (const p of points) if (isDay(p.time) && isNum(p.value)) values.set(p.time, p.value);
  const days = new Set<string>(values.keys());
  for (const b of bars) if (isDay(b.time)) days.add(b.time);
  const runs: LineData<Time>[][] = [];
  let run: LineData<Time>[] = [];
  for (const time of Array.from(days).sort()) {
    const value = values.get(time);
    if (value === undefined) {
      if (run.length > 0) runs.push(run);
      run = [];
    } else {
      run.push({ time, value });
    }
  }
  if (run.length > 0) runs.push(run);
  return runs;
}

// --- the three indicator lines ----------------------------------------------------------------

export type LineKey = keyof LinesOut;

export interface LineSpec {
  key: LineKey;
  /** The legend's words. */
  label: string;
  color: string;
  width: 1 | 2;
}

/** The lines under the candles, back to front. Each is named in the legend below the chart. */
export const LINE_SPECS: readonly LineSpec[] = [
  { key: 'sma200', label: '200-day average', color: CHART_COLORS.warn, width: 2 },
  { key: 'sma50', label: '50-day average', color: CHART_COLORS.accent, width: 2 },
  { key: 'ema20', label: '20-day average', color: CHART_COLORS.inkMuted, width: 1 },
];

/** The runs of each of the three lines. */
export function allLineRuns(lines: LinesOut, bars: BarOut[]): Record<LineKey, LineData<Time>[][]> {
  return {
    sma50: lineRuns(lines.sma50, bars),
    sma200: lineRuns(lines.sma200, bars),
    ema20: lineRuns(lines.ema20, bars),
  };
}

// --- the trade levels -------------------------------------------------------------------------

export type LevelStyle = 'solid' | 'dashed' | 'dotted';

export interface LevelLine {
  /** The label on the price axis. */
  title: string;
  price: number;
  color: string;
  style: LevelStyle;
}

/** The horizontal price lines, each only when the API gave that level. */
export function levelLines(levels: LevelsOut | null): LevelLine[] {
  if (levels === null) return [];
  const all: { title: string; price: number | null; color: string; style: LevelStyle }[] = [
    { title: 'Buy above', price: levels.trigger, color: CHART_COLORS.bull, style: 'dashed' },
    { title: 'Stop', price: levels.stop, color: CHART_COLORS.bear, style: 'solid' },
    { title: '2R reference', price: levels.target_2r, color: CHART_COLORS.inkMuted, style: 'dotted' },
    { title: 'Trail', price: levels.trail, color: CHART_COLORS.warn, style: 'dashed' },
  ];
  return all.flatMap((l) => (isNum(l.price) ? [{ ...l, price: l.price }] : []));
}
