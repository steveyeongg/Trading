import { useId, type ReactNode } from 'react';

/** What a field hands its input so the label, the error and the input are tied together. */
export interface FieldA11y {
  id: string;
  'aria-invalid': boolean;
  'aria-describedby': string | undefined;
}

/**
 * A labelled form field; the input comes from `children` (called with the ids to put on it).
 * `hint` is a line of help under the input (a unit or a bound); the input points at it and at the
 * error with `aria-describedby`.
 */
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
  children: (a11y: FieldA11y) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    [hint !== undefined ? hintId : null, error !== undefined ? errorId : null]
      .filter((part) => part !== null)
      .join(' ') || undefined;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="mb-1 block text-xs text-ink-muted">
        {label}
      </label>
      {children({
        id,
        'aria-invalid': error !== undefined,
        'aria-describedby': describedBy,
      })}
      {hint !== undefined && (
        <p id={hintId} className="mt-1 text-xs text-ink-muted">
          {hint}
        </p>
      )}
      {error !== undefined && (
        <p id={errorId} role="alert" className="mt-1 text-xs text-bear">
          {error}
        </p>
      )}
    </div>
  );
}
