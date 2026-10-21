'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Field } from '@/components/Field';
import { SectionHeading } from '@/components/SectionHeading';
import { ApiError } from '@/lib/api';
import {
  ADD_FORM_NOTE,
  BOUGHT_DATE_FUTURE_TEXT,
  BOUGHT_DATE_REQUIRED_TEXT,
  FILL_INVALID_TEXT,
  IDEA_LOADING_TEXT,
  IDEA_LOAD_FAILED_TEXT,
  IDEA_MISMATCH_TEXT,
  SHARES_INVALID_TEXT,
  SHOW_EXISTING_POSITION_TEXT,
  STOP_INVALID_TEXT,
  STOP_NOT_BELOW_FILL_TEXT,
  SYMBOL_INVALID_TEXT,
  SYMBOL_REQUIRED_TEXT,
  fillPlaceholderText,
  ideaLineText,
  riskPreviewText,
} from '@/lib/copy';
import { errorText } from '@/lib/error-text';
import { money, pct, price, todayET } from '@/lib/format';
import { INPUT_CLASS, PRIMARY_BUTTON_CLASS, SECONDARY_BUTTON_CLASS } from '@/lib/form-styles';
import { parseAmount, plainNumber } from '@/lib/position-utils';
import { swingApi } from '@/lib/swing-api';
import type { IdeaOut, PositionIn, PositionOut } from '@/lib/swing-types';

// The server's own pattern for a symbol (`swing_routes._SYMBOL`).
const SYMBOL_PATTERN = /^[A-Za-z0-9.-]{1,16}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

interface Values {
  symbol: string;
  openedOn: string;
  fill: string;
  shares: string;
  stop: string;
}

interface Errors {
  symbol?: string;
  openedOn?: string;
  fill?: string;
  shares?: string;
  stop?: string;
}

const clean = (symbol: string) => symbol.trim().toUpperCase();

/**
 * The checks made before anything is sent: a real date that is not after `today` (New York's),
 * shares > 0, fill > 0, stop > 0 and below the fill.
 * `numbers` is set only when all three amounts are good, so the risk preview and the request can
 * use them without checking again.
 */
function check(
  v: Values,
  today: string,
): {
  errors: Errors;
  numbers: { fill: number; shares: number; stop: number } | null;
} {
  const errors: Errors = {};
  const symbol = clean(v.symbol);
  if (symbol === '') errors.symbol = SYMBOL_REQUIRED_TEXT;
  else if (!SYMBOL_PATTERN.test(symbol)) errors.symbol = SYMBOL_INVALID_TEXT;
  if (!DATE_PATTERN.test(v.openedOn)) errors.openedOn = BOUGHT_DATE_REQUIRED_TEXT;
  else if (v.openedOn > today) errors.openedOn = BOUGHT_DATE_FUTURE_TEXT;

  const fill = parseAmount(v.fill);
  const shares = parseAmount(v.shares);
  const stop = parseAmount(v.stop);
  if (fill === null || fill <= 0) errors.fill = FILL_INVALID_TEXT;
  if (shares === null || shares <= 0) errors.shares = SHARES_INVALID_TEXT;
  if (stop === null || stop <= 0) errors.stop = STOP_INVALID_TEXT;
  else if (fill !== null && fill > 0 && stop >= fill) errors.stop = STOP_NOT_BELOW_FILL_TEXT;

  const valid =
    errors.fill === undefined && errors.shares === undefined && errors.stop === undefined;
  return {
    errors,
    numbers:
      valid && fill !== null && shares !== null && stop !== null ? { fill, shares, stop } : null,
  };
}

/**
 * The form that records a trade the user placed themselves. Opened with a symbol and an idea id
 * (from "I bought this" on the stock page), it looks the idea up and fills in the stop and the
 * share count; it never fills in the fill price, because the user records what they really paid.
 * If the idea can't be matched or loaded, the form works the same with the symbol alone and sends
 * no `idea_id`.
 */
export function PositionForm({
  initialSymbol,
  ideaId,
  open,
  onCancel,
  onCreated,
  onShowExisting,
}: {
  initialSymbol: string;
  ideaId: string | null;
  /** The open positions, so a "you already have one" answer can point at its row. */
  open: PositionOut[];
  onCancel: () => void;
  onCreated: (created: PositionOut) => void;
  onShowExisting: (id: string) => void;
}) {
  const headingId = useId();
  const [values, setValues] = useState<Values>(() => ({
    symbol: clean(initialSymbol),
    openedOn: todayET(), // New York's calendar date, not the browser's
    fill: '',
    shares: '',
    stop: '',
  }));
  const [submitted, setSubmitted] = useState(false);

  // The button that opened the form is gone, so put focus in the form: on the symbol, or, when the
  // symbol came with the link, on the fill price (the one thing the user must supply).
  const symbolRef = useRef<HTMLInputElement>(null);
  const fillRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    (clean(initialSymbol) === '' ? symbolRef : fillRef).current?.focus();
  }, [initialSymbol]); // the page re-creates the form (a new key) when the link's symbol changes

  // --- the idea this trade came from ---
  const wantIdea = clean(initialSymbol) !== '' && ideaId !== null;
  const stock = useQuery({
    queryKey: ['stock', clean(initialSymbol)],
    queryFn: () => swingApi.stock(clean(initialSymbol)),
    enabled: wantIdea,
    retry: false,
    staleTime: 60_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  const found = wantIdea ? (stock.data?.idea ?? null) : null;
  const matched: IdeaOut | null = found !== null && found.id === ideaId ? found : null;

  // Fill in the stop and the shares once, and only into boxes still empty (the user may already
  // have typed while the idea was loading).
  const prefilled = useRef(false);
  useEffect(() => {
    if (matched === null || prefilled.current) return;
    prefilled.current = true;
    setValues((v) => ({
      ...v,
      stop: v.stop === '' && matched.stop !== null ? plainNumber(matched.stop) : v.stop,
      shares: v.shares === '' && matched.shares > 0 ? plainNumber(matched.shares) : v.shares,
    }));
  }, [matched]);

  // The idea is linked only while the symbol in the form is still the idea's own.
  const linkedIdeaId =
    matched !== null && matched.id !== null && clean(values.symbol) === matched.symbol.toUpperCase()
      ? matched.id
      : null;

  const create = useMutation<PositionOut, unknown, PositionIn>({
    mutationFn: (body) => swingApi.createPosition(body),
    onSuccess: (created) => onCreated(created),
  });

  const today = todayET();
  const { errors, numbers } = check(values, today);
  const shown: Errors = submitted ? errors : {};
  const preview =
    numbers === null
      ? null
      : riskPreviewText(
          money(numbers.shares * (numbers.fill - numbers.stop)),
          pct((numbers.fill - numbers.stop) / numbers.fill, 1),
        );

  const change = (field: keyof Values) => (value: string) => {
    if (create.isError) create.reset();
    setValues((v) => ({ ...v, [field]: value }));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (numbers === null || Object.keys(errors).length > 0) return;
    create.mutate({
      symbol: clean(values.symbol),
      opened_on: values.openedOn,
      entry_price: numbers.fill,
      shares: numbers.shares,
      initial_stop: numbers.stop,
      ...(linkedIdeaId !== null ? { idea_id: linkedIdeaId } : {}),
    });
  };

  const existing =
    create.error instanceof ApiError && create.error.status === 409
      ? open.find((p) => p.symbol.toUpperCase() === clean(values.symbol))
      : undefined;

  let ideaNote: ReactNode = null;
  if (wantIdea) {
    if (stock.isPending) {
      ideaNote = <p className="text-sm text-ink-muted">{IDEA_LOADING_TEXT}</p>;
    } else if (stock.isError) {
      ideaNote = <p className="text-sm text-ink-muted">{IDEA_LOAD_FAILED_TEXT}</p>;
    } else if (matched === null) {
      ideaNote = <p className="text-sm text-ink-muted">{IDEA_MISMATCH_TEXT}</p>;
    } else if (linkedIdeaId !== null) {
      ideaNote = (
        <p className="text-sm text-accent">
          {ideaLineText(matched.setup_label, price(matched.buy_above))}
        </p>
      );
    }
  }

  const hasBuyAbove = matched !== null && Number.isFinite(matched.buy_above);

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-labelledby={headingId}
      className="space-y-4 rounded-lg border border-line bg-bg-surface p-4"
    >
      <div>
        <SectionHeading id={headingId}>Add a position</SectionHeading>
        <p className="mt-1 text-sm text-ink-muted">{ADD_FORM_NOTE}</p>
      </div>
      {ideaNote}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label="Symbol" error={shown.symbol}>
          {(a) => (
            <input
              {...a}
              ref={symbolRef}
              type="text"
              value={values.symbol}
              onChange={(e) => change('symbol')(e.target.value.toUpperCase())}
              maxLength={16}
              autoCapitalize="characters"
              autoComplete="off"
              spellCheck={false}
              placeholder="e.g. MSFT"
              className={`${INPUT_CLASS} font-mono uppercase`}
            />
          )}
        </Field>
        <Field label="Bought on" error={shown.openedOn}>
          {(a) => (
            <input
              {...a}
              type="date"
              max={today}
              value={values.openedOn}
              onChange={(e) => change('openedOn')(e.target.value)}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field label="Fill price" error={shown.fill}>
          {(a) => (
            <input
              {...a}
              ref={fillRef}
              type="number"
              inputMode="decimal"
              step="any"
              min="0"
              value={values.fill}
              onChange={(e) => change('fill')(e.target.value)}
              placeholder={fillPlaceholderText(hasBuyAbove ? price(matched.buy_above) : null)}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field label="Shares" error={shown.shares}>
          {(a) => (
            <input
              {...a}
              type="number"
              inputMode="decimal"
              step="any"
              min="0"
              value={values.shares}
              onChange={(e) => change('shares')(e.target.value)}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field label="Stop" error={shown.stop}>
          {(a) => (
            <input
              {...a}
              type="number"
              inputMode="decimal"
              step="any"
              min="0"
              value={values.stop}
              onChange={(e) => change('stop')(e.target.value)}
              className={INPUT_CLASS}
            />
          )}
        </Field>
      </div>

      {preview !== null && <p className="text-sm text-ink">{preview}</p>}

      {create.isError && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-bear/40 bg-bg-subtle px-3 py-2 text-sm text-ink"
        >
          <span className="min-w-0 break-words">{errorText(create.error)}</span>
          {existing !== undefined && (
            <a
              href={`#position-${existing.id}`}
              onClick={(event) => {
                event.preventDefault(); // the page scrolls to the row and highlights it
                onShowExisting(existing.id);
              }}
              className="text-accent hover:underline"
            >
              {SHOW_EXISTING_POSITION_TEXT}
            </a>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={create.isPending} className={PRIMARY_BUTTON_CLASS}>
          {create.isPending ? 'Recording…' : 'Record position'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={create.isPending}
          className={SECONDARY_BUTTON_CLASS}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
