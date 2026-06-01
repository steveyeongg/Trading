'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useMemo, useState } from 'react';
import { api } from '@/lib/api';
import type {
  Horizon,
  ScreenerPublishedRow,
  ScreenerResponse,
} from '@/lib/types';

const HORIZONS: Horizon[] = ['intraday', 'swing', 'position', 'long-term'];

export default function ScannerPage() {
  return (
    <Suspense fallback={null}>
      <ScannerForm />
    </Suspense>
  );
}

function ScannerForm() {
  const search = useSearchParams();
  const initialSymbols = search.get('symbols') ?? '';
  const [universe, setUniverse] = useState<string>(initialSymbols ? 'manual' : 'default_watchlist');
  const [manualInput, setManualInput] = useState<string>(initialSymbols);
  const [horizon, setHorizon] = useState<Horizon>('swing');
  const [minComposite, setMinComposite] = useState<number>(60);
  const [topN, setTopN] = useState<number>(10);
  const [includeExplanation, setIncludeExplanation] = useState(false);

  const { data: universesResp, isLoading: universesLoading } = useQuery({
    queryKey: ['universes'],
    queryFn: api.getUniverses,
    staleTime: 5 * 60_000,
  });

  const universes = universesResp?.universes ?? [];

  const mutation = useMutation({
    mutationFn: async () => {
      const symbols = manualInput
        .split(/[\s,]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      return api.runScreener({
        universe,
        symbols,
        horizon,
        min_composite: minComposite,
        top_n: topN,
        include_explanation: includeExplanation,
      });
    },
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Scanner</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Rank candidates across a universe — or enter symbols manually. Rows that
          don&apos;t publish are kept with their gate reason attached.
        </p>
      </header>

      <form
        onSubmit={onSubmit}
        className="rounded-lg border border-line bg-bg-surface p-4 space-y-4"
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Universe">
            <select
              className="w-full rounded border border-line bg-bg px-2 py-1.5 font-mono text-sm"
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              disabled={universesLoading}
            >
              <option value="manual">manual (symbols below)</option>
              {universes.map((u) => (
                <option key={u.key} value={u.key}>
                  {u.label} ({u.symbol_count})
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-ink-subtle">
              {universes.find((u) => u.key === universe)?.description ??
                'Free-form list; manual symbols below.'}
            </p>
          </Field>

          <Field label="Manual symbols (optional — appends to universe)">
            <input
              className="w-full rounded border border-line bg-bg px-2 py-1.5 font-mono text-sm"
              placeholder="AAPL, NVDA, TSLA"
              value={manualInput}
              onChange={(e) => setManualInput(e.target.value)}
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Horizon">
            <select
              className="w-full rounded border border-line bg-bg px-2 py-1.5 text-sm"
              value={horizon}
              onChange={(e) => setHorizon(e.target.value as Horizon)}
            >
              {HORIZONS.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Min composite |score|">
            <input
              type="number"
              min={0}
              max={100}
              className="w-full rounded border border-line bg-bg px-2 py-1.5 font-mono text-sm"
              value={minComposite}
              onChange={(e) => setMinComposite(Number(e.target.value))}
            />
          </Field>

          <Field label="Top N">
            <input
              type="number"
              min={1}
              max={500}
              className="w-full rounded border border-line bg-bg px-2 py-1.5 font-mono text-sm"
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
            />
          </Field>

          <Field label="Explain top results">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={includeExplanation}
                onChange={(e) => setIncludeExplanation(e.target.checked)}
              />
              DeepSeek rationale
            </label>
          </Field>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={mutation.isPending}
            className="rounded bg-accent px-4 py-1.5 text-sm font-medium text-bg disabled:opacity-50"
          >
            {mutation.isPending ? 'Scanning…' : 'Run scan'}
          </button>
          {mutation.error instanceof Error && (
            <span className="text-sm text-warn">Error: {mutation.error.message}</span>
          )}
        </div>
      </form>

      {mutation.data && <Results data={mutation.data} />}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs uppercase tracking-wider text-ink-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

function Results({ data }: { data: ScreenerResponse }) {
  const published = useMemo(
    () => data.results.filter((r): r is ScreenerPublishedRow => r.published),
    [data.results],
  );
  const skipped = data.results.filter((r) => !r.published);

  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">
          Results <span className="text-ink-muted">— {data.run_id}</span>
        </h2>
        <p className="text-xs text-ink-subtle">
          {data.published} published of {data.scanned} scanned · universe{' '}
          <code className="font-mono text-ink-muted">{data.universe}</code>
        </p>
      </div>

      {published.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-line bg-bg-surface">
          <table className="w-full text-sm">
            <thead className="border-b border-line bg-bg-subtle text-xs uppercase text-ink-muted">
              <tr>
                <Th>#</Th>
                <Th>Symbol</Th>
                <Th>Dir</Th>
                <Th align="right">Comp</Th>
                <Th align="right">Conf</Th>
                <Th>Conviction</Th>
                <Th align="right">Entry</Th>
                <Th align="right">Stop</Th>
                <Th align="right">T1</Th>
                <Th align="right">T2</Th>
                <Th align="right">T3</Th>
                <Th align="right">R:R</Th>
                <Th align="right">Tech</Th>
                <Th align="right">Quant</Th>
                <Th align="right">News</Th>
                <Th align="right">Macro</Th>
                <Th align="right">Risk</Th>
              </tr>
            </thead>
            <tbody>
              {published.map((r) => (
                <PublishedRow key={r.symbol} row={r} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="rounded-lg border border-line bg-bg-surface p-4 text-sm text-ink-muted">
          No symbols cleared the gates. Either lower the min composite or scan a
          wider universe.
        </p>
      )}

      {skipped.length > 0 && (
        <details className="rounded-lg border border-line bg-bg-surface p-4">
          <summary className="cursor-pointer text-sm font-medium">
            No-signal reasons ({skipped.length})
          </summary>
          <ul className="mt-3 space-y-1 font-mono text-xs">
            {skipped.map((r) => (
              <li key={r.symbol} className="flex gap-3">
                <span className="w-16 text-ink">{r.symbol}</span>
                <span className="text-ink-muted">
                  {('no_signal_reason' in r && r.no_signal_reason) || '—'}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {data.skipped.length > 0 && (
        <details className="rounded-lg border border-line bg-bg-surface p-4">
          <summary className="cursor-pointer text-sm font-medium">
            Skipped ({data.skipped.length}) — usually missing bars
          </summary>
          <ul className="mt-3 space-y-1 font-mono text-xs">
            {data.skipped.map((s) => (
              <li key={s.symbol} className="flex gap-3">
                <span className="w-16 text-ink">{s.symbol}</span>
                <span className="text-ink-muted">{s.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      <p className="text-xs text-ink-subtle">
        Informational only. Not investment advice. Always verify and manage risk independently.
      </p>
    </section>
  );
}

function PublishedRow({ row }: { row: ScreenerPublishedRow }) {
  const t = row.take_profit_levels;
  const dirColor = row.direction === 'long' ? 'text-bull' : 'text-bear';
  return (
    <tr className="border-b border-line/50 last:border-0 hover:bg-bg-subtle">
      <Td>{row.rank}</Td>
      <Td>
        <Link href={`/symbols/${row.symbol}`} className="font-mono text-accent hover:underline">
          {row.symbol}
        </Link>
      </Td>
      <Td className={dirColor}>{row.direction.toUpperCase()}</Td>
      <Td align="right" className={dirColor}>
        {row.composite_score >= 0 ? '+' : ''}
        {row.composite_score.toFixed(0)}
      </Td>
      <Td align="right">{row.confidence_pct.toFixed(0)}%</Td>
      <Td className="capitalize">{row.conviction}</Td>
      <Td align="right">{fmt(row.entry_price)}</Td>
      <Td align="right">{fmt(row.stop_price)}</Td>
      <Td align="right">{fmt(t[0])}</Td>
      <Td align="right">{fmt(t[1])}</Td>
      <Td align="right">{fmt(t[2])}</Td>
      <Td align="right">{fmt(row.expected_rr, 2)}</Td>
      <Td align="right">{row.sub_scores.tech.toFixed(0)}</Td>
      <Td align="right">{row.sub_scores.quant.toFixed(0)}</Td>
      <Td align="right">{row.sub_scores.news.toFixed(0)}</Td>
      <Td align="right">{row.sub_scores.macro.toFixed(0)}</Td>
      <Td align="right">{row.sub_scores.risk.toFixed(0)}</Td>
    </tr>
  );
}

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(digits);
}

function Th({ children, align = 'left' }: { children: React.ReactNode; align?: 'left' | 'right' }) {
  return (
    <th className={`px-3 py-2 font-medium ${align === 'right' ? 'text-right' : 'text-left'}`}>
      {children}
    </th>
  );
}

function Td({
  children,
  align = 'left',
  className = '',
}: {
  children: React.ReactNode;
  align?: 'left' | 'right';
  className?: string;
}) {
  return (
    <td
      className={`px-3 py-2 font-mono text-xs ${align === 'right' ? 'text-right' : 'text-left'} ${className}`}
    >
      {children}
    </td>
  );
}
