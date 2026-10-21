import Link from 'next/link';
import type { ReactNode } from 'react';
import { IdeaBadges } from '@/components/IdeaBadges';
import { Term } from '@/components/Term';
import { money, price } from '@/lib/format';
import { hasMeasuredRecord, ideaKind, trackRecordText } from '@/lib/idea-text';
import type { IdeaOut } from '@/lib/swing-types';

export const WATCH_ONLY_TEXT = 'Watch-only (market in downtrend)';
export const WATCH_ONLY_NOTE = 'This is not a buy idea tonight, so no share count or risk is shown.';

const shares = new Intl.NumberFormat('en-US');

/** A share price as dollars: 148.2 -> "$148.20", and "—" when the price is missing. */
const dollars = (x: number | null) => {
  const text = price(x);
  return /\d/.test(text) ? `$${text}` : text;
};

/** The route that opens Positions with this idea filled in; the idea is left out when it has no id. */
export function boughtHref(idea: IdeaOut): string {
  const query = [`new=${encodeURIComponent(idea.symbol)}`];
  if (idea.id !== null && idea.id !== '') query.push(`idea=${encodeURIComponent(idea.id)}`);
  return `/positions?${query.join('&')}`;
}

function Figure({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="mt-0.5 font-mono text-lg tabular-nums">{children}</dd>
    </div>
  );
}

/**
 * The idea behind the stock page: its setup, the three levels, the size, the setup's track record,
 * the notes (Unproven, you already hold it, overlaps, market not in an uptrend) and what to do
 * next. A setup that has not passed testing is muted and dashed; a watch-only idea has no size and
 * no "I bought this".
 */
export function StockIdea({ idea }: { idea: IdeaOut }) {
  const kind = ideaKind(idea);
  const watch = kind === 'watch';
  const unproven = !idea.live;
  const measured = hasMeasuredRecord(idea, kind);

  return (
    <section
      aria-labelledby="idea-heading"
      className={`rounded-lg border bg-bg-surface p-4 ${
        unproven ? 'border-dashed border-line-strong text-ink-muted' : 'border-line text-ink'
      }`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id="idea-heading" className="text-base font-semibold">
          <span className="font-normal text-ink-muted">Setup: </span>
          {idea.setup_label}
        </h2>
        <IdeaBadges idea={idea} unproven={unproven} />
      </div>

      {watch && (
        <div className="mt-2 text-sm">
          <p className="font-medium text-warn">{WATCH_ONLY_TEXT}</p>
          <p className="text-ink-muted">{WATCH_ONLY_NOTE}</p>
        </div>
      )}

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
        <Figure label="Buy above">{dollars(idea.buy_above)}</Figure>
        <Figure label="Stop">{dollars(idea.stop)}</Figure>
        <Figure
          label={
            <>
              <Term k="R">2R</Term> reference
            </>
          }
        >
          {dollars(idea.target_2r)}
        </Figure>
        {!watch && <Figure label="Shares">{shares.format(idea.shares)}</Figure>}
        {!watch && <Figure label="Risk $">{money(idea.risk_dollars)}</Figure>}
      </dl>
      {!watch && (
        <p className="mt-2 text-xs text-ink-muted">
          Risk $ is what you lose if the price falls to the stop; that amount is 1
          <Term k="R">R</Term>. A gap through the stop can cost more.
        </p>
      )}

      <p className="mt-3 text-sm">
        <span className="text-ink-muted">Track record: </span>
        {trackRecordText(idea, kind)}
      </p>
      {measured && (
        <p className="mt-0.5 text-xs text-ink-muted">
          Results are in <Term k="R">R</Term>, a multiple of the amount risked on each trade.
        </p>
      )}

      {!watch && (
        <div className="mt-4">
          {idea.already_held ? (
            <Link href="/positions" className="text-sm text-accent hover:underline">
              See it in your Positions
            </Link>
          ) : (
            <Link
              href={boughtHref(idea)}
              className="inline-block rounded border border-accent-muted px-4 py-2 text-sm font-medium text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
            >
              I bought this
            </Link>
          )}
        </div>
      )}
    </section>
  );
}
