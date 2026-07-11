// Auto-flipping trailing stop — ported from the original momentum_chart_viewer.
// LONG: trail starts at close*(1-stop%), only rises; flips SHORT when low <= trail.
// SHORT: trail starts at close*(1+stop%), only falls; flips LONG when high >= trail.

export type Bar = { date: string; open: number; high: number; low: number; close: number; volume: number };

export type TrailPoint = { date: string; trail: number; direction: "LONG" | "SHORT" };

export function autoFlippingTrail(bars: Bar[], stopPct = 10): {
  points: TrailPoint[];
  flips: number[];
  currentDirection: "LONG" | "SHORT";
} {
  const m = stopPct / 100;
  const points: TrailPoint[] = [];
  const flips: number[] = [];
  let dir: "LONG" | "SHORT" = "LONG";
  let trail: number | null = null;

  for (let i = 0; i < bars.length; i++) {
    const { close, high, low } = bars[i];
    if (trail === null) {
      trail = dir === "LONG" ? close * (1 - m) : close * (1 + m);
      points.push({ date: bars[i].date, trail, direction: dir });
      continue;
    }

    if (dir === "LONG") {
      if (low <= trail) {
        flips.push(i);
        dir = "SHORT";
        trail = high * (1 + m);
        points.push({ date: bars[i].date, trail, direction: dir });
        continue;
      }
      trail = Math.max(trail, close * (1 - m));
    } else {
      if (high >= trail) {
        flips.push(i);
        dir = "LONG";
        trail = low * (1 - m);
        points.push({ date: bars[i].date, trail, direction: dir });
        continue;
      }
      trail = Math.min(trail, close * (1 + m));
    }
    points.push({ date: bars[i].date, trail, direction: dir });
  }

  return { points, flips, currentDirection: dir };
}
