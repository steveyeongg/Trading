import { SectionHeading } from '@/components/SectionHeading';
import { TermText } from '@/components/TermText';
import { plural } from '@/lib/copy';
import type { CheckOut } from '@/lib/swing-types';

/**
 * The line above the checklist of a stock that has no idea tonight: how many rules it failed, or
 * that it passes them all and still no setup triggered.
 */
export function checklistSummary(symbol: string, items: CheckOut[]): string {
  if (items.length === 0) return `No rules could be checked for ${symbol} tonight.`;
  const failed = items.filter((item) => !item.passed).length;
  if (failed === 0) return `${symbol} passes the universe rules but no setup triggered tonight.`;
  return `${symbol} doesn't qualify tonight: ${failed} ${plural(failed, 'rule')} failed.`;
}

// Mark, rule, this stock's value, the limit. The mark's column is the same in the header, so the
// failed rows' left edge does not push the others over.
const COLUMNS = 'sm:grid-cols-[1.25rem_minmax(0,2fr)_minmax(0,1.3fr)_minmax(0,1.3fr)]';

/**
 * Every rule a stock is held to, in the server's order: ✓ or ✗, the rule in plain English, the
 * stock's own number and the limit, all as the API sends them. A failed rule has a red edge and
 * tint, and the words "Failed" / "Passed" for a screen reader, so colour is not the only cue. When
 * the stock has no idea (`hasIdea` false) a summary line comes first.
 */
export function EligibilityChecklist({
  symbol,
  items,
  hasIdea,
}: {
  symbol: string;
  items: CheckOut[];
  hasIdea: boolean;
}) {
  return (
    <section aria-labelledby="checklist-heading" className="space-y-3">
      <SectionHeading id="checklist-heading">Eligibility checklist</SectionHeading>
      {!hasIdea && <p className="text-sm font-medium text-ink">{checklistSummary(symbol, items)}</p>}
      {items.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-line bg-bg-surface">
          <div
            aria-hidden="true"
            className={`hidden gap-x-2 border-l-2 border-transparent px-3 py-2 text-xs uppercase tracking-wider text-ink-muted sm:grid ${COLUMNS}`}
          >
            <span />
            <span>Rule</span>
            <span>This stock</span>
            <span>Needed</span>
          </div>
          <ul>
            {items.map((item, index) => (
              <li
                key={`${index}:${item.rule}`}
                // The red edge is the left border; the rows are told apart by the top border (the
                // first one only under the column titles, where the table has them).
                className={`grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-2 gap-y-0.5 border-l-2 border-t border-t-line px-3 py-2 text-sm first:border-t-0 sm:first:border-t ${COLUMNS} ${
                  item.passed ? 'border-l-transparent' : 'border-l-bear bg-bear/10'
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`font-semibold ${item.passed ? 'text-bull' : 'text-bear'}`}
                >
                  {item.passed ? '✓' : '✗'}
                </span>
                <span className={`min-w-0 break-words ${item.passed ? 'text-ink' : 'font-medium text-ink'}`}>
                  <span className="sr-only">{item.passed ? 'Passed: ' : 'Failed: '}</span>
                  <TermText>{item.rule}</TermText>
                </span>
                <span className="col-start-2 min-w-0 break-words text-ink-muted sm:col-start-auto">
                  <span className="text-ink-subtle sm:sr-only">This stock: </span>
                  <TermText>{item.value}</TermText>
                </span>
                <span className="col-start-2 min-w-0 break-words text-ink-muted sm:col-start-auto">
                  <span className="text-ink-subtle sm:sr-only">Needed: </span>
                  <TermText>{item.threshold}</TermText>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
