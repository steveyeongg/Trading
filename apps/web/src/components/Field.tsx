import { useId, type ReactNode } from 'react';

/** What a field hands its input so the label, the error and the input are tied together. */
export interface FieldA11y {
  id: string;
  'aria-invalid': boolean;
  'aria-describedby': string | undefined;
}

/** A labelled form field; the input comes from `children` (called with the ids to put on it). */
export function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string | undefined;
  children: (a11y: FieldA11y) => ReactNode;
}) {
  const id = useId();
  const errorId = `${id}-error`;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="mb-1 block text-xs text-ink-muted">
        {label}
      </label>
      {children({
        id,
        'aria-invalid': error !== undefined,
        'aria-describedby': error !== undefined ? errorId : undefined,
      })}
      {error !== undefined && (
        <p id={errorId} role="alert" className="mt-1 text-xs text-bear">
          {error}
        </p>
      )}
    </div>
  );
}
