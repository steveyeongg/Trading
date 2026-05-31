import { RegimeBar } from '@/components/RegimeBar';
import { Watchlist } from '@/components/Watchlist';

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <RegimeBar />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside>
          <Watchlist />
          <p className="mt-3 px-1 text-xs text-ink-subtle">
            Click any ticker to see the full signal card.
          </p>
        </aside>

        <section className="space-y-4">
          <div className="rounded-lg border border-line bg-bg-surface p-6">
            <h2 className="text-lg font-semibold">Welcome to ATLAS</h2>
            <p className="mt-2 text-sm text-ink-muted">
              Pick a ticker from the watchlist to view its multi-engine signal.
              Each symbol page surfaces the technical, quant, macro, sentiment,
              and liquidity sub-scores, plus the risk-sized trade plan and the
              generated rationale.
            </p>
            <ul className="mt-3 list-disc pl-5 text-sm text-ink-muted">
              <li>Regime + macro snapshot above is refreshed by{' '}
                <code className="font-mono text-ink">macro_engine.refresh</code>.
              </li>
              <li>Sentiment is pulled from{' '}
                <code className="font-mono text-ink">news_ingest</code>{' '}
                — refresh hourly.
              </li>
              <li>
                Tickers showing <span className="font-mono text-ink-muted">no bars</span>{' '}
                need ingestion first:{' '}
                <code className="font-mono text-ink">uv run python -m ingest_equities synthetic --symbols AAPL --n-bars 1500</code>.
              </li>
            </ul>
          </div>
        </section>
      </div>
    </div>
  );
}
