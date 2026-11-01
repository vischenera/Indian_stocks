"""Tunables, filter thresholds and scan presets — NSE edition.

All windows are in *trading days*. The rolling history window is 90 days.
Prices are in INR and market caps in INR (1 crore = 1e7), so e.g.
5e9 = ₹500 Cr and 1e12 = ₹1,00,000 Cr (1 lakh crore).

Market data comes only from NSE's own end-of-day files (the daily "bhavcopy"
and companions) on the nsearchives host — one file per trading day carries
every listed stock, so a daily update is a single download.
"""

# Rolling history window kept per symbol (trading days)
WINDOW_DAYS = 90

# Calendar days the first backfill walks back. Must cover WINDOW_DAYS trading
# days plus weekends and exchange holidays (90 × 7/5 ≈ 126, + ~15 slack).
# Anything beyond WINDOW_DAYS trading days is trimmed after ingest.
BACKFILL_CALENDAR_DAYS = 145

# Only the regular-market series. BE/BZ are trade-for-trade (no intraday,
# usually illiquid), SM/ST are the SME board — none of them scanner material.
NSE_SERIES = ("EQ",)

# ---- NSE end-of-day files ---------------------------------------------------
# Each template is str.format()-ed with d=<datetime.date>. Tried in order; the
# first that returns a parseable file wins. On weekends only the first is
# tried (special Saturday/Sunday sessions still publish it).
BHAVCOPY_URLS = (
    # CM "UDiFF" common bhavcopy — the official format since 8 Jul 2024.
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
    # Full bhavcopy with delivery data — independent fallback file.
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv",
)
# Daily "PR" press bundle; contains MCAP<DDMMYYYY>.csv (issue size + market cap).
PR_ZIP_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip"
# Official list of listed equities (symbol, company name, series).
EQUITY_LIST_URLS = (
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
)
# Index constituent lists — the only NSE file that carries an industry per
# stock. Nifty Total Market (~750 stocks) first, Nifty 500 as a fallback;
# stocks outside them get no sector.
INDUSTRY_LIST_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
)

# ---- Politeness / resilience -------------------------------------------------
# Seconds between requests to NSE (a full backfill is ~150 requests).
REQUEST_SPACING_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
# Attempts per URL on 403/429/5xx/network errors before treating NSE as
# unavailable for this run. 404 is never retried: it means "not published".
REQUEST_RETRIES = 3
# Backoff base (seconds): wait = BACKOFF_BASE * 2**attempt (+ jitter).
BACKOFF_BASE = 3.0
# NSE never closes this many weekdays in a row. Seeing it with no file found
# means the URL format changed or files moved — fail loudly, don't record
# fake holidays.
MAX_CONSECUTIVE_MISSING_WEEKDAYS = 6

# Universe (EQUITY_L) and industry list refresh interval.
META_REFRESH_DAYS = 7

# ---- Split / bonus back-adjustment -------------------------------------------
# On an ex-date NSE sets the day's "previous close" to the *adjusted* close.
# If it differs from our stored close by more than this fraction, every older
# bar for that symbol is scaled by the ratio (volume inversely), giving a
# continuous series like an adjusted-price feed.
ADJUST_MIN_DEVIATION = 0.005
# Real corporate actions hit a handful of stocks a day. If more than this
# share of a day's symbols look "adjusted", it's a data gap (e.g. a missed
# session), not corporate actions — skip adjustments for that day.
ADJUST_MAX_SHARE = 0.05

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
