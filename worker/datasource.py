"""Market-data source behind a small interface.

Today: yfinance (NSE symbols are queried with the ".NS" suffix; bare symbols
everywhere else in the pipeline). To swap in another source later (NSE
bhavcopy files, a broker API), implement the same two methods in a new class
and return it from `get_source()`. Nothing else in the worker needs to change.
"""

import random
import threading
import time

import yfinance as yf

from config import (
    BACKOFF_BASE, MIN_REQUEST_INTERVAL, RATE_LIMIT_MARKERS, REQUEST_RETRIES,
    YAHOO_SUFFIX,
)


class RateLimited(Exception):
    """Raised when the upstream signals throttling / auth wall."""


def is_rate_limit_error(exc) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in RATE_LIMIT_MARKERS)


class _GlobalLimiter:
    """Process-wide minimum spacing between requests across all threads."""

    def __init__(self, min_interval):
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            delta = now - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.time()


class YFinanceSource:
    """yfinance-backed implementation."""

    def __init__(self):
        self._limiter = _GlobalLimiter(MIN_REQUEST_INTERVAL)
        self._logged_empty_sample = False

    def _call(self, fn, label=""):
        """Run `fn` with global spacing, retries and exponential backoff.

        Re-raises RateLimited if throttling persists past all retries so the
        caller can stop the run cleanly and resume next schedule.
        """
        last_exc = None
        for attempt in range(REQUEST_RETRIES):
            self._limiter.wait()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - normalise upstream errors
                last_exc = exc
                if is_rate_limit_error(exc):
                    if attempt < REQUEST_RETRIES - 1:
                        wait = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
                        time.sleep(wait)
                        continue
                    raise RateLimited(str(exc)) from exc
                # Non-throttle error: don't retry, treat as "no data", but log
                # it so silent systemic failures (e.g. every symbol failing
                # the same way) are visible instead of looking like empty data.
                print(f"fetch error [{label}]: {type(exc).__name__}: {exc}")
                return None
        # Unreachable: every branch above returns, continues, or raises before
        # the loop can exhaust naturally. Kept only as a defensive fallback.
        raise RuntimeError("unreachable: retry loop exited without returning")

    def fetch_history(self, symbol, days):
        """Return list of (date, open, high, low, close, volume) or [].

        `days` is calendar days; yfinance period string e.g. '90d'.
        """
        def _go():
            hist = yf.Ticker(symbol + YAHOO_SUFFIX).history(period=f"{days}d")
            if hist is None or hist.empty:
                if not self._logged_empty_sample:
                    self._logged_empty_sample = True
                    print(f"Empty history sample [{symbol}]: "
                          f"{'None' if hist is None else repr(hist)}")
                return []
            bars = []
            for ts, row in hist.iterrows():
                bars.append((
                    ts.date(),
                    _f(row.get("Open")), _f(row.get("High")),
                    _f(row.get("Low")), _f(row.get("Close")),
                    _i(row.get("Volume")),
                ))
            return bars

        result = self._call(_go, label=f"history:{symbol}")
        return result or []

    def fetch_info(self, symbol):
        """Return (company_name, sector, market_cap). Best-effort; never raises
        for non-throttle errors."""
        def _go():
            info = yf.Ticker(symbol + YAHOO_SUFFIX).get_info()
            name = info.get("longName") or info.get("shortName") or symbol
            sector = info.get("sector") or "N/A"
            mcap = info.get("marketCap") or 0
            return (name, sector, int(mcap) if mcap else 0)

        result = self._call(_go, label=f"info:{symbol}")
        return result or (symbol, "N/A", 0)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def get_source():
    """Factory — swap implementation here to change providers."""
    return YFinanceSource()
