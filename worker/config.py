"""Tunables, filter thresholds and scan presets — NSE edition.

All windows are in *trading days*. The rolling history window is 90 days.
Prices are in INR and market caps in INR (1 crore = 1e7), so e.g.
5e9 = ₹500 Cr and 1e12 = ₹1,00,000 Cr (1 lakh crore).
"""

# Rolling history window kept per symbol (trading days)
WINDOW_DAYS = 90

# Suffix appended to NSE symbols when querying Yahoo Finance.
YAHOO_SUFFIX = ".NS"

# How many calendar days the daily incremental fetch should look back.
# A small buffer covers weekends/holidays; duplicates are de-duped on upsert.
INCREMENTAL_LOOKBACK_DAYS = 7

# Backfill chunking: symbols pulled per scheduled run before a clean stop.
# The NSE main board is ~2,000 symbols, so a single run covers the whole
# universe; the resume cursor still kicks in if a run is throttled or cut
# short, picking up next schedule.
BACKFILL_CHUNK = 10000

# ---- Rate limiting (cloud / datacenter-IP friendly) ----
# Minimum seconds between requests across all worker threads.
MIN_REQUEST_INTERVAL = 0.20
# Threads. Kept modest because datacenter IPs get throttled faster than home IPs.
MAX_WORKERS = 8
# Per-request retry attempts on transient/auth errors before giving up.
REQUEST_RETRIES = 3
# Backoff base (seconds): wait = BACKOFF_BASE * 2**attempt (+ jitter).
BACKOFF_BASE = 2.0
# Substrings that indicate a Yahoo rate-limit / auth wall (treated as throttle).
RATE_LIMIT_MARKERS = (
    "rate limit", "429", "401", "unauthorized", "invalid crumb",
    "unable to access", "too many requests", "yahoo-finance-api-feedback",
)

# Default trailing-stop % used by the chart's auto-flipping trail.
DEFAULT_STOP_PCT = 10.0

# Scan presets — ported from the US scanner and recalibrated for NSE:
# prices in ₹, market caps in INR, volume floors tuned to NSE liquidity.
# Momentum thresholds are slightly looser than the US originals because
# NSE's 5/10/20% circuit bands cap single-day moves.
PRESETS = {
    "day_trading": {
        "name": "Day Trading (Intraday)",
        "period_days": 1, "stop_percentage": 5,
        "min_price": 50.0, "max_price": 20_000, "min_volume": 500_000,
        "min_mcap": 1e10, "max_mcap": 5e12,          # ₹1,000 Cr – ₹5,00,000 Cr
        "min_momentum": 0, "max_volatility": 999,
    },
    "aggressive_swing": {
        "name": "Aggressive Swing (10d)",
        "period_days": 10, "stop_percentage": 10,
        "min_price": 20.0, "max_price": 5_000, "min_volume": 200_000,
        "min_mcap": 4e9, "max_mcap": 4e11,           # ₹400 Cr – ₹40,000 Cr
        "min_momentum": 5, "max_volatility": 999,
    },
    "conservative_swing": {
        "name": "Conservative Swing (30d)",
        "period_days": 30, "stop_percentage": 15,
        "min_price": 50.0, "max_price": 20_000, "min_volume": 100_000,
        "min_mcap": 1e10, "max_mcap": 1e12,          # ₹1,000 Cr – ₹1,00,000 Cr
        "min_momentum": 0, "max_volatility": 999,
    },
    "momentum": {
        "name": "Momentum (10d)",
        "period_days": 10, "stop_percentage": 12,
        "min_price": 20.0, "max_price": 5_000, "min_volume": 300_000,
        "min_mcap": 4e9, "max_mcap": 2.5e11,         # ₹400 Cr – ₹25,000 Cr
        "min_momentum": 8, "max_volatility": 999,
    },
    "small_cap": {
        "name": "Small Cap (30d)",
        "period_days": 30, "stop_percentage": 15,
        "min_price": 10.0, "max_price": 1_000, "min_volume": 100_000,
        "min_mcap": 1e9, "max_mcap": 4e10,           # ₹100 Cr – ₹4,000 Cr
        "min_momentum": 0, "max_volatility": 999,
    },
    "all_caps": {
        "name": "All Caps (30d)",
        "period_days": 30, "stop_percentage": 15,
        "min_price": 20.0, "max_price": 1_000_000, "min_volume": 100_000,
        "min_mcap": 0, "max_mcap": float("inf"),
        "min_momentum": 0, "max_volatility": 999,
    },
    "all_caps_90": {
        "name": "All Caps (90d)",
        "period_days": 90, "stop_percentage": 15,
        "min_price": 20.0, "max_price": 1_000_000, "min_volume": 100_000,
        "min_mcap": 0, "max_mcap": float("inf"),
        "min_momentum": 0, "max_volatility": 999,
    },
    # Fresh, steep, CLEAN climbs out of a base with volume behind them.
    # min_slope_pct_day is loosened vs the US preset (0.8 → 0.6): circuit
    # bands make Indian breakouts shallower per-day but more persistent.
    "breakout": {
        "name": "Breakout (fresh momentum)",
        "period_days": 10, "stop_percentage": 10,
        "min_price": 20.0, "max_price": 1_000_000, "min_volume": 100_000,
        "min_mcap": 0, "max_mcap": float("inf"),
        "min_momentum": 0, "max_volatility": 999,
        "min_slope_pct_day": 0.6, "min_trend_r2": 0.7,
        "min_up_day_ratio": 0.6, "min_vol_expansion": 1.5,
        "max_breakout_age": 5,
    },
}
