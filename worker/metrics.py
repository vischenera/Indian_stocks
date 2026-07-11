"""Momentum / trailing-stop / volatility metrics.

Ported from the original v3.0 `calculate_metrics`. Operates on an ascending list
of bar dicts: {date, open, high, low, close, volume}.
"""

import math


def calculate_metrics(bars, period_days, stop_percentage, is_intraday=False):
    """Return a metrics dict, or None if there isn't enough data."""
    if not bars or len(bars) < 5:
        return None

    period = bars[-period_days:] if len(bars) > period_days else bars
    if len(period) < 2:
        return None

    try:
        closes = [b["close"] for b in period]
        volumes = [b["volume"] for b in period]
        highs = [b["high"] for b in period]
        opens = [b["open"] for b in period]

        current_price = closes[-1]
        current_volume = volumes[-1]
        avg_volume = sum(volumes) / len(volumes)

        if is_intraday and period_days == 1:
            start_price = opens[0]
            highest_high = max(max(highs), opens[0])
        else:
            start_price = closes[0]
            highest_high = max(highs)

        if not start_price or start_price <= 0:
            return None

        period_gain_pct = ((current_price - start_price) / start_price) * 100

        trailing_stop_level = highest_high * (1 - stop_percentage / 100.0)
        distance_to_stop_pct = (
            ((current_price - trailing_stop_level) / current_price) * 100
            if current_price else 0
        )
        stop_triggered = current_price < trailing_stop_level

        # Momentum: recent vs previous average, scaled by volume expansion.
        momentum_score = 0.0
        if len(period) >= 10:
            split = max(3, len(period) // 3)
            recent_avg = sum(closes[-split:]) / split
            previous_avg = sum(closes[:-split]) / max(1, len(closes) - split)
            if previous_avg > 0:
                momentum_ratio = (recent_avg / previous_avg - 1) * 100
                recent_vol = sum(volumes[-split:]) / split
                previous_vol = sum(volumes[:-split]) / max(1, len(volumes) - split)
                volume_ratio = (recent_vol / previous_vol) if previous_vol > 0 else 1
                momentum_score = momentum_ratio * (1 + (volume_ratio - 1) * 0.5)

        # Annualised volatility from daily returns.
        volatility = 0.0
        if len(period) >= 5:
            returns = [
                (closes[i] / closes[i - 1] - 1)
                for i in range(1, len(closes)) if closes[i - 1]
            ]
            if len(returns) >= 2:
                mean = sum(returns) / len(returns)
                var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
                volatility = math.sqrt(var) * math.sqrt(252) * 100

        return {
            "current_price": float(current_price),
            "period_gain_pct": float(period_gain_pct),
            "momentum_score": float(momentum_score),
            "volatility": float(volatility),
            "highest_high": float(highest_high),
            "trailing_stop_level": float(trailing_stop_level),
            "distance_to_stop_pct": float(distance_to_stop_pct),
            "stop_triggered": bool(stop_triggered),
            "volume": int(current_volume),
            "avg_volume": int(avg_volume),
        }
    except (KeyError, TypeError, ZeroDivisionError):
        return None


# ---- Breakout detection ------------------------------------------------
# Quantifies the "steep, straight, volume-backed fresh breakout" setup:
#   slope_pct_day  linear-regression slope of the last SLOPE_WINDOW closes,
#                  as % of the window's mean close per day (steepness)
#   trend_r2       R^2 of that regression (straightness — kills choppy spikes)
#   up_day_ratio   fraction of up-days in the window (persistence)
#   vol_expansion  avg volume of the window vs the prior VOL_BASELINE days
#   breakout_age   sessions since close last crossed above the max high of
#                  the preceding BREAKOUT_LOOKBACK days (None = no cross
#                  within BREAKOUT_RECENT sessions)
#   breakout_score slope * r2 — ranking key: steepest CLEAN trend first
# Mirrored in web/lib/metrics.ts — keep both in sync.

SLOPE_WINDOW = 10
VOL_BASELINE = 30
BREAKOUT_LOOKBACK = 20
BREAKOUT_RECENT = 5


def breakout_metrics(bars):
    out = {
        "slope_pct_day": 0.0, "trend_r2": 0.0, "up_day_ratio": 0.0,
        "vol_expansion": 0.0, "breakout_age": None, "breakout_score": 0.0,
        "slope3_pct_day": 0.0,
    }
    if not bars or len(bars) < SLOPE_WINDOW + 2:
        return out
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b["volume"] for b in bars]
    if any(c is None or c <= 0 for c in closes[-SLOPE_WINDOW:]):
        return out

    # Regression on the last SLOPE_WINDOW closes.
    ys = closes[-SLOPE_WINDOW:]
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx > 0 and mean_y > 0:
        slope = sxy / sxx
        out["slope_pct_day"] = (slope / mean_y) * 100
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        if ss_tot > 0:
            ss_res = sum((y - (mean_y + slope * (x - mean_x))) ** 2 for x, y in zip(xs, ys))
            out["trend_r2"] = max(0.0, 1 - ss_res / ss_tot)

    ups = sum(1 for i in range(len(closes) - SLOPE_WINDOW, len(closes))
              if closes[i] is not None and closes[i - 1] and closes[i] > closes[i - 1])
    out["up_day_ratio"] = ups / SLOPE_WINDOW

    recent_vol = volumes[-SLOPE_WINDOW:]
    prior_vol = volumes[-(SLOPE_WINDOW + VOL_BASELINE):-SLOPE_WINDOW]
    recent_avg = sum(v or 0 for v in recent_vol) / len(recent_vol)
    if prior_vol:
        prior_avg = sum(v or 0 for v in prior_vol) / len(prior_vol)
        if prior_avg > 0:
            out["vol_expansion"] = recent_avg / prior_avg

    # Most recent session (within BREAKOUT_RECENT) whose close cleared the
    # max high of the preceding BREAKOUT_LOOKBACK sessions.
    last = len(bars) - 1
    for age in range(BREAKOUT_RECENT):
        idx = last - age
        if idx - BREAKOUT_LOOKBACK < 0:
            break
        prior_high = max((h for h in highs[idx - BREAKOUT_LOOKBACK:idx] if h is not None), default=None)
        c = closes[idx]
        if prior_high is not None and c is not None and c > prior_high:
            out["breakout_age"] = age
            break

    # Short-horizon slope (last 3 sessions, %/day): the "is the move still
    # alive" check. Entry is early (age 0-2); once age >= 5 a non-positive
    # 3-day slope marks the move as fading -> step out.
    if len(closes) >= 4 and closes[-4] and closes[-1]:
        out["slope3_pct_day"] = ((closes[-1] / closes[-4]) - 1) / 3 * 100

    # Ranking blends the 10d and 3d slopes (geometric mean) so the score
    # decays the same day momentum dies (slope3 <= 0 -> 0) and fresh
    # ignitions outrank stale runs, while a single small red day only
    # dents it. R^2 still scales for trend cleanliness.
    out["breakout_score"] = out["trend_r2"] * math.sqrt(
        max(0.0, out["slope_pct_day"]) * max(0.0, out["slope3_pct_day"])
    )
    return out
