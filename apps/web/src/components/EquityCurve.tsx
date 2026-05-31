'use client';

import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts';
import { useEffect, useRef } from 'react';

interface Props {
  // [[epoch_seconds, equity], ...]
  curve: Array<[number, number]>;
  initialCapital: number;
}

export function EquityCurve({ curve, initialCapital }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#13171b' },
        textColor: '#8a96a3',
        fontFamily: 'ui-monospace, monospace',
      },
      grid: { vertLines: { color: '#1a1f25' }, horzLines: { color: '#1a1f25' } },
      rightPriceScale: { borderColor: '#262d35' },
      timeScale: { borderColor: '#262d35', timeVisible: true, secondsVisible: false },
      autoSize: true,
      height: 300,
    });
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#5eead4',
      topColor: '#5eead455',
      bottomColor: '#5eead400',
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    // Dedupe + sort by time — lightweight-charts requires strictly ascending,
    // unique timestamps. Synthetic minute bars are already unique, but guard.
    const seen = new Set<number>();
    const data = curve
      .filter(([t]) => {
        if (seen.has(t)) return false;
        seen.add(t);
        return true;
      })
      .map(([t, v]) => ({ time: t as never, value: v }));
    seriesRef.current.setData(data);

    // Baseline at initial capital so gains/losses read at a glance.
    seriesRef.current.createPriceLine({
      price: initialCapital,
      color: '#566374',
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: 'start',
    });
    chartRef.current?.timeScale().fitContent();
  }, [curve, initialCapital]);

  return <div ref={containerRef} className="h-[300px] w-full" />;
}
