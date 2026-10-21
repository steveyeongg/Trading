import { useQuery } from '@tanstack/react-query';
import { isUnreachable, swingApi } from './swing-api';
import type { StatusOut } from './swing-types';

const REFRESH_MS = 15_000; // normally: every 15 seconds
const REFRESH_WHILE_RUNNING_MS = 5_000; // while a scan runs

/**
 * `GET /v1/system/status`, kept fresh: every 15 s, every 5 s while the last job is running, and
 * when the tab is focused again. The status panel and the Telegram test button on Settings both
 * read it, under the one `['status']` key, so it is fetched once.
 */
export function useSystemStatus() {
  return useQuery<StatusOut>({
    queryKey: ['status'],
    queryFn: () => swingApi.status(),
    refetchInterval: (query) =>
      query.state.data?.last_scan?.status === 'running' ? REFRESH_WHILE_RUNNING_MS : REFRESH_MS,
    refetchOnWindowFocus: true,
    staleTime: 0,
    // Only a request that never reached the API is worth one more try.
    retry: (failures, err) => failures < 1 && isUnreachable(err),
  });
}
