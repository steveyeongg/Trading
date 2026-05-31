'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { AlertDelivery } from '@/lib/types';

function shortTs(s: string): string {
  return s.slice(0, 19).replace('T', ' ');
}

export function AlertDeliveries() {
  const { data, isLoading } = useQuery({
    queryKey: ['alert-deliveries'],
    queryFn: () => api.getDeliveries(50),
    refetchInterval: 15_000,
  });

  return (
    <section className="rounded-lg border border-line bg-bg-surface p-4">
      <h2 className="mb-3 text-sm font-semibold">Recent deliveries</h2>
      {isLoading ? (
        <div className="text-xs text-ink-muted">Loading…</div>
      ) : !data || data.deliveries.length === 0 ? (
        <div className="text-xs text-ink-subtle">
          Nothing fired yet. Alerts evaluate against live signals from the stream —
          keep the dashboard open with a watchlist, or lower a rule&apos;s threshold.
        </div>
      ) : (
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="text-ink-muted">
              <th className="text-left">When</th>
              <th className="text-left">Symbol</th>
              <th className="text-left">Channel</th>
              <th className="text-left">Result</th>
            </tr>
          </thead>
          <tbody>
            {data.deliveries.map((d: AlertDelivery, i: number) => (
              <tr key={i} className="border-t border-line">
                <td className="py-1 text-ink-subtle">{shortTs(d.fired_at)}</td>
                <td>{d.symbol}</td>
                <td className="text-accent">{d.channel}</td>
                <td className={d.ok ? 'text-bull' : 'text-bear'}>
                  {d.ok ? 'ok' : 'fail'}{d.detail ? ` · ${d.detail}` : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
