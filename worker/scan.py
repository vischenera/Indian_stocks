"""Self-resuming scan orchestrator.

State machine driven by `job_state`:
  phase = 'backfill'    → pull 90-day history for the universe in chunks,
                          saving a cursor so runs resume where they left off.
  phase = 'incremental' → fetch the last few days for every symbol.

Either way, after updating data it recomputes metrics for every preset and
writes a fresh scan_results set plus a scan_runs status row.
"""

import time
import traceback

import config
import db
from datasource import RateLimited, get_source
from metrics import breakout_metrics, calculate_metrics
from universe import get_all_nse_tickers

PHASE_KEY = "phase"
CURSOR_KEY = "backfill_cursor"
PROGRESS_SAVE_EVERY = 10


def ensure_universe(conn):
    """Populate the tickers table if empty (metadata filled during backfill)."""
    symbols = db.all_symbols(conn)
    if symbols:
        return symbols
    print("Universe empty — downloading NSE ticker list...")
    tickers = get_all_nse_tickers()
    db.upsert_tickers(conn, [(s, name, None, "NSE", None) for s, name in tickers])
    print(f"Loaded {len(tickers)} symbols.")
    return db.all_symbols(conn)


def _store_symbol(conn, symbol, bars, info=None):
    """Upsert OHLCV (and optional ticker meta), then trim to the window."""
    if not bars:
        return False
    db.upsert_ohlcv(conn, symbol, bars)
    db.trim_ohlcv(conn, symbol, config.WINDOW_DAYS)
    if info is not None:
        name, sector, mcap = info
        db.upsert_tickers(conn, [(symbol, name, sector, None, mcap)])
    return True


def backfill_step(conn, src, symbols):
    """Process the next chunk. Returns (rate_limited, processed, errors, done)."""
    cursor = int(db.get_state(conn, CURSOR_KEY, 0))
    chunk = symbols[cursor:cursor + config.BACKFILL_CHUNK]
    print(f"Backfill: cursor={cursor}/{len(symbols)}, chunk={len(chunk)}")

    processed = errors = valid = 0
    rate_limited = False

    for i, symbol in enumerate(chunk):
        try:
            bars = src.fetch_history(symbol, config.WINDOW_DAYS)
            info = src.fetch_info(symbol) if bars else None
            if _store_symbol(conn, symbol, bars, info):
                valid += 1
            processed += 1
        except RateLimited as exc:
            print(f"Rate limited at {symbol}: {exc}. Stopping; will resume.")
            rate_limited = True
            break
        except Exception:  # noqa: BLE001
            errors += 1
            traceback.print_exc()
        # Advance cursor as we go so resume is granular.
        if (i + 1) % PROGRESS_SAVE_EVERY == 0:
            db.set_state(conn, CURSOR_KEY, cursor + i + 1)
            print(f"Progress: {cursor + i + 1}/{len(symbols)} "
                  f"({valid} valid, {processed} ok, {errors} errors)")

    # Persist final cursor for this run.
    new_cursor = cursor + (processed + errors)
    reached_end = new_cursor >= len(symbols) and not rate_limited

    # A full pass that yielded zero real data points to a systemic fetch
    # problem (e.g. upstream blocking this IP range), not per-symbol gaps.
    # Don't mark the backfill complete on that basis — retry the same range.
    if reached_end and valid == 0 and len(chunk) > 0:
        print(f"WARNING: 0/{len(chunk)} symbols returned data this run "
              f"({processed} ok, {errors} errors) — not marking backfill "
              "complete, will retry next run.")
        db.set_state(conn, CURSOR_KEY, cursor)
        return rate_limited, processed, errors, False

    db.set_state(conn, CURSOR_KEY, new_cursor)
    if reached_end:
        db.set_state(conn, PHASE_KEY, "incremental")
        db.set_state(conn, CURSOR_KEY, 0)
        print("Backfill complete → switching to incremental mode.")
    return rate_limited, processed, errors, reached_end


def incremental_step(conn, src, symbols):
    """Fetch the last few days for every symbol. Returns (rate_limited, processed, errors)."""
    print(f"Incremental update for {len(symbols)} symbols")
    processed = errors = 0
    rate_limited = False
    for symbol in symbols:
        try:
            bars = src.fetch_history(symbol, config.INCREMENTAL_LOOKBACK_DAYS)
            _store_symbol(conn, symbol, bars)
            processed += 1
        except RateLimited as exc:
            print(f"Rate limited at {symbol}: {exc}. Stopping; will resume.")
            rate_limited = True
            break
        except Exception:  # noqa: BLE001
            errors += 1
            traceback.print_exc()
    return rate_limited, processed, errors


def evaluate_symbol(cfg, symbol, name, sector, mcap, bars, bm=None):
    """Apply one preset's filters + metrics to a symbol's bars.

    Pure function (no DB) — mirrors the original apply_filters pipeline exactly,
    so it can be unit-tested in isolation. Returns a result row dict or None.
    `bm` is the symbol's precomputed breakout_metrics dict (window-independent).
    """
    if not bars:
        return None
    last_close = bars[-1]["close"]
    avg_vol = sum(b["volume"] for b in bars) / len(bars)
    # Hard filters (cheap) before the metrics math — same order as the original.
    if last_close is None or last_close < cfg["min_price"] or last_close > cfg["max_price"]:
        return None
    if avg_vol < cfg["min_volume"]:
        return None
    if mcap < cfg["min_mcap"] or mcap > cfg["max_mcap"]:
        return None
    bm = bm if bm is not None else breakout_metrics(bars)
    # Breakout-style gates, only enforced by presets that define them.
    if "min_slope_pct_day" in cfg:
        if bm["slope_pct_day"] < cfg["min_slope_pct_day"]:
            return None
        if bm["trend_r2"] < cfg["min_trend_r2"]:
            return None
        if bm["up_day_ratio"] < cfg["min_up_day_ratio"]:
            return None
        if bm["vol_expansion"] < cfg["min_vol_expansion"]:
            return None
        if bm["breakout_age"] is None or bm["breakout_age"] > cfg["max_breakout_age"]:
            return None
    m = calculate_metrics(bars, cfg["period_days"], cfg["stop_percentage"],
                          cfg["period_days"] == 1)
    if m is None:
        return None
    if m["momentum_score"] < cfg.get("min_momentum", -999):
        return None
    if cfg.get("max_volatility", 999) < 999 and m["volatility"] > cfg["max_volatility"]:
        return None
    return {"symbol": symbol, "company_name": name, "sector": sector,
            "market_cap": mcap, **m, **bm}


def compute_results(conn, run_id, symbols):
    """Recompute metrics for every preset from DB data; store scan_results."""
    total_valid = 0
    # Pre-load ticker meta once.
    meta = {}
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, company_name, sector, market_cap FROM tickers")
        for sym, name, sector, mcap in cur.fetchall():
            meta[sym] = (name, sector, mcap or 0)

    # Bulk-load all OHLCV in a single query (far fewer round-trips than per-symbol).
    all_bars = db.load_all_ohlcv(conn)

    # Breakout metrics are window-independent — compute once per symbol,
    # not once per (preset, symbol).
    bm_by_symbol = {s: breakout_metrics(all_bars.get(s) or []) for s in symbols}

    for preset_key, cfg in config.PRESETS.items():
        rows = []
        for symbol in symbols:
            name, sector, mcap = meta.get(symbol, (symbol, "N/A", 0))
            row = evaluate_symbol(cfg, symbol, name, sector, mcap,
                                  all_bars.get(symbol), bm_by_symbol.get(symbol))
            if row is not None:
                rows.append(row)
        db.insert_results(conn, run_id, preset_key, rows)
        total_valid += len(rows)
        print(f"  preset {preset_key}: {len(rows)} matches")

    return total_valid


def run_once():
    start = time.time()
    conn = db.get_conn()
    try:
        db.init_schema(conn)
        symbols = ensure_universe(conn)
        phase = db.get_state(conn, PHASE_KEY, "backfill")
        src = get_source()

        run_id = db.start_run(conn, phase, len(symbols))
        rate_limited = errors = processed = 0
        done = False

        if phase == "backfill":
            rate_limited, processed, errors, done = backfill_step(conn, src, symbols)
        else:
            rate_limited, processed, errors = incremental_step(conn, src, symbols)

        valid = compute_results(conn, run_id, symbols)
        db.prune_old_runs(conn)

        if rate_limited:
            status = "rate_limited"
        elif phase == "backfill" and not done:
            status = "partial"
        else:
            status = "success"

        cursor = int(db.get_state(conn, CURSOR_KEY, 0))
        msg = f"phase={phase} processed={processed} cursor={cursor}/{len(symbols)}"
        db.finish_run(conn, run_id, status, processed, valid, errors, bool(rate_limited), msg)

        print(f"Run done in {(time.time() - start) / 60:.1f} min — status={status}, "
              f"valid={valid}, errors={errors}")
        return status
    finally:
        conn.close()
