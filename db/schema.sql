-- Indian Stock Momentum Scanner (NSE) — Postgres schema
-- Applied automatically by worker/db.py on startup; kept here as canonical reference.

-- Universe metadata (one row per symbol)
CREATE TABLE IF NOT EXISTS tickers (
    symbol        TEXT PRIMARY KEY,
    company_name  TEXT,
    sector        TEXT,
    exchange      TEXT,
    market_cap    BIGINT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- Rolling daily OHLCV (kept to ~90 trading days per symbol)
CREATE TABLE IF NOT EXISTS daily_ohlcv (
    symbol  TEXT NOT NULL REFERENCES tickers(symbol) ON DELETE CASCADE,
    date    DATE NOT NULL,
    open    DOUBLE PRECISION,
    high    DOUBLE PRECISION,
    low     DOUBLE PRECISION,
    close   DOUBLE PRECISION,
    volume  BIGINT,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON daily_ohlcv (symbol, date DESC);

-- One row per scan/fetch run (drives the dashboard "Data Status" panel)
CREATE TABLE IF NOT EXISTS scan_runs (
    id                 BIGSERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    mode               TEXT,            -- 'backfill' | 'incremental'
    status             TEXT,            -- 'running' | 'success' | 'partial' | 'rate_limited' | 'error'
    symbols_total      INTEGER DEFAULT 0,
    symbols_processed  INTEGER DEFAULT 0,
    valid_count        INTEGER DEFAULT 0,
    error_count        INTEGER DEFAULT 0,
    rate_limited       BOOLEAN DEFAULT FALSE,
    message            TEXT
);

-- Pre-computed scan results, one row per (run, preset, symbol).
-- The dashboard reads the latest run for a given preset.
CREATE TABLE IF NOT EXISTS scan_results (
    run_id               BIGINT NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    preset               TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    company_name         TEXT,
    sector               TEXT,
    market_cap           BIGINT,
    current_price        DOUBLE PRECISION,
    period_gain_pct      DOUBLE PRECISION,
    momentum_score       DOUBLE PRECISION,
    volatility           DOUBLE PRECISION,
    highest_high         DOUBLE PRECISION,
    trailing_stop_level  DOUBLE PRECISION,
    distance_to_stop_pct DOUBLE PRECISION,
    stop_triggered       BOOLEAN,
    volume               BIGINT,
    avg_volume           BIGINT,
    slope_pct_day        DOUBLE PRECISION,
    trend_r2             DOUBLE PRECISION,
    up_day_ratio         DOUBLE PRECISION,
    vol_expansion        DOUBLE PRECISION,
    breakout_age         INTEGER,
    breakout_score       DOUBLE PRECISION,
    slope3_pct_day       DOUBLE PRECISION,
    PRIMARY KEY (run_id, preset, symbol)
);
CREATE INDEX IF NOT EXISTS idx_results_preset_run ON scan_results (preset, run_id DESC);
-- Idempotent upgrades for databases created before the breakout columns.
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS slope_pct_day  DOUBLE PRECISION;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS trend_r2       DOUBLE PRECISION;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS up_day_ratio   DOUBLE PRECISION;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS vol_expansion  DOUBLE PRECISION;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS breakout_age   INTEGER;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS breakout_score DOUBLE PRECISION;
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS slope3_pct_day DOUBLE PRECISION;

-- Tiny key/value store for the self-resuming backfill cursor & phase.
CREATE TABLE IF NOT EXISTS job_state (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ---- Paper-trading bot ------------------------------------------------
-- Single-row config edited from the dashboard; the worker replays the whole
-- simulation deterministically from stored OHLCV on every scan run.
CREATE TABLE IF NOT EXISTS bot_config (
    id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    capital       DOUBLE PRECISION NOT NULL DEFAULT 100000,
    slots         INTEGER NOT NULL DEFAULT 5,
    start_days    INTEGER NOT NULL DEFAULT 90,
    slippage_pct  DOUBLE PRECISION NOT NULL DEFAULT 0.25,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- One row per simulated trade; exit columns NULL while the position is open.
CREATE TABLE IF NOT EXISTS bot_trades (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    entry_date    DATE NOT NULL,
    entry_price   DOUBLE PRECISION NOT NULL,
    shares        DOUBLE PRECISION NOT NULL,
    cost          DOUBLE PRECISION NOT NULL,
    stop_level    DOUBLE PRECISION,           -- current trail (expected exit)
    exit_date     DATE,
    exit_price    DOUBLE PRECISION,
    exit_reason   TEXT,                       -- 'trail' | 'fading' | NULL (open)
    pnl           DOUBLE PRECISION
);

-- Daily equity curve (cash + marked-to-close open positions).
CREATE TABLE IF NOT EXISTS bot_equity (
    date    DATE PRIMARY KEY,
    equity  DOUBLE PRECISION NOT NULL,
    cash    DOUBLE PRECISION NOT NULL
);
