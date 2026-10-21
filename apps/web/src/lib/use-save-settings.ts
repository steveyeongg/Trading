import { useMutation, useQueryClient } from '@tanstack/react-query';
import { swingApi } from './swing-api';
import type { SettingsIn, SwingSettings } from './swing-types';

/** The mutation key of every settings save, so a form can tell that another save is in flight. */
export const SAVE_SETTINGS_KEY = ['save-settings'] as const;

// Settings change the share counts on Today, the track record's universe warning and the open
// positions' risk, so a save refetches all of them.
const AFFECTED_KEYS = [['settings'], ['today'], ['track-record'], ['positions', 'open']] as const;

/** Which of `saved`'s fields a request named: the only ones the response may overwrite. */
export function sentFields(patch: SettingsIn, saved: SwingSettings): Partial<SwingSettings> {
  const out: Partial<SwingSettings> = {};
  for (const key of Object.keys(patch) as (keyof SwingSettings)[]) {
    Object.assign(out, { [key]: saved[key] });
  }
  return out;
}

/**
 * `PUT /v1/settings` with the fields to change. A 422 arrives as a `ValidationError` (see
 * `splitIssues`). On success it refetches what the settings affect; the caller hands the server's
 * answer to its own state.
 */
export function useSaveSettings() {
  const queryClient = useQueryClient();
  return useMutation<SwingSettings, unknown, SettingsIn>({
    mutationKey: SAVE_SETTINGS_KEY,
    mutationFn: (patch) => swingApi.saveSettings(patch),
    onSuccess: () => {
      for (const queryKey of AFFECTED_KEYS) void queryClient.invalidateQueries({ queryKey });
    },
  });
}
