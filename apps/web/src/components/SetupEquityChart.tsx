'use client';

import {
  ColorType,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';
import { CHART_COLORS } from '@/lib/chart-colors';
import {
  EQUITY_NONE_TEXT,
  EQUITY_SPY_LABEL,
  EQUITY_STRATEGY_LABEL,
  equityCaptionText,
} from '@/lib/copy';
import { money, sessionDateYear } from '@/lib/format';
import { equityDescription, equityLine, equitySpan } from '@/lib/track-record';
import type { PointOut } from '@/lib/swing-types';

const STRATEGY_COLOR = CHART_COLORS.accent;
const SPY_COLOR = CHART_COLORS.inkMuted;

/**
 * A setup's simulated account value against SPY: two lines of weekly points, in dollars. The
 * report's series covers the out-of-sample window (2023 onward) only, which the legend and the
 * caption say. Set up like `DailyChart`: one chart for as long as there is data, sized by
 * `autoSize` (so it is as wide as its box and never makes a phone page scroll sideways), and
 * removed on unmount. The chart is a fixed picture (no pan or zoom), so a touch drag over it still
 * scrolls the page. With no series at all it says so instead of drawing an empty frame.
 */
export function SetupEquityChart({ strategy, spy }: { strategy: PointOut[]; spy: PointOut[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const strategyRef = useRef<ISeriesApi<'Line'> | null>(null);
  const spyRef = useRef<ISeriesApi<'Line'> | null>(null);
  // The data the chart was last framed for; a refetch of the same series keeps it as it is.
  const framedRef = useRef<string | null>(null);

  const strategyData = useMemo(() => equityLine(strategy), [strategy]);
  const spyData = useMemo(() => equityLine(spy), [spy]);
  const span = useMemo(() => equitySpan(strategyData, spyData), [strategyData, spyData]);

  const empty = span === null;

  // Create the chart when there is something to draw, and remove it when there is not.
  useEffect(() => {
    const container = containerRef.current;
    if (empty || container === null) return;
    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: CHART_COLORS.surface },
        textColor: CHART_COLORS.inkMuted,
        fontFamily: 'ui-monospace, monospace',
      },
      grid: {
        vertLines: { color: CHART_COLORS.grid },
        horzLines: { color: CHART_COLORS.grid },
      },
      rightPriceScale: { borderColor: CHART_COLORS.border },
      timeScale: { borderColor: CHART_COLORS.border },
      localization: { priceFormatter: (value: number) => money(value) },
      handleScroll: false,
      handleScale: false,
      autoSize: true,
    });
    // SPY first, so the strategy is drawn over it.
    spyRef.current = chart.addSeries(LineSeries, {
      color: SPY_COLOR,
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
    });
    strategyRef.current = chart.addSeries(LineSeries, {
      color: STRATEGY_COLOR,
      lineWidth: 2,
      priceLineVisible: false,
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      strategyRef.current = null;
      spyRef.current = null;
      framedRef.current = null;
    };
  }, [empty]);

  // Push the data, and show all of it when it is new.
  useEffect(() => {
    const chart = chartRef.current;
    const strategySeries = strategyRef.current;
    const spySeries = spyRef.current;
    if (chart === null || strategySeries === null || spySeries === null) return;
    strategySeries.setData(strategyData);
    spySeries.setData(spyData);
    const framed = `${span?.from ?? ''}|${span?.to ?? ''}|${strategyData.length}|${spyData.length}`;
    if (framed !== framedRef.current) {
      framedRef.current = framed;
      chart.timeScale().fitContent();
    }
  }, [strategyData, spyData, span]);

  const legend = [
    { show: strategyData.length > 0, label: EQUITY_STRATEGY_LABEL, color: STRATEGY_COLOR, dashed: false },
    { show: spyData.length > 0, label: EQUITY_SPY_LABEL, color: SPY_COLOR, dashed: true },
  ].filter((item) => item.show);

  if (span === null) {
    return <p className="px-2 py-6 text-center text-sm text-ink-muted">{EQUITY_NONE_TEXT}</p>;
  }
  return (
    <div className="min-w-0">
      <div
        ref={containerRef}
        role="img"
        aria-label={equityDescription(strategyData, spyData)}
        className="h-[240px] w-full min-w-0 overflow-hidden sm:h-[320px]"
      />
      <ul aria-label="Lines on the chart" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
        {legend.map((item) => (
          <li key={item.label} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block w-4 border-t-2"
              style={{ borderColor: item.color, borderStyle: item.dashed ? 'dashed' : 'solid' }}
            />
            {item.label}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-xs text-ink-muted">
        {equityCaptionText(sessionDateYear(span.from), sessionDateYear(span.to))}
      </p>
    </div>
  );
}
