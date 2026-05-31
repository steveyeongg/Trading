'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { Order } from '@/lib/types';

function shortTs(s: string): string {
  return s.slice(0, 19).replace('T', ' ');
}

export function OrdersPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ['orders'],
    queryFn: () => api.getOrders(50),
    refetchInterval: 20_000,
  });

  return (
    <div className="rounded-lg border border-line bg-bg-surface">
      <div className="border-b border-line px-4 py-2 text-xs uppercase tracking-wider text-ink-muted">
        Orders
      </div>
      {isLoading ? (
        <div className="p-4 text-xs text-ink-muted">Loading…</div>
      ) : !data || data.orders.length === 0 ? (
        <div className="p-4 text-xs text-ink-subtle">
          No orders yet. Paper-trade a signal from a symbol page (Elite tier+).
        </div>
      ) : (
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="text-ink-muted">
              <th className="px-3 py-2 text-left">When</th>
              <th className="px-2 text-left">Symbol</th>
              <th className="px-2 text-left">Side</th>
              <th className="px-2 text-right">Qty</th>
              <th className="px-2 text-right">Fill</th>
              <th className="px-2 text-right">P&L</th>
              <th className="px-2 text-left">Status</th>
              <th className="px-3 text-left">Broker</th>
            </tr>
          </thead>
          <tbody>
            {data.orders.map((o: Order) => (
              <tr key={o.id} className="border-t border-line">
                <td className="px-3 py-1 text-ink-subtle">{shortTs(o.created_at)}</td>
                <td className="px-2">{o.symbol}</td>
                <td className={`px-2 ${o.side === 'buy' ? 'text-bull' : 'text-bear'}`}>{o.side}/{o.intent}</td>
                <td className="px-2 text-right">{o.quantity}</td>
                <td className="px-2 text-right">{o.fill_price ? `$${o.fill_price.toFixed(2)}` : '—'}</td>
                <td className={`px-2 text-right ${(o.realized_pnl ?? 0) >= 0 ? 'text-bull' : 'text-bear'}`}>
                  {o.realized_pnl !== null ? `$${o.realized_pnl.toFixed(0)}` : '—'}
                </td>
                <td className={`px-2 ${o.status === 'filled' ? 'text-bull' : 'text-bear'}`}>{o.status}</td>
                <td className="px-3 text-ink-subtle">{o.broker}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
