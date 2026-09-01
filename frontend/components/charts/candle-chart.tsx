"use client";

import * as React from "react";
import { useTheme } from "next-themes";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type Time,
} from "lightweight-charts";
import type { CrossEvent, KlinesResponse, SeriesPoint } from "@/lib/api";

/**
 * Candlesticks with the same indicator values the thesis was written from.
 *
 * Two charts rather than one: lightweight-charts v4 has no multi-pane API, so
 * MACD gets its own instance underneath with the time scales kept in sync.
 *
 * Colours are read from the CSS custom properties at paint time instead of
 * being hardcoded. Canvas doesn't inherit CSS, so a theme switch has to be
 * pushed into the chart imperatively — without the `resolvedTheme` effect
 * below, flipping to light mode leaves a black chart on a white page.
 */

/**
 * Read a theme token as a colour lightweight-charts will accept.
 *
 * The tokens are stored as bare HSL components ("220 12% 62%") so Tailwind
 * can compose them with an alpha channel. lightweight-charts runs its own
 * colour parser that handles rgb()/hex only — it throws "Cannot parse color"
 * on `hsl()` in either the modern or the legacy comma syntax — so the value
 * is converted to rgb() here rather than passed through.
 */
function hslToRgb(h: number, s: number, l: number): string {
  const sat = s / 100;
  const lig = l / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = lig - c / 2;
  const [r, g, b] =
    h < 60
      ? [c, x, 0]
      : h < 120
        ? [x, c, 0]
        : h < 180
          ? [0, c, x]
          : h < 240
            ? [0, x, c]
            : h < 300
              ? [x, 0, c]
              : [c, 0, x];
  const to255 = (v: number) => Math.round((v + m) * 255);
  return `rgb(${to255(r)}, ${to255(g)}, ${to255(b)})`;
}

const cssVar = (name: string, fallback: string) => {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!v) return fallback;
  const m = v.match(/^([\d.]+)\s+([\d.]+)%\s+([\d.]+)%$/);
  if (!m) return v || fallback;
  return hslToRgb(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
};

/** Daily bars only, so the date half of the timestamp is the whole key. */
const toTime = (iso: string): Time => iso.slice(0, 10) as Time;

const toLine = (points: SeriesPoint[]) =>
  points
    .filter((p) => p.value !== null && p.value !== undefined)
    .map((p) => ({ time: toTime(p.time), value: p.value as number }));

export function CandleChart({ data }: { data: KlinesResponse }) {
  const priceRef = React.useRef<HTMLDivElement>(null);
  const macdRef = React.useRef<HTMLDivElement>(null);
  const chartsRef = React.useRef<{ price?: IChartApi; macd?: IChartApi }>({});
  const { resolvedTheme } = useTheme();
  const [showBands, setShowBands] = React.useState(true);
  const [showSma, setShowSma] = React.useState(true);

  React.useEffect(() => {
    if (!priceRef.current || !macdRef.current) return;

    const palette = {
      bg: cssVar("--background", "#0a0e17"),
      text: cssVar("--muted-foreground", "#8b93a7"),
      grid: cssVar("--border", "#232936"),
      bull: cssVar("--bull", "#26a65b"),
      bear: cssVar("--bear", "#e04a4a"),
      primary: cssVar("--primary", "#8b5cf6"),
      flat: cssVar("--flat", "#8b93a7"),
      held: cssVar("--held", "#a855f7"),
    };

    const common = {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: palette.text,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: palette.grid, style: LineStyle.Dotted },
        horzLines: { color: palette.grid, style: LineStyle.Dotted },
      },
      rightPriceScale: { borderColor: palette.grid },
      timeScale: { borderColor: palette.grid, rightOffset: 4 },
      crosshair: { mode: CrosshairMode.Normal },
      handleScale: { axisPressedMouseMove: { price: false } },
    };

    const priceChart = createChart(priceRef.current, {
      ...common,
      width: priceRef.current.clientWidth,
      height: 380,
    });
    const macdChart = createChart(macdRef.current, {
      ...common,
      width: macdRef.current.clientWidth,
      height: 130,
      timeScale: { ...common.timeScale, visible: true },
    });
    chartsRef.current = { price: priceChart, macd: macdChart };

    const candles = priceChart.addCandlestickSeries({
      upColor: palette.bull,
      downColor: palette.bear,
      borderUpColor: palette.bull,
      borderDownColor: palette.bear,
      wickUpColor: palette.bull,
      wickDownColor: palette.bear,
    });
    candles.setData(
      data.bars
        .filter((b) => b.close !== null)
        .map((b) => ({
          time: toTime(b.time),
          open: b.open as number,
          high: b.high as number,
          low: b.low as number,
          close: b.close as number,
        })),
    );

    if (showSma) {
      const fast = priceChart.addLineSeries({
        color: palette.primary,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      fast.setData(toLine(data.overlays.sma_fast));

      const slow = priceChart.addLineSeries({
        color: palette.held,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      slow.setData(toLine(data.overlays.sma_slow));
    }

    if (showBands) {
      const bandOpts = {
        color: palette.flat,
        lineWidth: 1 as const,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      };
      priceChart.addLineSeries(bandOpts).setData(
        toLine(data.overlays.bollinger.upper),
      );
      priceChart
        .addLineSeries({ ...bandOpts, lineStyle: LineStyle.Dotted })
        .setData(toLine(data.overlays.bollinger.mid));
      priceChart.addLineSeries(bandOpts).setData(
        toLine(data.overlays.bollinger.lower),
      );
    }

    // Cross events as chart markers — the point where the interpretation
    // becomes visible on the price itself rather than a number in a table.
    const markers = data.overlays.sma_cross_events.map((e: CrossEvent) => ({
      time: toTime(e.time),
      position: e.type === "golden" ? ("belowBar" as const) : ("aboveBar" as const),
      color: e.type === "golden" ? palette.bull : palette.bear,
      shape: e.type === "golden" ? ("arrowUp" as const) : ("arrowDown" as const),
      text: e.type === "golden" ? "Golden" : "Death",
    }));
    if (markers.length) candles.setMarkers(markers);

    const hist = macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    hist.setData(
      data.overlays.macd.hist
        .filter((p) => p.value !== null)
        .map((p) => ({
          time: toTime(p.time),
          value: p.value as number,
          color: (p.value as number) >= 0 ? palette.bull : palette.bear,
        })),
    );
    const macdLine = macdChart.addLineSeries({
      color: palette.primary,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    macdLine.setData(toLine(data.overlays.macd.macd));
    const signalLine = macdChart.addLineSeries({
      color: palette.flat,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    signalLine.setData(toLine(data.overlays.macd.signal));

    // Keep the two panes showing the same window.
    let syncing = false;
    const sync = (from: IChartApi, to: IChartApi) => () => {
      if (syncing) return;
      const range = from.timeScale().getVisibleLogicalRange();
      if (!range) return;
      syncing = true;
      to.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    const onPrice = sync(priceChart, macdChart);
    const onMacd = sync(macdChart, priceChart);
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(onPrice);
    macdChart.timeScale().subscribeVisibleLogicalRangeChange(onMacd);

    priceChart.timeScale().fitContent();
    macdChart.timeScale().fitContent();

    const observer = new ResizeObserver(() => {
      if (priceRef.current) priceChart.applyOptions({ width: priceRef.current.clientWidth });
      if (macdRef.current) macdChart.applyOptions({ width: macdRef.current.clientWidth });
    });
    observer.observe(priceRef.current);

    return () => {
      observer.disconnect();
      priceChart.timeScale().unsubscribeVisibleLogicalRangeChange(onPrice);
      macdChart.timeScale().unsubscribeVisibleLogicalRangeChange(onMacd);
      priceChart.remove();
      macdChart.remove();
      chartsRef.current = {};
    };
  }, [data, resolvedTheme, showBands, showSma]);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-[11px]">
        <button
          onClick={() => setShowSma((v) => !v)}
          className={`flex items-center gap-1.5 rounded-md border px-2 py-1 transition-colors ${
            showSma ? "border-primary/40 bg-accent text-accent-foreground" : "text-muted-foreground"
          }`}
        >
          <span className="h-0.5 w-3 rounded bg-primary" /> SMA 50/200
        </button>
        <button
          onClick={() => setShowBands((v) => !v)}
          className={`flex items-center gap-1.5 rounded-md border px-2 py-1 transition-colors ${
            showBands ? "border-primary/40 bg-accent text-accent-foreground" : "text-muted-foreground"
          }`}
        >
          <span className="h-0.5 w-3 rounded bg-flat" /> Bollinger
        </button>
        {data.overlays.sma_cross_events.length > 0 && (
          <span className="text-muted-foreground">
            {data.overlays.sma_cross_events.length} SMA cross
            {data.overlays.sma_cross_events.length === 1 ? "" : "es"} marked
          </span>
        )}
      </div>

      <div ref={priceRef} className="w-full overflow-hidden rounded-md" />
      <div className="px-1 text-[11px] font-medium text-muted-foreground">MACD (12, 26, 9)</div>
      <div ref={macdRef} className="w-full overflow-hidden rounded-md" />

      {data.warnings.length > 0 && (
        <ul className="space-y-0.5 rounded-md border border-delayed/40 bg-delayed-muted px-3 py-2 text-[11px] text-delayed">
          {data.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
