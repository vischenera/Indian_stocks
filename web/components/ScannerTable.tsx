"use client";

export type Row = {
  symbol: string;
  company_name: string;
  sector: string;
  market_cap: number;
  current_price: number;
  period_gain_pct: number;
  momentum_score: number;
  volatility: number;
  trailing_stop_level: number;
  distance_to_stop_pct: number;
  stop_triggered: boolean;
  avg_volume: number;
  slope_pct_day?: number;
  trend_r2?: number;
  up_day_ratio?: number;
  vol_expansion?: number;
  breakout_age?: number | null;
  breakout_score?: number;
  slope3_pct_day?: number;
  spark?: number[];
  spark90?: number[];
};

// Inline price sparkline: closes over the selected period, colored by the
// app's up/down status tokens (polarity is also carried by the line's shape,
// so color is never the only encoding). No axes — it's a glance, not a chart.
function Spark({ data }: { data?: number[] }) {
  if (!data || data.length < 2) return <span className="text-slate-600">—</span>;
  const w = 76, h = 24, pad = 2;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((v, i) =>
    `${(pad + (i * (w - 2 * pad)) / (data.length - 1)).toFixed(1)},${(h - pad - ((v - min) / span) * (h - 2 * pad)).toFixed(1)}`
  );
  const color = data[data.length - 1] >= data[0] ? "#34d399" : "#f87171";
  const [lx, ly] = pts[pts.length - 1].split(",");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" className="block">
      <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth="1.5"
        strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lx} cy={ly} r="2" fill={color} />
    </svg>
  );
}

// DB columns can arrive as null/string/NaN depending on source (worker vs.
// live-compute) — never trust a raw field before calling .toFixed() on it.
const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};
const fmt = (v: unknown, digits: number) => num(v).toFixed(digits);

// Market cap in Indian units: crores below one lakh crore, "L Cr" above.
const fmtMcap = (v: unknown) => {
  const n = num(v);
  if (n >= 1e12) return `₹${(n / 1e12).toFixed(2)}L Cr`;
  if (n >= 1e7) return `₹${Math.round(n / 1e7).toLocaleString("en-IN")} Cr`;
  return n > 0 ? `₹${(n / 1e7).toFixed(2)} Cr` : "—";
};
const fmtVol = (v: unknown) => {
  const n = num(v);
  return n >= 1e7 ? `${(n / 1e7).toFixed(1)}Cr` : n >= 1e5 ? `${(n / 1e5).toFixed(1)}L` : `${(n / 1e3).toFixed(0)}K`;
};

export type SortKey =
  | "symbol" | "company_name" | "sector" | "market_cap" | "period_gain_pct"
  | "current_price" | "trailing_stop_level" | "avg_volume" | "momentum_score"
  | "slope_pct_day" | "breakout_score" | "breakout_age" | "up_day_ratio" | "vol_expansion";

const COLUMNS: { key: SortKey; label: string; align?: "right" | "center" }[] = [
  { key: "symbol", label: "Ticker" },
  { key: "company_name", label: "Company" },
  { key: "sector", label: "Sector" },
  { key: "market_cap", label: "MCap", align: "right" },
  { key: "period_gain_pct", label: "Gain%", align: "right" },
  { key: "current_price", label: "Price", align: "right" },
  { key: "trailing_stop_level", label: "Exit", align: "right" },
  { key: "breakout_score", label: "Score", align: "right" },
  { key: "breakout_age", label: "Age", align: "right" },
  { key: "slope_pct_day", label: "Slope", align: "right" },
  { key: "up_day_ratio", label: "Up%", align: "right" },
  { key: "vol_expansion", label: "Vol×", align: "right" },
  { key: "avg_volume", label: "Vol", align: "right" },
  { key: "momentum_score", label: "Mom", align: "right" },
];

// Sorting is fully server-driven: header clicks call onSort, the parent
// refetches with the new sort params, and the server orders the FULL result
// set before applying the row limit. (A previous client-side sort here only
// reordered the already-truncated top-N of a different column's ranking,
// which made "largest by MCap" depend on what happened to rank in the
// top-N by gain.)
export default function ScannerTable({
  rows, sortKey, sortDir, onSort,
}: {
  rows: Row[];
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (key: SortKey) => void;
}) {
  if (!rows.length) {
    return <div className="rounded-lg border border-slate-800 p-8 text-center text-slate-400">No matches yet.</div>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900 text-left text-slate-400">
          <tr>
            <th className="px-3 py-2">#</th>
            {COLUMNS.flatMap((c) => {
              const th = (
                <th key={c.key} className={`cursor-pointer select-none px-3 py-2 hover:text-slate-200 ${c.align === "right" ? "text-right" : ""}`}
                  onClick={() => onSort(c.key)}>
                  {c.label}{sortKey === c.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              );
              // Non-sortable sparkline columns, pinned right after Ticker:
              // selected-period trend, then the fixed 90d pattern view.
              return c.key === "symbol"
                ? [th,
                   <th key="trend" className="px-3 py-2">Trend</th>,
                   <th key="trend90" className="px-3 py-2">90d</th>]
                : [th];
            })}
            <th className="px-3 py-2 text-center">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.symbol} className="border-t border-slate-800 hover:bg-slate-900/60">
              <td className="px-3 py-2 text-slate-500">{i + 1}</td>
              <td className="px-3 py-2 font-medium">
                <a className="text-sky-400 hover:underline" href={`/stock/${r.symbol}`}>{r.symbol}</a>
              </td>
              <td className="px-3 py-2">
                <a href={`/stock/${r.symbol}`} aria-label={`${r.symbol} chart`}>
                  <Spark data={r.spark} />
                </a>
              </td>
              <td className="px-3 py-2">
                <a href={`/stock/${r.symbol}`} aria-label={`${r.symbol} 90-day chart`}>
                  <Spark data={r.spark90} />
                </a>
              </td>
              <td className="px-3 py-2 text-slate-300">
                <div className="max-w-[14rem] truncate" title={r.company_name || ""}>{r.company_name || ""}</div>
              </td>
              <td className="px-3 py-2 text-slate-400">
                <div className="max-w-[9rem] truncate" title={r.sector || ""}>{r.sector || ""}</div>
              </td>
              <td className="px-3 py-2 text-right">{fmtMcap(r.market_cap)}</td>
              <td className={`px-3 py-2 text-right ${num(r.period_gain_pct) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {fmt(r.period_gain_pct, 1)}%
              </td>
              <td className="px-3 py-2 text-right">₹{fmt(r.current_price, 2)}</td>
              <td className="px-3 py-2 text-right text-slate-400">
                ₹{fmt(r.trailing_stop_level, 2)}
                <span className="ml-1 text-xs text-slate-500">−{fmt(r.distance_to_stop_pct, 1)}%</span>
              </td>
              <td className="px-3 py-2 text-right font-medium text-sky-300">
                {r.breakout_score == null ? "—" : fmt(r.breakout_score, 2)}
              </td>
              <td className="px-3 py-2 text-right text-slate-400">
                {r.breakout_age == null ? "—" : `${r.breakout_age}d`}
              </td>
              <td className="px-3 py-2 text-right text-slate-400">
                {r.slope_pct_day == null ? "—" : `${fmt(r.slope_pct_day, 2)}%`}
              </td>
              <td className="px-3 py-2 text-right text-slate-400">
                {r.up_day_ratio == null ? "—" : `${fmt(num(r.up_day_ratio) * 100, 0)}%`}
              </td>
              <td className="px-3 py-2 text-right text-slate-400">
                {r.vol_expansion == null ? "—" : `${fmt(r.vol_expansion, 1)}×`}
              </td>
              <td className="px-3 py-2 text-right text-slate-400">{fmtVol(r.avg_volume)}</td>
              <td className="px-3 py-2 text-right">{fmt(r.momentum_score, 1)}</td>
              <td className="px-3 py-2 text-center">
                {r.stop_triggered ? (
                  <span className="text-rose-400">✕ Stop</span>
                ) : r.breakout_age != null && r.breakout_age >= 5 && num(r.slope3_pct_day) <= 0 ? (
                  // Move is ~a week old and short-term momentum has died:
                  // the "step out" signal, ahead of the deeper trailing stop.
                  <span className="text-amber-400">▼ Fading</span>
                ) : (
                  <span className="text-emerald-400">● Live</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
