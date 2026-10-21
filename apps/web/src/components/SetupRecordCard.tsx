import type { ReactNode } from 'react';
import { SECTION_HEADING_CLASS } from '@/components/SectionHeading';
import { SetupEquityChart } from '@/components/SetupEquityChart';
import { Term } from '@/components/Term';
import {
  COLUMN_SUBTITLES,
  NO_VERSIONS_TEXT,
  PORTFOLIO_CAPTION_TEXT,
  RESULTS_CAPTION_NOTE,
  RESULTS_CAPTION_PREFIX,
  RESULTS_CAPTION_SUFFIX,
  SPY_ROW_LABEL,
  STATS_FOOTNOTE_TEXT,
  SECTION_EQUITY_TEXT,
  SECTION_RESULTS_TEXT,
  SECTION_VERSIONS_TEXT,
  combinedNoteText,
  distinctNoteText,
  randomCompareText,
  randomEntryText,
  stillOpenText,
  versionText,
} from '@/lib/copy';
import { count, pct, ratio, r, sessionDateYear, sessions, signedNumber } from '@/lib/format';
import type { FamilyOut, PortfolioStatsOut, StatsOut, VersionOut } from '@/lib/swing-types';
import {
  isFiniteNumber,
  recordColumns,
  verdictBadge,
  verdictNote,
  type BadgeTone,
  type RecordColumn,
} from '@/lib/track-record';

const TH = 'px-3 py-2 font-medium';
const TD = 'px-3 py-2.5 align-top';
const NUMBER_TD = `${TD} text-right tabular-nums`;
const TABLE_BOX = 'overflow-x-auto rounded border border-line';

const BADGE_TONE: Record<BadgeTone, string> = {
  bull: 'border-bull/50 bg-bull/10 text-bull',
  warn: 'border-warn/50 bg-warn/10 text-warn',
  muted: 'border-line-strong text-ink-muted',
};

/** The family's verdict as a badge. The words carry the meaning; the colour only repeats it. */
function VerdictBadge({ verdict, text }: { verdict: string; text: string }) {
  const badge = verdictBadge(verdict, text);
  return (
    <span
      className={`inline-block max-w-full break-words rounded border px-2.5 py-1 text-sm font-medium ${BADGE_TONE[badge.tone]}`}
    >
      {badge.label}
    </span>
  );
}

// --- the results table ---------------------------------------------------------------------------------

interface RowSpec {
  label: string;
  cell: (stats: StatsOut) => ReactNode;
}

// Expectancy has two decimals, like the random-pick line and the verdict text it is compared with.
const ROWS: readonly RowSpec[] = [
  {
    label: 'Trades',
    cell: (s) => (
      <>
        {count(s.n)}
        {isFiniteNumber(s.n_distinct) && (
          <span className="block text-xs text-ink-muted">{`(${count(s.n_distinct)} distinct)`}</span>
        )}
      </>
    ),
  },
  { label: 'Win rate', cell: (s) => pct(s.win_rate) },
  { label: 'Avg win', cell: (s) => r(s.avg_win_r) },
  { label: 'Avg loss', cell: (s) => r(s.avg_loss_r) },
  { label: 'Expectancy', cell: (s) => r(s.expectancy_r, 2) },
  { label: 'Profit factor', cell: (s) => ratio(s.profit_factor) },
  { label: 'Avg hold', cell: (s) => sessions(s.avg_hold) },
];

/** One column's header: its name, and under it what window the numbers cover. */
function ColumnHeader({ column }: { column: RecordColumn }) {
  return (
    <th scope="col" className={`${TH} min-w-[7.5rem] text-right`}>
      {column.title}
      <span className="mt-0.5 block text-[11px] font-normal normal-case tracking-normal text-ink-subtle">
        {COLUMN_SUBTITLES[column.key]}
      </span>
    </th>
  );
}

/**
 * In-sample, Out-of-sample, Live and Reconstructed side by side. A column with nothing to show
 * says why in one cell that spans its rows ("Not run yet", "No closed live ideas yet"), so a dash
 * is never left to be guessed at. It scrolls sideways inside its own box on a phone.
 */
function ResultsTable({ columns }: { columns: RecordColumn[] }) {
  return (
    <div className={TABLE_BOX}>
      <table className="w-full min-w-[40rem] border-collapse text-sm">
        <caption className="caption-top text-left text-xs text-ink-muted">
          {/* Sticks to the left edge while the table scrolls sideways, and is never wider than the
              screen, so the sentence stays readable on a phone. */}
          <div className="sticky left-0 w-[calc(100vw-6rem)] max-w-3xl px-3 py-2">
            {RESULTS_CAPTION_PREFIX} <Term k="R">R</Term> {RESULTS_CAPTION_SUFFIX}{' '}
            {RESULTS_CAPTION_NOTE}
          </div>
        </caption>
        <thead>
          <tr className="border-t border-line text-left text-xs uppercase tracking-wider text-ink-muted">
            <th scope="col" className={TH}>
              <span className="sr-only">Measure</span>
            </th>
            {columns.map((column) => (
              <ColumnHeader key={column.key} column={column} />
            ))}
          </tr>
        </thead>
        <tbody>
          {ROWS.map((row, rowIndex) => (
            <tr key={row.label} className="border-t border-line">
              <th scope="row" className={`${TD} text-left font-normal text-ink-muted`}>
                {row.label}
              </th>
              {columns.map((column) => {
                if (column.emptyText !== null || column.stats === null) {
                  // One cell for the whole column, drawn in its first row.
                  return rowIndex === 0 ? (
                    <td
                      key={column.key}
                      rowSpan={ROWS.length}
                      className="border-l border-line px-3 py-2.5 text-center align-middle text-xs text-ink-muted"
                    >
                      {column.emptyText}
                    </td>
                  ) : null;
                }
                return (
                  <td key={column.key} className={NUMBER_TD}>
                    {row.cell(column.stats)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The muted notes under the table: still-open counts, the distinct-trades line, and the key. */
function ResultsNotes({ family }: { family: FamilyOut }) {
  const open = [
    { name: 'Live', stats: family.live },
    { name: 'Reconstructed', stats: family.reconstructed },
  ].flatMap(({ name, stats }) =>
    isFiniteNumber(stats?.open) ? [`${name}: ${stillOpenText(count(stats.open))}`] : [],
  );
  const distinct = family.oos_distinct;
  return (
    <div className="space-y-1 text-xs text-ink-muted">
      {open.length > 0 && <p>{open.join(' · ')}</p>}
      {distinct !== null && distinct !== undefined && (
        <p>{distinctNoteText(count(distinct.n), r(distinct.expectancy_r, 2))}</p>
      )}
      <p>{STATS_FOOTNOTE_TEXT}</p>
    </div>
  );
}

// --- the account simulation ------------------------------------------------------------------------------

function PortfolioRow({
  label,
  stats,
  showTrades,
}: {
  label: string;
  stats: PortfolioStatsOut | null;
  showTrades: boolean;
}) {
  return (
    <tr className="border-t border-line">
      <th scope="row" className={`${TD} text-left font-normal`}>
        {label}
      </th>
      <td className={NUMBER_TD}>{pct(stats?.cagr, 1)}</td>
      <td className={NUMBER_TD}>{pct(stats?.max_dd)}</td>
      <td className={NUMBER_TD}>{ratio(stats?.sharpe)}</td>
      <td className={NUMBER_TD}>{pct(stats?.exposure)}</td>
      {/* SPY is bought once and held: the simulation counts no trades for it. */}
      <td className={NUMBER_TD}>{showTrades ? count(stats?.trades) : '—'}</td>
    </tr>
  );
}

function PortfolioTable({ family }: { family: FamilyOut }) {
  const { portfolio } = family;
  return (
    <div className={TABLE_BOX}>
      <table className="w-full min-w-[30rem] border-collapse text-sm">
        <caption className="sr-only">{PORTFOLIO_CAPTION_TEXT}</caption>
        <thead>
          <tr className="text-left text-xs uppercase tracking-wider text-ink-muted">
            <th scope="col" className={TH}>
              <span className="sr-only">Account</span>
            </th>
            <th scope="col" className={`${TH} text-right`}>
              CAGR
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Max drawdown
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Sharpe
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Exposure
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Trades
            </th>
          </tr>
        </thead>
        <tbody>
          <PortfolioRow label="Strategy" stats={portfolio.strategy} showTrades />
          <PortfolioRow label={SPY_ROW_LABEL} stats={portfolio.spy} showTrades={false} />
        </tbody>
      </table>
    </div>
  );
}

// --- random picks, versions, warnings ----------------------------------------------------------------------

/** The random-entry comparison; nothing at all when the report has no baseline. */
function RandomEntry({ family }: { family: FamilyOut }) {
  const { baseline } = family;
  if (baseline === null || baseline === undefined || !isFiniteNumber(baseline.median)) return null;
  const expectancy = family.oos?.expectancy_r;
  return (
    <div className="space-y-1 text-sm">
      <p className="text-ink">
        {randomEntryText(
          signedNumber(baseline.median),
          isFiniteNumber(baseline.p5) ? signedNumber(baseline.p5) : null,
          isFiniteNumber(baseline.p95) ? signedNumber(baseline.p95) : null,
        )}
      </p>
      {isFiniteNumber(expectancy) && (
        <p className="text-ink-muted">
          {randomCompareText(r(expectancy, 2), expectancy > baseline.median)}
        </p>
      )}
    </div>
  );
}

const PASSED_TONE = 'text-bull';
const FAILED_TONE = 'text-warn';

function VersionList({ versions }: { versions: VersionOut[] }) {
  if (versions.length === 0) return <p className="text-sm text-ink-muted">{NO_VERSIONS_TEXT}</p>;
  return (
    <ul aria-label="Versions" className="space-y-1 text-sm">
      {versions.map((v) => (
        <li
          key={v.version}
          className={`break-words ${
            v.passed === true ? PASSED_TONE : v.passed === false ? FAILED_TONE : 'text-ink-muted'
          }`}
        >
          {versionText(
            v.version,
            v.run_index,
            v.freeze_date === null ? null : sessionDateYear(v.freeze_date),
            v.passed,
          )}
        </li>
      ))}
    </ul>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul
      aria-label="Warnings"
      className="space-y-1 rounded border border-warn/50 bg-warn/10 px-3 py-2 text-sm text-warn"
    >
      {warnings.map((warning) => (
        <li key={warning} className="break-words">
          {warning}
        </li>
      ))}
    </ul>
  );
}

// --- the card ----------------------------------------------------------------------------------------------

/**
 * One setup family's record: its verdict, the four result columns, the simulated account against
 * SPY, the random-entry comparison, and every version tested. Past results are labelled simulated
 * or historical throughout.
 */
export function SetupRecordCard({ family }: { family: FamilyOut }) {
  const headingId = `${family.family}-record-heading`;
  const columns = recordColumns(family);
  const note = verdictNote(family.verdict);
  return (
    <section
      aria-labelledby={headingId}
      className="min-w-0 space-y-5 rounded-lg border border-line bg-bg-surface p-4 sm:p-5"
    >
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h2 id={headingId} className="text-lg font-semibold">
            {family.label}
          </h2>
          <VerdictBadge verdict={family.verdict} text={family.verdict_text} />
        </div>
        {note !== null && <p className="text-sm text-ink-muted">{note}</p>}
      </header>

      <Warnings warnings={family.warnings} />

      <div className="space-y-2">
        <h3 className={SECTION_HEADING_CLASS}>{SECTION_RESULTS_TEXT}</h3>
        <ResultsTable columns={columns} />
        <ResultsNotes family={family} />
      </div>

      <div className="space-y-3">
        <h3 className={SECTION_HEADING_CLASS}>{SECTION_EQUITY_TEXT}</h3>
        <div className="rounded border border-line p-3">
          <SetupEquityChart strategy={family.equity.strategy} spy={family.equity.spy} />
        </div>
        {/* With no simulation at all, the chart's "No equity series" line is the whole story. */}
        {(family.portfolio.strategy !== null || family.portfolio.spy !== null) && (
          <PortfolioTable family={family} />
        )}
        {family.portfolio.combined_note !== null && (
          <p className="text-xs text-ink-muted">{combinedNoteText(family.portfolio.combined_note)}</p>
        )}
      </div>

      <RandomEntry family={family} />

      <div className="space-y-2">
        <h3 className={SECTION_HEADING_CLASS}>{SECTION_VERSIONS_TEXT}</h3>
        <VersionList versions={family.versions} />
      </div>
    </section>
  );
}
