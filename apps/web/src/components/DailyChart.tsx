'use client';

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type AutoscaleInfoProvider,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';
import { CHART_COLORS } from '@/lib/chart-colors';
import { CHART_NO_BARS_TEXT } from '@/lib/copy';
import {
  LINE_SPECS,
  allLineRuns,
  candleData,
  levelLines,
  volumeData,
  type LevelStyle,
  type LineKey,
  type LineSpec,
} from '@/lib/daily-chart-data';
import { price } from '@/lib/format';
import type { BarOut, LevelsOut, LinesOut } from '@/lib/swing-types';

/** The bars on screen when the chart opens (about eight months); the rest is a scroll away. */
const VISIBLE_BARS = 180;
/** Empty room to the right of the last bar, in bars. */
const RIGHT_PADDING = 5;

const PRICE_LINE_STYLE: Record<LevelStyle, LineStyle> = {
  solid: LineStyle.Solid,
  dashed: LineStyle.Dashed,
  dotted: LineStyle.Dotted,
};

// Each indicator line is one series per run of consecutive days (see `lineRuns`): a gap in the
// data is then a gap in the line. Most lines have a single run.
type LineSeriesMap = Record<LineKey, ISeriesApi<'Line'>[]>;
const noLines = (): LineSeriesMap => ({ sma50: [], sma200: [], ema20: [] });

const addLine = (chart: IChartApi, spec: LineSpec) =>
  chart.addSeries(LineSeries, {
    color: spec.color,
    lineWidth: spec.width,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

/**
 * The stock's daily candles with volume, the 50-day, 200-day and 20-day averages, and the trade
 * levels as price lines. Set up like the legacy `PriceChart`: one chart for the life of the
 * component, a resize observer through `autoSize`, and removal on unmount. The chart is as wide as
 * its box, so it never makes the page scroll sideways on a phone.
 */
export function DailyChart({
  bars,
  lines,
  levels,
}: {
  bars: BarOut[];
  lines: LinesOut;
  levels: LevelsOut | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const lineRefs = useRef<LineSeriesMap>(noLines());
  const priceLinesRef = useRef<IPriceLine[]>([]);
  // The prices of the levels, read by the candle series when it sets its price range, so that the
  // stop and the 2R line are inside the picture even when they sit outside the candles.
  const levelPricesRef = useRef<number[]>([]);
  // The data the chart was last framed for; a refetch of the same bars keeps the user's zoom.
  const framedRef = useRef<string | null>(null);

  const candles = useMemo(() => candleData(bars), [bars]);
  const volumes = useMemo(() => volumeData(bars), [bars]);
  const lineSets = useMemo(() => allLineRuns(lines, bars), [lines, bars]);
  const levelList = useMemo(() => levelLines(levels), [levels]);

  // The candle series asks this for its price range: the candles', stretched to hold the levels.
  const widenForLevels: AutoscaleInfoProvider = (base) => {
    const info = base();
    const prices = levelPricesRef.current;
    if (info === null || info.priceRange === null || prices.length === 0) return info;
    return {
      ...info,
      priceRange: {
        minValue: Math.min(info.priceRange.minValue, ...prices),
        maxValue: Math.max(info.priceRange.maxValue, ...prices),
      },
    };
  };

  // Create the chart once.
  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;
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
      rightPriceScale: {
        borderColor: CHART_COLORS.border,
        // Candles above the volume columns, which take the bottom fifth.
        scaleMargins: { top: 0.08, bottom: 0.22 },
      },
      timeScale: { borderColor: CHART_COLORS.border, rightOffset: RIGHT_PADDING },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });

    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      color: CHART_COLORS.borderStrong,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: CHART_COLORS.bull,
      downColor: CHART_COLORS.bear,
      borderUpColor: CHART_COLORS.bull,
      borderDownColor: CHART_COLORS.bear,
      wickUpColor: CHART_COLORS.bull,
      wickDownColor: CHART_COLORS.bear,
      autoscaleInfoProvider: widenForLevels,
    });

    chartRef.current = chart;
    candleRef.current = candle;
    volumeRef.current = volume;
    lineRefs.current = noLines();

    return () => {
      // `remove` disposes the series and their price lines with the chart.
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      lineRefs.current = noLines();
      priceLinesRef.current = [];
      framedRef.current = null;
    };
  }, []);

  // Push the bars and the lines.
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    const volume = volumeRef.current;
    if (chart === null || candle === null || volume === null) return;
    candle.setData(candles);
    volume.setData(volumes);
    for (const spec of LINE_SPECS) {
      const runs = lineSets[spec.key];
      const series = lineRefs.current[spec.key];
      while (series.length > runs.length) chart.removeSeries(series.pop() as ISeriesApi<'Line'>);
      while (series.length < runs.length) series.push(addLine(chart, spec));
      runs.forEach((run, index) => series[index].setData(run));
    }

    // Frame the newest bars when the data is new, not when the same bars arrive again.
    const framed = `${candles[0]?.time ?? ''}|${candles[candles.length - 1]?.time ?? ''}|${candles.length}`;
    if (framed !== framedRef.current) {
      framedRef.current = framed;
      const total = candles.length;
      if (total > VISIBLE_BARS) {
        chart.timeScale().setVisibleLogicalRange({
          from: total - VISIBLE_BARS,
          to: total - 1 + RIGHT_PADDING,
        });
      } else {
        chart.timeScale().fitContent();
      }
    }
  }, [candles, volumes, lineSets]);

  // Draw the trade levels.
  useEffect(() => {
    const candle = candleRef.current;
    if (candle === null) return;
    for (const line of priceLinesRef.current) candle.removePriceLine(line);
    levelPricesRef.current = levelList.map((l) => l.price);
    priceLinesRef.current = levelList.map((l) =>
      candle.createPriceLine({
        price: l.price,
        color: l.color,
        lineWidth: 1,
        lineStyle: PRICE_LINE_STYLE[l.style],
        axisLabelVisible: true,
        title: l.title,
      }),
    );
  }, [levelList]);

  const empty = candles.length === 0;
  const description = [
    'Daily price chart with 50-day, 200-day and 20-day averages',
    ...levelList.map((l) => `${l.title} ${price(l.price)}`),
  ].join('. ');

  return (
    <div className="min-w-0">
      <div className="relative">
        <div
          ref={containerRef}
          role="img"
          aria-label={description}
          className="h-[320px] w-full min-w-0 overflow-hidden sm:h-[420px]"
        />
        {empty && (
          <p className="absolute inset-0 flex items-center justify-center px-4 text-center text-sm text-ink-muted">
            {CHART_NO_BARS_TEXT}
          </p>
        )}
      </div>
      <ul aria-label="Lines on the chart" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
        {LINE_SPECS.map((spec) => (
          <li key={spec.key} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-0.5 w-4 rounded"
              style={{ backgroundColor: spec.color }}
            />
            {spec.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
