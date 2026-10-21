// What the Settings form needs to turn the server's numbers into what the user types, and back.
//
// The server stores percents as fractions (risk 0.01 = 1%) and the dollar volume in dollars. The
// form shows percents as % and the dollar volume in millions, so every field goes through one of
// the converters here. They are exact for what a person types: 1% is 0.01 and shows as 1 again,
// never 1.0000000002 or 0.010000000000000002.

import { SETTINGS_FIELD_TEXT } from './copy';
import { parseAmount, plainNumber } from './position-utils';
import { issueMessage, ValidationError } from './swing-api';
import type { SettingsIn, SwingSettings } from './swing-types';

/** The universe floors the setups were validated on (`UniverseFilter` defaults). */
export const DEFAULT_MIN_PRICE = 10;
export const DEFAULT_MIN_DOLLAR_VOLUME = 20_000_000;

/** The form's number fields, in the order they are shown (the digest toggle is not one). */
export type FieldKey = Exclude<keyof SwingSettings, 'telegram_digest'>;

export const ACCOUNT_FIELDS: readonly FieldKey[] = [
  'equity',
  'risk_pct',
  'max_position_pct',
  'max_positions',
  'max_total_risk_pct',
];
export const UNIVERSE_FIELDS: readonly FieldKey[] = ['min_price', 'min_dollar_volume'];
export const FIELD_KEYS: readonly FieldKey[] = [...ACCOUNT_FIELDS, ...UNIVERSE_FIELDS];

/** How a field is typed: dollars, a percent, a count, or millions of dollars. */
export type FieldKind = 'dollars' | 'percent' | 'count' | 'millions';

interface FieldRule {
  kind: FieldKind;
  /** Whether a value, in the server's units, is inside the server's bounds. */
  ok: (value: number) => boolean;
}

// The bounds are SwingSettings' (swing_engine/settings.py): equity > 0; risk_pct in (0, 0.05];
// max_position_pct in (0, 1]; max_positions 1 to 50; max_total_risk_pct in (0, 0.5];
// min_price >= 1; min_dollar_volume >= 1,000,000.
const RULES: Record<FieldKey, FieldRule> = {
  equity: { kind: 'dollars', ok: (v) => v > 0 },
  risk_pct: { kind: 'percent', ok: (v) => v > 0 && v <= 0.05 },
  max_position_pct: { kind: 'percent', ok: (v) => v > 0 && v <= 1 },
  max_positions: { kind: 'count', ok: (v) => Number.isInteger(v) && v >= 1 && v <= 50 },
  max_total_risk_pct: { kind: 'percent', ok: (v) => v > 0 && v <= 0.5 },
  min_price: { kind: 'dollars', ok: (v) => v >= 1 },
  min_dollar_volume: { kind: 'millions', ok: (v) => v >= 1_000_000 },
};

export const fieldKind = (key: FieldKey): FieldKind => RULES[key].kind;

// --- converters ---------------------------------------------------------------------------------

/** A fraction as the % a person types: 0.01 -> "1", 0.0035 -> "0.35". Two decimals at most. */
export function toPercentText(fraction: number): string {
  return String(Math.round(fraction * 10000) / 100);
}

/**
 * The % a person typed as the fraction the server stores: "1" -> 0.01, "0.35" -> 0.0035.
 * Null when the text is not a number. Dividing by 100 can leave binary noise (0.35 / 100 is
 * 0.0035000000000000005), so the result is rounded at the tenth decimal, far below anything typed.
 */
export function percentToFraction(text: string): number | null {
  const percent = parseAmount(text);
  return percent === null ? null : Number((percent / 100).toFixed(10));
}

/** Dollars as the millions a person types: 20000000 -> "20", 1500000 -> "1.5". */
export function toMillionsText(dollars: number): string {
  return String(Number((dollars / 1_000_000).toFixed(6)));
}

/** The millions a person typed as dollars: "20" -> 20000000, "1.5" -> 1500000. */
export function millionsToDollars(text: string): number | null {
  const millions = parseAmount(text);
  return millions === null ? null : Math.round(millions * 1_000_000);
}

/** A stored value as the text of its input. */
export function toText(key: FieldKey, stored: number): string {
  switch (fieldKind(key)) {
    case 'percent':
      return toPercentText(stored);
    case 'millions':
      return toMillionsText(stored);
    default:
      return plainNumber(stored);
  }
}

/** What was typed, in the server's units; null when it is not a number. */
export function fromText(key: FieldKey, text: string): number | null {
  switch (fieldKind(key)) {
    case 'percent':
      return percentToFraction(text);
    case 'millions':
      return millionsToDollars(text);
    default:
      return parseAmount(text);
  }
}

// --- the form's values --------------------------------------------------------------------------

export type FormValues = Record<FieldKey, string>;
export type FormErrors = Partial<Record<FieldKey, string>>;

/** Every field of the stored settings as the text its input shows. */
export function textsFrom(settings: SwingSettings): FormValues {
  return Object.fromEntries(
    FIELD_KEYS.map((key) => [key, toText(key, settings[key])]),
  ) as FormValues;
}

/**
 * The checks made before anything is sent, with the server's bounds: the message for each field
 * that is out of bounds (or not a number), and the values of the fields that are fine.
 */
export function checkValues(values: FormValues): {
  errors: FormErrors;
  numbers: Partial<Record<FieldKey, number>>;
} {
  const errors: FormErrors = {};
  const numbers: Partial<Record<FieldKey, number>> = {};
  for (const key of FIELD_KEYS) {
    const value = fromText(key, values[key]);
    if (value === null || !RULES[key].ok(value)) errors[key] = SETTINGS_FIELD_TEXT[key].invalid;
    else numbers[key] = value;
  }
  return { errors, numbers };
}

/**
 * The fields to send: those whose text differs from what was loaded, and whose value really
 * changed (typing "1.0" over "1" is not a change). A field with text that is not a number counts
 * as changed, so that the save reports it.
 */
export function changedKeys(values: FormValues, saved: SwingSettings): FieldKey[] {
  const initial = textsFrom(saved);
  return FIELD_KEYS.filter((key) => {
    if (values[key].trim() === initial[key]) return false;
    const value = fromText(key, values[key]);
    return value === null || value !== saved[key];
  });
}

/** The save request: the changed fields only, in the server's units. Null if any is invalid. */
export function buildPatch(values: FormValues, saved: SwingSettings): SettingsIn | null {
  const { errors, numbers } = checkValues(values);
  const patch: SettingsIn = {};
  for (const key of changedKeys(values, saved)) {
    const value = numbers[key];
    if (errors[key] !== undefined || value === undefined) return null;
    patch[key] = value;
  }
  return patch;
}

/**
 * True when the price or dollar-volume floor (the typed value, or the saved one while the box has
 * not been filled in with a number) is not the default the setups were validated on.
 */
export function universeDiffers(values: FormValues, saved: SwingSettings): boolean {
  const price = fromText('min_price', values.min_price) ?? saved.min_price;
  const volume = fromText('min_dollar_volume', values.min_dollar_volume) ?? saved.min_dollar_volume;
  return price !== DEFAULT_MIN_PRICE || volume !== DEFAULT_MIN_DOLLAR_VOLUME;
}

// --- the server's 422 ---------------------------------------------------------------------------

const isFieldKey = (name: unknown): name is FieldKey =>
  typeof name === 'string' && (FIELD_KEYS as readonly string[]).includes(name);

/**
 * A 422's messages sorted by where they belong: `fields` holds the first message for each form
 * field the entry's `loc` names (`["body", "risk_pct"]`), and `general` holds the rest (an
 * unknown field, a body the server could not read), joined, for the top of the form. Null when
 * `err` is not a 422.
 */
export function splitIssues(
  err: unknown,
): { fields: FormErrors; general: string | null } | null {
  if (!(err instanceof ValidationError)) return null;
  const fields: FormErrors = {};
  const general: string[] = [];
  for (const issue of err.issues) {
    const name = issue.loc[0] === 'body' ? issue.loc[1] : issue.loc[0];
    const message = issueMessage(issue);
    if (isFieldKey(name)) fields[name] ??= message;
    else general.push(message);
  }
  return { fields, general: general.length > 0 ? general.join(' ') : null };
}
