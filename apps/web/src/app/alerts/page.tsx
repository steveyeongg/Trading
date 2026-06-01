'use client';

import { AlertDeliveries } from '@/components/AlertDeliveries';
import { AlertRules } from '@/components/AlertRules';

export default function AlertsPage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Alerts</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Rules fire when composite scores cross thresholds. Cooldowns prevent
          duplicate fires. Configure channels (Telegram / webhook / email / log)
          via environment variables on the backend.
        </p>
      </header>

      <section>
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wider text-ink-muted">
          Rules
        </h2>
        <AlertRules />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wider text-ink-muted">
          Recent deliveries
        </h2>
        <AlertDeliveries />
      </section>

      <p className="text-xs text-ink-subtle">
        Alerts are informational. Every signal includes an invalidation —
        position management remains your responsibility.
      </p>
    </div>
  );
}
