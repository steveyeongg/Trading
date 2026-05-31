'use client';

import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '@/lib/api';
import { EquityCurve } from '@/components/EquityCurve';
import type { BacktestRequest, BacktestResponse } from '@/lib/types';

const METRIC_ROWS: Array<{ key: string; label: string; pct?: boolean; signed?: boolean }> = [
  { key: 'n_trades', label: 'Trades' },
  { key: 'hit_rate', label: 'Hit rate', pct: true },
  { key: 'total_return', label: 'Total return', pct: true, signed: true },
  { key: 'cagr', label: 'CAGR', pct: true, signed: true },
  { key: 'max_drawdown', label: 'Max drawdown', pct: true },
  { key: 'sharpe', label: 'Sharpe', signed: true },
  { key: 'sharpe_ci_lo', label: 'Sharpe CI low', signed: true },
  { key: 'sharpe_ci_hi', label: 'Sharpe CI high', signed: true },
  { key: 'sortino', label: 'Sortino', signed: true },
  { key: 'calmar', label: 'Calmar', signed: true },
  { key: 'profit_factor', label: 'Profit factor' },
  { key: 'expectancy_per_trade', label: 'Expectancy/trade', signed: true },
  { key: 'avg_bars_held', label: 'Avg bars held' },
];

function fmt(v: number | undefined, pct?: boolean, signed?: boolean): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '—';
  if (pct) {
    const s = (v * 100).toFixed(2) + '%';
    return signed && v > 0 ? '+' + s : s;
  }
  const s = v.toFixed(v >= 100 ? 0 : 3);
  return signed && v > 0 ? '+' + s : s;
}

export default function BacktestPage() {
  const [req, setReq] = useState<BacktestRequest>({
    symbol: 'SYN',
    strategy: 'trend-follower',
    n_bars: 3000,
    seed: 11,
    initial_capital: 100_000,
    cost_multiplier: 1.0,
    cost_sweep: true,
  });

  const mutation = useMutation<BacktestResponse, Error, BacktestRequest>({
    mutationFn: (r) => api.backtest(r),
  });

  const result = mutation.data;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">Backtest</h1>

      <form
        className="flex flex-wrap items-end gap-4 rounded-lg border border-line bg-bg-surface p-4 font-mono text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate(req);
        }}
      >
        <Field label="Strategy">
          <select
            value={req.strategy}
            onChange={(e) => setReq({ ...req, strategy: e.target.value as BacktestRequest['strategy'] })}
            className="rounded border border-line bg-bg-subtle px-2 py-1"
          >
            <option value="trend-follower">trend-follower</option>
            <option value="atlas">atlas</option>
          </select>
        </Field>
        <Field label="Bars">
          <input
            type="number"
            value={req.n_bars}
            min={300}
            max={20000}
            onChange={(e) => setReq({ ...req, n_bars: Number(e.target.value) })}
            className="w-24 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <Field label="Seed">
          <input
            type="number"
            value={req.seed}
            onChange={(e) => setReq({ ...req, seed: Number(e.target.value) })}
            className="w-20 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <Field label="Cost ×">
          <input
            type="number"
            step="0.5"
            value={req.cost_multiplier}
            onChange={(e) => setReq({ ...req, cost_multiplier: Number(e.target.value) })}
            className="w-20 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={req.cost_sweep}
            onChange={(e) => setReq({ ...req, cost_sweep: e.target.checked })}
          />
          <span className="text-ink-muted">cost sweep</span>
        </label>
        <button
          type="submit"
          disabled={mutation.isPending}
          className="rounded bg-accent px-4 py-1.5 font-semibold uppercase tracking-wider text-bg hover:opacity-90 disabled:opacity-50"
        >
          {mutation.isPending ? 'Running…' : 'Run'}
        </button>
      </form>

      {mutation.isError && (
        <div className="rounded-lg border border-bear bg-bg-surface p-4 text-sm text-bear">
          {mutation.error.message}
        </div>
      )}

      {result && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
          <div className="space-y-4">
            <div className="rounded-lg border border-line bg-bg-surface p-3">
              <h3 className="mb-2 text-xs uppercase tracking-wider text-ink-muted">
                Equity curve · {result.strategy} · {result.n_trades} trades
              </h3>
              <EquityCurve curve={result.equity_curve} initialCapital={req.initial_capital ?? 100000} />
            </div>

            {result.cost_sweep && (
              <div className="rounded-lg border border-line bg-bg-surface p-4">
                <h3 className="mb-2 text-xs uppercase tracking-wider text-ink-muted">
                  Cost sensitivity
                </h3>
                <table className="w-full font-mono text-xs">
                  <thead>
                    <tr className="text-ink-muted">
                      <th className="text-left">multiplier</th>
                      <th className="text-right">return</th>
                      <th className="text-right">sharpe</th>
                      <th className="text-right">max dd</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(result.cost_sweep).map(([mult, m]) => (
                      <tr key={mult} className="border-t border-line">
                        <td>{mult}×</td>
                        <td className={`text-right ${(m.total_return ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>
                          {fmt(m.total_return, true, true)}
                        </td>
                        <td className="text-right">{fmt(m.sharpe, false, true)}</td>
                        <td className="text-right text-bear">{fmt(m.max_drawdown, true)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="rounded-lg border border-line bg-bg-surface p-4">
            <h3 className="mb-3 text-xs uppercase tracking-wider text-ink-muted">Metrics</h3>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 font-mono text-xs">
              {METRIC_ROWS.map((row) => {
                const v = result.metrics[row.key];
                const tone = row.signed && v !== undefined && !Number.isNaN(v)
                  ? v >= 0 ? 'text-bull' : 'text-bear'
                  : 'text-ink';
                return (
                  <div key={row.key} className="contents">
                    <dt className="text-ink-muted">{row.label}</dt>
                    <dd className={`text-right ${tone}`}>{fmt(v, row.pct, row.signed)}</dd>
                  </div>
                );
              })}
            </dl>
            <p className="mt-3 text-[10px] leading-relaxed text-ink-subtle">
              Synthetic GBM data. Trend-follower has no real edge on a random
              walk — negative Sharpe + cost-scaled drawdown is the expected,
              honest result. Real bars + the atlas strategy tell a different story.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wider text-ink-muted">{label}</span>
      {children}
    </label>
  );
}
