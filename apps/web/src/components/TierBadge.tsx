'use client';

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { Me } from '@/lib/types';

const TIER_COLOR: Record<string, string> = {
  free: 'text-ink-muted border-line',
  pro: 'text-accent border-accent',
  elite: 'text-bull border-bull',
  quant: 'text-bull border-bull',
  enterprise: 'text-warn border-warn',
};

export function TierBadge() {
  const { data } = useQuery<Me>({ queryKey: ['me'], queryFn: api.me, staleTime: 30_000 });
  const tier = data?.tier ?? 'free';
  return (
    <span
      className={`rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${TIER_COLOR[tier] ?? TIER_COLOR.free}`}
      title={data?.email ?? undefined}
    >
      {tier}
    </span>
  );
}
