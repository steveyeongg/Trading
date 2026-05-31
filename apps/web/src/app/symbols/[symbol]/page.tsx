'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { api, ApiError } from '@/lib/api';
import { RegimeBar } from '@/components/RegimeBar';
import { Watchlist } from '@/components/Watchlist';
import { SignalCard } from '@/components/SignalCard';
import { PriceChart } from '@/components/PriceChart';
import type { SignalDebug } from '@/lib/types';

export default function SymbolPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = (params.symbol ?? '').toUpperCase();

  const { data, isLoading, error } = useQuery<SignalDebug>({
    queryKey: ['debug', symbol],
    queryFn: () => api.debug(symbol),
    enabled: !!symbol,
  });

  return (
    <div className="space-y-6">
      <RegimeBar />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[280px_1fr]">
        <aside>
          <Watchlist selected={symbol} />
          <Link href="/" className="mt-3 inline-block px-1 text-xs text-ink-muted hover:text-ink">
            ← back to dashboard
          </Link>
        </aside>

        <section className="space-y-4">
          {isLoading && (
            <div className="rounded-lg border border-line bg-bg-surface p-6 text-sm text-ink-muted">
              Loading {symbol}…
            </div>
          )}

          {error instanceof ApiError && error.status === 404 && (
            <NoBars symbol={symbol} />
          )}

          {error && !(error instanceof ApiError && error.status === 404) && (
            <div className="rounded-lg border border-bear bg-bg-surface p-6 text-sm text-bear">
              Failed to load: {(error as Error).message}
            </div>
          )}

          {data && (
            <>
              <PriceChart symbol={symbol} signal={data.signal} />

              <SignalCard signal={data.signal} veto={data.veto} />

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Panel title="Macro Snapshot">
                  {data.macro_snapshot ? (
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs">
                      <Item label="Regime"        value={data.macro_snapshot.regime} />
                      <Item label="Confidence"    value={`${(data.macro_snapshot.regime_confidence * 100).toFixed(0)}%`} />
                      <Item label="VIX"           value={fmt(data.macro_snapshot.vix, 2)} />
                      <Item label="VIX z-score"   value={fmt(data.macro_snapshot.vix_z, 2)} />
                      <Item label="10Y-2Y spread" value={fmt(data.macro_snapshot.term_spread, 2)} />
                      <Item label="DXY z-score"   value={fmt(data.macro_snapshot.dxy_z, 2)} />
                    </dl>
                  ) : (
                    <Empty>no snapshot in Redis — run <code>macro_engine.refresh</code></Empty>
                  )}
                </Panel>

                <Panel title="Sentiment Snapshot">
                  {data.sentiment_snapshot && data.sentiment_snapshot.news_count > 0 ? (
                    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs">
                      <Item label="News score"      value={fmt(data.sentiment_snapshot.news_sent_score, 2)} />
                      <Item label="Confidence"      value={fmt(data.sentiment_snapshot.news_sent_confidence, 2)} />
                      <Item label="Items (48h)"     value={data.sentiment_snapshot.news_count.toFixed(0)} />
                    </dl>
                  ) : (
                    <Empty>no news for {symbol} in last 48h — run <code>news_ingest.refresh</code></Empty>
                  )}
                </Panel>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-bg-surface p-4">
      <h3 className="mb-3 text-xs uppercase tracking-wider text-ink-muted">{title}</h3>
      {children}
    </div>
  );
}

function Item({ label, value }: { label: string; value: string | number }) {
  return (
    <>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="text-right text-ink">{value}</dd>
    </>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-xs text-ink-muted">{children}</div>;
}

function fmt(n: number | undefined, digits = 2): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—';
  return n.toFixed(digits);
}

function NoBars({ symbol }: { symbol: string }) {
  return (
    <div className="rounded-lg border border-line bg-bg-surface p-6 text-sm">
      <div className="text-warn">No bars stored for {symbol}.</div>
      <p className="mt-2 text-ink-muted">Ingest bars first:</p>
      <pre className="mt-2 rounded bg-bg-subtle p-3 font-mono text-xs">
{`# Real Polygon backfill
uv run python -m ingest_equities backfill --symbols ${symbol} --days 30

# Or offline synthetic
uv run python -m ingest_equities synthetic --symbols ${symbol} --n-bars 1500`}
      </pre>
    </div>
  );
}
