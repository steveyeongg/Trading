'use client';

import { useQuery } from '@tanstack/react-query';
import { useId, useMemo, useState } from 'react';
import {
  EmptyState,
  NO_SETUP_PASSED_TEXT,
  NO_VALIDATED_IDEAS_TEXT,
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
import { LoadFailure } from '@/components/LoadFailure';
import { PositionActionsCard } from '@/components/PositionActionsCard';
import { RegimeBanner } from '@/components/RegimeBanner';
import { SectionHeading } from '@/components/SectionHeading';
import { DISCLAIMER_TEXT } from '@/lib/copy';
import { money } from '@/lib/format';
import { swingApi } from '@/lib/swing-api';
import type { IdeaOut, TodayOut } from '@/lib/swing-types';

const LOADING_TEXT = "Loading today's ideas…";

const REFRESH_MS = 60_000; // normally: once a minute
const REFRESH_WHILE_RUNNING_MS = 5_000; // while a scan runs

/** "(14)": always the whole list's count, whatever the filters show. */
const totalText = (all: number) => `(${all})`;

/** "showing 10 after filters", only when the filters hide some of the list. */
function FilterNote({ shown, all }: { shown: number; all: number }) {
  if (shown === all) return null;
  return <span className="text-xs font-normal normal-case tracking-normal text-ink-muted">{`showing ${shown} after filters`}</span>;
}

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
  const title = `Unproven ideas: these setups have not passed testing ${totalText(all.length)}`;
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
          <span className="min-w-0">
            {title} <FilterNote shown={shown.length} all={all.length} />
          </span>
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

  // The server already splits live from unproven. As a guard, only a row that is live may sit in
  // the validated table; any row of `ideas` that is not live is listed with the unproven ones.
  const live = useMemo(() => today.ideas.filter((i) => i.live), [today.ideas]);
  const unprovenAll = useMemo(
    () => [...today.unproven, ...today.ideas.filter((i) => !i.live)].sort((a, b) => a.rank - b.rank),
    [today.ideas, today.unproven],
  );

  const options = useMemo(
    () => setupOptions(live, unprovenAll, today.watch_only),
    [live, unprovenAll, today.watch_only],
  );
  // A setup filter whose family has gone from the data counts as "All".
  const active: IdeaFilters = options.some((o) => o.value === filters.setup)
    ? filters
    : { ...filters, setup: ALL_SETUPS };
  const validated = useMemo(() => filterIdeas(live, active), [live, active]);
  const unproven = useMemo(() => filterIdeas(unprovenAll, active), [unprovenAll, active]);
  const reset = () => setFilters({ setup: ALL_SETUPS, minPrice: 0 });

  const firstRun = today.empty_reason === 'first_run';
  const hasLive = live.length > 0;
  // The Unproven section leads the page only when no setup at all has passed testing; when one
  // has (even if it found nothing tonight), it stays collapsed until the user opens it.
  const unprovenExpanded = unprovenChoice ?? !today.any_live_setup;
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
              <SectionHeading id="ideas-heading">
                {`TRADE IDEAS FOR NEXT SESSION ${totalText(live.length)}`}{' '}
                <FilterNote shown={validated.length} all={live.length} />
              </SectionHeading>
              {(live.length > 0 || unprovenAll.length > 0) && (
                <IdeasFilters filters={active} options={options} onChange={setFilters} />
              )}
            </div>
            {!hasLive ? (
              <p className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted">
                {today.any_live_setup ? NO_VALIDATED_IDEAS_TEXT : NO_SETUP_PASSED_TEXT}
              </p>
            ) : validated.length > 0 ? (
              <IdeasTable ideas={validated} kind="validated" label="Trade ideas for the next session" />
            ) : (
              <FilteredOut hidden={live.length} onReset={reset} />
            )}
          </section>

          {today.watch_only.length > 0 && (
            <section aria-labelledby="watch-heading" className="space-y-3">
              <div>
                <SectionHeading id="watch-heading">
                  {`WATCH-ONLY NAMES (${today.watch_only.length})`}
                </SectionHeading>
                <p className="mt-1 text-xs text-ink-muted">
                  These are not buy ideas tonight, so no share count or risk is shown.
                </p>
              </div>
              <IdeasTable ideas={today.watch_only} kind="watch" label="Watch-only names" />
            </section>
          )}

          {unprovenAll.length > 0 && (
            <UnprovenSection
              all={unprovenAll}
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
