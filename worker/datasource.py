"""NSE end-of-day data source.

Everything comes from NSE's own published files — no third-party feeds:

  * Daily bhavcopy        → OHLCV + previous close for every stock, one file/day
  * Daily PR bundle       → MCAP<DDMMYYYY>.csv: issue size (shares) per stock
  * EQUITY_L.csv          → the universe of listed equities + company names
  * Index constituent CSV → industry per stock (Nifty Total Market / 500)

All parsers locate columns by *header name* (case/whitespace-insensitive,
with aliases), never by position, so a reordered or extended file keeps
working. To change provider (e.g. a broker API), implement the same four
`fetch_*` methods in another class and return it from `get_source()`.

HTTP semantics, relied on by the scan state machine:
  * 404 / 410          → "not published" (holiday, or not out yet) → None
  * 401/403/429/5xx,
    resets, timeouts   → retried with backoff, then SourceUnavailable
"""

import csv
import http.client
import io
import random
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime
from urllib.parse import urlparse

import config


class SourceUnavailable(Exception):
    """NSE blocked/throttled us, is down, or served something unparseable."""


_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/zip,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Network failures worth retrying. URLError/TimeoutError are OSError
# subclasses; a CDN dropping the connection raises RemoteDisconnected (an
# OSError urllib does not wrap) or IncompleteRead (an HTTPException).
_NET_ERRORS = (OSError, http.client.HTTPException)
_NOT_FOUND = (404, 410)
_RETRYABLE = (401, 403, 408, 429, 500, 502, 503, 504)


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

def _norm(name):
    """Header key normaliser: ' Close Price ' -> 'CLOSE PRICE'."""
    return " ".join((name or "").replace("﻿", "").split()).upper()


def _num(v):
    v = (v or "").strip().replace(",", "")
    if v in ("", "-", "NA", "N/A", "NIL"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_date(v):
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%d%m%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _decode(raw):
    return raw.decode("utf-8-sig", errors="replace")


def _csv_rows(text):
    """Yield dicts with normalised keys and stripped values."""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return
    keys = [_norm(h) for h in header]
    for row in reader:
        if not row:
            continue
        yield {k: (row[i].strip() if i < len(row) else "") for i, k in enumerate(keys)}


def _pick(keys, *aliases):
    """First header among `keys` equal to an alias (exact, normalised)."""
    for a in aliases:
        if a in keys:
            return a
    return None


def _pick_containing(keys, *fragments):
    """First header containing any fragment (for verbose headers like 'Market Cap(Rs.)')."""
    for k in keys:
        if any(f in k for f in fragments):
            return k
    return None


def _zip_members(raw):
    """Return [(name, text)] for every CSV in a zip payload."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return [(n, _decode(zf.read(n))) for n in zf.namelist()
                if n.lower().endswith(".csv")]


def _looks_like_html(raw):
    head = raw[:512].lstrip().lower()
    return head.startswith(b"<") or b"<html" in head


# --------------------------------------------------------------------------
# Bhavcopy (both formats share one header-driven parser)
# --------------------------------------------------------------------------

# Aliases cover: UDiFF common bhavcopy (TckrSymb, ClsPric, ...),
# sec_bhavdata_full (SYMBOL, CLOSE_PRICE, ... with padded headers) and the
# pre-2024 cm*bhav.csv layout (SYMBOL, CLOSE, TOTTRDQTY, ...).
_BHAV_COLS = {
    "symbol": ("TCKRSYMB", "SYMBOL"),
    "series": ("SCTYSRS", "SERIES"),
    "open": ("OPNPRIC", "OPEN_PRICE", "OPEN"),
    "high": ("HGHPRIC", "HIGH_PRICE", "HIGH"),
    "low": ("LWPRIC", "LOW_PRICE", "LOW"),
    "close": ("CLSPRIC", "CLOSE_PRICE", "CLOSE"),
    "prev_close": ("PRVSCLSGPRIC", "PREV_CLOSE", "PREVCLOSE"),
    "volume": ("TTLTRADGVOL", "TTL_TRD_QNTY", "TOTTRDQTY"),
    "name": ("FININSTRMNM",),
    "date": ("TRADDT", "DATE1", "TIMESTAMP"),
}
_BHAV_REQUIRED = ("symbol", "series", "open", "high", "low", "close", "volume")


def parse_bhavcopy(text, expected_date=None):
    """Parse a bhavcopy CSV → list of (symbol, name, open, high, low, close,
    prev_close, volume) for the configured series.

    Raises ValueError if required columns are missing or the file is for a
    different trading date than `expected_date`.
    """
    rows = _csv_rows(text)
    first = next(rows, None)
    if first is None:
        raise ValueError("empty file")
    keys = list(first.keys())
    col = {f: _pick(keys, *aliases) for f, aliases in _BHAV_COLS.items()}
    missing = [f for f in _BHAV_REQUIRED if not col[f]]
    if missing:
        raise ValueError(f"missing columns {missing}; header={keys[:12]}")

    out, seen = [], set()
    for r in [first, *rows]:
        if col["date"] and expected_date is not None:
            d = _parse_date(r[col["date"]])
            if d is not None and d != expected_date:
                raise ValueError(f"file is for {d}, expected {expected_date}")
        if r[col["series"]].upper() not in config.NSE_SERIES:
            continue
        sym = r[col["symbol"]].upper()
        if not sym or sym in seen:
            continue
        close = _num(r[col["close"]])
        if close is None or close <= 0:
            continue
        seen.add(sym)
        vol = _num(r[col["volume"]])
        out.append((
            sym,
            (r[col["name"]] or None) if col["name"] else None,
            _num(r[col["open"]]), _num(r[col["high"]]), _num(r[col["low"]]), close,
            _num(r[col["prev_close"]]) if col["prev_close"] else None,
            int(vol) if vol is not None else 0,
        ))
    if not out:
        raise ValueError(f"no {'/'.join(config.NSE_SERIES)} rows")
    return out


# --------------------------------------------------------------------------
# PR bundle → MCAP file
# --------------------------------------------------------------------------

def parse_mcap(text):
    """Parse an MCAP CSV → {symbol: (issue_size_shares|None, market_cap_rs|None)}."""
    rows = list(_csv_rows(text))
    if not rows:
        raise ValueError("empty MCAP file")
    keys = list(rows[0].keys())
    c_sym = _pick(keys, "SYMBOL") or _pick_containing(keys, "SYMBOL")
    c_series = _pick(keys, "SERIES") or _pick_containing(keys, "SERIES")
    c_issue = _pick_containing(keys, "ISSUE SIZE", "ISSUED SHARES", "NO OF SHARES")
    c_mcap = _pick_containing(keys, "MARKET CAP", "MKT CAP", "MCAP")
    if not c_sym or not (c_issue or c_mcap):
        raise ValueError(f"MCAP columns not found; header={keys}")
    out = {}
    for r in rows:
        if c_series and r[c_series].upper() not in config.NSE_SERIES:
            continue
        sym = r[c_sym].upper()
        if not sym:
            continue
        issue = _num(r[c_issue]) if c_issue else None
        mcap = _num(r[c_mcap]) if c_mcap else None
        if issue or mcap:
            out.setdefault(sym, (issue, mcap))
    if not out:
        raise ValueError("MCAP file had no usable rows")
    return out


def _find_mcap_member(members):
    """Pick the MCAP CSV out of a PR bundle: by name, else by header."""
    for name, text in members:
        if name.rsplit("/", 1)[-1].lower().startswith("mcap"):
            return name, text
    for name, text in members:
        header = _norm(text.split("\n", 1)[0])
        if "ISSUE SIZE" in header or "MARKET CAP" in header:
            return name, text
    return None


# --------------------------------------------------------------------------
# Universe + industries
# --------------------------------------------------------------------------

def parse_equity_list(text):
    """Parse EQUITY_L.csv → sorted, de-duplicated [(symbol, company_name)]."""
    out = {}
    for r in _csv_rows(text):
        sym = r.get("SYMBOL", "").upper()
        if not sym or r.get("SERIES", "").upper() not in config.NSE_SERIES:
            continue
        if any(ch in sym for ch in ("/", "$", "^")):
            continue
        out[sym] = r.get("NAME OF COMPANY") or sym
    return sorted(out.items())


def parse_industry_list(text):
    """Parse an index constituent CSV → {symbol: industry}."""
    out = {}
    for r in _csv_rows(text):
        sym = r.get("SYMBOL", "").upper()
        ind = r.get("INDUSTRY", "")
        if sym and ind:
            out[sym] = ind
    return out


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------

class NseArchiveSource:
    """Fetches NSE end-of-day files from the public archive hosts."""

    def __init__(self):
        self._last_request = 0.0
        self.requests = 0

    # ---- HTTP ----

    def _get(self, url):
        """GET → bytes, or None when the file isn't published (404/410).

        Raises SourceUnavailable after retries on blocks, throttling, server
        errors and network failures.
        """
        host = urlparse(url).netloc
        last = None
        for attempt in range(config.REQUEST_RETRIES):
            wait = config.REQUEST_SPACING_SECONDS - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_SECONDS) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code in _NOT_FOUND:
                    return None
                if e.code not in _RETRYABLE:
                    raise SourceUnavailable(f"HTTP {e.code} from {host}") from e
                last = f"HTTP {e.code} from {host}"
            except _NET_ERRORS as e:
                last = f"{type(e).__name__} from {host}: {e}"
            if attempt < config.REQUEST_RETRIES - 1:
                time.sleep(config.BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1))
        raise SourceUnavailable(last)

    # ---- data ----

    def fetch_bhavcopy(self, d: date):
        """Return (rows, source_label) for trading date `d`, or None if no
        source has published a file for that date (holiday / not out yet).

        rows: [(symbol, name, open, high, low, close, prev_close, volume)].
        Raises SourceUnavailable when NSE blocks us or serves junk.
        """
        urls = config.BHAVCOPY_URLS if d.weekday() < 5 else config.BHAVCOPY_URLS[:1]
        problems = []
        for url_t in urls:
            url = url_t.format(d=d)
            raw = self._get(url)
            if raw is None:
                continue
            label = url.rsplit("/", 1)[-1]
            try:
                if raw[:2] == b"PK":
                    members = _zip_members(raw)
                    if not members:
                        raise ValueError("zip has no CSV")
                    text = members[0][1]
                elif _looks_like_html(raw):
                    raise ValueError("got an HTML page, not a CSV (blocked or error page?)")
                else:
                    text = _decode(raw)
                return parse_bhavcopy(text, expected_date=d), label
            except (ValueError, zipfile.BadZipFile, csv.Error) as e:
                problems.append(f"{label}: {e}")
                print(f"  bhavcopy {d}: unusable {label}: {e}")
        if problems:
            # Something answered but nothing parsed — never mistake that for
            # a holiday.
            raise SourceUnavailable("; ".join(problems))
        return None

    def fetch_market_caps(self, d: date):
        """Return {symbol: (issue_size, market_cap)} from the PR bundle for
        `d`, or None if that bundle isn't published."""
        url = config.PR_ZIP_URL.format(d=d)
        raw = self._get(url)
        if raw is None:
            return None
        try:
            found = _find_mcap_member(_zip_members(raw))
            if not found:
                raise ValueError("no MCAP csv in PR bundle")
            return parse_mcap(found[1])
        except (ValueError, zipfile.BadZipFile, csv.Error) as e:
            raise SourceUnavailable(f"PR bundle {d}: {e}") from e

    def _first_parseable(self, urls, parser, minimum, what):
        problems = []
        for url in urls:
            try:
                raw = self._get(url)
            except SourceUnavailable as e:
                problems.append(str(e))
                continue
            if raw is None:
                problems.append(f"404 {url}")
                continue
            try:
                result = parser(_decode(raw))
            except (ValueError, csv.Error) as e:
                problems.append(f"{url}: {e}")
                continue
            if len(result) >= minimum:
                return result
            problems.append(f"{url}: only {len(result)} rows")
        raise SourceUnavailable(f"{what} unavailable: " + "; ".join(problems))

    def fetch_universe(self):
        """[(symbol, company_name)] for every listed equity in the configured series."""
        return self._first_parseable(config.EQUITY_LIST_URLS, parse_equity_list, 500,
                                     "EQUITY_L universe")

    def fetch_industries(self):
        """{symbol: industry} for index constituents (~500–750 stocks)."""
        return self._first_parseable(config.INDUSTRY_LIST_URLS, parse_industry_list, 100,
                                     "industry list")


def get_source():
    """Factory — swap implementation here to change providers."""
    return NseArchiveSource()
