"use client";

import * as React from "react";
import { useTheme } from "next-themes";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type Time,
} from "lightweight-charts";
import type { DCFResult } from "@/lib/valuation";

/**
 * Annual undiscounted FCF vs. its present value, leading into the PV of
 * terminal value — a single-pane `addHistogramSeries()` reuse of
 * `candle-chart.tsx`'s pattern, much smaller in scope since there is no
 * real time axis or pane-sync need here (see that file for the
 * `hslToRgb`/`cssVar` token-to-canvas-colour conversion this copies:
 * lightweight-charts cannot parse this app's `hsl()` design tokens directly).
 *
 * There are no real dates to plot — DCF years are relative ("year 1",
 * "year 2"...) — so consecutive calendar years starting next year stand in
 * as the x-axis. They read naturally ("year 1" of a forecast made now IS
 * next year) and avoid a custom tick formatter for something this small.
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

export function CashflowChart({ result }: { result: DCFResult }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const { resolvedTheme } = useTheme();

  React.useEffect(() => {
    if (!ref.current || !result.ok || result.projectedFCF.length === 0) return;

    const palette = {
      text: cssVar("--muted-foreground", "#8b93a7"),
      grid: cssVar("--border", "#232936"),
      muted: cssVar("--muted-foreground", "#8b93a7"),
      primary: cssVar("--primary", "#8b5cf6"),
      held: cssVar("--held", "#a855f7"),
    };

    const chart = createChart(ref.current, {
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
      timeScale: { borderColor: palette.grid },
      crosshair: { mode: CrosshairMode.Normal },
      handleScale: { axisPressedMouseMove: { price: false } },
      width: ref.current.clientWidth,
      height: 220,
    });

    const baseYear = new Date().getFullYear();
    const toTime = (offsetYears: number): Time => `${baseYear + offsetYears}-01-01` as Time;

    // Drawn first, so the shorter present-value bar (added second, below)
    // paints over its base — the visible remainder above it IS the discount.
    const undiscounted = chart.addHistogramSeries({
      priceLineVisible: false, lastValueVisible: false, color: palette.muted,
    });
    undiscounted.setData(
      result.projectedFCF.map((p) => ({
        time: toTime(p.year), value: p.fcf, color: palette.muted,
      })),
    );

    const discounted = chart.addHistogramSeries({
      priceLineVisible: false, lastValueVisible: false, color: palette.primary,
    });
    discounted.setData([
      ...result.projectedFCF.map((p) => ({
        time: toTime(p.year), value: p.pv, color: palette.primary,
      })),
      {
        time: toTime(result.projectedFCF.length + 1),
        value: result.pvTerminalValue,
        color: palette.held,
      },
    ]);

    chart.timeScale().fitContent();

    const observer = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    });
    observer.observe(ref.current);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [result, resolvedTheme]);

  if (!result.ok || result.projectedFCF.length === 0) return null;

  return (
    <div className="space-y-1.5">
      <div ref={ref} className="w-full" />
      <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
        <Legend swatch="bg-muted-foreground/50" label="Undiscounted FCF" />
        <Legend swatch="bg-primary" label="Present value" />
        <Legend swatch="bg-held" label="PV of terminal value" />
      </div>
    </div>
  );
}

function Legend({ swatch, label }: { swatch: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-sm ${swatch}`} />
      {label}
    </span>
  );
}
