'use client';

import { useQuery } from '@tanstack/react-query';
import { useId, useMemo, useState } from 'react';
import { ApiDown } from '@/components/ApiDown';
import {
  EmptyState,
  NO_SETUP_PASSED_TEXT,
  ScanFailedBanner,
} from '@/components/EmptyState';
import {
  ALL_SETUPS,
  DEFAULT_FILTERS,
  IdeasFilters,
  IdeasTable,
  filterIdeas,
  setupOptions,
  type IdeaFilters,
} from '@/components/IdeasTable';
import { PositionActionsCard } from '@/components/PositionActionsCard';
import { RegimeBanner } from '@/components/RegimeBanner';
import { errorText } from '@/lib/error-text';
import { money } from '@/lib/format';
import { isUnreachable, swingApi } from '@/lib/swing-api';
import type { IdeaOut, TodayOut } from '@/lib/swing-types';

const LOADING_TEXT = "Loading today's ideas…";
const DISCLAIMER_TEXT = 'Informational only, not financial advice. You place every order yourself.';

const REFRESH_MS = 60_000; // normally: once a minute
const REFRESH_WHILE_RUNNING_MS = 5_000; // while a scan runs

const HEADING = 'text-sm font-semibold uppercase tracking-wider';

/** "(14)", or "(10 of 14)" when the filters hide some. */
const countText = (shown: number, all: number) => (shown === all ? `(${all})` : `(${shown} of ${all})`);

function FilteredOut({ hidden, onReset }: { hidden: number; onReset: () => void }) {
  return (
    <p className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted">
      {`No ideas match these filters (${hidden} hidden). `}
      <button type="button" onClick={onReset} className="text-accent hover:underline">
        Show all
      </button>
    </p>
  );
}

/** The ideas of setups that have not passed testing: collapsible, muted, and clearly labelled. */
function UnprovenSection({
  all,
  shown,
  expanded,
  onToggle,
  onReset,
}: {
  all: IdeaOut[];
  shown: IdeaOut[];
  expanded: boolean;
  onToggle: () => void;
  onReset: () => void;
}) {
  const panelId = useId();
  const title = `Unproven ideas: these setups have not passed testing ${countText(shown.length, all.length)}`;
  return (
    <section aria-label="Unproven ideas" className="space-y-3">
      <h2 className="text-sm font-semibold text-ink-muted">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={panelId}
          onClick={onToggle}
          className="flex w-full items-center justify-between gap-3 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          <span>{title}</span>
          <span aria-hidden="true" className="shrink-0 text-xs font-normal text-ink-subtle">
            {expanded ? 'Hide ▴' : 'Show ▾'}
          </span>
        </button>
      </h2>
      <div id={panelId} hidden={!expanded} className="space-y-3">
        {shown.length > 0 ? (
          <IdeasTable ideas={shown} kind="unproven" label="Unproven ideas" />
        ) : (
          <FilteredOut hidden={all.length} onReset={onReset} />
        )}
      </div>
    </section>
  );
}

function TodayView({ today }: { today: TodayOut }) {
  const [filters, setFilters] = useState<IdeaFilters>(DEFAULT_FILTERS);
  // The user's own choice for the Unproven section; until they make one, it follows the data.
  const [unprovenChoice, setUnprovenChoice] = useState<boolean | null>(null);

  const options = useMemo(
    () => setupOptions(today.ideas, today.unproven, today.watch_only),
    [today.ideas, today.unproven, today.watch_only],
  );
  // A setup filter whose family has gone from the data counts as "All".
  const active: IdeaFilters = options.some((o) => o.value === filters.setup)
    ? filters
    : { ...filters, setup: ALL_SETUPS };
  const validated = useMemo(() => filterIdeas(today.ideas, active), [today.ideas, active]);
  const unproven = useMemo(() => filterIdeas(today.unproven, active), [today.unproven, active]);
  const reset = () => setFilters({ setup: ALL_SETUPS, minPrice: 0 });

  const firstRun = today.empty_reason === 'first_run';
  const hasLive = today.ideas.length > 0;
  // Unproven ideas lead the page when nothing is validated; otherwise they stay out of the way.
  const unprovenExpanded = unprovenChoice ?? !hasLive;
  const { capacity } = today;

  return (
    <div className="space-y-6">
      <RegimeBanner today={today} />
      {today.empty_reason === 'last_scan_failed' && <ScanFailedBanner freshness={today.freshness} />}
      <PositionActionsCard actions={today.positions} openPositions={capacity.open_positions} />
      <EmptyState today={today} />

      {!firstRun && (
        <>
          <section aria-labelledby="ideas-heading" className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
              <h2 id="ideas-heading" className={HEADING}>
                {`TRADE IDEAS FOR NEXT SESSION ${countText(validated.length, today.ideas.length)}`}
              </h2>
              {(today.ideas.length > 0 || today.unproven.length > 0) && (
                <IdeasFilters filters={active} options={options} onChange={setFilters} />
              )}
            </div>
            {!hasLive ? (
              <p className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted">
                {NO_SETUP_PASSED_TEXT}
              </p>
            ) : validated.length > 0 ? (
              <IdeasTable ideas={validated} kind="validated" label="Trade ideas for the next session" />
            ) : (
              <FilteredOut hidden={today.ideas.length} onReset={reset} />
            )}
          </section>

          {today.watch_only.length > 0 && (
            <section aria-labelledby="watch-heading" className="space-y-3">
              <div>
                <h2 id="watch-heading" className={HEADING}>
                  {`WATCH-ONLY NAMES (${today.watch_only.length})`}
                </h2>
                <p className="mt-1 text-xs text-ink-muted">
                  These are not buy ideas tonight, so no share count or risk is shown.
                </p>
              </div>
              <IdeasTable ideas={today.watch_only} kind="watch" label="Watch-only names" />
            </section>
          )}

          {today.unproven.length > 0 && (
            <UnprovenSection
              all={today.unproven}
              shown={unproven}
              expanded={unprovenExpanded}
              onToggle={() => setUnprovenChoice(!unprovenExpanded)}
              onReset={reset}
            />
          )}
        </>
      )}

      <p className="text-sm text-ink-muted">
        {`${capacity.open_positions} of ${capacity.max_positions} positions open · open risk ${money(
          capacity.open_risk_dollars,
        )} of ${money(capacity.max_risk_dollars)}`}
      </p>
    </div>
  );
}

function LoadFailure({
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

export default function TodayPage() {
  const { data, error, isPending, isFetching, refetch } = useQuery<TodayOut>({
    queryKey: ['today'],
    queryFn: () => swingApi.today(),
    // Once a minute, every few seconds while a scan runs, and whenever the tab is focused again.
    refetchInterval: (query) =>
      query.state.data?.freshness.last_scan_status === 'running'
        ? REFRESH_WHILE_RUNNING_MS
        : REFRESH_MS,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });

  return (
    <div className="space-y-6">
      <h1 className="sr-only">Today</h1>
      {error !== null && (
        <LoadFailure error={error} retrying={isFetching} onRetry={() => void refetch()} />
      )}
      {data !== undefined ? (
        <TodayView today={data} />
      ) : (
        isPending && <p className="text-sm text-ink-muted">{LOADING_TEXT}</p>
      )}
      <p className="border-t border-line pt-4 text-xs text-ink-subtle">{DISCLAIMER_TEXT}</p>
    </div>
  );
}
