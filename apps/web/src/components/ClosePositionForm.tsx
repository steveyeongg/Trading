'use client';

import { useMutation } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Field } from '@/components/Field';
import { Term } from '@/components/Term';
import {
  CLOSE_FORM_NOTE,
  EXIT_INVALID_TEXT,
  EXIT_REASONS,
  SOLD_BEFORE_BOUGHT_TEXT,
  SOLD_DATE_FUTURE_TEXT,
  SOLD_DATE_REQUIRED_TEXT,
  closedResultText,
} from '@/lib/copy';
import { errorText } from '@/lib/error-text';
import { price, r, sessionDate, todayET } from '@/lib/format';
import { INPUT_CLASS, PRIMARY_BUTTON_CLASS, SECONDARY_BUTTON_CLASS } from '@/lib/form-styles';
import { parseAmount, rTone } from '@/lib/position-utils';
import { swingApi } from '@/lib/swing-api';
import type { CloseIn, PositionOut } from '@/lib/swing-types';

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Closes an open position at the price the user got. When the server has recorded it, the form is
 * replaced by the result in R, which stays until the user presses Done (the page keeps the row in
 * place until then, so the result is seen before the row moves to Closed trades).
 *
 * `onClosed` is called once, with the closed position, as soon as the server has it; `onDone` when
 * the user has seen the result; `onCancel` abandons the form.
 */
export function ClosePositionForm({
  position,
  onClosed,
  onDone,
  onCancel,
}: {
  position: PositionOut;
  onClosed: (closed: PositionOut) => void;
  onDone: () => void;
  onCancel: () => void;
}) {
  const reasonId = useId();
  const [soldOn, setSoldOn] = useState(() => todayET()); // New York's calendar date
  const [exit, setExit] = useState('');
  const [reason, setReason] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const today = todayET();

  // The button that opened the form is gone, so put focus on the one thing left to fill in.
  const exitRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    exitRef.current?.focus();
  }, []);

  const close = useMutation<PositionOut, unknown, CloseIn>({
    mutationFn: (body) => swingApi.closePosition(position.id, body),
    onSuccess: (closed) => onClosed(closed),
  });

  const exitPrice = parseAmount(exit);
  const errors: { soldOn?: string; exit?: string } = {};
  if (!DATE_PATTERN.test(soldOn)) errors.soldOn = SOLD_DATE_REQUIRED_TEXT;
  else if (soldOn > today) errors.soldOn = SOLD_DATE_FUTURE_TEXT;
  else if (soldOn < position.opened_on) errors.soldOn = SOLD_BEFORE_BOUGHT_TEXT;
  if (exitPrice === null || exitPrice <= 0) errors.exit = EXIT_INVALID_TEXT;
  const shown = submitted ? errors : {};

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (exitPrice === null || Object.keys(errors).length > 0) return;
    close.mutate({
      closed_on: soldOn,
      exit_price: exitPrice,
      exit_reason: reason === '' ? null : reason,
    });
  };

  const touched = () => {
    if (close.isError) close.reset();
  };

  // The submit button is replaced by the result, so focus goes to the result.
  const resultRef = useRef<HTMLDivElement>(null);
  const closedOk = close.isSuccess;
  useEffect(() => {
    if (closedOk) resultRef.current?.focus();
  }, [closedOk]);

  // The server has the close: show the result in R, then let the user move on.
  if (close.isSuccess) {
    const done = close.data;
    return (
      <div
        ref={resultRef}
        role="status"
        tabIndex={-1}
        className="space-y-2 rounded border border-line bg-bg-subtle px-4 py-3 text-sm outline-none focus-visible:ring-1 focus-visible:ring-accent"
      >
        <p className="text-ink">
          {closedResultText(done.symbol, price(done.exit_price))}
          {done.closed_on !== null && (
            <span className="text-ink-muted">{` on ${sessionDate(done.closed_on)}`}</span>
          )}
        </p>
        <p>
          <span className="text-ink-muted">
            Result in <Term k="R">R</Term>:{' '}
          </span>
          <span className={`font-mono text-lg font-semibold ${rTone(done.r_multiple)}`}>
            {r(done.r_multiple)}
          </span>
        </p>
        <button type="button" onClick={onDone} className={SECONDARY_BUTTON_CLASS}>
          Done
        </button>
      </div>
    );
  }

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-label={`Close ${position.symbol}`}
      className="space-y-3 rounded border border-line bg-bg-subtle p-3"
    >
      <p className="text-sm text-ink-muted">{CLOSE_FORM_NOTE}</p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field label="Sold on" error={shown.soldOn}>
          {(a) => (
            <input
              {...a}
              type="date"
              max={today}
              value={soldOn}
              onChange={(e) => {
                touched();
                setSoldOn(e.target.value);
              }}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <Field label="Exit price" error={shown.exit}>
          {(a) => (
            <input
              {...a}
              ref={exitRef}
              type="number"
              inputMode="decimal"
              step="any"
              min="0"
              value={exit}
              onChange={(e) => {
                touched();
                setExit(e.target.value);
              }}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <div className="min-w-0">
          <label htmlFor={reasonId} className="mb-1 block text-xs text-ink-muted">
            Exit reason (optional)
          </label>
          <select
            id={reasonId}
            value={reason}
            onChange={(e) => {
              touched();
              setReason(e.target.value);
            }}
            className={INPUT_CLASS}
          >
            <option value="">Not set</option>
            {EXIT_REASONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {close.isError && (
        <p
          role="alert"
          className="break-words rounded border border-bear/40 px-3 py-2 text-sm text-ink"
        >
          {errorText(close.error)}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={close.isPending} className={PRIMARY_BUTTON_CLASS}>
          {close.isPending ? 'Closing…' : 'Record close'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={close.isPending}
          className={SECONDARY_BUTTON_CLASS}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
