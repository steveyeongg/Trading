'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';

/**
 * Renders `/v1/providers/status` — BLUEPRINT §13.1.
 *
 * Surfaces which external dependencies are configured + the documented
 * fallback for each. The §17 policy block is rendered as a footer so the
 * user understands missing keys won't crash anything.
 */
export function ProvidersStatusPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ['providers-status'],
    queryFn: api.providersStatus,
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="rounded-lg border border-line bg-bg-surface p-4 text-sm text-ink-muted">
        Loading provider status…
      </div>
    );
  }
  if (!data) return null;

  return (
    <div className="space-y-4 rounded-lg border border-line bg-bg-surface p-4">
      <header className="flex items-baseline justify-between">
        <h3 className="text-xs uppercase tracking-wider text-ink-muted">
          Provider status
        </h3>
        <span className="font-mono text-xs text-ink-subtle">
          {data.summary.available} of {data.summary.total} available
        </span>
      </header>

      <div className="space-y-3">
        {Object.entries(data.providers).map(([category, rows]) => (
          <div key={category}>
            <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-muted">
              {category.replace('_', ' ')}
            </h4>
            <ul className="space-y-1">
              {rows.map((p) => (
                <li
                  key={p.name}
                  className="flex items-baseline justify-between gap-3 font-mono text-xs"
                >
                  <span className="flex items-center gap-2">
                    <span
                      className={`inline-block h-2 w-2 rounded-full ${
                        p.available ? 'bg-bull' : 'bg-ink-subtle'
                      }`}
                    />
                    <span className="text-ink">{p.name}</span>
                  </span>
                  <span className="text-right text-ink-muted">
                    {p.available ? 'configured' : `fallback: ${p.fallback}`}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <p className="text-xs text-ink-subtle">
        Missing optional keys never crash the pipeline. Telegram missing →
        log channel still active. DeepSeek missing → templated rationale.
        FRED missing → synthetic macro series.
      </p>
    </div>
  );
}
