'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useId, type ReactNode } from 'react';
import { Term } from '@/components/Term';
import { money, pct, price, r } from '@/lib/format';
import type { IdeaOut } from '@/lib/swing-types';

/** Which list a table shows: tested setups, setups that have not passed, or watch-only names. */
export type TableKind = 'validated' | 'unproven' | 'watch';

// --- filters (applied on the client, to the validated and the unproven table) -------------------

export const ALL_SETUPS = 'all';
/** "Any price" is last, so an idea below $10 (a lower price floor in Settings) can still be shown. */
export const MIN_PRICES = [10, 20, 50, 0] as const;

export interface IdeaFilters {
  /** `ALL_SETUPS`, or an idea's `setup` key. */
  setup: string;
  /** Hide ideas whose buy-above price is lower than this; 0 hides none. */
  minPrice: number;
}

export const DEFAULT_FILTERS: IdeaFilters = { setup: ALL_SETUPS, minPrice: 10 };

export interface SetupOption {
  value: string;
  label: string;
}

/** The setup filter's choices: All, then each setup family with the label the data gives it. */
export function setupOptions(...lists: IdeaOut[][]): SetupOption[] {
  const labels = new Map<string, string>([
    ['breakout', 'Breakout'],
    ['pullback', 'Pullback'],
  ]);
  for (const list of lists) for (const idea of list) labels.set(idea.setup, idea.setup_label);
  return [
    { value: ALL_SETUPS, label: 'All setups' },
    ...Array.from(labels, ([value, label]) => ({ value, label })),
  ];
}

/** The ideas that pass the filters. An idea with no buy-above price cannot be compared, so it stays. */
export function filterIdeas(ideas: IdeaOut[], filters: IdeaFilters): IdeaOut[] {
  return ideas.filter(
    (idea) =>
      (filters.setup === ALL_SETUPS || idea.setup === filters.setup) &&
      (idea.buy_above === null || idea.buy_above >= filters.minPrice),
  );
}

const SELECT_CLASS =
  'rounded border border-line bg-bg-subtle px-2 py-1 text-sm text-ink focus:border-accent focus:outline-none';

export function IdeasFilters({
  filters,
  options,
  onChange,
}: {
  filters: IdeaFilters;
  options: SetupOption[];
  onChange: (next: IdeaFilters) => void;
}) {
  const setupId = useId();
  const priceId = useId();
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-ink-muted">filter:</span>
      <label htmlFor={setupId} className="sr-only">
        Setup type
      </label>
      <select
        id={setupId}
        value={filters.setup}
        onChange={(e) => onChange({ ...filters, setup: e.target.value })}
        className={SELECT_CLASS}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <label htmlFor={priceId} className="sr-only">
        Minimum buy-above price
      </label>
      <select
        id={priceId}
        value={filters.minPrice}
        onChange={(e) => onChange({ ...filters, minPrice: Number(e.target.value) })}
        className={SELECT_CLASS}
      >
        {MIN_PRICES.map((p) => (
          <option key={p} value={p}>
            {p === 0 ? 'Any price' : `$${p}+`}
          </option>
        ))}
      </select>
    </div>
  );
}

// --- the table ----------------------------------------------------------------------------------

const count = new Intl.NumberFormat('en-US');

type BadgeTone = 'warn' | 'accent' | 'muted';
const BADGE_TONE: Record<BadgeTone, string> = {
  warn: 'border-warn/50 text-warn',
  accent: 'border-accent-muted text-accent',
  muted: 'border-line-strong text-ink-muted',
};

function Badge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] leading-none ${BADGE_TONE[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * The track-record cell. A tested setup shows "52% win · +0.4R · 312 trades", marked "(in-sample)"
 * when the figures come from the tuning period instead of the out-of-sample test. A setup that has
 * not passed shows why, in the validation's own words.
 */
export function trackRecordText(idea: IdeaOut, kind: TableKind): string {
  const t = idea.track_record;
  if (kind === 'unproven' || !idea.live) return t.verdict_text;
  if (t.win_rate === null && t.expectancy_r === null && t.trades === null) return t.verdict_text;
  const trades =
    t.trades === null ? '—' : `${count.format(t.trades)} ${t.trades === 1 ? 'trade' : 'trades'}`;
  const text = `${pct(t.win_rate)} win · ${r(t.expectancy_r)} · ${trades}`;
  return t.source === 'insample' ? `${text} (in-sample)` : text;
}

function Badges({ idea, kind }: { idea: IdeaOut; kind: TableKind }) {
  const unproven = kind === 'unproven' || !idea.live;
  const badges: ReactNode[] = [];
  if (unproven) {
    badges.push(
      <Badge key="unproven" tone="muted">
        Unproven
      </Badge>,
    );
  }
  if (idea.already_held) {
    badges.push(
      <Badge key="held" tone="accent">
        you already hold this
      </Badge>,
    );
  }
  if (idea.overlaps.length > 0) {
    badges.push(
      <Badge key="overlaps" tone="warn">
        {`overlaps with ${idea.overlaps.join(', ')}`}
      </Badge>,
    );
  }
  if (idea.regime_warning) {
    badges.push(
      <Badge key="regime" tone="warn">
        market not in uptrend
      </Badge>,
    );
  }
  if (badges.length === 0) return <span className="text-ink-subtle">—</span>;
  return <div className="flex flex-wrap gap-1">{badges}</div>;
}

const TH = 'px-3 py-2 font-medium';
const TD = 'px-3 py-2.5 align-top';

function IdeaRow({ idea, kind, place }: { idea: IdeaOut; kind: TableKind; place: number }) {
  const router = useRouter();
  const href = `/stocks/${encodeURIComponent(idea.symbol)}`;
  const muted = kind === 'unproven' || !idea.live;
  const sized = kind !== 'watch';
  return (
    <tr
      // The symbol is a real link (keyboard, middle-click). A click anywhere else on the row
      // follows it too, unless the click was on a link or button, or ended a text selection.
      onClick={(event) => {
        if (event.defaultPrevented) return;
        if ((event.target as Element).closest('a, button')) return;
        if (window.getSelection()?.toString()) return;
        router.push(href);
      }}
      className={`cursor-pointer border-t border-line hover:bg-bg-subtle ${
        muted ? 'text-ink-muted' : 'text-ink'
      }`}
    >
      <td className={`${TD} text-ink-subtle`}>{place}</td>
      <td className={TD}>
        <Link
          href={href}
          className="block rounded outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          <span
            className={`font-mono font-semibold ${muted ? 'text-ink-muted' : 'text-ink'} hover:text-accent`}
          >
            {idea.symbol}
          </span>
          {idea.name !== null && idea.name !== '' && (
            <span className="block max-w-[14rem] truncate text-xs text-ink-muted">{idea.name}</span>
          )}
        </Link>
      </td>
      <td className={`${TD} whitespace-nowrap`}>{idea.setup_label}</td>
      <td className={`${TD} text-right tabular-nums`}>{price(idea.buy_above)}</td>
      <td className={`${TD} text-right tabular-nums`}>{price(idea.stop)}</td>
      {sized && <td className={`${TD} text-right tabular-nums`}>{count.format(idea.shares)}</td>}
      {sized && <td className={`${TD} text-right tabular-nums`}>{money(idea.risk_dollars)}</td>}
      <td className={`${TD} text-right tabular-nums`}>
        {idea.rs_pct === null || !Number.isFinite(idea.rs_pct) ? '—' : Math.round(idea.rs_pct)}
      </td>
      <td className={`${TD} min-w-[12rem]`}>{trackRecordText(idea, kind)}</td>
      <td className={`${TD} min-w-[10rem]`}>
        <Badges idea={idea} kind={kind} />
      </td>
    </tr>
  );
}

/**
 * Ideas in the server's rank order (numbered 1, 2, 3... down this table) as a table that scrolls sideways inside its own box on a narrow screen (the page
 * itself never does). A watch-only table has no Shares or Risk $ columns: nothing is sized for it.
 * `label` names the table for screen readers.
 */
export function IdeasTable({
  ideas,
  kind,
  label,
}: {
  ideas: IdeaOut[];
  kind: TableKind;
  label: string;
}) {
  const sized = kind !== 'watch';
  return (
    <div
      className={`overflow-x-auto rounded-lg border bg-bg-surface ${
        kind === 'validated' ? 'border-line' : 'border-dashed border-line-strong'
      }`}
    >
      <table className={`w-full border-collapse text-sm ${sized ? 'min-w-[62rem]' : 'min-w-[48rem]'}`}>
        <caption className="sr-only">{label}</caption>
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-ink-muted">
            <th scope="col" className={TH}>
              #
            </th>
            <th scope="col" className={TH}>
              Stock
            </th>
            <th scope="col" className={TH}>
              Setup
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Buy above
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Stop
            </th>
            {sized && (
              <th scope="col" className={`${TH} text-right`}>
                Shares
              </th>
            )}
            {sized && (
              <th scope="col" className={`${TH} text-right`}>
                Risk $
              </th>
            )}
            <th scope="col" className={`${TH} text-right`}>
              <Term k="RS">Strength</Term>
            </th>
            <th scope="col" className={TH}>
              Track record
            </th>
            <th scope="col" className={TH}>
              Notes
            </th>
          </tr>
        </thead>
        <tbody>
          {ideas.map((idea, index) => (
            <IdeaRow
              key={`${idea.symbol}:${idea.setup}:${idea.watch_only}`}
              idea={idea}
              kind={kind}
              place={index + 1}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
