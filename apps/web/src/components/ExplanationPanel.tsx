'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';

/**
 * Structured BLUEPRINT §10.3 explanation — calls `/v1/explain/signal` which
 * returns the deterministic templated payload (or DeepSeek when configured),
 * with the safety-repair pass already applied server-side.
 *
 * Renders the no-signal reason when the gates killed the candidate so the
 * user can see *why* — closes the §11.2 #12 gap.
 */
export function ExplanationPanel({ symbol }: { symbol: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['explain', symbol],
    queryFn: () => api.explainSignal(symbol),
    enabled: !!symbol,
    // Bounded refresh — the server caches by composite-bucket so repeat calls
    // within 15min are nearly free; a 60s poll keeps the panel current.
    refetchInterval: 60_000,
    retry: false,
  });

  if (isLoading) {
    return (
      <Card title="Explanation">
        <p className="text-sm text-ink-muted">Generating explanation…</p>
      </Card>
    );
  }

  if (error) {
    return (
      <Card title="Explanation">
        <p className="text-sm text-warn">Failed to load explanation.</p>
      </Card>
    );
  }

  if (!data) return null;

  if (!data.explained) {
    return (
      <Card title="No signal">
        <p className="text-sm text-ink">
          ATLAS didn&apos;t publish a signal for {symbol}.
        </p>
        <p className="mt-2 font-mono text-xs text-ink-muted">
          {data.no_signal_reason ?? 'gated'}
        </p>
        <p className="mt-3 text-xs text-ink-subtle">
          The deterministic scorer needs both composite ≥ threshold and ≥2 engines
          confirming the direction; risk vetoes (stale data, adverse news,
          drawdown circuit) override.
        </p>
      </Card>
    );
  }

  const p = data.payload!;
  return (
    <Card title="Explanation" badge={p.source}>
      <Section heading="Summary">{p.summary || '—'}</Section>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Bullets heading="Bull case" items={p.bull_case} tone="bull" />
        <Bullets heading="Bear case" items={p.bear_case} tone="bear" />
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Section heading="Why entry">{p.why_entry || '—'}</Section>
        <Section heading="Why stop">{p.why_stop || '—'}</Section>
        <Section heading="Target logic">{p.target_logic || '—'}</Section>
      </div>

      <Section heading="Confidence">{p.confidence_comment || '—'}</Section>
      <Section heading="Final view">{p.final_view || '—'}</Section>

      {p.safety_repaired && (
        <p className="mt-2 rounded border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn">
          Safety repair applied — forbidden phrasing was rewritten before display.
        </p>
      )}
      <p className="mt-3 text-xs text-ink-subtle">
        Informational only. Not financial advice. Trading involves risk.
      </p>
    </Card>
  );
}

function Card({
  title,
  badge,
  children,
}: {
  title: string;
  badge?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3 rounded-lg border border-line bg-bg-surface p-4">
      <div className="flex items-baseline justify-between">
        <h3 className="text-xs uppercase tracking-wider text-ink-muted">{title}</h3>
        {badge && (
          <span className="font-mono text-xs text-ink-subtle">{badge}</span>
        )}
      </div>
      {children}
    </div>
  );
}

function Section({ heading, children }: { heading: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="text-xs font-medium text-ink-muted">{heading}</h4>
      <p className="mt-1 text-sm leading-relaxed text-ink">{children}</p>
    </div>
  );
}

function Bullets({
  heading,
  items,
  tone,
}: {
  heading: string;
  items: string[];
  tone: 'bull' | 'bear';
}) {
  const color = tone === 'bull' ? 'text-bull' : 'text-bear';
  if (!items.length) {
    return (
      <div>
        <h4 className={`text-xs font-medium ${color}`}>{heading}</h4>
        <p className="mt-1 text-sm text-ink-subtle">_(none)_</p>
      </div>
    );
  }
  return (
    <div>
      <h4 className={`text-xs font-medium ${color}`}>{heading}</h4>
      <ul className="mt-1 list-disc pl-5 text-sm leading-relaxed text-ink">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}
