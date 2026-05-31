'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { api, ApiError } from '@/lib/api';
import { OrdersPanel } from '@/components/OrdersPanel';
import type { Holding, PortfolioSummary } from '@/lib/types';

function usd(n: number): string {
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}
function pct(n: number, signed = false): string {
  const s = (n * 100).toFixed(2) + '%';
  return signed && n > 0 ? '+' + s : s;
}

export default function PortfolioPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery<PortfolioSummary>({
    queryKey: ['portfolio', 'default'],
    queryFn: () => api.portfolio('default'),
    refetchInterval: 30_000,
  });

  const closePosition = useMutation({
    mutationFn: (h: Holding) =>
      api.execute({ symbol: h.symbol, intent: 'close', quantity: h.quantity, limit_price: h.last_price }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio', 'default'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['journal'] });
    },
  });

  if (isLoading) {
    return <div className="text-sm text-ink-muted">Loading portfolio…</div>;
  }

  if (error) {
    const isConn = !(error instanceof ApiError) || error.status >= 500;
    return (
      <div className="rounded-lg border border-line bg-bg-surface p-6 text-sm">
        <div className="text-warn">Couldn&apos;t load the portfolio.</div>
        {isConn && (
          <>
            <p className="mt-2 text-ink-muted">
              Make sure Postgres is up, the schema is migrated, and demo positions are seeded:
            </p>
            <pre className="mt-2 rounded bg-bg-subtle p-3 font-mono text-xs">
{`docker compose -f infra/docker/docker-compose.yml up -d
uv run python -m atlas_shared.migrate up
uv run python -m portfolio_service.seed`}
            </pre>
          </>
        )}
      </div>
    );
  }

  if (!data) return null;

  const pnlTone = data.unrealized_pnl >= 0 ? 'text-bull' : 'text-bear';

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Portfolio</h1>

      {/* Top metric strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
        <Metric label="Equity" value={usd(data.equity)} />
        <Metric label="Cash" value={usd(data.cash)} />
        <Metric label="Invested" value={usd(data.invested)} />
        <Metric label="Unrealized P&L" value={usd(data.unrealized_pnl)} tone={pnlTone} sub={pct(data.unrealized_pct, true)} />
        <Metric label="VaR 95% (1d)" value={usd(data.var_95)} tone="text-bear" sub={pct(data.var_95_pct)} />
        <Metric label="Positions" value={String(data.n_positions)} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_280px]">
        {/* Holdings table */}
        <div className="rounded-lg border border-line bg-bg-surface">
          <div className="border-b border-line px-4 py-2 text-xs uppercase tracking-wider text-ink-muted">
            Holdings
          </div>
          {data.holdings.length === 0 ? (
            <div className="p-4 text-sm text-ink-muted">
              No open positions. Seed demo positions:{' '}
              <code className="font-mono text-ink">uv run python -m portfolio_service.seed</code>
            </div>
          ) : (
            <table className="w-full font-mono text-xs">
              <thead>
                <tr className="text-ink-muted">
                  <th className="px-4 py-2 text-left">Symbol</th>
                  <th className="px-2 text-right">Qty</th>
                  <th className="px-2 text-right">Avg cost</th>
                  <th className="px-2 text-right">Last</th>
                  <th className="px-2 text-right">Mkt value</th>
                  <th className="px-2 text-right">P&L</th>
                  <th className="px-2 text-right">Wt %</th>
                  <th className="px-2 text-right"> </th>
                </tr>
              </thead>
              <tbody>
                {data.holdings.map((h: Holding) => {
                  const tone = h.unrealized_pnl >= 0 ? 'text-bull' : 'text-bear';
                  const closing = closePosition.isPending && closePosition.variables?.symbol === h.symbol;
                  return (
                    <tr key={h.symbol} className="border-t border-line hover:bg-bg-subtle">
                      <td className="px-4 py-2">
                        <Link href={`/symbols/${h.symbol}`} className="text-accent hover:underline">
                          {h.symbol}
                        </Link>
                        <div className="text-[10px] text-ink-subtle">{h.sector ?? '—'}</div>
                      </td>
                      <td className="px-2 text-right">{h.quantity}</td>
                      <td className="px-2 text-right">${h.avg_cost.toFixed(2)}</td>
                      <td className="px-2 text-right">${h.last_price.toFixed(2)}</td>
                      <td className="px-2 text-right">{usd(h.market_value)}</td>
                      <td className={`px-2 text-right ${tone}`}>
                        {usd(h.unrealized_pnl)}
                        <div className="text-[10px]">{pct(h.unrealized_pct, true)}</div>
                      </td>
                      <td className="px-2 text-right">{h.weight_pct.toFixed(1)}%</td>
                      <td className="px-2 text-right">
                        <button
                          onClick={() => closePosition.mutate(h)}
                          disabled={closing}
                          className="rounded border border-line-strong px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-muted hover:border-bear hover:text-bear disabled:opacity-40"
                          title="Close position (paper, Elite tier+)"
                        >
                          {closing ? '…' : 'Close'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {closePosition.isError && (
            <div className="px-4 py-2 text-xs text-bear">
              {closePosition.error instanceof ApiError && closePosition.error.status === 403
                ? 'Closing requires Elite tier+ — switch tier in Settings.'
                : 'Close failed (store unavailable?).'}
            </div>
          )}
        </div>

        {/* Sector exposure */}
        <div className="rounded-lg border border-line bg-bg-surface p-4">
          <h3 className="mb-3 text-xs uppercase tracking-wider text-ink-muted">Sector exposure</h3>
          <div className="space-y-2 font-mono text-xs">
            {Object.entries(data.sector_exposure)
              .sort((a, b) => b[1] - a[1])
              .map(([sector, wt]) => (
                <div key={sector}>
                  <div className="flex justify-between">
                    <span className="text-ink-muted">{sector}</span>
                    <span className={wt > 30 ? 'text-warn' : 'text-ink'}>{wt.toFixed(1)}%</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded bg-bg-subtle">
                    <div
                      className={`h-full rounded ${wt > 30 ? 'bg-warn' : 'bg-accent'}`}
                      style={{ width: `${Math.min(100, wt)}%` }}
                    />
                  </div>
                </div>
              ))}
          </div>
          <p className="mt-3 text-[10px] leading-relaxed text-ink-subtle">
            Sectors above 30% are flagged (concentration cap, BLUEPRINT §10.3).
            VaR is a parametric 1-day 95% estimate assuming independence — a
            conservative headline; CVaR + correlation land in Phase 3.
          </p>
        </div>
      </div>

      <OrdersPanel />
    </div>
  );
}

function Metric({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-line bg-bg-surface p-3">
      <div className="text-xs text-ink-muted">{label}</div>
      <div className={`font-mono text-lg ${tone ?? 'text-ink'}`}>{value}</div>
      {sub && <div className={`font-mono text-xs ${tone ?? 'text-ink-muted'}`}>{sub}</div>}
    </div>
  );
}
