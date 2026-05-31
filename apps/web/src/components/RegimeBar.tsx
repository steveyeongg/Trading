'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { useLiveSubjects } from '@/lib/stream';
import type { RegimeSnapshot } from '@/lib/types';

const REGIME_COLOR: Record<string, string> = {
  'risk-on': 'text-bull',
  'risk-off': 'text-bear',
  'late-cycle': 'text-warn',
  stagflation: 'text-bear',
  unknown: 'text-ink-muted',
};

function fmt(n: number | undefined, digits = 2): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—';
  return n.toFixed(digits);
}

export function RegimeBar() {
  // Live via WebSocket; the poll is a slow fallback if the socket is down.
  useLiveSubjects(['regime.global']);
  const { data, isLoading } = useQuery<RegimeSnapshot>({
    queryKey: ['regime'],
    queryFn: api.regime,
    refetchInterval: 120_000,
  });

  if (isLoading) {
    return <div className="h-10 animate-pulse rounded bg-bg-surface" />;
  }
  if (!data) return null;

  const regime = data.regime;
  const color = REGIME_COLOR[regime] ?? 'text-ink-muted';

  return (
    <div className="flex items-center gap-6 rounded-md border border-line bg-bg-surface px-4 py-2 font-mono text-sm">
      <div>
        <span className="text-ink-muted">Regime:</span>{' '}
        <span className={`font-semibold ${color}`}>{regime}</span>
        <span className="text-ink-subtle"> ({(data.regime_confidence * 100).toFixed(0)}%)</span>
      </div>
      <Stat label="VIX" value={fmt(data.vix, 2)} />
      <Stat label="VIX·z" value={fmt(data.vix_z)} tone={signTone(data.vix_z, true)} />
      <Stat label="Spread 10Y-2Y" value={fmt(data.term_spread, 2)} />
      <Stat label="DXY·z" value={fmt(data.dxy_z)} tone={signTone(data.dxy_z, true)} />
      <Stat label="CPI·yoy" value={data.cpi_yoy !== undefined ? (data.cpi_yoy * 100).toFixed(2) + '%' : '—'} />
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'bull' | 'bear' | undefined }) {
  const toneClass = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-ink';
  return (
    <div className="flex items-baseline gap-1">
      <span className="text-ink-muted">{label}</span>
      <span className={toneClass}>{value}</span>
    </div>
  );
}

function signTone(v: number | undefined, inverse = false): 'bull' | 'bear' | undefined {
  if (v === undefined || v === null) return undefined;
  if (Math.abs(v) < 0.2) return undefined;
  const positive = v > 0;
  return (positive !== inverse) ? 'bull' : 'bear';
}
