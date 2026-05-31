'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, ApiError } from '@/lib/api';
import type { AlertRule, NewAlertRule } from '@/lib/types';

const METRICS = ['composite', 'confidence', 'tech', 'quant', 'macro', 'sent', 'liq'];
const OPS = ['>=', '<=', '>', '<', '=='];
const CHANNELS = ['log', 'webhook', 'telegram', 'email'];

const BLANK: NewAlertRule = {
  name: '',
  metric: 'composite',
  op: '>=',
  threshold: 70,
  symbol: '',
  channels: ['log'],
};

export function AlertRules() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({ queryKey: ['alerts'], queryFn: api.getAlerts });
  const [draft, setDraft] = useState<NewAlertRule>(BLANK);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['alerts'] });

  const create = useMutation({
    mutationFn: (r: NewAlertRule) => api.createAlert({ ...r, symbol: r.symbol || null }),
    onSuccess: () => {
      setDraft(BLANK);
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteAlert(id),
    onSuccess: invalidate,
  });

  const storeDown = error instanceof ApiError && error.status === 503;

  return (
    <section className="rounded-lg border border-line bg-bg-surface p-4">
      <h2 className="mb-1 text-sm font-semibold">Alert rules</h2>
      <p className="mb-4 text-xs text-ink-muted">
        Fire when a live signal crosses a threshold. Evaluated by the stream
        broadcaster; delivered to the chosen channels (cooldown-gated).
        {storeDown && <span className="ml-1 text-warn">Postgres unreachable — rules can&apos;t persist.</span>}
      </p>

      {/* Existing rules */}
      {isLoading ? (
        <div className="text-xs text-ink-muted">Loading…</div>
      ) : (
        <ul className="mb-4 space-y-1.5">
          {(data?.rules ?? []).map((r: AlertRule) => (
            <li key={r.id} className="flex items-center justify-between rounded border border-line bg-bg-subtle px-3 py-2 font-mono text-xs">
              <span>
                <span className="text-ink">{r.name}</span>{' '}
                <span className="text-ink-muted">
                  {r.symbol ? `${r.symbol} ` : ''}{r.metric} {r.op} {r.threshold}
                  {r.direction ? ` · ${r.direction}` : ''}
                </span>{' '}
                <span className="text-accent">[{r.channels.join(', ')}]</span>
                <span className="text-ink-subtle"> · {r.cooldown_s}s</span>
              </span>
              <button
                onClick={() => remove.mutate(r.id)}
                className="text-ink-subtle hover:text-bear"
                aria-label={`delete ${r.name}`}
              >
                ×
              </button>
            </li>
          ))}
          {data?.rules?.length === 0 && <li className="text-xs text-ink-subtle">No rules yet.</li>}
        </ul>
      )}

      {/* New rule form */}
      <div className="flex flex-wrap items-end gap-2 font-mono text-xs">
        <Field label="Name">
          <input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            placeholder="Strong composite"
            className="w-36 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <Field label="Symbol">
          <input
            value={draft.symbol ?? ''}
            onChange={(e) => setDraft({ ...draft, symbol: e.target.value.toUpperCase() })}
            placeholder="any"
            className="w-20 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <Field label="Metric">
          <select value={draft.metric} onChange={(e) => setDraft({ ...draft, metric: e.target.value })} className="rounded border border-line bg-bg-subtle px-2 py-1">
            {METRICS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
        <Field label="Op">
          <select value={draft.op} onChange={(e) => setDraft({ ...draft, op: e.target.value })} className="rounded border border-line bg-bg-subtle px-2 py-1">
            {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
        <Field label="Value">
          <input
            type="number"
            value={draft.threshold}
            onChange={(e) => setDraft({ ...draft, threshold: Number(e.target.value) })}
            className="w-20 rounded border border-line bg-bg-subtle px-2 py-1"
          />
        </Field>
        <Field label="Channels">
          <select
            multiple
            value={draft.channels}
            onChange={(e) => setDraft({ ...draft, channels: Array.from(e.target.selectedOptions, (o) => o.value) })}
            className="h-[26px] rounded border border-line bg-bg-subtle px-2 py-0.5"
          >
            {CHANNELS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </Field>
        <button
          onClick={() => draft.name && create.mutate(draft)}
          disabled={!draft.name || create.isPending}
          className="rounded bg-accent px-3 py-1 font-semibold uppercase tracking-wider text-bg hover:opacity-90 disabled:opacity-40"
        >
          Add
        </button>
      </div>
      {create.isError && (
        <div className="mt-2 text-xs text-bear">
          {create.error instanceof ApiError && create.error.status === 503
            ? 'store unavailable — start Postgres + migrate'
            : 'create failed'}
        </div>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="uppercase tracking-wider text-ink-subtle">{label}</span>
      {children}
    </label>
  );
}
