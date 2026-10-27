'use client';

import { useQuery } from '@tanstack/react-query';
import { LoadFailure } from '@/components/LoadFailure';
import { SetupRecordCard } from '@/components/SetupRecordCard';
import {
  DISCLAIMER_TEXT,
  TRACK_RECORD_EMPTY_TEXT,
  TRACK_RECORD_INTRO_TEXT,
  TRACK_RECORD_LOADING_TEXT,
} from '@/lib/copy';
import { isUnreachable, swingApi } from '@/lib/swing-api';
import type { TrackRecordOut } from '@/lib/swing-types';

const REFRESH_MS = 10 * 60_000; // the reports are committed files; the live record moves once a night

export default function TrackRecordPage() {
  const { data, error, isPending, isFetching, refetch } = useQuery<TrackRecordOut>({
    queryKey: ['track-record'],
    queryFn: () => swingApi.trackRecord(),
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
    // Only a request that never reached the API is worth one more try; the Retry button asks
    // again for any other failure.
    retry: (failures, err) => failures < 1 && isUnreachable(err),
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Track record</h1>
        <p className="mt-1 max-w-3xl text-sm text-ink-muted">{TRACK_RECORD_INTRO_TEXT}</p>
      </header>

      {error !== null && (
        <LoadFailure error={error} retrying={isFetching} onRetry={() => void refetch()} />
      )}

      {data !== undefined ? (
        data.families.length > 0 ? (
          <div className="space-y-6">
            {data.families.map((family) => (
              <SetupRecordCard key={family.family} family={family} />
            ))}
          </div>
        ) : (
          <p className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted">
            {TRACK_RECORD_EMPTY_TEXT}
          </p>
        )
      ) : (
        isPending && <p className="text-sm text-ink-muted">{TRACK_RECORD_LOADING_TEXT}</p>
      )}

      <p className="border-t border-line pt-4 text-xs text-ink-subtle">{DISCLAIMER_TEXT}</p>
    </div>
  );
}
