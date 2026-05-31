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
import { useEffect, useRef } from 'react';
import { api } from '@/lib/api';
import type { BarsResponse, Signal } from '@/lib/types';

interface Props {
  symbol: string;
  resolution?: string;
  signal?: Signal | null;
}

export function PriceChart({ symbol, resolution = '1m', signal }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  const { data, isLoading } = useQuery<BarsResponse>({
    queryKey: ['bars', symbol, resolution],
    queryFn: () => api.bars(symbol, resolution, 300),
    refetchInterval: 30_000,
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
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wider text-ink-muted">
          {symbol} · {resolution}
        </h3>
        {isLoading && <span className="text-xs text-ink-subtle">loading…</span>}
        {data && data.bars.length === 0 && (
          <span className="text-xs text-warn">no bars — ingest first</span>
        )}
      </div>
      <div ref={containerRef} className="h-[360px] w-full" />
    </div>
  );
}
