'use client';

import { useIsMutating } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Field } from '@/components/Field';
import { SectionHeading } from '@/components/SectionHeading';
import {
  SETTINGS_ACCOUNT_HEADING,
  SETTINGS_FIELD_TEXT,
  SETTINGS_FIX_FIELDS_TEXT,
  SETTINGS_NO_CHANGES_TEXT,
  SETTINGS_SAVED_TEXT,
  SETTINGS_SAVE_LABEL,
  SETTINGS_SAVING_LABEL,
  SETTINGS_UNIVERSE_HEADING,
  UNIVERSE_WARNING_TEXT,
} from '@/lib/copy';
import { errorText } from '@/lib/error-text';
import { INPUT_CLASS, PRIMARY_BUTTON_CLASS } from '@/lib/form-styles';
import {
  ACCOUNT_FIELDS,
  UNIVERSE_FIELDS,
  buildPatch,
  changedKeys,
  checkValues,
  fieldKind,
  isPristine,
  splitIssues,
  textsFrom,
  universeDiffers,
  type FieldKey,
  type FormErrors,
  type FormValues,
} from '@/lib/settings-utils';
import { SAVE_SETTINGS_KEY, sentFields, useSaveSettings } from '@/lib/use-save-settings';
import type { SettingsIn, SwingSettings } from '@/lib/swing-types';

/** Where focus goes once a save attempt is over (the Save button is off while it runs). */
type FocusNext = 'saved' | 'field' | 'error';

function NumberField({
  name,
  value,
  error,
  onChange,
}: {
  name: FieldKey;
  value: string;
  error: string | undefined;
  onChange: (text: string) => void;
}) {
  const text = SETTINGS_FIELD_TEXT[name];
  const whole = fieldKind(name) === 'count';
  return (
    <Field label={text.label} hint={text.hint} error={error}>
      {(a) => (
        <input
          {...a}
          name={name}
          type="number"
          inputMode={whole ? 'numeric' : 'decimal'}
          step={whole ? '1' : 'any'}
          min="0"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={INPUT_CLASS}
        />
      )}
    </Field>
  );
}

/**
 * The number settings: account and risk limits, and the two universe floors. Percents are typed
 * as % and the dollar volume in millions (`settings-utils` converts both ways); a save sends only
 * the fields that changed. Client checks use the server's bounds; a 422 shows its messages next
 * to the fields they name, and any other beside the Save button.
 */
export function SettingsForm({
  saved,
  onSaved,
  onDirtyChange,
}: {
  /** The settings as last loaded or saved. When they are replaced while the boxes still hold
   * what the old ones showed, the boxes follow; otherwise what is typed is kept. */
  saved: SwingSettings;
  /** The server's answer to a save, for the fields that save named. */
  onSaved: (patch: SettingsIn, response: SwingSettings) => void;
  /** Whether any box holds something other than what `saved` shows (fresh data must not
   * replace the form while it does). */
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [values, setValues] = useState<FormValues>(() => textsFrom(saved));
  const [submitted, setSubmitted] = useState(false);
  const [serverErrors, setServerErrors] = useState<FormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);
  const [focusNext, setFocusNext] = useState<FocusNext | null>(null);

  const save = useSaveSettings();
  const saving = save.isPending;
  // The digest toggle saves through the same route; only one save at a time.
  const otherSaving = useIsMutating({ mutationKey: SAVE_SETTINGS_KEY }) > (saving ? 1 : 0);

  const formRef = useRef<HTMLFormElement>(null);
  const savedRef = useRef<HTMLSpanElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  // Tell the page whether anything has been typed.
  const dirty = !isPristine(values, saved);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  // New stored settings (the page applies fresh data only while nothing is typed). A box that still
  // holds what the old settings showed takes the new text; one with typing in it is left alone.
  const previous = useRef(saved);
  useEffect(() => {
    const before = previous.current;
    if (before === saved) return;
    previous.current = saved;
    setValues((all) => (isPristine(all, before) ? textsFrom(saved) : all));
  }, [saved]);

  const changed = useMemo(() => changedKeys(values, saved), [values, saved]);
  const { errors: clientErrors } = useMemo(() => checkValues(values), [values]);
  const showUniverseWarning = universeDiffers(values, saved);

  // Server messages first (they are about what was just sent), then the client's own checks once
  // a save was tried.
  const errorFor = (key: FieldKey): string | undefined =>
    serverErrors[key] ?? (submitted ? clientErrors[key] : undefined);

  // The button that was pressed is off while the save runs, so focus is handed on when it ends:
  // to the "Settings saved" line, the first field with a message, or the form-level message.
  useEffect(() => {
    if (focusNext === null || saving) return;
    if (focusNext === 'saved') savedRef.current?.focus();
    else if (focusNext === 'field') {
      formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
    } else errorRef.current?.focus();
    setFocusNext(null);
  }, [focusNext, saving]);

  const edit = (key: FieldKey, text: string) => {
    setValues((all) => ({ ...all, [key]: text }));
    setJustSaved(false);
    if (serverErrors[key] !== undefined) {
      setServerErrors((all) => {
        const next = { ...all };
        delete next[key];
        return next;
      });
    }
    if (formError !== null) setFormError(null);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    setJustSaved(false);
    setServerErrors({});
    setFormError(null);
    if (changed.length === 0) return;
    const patch = buildPatch(values, saved);
    if (patch === null) {
      setFocusNext('field');
      return;
    }
    save.mutate(patch, {
      onSuccess: (response) => {
        onSaved(patch, response);
        // Show what the server kept for the fields just saved; leave any other text alone.
        const sent = sentFields(patch, response);
        const fresh = textsFrom({ ...saved, ...sent });
        setValues((all) => {
          const next = { ...all };
          for (const key of Object.keys(patch) as FieldKey[]) next[key] = fresh[key];
          return next;
        });
        setSubmitted(false);
        setJustSaved(true);
        setFocusNext('saved');
      },
      onError: (err) => {
        const split = splitIssues(err);
        if (split === null) {
          setFormError(errorText(err));
          setFocusNext('error');
          return;
        }
        setServerErrors(split.fields);
        setFormError(split.general);
        setFocusNext(Object.keys(split.fields).length > 0 ? 'field' : 'error');
      },
    });
  };

  const hasChanges = changed.length > 0;
  const hasMessages =
    Object.keys(serverErrors).length > 0 || (submitted && Object.keys(clientErrors).length > 0);
  const statusText = justSaved
    ? SETTINGS_SAVED_TEXT
    : hasMessages && hasChanges
      ? SETTINGS_FIX_FIELDS_TEXT
      : !hasChanges
        ? SETTINGS_NO_CHANGES_TEXT
        : '';

  const fields = (keys: readonly FieldKey[]) =>
    keys.map((key) => (
      <NumberField
        key={key}
        name={key}
        value={values[key]}
        error={errorFor(key)}
        onChange={(text) => edit(key, text)}
      />
    ));

  return (
    <form
      ref={formRef}
      onSubmit={submit}
      noValidate
      aria-label="Account, risk and universe settings"
      className="space-y-6"
    >
      {formError !== null && (
        <p
          ref={errorRef}
          role="alert"
          tabIndex={-1}
          className="break-words rounded-lg border border-bear/50 bg-bear/10 px-4 py-3 text-sm text-bear outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          {formError}
        </p>
      )}

      <fieldset disabled={saving} className="m-0 min-w-0 space-y-6 border-0 p-0">
        <section aria-labelledby="account-heading" className="space-y-3">
          <SectionHeading id="account-heading">{SETTINGS_ACCOUNT_HEADING}</SectionHeading>
          <div className="grid gap-4 rounded-lg border border-line bg-bg-surface p-4 sm:grid-cols-2">
            {fields(ACCOUNT_FIELDS)}
          </div>
        </section>

        <section aria-labelledby="universe-heading" className="space-y-3">
          <SectionHeading id="universe-heading">{SETTINGS_UNIVERSE_HEADING}</SectionHeading>
          <div className="grid gap-4 rounded-lg border border-line bg-bg-surface p-4 sm:grid-cols-2">
            {fields(UNIVERSE_FIELDS)}
          </div>
          {showUniverseWarning && (
            <p className="rounded-lg border border-warn/50 bg-warn/10 px-4 py-3 text-sm text-warn">
              {UNIVERSE_WARNING_TEXT}
            </p>
          )}
        </section>
      </fieldset>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="submit"
          disabled={saving || otherSaving || !hasChanges}
          className={PRIMARY_BUTTON_CLASS}
        >
          {saving ? SETTINGS_SAVING_LABEL : SETTINGS_SAVE_LABEL}
        </button>
        {/* Always present, so a screen reader announces the text when it changes. */}
        <span
          ref={savedRef}
          role="status"
          tabIndex={-1}
          className="text-sm text-ink-muted outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          {statusText}
        </span>
      </div>
    </form>
  );
}
