// Small helpers the Positions page and its two forms share.

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

/** The colour of tonight's action on an open position; a kind this build doesn't know is plain. */
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

// Classes the forms share.
export const INPUT_CLASS =
  'w-full rounded border border-line bg-bg-subtle px-2 py-1.5 text-sm text-ink placeholder:text-ink-subtle focus:border-accent focus:outline-none aria-invalid:border-bear';
export const PRIMARY_BUTTON_CLASS =
  'rounded border border-accent-muted px-4 py-1.5 text-sm font-medium text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50';
export const SECONDARY_BUTTON_CLASS =
  'rounded border border-line-strong px-3 py-1.5 text-sm text-ink hover:bg-bg-subtle focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50';
