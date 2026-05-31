'use client';

import { useQuery } from '@tanstack/react-query';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
} from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { BarsResponse, Signal } from '@/lib/types';

interface Props {
  symbol: string;
  /** Initial timeframe. Falls back to `1D` (5-min candles × ~1 trading day). */
  initialTimeframe?: TimeframeId;
  signal?: Signal | null;
}

type TimeframeId = '1D' | '5D' | '1M' | '3M' | '6M' | '1Y';

interface TimeframeSpec {
  id: TimeframeId;
  label: string;
  resolution: string;
  limit: number;
  description: string; // shown in title tag — helps users understand the trade-off
}

// The (resolution, limit) pairs were picked so the visible candle count stays
// in the 80–500 range — dense enough to see structure, sparse enough that each
// candle is wider than a hairline. All paths aggregate from the 1m storage.
const TIMEFRAMES: readonly TimeframeSpec[] = [
  { id: '1D', label: '1D', resolution: '5m',  limit: 100, description: '5-min · last ~8h' },
  { id: '5D', label: '5D', resolution: '15m', limit: 130, description: '15-min · last ~5 sessions' },
  { id: '1M', label: '1M', resolution: '1h',  limit: 160, description: 'hourly · last ~1 month' },
  { id: '3M', label: '3M', resolution: '4h',  limit: 130, description: '4-hour · last ~3 months' },
  { id: '6M', label: '6M', resolution: '1d',  limit: 130, description: 'daily · last ~6 months' },
  { id: '1Y', label: '1Y', resolution: '1d',  limit: 260, description: 'daily · last ~1 year' },
];

const TF_BY_ID: Record<TimeframeId, TimeframeSpec> =
  Object.fromEntries(TIMEFRAMES.map((tf) => [tf.id, tf])) as Record<TimeframeId, TimeframeSpec>;

export function PriceChart({ symbol, initialTimeframe = '5D', signal }: Props) {
  const [tfId, setTfId] = useState<TimeframeId>(initialTimeframe);
  const tf = TF_BY_ID[tfId];

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  const { data, isLoading } = useQuery<BarsResponse>({
    queryKey: ['bars', symbol, tf.resolution, tf.limit],
    queryFn: () => api.bars(symbol, tf.resolution, tf.limit),
    // Refetch slower at longer timeframes — 1D should feel live, 1Y can be lazy.
    refetchInterval: tf.id === '1D' ? 30_000 : tf.id === '5D' ? 60_000 : 300_000,
  });

  // Create chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#13171b' },
        textColor: '#8a96a3',
        fontFamily: 'ui-monospace, monospace',
      },
      grid: {
        vertLines: { color: '#1a1f25' },
        horzLines: { color: '#1a1f25' },
      },
      rightPriceScale: { borderColor: '#262d35' },
      timeScale: { borderColor: '#262d35', timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      autoSize: true,
      height: 360,
    });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      color: '#3a434e',
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    chartRef.current = chart;
    candleRef.current = candle;
    volumeRef.current = volume;

    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  // Push bar data.
  useEffect(() => {
    if (!candleRef.current || !volumeRef.current || !data) return;
    const candles = data.bars.map((b) => ({
      time: b.time as never,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    const vols = data.bars.map((b) => ({
      time: b.time as never,
      value: b.volume,
      color: b.close >= b.open ? '#22c55e44' : '#ef444444',
    }));
    candleRef.current.setData(candles);
    volumeRef.current.setData(vols);
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  // Overlay entry / stop / target price lines from the signal.
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    // Clear previous lines.
    for (const pl of priceLinesRef.current) series.removePriceLine(pl);
    priceLinesRef.current = [];
    if (!signal) return;

    const add = (price: number | null, color: string, title: string) => {
      if (price === null || price === undefined) return;
      const pl = series.createPriceLine({
        price,
        color,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title,
      });
      priceLinesRef.current.push(pl);
    };

    add(signal.entry_price, '#5eead4', 'Entry');
    add(signal.stop_price, '#ef4444', 'Stop');
    signal.take_profit_levels.forEach((t, i) => add(t, '#22c55e', `T${i + 1}`));
  }, [signal]);

  return (
    <div className="rounded-lg border border-line bg-bg-surface p-3">
      <div className="mb-2 flex items-center justify-between gap-4">
        <h3 className="text-xs uppercase tracking-wider text-ink-muted">
          {symbol} · <span title={tf.description}>{tf.resolution}</span>
        </h3>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded border border-line text-xs font-mono">
            {TIMEFRAMES.map((opt) => (
              <button
                key={opt.id}
                type="button"
                onClick={() => setTfId(opt.id)}
                title={opt.description}
                className={
                  'px-2 py-1 transition-colors ' +
                  (opt.id === tfId
                    ? 'bg-ink-muted/20 text-ink-base'
                    : 'bg-bg-subtle text-ink-subtle hover:bg-bg-subtle/60 hover:text-ink-muted')
                }
              >
                {opt.label}
              </button>
            ))}
          </div>
          {isLoading && <span className="text-xs text-ink-subtle">loading…</span>}
          {data && data.bars.length === 0 && (
            <span className="text-xs text-warn">no bars — ingest first</span>
          )}
        </div>
      </div>
      <div ref={containerRef} className="h-[360px] w-full" />
    </div>
  );
}
