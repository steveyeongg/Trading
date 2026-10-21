'use client';

import { useIsMutating, useMutation } from '@tanstack/react-query';
import { useId } from 'react';
import { SectionHeading } from '@/components/SectionHeading';
import {
  SETTINGS_TELEGRAM_HEADING,
  TELEGRAM_DIGEST_LABEL,
  TELEGRAM_DIGEST_NOTE,
  TELEGRAM_NOT_CONFIGURED_HINT,
  TELEGRAM_TESTING_LABEL,
  TELEGRAM_TEST_LABEL,
  telegramTestResultText,
} from '@/lib/copy';
import { errorText } from '@/lib/error-text';
import { SECONDARY_BUTTON_CLASS } from '@/lib/form-styles';
import { swingApi } from '@/lib/swing-api';
import type { SettingsIn, SwingSettings, TelegramTestOut } from '@/lib/swing-types';
import { SAVE_SETTINGS_KEY, useSaveSettings } from '@/lib/use-save-settings';
import { useSystemStatus } from '@/lib/use-system-status';

/**
 * The nightly digest switch and the "Send test message" button.
 *
 * The switch saves as soon as it is flipped (only that field is sent) and shows the requested
 * value while the save runs; if the save fails it goes back and says why. The test button posts
 * one fixed message and shows the answer inline; it is off while the request runs and while the
 * Telegram keys are not set, which is read from the system status. The API redacts its answer;
 * nothing here reads or logs a token.
 */
export function TelegramSettings({
  saved,
  onSaved,
}: {
  saved: SwingSettings;
  onSaved: (patch: SettingsIn, response: SwingSettings) => void;
}) {
  const noteId = useId();
  const hintId = useId();
  const toggle = useSaveSettings();
  // The form's Save button shares the route, and only one save runs at a time. The switch stays
  // enabled during its own save (so keyboard focus is not lost); a second flip then does nothing.
  const formSaving =
    useIsMutating({ mutationKey: SAVE_SETTINGS_KEY }) - (toggle.isPending ? 1 : 0) > 0;
  const checked = toggle.isPending
    ? (toggle.variables?.telegram_digest ?? saved.telegram_digest)
    : saved.telegram_digest;

  const status = useSystemStatus();
  // Unknown (still loading, or the status failed) counts as set: the server answers a test with
  // the reason when it is not.
  const notConfigured = status.data?.telegram_configured === false;
  const test = useMutation<TelegramTestOut>({ mutationFn: () => swingApi.telegramTest() });

  return (
    <section aria-labelledby="telegram-heading" className="space-y-3">
      <SectionHeading id="telegram-heading">{SETTINGS_TELEGRAM_HEADING}</SectionHeading>
      <div className="space-y-4 rounded-lg border border-line bg-bg-surface p-4 text-sm">
        <div>
          <label className="flex items-start gap-3">
            <input
              type="checkbox"
              checked={checked}
              disabled={formSaving}
              aria-describedby={noteId}
              aria-busy={toggle.isPending}
              onChange={(e) => {
                if (toggle.isPending) return;
                toggle.reset();
                toggle.mutate(
                  { telegram_digest: e.target.checked },
                  { onSuccess: (response, patch) => onSaved(patch, response) },
                );
              }}
              className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
            />
            <span>{TELEGRAM_DIGEST_LABEL}</span>
          </label>
          <p id={noteId} className="mt-1 pl-7 text-xs text-ink-muted">
            {TELEGRAM_DIGEST_NOTE}
          </p>
          {toggle.isError && (
            <p role="alert" className="mt-2 break-words pl-7 text-sm text-bear">
              {errorText(toggle.error)}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <button
              type="button"
              disabled={test.isPending || notConfigured}
              aria-describedby={notConfigured ? hintId : undefined}
              onClick={() => test.mutate()}
              className={SECONDARY_BUTTON_CLASS}
            >
              {test.isPending ? TELEGRAM_TESTING_LABEL : TELEGRAM_TEST_LABEL}
            </button>
            {/* Always present, so a screen reader announces the answer when it arrives. */}
            <span
              role="status"
              className={`min-w-0 break-words text-sm ${
                test.isError || test.data?.ok === false ? 'text-warn' : 'text-ink-muted'
              }`}
            >
              {test.isError
                ? errorText(test.error)
                : test.data !== undefined
                  ? telegramTestResultText(test.data.ok, test.data.detail)
                  : ''}
            </span>
          </div>
          {notConfigured && (
            <p id={hintId} className="text-xs text-ink-muted">
              {TELEGRAM_NOT_CONFIGURED_HINT}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
