'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useMemo, useState, type FormEvent } from 'react';
import { ClosePositionForm } from '@/components/ClosePositionForm';
import { LoadFailure } from '@/components/LoadFailure';
import { Field, PositionForm } from '@/components/PositionForm';
import { SectionHeading } from '@/components/SectionHeading';
import { Term } from '@/components/Term';
import {
  DISCLAIMER_TEXT,
  NO_ACTION_YET_TEXT,
  NO_CLOSED_POSITIONS_TEXT,
  NO_OPEN_POSITIONS_TEXT,
  POSITIONS_INTRO_TEXT,
  POSITIONS_LOADING_TEXT,
  STOP_EDIT_HINT,
  STOP_INVALID_TEXT,
  STOP_SAVED_TEXT,
  STOP_UNCHANGED_TEXT,
  exitReasonLabel,
} from '@/lib/copy';
import { errorText } from '@/lib/error-text';
import { price, r, sessionDate } from '@/lib/format';
import {
  INPUT_CLASS,
  PRIMARY_BUTTON_CLASS,
  SECONDARY_BUTTON_CLASS,
  actionTone,
  parseAmount,
  plainNumber,
  rTone,
} from '@/lib/position-utils';
import { isUnreachable, swingApi } from '@/lib/swing-api';
import type { PositionFilter, PositionOut } from '@/lib/swing-types';

const REFRESH_MS = 5 * 60_000; // positions change once a night, or when the user acts
const HIGHLIGHT_MS = 6_000; // how long a new (or pointed-at) row stays highlighted

const quantity = new Intl.NumberFormat('en-US', { maximumFractionDigits: 4 });

const TH = 'px-3 py-2 font-medium';
const TD = 'px-3 py-2.5 align-top';
const EMPTY_CLASS =
  'rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted';

function usePositions(status: PositionFilter) {
  return useQuery<PositionOut[]>({
    queryKey: ['positions', status],
    queryFn: () => swingApi.positions(status),
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
    staleTime: 0,
    // Only a request that never reached the API is worth one more try.
    retry: (failures, err) => failures < 1 && isUnreachable(err),
  });
}

/** The stock's symbol (a link to its page) with its name under it. */
function StockCell({ position }: { position: PositionOut }) {
  return (
    <>
      <Link
        href={`/stocks/${encodeURIComponent(position.symbol)}`}
        className="rounded font-mono font-semibold hover:text-accent focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
      >
        {position.symbol}
      </Link>
      {position.name !== null && position.name !== '' && (
        <span className="block max-w-[14rem] truncate text-xs text-ink-muted">{position.name}</span>
      )}
    </>
  );
}

// --- edit the stop -------------------------------------------------------------------------------

function StopEditor({
  position,
  onSaved,
  onCancel,
}: {
  position: PositionOut;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const current = position.current_stop ?? position.initial_stop;
  const [value, setValue] = useState(() => plainNumber(current));
  const [submitted, setSubmitted] = useState(false);

  const save = useMutation<PositionOut, unknown, number>({
    mutationFn: (stop) => swingApi.updatePosition(position.id, { current_stop: stop }),
    onSuccess: () => onSaved(),
  });

  const amount = parseAmount(value);
  const problem =
    amount === null || amount <= 0
      ? STOP_INVALID_TEXT
      : amount === current
        ? STOP_UNCHANGED_TEXT
        : undefined;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (problem !== undefined || amount === null) return;
    save.mutate(amount);
  };

  return (
    <form onSubmit={submit} noValidate aria-label={`Edit the stop for ${position.symbol}`}>
      <div className="flex flex-wrap items-start gap-2">
        <div className="w-40">
          <Field label="New stop" error={submitted ? problem : undefined}>
            {(a) => (
              <input
                {...a}
                type="number"
                inputMode="decimal"
                step="any"
                min="0"
                value={value}
                onChange={(e) => {
                  if (save.isError) save.reset();
                  setValue(e.target.value);
                }}
                className={INPUT_CLASS}
              />
            )}
          </Field>
        </div>
        {/* sm:pt-5 lines the buttons up with the input (the label above it is 1.25rem tall). */}
        <div className="flex flex-wrap gap-2 sm:pt-5">
          <button type="submit" disabled={save.isPending} className={PRIMARY_BUTTON_CLASS}>
            {save.isPending ? 'Saving…' : 'Save stop'}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={save.isPending}
            className={SECONDARY_BUTTON_CLASS}
          >
            Cancel
          </button>
        </div>
      </div>
      <p className="mt-1 text-xs text-ink-muted">{STOP_EDIT_HINT}</p>
      {save.isError && (
        <p role="alert" className="mt-2 break-words text-sm text-bear">
          {errorText(save.error)}
        </p>
      )}
    </form>
  );
}

// --- the open table ------------------------------------------------------------------------------

type Mode = 'idle' | 'stop' | 'close';

/** One open position: its row, and under it the controls (edit the stop, close). */
function OpenPositionRows({
  position,
  highlighted,
  onChanged,
  onClosed,
  onDone,
}: {
  position: PositionOut;
  highlighted: boolean;
  /** The stop changed: refetch the positions and Today. */
  onChanged: () => void;
  onClosed: (closed: PositionOut) => void;
  onDone: (id: string) => void;
}) {
  const [mode, setMode] = useState<Mode>('idle');
  const [stopSaved, setStopSaved] = useState(false);
  const stop = position.current_stop ?? position.initial_stop;
  const raised = stop !== position.initial_stop;

  return (
    // One <tbody> per position, so its two rows share a border and a highlight.
    <tbody
      id={`position-${position.id}`}
      className={`scroll-mt-24 border-t border-line transition-colors ${
        highlighted ? 'bg-accent/10' : ''
      }`}
    >
      <tr>
        <td className={TD}>
          <StockCell position={position} />
        </td>
        <td className={`${TD} whitespace-nowrap`}>{sessionDate(position.opened_on)}</td>
        <td className={`${TD} text-right tabular-nums`}>{price(position.entry_price)}</td>
        <td className={`${TD} text-right tabular-nums`}>{quantity.format(position.shares)}</td>
        <td className={`${TD} text-right tabular-nums`}>
          {price(stop)}
          {raised && (
            <span className="block text-xs text-ink-muted">
              {`from ${price(position.initial_stop)}`}
            </span>
          )}
        </td>
        <td className={`${TD} text-right tabular-nums`}>
          {r(position.r_now)}
          {position.last_close !== null && (
            <span className="block text-xs text-ink-muted">{price(position.last_close)}</span>
          )}
        </td>
        <td className={`${TD} min-w-[14rem]`}>
          {position.last_action_text !== null && position.last_action_text !== '' ? (
            <>
              <span className={`break-words ${actionTone(position.last_action)}`}>
                {position.last_action_text}
              </span>
              {position.last_action_on !== null && (
                <span className="block text-xs text-ink-muted">
                  {sessionDate(position.last_action_on)}
                </span>
              )}
            </>
          ) : (
            <span className="text-ink-muted">{NO_ACTION_YET_TEXT}</span>
          )}
        </td>
      </tr>
      <tr>
        <td colSpan={7} className="px-3 pb-3">
          {/* Sticks to the left edge while the table scrolls sideways, and is never wider than the
              screen, so a form stacks and stays in view on a phone. */}
          <div className="sticky left-0 w-[calc(100vw-4rem)] max-w-3xl">
            {mode === 'idle' && (
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setStopSaved(false);
                    setMode('stop');
                  }}
                  aria-label={`Edit the stop for ${position.symbol}`}
                  className={SECONDARY_BUTTON_CLASS}
                >
                  Edit stop
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setStopSaved(false);
                    setMode('close');
                  }}
                  aria-label={`Close the position in ${position.symbol}`}
                  className={SECONDARY_BUTTON_CLASS}
                >
                  Close position
                </button>
                {stopSaved && (
                  <span role="status" className="text-sm text-ink-muted">
                    {STOP_SAVED_TEXT}
                  </span>
                )}
              </div>
            )}
            {mode === 'stop' && (
              <StopEditor
                position={position}
                onSaved={() => {
                  setStopSaved(true);
                  setMode('idle');
                  onChanged();
                }}
                onCancel={() => setMode('idle')}
              />
            )}
            {mode === 'close' && (
              <ClosePositionForm
                position={position}
                onClosed={onClosed}
                onDone={() => onDone(position.id)}
                onCancel={() => setMode('idle')}
              />
            )}
          </div>
        </td>
      </tr>
    </tbody>
  );
}

function OpenTable({
  rows,
  highlightId,
  onChanged,
  onClosed,
  onDone,
}: {
  rows: PositionOut[];
  highlightId: string | null;
  onChanged: () => void;
  onClosed: (closed: PositionOut) => void;
  onDone: (id: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-bg-surface">
      <table className="w-full min-w-[56rem] border-collapse text-sm">
        <caption className="sr-only">Open positions</caption>
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-ink-muted">
            <th scope="col" className={TH}>
              Stock
            </th>
            <th scope="col" className={TH}>
              Opened
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Entry
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Shares
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Stop
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Now (<Term k="R">R</Term>)
            </th>
            <th scope="col" className={TH}>
              Latest action
            </th>
          </tr>
        </thead>
        {rows.map((position) => (
          <OpenPositionRows
            key={position.id}
            position={position}
            highlighted={position.id === highlightId}
            onChanged={onChanged}
            onClosed={onClosed}
            onDone={onDone}
          />
        ))}
      </table>
    </div>
  );
}

// --- the closed table ----------------------------------------------------------------------------

/** Newest closed first; a trade closed the same day sorts by its open date. */
function newestClosedFirst(rows: PositionOut[]): PositionOut[] {
  const key = (p: PositionOut) => `${p.closed_on ?? ''}|${p.opened_on}`;
  return [...rows].sort((a, b) => (key(a) < key(b) ? 1 : key(a) > key(b) ? -1 : 0));
}

function ClosedTable({ rows }: { rows: PositionOut[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-bg-surface">
      <table className="w-full min-w-[48rem] border-collapse text-sm">
        <caption className="sr-only">Closed trades</caption>
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-ink-muted">
            <th scope="col" className={TH}>
              Stock
            </th>
            <th scope="col" className={TH}>
              Opened
            </th>
            <th scope="col" className={TH}>
              Closed
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Entry
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Exit
            </th>
            <th scope="col" className={`${TH} text-right`}>
              <Term k="R">R</Term>
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Held (sessions)
            </th>
            <th scope="col" className={TH}>
              Reason
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.id} className="border-t border-line">
              <td className={TD}>
                <StockCell position={p} />
              </td>
              <td className={`${TD} whitespace-nowrap`}>{sessionDate(p.opened_on)}</td>
              <td className={`${TD} whitespace-nowrap`}>{sessionDate(p.closed_on)}</td>
              <td className={`${TD} text-right tabular-nums`}>{price(p.entry_price)}</td>
              <td className={`${TD} text-right tabular-nums`}>{price(p.exit_price)}</td>
              <td className={`${TD} text-right font-medium tabular-nums ${rTone(p.r_multiple)}`}>
                {r(p.r_multiple)}
              </td>
              <td className={`${TD} text-right tabular-nums`}>{p.held_sessions ?? '—'}</td>
              <td className={TD}>{exitReasonLabel(p.exit_reason)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- the page ------------------------------------------------------------------------------------

function PositionsView() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  // "I bought this" on a stock page arrives as /positions?new=SYM&idea=ID.
  const newSymbol = (searchParams.get('new') ?? '').trim().toUpperCase();
  const ideaParam = (searchParams.get('idea') ?? '').trim();

  const open = usePositions('open');
  const closed = usePositions('closed');

  const [formOpen, setFormOpen] = useState(newSymbol !== '');
  useEffect(() => {
    if (newSymbol !== '') setFormOpen(true);
  }, [newSymbol]);

  // The row to point at: a new position, or the one a "you already have one" answer is about.
  // `n` makes pointing at the same row twice scroll twice.
  const [highlight, setHighlight] = useState<{ id: string; n: number } | null>(null);
  const openData = open.data;
  useEffect(() => {
    if (highlight === null) return;
    const row = document.getElementById(`position-${highlight.id}`);
    if (row === null) return; // not drawn yet: this runs again when the data arrives
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const timer = window.setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [highlight, openData]);

  // Positions just closed in this visit. Each stays in the open table, showing its result in R,
  // until the user presses Done (`done`), even if a refetch has already dropped it from the open
  // list.
  const [justClosed, setJustClosed] = useState<
    Record<string, { position: PositionOut; done: boolean }>
  >({});

  const refresh = (keys: readonly (readonly string[])[]) =>
    Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
  const refreshAll = () =>
    refresh([['positions', 'open'], ['positions', 'closed'], ['today']]);

  const openRows = useMemo(() => {
    const list = openData ?? [];
    const listed = new Set(list.map((p) => p.id));
    const kept = Object.values(justClosed)
      .filter((c) => !listed.has(c.position.id))
      .map((c) => c.position);
    return [...list, ...kept].filter((p) => justClosed[p.id]?.done !== true);
  }, [openData, justClosed]);
  // The heading counts open positions, not the ones just closed that are still showing a result.
  const openCount = openRows.filter((p) => justClosed[p.id] === undefined).length;
  const closedRows = useMemo(() => newestClosedFirst(closed.data ?? []), [closed.data]);

  const leaveForm = () => {
    setFormOpen(false);
    if (newSymbol !== '' || ideaParam !== '') router.replace('/positions');
  };

  const failure = open.error ?? closed.error;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Positions</h1>
          <p className="mt-1 max-w-2xl text-sm text-ink-muted">{POSITIONS_INTRO_TEXT}</p>
        </div>
        {!formOpen && (
          <button type="button" onClick={() => setFormOpen(true)} className={PRIMARY_BUTTON_CLASS}>
            Add a position
          </button>
        )}
      </header>

      {failure !== null && (
        <LoadFailure
          error={failure}
          retrying={open.isFetching || closed.isFetching}
          onRetry={() => {
            void open.refetch();
            void closed.refetch();
          }}
        />
      )}

      {formOpen && (
        <PositionForm
          key={`${newSymbol}|${ideaParam}`}
          initialSymbol={newSymbol}
          ideaId={ideaParam === '' ? null : ideaParam}
          open={openData ?? []}
          onCancel={leaveForm}
          onShowExisting={(id) => setHighlight({ id, n: Date.now() })}
          onCreated={(created) => {
            setFormOpen(false);
            router.replace('/positions');
            void refreshAll().then(() => setHighlight({ id: created.id, n: Date.now() }));
          }}
        />
      )}

      <section aria-labelledby="open-heading" className="space-y-3">
        <SectionHeading id="open-heading">
          {`OPEN POSITIONS${openData !== undefined ? ` (${openCount})` : ''}`}
        </SectionHeading>
        {openData === undefined ? (
          open.isPending && <p className="text-sm text-ink-muted">{POSITIONS_LOADING_TEXT}</p>
        ) : openRows.length === 0 ? (
          <p className={EMPTY_CLASS}>{NO_OPEN_POSITIONS_TEXT}</p>
        ) : (
          <OpenTable
            rows={openRows}
            highlightId={highlight?.id ?? null}
            onChanged={() => void refreshAll()}
            onClosed={(closedNow) => {
              setJustClosed((all) => ({
                ...all,
                [closedNow.id]: { position: closedNow, done: false },
              }));
              void refresh([['positions', 'closed'], ['today']]);
            }}
            onDone={(id) => {
              setJustClosed((all) => {
                const entry = all[id];
                return entry === undefined ? all : { ...all, [id]: { ...entry, done: true } };
              });
              void refreshAll();
            }}
          />
        )}
      </section>

      <section aria-labelledby="closed-heading" className="space-y-3">
        <SectionHeading id="closed-heading">
          {`CLOSED TRADES${closed.data !== undefined ? ` (${closedRows.length})` : ''}`}
        </SectionHeading>
        {closed.data === undefined ? (
          closed.isPending && <p className="text-sm text-ink-muted">{POSITIONS_LOADING_TEXT}</p>
        ) : closedRows.length === 0 ? (
          <p className={EMPTY_CLASS}>{NO_CLOSED_POSITIONS_TEXT}</p>
        ) : (
          <ClosedTable rows={closedRows} />
        )}
      </section>

      <p className="border-t border-line pt-4 text-xs text-ink-subtle">{DISCLAIMER_TEXT}</p>
    </div>
  );
}

export default function PositionsPage() {
  // `useSearchParams` needs a Suspense boundary above it, or `next build` fails on this page.
  return (
    <Suspense fallback={<p className="text-sm text-ink-muted">{POSITIONS_LOADING_TEXT}</p>}>
      <PositionsView />
    </Suspense>
  );
}
