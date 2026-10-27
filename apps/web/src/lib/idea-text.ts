// Words about one trade idea that Today's tables and the stock page both use, so the two say the
// same thing.

import { plural } from './copy';
import { pct, r } from './format';
import type { IdeaOut } from './swing-types';

/** Which list an idea belongs to: tested setups, setups that have not passed, or watch-only names. */
export type TableKind = 'validated' | 'unproven' | 'watch';

const count = new Intl.NumberFormat('en-US');

/** The list a lone idea would sit in: watch-only first, then by whether its setup has passed. */
export function ideaKind(idea: IdeaOut): TableKind {
  if (idea.watch_only) return 'watch';
  return idea.live ? 'validated' : 'unproven';
}

/** Whether the track record is measured figures (as opposed to the validation's own words). */
export function hasMeasuredRecord(idea: IdeaOut, kind: TableKind): boolean {
  const t = idea.track_record;
  if (kind === 'unproven' || !idea.live) return false;
  return !(t.win_rate === null && t.expectancy_r === null && t.trades === null);
}

/**
 * The track-record text. A tested setup shows "52% win · +0.4R · 312 trades", marked "(in-sample)"
 * when the figures come from the tuning period instead of the out-of-sample test. A setup that has
 * not passed shows why, in the validation's own words.
 */
export function trackRecordText(idea: IdeaOut, kind: TableKind): string {
  const t = idea.track_record;
  if (!hasMeasuredRecord(idea, kind)) return t.verdict_text;
  const trades =
    t.trades === null ? '—' : `${count.format(t.trades)} ${plural(t.trades, 'trade')}`;
  const text = `${pct(t.win_rate)} win · ${r(t.expectancy_r)} · ${trades}`;
  return t.source === 'insample' ? `${text} (in-sample)` : text;
}
