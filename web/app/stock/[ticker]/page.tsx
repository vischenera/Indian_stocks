"use client";

import { useEffect, useMemo, useState } from "react";
import CandleChart from "@/components/CandleChart";
import { autoFlippingTrail, Bar } from "@/lib/trail";

export default function StockPage({ params }: { params: { ticker: string } }) {
  const symbol = params.ticker.toUpperCase();
  const [bars, setBars] = useState<Bar[]>([]);
  const [meta, setMeta] = useState<any>(null);
  // The input is free text while editing; the chart's stopPct only updates
  // (with clamping) when editing finishes — clamping on every keystroke
  // rewrote the field mid-edit and recomputed the chart per digit.
  const [stopPct, setStopPct] = useState(10);
  const [stopInput, setStopInput] = useState("10");
  const [loading, setLoading] = useState(true);

  // Restore the last-used stop % (shared across all symbols).
  useEffect(() => {
    try {
      const saved = Number(localStorage.getItem("chart-stop-pct"));
      if (Number.isFinite(saved) && saved >= 0.1 && saved <= 90) {
        setStopPct(saved);
        setStopInput(String(saved));
      }
    } catch {}
  }, []);

  const commitStop = () => {
    const n = Number(stopInput);
    const clamped = Number.isFinite(n) && n > 0 ? Math.min(90, Math.max(0.1, n)) : stopPct;
    setStopPct(clamped);
    setStopInput(String(clamped));
    try {
      localStorage.setItem("chart-stop-pct", String(clamped));
    } catch {}
  };

  useEffect(() => {
    setLoading(true);
    fetch(`/api/ohlcv/${symbol}?days=90`)
      .then((r) => r.json())
      .then((d) => {
        setBars(d.bars || []);
        setMeta(d.meta || null);
      })
      .finally(() => setLoading(false));
  }, [symbol]);

  // Expected exit = the current trailing-stop level for the active direction.
  const exit = useMemo(() => {
    if (!bars.length) return null;
    const { points, currentDirection } = autoFlippingTrail(bars, stopPct);
    const trail = points[points.length - 1]?.trail;
    const price = bars[bars.length - 1]?.close;
    if (!trail || !price) return null;
    const distPct = ((price - trail) / price) * 100;
    return { trail, price, distPct, direction: currentDirection };
  }, [bars, stopPct]);

  // Short-horizon momentum: 3-session slope (%/day). Positive = the move is
  // still alive; non-positive = fading, consider stepping out ahead of the
  // (deeper) trailing stop.
  const slope3 = useMemo(() => {
    if (bars.length < 4) return null;
    const c1 = bars[bars.length - 1]?.close;
    const c4 = bars[bars.length - 4]?.close;
    if (!c1 || !c4) return null;
    return ((c1 / c4) - 1) / 3 * 100;
  }, [bars]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <a href="/" className="text-sm text-sky-400 hover:underline">← Back</a>
          <div className="flex items-baseline gap-3">
            <h1 className="text-2xl font-semibold">{symbol}</h1>
            <a
              href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(`NSE:${symbol}`)}`}
              target="_blank" rel="noopener noreferrer"
              className="text-sm text-sky-400 hover:underline"
            >
              TradingView ↗
            </a>
          </div>
          {meta && <p className="text-slate-400">{meta.company_name} · {meta.sector}</p>}
          {exit && (
            <p className="mt-1 text-sm">
              <span className="text-slate-400">
                {exit.direction === "LONG" ? "Expected exit (LONG): " : "Stopped out — re-entry trigger: "}
              </span>
              <span className={exit.direction === "LONG" ? "text-emerald-400" : "text-orange-400"}>
                ₹{exit.trail.toFixed(2)}
              </span>
              <span className="ml-1 text-slate-500">
                ({exit.distPct >= 0 ? "−" : "+"}{Math.abs(exit.distPct).toFixed(1)}% {exit.direction === "LONG" ? "below" : "above"} price)
              </span>
            </p>
          )}
          {slope3 !== null && exit?.direction === "LONG" && (
            <p className="text-sm">
              <span className="text-slate-400">Momentum (3d): </span>
              {slope3 > 0 ? (
                <span className="text-emerald-400">sustaining · +{slope3.toFixed(1)}%/day</span>
              ) : (
                <span className="text-amber-400">fading · {slope3.toFixed(1)}%/day — consider stepping out</span>
              )}
            </p>
          )}
        </div>
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">Trailing stop %</span>
          <input type="number" min={0.1} max={90} step={0.1} value={stopInput}
            onChange={(e) => setStopInput(e.target.value)}
            onBlur={commitStop}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
            className="w-24 rounded border border-slate-700 bg-slate-900 px-3 py-1.5" />
        </label>
      </div>

      {loading ? (
        <div className="rounded-lg border border-slate-800 p-8 text-center text-slate-400">Loading chart…</div>
      ) : bars.length ? (
        <div className="rounded-lg border border-slate-800 p-2">
          <CandleChart bars={bars} stopPct={stopPct} />
          <p className="px-2 py-2 text-xs text-slate-500">
            Green line = LONG trailing stop, orange = SHORT. Arrows mark auto-flips.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-slate-800 p-8 text-center text-slate-400">
          No data for {symbol} yet.
        </div>
      )}
    </div>
  );
}
