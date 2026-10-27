'use client';

import { useIsMutating, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { LoadFailure } from '@/components/LoadFailure';
import { SettingsForm } from '@/components/SettingsForm';
import { SystemStatusPanel } from '@/components/SystemStatusPanel';
import { TelegramSettings } from '@/components/TelegramSettings';
import {
  DISCLAIMER_TEXT,
  LEGACY_SETTINGS_LINK_TEXT,
  SETTINGS_INTRO_TEXT,
  SETTINGS_LOADING_TEXT,
} from '@/lib/copy';
import { isUnreachable, swingApi } from '@/lib/swing-api';
import type { SettingsIn, SwingSettings } from '@/lib/swing-types';
import { sameSettings } from '@/lib/settings-utils';
import { SAVE_SETTINGS_KEY, sentFields } from '@/lib/use-save-settings';

/**
 * The settings as last loaded or saved, shared by the form and the digest switch. A save names
 * the fields it changed, and only those take the server's answer, so two saves that cross never
 * undo each other.
 *
 * Fresh data from the server (a retry, or another window's save) replaces the settings only when
 * nothing has been typed in the form and no save is running; otherwise the form keeps what the
 * user is typing. Each arrival is looked at once, by `updatedAt`: the refetch that follows our own
 * save carries the same values, so it changes nothing.
 */
function SettingsView({ settings, updatedAt }: { settings: SwingSettings; updatedAt: number }) {
  const [saved, setSaved] = useState<SwingSettings>(settings);
  const [dirty, setDirty] = useState(false);
  const saving = useIsMutating({ mutationKey: SAVE_SETTINGS_KEY }) > 0;
  const applySaved = (patch: SettingsIn, response: SwingSettings) =>
    setSaved((current) => ({ ...current, ...sentFields(patch, response) }));

  const looked = useRef(updatedAt);
  useEffect(() => {
    if (looked.current === updatedAt) return;
    looked.current = updatedAt;
    if (!dirty && !saving && !sameSettings(settings, saved)) setSaved(settings);
  }, [updatedAt, settings, saved, dirty, saving]);

  return (
    <>
      <SettingsForm saved={saved} onSaved={applySaved} onDirtyChange={setDirty} />
      <TelegramSettings saved={saved} onSaved={applySaved} />
    </>
  );
}

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const settings = useQuery<SwingSettings>({
    queryKey: ['settings'],
    queryFn: () => swingApi.settings(),
    // The form keeps what the user is typing, so no timed or focus refetch (it would only
    // matter if another window saved something else).
    refetchInterval: false,
    refetchOnWindowFocus: false,
    staleTime: 0,
    // Only a request that never reached the API is worth one more try.
    retry: (failures, err) => failures < 1 && isUnreachable(err),
  });

  // With the API down, the one "can't reach" notice above is enough: the status panel would only
  // repeat it. Retry asks for both again.
  const apiDown = settings.data === undefined && isUnreachable(settings.error);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="mt-1 max-w-2xl text-sm text-ink-muted">{SETTINGS_INTRO_TEXT}</p>
      </header>

      {settings.error !== null && (
        <LoadFailure
          error={settings.error}
          retrying={settings.isFetching}
          onRetry={() => {
            void settings.refetch();
            void queryClient.invalidateQueries({ queryKey: ['status'] });
          }}
        />
      )}

      {settings.data !== undefined ? (
        <SettingsView settings={settings.data} updatedAt={settings.dataUpdatedAt} />
      ) : (
        settings.isPending && <p className="text-sm text-ink-muted">{SETTINGS_LOADING_TEXT}</p>
      )}

      {!apiDown && <SystemStatusPanel />}

      <div className="space-y-2 border-t border-line pt-4">
        <p className="text-xs text-ink-subtle">{DISCLAIMER_TEXT}</p>
        <p className="text-xs">
          <Link
            href="/settings/legacy"
            className="rounded text-ink-muted underline hover:text-ink focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
          >
            {LEGACY_SETTINGS_LINK_TEXT}
          </Link>
        </p>
      </div>
    </div>
  );
}
