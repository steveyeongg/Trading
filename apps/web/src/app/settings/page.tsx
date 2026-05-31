'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, ApiError } from '@/lib/api';
import { AlertRules } from '@/components/AlertRules';
import { AlertDeliveries } from '@/components/AlertDeliveries';
import { TierSwitcher } from '@/components/TierSwitcher';

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['watchlist'],
    queryFn: api.getWatchlist,
  });

  const [symbols, setSymbols] = useState<string[]>([]);
  const [input, setInput] = useState('');
  const [saved, setSaved] = useState(false);

  // Hydrate local editor state once the server list arrives.
  useEffect(() => {
    if (data?.symbols) setSymbols(data.symbols);
  }, [data?.symbols]);

  const mutation = useMutation({
    mutationFn: (next: string[]) => api.setWatchlist(next),
    onSuccess: (res) => {
      // Push the saved list into the shared cache so the watchlist sidebar
      // updates everywhere immediately.
      queryClient.setQueryData(['watchlist'], { symbols: res.symbols });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  const add = () => {
    const cleaned = input
      .split(/[\s,]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!cleaned.length) return;
    setSymbols((prev) => Array.from(new Set([...prev, ...cleaned])));
    setInput('');
  };

  const remove = (sym: string) => setSymbols((prev) => prev.filter((s) => s !== sym));

  const dirty = JSON.stringify(symbols) !== JSON.stringify(data?.symbols ?? []);

  if (isLoading) return <div className="text-sm text-ink-muted">Loading settings…</div>;

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-lg font-semibold">Settings</h1>

      <TierSwitcher />

      <section className="rounded-lg border border-line bg-bg-surface p-4">
        <h2 className="mb-1 text-sm font-semibold">Watchlist</h2>
        <p className="mb-4 text-xs text-ink-muted">
          Symbols shown on the dashboard sidebar and streamed live. Up to 50.
          {data?.fallback && (
            <span className="ml-1 text-warn">
              (showing fallback — Postgres unreachable; edits won&apos;t persist)
            </span>
          )}
        </p>

        <div className="mb-3 flex flex-wrap gap-2">
          {symbols.map((sym) => (
            <span
              key={sym}
              className="flex items-center gap-1.5 rounded border border-line bg-bg-subtle px-2 py-1 font-mono text-xs"
            >
              {sym}
              <button
                onClick={() => remove(sym)}
                className="text-ink-subtle hover:text-bear"
                aria-label={`remove ${sym}`}
              >
                ×
              </button>
            </span>
          ))}
          {symbols.length === 0 && <span className="text-xs text-ink-subtle">No symbols.</span>}
        </div>

        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                add();
              }
            }}
            placeholder="AAPL, MSFT, NVDA…"
            className="flex-1 rounded border border-line bg-bg-subtle px-3 py-1.5 font-mono text-sm placeholder:text-ink-subtle"
          />
          <button
            onClick={add}
            className="rounded border border-line-strong px-3 py-1.5 text-sm hover:bg-bg-subtle"
          >
            Add
          </button>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={() => mutation.mutate(symbols)}
            disabled={!dirty || mutation.isPending}
            className="rounded bg-accent px-4 py-1.5 text-sm font-semibold uppercase tracking-wider text-bg hover:opacity-90 disabled:opacity-40"
          >
            {mutation.isPending ? 'Saving…' : 'Save'}
          </button>
          {dirty && <span className="text-xs text-warn">unsaved changes</span>}
          {saved && <span className="text-xs text-bull">saved ✓</span>}
          {mutation.isError && (
            <span className="text-xs text-bear">
              {mutation.error instanceof ApiError && mutation.error.status === 503
                ? 'store unavailable — start Postgres + migrate'
                : 'save failed'}
            </span>
          )}
        </div>
      </section>

      <AlertRules />
      <AlertDeliveries />

      <section className="rounded-lg border border-line bg-bg-surface p-4 text-xs text-ink-muted">
        <h2 className="mb-2 text-sm font-semibold text-ink">Coming soon</h2>
        <ul className="list-disc space-y-1 pl-5">
          <li>Broker connection for one-click trade execution.</li>
          <li>Per-user accounts &amp; tiers (Clerk/Auth0).</li>
          <li>Model + provider selection (trend model version, LLM for rationales).</li>
        </ul>
        <p className="mt-3">
          Channel credentials are env-configured on the server:{' '}
          <code className="font-mono text-ink">ALERT_WEBHOOK_URL</code>,{' '}
          <code className="font-mono text-ink">TELEGRAM_BOT_TOKEN/CHAT_ID</code>,{' '}
          <code className="font-mono text-ink">SMTP_*</code>. Unconfigured channels are skipped.
        </p>
      </section>
    </div>
  );
}
