import { NextRequest, NextResponse } from "next/server";
import { getSql } from "@/lib/db";
import { PRESET_DEFS } from "@/lib/presets";
import { breakoutMetrics, calculateMetrics, Bar } from "@/lib/metrics";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

// Whitelist sortable columns — used for JS sort, never interpolated into SQL.
// Matches the clickable column headers in ScannerTable.
const SORTABLE = new Set([
  "symbol", "company_name", "sector", "market_cap", "period_gain_pct",
  "current_price", "trailing_stop_level", "avg_volume", "momentum_score", "volatility",
  "slope_pct_day", "breakout_score", "breakout_age", "up_day_ratio", "vol_expansion",
]);

// Scan data changes at most once per worker run (daily + manual triggers), so
// let the CDN serve repeat loads for 60s. Browsers still revalidate every time
// (max-age=0); only Vercel's edge holds it. Status stays fully uncached — this
// is deliberate and safe in a way the earlier stale-dashboard bug was not
// (that was an unbounded driver-level cache, not a 60s edge TTL).
const CACHE_SHORT = {
  headers: { "Cache-Control": "public, max-age=0, s-maxage=60, stale-while-revalidate=300" },
};

// Sort that treats a real 0 as 0, not "missing" — a plain `Number(v) ||
// -Infinity` sends legitimate zero values to the bottom. Strings sort
// lexicographically; missing/NaN always sink to the end.
function sortRows<T extends Record<string, any>>(rows: T[], key: string, dir: "asc" | "desc") {
  const mul = dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "string" && typeof bv === "string") return mul * av.localeCompare(bv);
    const an = Number(av), bn = Number(bv);
    const A = Number.isFinite(an) ? an : dir === "asc" ? Infinity : -Infinity;
    const B = Number.isFinite(bn) ? bn : dir === "asc" ? Infinity : -Infinity;
    return mul * (A - B);
  });
}

// Symbols keep at most this many stored days (mirrors worker/config.py WINDOW_DAYS).
const STORED_WINDOW_DAYS = 90;

// Recompute results live from stored OHLCV for a custom day-window, using the
// preset's other filter thresholds unchanged. Only hit when `days` differs
// from the preset's own default — otherwise the precomputed path below (fast,
// worker-side) is used.
//
// Mirrors worker/scan.py's evaluate_symbol: price/volume/mcap gates are
// checked against the *full* stored history (not the requested day-window),
// same as the worker does — only the returned metrics (gain%, momentum,
// trailing stop, etc.) are computed over the `days` window.
async function computeLive(sql: ReturnType<typeof getSql>, preset: string, days: number) {
  const def = PRESET_DEFS[preset];
  if (!def) return [];

  const [barRows, metaRows] = await Promise.all([
    sql`
      SELECT symbol, to_char(date, 'YYYY-MM-DD') AS date, open, high, low, close, volume
      FROM (
        SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
        FROM daily_ohlcv
        WHERE close IS NOT NULL AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
      ) t
      WHERE rn <= ${STORED_WINDOW_DAYS}
      ORDER BY symbol, date ASC
    `,
    sql`SELECT symbol, company_name, sector, market_cap FROM tickers`,
  ]);

  const bySymbol = new Map<string, Bar[]>();
  for (const r of barRows as any[]) {
    const bars = bySymbol.get(r.symbol) ?? [];
    bars.push({ date: r.date, open: r.open, high: r.high, low: r.low, close: r.close, volume: Number(r.volume) });
    bySymbol.set(r.symbol, bars);
  }

  const meta = new Map<string, { company_name: string; sector: string; market_cap: number }>();
  for (const r of metaRows as any[]) {
    meta.set(r.symbol, { company_name: r.company_name, sector: r.sector, market_cap: Number(r.market_cap) || 0 });
  }

  const rows: any[] = [];
  for (const [symbol, bars] of bySymbol) {
    const m = meta.get(symbol) ?? { company_name: symbol, sector: "N/A", market_cap: 0 };
    // Full-history average volume, same basis as the worker's own gate —
    // independent of the requested `days` window.
    const fullHistoryAvgVolume = bars.reduce((a, b) => a + b.volume, 0) / bars.length;
    const lastClose = bars[bars.length - 1].close;
    if (lastClose < def.minPrice || lastClose > def.maxPrice) continue;
    if (fullHistoryAvgVolume < def.minVolume) continue;
    if (m.market_cap < def.minMcap || m.market_cap > def.maxMcap) continue;

    // Breakout gates (mirrors worker/scan.py evaluate_symbol).
    const bm = breakoutMetrics(bars);
    if (def.minSlopePctDay !== undefined) {
      if (bm.slope_pct_day < def.minSlopePctDay) continue;
      if (bm.trend_r2 < (def.minTrendR2 ?? 0)) continue;
      if (bm.up_day_ratio < (def.minUpDayRatio ?? 0)) continue;
      if (bm.vol_expansion < (def.minVolExpansion ?? 0)) continue;
      if (bm.breakout_age === null || bm.breakout_age > (def.maxBreakoutAge ?? Infinity)) continue;
    }

    const metrics = calculateMetrics(bars, days, def.stopPercentage, def.isIntraday);
    if (!metrics) continue;
    if (metrics.momentum_score < def.minMomentum) continue;
    if (def.maxVolatility < 999 && metrics.volatility > def.maxVolatility) continue;
    rows.push({ symbol, company_name: m.company_name, sector: m.sector, market_cap: m.market_cap, ...metrics, ...bm });
  }
  return rows;
}

// Attach per-row sparkline arrays to the FINAL, already-limited row set:
// `spark90` is the full stored window (fixed scale — comparable across rows
// regardless of the selected period) and `spark` is the selected-period tail
// sliced from the same series. One query for <= `limit` symbols, shared by
// all three result paths.
async function attachSparks(sql: ReturnType<typeof getSql>, rows: any[], days: number) {
  if (!rows.length) return rows;
  const window = Math.min(STORED_WINDOW_DAYS, Math.max(5, days));
  const symbols = rows.map((r) => r.symbol);
  const sparkRows = await sql`
    SELECT symbol, close FROM (
      SELECT symbol, date, close,
             row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
      FROM daily_ohlcv
      WHERE symbol = ANY(${symbols}) AND close IS NOT NULL
    ) t
    WHERE rn <= ${STORED_WINDOW_DAYS}
    ORDER BY symbol, date ASC
  `;
  const bySymbol = new Map<string, number[]>();
  for (const r of sparkRows as any[]) {
    const arr = bySymbol.get(r.symbol) ?? [];
    arr.push(Number(r.close));
    bySymbol.set(r.symbol, arr);
  }
  for (const r of rows) {
    const full = bySymbol.get(r.symbol) ?? [];
    r.spark90 = full;
    r.spark = full.slice(-window);
  }
  return rows;
}

export async function GET(req: NextRequest) {
  try {
    const sql = getSql();
    const p = req.nextUrl.searchParams;
    const preset = p.get("preset") || "all_caps_90";
    const sortKey = SORTABLE.has(p.get("sort") || "") ? (p.get("sort") as string) : "period_gain_pct";
    const sortDir: "asc" | "desc" = p.get("dir") === "asc" ? "asc" : "desc";
    const limit = Math.min(1000, Math.max(1, Number(p.get("limit") || 300)));
    const onlyActive = p.get("onlyActive") === "1";
    const daysParam = Number(p.get("days"));
    const days = Number.isFinite(daysParam) && daysParam > 0 ? Math.min(90, Math.max(1, daysParam)) : null;
    // Optional market-cap band (rupees), layered on top of the preset's own
    // mcap gates. 0 / missing means unbounded on that side.
    const mcapMin = Math.max(0, Number(p.get("mcapMin")) || 0);
    const mcapMaxRaw = Number(p.get("mcapMax"));
    const mcapMax = Number.isFinite(mcapMaxRaw) && mcapMaxRaw > 0 ? mcapMaxRaw : Infinity;
    const inMcapBand = (r: any) => {
      const mc = Number(r.market_cap) || 0;
      return mc >= mcapMin && mc <= mcapMax;
    };

    const liveDays = days ?? PRESET_DEFS[preset]?.periodDays;
    const wantsLive = days !== null && days !== PRESET_DEFS[preset]?.periodDays;

    if (wantsLive) {
      let rows = await computeLive(sql, preset, liveDays!);
      if (onlyActive) rows = rows.filter((r) => !r.stop_triggered);
      rows = rows.filter(inMcapBand);
      sortRows(rows, sortKey, sortDir);
      const out = await attachSparks(sql, rows.slice(0, limit), liveDays!);
      return NextResponse.json({ runId: null, rows: out }, CACHE_SHORT);
    }

    // Latest run that actually has rows for this preset.
    const latest = await sql`
      SELECT max(run_id) AS run_id FROM scan_results WHERE preset = ${preset}
    `;
    const runId = latest[0]?.run_id;
    if (!runId) {
      // No precomputed data yet (e.g. a newly added preset the worker hasn't
      // scanned since deploy) — compute live instead of returning empty.
      let rows = liveDays ? await computeLive(sql, preset, liveDays) : [];
      if (onlyActive) rows = rows.filter((r) => !r.stop_triggered);
      rows = rows.filter(inMcapBand);
      sortRows(rows, sortKey, sortDir);
      const out = await attachSparks(sql, rows.slice(0, limit), liveDays ?? 30);
      return NextResponse.json({ runId: null, rows: out }, CACHE_SHORT);
    }

    // Parameterized fetch (two typed branches for the optional active filter).
    const rows = onlyActive
      ? await sql`
          SELECT symbol, company_name, sector, market_cap, current_price,
                 period_gain_pct, momentum_score, volatility, highest_high,
                 trailing_stop_level, distance_to_stop_pct, stop_triggered,
                 volume, avg_volume, slope_pct_day, trend_r2, up_day_ratio,
                 vol_expansion, breakout_age, breakout_score, slope3_pct_day
          FROM scan_results
          WHERE run_id = ${runId} AND preset = ${preset} AND stop_triggered = false`
      : await sql`
          SELECT symbol, company_name, sector, market_cap, current_price,
                 period_gain_pct, momentum_score, volatility, highest_high,
                 trailing_stop_level, distance_to_stop_pct, stop_triggered,
                 volume, avg_volume, slope_pct_day, trend_r2, up_day_ratio,
                 vol_expansion, breakout_age, breakout_score, slope3_pct_day
          FROM scan_results
          WHERE run_id = ${runId} AND preset = ${preset}`;

    // Neon returns BIGINT columns as strings, and any numeric column can be
    // null for a symbol with incomplete data — coerce everything to a real
    // finite number so the client never has to defend against NaN/null/string.
    const safeNum = (v: any) => {
      const n = Number(v);
      return Number.isFinite(n) ? n : 0;
    };
    const coerced = rows.map((r: any) => ({
      ...r,
      market_cap: safeNum(r.market_cap),
      volume: safeNum(r.volume),
      avg_volume: safeNum(r.avg_volume),
      current_price: safeNum(r.current_price),
      period_gain_pct: safeNum(r.period_gain_pct),
      momentum_score: safeNum(r.momentum_score),
      volatility: safeNum(r.volatility),
      highest_high: safeNum(r.highest_high),
      trailing_stop_level: safeNum(r.trailing_stop_level),
      distance_to_stop_pct: safeNum(r.distance_to_stop_pct),
      slope_pct_day: safeNum(r.slope_pct_day),
      trend_r2: safeNum(r.trend_r2),
      up_day_ratio: safeNum(r.up_day_ratio),
      vol_expansion: safeNum(r.vol_expansion),
      breakout_score: safeNum(r.breakout_score),
      slope3_pct_day: safeNum(r.slope3_pct_day),
      // 0 means "broke out today" — keep null (no recent breakout) distinct.
      breakout_age: r.breakout_age === null || r.breakout_age === undefined ? null : Number(r.breakout_age),
    }));

    // Band-filter, sort by the chosen column, and cap to limit.
    const banded = coerced.filter(inMcapBand);
    sortRows(banded, sortKey, sortDir);
    const out = await attachSparks(sql, banded.slice(0, limit), liveDays ?? 30);

    return NextResponse.json({ runId, rows: out }, CACHE_SHORT);
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
