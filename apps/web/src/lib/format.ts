// Display helpers for the swing dashboard. Every one is null/NaN-safe and renders a missing or
// non-finite value as "—", so a page never prints "NaN" or "null".

const DASH = '—';

/** True for a finite number; false for null, undefined, NaN and the infinities. */
export const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);

// A negative number that rounds to zero prints as "-0"; drop the sign.
const dropNegativeZero = (s: string) => s.replace(/^-(?=\$?0(?:\.0+)?%?$)/, '');

const usdWhole = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});
const twoDecimals = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const oneDecimal = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
const wholeNumber = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const percentFormats = new Map<number, Intl.NumberFormat>();

/** Dollars rounded to the whole dollar: 1234.4 -> "$1,234". */
export function money(x: number | null | undefined): string {
  return isNum(x) ? dropNegativeZero(usdWhole.format(x)) : DASH;
}

/** A share price with 2 decimals: 148.2 -> "148.20". */
export function price(x: number | null | undefined): string {
  return isNum(x) ? dropNegativeZero(twoDecimals.format(x)) : DASH;
}

/** A fraction as a percent: pct(0.42) -> "42%", pct(0.425, 1) -> "42.5%". */
export function pct(x: number | null | undefined, digits = 0): string {
  if (!isNum(x)) return DASH;
  let format = percentFormats.get(digits);
  if (format === undefined) {
    format = new Intl.NumberFormat('en-US', {
      style: 'percent',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    percentFormats.set(digits, format);
  }
  return dropNegativeZero(format.format(x));
}

const decimalFormats = new Map<number, Intl.NumberFormat>();

/** A signed number with `digits` places ("+0.09", "-1.0"); zero, or too small to show, has no sign. */
function signedDecimal(x: number, digits: number): string {
  let format = decimalFormats.get(digits);
  if (format === undefined) {
    format = new Intl.NumberFormat('en-US', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    decimalFormats.set(digits, format);
  }
  const size = format.format(Math.abs(x));
  if (/^[0.]+$/.test(size)) return size;
  return `${x > 0 ? '+' : '-'}${size}`;
}

/**
 * A result in R, signed, one decimal by default: r(0.4) -> "+0.4R", r(-1) -> "-1.0R",
 * r(0) -> "0.0R". `digits` asks for more places: r(0.059, 2) -> "+0.06R".
 */
export function r(x: number | null | undefined, digits = 1): string {
  return isNum(x) ? `${signedDecimal(x, digits)}R` : DASH;
}

/** Like `r(x, 2)` without the trailing "R" ("+0.09"), for copy that writes its own "R". */
export function signedNumber(x: number | null | undefined, digits = 2): string {
  return isNum(x) ? signedDecimal(x, digits) : DASH;
}

/** A whole number with thousands separators: 18273 -> "18,273". */
export function count(x: number | null | undefined): string {
  return isNum(x) ? wholeNumber.format(x) : DASH;
}

/** A ratio with 2 decimals (profit factor, Sharpe): 1.1149 -> "1.11", -0.38 -> "-0.38". */
export function ratio(x: number | null | undefined): string {
  return isNum(x) ? dropNegativeZero(twoDecimals.format(x)) : DASH;
}

/** An average holding time: 19.25 -> "19.3 sessions". */
export function sessions(x: number | null | undefined): string {
  return isNum(x) ? `${oneDecimal.format(x)} sessions` : DASH;
}

// The weekday, day and month of a date, in the form "Mon 19 Oct" (and with the year).
const calendarDay = new Intl.DateTimeFormat('en-US', {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

/** The parts of a session date "YYYY-MM-DD", or null when it is not a real calendar date. */
function calendarParts(
  iso: string | null | undefined,
): { weekday: string; day: string; month: string; year: string } | null {
  const match = typeof iso === 'string' ? /^(\d{4})-(\d{2})-(\d{2})/.exec(iso) : null;
  if (match === null) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(Date.UTC(year, month - 1, day));
  const real =
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  if (!real) return null; // 2026-02-30 and the like
  const part = Object.fromEntries(calendarDay.formatToParts(date).map((p) => [p.type, p.value]));
  return { weekday: part.weekday, day: part.day, month: part.month, year: part.year };
}

/**
 * A session date "YYYY-MM-DD" as "Mon 19 Oct". It is a calendar date: the digits are used as they
 * are and never shifted through the viewer's timezone (so it reads the same in Kuala Lumpur and
 * New York).
 */
export function sessionDate(iso: string | null | undefined): string {
  const parts = calendarParts(iso);
  return parts === null ? DASH : `${parts.weekday} ${parts.day} ${parts.month}`;
}

/** Like `sessionDate`, with the year: "Mon 19 Oct 2026" (for a history that spans years). */
export function sessionDateYear(iso: string | null | undefined): string {
  const parts = calendarParts(iso);
  return parts === null ? DASH : `${parts.weekday} ${parts.day} ${parts.month} ${parts.year}`;
}

const newYork = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  weekday: 'short',
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
});

/**
 * A UTC ISO timestamp in New York time: "2026-10-19T20:45:00Z" -> "Mon 4:45pm ET". A timestamp
 * with no offset is read as UTC.
 */
export function dateTimeET(iso: string | null | undefined): string {
  if (typeof iso !== 'string' || iso === '') return DASH;
  const timed = /T\d{2}:\d{2}/.test(iso);
  const zoned = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i.test(iso);
  const when = new Date(timed && !zoned ? `${iso}Z` : iso);
  if (Number.isNaN(when.getTime())) return DASH;
  const part = Object.fromEntries(newYork.formatToParts(when).map((p) => [p.type, p.value]));
  return `${part.weekday} ${part.hour}:${part.minute}${part.dayPeriod.toLowerCase()} ET`;
}

const newYorkDay = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/**
 * The calendar date in New York right now (or at `now`), as "YYYY-MM-DD". Trades are dated by the
 * New York session they happened in, so a form's "today" default comes from here and never from
 * the browser's own local date (in Kuala Lumpur the two differ for half of every day).
 */
export function todayET(now: Date = new Date()): string {
  const part = Object.fromEntries(newYorkDay.formatToParts(now).map((p) => [p.type, p.value]));
  return `${part.year}-${part.month}-${part.day}`;
}
