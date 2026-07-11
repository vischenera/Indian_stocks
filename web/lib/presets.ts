// Mirrors worker/config.py PRESETS (keys must match what the worker writes).
export const PRESETS: { key: string; name: string }[] = [
  { key: "all_caps_90", name: "All Caps (90d)" },
  { key: "breakout", name: "Breakout (fresh momentum)" },
  { key: "all_caps", name: "All Caps (30d)" },
  { key: "conservative_swing", name: "Conservative Swing (30d)" },
  { key: "aggressive_swing", name: "Aggressive Swing (10d)" },
  { key: "momentum", name: "Momentum (10d)" },
  { key: "small_cap", name: "Small Cap (30d)" },
  { key: "day_trading", name: "Day Trading (Intraday)" },
];

// Full filter thresholds — mirrors worker/config.py PRESETS. Prices are in ₹
// and market caps in INR (1 crore = 1e7, so 1e10 = ₹1,000 Cr). Used when a
// custom period-days is requested and results are computed live instead of
// read from the worker's precomputed scan_results table.
export type PresetDef = {
  periodDays: number; stopPercentage: number;
  minPrice: number; maxPrice: number; minVolume: number;
  minMcap: number; maxMcap: number; minMomentum: number; maxVolatility: number;
  isIntraday: boolean;
  // Breakout-style gates, only present on presets that enforce them.
  minSlopePctDay?: number; minTrendR2?: number; minUpDayRatio?: number;
  minVolExpansion?: number; maxBreakoutAge?: number;
};

export const PRESET_DEFS: Record<string, PresetDef> = {
  day_trading: {
    periodDays: 1, stopPercentage: 5,
    minPrice: 50, maxPrice: 20_000, minVolume: 500_000,
    minMcap: 1e10, maxMcap: 5e12, minMomentum: 0, maxVolatility: 999,
    isIntraday: true,
  },
  aggressive_swing: {
    periodDays: 10, stopPercentage: 10,
    minPrice: 20, maxPrice: 5_000, minVolume: 200_000,
    minMcap: 4e9, maxMcap: 4e11, minMomentum: 5, maxVolatility: 999,
    isIntraday: false,
  },
  conservative_swing: {
    periodDays: 30, stopPercentage: 15,
    minPrice: 50, maxPrice: 20_000, minVolume: 100_000,
    minMcap: 1e10, maxMcap: 1e12, minMomentum: 0, maxVolatility: 999,
    isIntraday: false,
  },
  momentum: {
    periodDays: 10, stopPercentage: 12,
    minPrice: 20, maxPrice: 5_000, minVolume: 300_000,
    minMcap: 4e9, maxMcap: 2.5e11, minMomentum: 8, maxVolatility: 999,
    isIntraday: false,
  },
  small_cap: {
    periodDays: 30, stopPercentage: 15,
    minPrice: 10, maxPrice: 1_000, minVolume: 100_000,
    minMcap: 1e9, maxMcap: 4e10, minMomentum: 0, maxVolatility: 999,
    isIntraday: false,
  },
  all_caps: {
    periodDays: 30, stopPercentage: 15,
    minPrice: 20, maxPrice: 1_000_000, minVolume: 100_000,
    minMcap: 0, maxMcap: Infinity, minMomentum: 0, maxVolatility: 999,
    isIntraday: false,
  },
  all_caps_90: {
    periodDays: 90, stopPercentage: 15,
    minPrice: 20, maxPrice: 1_000_000, minVolume: 100_000,
    minMcap: 0, maxMcap: Infinity, minMomentum: 0, maxVolatility: 999,
    isIntraday: false,
  },
  breakout: {
    periodDays: 10, stopPercentage: 10,
    minPrice: 20, maxPrice: 1_000_000, minVolume: 100_000,
    minMcap: 0, maxMcap: Infinity, minMomentum: 0, maxVolatility: 999,
    isIntraday: false,
    minSlopePctDay: 0.6, minTrendR2: 0.7, minUpDayRatio: 0.6,
    minVolExpansion: 1.5, maxBreakoutAge: 5,
  },
};
