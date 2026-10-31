"""Postgres access for the worker: connection, schema bootstrap, upserts, state."""

import json
import os

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


# ---- tickers (universe + metadata) ----

def all_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM tickers ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def count_tickers(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tickers")
        return cur.fetchone()[0]


def sync_universe(conn, rows):
    """rows: [(symbol, company_name)] from EQUITY_L. Inserts new symbols and
    refreshes names (EQUITY_L names are the canonical ones)."""
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO tickers (symbol, company_name, exchange, updated_at) VALUES %s
            ON CONFLICT (symbol) DO UPDATE SET
                company_name = EXCLUDED.company_name, updated_at = now()
            """,
            rows,
            template="(%s, %s, 'NSE', now())",
            page_size=1000,
        )
    conn.commit()


def prune_tickers(conn, keep):
    """Delete tickers no longer listed (their bars cascade). Returns count."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tickers WHERE NOT (symbol = ANY(%s))", (list(keep),))
        n = cur.rowcount
    conn.commit()
    return n


def update_sectors(conn, mapping):
    """mapping: {symbol: industry}. Returns rows updated."""
    return _update_column(conn, "sector", mapping, "text")


def update_market_caps(conn, mapping):
    """mapping: {symbol: market_cap_rupees}. Returns rows updated."""
    return _update_column(conn, "market_cap", mapping, "bigint")


def _update_column(conn, column, mapping, sqltype):
    if not mapping:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""
            UPDATE tickers t SET {column} = v.val::{sqltype}, updated_at = now()
            FROM (VALUES %s) AS v(symbol, val)
            WHERE t.symbol = v.symbol
            """,
            list(mapping.items()),
            page_size=len(mapping),  # one statement, so rowcount is the total
        )
        n = cur.rowcount
    conn.commit()
    return n


# ---- market_days (which trading dates are ingested / known closed) ----

def known_market_days(conn):
    """{date: 'ok' | 'holiday'}"""
    with conn.cursor() as cur:
        cur.execute("SELECT date, status FROM market_days")
        return dict(cur.fetchall())


def mark_holidays(conn, days):
    if not days:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO market_days (date, status) VALUES %s ON CONFLICT (date) DO NOTHING",
            [(d, "holiday") for d in days],
            template="(%s, %s)",
        )
    conn.commit()


def recent_ok_days(conn, n):
    """The `n` most recent ingested trading dates, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date FROM market_days WHERE status = 'ok' ORDER BY date DESC LIMIT %s", (n,))
        return [r[0] for r in cur.fetchall()]


# ---- daily_ohlcv ----

def ingest_day(conn, d, rows, source, add_missing_tickers=False,
               adjust_min_dev=0.005, adjust_max_share=0.05):
    """Store one trading day's bhavcopy atomically.

    rows: [(symbol, name, open, high, low, close, prev_close, volume)].
    Only symbols already in `tickers` are kept, unless `add_missing_tickers`
    (used when the universe list couldn't be fetched on a fresh database).

    Before inserting, detects splits/bonuses: NSE reports the ex-date's
    previous close already adjusted, so prev_close / our last stored close
    is the adjustment factor, applied to all of that symbol's older bars.

    Returns (rows_stored, [(symbol, ratio), ...], adjustments_skipped).
    """
    with conn.cursor() as cur:
        if add_missing_tickers:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO tickers (symbol, company_name, exchange) VALUES %s "
                "ON CONFLICT (symbol) DO NOTHING",
                [(r[0], r[1] or r[0]) for r in rows],
                template="(%s, %s, 'NSE')",
                page_size=1000,
            )
        cur.execute("SELECT symbol FROM tickers")
        universe = {r[0] for r in cur.fetchall()}
        rows = [r for r in rows if r[0] in universe]

        cur.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, close FROM daily_ohlcv
            WHERE date < %s AND symbol = ANY(%s)
            ORDER BY symbol, date DESC
            """,
            (d, [r[0] for r in rows]),
        )
        last_close = dict(cur.fetchall())
        adjustments = []
        for sym, _n, _o, _h, _l, _c, prev, _v in rows:
            lc = last_close.get(sym)
            if prev and lc and lc > 0 and abs(prev / lc - 1) > adjust_min_dev:
                adjustments.append((sym, prev / lc))

        skipped = bool(rows) and len(adjustments) > max(10, adjust_max_share * len(rows))
        if not skipped:
            for sym, ratio in adjustments:
                cur.execute(
                    """
                    UPDATE daily_ohlcv SET open = open * %(r)s, high = high * %(r)s,
                        low = low * %(r)s, close = close * %(r)s,
                        volume = round(volume / %(r)s)::bigint
                    WHERE symbol = %(s)s AND date < %(d)s
                    """,
                    {"r": ratio, "s": sym, "d": d},
                )

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO daily_ohlcv (symbol, date, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume
            """,
            [(s, d, o, h, l, c, v) for (s, _n, o, h, l, c, _p, v) in rows],
            page_size=1000,
        )
        cur.execute(
            """
            INSERT INTO market_days (date, status, source, symbols, fetched_at)
            VALUES (%s, 'ok', %s, %s, now())
            ON CONFLICT (date) DO UPDATE SET status = 'ok', source = EXCLUDED.source,
                symbols = EXCLUDED.symbols, fetched_at = now()
            """,
            (d, source, len(rows)),
        )
    conn.commit()
    return len(rows), adjustments, skipped


def trim_window(conn, keep_days):
    """Drop bars older than the `keep_days`-th most recent trading date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM daily_ohlcv WHERE date < (
                SELECT min(date) FROM (
                    SELECT date FROM market_days WHERE status = 'ok'
                    ORDER BY date DESC LIMIT %s
                ) w
            )
            """,
            (keep_days,),
        )
        n = cur.rowcount
    conn.commit()
    return n


def closes_on(conn, d):
    """{symbol: close} for one date."""
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, close FROM daily_ohlcv WHERE date = %s", (d,))
        return dict(cur.fetchall())


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


def finish_run(conn, run_id, status, processed, valid, errors, rate_limited,
               message="", symbols_total=None):
    """`rate_limited` = NSE blocked/throttled us partway (run resumes next time)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scan_runs SET finished_at = now(), status = %s,
                symbols_processed = %s, valid_count = %s, error_count = %s,
                rate_limited = %s, message = %s,
                symbols_total = COALESCE(%s, symbols_total)
            WHERE id = %s
            """,
            (status, processed, valid, errors, rate_limited, message, symbols_total, run_id),
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
