import { ApiDown } from '@/components/ApiDown';
import { errorText } from '@/lib/error-text';
import { isUnreachable } from '@/lib/swing-api';

/**
 * A request that failed, with a Retry button. An unreachable API gets the standard "can't reach
 * the API" notice; anything else shows the server's own reason in a bordered notice.
 */
export function LoadFailure({
  error,
  retrying,
  onRetry,
}: {
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
}) {
  const retry = (
    <button
      type="button"
      onClick={onRetry}
      disabled={retrying}
      className="rounded border border-line-strong px-3 py-1.5 text-sm text-ink hover:bg-bg-subtle disabled:cursor-not-allowed disabled:opacity-50"
    >
      {retrying ? 'Retrying…' : 'Retry'}
    </button>
  );
  if (isUnreachable(error)) {
    return (
      <div className="space-y-3">
        <ApiDown />
        {retry}
      </div>
    );
  }
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded border border-warn/40 bg-bg-surface px-4 py-3 text-sm text-ink"
    >
      <span className="min-w-0 break-words">{errorText(error)}</span>
      {retry}
    </div>
  );
}
