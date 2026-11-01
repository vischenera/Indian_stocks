"""Self-driving scan orchestrator — NSE bhavcopy edition.

Every run performs the same idempotent steps:

  1. Weekly: refresh the universe (EQUITY_L) and industries (index lists).
  2. Ingest every trading day in the window that isn't stored yet, oldest
     first — one bhavcopy file per day covers the whole market. On a fresh
     database that *is* the ~90-trading-day backfill (~145 small downloads,
     a few minutes); after that a run normally fetches just the new day.
  3. Trim to the last WINDOW_DAYS trading days; refresh market caps.
  4. Recompute every preset → scan_results + a scan_runs status row.

If NSE blocks us midway, everything ingested so far is kept and the next run
continues from the first missing day. There is no cursor to babysit: the
`market_days` table records which dates are done (or were closures).
"""

import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import config
import db
from datasource import SourceUnavailable, get_source
from metrics import breakout_metrics, calculate_metrics

PHASE_KEY = "phase"
PROGRESS_KEY = "backfill_cursor"        # trading days stored (dashboard progress)
PROGRESS_TOTAL_KEY = "backfill_total"   # = WINDOW_DAYS
META_KEY = "meta_refreshed_on"
MCAP_KEY = "mcap_date"

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST


def today_ist():
    return datetime.now(IST).date()


# ---------------------------------------------------------------------------
# Metadata: universe, names, industries, market caps
# ---------------------------------------------------------------------------

def refresh_metadata(conn, src, today):
    """Weekly universe/name/industry refresh. Returns a list of problems."""
    last = db.get_state(conn, META_KEY)
    if (last and db.count_tickers(conn)
            and (today - date.fromisoformat(last)).days < config.META_REFRESH_DAYS):
        return []
    problems = []
    try:
        universe = src.fetch_universe()
        db.sync_universe(conn, universe)
        removed = db.prune_tickers(conn, {s for s, _ in universe})
        print(f"Universe: {len(universe)} listed {'/'.join(config.NSE_SERIES)} symbols"
              + (f", {removed} no longer listed removed" if removed else ""))
        db.set_state(conn, META_KEY, today.isoformat())
    except SourceUnavailable as e:
        problems.append(f"universe: {e}")
        print(f"WARNING: {problems[-1]}")
    try:
        industries = src.fetch_industries()
        print(f"Industries: {db.update_sectors(conn, industries)} symbols tagged")
    except SourceUnavailable as e:
        problems.append(f"industries: {e}")
        print(f"WARNING: {problems[-1]}")
    return problems


def refresh_market_caps(conn, src):
    """Market cap = issue size (shares, from NSE's MCAP file) × close.

    Uses the newest PR bundle among the last 3 ingested trading days; skipped
    when already current. Returns a problem string or None.
    """
    current = db.get_state(conn, MCAP_KEY)
    days = db.recent_ok_days(conn, 3)
    for d in days:
        if d.isoformat() == current:
            return None          # already applied; nothing newer published yet
        try:
            caps = src.fetch_market_caps(d)
        except SourceUnavailable as e:
            return f"market caps: {e}"
        if caps is None:
            continue
        closes = db.closes_on(conn, d)
        mcaps, ratios = {}, []
        for sym, (issue, reported) in caps.items():
            close = closes.get(sym)
            if issue and close:
                mcaps[sym] = int(round(issue * close))
                if reported:
                    ratios.append(reported / mcaps[sym])
            elif reported:
                mcaps[sym] = int(round(reported))
        n = db.update_market_caps(conn, mcaps)
        db.set_state(conn, MCAP_KEY, d.isoformat())
        vals = sorted(mcaps.values())
        median = vals[len(vals) // 2] if vals else 0
        print(f"Market caps: {n} symbols from PR bundle {d} "
              f"(median ₹{median / 1e7:,.0f} Cr)")
        if ratios:
            r = sorted(ratios)[len(ratios) // 2]
            if abs(r - 1) > 0.05:
                print(f"WARNING: NSE's own market-cap column differs from issue size × "
                      f"close by a median factor of {r:.4g} — check MCAP units")
        return None
    if not days:
        return None
    return "market caps: no PR bundle for the last 3 trading days"


# ---------------------------------------------------------------------------
# Price ingestion
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    days_ok: int = 0
    rows: int = 0
    holidays: int = 0
    pending: list = field(default_factory=list)   # no file yet, no later file either
    blocked: str = ""                             # NSE unavailable (resumes next run)
    error: str = ""                               # systemic: files not where expected

    @property
    def caught_up(self):
        return not self.blocked and not self.error


def days_to_fetch(known, today):
    """Dates to try this run, oldest first.

    Fresh database → the whole backfill window. Otherwise every date after
    the newest ingested day. Older gaps are never back-filled out of order,
    because split/bonus adjustment assumes days arrive chronologically.
    """
    start = today - timedelta(days=config.BACKFILL_CALENDAR_DAYS)
    ok_days = [d for d, s in known.items() if s == "ok"]
    if ok_days:
        start = max(start, max(ok_days) + timedelta(days=1))
    out, d = [], start
    while d <= today:
        if d not in known:
            out.append(d)
        d += timedelta(days=1)
    return out


def ingest_days(conn, src, today, add_missing_tickers=False):
    res = IngestResult()
    todo = days_to_fetch(db.known_market_days(conn), today)
    if todo:
        print(f"Fetching up to {len(todo)} day(s): {todo[0]} → {todo[-1]}")
    missing_weekdays = 0
    for d in todo:
        try:
            got = src.fetch_bhavcopy(d)
        except SourceUnavailable as e:
            res.blocked = f"{d}: {e}"
            print(f"NSE unavailable at {d}: {e}. Stopping; the next run resumes here.")
            break
        if got is None:
            res.pending.append(d)
            if d.weekday() < 5:
                missing_weekdays += 1
                if missing_weekdays >= config.MAX_CONSECUTIVE_MISSING_WEEKDAYS:
                    res.error = (f"no bhavcopy published for {missing_weekdays} weekdays in a "
                                 f"row ({res.pending[0]} → {d}) — NSE probably changed the "
                                 "file URL; update config.BHAVCOPY_URLS")
                    print(f"ERROR: {res.error}")
                    break
            continue

        rows, source = got
        n, adjusted, skipped = db.ingest_day(
            conn, d, rows, source, add_missing_tickers,
            config.ADJUST_MIN_DEVIATION, config.ADJUST_MAX_SHARE)
        note = ""
        if skipped:
            note = (f" — {len(adjusted)} symbols' previous close didn't match stored "
                    "history (data gap?); back-adjustment skipped for this day")
        elif adjusted:
            note = " — adjusted history for " + ", ".join(
                f"{s} ×{r:.4g}" for s, r in adjusted[:8]) + (" …" if len(adjusted) > 8 else "")
        print(f"  {d}: {n} symbols from {source}{note}")
        # A later day's file exists, so the earlier gaps were market closures.
        db.mark_holidays(conn, res.pending)
        res.holidays += len(res.pending)
        res.pending, missing_weekdays = [], 0
        res.days_ok += 1
        res.rows += n
    if res.pending:
        print(f"Not published yet: {', '.join(map(str, res.pending))} (retried next run)")
    return res


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
    run_id = None
    try:
        db.init_schema(conn)
        src = get_source()
        today = today_ist()
        phase = db.get_state(conn, PHASE_KEY, "backfill")
        run_id = db.start_run(conn, phase, db.count_tickers(conn))
        print(f"Run {run_id}: phase={phase}, today (IST) {today}")

        problems = refresh_metadata(conn, src, today)
        fallback = db.count_tickers(conn) == 0
        if fallback:
            problems.append("universe taken from the bhavcopy (EQUITY_L unavailable)")
        ing = ingest_days(conn, src, today, add_missing_tickers=fallback)
        db.trim_window(conn, config.WINDOW_DAYS)
        mcap_problem = refresh_market_caps(conn, src)
        if mcap_problem:
            problems.append(mcap_problem)
            print(f"WARNING: {mcap_problem}")

        symbols = db.all_symbols(conn)
        valid = compute_results(conn, run_id, symbols)
        db.prune_old_runs(conn)

        stored = len(db.recent_ok_days(conn, config.WINDOW_DAYS))
        if ing.caught_up:
            phase = "incremental"
        db.set_state(conn, PHASE_KEY, phase)
        db.set_state(conn, PROGRESS_KEY, stored)
        db.set_state(conn, PROGRESS_TOTAL_KEY, config.WINDOW_DAYS)

        if ing.error or (ing.blocked and ing.days_ok == 0):
            status = "error"
        elif ing.blocked:
            status = "rate_limited"      # partial progress kept; resumes next run
        else:
            status = "success"

        latest = db.recent_ok_days(conn, 1)
        parts = [f"+{ing.days_ok} day(s)",
                 f"latest {latest[0] if latest else 'none'}",
                 f"{stored}/{config.WINDOW_DAYS} days stored"]
        if ing.pending:
            parts.append(f"awaiting {ing.pending[-1]}")
        if ing.blocked:
            parts.append(f"NSE unavailable {ing.blocked}")
        if ing.error:
            parts.append(ing.error)
        parts += problems
        errors = len(problems) + (1 if ing.blocked or ing.error else 0)
        db.finish_run(conn, run_id, status, ing.days_ok, valid, errors, bool(ing.blocked),
                      " | ".join(parts)[:2000], symbols_total=len(symbols))

        print(f"Run done in {(time.time() - start) / 60:.1f} min — status={status}, "
              f"{src.requests} NSE request(s), valid={valid}, problems={errors}")
        return status
    except Exception:
        conn.rollback()
        if run_id is not None:
            try:
                db.finish_run(conn, run_id, "error", 0, 0, 1, False,
                              traceback.format_exc(limit=3)[-2000:])
            except Exception:  # noqa: BLE001 - never mask the original error
                pass
        raise
    finally:
        conn.close()


def probe(today=None):
    """Check every NSE endpoint from this machine without touching a database.

    Usage: python run.py --probe
    """
    src = get_source()
    today = today or today_ist()
    healthy = True
    print(f"Probing NSE archives (today IST {today})")
    latest = None
    for back in range(10):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            got = src.fetch_bhavcopy(d)
        except SourceUnavailable as e:
            print(f"  bhavcopy {d}: UNAVAILABLE — {e}")
            healthy = False
            break
        if got is None:
            print(f"  bhavcopy {d}: not published")
            continue
        rows, label = got
        print(f"  bhavcopy {d}: OK — {len(rows)} rows from {label}, e.g. {rows[0]}")
        latest = d
        break
    if latest is None:
        healthy = False
    else:
        try:
            caps = src.fetch_market_caps(latest)
            if caps:
                sym = next(iter(caps))
                print(f"  market caps {latest}: OK — {len(caps)} symbols, e.g. {sym} {caps[sym]}")
            else:
                print(f"  market caps {latest}: PR bundle not published")
        except SourceUnavailable as e:
            print(f"  market caps: UNAVAILABLE — {e}")
            healthy = False
    for what, fn in (("universe", src.fetch_universe), ("industries", src.fetch_industries)):
        try:
            print(f"  {what}: OK — {len(fn())} symbols")
        except SourceUnavailable as e:
            print(f"  {what}: UNAVAILABLE — {e}")
            healthy = False
    print("Probe", "passed" if healthy else "FAILED", f"({src.requests} requests)")
    return healthy
