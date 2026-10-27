import type { ReactNode } from 'react';
import type { IdeaOut } from '@/lib/swing-types';

type BadgeTone = 'warn' | 'accent' | 'muted';
const BADGE_TONE: Record<BadgeTone, string> = {
  warn: 'border-warn/50 text-warn',
  accent: 'border-accent-muted text-accent',
  muted: 'border-line-strong text-ink-muted',
};

export function Badge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-block whitespace-nowrap rounded border px-1.5 py-0.5 text-[11px] leading-none ${BADGE_TONE[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * The notes on one idea: Unproven (its setup has not passed testing), you already hold it, it
 * overlaps with another idea, and the market is not in an uptrend. `unproven` is the caller's call,
 * because a list can mark a row unproven even when the idea itself says otherwise. Renders nothing
 * when there is nothing to say, unless `empty` gives the placeholder to show instead.
 */
export function IdeaBadges({
  idea,
  unproven,
  empty = null,
}: {
  idea: IdeaOut;
  unproven: boolean;
  empty?: ReactNode;
}) {
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
  if (badges.length === 0) return <>{empty}</>;
  return <div className="flex flex-wrap gap-1">{badges}</div>;
}
