"""Postgres access for the worker: connection, schema bootstrap, upserts, state."""

import json
import os
from datetime import date

import psycopg2
import psycopg2.extras

# Resolve the schema file relative to this package so it works from any CWD.
_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "schema.sql")


def get_conn():
    """Open a new Postgres connection from DATABASE_URL."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def init_schema(conn):
    """Apply db/schema.sql (idempotent)."""
    with open(_SCHEMA_PATH, "r") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# ---- job_state (backfill cursor / phase) ----

def get_state(conn, key, default=None):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM job_state WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else default


def set_state(conn, key, value):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, json.dumps(value)),
        )
    conn.commit()


# ---- tickers ----

def upsert_tickers(conn, rows):
    """rows: iterable of (symbol, company_name, sector, exchange, market_cap)."""
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO tickers (symbol, company_name, sector, exchange, market_cap, updated_at)
            VALUES %s
            ON CONFLICT (symbol) DO UPDATE SET
                company_name = COALESCE(EXCLUDED.company_name, tickers.company_name),
                sector       = COALESCE(EXCLUDED.sector, tickers.sector),
                exchange     = COALESCE(EXCLUDED.exchange, tickers.exchange),
                market_cap   = COALESCE(EXCLUDED.market_cap, tickers.market_cap),
                updated_at   = now()
            """,
            [(s, n, sec, ex, mc) for (s, n, sec, ex, mc) in rows],
            template="(%s, %s, %s, %s, %s, now())",
        )
    conn.commit()


def all_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM tickers ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


# ---- daily_ohlcv ----

def upsert_ohlcv(conn, symbol, bars):
    """bars: iterable of (date, open, high, low, close, volume). Upserts then trims."""
    if not bars:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO daily_ohlcv (symbol, date, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume
            """,
            [(symbol, d, o, h, l, c, v) for (d, o, h, l, c, v) in bars],
        )
    conn.commit()


def trim_ohlcv(conn, symbol, keep_days):
    """Keep only the most recent `keep_days` rows for a symbol."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM daily_ohlcv
            WHERE symbol = %s AND date NOT IN (
                SELECT date FROM daily_ohlcv WHERE symbol = %s
                ORDER BY date DESC LIMIT %s
            )
            """,
            (symbol, symbol, keep_days),
        )
    conn.commit()


def load_ohlcv(conn, symbol):
    """Return ascending list of dict bars for a symbol."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM daily_ohlcv WHERE symbol = %s ORDER BY date ASC
            """,
            (symbol,),
        )
        return cur.fetchall()


def load_all_ohlcv(conn):
    """Return {symbol: [ascending bar dicts]} for the whole table in one query."""
    out = {}
    with conn.cursor(name="ohlcv_stream") as cur:  # server-side cursor for memory
        cur.itersize = 50000
        cur.execute(
            "SELECT symbol, date, open, high, low, close, volume "
            "FROM daily_ohlcv ORDER BY symbol, date ASC"
        )
        for symbol, d, o, h, l, c, v in cur:
            out.setdefault(symbol, []).append(
                {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}
            )
    return out


def latest_ohlcv_date(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM daily_ohlcv WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


# ---- scan_runs ----

def start_run(conn, mode, symbols_total):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scan_runs (mode, status, symbols_total) VALUES (%s, 'running', %s) RETURNING id",
            (mode, symbols_total),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run(conn, run_id, status, processed, valid, errors, rate_limited, message=""):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scan_runs SET finished_at = now(), status = %s,
                symbols_processed = %s, valid_count = %s, error_count = %s,
                rate_limited = %s, message = %s
            WHERE id = %s
            """,
            (status, processed, valid, errors, rate_limited, message, run_id),
        )
    conn.commit()


# ---- scan_results ----

def insert_results(conn, run_id, preset, results):
    """results: iterable of dicts produced by metrics.calculate_metrics + meta."""
    if not results:
        return
    cols = [
        "current_price", "period_gain_pct", "momentum_score", "volatility",
        "highest_high", "trailing_stop_level", "distance_to_stop_pct",
        "stop_triggered", "volume", "avg_volume",
        "slope_pct_day", "trend_r2", "up_day_ratio", "vol_expansion",
        "breakout_age", "breakout_score", "slope3_pct_day",
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO scan_results (
                run_id, preset, symbol, company_name, sector, market_cap,
                current_price, period_gain_pct, momentum_score, volatility,
                highest_high, trailing_stop_level, distance_to_stop_pct,
                stop_triggered, volume, avg_volume,
                slope_pct_day, trend_r2, up_day_ratio, vol_expansion,
                breakout_age, breakout_score, slope3_pct_day
            ) VALUES %s
            ON CONFLICT (run_id, preset, symbol) DO NOTHING
            """,
            [
                (run_id, preset, r["symbol"], r.get("company_name"), r.get("sector"),
                 r.get("market_cap"), *[r[c] for c in cols])
                for r in results
            ],
        )
    conn.commit()


def prune_old_runs(conn, keep=60):
    """Keep only the most recent `keep` runs to bound storage."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM scan_runs WHERE id NOT IN (
                SELECT id FROM scan_runs ORDER BY id DESC LIMIT %s
            )
            """,
            (keep,),
        )
    conn.commit()
