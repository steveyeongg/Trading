import Link from 'next/link';
import { SectionHeading } from '@/components/SectionHeading';
import { plural } from '@/lib/copy';
import type { ActionOut } from '@/lib/swing-types';

// An action's colour by its kind. A kind this build does not know keeps the plain colour and shows
// the server's text as given.
const KIND_TONE: Record<string, string> = {
  stop_hit: 'font-medium text-bear',
  max_hold: 'text-warn',
  no_data: 'text-warn',
  raise_stop: 'text-ink',
  hold: 'text-ink-muted',
};

export const NO_POSITIONS_HINT = "Record a trade from any idea's stock page.";

/**
 * What to do with the open positions tonight, one line each, every line linking to /positions.
 * `openPositions` is the number of open positions, which can exceed the number of actions when a
 * position has not been looked at by a scan yet.
 */
export function PositionActionsCard({
  actions,
  openPositions,
}: {
  actions: ActionOut[];
  openPositions: number;
}) {
  const none = actions.length === 0 && openPositions === 0;
  return (
    <section
      aria-labelledby="positions-heading"
      className="rounded-lg border border-line bg-bg-surface px-4 py-3"
    >
      <SectionHeading id="positions-heading">
        {none
          ? 'YOUR POSITIONS: none open'
          : `YOUR POSITIONS: ${actions.length} ${plural(actions.length, 'action')}`}
      </SectionHeading>
      {none && <p className="mt-1 text-sm text-ink-muted">{NO_POSITIONS_HINT}</p>}
      {!none && actions.length === 0 && (
        <p className="mt-1 text-sm text-ink-muted">
          {`${openPositions} open. Their actions appear after the next scan.`}
        </p>
      )}
      {actions.length > 0 && (
        <ul className="mt-2 divide-y divide-line">
          {actions.map((action) => (
            <li key={action.position_id}>
              <Link
                href="/positions"
                className={`flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5 text-sm hover:bg-bg-subtle ${
                  KIND_TONE[action.kind] ?? 'text-ink'
                }`}
              >
                <span className="font-mono font-semibold">{action.symbol}</span>
                <span className="min-w-0 break-words">{action.text}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
