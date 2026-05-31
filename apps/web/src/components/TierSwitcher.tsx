'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, getDevTier, setDevTier } from '@/lib/api';
import type { Me } from '@/lib/types';

const TIERS = ['free', 'pro', 'elite', 'quant', 'enterprise'];

/**
 * Dev-only tier switcher. In production this whole control disappears —
 * the tier comes from the signed JWT (Clerk/Auth0), not a local toggle.
 * Here it lets you exercise entitlement gating end-to-end without a login.
 */
export function TierSwitcher() {
  const queryClient = useQueryClient();
  const [tier, setTier] = useState('free');
  const { data } = useQuery<Me>({ queryKey: ['me'], queryFn: api.me });

  useEffect(() => {
    setTier(getDevTier());
  }, []);

  const apply = (next: string) => {
    setDevTier(next);
    setTier(next);
    // Everything tier-dependent re-reads on invalidation.
    queryClient.invalidateQueries({ queryKey: ['me'] });
    queryClient.invalidateQueries({ queryKey: ['alerts'] });
    queryClient.invalidateQueries({ queryKey: ['watchlist'] });
  };

  const ent = data?.entitlements;

  return (
    <section className="rounded-lg border border-line bg-bg-surface p-4">
      <h2 className="mb-1 text-sm font-semibold">Account tier</h2>
      <p className="mb-3 text-xs text-ink-muted">
        Dev-mode switcher — exercises entitlement gating without a real login.
        Production reads the tier from a signed JWT (Clerk/Auth0); this control
        is hidden there.
      </p>
      <div className="flex flex-wrap gap-2">
        {TIERS.map((t) => (
          <button
            key={t}
            onClick={() => apply(t)}
            className={`rounded-full border px-3 py-1 font-mono text-xs uppercase tracking-wider ${
              t === tier ? 'border-accent text-accent' : 'border-line text-ink-muted hover:text-ink'
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      {ent && (
        <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs sm:grid-cols-3">
          <Item label="Watchlist" value={ent.watchlist_size ?? '∞'} />
          <Item label="Alerts" value={ent.max_alerts ?? '∞'} />
          <Item label="AI/day" value={ent.ai_explanations_per_day ?? '∞'} />
          <Item label="Backtest yrs" value={ent.backtest_years ?? '∞'} />
          <Item label="Auto-trade" value={ent.broker_autotrade ? 'yes' : 'no'} />
          <Item label="API/min" value={ent.api_rate_per_min} />
          <Item label="Assets" value={ent.asset_classes.length} />
          <Item label="Channels" value={ent.channels.join(',')} span />
        </dl>
      )}
    </section>
  );
}

function Item({ label, value, span }: { label: string; value: string | number; span?: boolean }) {
  return (
    <div className={span ? 'col-span-2 sm:col-span-3' : ''}>
      <dt className="inline text-ink-muted">{label}: </dt>
      <dd className="inline text-ink">{value}</dd>
    </div>
  );
}
