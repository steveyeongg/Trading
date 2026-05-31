'use client';

import { useQueries, useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { api, ApiError } from '@/lib/api';
import { useLiveSubjects } from '@/lib/stream';
import type { SignalDebug } from '@/lib/types';

const DEFAULT_WATCHLIST = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'SPY', 'QQQ', 'BTC', 'ETH'];

export function Watchlist({ symbols: override, selected }: { symbols?: string[]; selected?: string }) {
  // Symbols come from the server-side watchlist (editable in Settings). A
  // caller can still override; otherwise fetch with a hardcoded fallback.
  const { data: wl } = useQuery({
    queryKey: ['watchlist'],
    queryFn: api.getWatchlist,
    enabled: !override,
    staleTime: 60_000,
  });
  const symbols = override ?? wl?.symbols ?? DEFAULT_WATCHLIST;

  // Live signal pushes for every watchlist symbol; poll is the slow fallback.
  useLiveSubjects(symbols.map((s) => `signals.${s}`));
  const results = useQueries({
    queries: symbols.map((s) => ({
      queryKey: ['debug', s],
      queryFn: () => api.debug(s),
      refetchInterval: 60_000,
      retry: false,
    })),
  });

  return (
    <div className="rounded-lg border border-line bg-bg-surface">
      <div className="border-b border-line px-4 py-2 text-xs uppercase tracking-wider text-ink-muted">
        Watchlist
      </div>
      <ul className="divide-y divide-line">
        {symbols.map((sym, i) => {
          const q = results[i];
          const isSelected = selected === sym;
          return (
            <li key={sym}>
              <Link
                href={`/symbols/${sym}`}
                className={`flex items-center justify-between px-4 py-2 hover:bg-bg-subtle ${isSelected ? 'bg-bg-subtle' : ''}`}
              >
                <span className="font-mono text-sm">{sym}</span>
                <Cell debug={q.data} loading={q.isLoading} error={q.error} />
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Cell({ debug, loading, error }: { debug: SignalDebug | undefined; loading: boolean; error: unknown }) {
  if (loading) {
    return <span className="text-xs text-ink-subtle">…</span>;
  }
  if (error instanceof ApiError && error.status === 404) {
    return <span className="text-xs text-ink-subtle">no bars</span>;
  }
  if (error) {
    return <span className="text-xs text-warn">err</span>;
  }
  if (!debug) {
    return <span className="text-xs text-ink-subtle">—</span>;
  }
  // Sub-scores are always present in debug; composite is the published number
  // when a signal exists, else derive from sub_scores via the same default
  // weights (which we hard-code locally to avoid duplicating the engine).
  if (debug.signal) {
    const sig = debug.signal;
    const isBull = sig.composite_score >= 0;
    const arrow = isBull ? '▲' : '▼';
    const color = isBull ? 'text-bull' : 'text-bear';
    return (
      <span className={`font-mono text-sm ${color}`}>
        {arrow} {sig.composite_score >= 0 ? '+' : ''}{sig.composite_score.toFixed(0)}
      </span>
    );
  }
  return (
    <span className="font-mono text-xs text-ink-muted">
      {debug.veto?.reason ?? 'no signal'}
    </span>
  );
}
