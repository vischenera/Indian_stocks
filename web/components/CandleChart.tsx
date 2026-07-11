"use client";

import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, TickMarkType } from "lightweight-charts";
import { autoFlippingTrail, Bar } from "@/lib/trail";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Short labels ("12 May") so the auto tick-spacer fits several marks even on
// a phone-width chart — the default formatter only labeled month boundaries,
// leaving the axis mostly blank.
function tickFormatter(time: any, tickMarkType: TickMarkType): string {
  const d = typeof time === "string"
    ? new Date(time)
    : new Date(time.year, time.month - 1, time.day);
  if (tickMarkType === TickMarkType.Year) return String(d.getFullYear());
  if (tickMarkType === TickMarkType.Month) return MONTHS[d.getMonth()];
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export default function CandleChart({ bars, stopPct = 10 }: { bars: Bar[]; stopPct?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || bars.length === 0) return;

    const chart: IChartApi = createChart(ref.current, {
      layout: { background: { type: ColorType.Solid, color: "#020617" }, textColor: "#cbd5e1" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      height: 460,
      timeScale: { borderColor: "#334155", tickMarkFormatter: tickFormatter },
      rightPriceScale: {
        borderColor: "#334155",
        // Reserve the bottom fifth of the pane for the volume histogram.
        scaleMargins: { top: 0.05, bottom: 0.25 },
      },
    });

    const candles = chart.addCandlestickSeries({
      upColor: "#10b981", downColor: "#ef4444",
      borderUpColor: "#10b981", borderDownColor: "#ef4444",
      wickUpColor: "#10b981", wickDownColor: "#ef4444",
    });
    candles.setData(bars.map((b) => ({ time: b.date, open: b.open, high: b.high, low: b.low, close: b.close })));

    // Volume histogram on its own hidden price scale, pinned to the bottom
    // fifth of the chart, tinted by up/down day.
    const volume = chart.addHistogramSeries({
      priceScaleId: "vol",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      visible: false,
    });
    volume.setData(bars.map((b) => ({
      time: b.date,
      value: b.volume,
      color: b.close >= b.open ? "rgba(16, 185, 129, 0.45)" : "rgba(239, 68, 68, 0.45)",
    })));

    // Auto-flipping trailing stop overlay: ONE continuous line whose color
    // switches at each flip via lightweight-charts' per-point `color`
    // (supported since v4.1). Two separate LONG/SHORT series proved fragile:
    // omitted dates draw straight connector lines across the gap, and dual
    // series can visually overlap. A single colored series can do neither.
    const { points, flips } = autoFlippingTrail(bars, stopPct);
    const trailLine = chart.addLineSeries({ lineWidth: 2, lastValueVisible: true });
    trailLine.setData(
      points.map((p) => ({
        time: p.date,
        value: p.trail,
        color: p.direction === "LONG" ? "#22c55e" : "#f97316",
      })) as any
    );

    if (flips.length) {
      candles.setMarkers(
        flips.map((i) => ({
          time: bars[i].date,
          position: points[i].direction === "LONG" ? "belowBar" : "aboveBar",
          color: points[i].direction === "LONG" ? "#22c55e" : "#f97316",
          shape: points[i].direction === "LONG" ? "arrowUp" : "arrowDown",
          text: points[i].direction,
        })) as any
      );
    }

    chart.timeScale().fitContent();
    const onResize = () => chart.applyOptions({ width: ref.current!.clientWidth });
    onResize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
    };
  }, [bars, stopPct]);

  return <div ref={ref} className="w-full" />;
}
