// Ported from worker/metrics.py — keep both in sync if the formula changes.

export type Bar = { date: string; open: number; high: number; low: number; close: number; volume: number };

export type Metrics = {
  current_price: number;
  period_gain_pct: number;
  momentum_score: number;
  volatility: number;
  highest_high: number;
  trailing_stop_level: number;
  distance_to_stop_pct: number;
  stop_triggered: boolean;
  volume: number;
  avg_volume: number;
};

export function calculateMetrics(
  bars: Bar[],
  periodDays: number,
  stopPercentage: number,
  isIntraday = false
): Metrics | null {
  if (!bars || bars.length < 5) return null;

  const period = bars.length > periodDays ? bars.slice(-periodDays) : bars;
  if (period.length < 2) return null;

  const closes = period.map((b) => b.close);
  const volumes = period.map((b) => b.volume);
  const highs = period.map((b) => b.high);
  const opens = period.map((b) => b.open);

  const currentPrice = closes[closes.length - 1];
  const currentVolume = volumes[volumes.length - 1];
  const avgVolume = volumes.reduce((a, b) => a + b, 0) / volumes.length;

  let startPrice: number;
  let highestHigh: number;
  if (isIntraday && periodDays === 1) {
    startPrice = opens[0];
    highestHigh = Math.max(Math.max(...highs), opens[0]);
  } else {
    startPrice = closes[0];
    highestHigh = Math.max(...highs);
  }
  if (!startPrice || startPrice <= 0) return null;

  const periodGainPct = ((currentPrice - startPrice) / startPrice) * 100;
  const trailingStopLevel = highestHigh * (1 - stopPercentage / 100);
  const distanceToStopPct = currentPrice ? ((currentPrice - trailingStopLevel) / currentPrice) * 100 : 0;
  const stopTriggered = currentPrice < trailingStopLevel;

  let momentumScore = 0;
  if (period.length >= 10) {
    const split = Math.max(3, Math.floor(period.length / 3));
    const recentAvg = closes.slice(-split).reduce((a, b) => a + b, 0) / split;
    const previousCloses = closes.slice(0, -split);
    const previousAvg = previousCloses.reduce((a, b) => a + b, 0) / Math.max(1, previousCloses.length);
    if (previousAvg > 0) {
      const momentumRatio = (recentAvg / previousAvg - 1) * 100;
      const recentVol = volumes.slice(-split).reduce((a, b) => a + b, 0) / split;
      const previousVolumes = volumes.slice(0, -split);
      const previousVol = previousVolumes.reduce((a, b) => a + b, 0) / Math.max(1, previousVolumes.length);
      const volumeRatio = previousVol > 0 ? recentVol / previousVol : 1;
      momentumScore = momentumRatio * (1 + (volumeRatio - 1) * 0.5);
    }
  }

  let volatility = 0;
  if (period.length >= 5) {
    const returns: number[] = [];
    for (let i = 1; i < closes.length; i++) {
      if (closes[i - 1]) returns.push(closes[i] / closes[i - 1] - 1);
    }
    if (returns.length >= 2) {
      const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
      const variance = returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (returns.length - 1);
      volatility = Math.sqrt(variance) * Math.sqrt(252) * 100;
    }
  }

  return {
    current_price: currentPrice,
    period_gain_pct: periodGainPct,
    momentum_score: momentumScore,
    volatility,
    highest_high: highestHigh,
    trailing_stop_level: trailingStopLevel,
    distance_to_stop_pct: distanceToStopPct,
    stop_triggered: stopTriggered,
    volume: currentVolume,
    avg_volume: avgVolume,
  };
}

// ---- Breakout detection — ported from worker/metrics.py breakout_metrics().
// Keep both in sync. Windows are fixed (not tied to the requested day range).

const SLOPE_WINDOW = 10;
const VOL_BASELINE = 30;
const BREAKOUT_LOOKBACK = 20;
const BREAKOUT_RECENT = 5;

export type BreakoutMetrics = {
  slope_pct_day: number;
  trend_r2: number;
  up_day_ratio: number;
  vol_expansion: number;
  breakout_age: number | null;
  breakout_score: number;
  slope3_pct_day: number;
};

export function breakoutMetrics(bars: Bar[]): BreakoutMetrics {
  const out: BreakoutMetrics = {
    slope_pct_day: 0, trend_r2: 0, up_day_ratio: 0,
    vol_expansion: 0, breakout_age: null, breakout_score: 0,
    slope3_pct_day: 0,
  };
  if (!bars || bars.length < SLOPE_WINDOW + 2) return out;
  const closes = bars.map((b) => b.close);
  const highs = bars.map((b) => b.high);
  const volumes = bars.map((b) => b.volume);
  const tail = closes.slice(-SLOPE_WINDOW);
  if (tail.some((c) => c === null || c === undefined || c <= 0)) return out;

  const n = tail.length;
  const meanX = (n - 1) / 2;
  const meanY = tail.reduce((a, b) => a + b, 0) / n;
  let sxx = 0, sxy = 0;
  tail.forEach((y, x) => {
    sxx += (x - meanX) ** 2;
    sxy += (x - meanX) * (y - meanY);
  });
  if (sxx > 0 && meanY > 0) {
    const slope = sxy / sxx;
    out.slope_pct_day = (slope / meanY) * 100;
    const ssTot = tail.reduce((a, y) => a + (y - meanY) ** 2, 0);
    if (ssTot > 0) {
      const ssRes = tail.reduce((a, y, x) => a + (y - (meanY + slope * (x - meanX))) ** 2, 0);
      out.trend_r2 = Math.max(0, 1 - ssRes / ssTot);
    }
  }

  let ups = 0;
  for (let i = closes.length - SLOPE_WINDOW; i < closes.length; i++) {
    if (closes[i] != null && closes[i - 1] && closes[i] > closes[i - 1]) ups++;
  }
  out.up_day_ratio = ups / SLOPE_WINDOW;

  const recentVol = volumes.slice(-SLOPE_WINDOW);
  const priorVol = volumes.slice(-(SLOPE_WINDOW + VOL_BASELINE), -SLOPE_WINDOW);
  const recentAvg = recentVol.reduce((a, v) => a + (v || 0), 0) / recentVol.length;
  if (priorVol.length) {
    const priorAvg = priorVol.reduce((a, v) => a + (v || 0), 0) / priorVol.length;
    if (priorAvg > 0) out.vol_expansion = recentAvg / priorAvg;
  }

  const last = bars.length - 1;
  for (let age = 0; age < BREAKOUT_RECENT; age++) {
    const idx = last - age;
    if (idx - BREAKOUT_LOOKBACK < 0) break;
    const window = highs.slice(idx - BREAKOUT_LOOKBACK, idx).filter((h) => h != null);
    if (!window.length) continue;
    const priorHigh = Math.max(...window);
    const c = closes[idx];
    if (c != null && c > priorHigh) {
      out.breakout_age = age;
      break;
    }
  }

  // Short-horizon slope (last 3 sessions, %/day): the "is the move still
  // alive" check. Entry is early (age 0-2); once age >= 5 a non-positive
  // 3-day slope marks the move as fading -> step out.
  const c1 = closes[closes.length - 1], c4 = closes[closes.length - 4];
  if (closes.length >= 4 && c4 && c1) {
    out.slope3_pct_day = ((c1 / c4) - 1) / 3 * 100;
  }

  // Ranking blends the 10d and 3d slopes (geometric mean) so the score
  // decays the same day momentum dies (slope3 <= 0 -> 0) and fresh
  // ignitions outrank stale runs, while a single small red day only
  // dents it. R^2 still scales for trend cleanliness.
  out.breakout_score = out.trend_r2 * Math.sqrt(
    Math.max(0, out.slope_pct_day) * Math.max(0, out.slope3_pct_day)
  );
  return out;
}
