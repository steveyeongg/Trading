'use client';

import { useQuery } from '@tanstack/react-query';
import { useParams } from 'next/navigation';
import { DailyChart } from '@/components/DailyChart';
import { EligibilityChecklist } from '@/components/EligibilityChecklist';
import { LoadFailure } from '@/components/LoadFailure';
import { SectionHeading } from '@/components/SectionHeading';
import { StockIdea } from '@/components/StockIdea';
import { TermText } from '@/components/TermText';
import {
  DISCLAIMER_TEXT,
  STOCK_EMPTY_SYMBOL_TEXT,
  STOCK_NOT_FOUND_HINT,
  STOCK_POSITION_LINES_NOTE,
  STOCK_MARKET_DATA_LOADING_TEXT,
  stockLoadingText,
  stockNotFoundText,
} from '@/lib/copy';
import { ApiError } from '@/lib/api';
import { sessionDate } from '@/lib/format';
import { LOADING_RETRY_MS, isLoadingMarketData, isUnreachable, swingApi } from '@/lib/swing-api';
import type { StockOut } from '@/lib/swing-types';

const REFRESH_MS = 5 * 60_000; // the scan data changes once a night; no live stream here

/** The route's `[symbol]` as a ticker: decoded (it may be %-encoded), trimmed and upper-case. */
function routeSymbol(raw: string | string[] | undefined): string {
  const text = (Array.isArray(raw) ? raw[0] : raw) ?? '';
  let decoded = text;
  try {
    decoded = decodeURIComponent(text);
  } catch {
    // A stray "%" is not an escape; the text as it came is the best reading.
  }
  return decoded.trim().toUpperCase();
}

/** A narration from the scan, or null when it is missing or blank (a blank section is hidden). */
const sentence = (text: string | null | undefined) =>
  typeof text === 'string' && text.trim() !== '' ? text : null;

function NotFound({ symbol }: { symbol: string }) {
  return (
    <div
      role="status"
      className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted"
    >
      <p className="text-ink">{stockNotFoundText(symbol)}</p>
      <p className="mt-1">{STOCK_NOT_FOUND_HINT}</p>
    </div>
  );
}

function Narration({ id, title, text }: { id: string; title: string; text: string }) {
  return (
    <section
      aria-labelledby={id}
      className="min-w-0 rounded-lg border border-line bg-bg-surface p-4"
    >
      <SectionHeading id={id}>{title}</SectionHeading>
      <p className="mt-2 break-words text-sm leading-relaxed text-ink">
        <TermText>{text}</TermText>
      </p>
    </section>
  );
}

function StockView({ stock }: { stock: StockOut }) {
  const { idea } = stock;
  const why = sentence(stock.why);
  const cancels = sentence(stock.cancels);
  return (
    <div className="space-y-6">
      <section
        aria-label={`${stock.symbol} daily chart`}
        className="min-w-0 rounded-lg border border-line bg-bg-surface p-3"
      >
        <DailyChart key={stock.symbol} bars={stock.bars} lines={stock.lines} levels={stock.levels} />
        {idea === null && stock.levels !== null && (
          // The server draws levels without an idea only for a stock that is an open position.
          <p className="mt-1 text-xs text-ink-muted">{STOCK_POSITION_LINES_NOTE}</p>
        )}
      </section>

      {idea !== null && <StockIdea idea={idea} />}

      {(why !== null || cancels !== null) && (
        <div className="grid gap-4 md:grid-cols-2">
          {why !== null && <Narration id="why-heading" title="Why this setup" text={why} />}
          {cancels !== null && (
            <Narration id="cancels-heading" title="What cancels it" text={cancels} />
          )}
        </div>
      )}

      <EligibilityChecklist symbol={stock.symbol} items={stock.checklist} idea={idea} />
    </div>
  );
}

export default function StockPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = routeSymbol(params?.symbol);
  // An address with no ticker in it (/stocks/%20): there is nothing to ask the API for.
  const noSymbol = symbol === '';

  const { data, error, failureReason, isPending, isFetching, refetch } = useQuery<StockOut>({
    queryKey: ['stock', symbol],
    queryFn: () => swingApi.stock(symbol),
    enabled: !noSymbol,
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
    // The API still loading its market data after a start (a 503 that says so) is asked again
    // every 10 seconds until it answers; a request that never reached the API is worth one more
    // try. Any other 404 or 503 is the server's answer, and the Retry button asks again.
    retry: (failures, err) => isLoadingMarketData(err) || (failures < 1 && isUnreachable(err)),
    retryDelay: (failures, err) =>
      isLoadingMarketData(err) ? LOADING_RETRY_MS : Math.min(1000 * 2 ** failures, 30_000),
  });
  const loadingMarketData = isFetching && isLoadingMarketData(failureReason);

  const notFound = error instanceof ApiError && error.status === 404;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-mono text-2xl font-semibold">
            {noSymbol ? 'Stock' : (data?.symbol ?? symbol)}
          </span>
          {data !== undefined && data.name !== null && data.name !== '' && (
            <span className="min-w-0 break-words text-base text-ink-muted">{data.name}</span>
          )}
        </h1>
        {!noSymbol && data !== undefined && !notFound && (
          <p className="mt-1 text-sm text-ink-muted">{`As of ${sessionDate(data.session)} close`}</p>
        )}
      </header>

      {noSymbol ? (
        <p className="rounded-lg border border-line bg-bg-surface px-4 py-3 text-sm text-ink-muted">
          {STOCK_EMPTY_SYMBOL_TEXT}
        </p>
      ) : notFound ? (
        <NotFound symbol={symbol} />
      ) : (
        <>
          {error !== null && (
            <LoadFailure error={error} retrying={isFetching} onRetry={() => void refetch()} />
          )}
          {loadingMarketData && (
            <p role="status" className="text-sm text-ink-muted">
              {STOCK_MARKET_DATA_LOADING_TEXT}
            </p>
          )}
          {data !== undefined ? (
            <StockView stock={data} />
          ) : (
            isPending &&
            !loadingMarketData && <p className="text-sm text-ink-muted">{stockLoadingText(symbol)}</p>
          )}
        </>
      )}

      <p className="border-t border-line pt-4 text-xs text-ink-subtle">{DISCLAIMER_TEXT}</p>
    </div>
  );
}
