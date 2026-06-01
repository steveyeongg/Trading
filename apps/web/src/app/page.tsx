import Link from 'next/link';
import { ManualSymbolInput } from '@/components/ManualSymbolInput';
import { RegimeBar } from '@/components/RegimeBar';
import { Watchlist } from '@/components/Watchlist';

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <RegimeBar />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside className="space-y-4">
          <ManualSymbolInput />
          <Watchlist />
          <p className="px-1 text-xs text-ink-subtle">
            Click any ticker to see the full signal card.
          </p>
        </aside>

        <section className="space-y-4">
          <div className="rounded-lg border border-line bg-bg-surface p-6">
            <h2 className="text-lg font-semibold">Start here</h2>
            <ul className="mt-3 space-y-2 text-sm text-ink-muted">
              <li>
                <Link href="/scanner" className="font-medium text-accent hover:underline">
                  Open the scanner →
                </Link>{' '}
                rank a universe (or manual list) by composite score with entry,
                stop, T1 / T2 / T3 and the gate reason for symbols that
                didn&apos;t publish.
              </li>
              <li>
                Use the quick-scan box to investigate any symbol — one ticker
                jumps to its detail page; multiple tickers route to the scanner.
              </li>
              <li>
                <Link href="/alerts" className="font-medium text-accent hover:underline">
                  Alert rules →
                </Link>{' '}
                fire on composite-score crossings; cooldowns prevent duplicate
                notifications.
              </li>
            </ul>
          </div>

          <div className="rounded-lg border border-line bg-bg-surface p-6">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-ink-muted">
              How signals are formed
            </h3>
            <p className="mt-2 text-sm text-ink-muted">
              Eight engines feed the composite score: technical, quant
              (XGBoost), news, sentiment, macro, options, liquidity, risk.
              Weights match BLUEPRINT §8.2 (tech and quant dominate). Every
              candidate carries either a published trade plan or a
              human-readable reason for being gated.
            </p>
            <ul className="mt-3 list-disc pl-5 text-sm text-ink-muted">
              <li>
                Regime + macro snapshot above is refreshed by{' '}
                <code className="font-mono text-ink">macro_engine.refresh</code>.
              </li>
              <li>
                Sentiment is pulled from{' '}
                <code className="font-mono text-ink">news_ingest</code> — refresh hourly.
              </li>
              <li>
                Symbols showing <span className="font-mono text-ink-muted">no bars</span>{' '}
                need ingestion first:{' '}
                <code className="font-mono text-ink">
                  uv run python -m ingest_equities synthetic --symbols AAPL --n-bars 1500
                </code>
                .
              </li>
            </ul>
          </div>

          <p className="px-1 text-xs text-ink-subtle">
            ATLAS outputs are informational and educational only — not financial
            advice or a guarantee of performance. Always verify signals and
            manage risk independently.
          </p>
        </section>
      </div>
    </div>
  );
}
