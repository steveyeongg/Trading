'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { api, ApiError } from '@/lib/api';
import type { JournalEntry, JournalResponse } from '@/lib/types';

function usd(n: number | null): string {
  if (n === null || n === undefined) return '—';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}
function r(n: number | null): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return (n > 0 ? '+' : '') + n.toFixed(2) + 'R';
}
function pct(n: number | null): string {
  if (n === null || n === undefined) return '—';
  return (n * 100).toFixed(1) + '%';
}
function shortTs(s: string | null): string {
  if (!s) return '—';
  return s.slice(0, 16).replace('T', ' ');
}

const EXIT_TONE: Record<string, string> = {
  target: 'text-bull',
  stop: 'text-bear',
  time: 'text-warn',
  eod: 'text-ink-muted',
};

export default function JournalPage() {
  const { data, isLoading, error } = useQuery<JournalResponse>({
    queryKey: ['journal'],
    queryFn: () => api.journal(200),
    refetchInterval: 30_000,
  });

  if (isLoading) return <div className="text-sm text-ink-muted">Loading journal…</div>;

  if (error) {
    const isConn = !(error instanceof ApiError) || error.status >= 500;
    return (
      <div className="rounded-lg border border-line bg-bg-surface p-6 text-sm">
        <div className="text-warn">Couldn&apos;t load the journal.</div>
        {isConn && (
          <>
            <p className="mt-2 text-ink-muted">Migrate, then auto-log a backtest&apos;s trades:</p>
            <pre className="mt-2 rounded bg-bg-subtle p-3 font-mono text-xs">
{`docker compose -f infra/docker/docker-compose.yml up -d
uv run python -m atlas_shared.migrate up
uv run python -m journal_service.seed --n-bars 4000`}
            </pre>
          </>
        )}
      </div>
    );
  }

  if (!data) return null;
  const a = data.attribution;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Journal</h1>

      {/* Attribution strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="Trades" value={String(a.n)} />
        <Metric label="Hit rate" value={pct(a.hit_rate)} />
        <Metric label="Avg win" value={r(a.avg_win_r)} tone="text-bull" />
        <Metric label="Avg loss" value={r(a.avg_loss_r)} tone="text-bear" />
        <Metric label="Expectancy" value={r(a.expectancy_r)} tone={(a.expectancy_r ?? 0) >= 0 ? 'text-bull' : 'text-bear'} />
        <Metric label="Total P&L" value={usd(a.total_pnl)} tone={a.total_pnl >= 0 ? 'text-bull' : 'text-bear'} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_240px]">
        {/* Entries table */}
        <div className="rounded-lg border border-line bg-bg-surface">
          <div className="border-b border-line px-4 py-2 text-xs uppercase tracking-wider text-ink-muted">
            Closed trades
          </div>
          {data.entries.length === 0 ? (
            <div className="p-4 text-sm text-ink-muted">
              No entries. Auto-log a backtest:{' '}
              <code className="font-mono text-ink">uv run python -m journal_service.seed</code>
            </div>
          ) : (
            <div className="max-h-[520px] overflow-auto">
              <table className="w-full font-mono text-xs">
                <thead className="sticky top-0 bg-bg-surface">
                  <tr className="text-ink-muted">
                    <th className="px-3 py-2 text-left">Symbol</th>
                    <th className="px-2 text-left">Side</th>
                    <th className="px-2 text-right">Entry</th>
                    <th className="px-2 text-right">Exit</th>
                    <th className="px-2 text-right">P&L</th>
                    <th className="px-2 text-right">R</th>
                    <th className="px-2 text-right">Bars</th>
                    <th className="px-2 text-left">Exit</th>
                    <th className="px-3 text-right">Closed</th>
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((e: JournalEntry, i: number) => {
                    const pnlTone = (e.realized_pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear';
                    return (
                      <tr key={i} className="border-t border-line hover:bg-bg-subtle">
                        <td className="px-3 py-1.5">
                          <Link href={`/symbols/${e.symbol}`} className="text-accent hover:underline">
                            {e.symbol}
                          </Link>
                        </td>
                        <td className={`px-2 ${e.side === 'long' ? 'text-bull' : 'text-bear'}`}>{e.side}</td>
                        <td className="px-2 text-right">${e.entry_price.toFixed(2)}</td>
                        <td className="px-2 text-right">{e.exit_price ? '$' + e.exit_price.toFixed(2) : '—'}</td>
                        <td className={`px-2 text-right ${pnlTone}`}>{usd(e.realized_pnl)}</td>
                        <td className={`px-2 text-right ${(e.r_multiple ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>{r(e.r_multiple)}</td>
                        <td className="px-2 text-right text-ink-muted">{e.bars_held ?? '—'}</td>
                        <td className={`px-2 ${EXIT_TONE[e.exit_reason ?? ''] ?? 'text-ink-muted'}`}>{e.exit_reason ?? '—'}</td>
                        <td className="px-3 text-right text-ink-subtle">{shortTs(e.closed_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Breakdowns */}
        <div className="space-y-4">
          <div className="rounded-lg border border-line bg-bg-surface p-4">
            <h3 className="mb-2 text-xs uppercase tracking-wider text-ink-muted">Exit reasons</h3>
            <div className="space-y-1 font-mono text-xs">
              {Object.entries(a.exit_reasons).sort((x, y) => y[1] - x[1]).map(([reason, n]) => (
                <div key={reason} className="flex justify-between">
                  <span className={EXIT_TONE[reason] ?? 'text-ink-muted'}>{reason}</span>
                  <span className="text-ink">{n}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="rounded-lg border border-line bg-bg-surface p-4">
            <h3 className="mb-2 text-xs uppercase tracking-wider text-ink-muted">By symbol</h3>
            <div className="space-y-1 font-mono text-xs">
              {Object.entries(a.by_symbol).sort((x, y) => y[1].pnl - x[1].pnl).map(([sym, d]) => (
                <div key={sym} className="flex justify-between">
                  <span className="text-ink-muted">{sym} <span className="text-ink-subtle">({d.n})</span></span>
                  <span className={d.pnl >= 0 ? 'text-bull' : 'text-bear'}>{usd(d.pnl)}</span>
                </div>
              ))}
            </div>
          </div>
          <p className="text-[10px] leading-relaxed text-ink-subtle">
            Entries auto-logged from backtest fills — R-multiple = realized P&L ÷
            initial risk (|entry−stop|×qty). Live broker fills feed the same
            table once execution lands (Phase 3).
          </p>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-line bg-bg-surface p-3">
      <div className="text-xs text-ink-muted">{label}</div>
      <div className={`font-mono text-lg ${tone ?? 'text-ink'}`}>{value}</div>
    </div>
  );
}
