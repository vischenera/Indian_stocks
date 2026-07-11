// Paper-trading bot: deterministic replay of the breakout strategy.
// Pure function — no I/O — so it is unit-testable and runs inside an API
// route on demand. Faithful port of the (removed) worker/bot.py engine;
// the synthetic-market test asserts identical numbers to the Python run.
//
// Discipline:
//   - signals computed at each historical EOD, no lookahead
//   - all action happens the next trading day
//   - entries / signal-exits fill at next day's (O+H+L+C)/4 +/- slippage
//   - trailing-stop exits fill AT the stop the day the low crosses it,
//     unless the day gaps open below the stop -> filled at the open
//   - budget per trade = realized pool / slots (compounding), fractional
//     shares, one position per symbol
//   - exits: 10% trail from highest high since entry, or the fading
//     step-out (>= 5 sessions old and 3-day slope <= 0), whichever first

import { breakoutMetrics, Bar } from "./metrics";
import { PRESET_DEFS } from "./presets";

export type BotConfig = {
  capital: number; slots: number; start_days: number; slippage_pct: number;
  // Strategy knobs — default to the breakout preset's values when omitted.
  stop_pct?: number; mcap_min?: number; mcap_max?: number; min_price?: number;
  min_slope?: number; min_r2?: number; min_up_ratio?: number; min_vol_x?: number;
  max_entry_age?: number; fade_age?: number;
};

export type SimTrade = {
  symbol: string;
  entry_date: string;
  entry_price: number;
  shares: number;
  cost: number;
  stop_level: number | null;
  exit_date: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  pnl: number | null;
};

export type SimResult = {
  trades: SimTrade[]; // closed first, then open (exit fields null)
  equity: { date: string; equity: number; cash: number }[];
};

const MIN_HISTORY = 42; // longest signal lookback: 10d slope + 30d vol baseline
const FADE_MIN_AGE = 5;
const BREAKOUT_LOOKBACK = 20;
const BREAKOUT_RECENT = 5;

const ohlc4 = (b: Bar): number | null => {
  const vals = [b.open, b.high, b.low, b.close].filter((v) => v);
  return vals.length ? vals.reduce((a, v) => a + v, 0) / vals.length : null;
};

const slope3 = (bars: Bar[]): number => {
  if (bars.length < 4) return 0;
  const c1 = bars[bars.length - 1]?.close, c4 = bars[bars.length - 4]?.close;
  return c1 && c4 ? ((c1 / c4) - 1) / 3 * 100 : 0;
};

type Position = {
  entry_date: string; entry_price: number; shares: number; cost: number;
  hh: number; age: number;
};

export function runBotSim(
  allBars: Map<string, Bar[]>,
  mcaps: Map<string, number>,
  cfg: BotConfig,
): SimResult {
  const gates = PRESET_DEFS["breakout"];
  const stopPct = (cfg.stop_pct ?? gates.stopPercentage) / 100;
  const slip = cfg.slippage_pct / 100;
  const slots = Math.max(1, cfg.slots);
  const fadeAge = cfg.fade_age ?? FADE_MIN_AGE;
  const mcapMin = cfg.mcap_min ?? 0;
  const mcapMax = cfg.mcap_max && cfg.mcap_max > 0 ? cfg.mcap_max : Infinity;
  const minPrice = cfg.min_price ?? gates.minPrice;
  const minSlope = cfg.min_slope ?? gates.minSlopePctDay!;
  const minR2 = cfg.min_r2 ?? gates.minTrendR2!;
  const minUpRatio = cfg.min_up_ratio ?? gates.minUpDayRatio!;
  const minVolX = cfg.min_vol_x ?? gates.minVolExpansion!;
  const maxEntryAge = cfg.max_entry_age ?? gates.maxBreakoutAge!;

  const symBars = new Map([...allBars].filter(([, b]) => b.length >= 5));
  const calSet = new Set<string>();
  for (const [, bars] of symBars) for (const b of bars) calSet.add(b.date);
  const calendar = [...calSet].sort();
  const symIdx = new Map<string, Map<string, number>>();
  for (const [s, bars] of symBars) symIdx.set(s, new Map(bars.map((b, i) => [b.date, i])));

  // Cheap pre-gate: did the close cross the prior 20d high within the last 5
  // sessions ending at index i? Skips the full metrics for ~95% of symbols.
  const freshOk = new Map<string, boolean[]>();
  for (const [s, bars] of symBars) {
    const n = bars.length;
    const crossed: boolean[] = new Array(n).fill(false);
    for (let j = BREAKOUT_LOOKBACK; j < n; j++) {
      let hi = -Infinity;
      for (let k = j - BREAKOUT_LOOKBACK; k < j; k++) {
        const h = bars[k].high;
        if (h != null && h > hi) hi = h;
      }
      const c = bars[j].close;
      crossed[j] = c != null && hi > 0 && c > hi;
    }
    const ok: boolean[] = new Array(n).fill(false);
    for (let i = 0; i < n; i++) {
      for (let a = 0; a < BREAKOUT_RECENT && a <= i; a++) {
        if (crossed[i - a]) { ok[i] = true; break; }
      }
    }
    freshOk.set(s, ok);
  }

  const simDays = calendar.slice(Math.max(0, calendar.length - Math.max(10, cfg.start_days)));

  let cash = cfg.capital;
  const positions = new Map<string, Position>();
  let pendingEntries: string[] = [];
  let pendingExits: { symbol: string; reason: string }[] = [];
  const trades: SimTrade[] = [];
  const equity: SimResult["equity"] = [];

  for (const day of simDays) {
    // 1. fill pending signal-exits at today's OHLC4
    for (const { symbol, reason } of pendingExits) {
      const pos = positions.get(symbol);
      if (!pos) continue;
      const idx = symIdx.get(symbol)?.get(day);
      if (idx === undefined) continue; // no bar today; signal re-fires later
      const px = ohlc4(symBars.get(symbol)![idx]);
      if (!px) continue;
      const fill = px * (1 - slip);
      cash += pos.shares * fill;
      trades.push({
        symbol, entry_date: pos.entry_date, entry_price: pos.entry_price,
        shares: pos.shares, cost: pos.cost, stop_level: null,
        exit_date: day, exit_price: fill, exit_reason: reason,
        pnl: pos.shares * (fill - pos.entry_price),
      });
      positions.delete(symbol);
    }
    pendingExits = [];

    // 2. fill pending entries at today's OHLC4
    for (const symbol of pendingEntries) {
      if (positions.has(symbol) || positions.size >= slots) continue;
      const idx = symIdx.get(symbol)?.get(day);
      if (idx === undefined) continue;
      const bar = symBars.get(symbol)![idx];
      const px = ohlc4(bar);
      if (!px) continue;
      let realizedPool = cash;
      for (const p of positions.values()) realizedPool += p.cost;
      const budget = Math.min(realizedPool / slots, cash);
      if (budget <= 0) continue;
      const fill = px * (1 + slip);
      cash -= budget;
      positions.set(symbol, {
        entry_date: day, entry_price: fill, shares: budget / fill,
        cost: budget, hh: bar.high || fill, age: 0,
      });
    }
    pendingEntries = [];

    // 3. intraday trailing stops
    for (const [symbol, pos] of [...positions]) {
      const idx = symIdx.get(symbol)?.get(day);
      if (idx === undefined) continue;
      const bar = symBars.get(symbol)![idx];
      if (pos.entry_date !== day) pos.age += 1;
      const stop = pos.hh * (1 - stopPct);
      if (bar.low != null && bar.low <= stop) {
        const fillBase = bar.open != null && bar.open < stop ? bar.open : stop;
        const fill = fillBase * (1 - slip);
        cash += pos.shares * fill;
        trades.push({
          symbol, entry_date: pos.entry_date, entry_price: pos.entry_price,
          shares: pos.shares, cost: pos.cost, stop_level: null,
          exit_date: day, exit_price: fill, exit_reason: "trail",
          pnl: pos.shares * (fill - pos.entry_price),
        });
        positions.delete(symbol);
        continue;
      }
      if (bar.high != null) pos.hh = Math.max(pos.hh, bar.high);
    }

    // 4. EOD mark + tomorrow's signals
    let mark = cash;
    for (const [symbol, pos] of positions) {
      const idx = symIdx.get(symbol)?.get(day);
      const close = idx !== undefined ? symBars.get(symbol)![idx].close : null;
      mark += pos.shares * (close || pos.entry_price);
    }
    equity.push({ date: day, equity: mark, cash });

    for (const [symbol, pos] of positions) {
      const idx = symIdx.get(symbol)?.get(day);
      if (idx === undefined || pos.age < fadeAge) continue;
      const bars = symBars.get(symbol)!;
      if (slope3(bars.slice(Math.max(0, idx - 3), idx + 1)) <= 0) {
        pendingExits.push({ symbol, reason: "fading" });
      }
    }

    const free = slots - positions.size + pendingExits.length;
    if (free > 0) {
      const candidates: [number, string][] = [];
      for (const [symbol, bars] of symBars) {
        if (positions.has(symbol)) continue;
        const idx = symIdx.get(symbol)?.get(day);
        if (idx === undefined || idx + 1 < MIN_HISTORY) continue;
        if (!freshOk.get(symbol)![idx]) continue;
        const window = bars.slice(Math.max(0, idx + 1 - 90), idx + 1);
        const lastClose = window[window.length - 1].close;
        if (!lastClose || lastClose < minPrice || lastClose > gates.maxPrice) continue;
        const avgVol = window.reduce((a, b) => a + (b.volume || 0), 0) / window.length;
        if (avgVol < gates.minVolume) continue;
        const mcap = mcaps.get(symbol) ?? 0;
        if (mcap < mcapMin || mcap > mcapMax) continue;
        const bm = breakoutMetrics(window);
        if (bm.slope_pct_day < minSlope) continue;
        if (bm.trend_r2 < minR2) continue;
        if (bm.up_day_ratio < minUpRatio) continue;
        if (bm.vol_expansion < minVolX) continue;
        if (bm.breakout_age === null || bm.breakout_age > maxEntryAge) continue;
        candidates.push([bm.breakout_score, symbol]);
      }
      candidates.sort((a, b) => b[0] - a[0]);
      pendingEntries = candidates.slice(0, free).map(([, s]) => s);
    }
  }

  // open positions appended with exit fields null and current stop as exit
  for (const [symbol, pos] of positions) {
    trades.push({
      symbol, entry_date: pos.entry_date, entry_price: pos.entry_price,
      shares: pos.shares, cost: pos.cost, stop_level: pos.hh * (1 - stopPct),
      exit_date: null, exit_price: null, exit_reason: null, pnl: null,
    });
  }

  return { trades, equity };
}
