// Small helpers for the Positions page and its forms (the form classes are in `form-styles.ts`).

import type { PositionOut } from './swing-types';

/** An amount typed into a form: a finite number, or null when the box is blank or not a number. */
export function parseAmount(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/** A number as the text of an input: at most 4 decimals, no trailing zeros, no thousands commas. */
export function plainNumber(x: number): string {
  return String(Number(x.toFixed(4)));
}

/** The colour of a result in R: green above zero, red below, plain at zero (as `r()` prints it). */
export function rTone(x: number | null | undefined): string {
  if (typeof x !== 'number' || !Number.isFinite(x) || Math.abs(x) < 0.05) return 'text-ink';
  return x > 0 ? 'text-bull' : 'text-bear';
}

/**
 * The colour of tonight's action on a position, on Today and on Positions: red for a stop hit,
 * amber for max hold and no data, plain for everything else (hold, raise stop, and a kind this
 * build doesn't know). A `switch`, so a stray key such as "constructor" can't match anything.
 */
export function actionTone(kind: string | null): string {
  switch (kind) {
    case 'stop_hit':
      return 'font-medium text-bear';
    case 'max_hold':
    case 'no_data':
      return 'text-warn';
    default:
      return 'text-ink';
  }
}

/** A position closed during this visit, kept on screen (with its result in R) until Done. */
export interface JustClosed {
  position: PositionOut;
  /** The user has pressed Done: the row goes. */
  done: boolean;
  /** The row's place in the open table when it was closed. */
  index: number;
}

/**
 * The rows of the open table: the server's open list, plus every just-closed position the list no
 * longer has, put back at the place it had until the user presses Done (a position the list still
 * has stays where the list puts it). Rows already marked done are left out.
 */
export function openRowsWithJustClosed(
  open: PositionOut[],
  justClosed: Record<string, JustClosed>,
): PositionOut[] {
  const rows = open.filter((p) => justClosed[p.id]?.done !== true);
  const listed = new Set(rows.map((p) => p.id));
  const kept = Object.values(justClosed)
    .filter((c) => !c.done && !listed.has(c.position.id))
    .sort((a, b) => a.index - b.index);
  for (const c of kept) rows.splice(Math.min(c.index, rows.length), 0, c.position);
  return rows;
}
