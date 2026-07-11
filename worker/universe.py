"""Download the NSE equity universe (main board, EQ series).

Primary source is NSE's official listed-equities CSV. NSE aggressively blocks
datacenter IPs on its main website, but the archives hosts are more lenient;
we try both. If every source fails the caller raises and the run simply
retries next schedule — and once the tickers table is seeded, the universe is
served from the DB and this module is only needed again for a re-seed.
"""

import csv
import io
import time
import urllib.error
import urllib.request

_SOURCES = (
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
)

_RETRIES = 4
_BACKOFF_SECONDS = 5

# Only the regular-market series. BE/BZ are trade-for-trade (no intraday,
# usually illiquid), SM/ST are the SME board — none of them scanner material.
_SERIES_KEEP = {"EQ"}


def _download(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/csv,*/*",
    })
    last_err = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < _RETRIES - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise last_err


def _parse(text):
    """Parse EQUITY_L.csv → list of (symbol, company_name).

    Header columns come with inconsistent whitespace ("NAME OF COMPANY",
    " SERIES", ...), so keys are normalised before lookup.
    """
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        clean = {(k or "").strip().upper(): (v or "").strip()
                 for k, v in row.items()}
        symbol = clean.get("SYMBOL", "")
        series = clean.get("SERIES", "")
        name = clean.get("NAME OF COMPANY", "")
        if not symbol or series not in _SERIES_KEEP:
            continue
        if any(ch in symbol for ch in ("/", "$", "^")):
            continue
        out.append((symbol, name or symbol))
    return out


def get_all_nse_tickers():
    """Return a sorted, de-duplicated list of (symbol, company_name)."""
    last_err = None
    for url in _SOURCES:
        try:
            rows = _parse(_download(url))
            if rows:
                dedup = {sym: name for sym, name in rows}
                return sorted(dedup.items())
            last_err = RuntimeError(f"empty/unparseable universe from {url}")
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            last_err = e
            print(f"universe source failed ({url}): {e}")
    raise RuntimeError(f"all NSE universe sources failed: {last_err}")
