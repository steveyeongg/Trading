'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '@/lib/api';
import type { ExecuteResult, Me, Signal } from '@/lib/types';
import { SubScoreBars } from './SubScoreBars';

const CONVICTION_PILL: Record<string, string> = {
  'very-high': 'bg-bull text-bg',
  high:        'bg-bull-soft text-bull border border-bull',
  medium:      'bg-bg-subtle text-ink border border-line-strong',
  low:         'bg-bg-subtle text-ink-muted border border-line',
};

function fmtPrice(p: number | null): string {
  if (p === null || p === undefined) return '—';
  return `$${p.toFixed(2)}`;
}

export function SignalCard({ signal, veto }: { signal: Signal | null; veto?: { reason: string; detail: string } | null }) {
  if (!signal) {
    return (
      <div className="rounded-lg border border-line bg-bg-surface p-4 font-mono text-sm">
        <div className="text-ink-muted">No published signal.</div>
        {veto && (
          <div className="mt-2 text-xs">
            <span className="text-warn">vetoed:</span>{' '}
            <span className="text-ink">{veto.reason}</span>{' '}
            {veto.detail && <span className="text-ink-subtle">— {veto.detail}</span>}
          </div>
        )}
      </div>
    );
  }

  const isLong = signal.direction === 'long';
  const dirColor = isLong ? 'text-bull' : 'text-bear';
  const dirArrow = isLong ? '▲' : '▼';

  return (
    <div className="rounded-lg border border-line bg-bg-surface p-4 font-mono text-sm">
      <header className="flex items-center justify-between border-b border-line pb-3">
        <div>
          <div className="text-xs text-ink-muted">{signal.horizon} · {signal.asset_class}</div>
          <div className="flex items-baseline gap-3">
            <h2 className="text-2xl font-semibold tracking-tight">{signal.symbol}</h2>
            <span className={`text-xl ${dirColor}`}>{dirArrow} {signal.direction.toUpperCase()}</span>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className={`rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wider ${CONVICTION_PILL[signal.conviction]}`}>
            {signal.conviction}
          </span>
          <div className="text-right">
            <div className="text-xs text-ink-muted">composite</div>
            <div className={`text-2xl font-bold ${signal.composite_score >= 0 ? 'text-bull' : 'text-bear'}`}>
              {signal.composite_score >= 0 ? '+' : ''}
              {signal.composite_score.toFixed(0)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs text-ink-muted">confidence</div>
            <div className="text-2xl font-bold text-ink">
              {signal.confidence_pct.toFixed(0)}%
            </div>
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-6 py-4 lg:grid-cols-[1fr_auto]">
        <div>
          <div className="mb-2 text-xs uppercase tracking-wider text-ink-muted">Sub-scores</div>
          <SubScoreBars subs={signal.sub_scores} composite={signal.composite_score} />
        </div>

        <div className="rounded-md border border-line bg-bg-subtle p-4 text-xs lg:w-72">
          <div className="mb-2 text-ink-muted uppercase tracking-wider">Trade Plan</div>
          <Field label="Entry"    value={fmtPrice(signal.entry_price)} />
          <Field label="Stop"     value={fmtPrice(signal.stop_price)} tone="bear" />
          {signal.take_profit_levels.map((t, i) => (
            <Field key={i} label={`T${i + 1}`} value={fmtPrice(t)} tone="bull" />
          ))}
          <hr className="my-2 border-line" />
          <Field label="Size" value={signal.position_size_pct === null ? '—' : `${signal.position_size_pct.toFixed(2)}%`} />
          <Field label="R:R"  value={signal.expected_rr === null ? '—' : `1 : ${signal.expected_rr.toFixed(1)}`} />
          <Field label="Regime" value={signal.regime} />
          <hr className="my-2 border-line" />
          <ExecuteButton signal={signal} />
        </div>
      </div>

      {signal.rationale_md && (
        <details className="border-t border-line pt-3">
          <summary className="cursor-pointer text-xs uppercase tracking-wider text-ink-muted">
            Rationale
          </summary>
          <pre className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-ink">
            {signal.rationale_md}
          </pre>
        </details>
      )}
    </div>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: 'bull' | 'bear' }) {
  const toneClass = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-ink';
  return (
    <div className="flex items-baseline justify-between py-0.5">
      <span className="text-ink-muted">{label}</span>
      <span className={toneClass}>{value}</span>
    </div>
  );
}

// Paper-trade the plan. Quantity sized from position_size_pct against a
// notional 100k book (matches the seeded portfolio cash). Tier-gated:
// disabled unless the user's tier allows broker autotrade.
function ExecuteButton({ signal }: { signal: Signal }) {
  const queryClient = useQueryClient();
  const { data: me } = useQuery<Me>({ queryKey: ['me'], queryFn: api.me });
  const canTrade = me?.entitlements.broker_autotrade ?? false;

  const entry = signal.entry_price ?? 0;
  const sizePct = signal.position_size_pct ?? 1;
  const qty = entry > 0 ? Math.max(1, Math.floor((sizePct / 100) * 100_000 / entry)) : 0;

  const mutation = useMutation<ExecuteResult, Error>({
    mutationFn: () =>
      api.execute({
        symbol: signal.symbol,
        intent: 'open',
        quantity: qty,
        limit_price: signal.entry_price,
        signal_ref: signal.id,
        // Full plan → the server monitor runs partial ladder exits + a
        // break-even/chandelier trailing stop. ATR ≈ initial risk / 1.8.
        stop: signal.stop_price,
        targets: signal.take_profit_levels ?? [],
        atr:
          signal.entry_price != null && signal.stop_price != null
            ? Math.abs(signal.entry_price - signal.stop_price) / 1.8
            : null,
        direction: signal.direction,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['portfolio', 'default'] });
      queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  if (!canTrade) {
    return (
      <div className="rounded border border-line bg-bg px-3 py-2 text-center text-[10px] text-ink-subtle">
        Paper execution requires Elite tier+. Switch tier in Settings.
      </div>
    );
  }

  const res = mutation.data;
  return (
    <div>
      <button
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || qty <= 0}
        className="w-full rounded bg-accent px-3 py-2 font-semibold uppercase tracking-wider text-bg hover:opacity-90 disabled:opacity-40"
      >
        {mutation.isPending ? 'Submitting…' : `Paper Buy ${qty}`}
      </button>
      {res && (
        <div className={`mt-1 text-center text-[10px] ${res.ok ? 'text-bull' : 'text-bear'}`}>
          {res.ok ? `filled @ $${res.fill_price?.toFixed(2)} (#${res.order_id?.slice(0, 8)})` : `rejected: ${res.detail}`}
        </div>
      )}
      {mutation.isError && (
        <div className="mt-1 text-center text-[10px] text-bear">
          {mutation.error instanceof ApiError && mutation.error.status === 503
            ? 'execution store unavailable — start Postgres + migrate'
            : 'execution failed'}
        </div>
      )}
    </div>
  );
}
